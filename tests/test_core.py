"""离线回归：排版、接口契约、密钥隔离及完整上传下载流程。"""
import json
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import fitz
import httpx
from fastapi.testclient import TestClient

from paperflow import app as web
import paperflow.translate as bridge
from paperflow.layout import Paragraph, caption_units, collect_paragraphs, find_cjk_font, parse_pages, render_inline_pdf, wrap_lines
from paperflow.translate import Cancelled, Settings, _api_batch, _codex_batch, translate, validate_response


TEXT = "Association of optical tracklets reduces orbital uncertainty and helps identify objects from sparse observations. "


def make_pdf(two_columns=False):
    with fitz.open() as document:
        page = document.new_page(width=600, height=800)
        if two_columns:
            for x in (40, 320):
                page.insert_textbox(fitz.Rect(x, 60, x + 240, 400), TEXT * 6, fontsize=10)
        else:
            page.insert_textbox(fitz.Rect(40, 50, 560, 200), TEXT * 3, fontsize=11)
            page.draw_rect(fitz.Rect(60, 240, 540, 420), color=(.1, .6, .3), width=2)
            page.insert_text((160, 325), "x = V x0", fontsize=16)
        return document.tobytes()


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        self.source.write_bytes(make_pdf())
        with fitz.open(self.source) as document:
            self.paragraphs = collect_paragraphs(document)

    def tearDown(self):
        self.temp.cleanup()

    def test_pages_and_invalid_ranges(self):
        self.assertEqual(parse_pages("1-3，2,5", 6), [0, 1, 2, 4])
        self.assertEqual(parse_pages("", 3), [0, 1, 2])
        for value in ["0", "5-2", "7", "1,,2", "-1", "1-a", "1-999999999999"]:
            with self.assertRaises(ValueError):
                parse_pages(value, 6)

    def test_multicolumn_reading_order_without_losing_content(self):
        with fitz.open(stream=make_pdf(True), filetype="pdf") as document:
            paragraphs = collect_paragraphs(document)
            self.assertEqual(len(paragraphs), 2)
            self.assertLess(paragraphs[0].right, paragraphs[1].left)
            self.assertEqual([p.region for p in paragraphs], [0, 1])
            self.assertEqual(sum(p.text.count("Association") for p in paragraphs), 12)

    def test_native_pdf_long_translation_and_vectors(self):
        output = self.root / "result.pdf"
        self.assertEqual(len(self.paragraphs), 1)
        text = "最优解 x∗ 的光学观测弧段数据关联可以降低轨道确定的不确定性。" * 100 + "末尾哨兵。"
        render_inline_pdf(self.source, output, self.paragraphs, {self.paragraphs[0].id: text})
        with fitz.open(output) as document:
            self.assertGreater(len(document), 1)
            self.assertIn("末尾哨兵", "".join(p.get_text() for p in document))
            self.assertIn("∗", "".join(p.get_text() for p in document))
            self.assertTrue(any(any(d.get("color") and d["color"][1] > .5 for d in p.get_drawings()) for p in document))
            gray = [d for p in document for d in p.get_drawings() if d.get("fill") and .9 < d["fill"][0] < .98]
            self.assertTrue(gray)
            self.assertTrue(all(d["rect"].y1 <= 804.01 for d in gray))

    def test_cjk_width_limit(self):
        font = fitz.Font(fontfile=find_cjk_font())
        lines = wrap_lines("光学轨迹中文English，排版宽度验证。" * 8, font, 10.5, 130)
        self.assertTrue(all(font.text_length(line, fontsize=10.5) <= 130.001 for line in lines))

    def test_font_subsetting_preserves_chinese_and_math_rendering(self):
        for text in ("轨迹数据关联，中文排版。", "最优解 x∗ 的轨迹数据关联。"):
            with self.subTest(text=text):
                translations = {p.id: text for p in self.paragraphs}
                full, compact = self.root / "full-fonts.pdf", self.root / "compact-fonts.pdf"
                with patch.object(fitz.Document, "subset_fonts"):
                    render_inline_pdf(self.source, full, self.paragraphs, translations)
                render_inline_pdf(self.source, compact, self.paragraphs, translations)
                with fitz.open(full) as original, fitz.open(compact) as output:
                    self.assertEqual(len(original), len(output))
                    extracted = "".join(page.get_text() for page in output)
                    self.assertEqual(extracted, "".join(page.get_text() for page in original))
                    # 字体中的空格可能提取为 NBSP，比较文本内容时统一空白。
                    self.assertIn(" ".join(text.split()), " ".join(extracted.split()))
                    for before, after in zip(original, output):
                        self.assertEqual(before.get_pixmap().samples, after.get_pixmap().samples)
                    if "∗" not in text:
                        self.assertFalse(any(font[4] == "PFMath" for page in output for font in page.get_fonts()))

    def test_algorithm_lines_remain_one_original_region(self):
        source = self.root / "algorithm.pdf"
        with fitz.open() as document:
            page = document.new_page(width=600, height=800)
            page.insert_text((40, 60), "Algorithm 1 Association and Uncertainty Pruning", fontsize=12)
            page.insert_text((40, 100), "1: Estimate the uncertainty of the optical tracklets", fontsize=11)
            page.insert_text((40, 160), "2: Find the global minimum using the BFGS algorithm", fontsize=11)
            document.save(source)
        with fitz.open(source) as document:
            paragraphs = collect_paragraphs(document)
            self.assertFalse(any(p.text.startswith(("1:", "2:")) for p in paragraphs))
            block = next(b for b in document[0].get_text("blocks") if b[4].startswith("1:"))
        legacy = Paragraph("legacy", 0, block[1], block[3], block[0], block[2], block[4])
        output = self.root / "algorithm-result.pdf"
        render_inline_pdf(source, output, [legacy], {"legacy": "不应插入算法中间的译文"})
        with fitz.open(output) as document:
            self.assertNotIn("不应插入", "".join(p.get_text() for p in document))

    def test_multi_panel_figure_stays_on_one_output_page(self):
        source = self.root / "panels.pdf"
        with fitz.open() as document:
            page = document.new_page(width=600, height=800)
            page.insert_textbox(fitz.Rect(40, 40, 560, 155), TEXT * 3, fontsize=11)
            for top in (200, 330, 460):
                page.draw_rect(fitz.Rect(65, top, 535, top + 100), color=(1, 0, 0), width=3)
            page.insert_text((40, 590), "Fig. 1 Three panels comparing different observation scenarios", fontsize=10)
            document.save(source)
        with fitz.open(source) as document:
            paragraphs = collect_paragraphs(document)
        translations = {p.id: "不同观测场景的对比。" if p.text.startswith("Fig.") else "观测弧段数据关联分析。" * 65 for p in paragraphs}
        output = self.root / "panels-output.pdf"
        render_inline_pdf(source, output, paragraphs, translations)
        with fitz.open(output) as document:
            pages_with_red = 0
            for page in document:
                pix = page.get_pixmap(matrix=fitz.Matrix(.3, .3), alpha=False)
                samples = pix.samples
                red = sum(r > 180 and g < 80 and b < 80 for r, g, b in zip(samples[::3], samples[1::3], samples[2::3]))
                pages_with_red += red > 20
            self.assertEqual(pages_with_red, 1)

    def test_table_rules_extend_protected_area_below_caption(self):
        with fitz.open() as document:
            page = document.new_page(width=600, height=800)
            page.insert_text((40, 200), "Table 1 Comparison of optical observation scenarios", fontsize=12)
            for y in (230, 270, 430):
                page.draw_line((40, y), (560, y), color=(0, 0, 0))
            units = caption_units(page)
            self.assertEqual(len(units), 1)
            self.assertGreater(units[0][1].y1, 430)
            self.assertLess(units[0][1].y0, 200)

    def test_translation_ids_cannot_duplicate_or_disappear(self):
        batch = [Paragraph("a", 0, 0, 1, 0, 1, TEXT), Paragraph("b", 0, 1, 2, 0, 1, TEXT)]
        for items in [[{"id": "a", "zh": "中文"}] * 2,
                      [{"id": "a", "zh": ""}, {"id": "b", "zh": "中文"}],
                      [{"id": "a", "zh": "中文"}],
                      [{"id": "a", "zh": "中文"}, {"id": "c", "zh": "中文"}]]:
            with self.assertRaises(ValueError):
                validate_response(json.dumps({"translations": items}), batch)
        good = '{"translations":[{"id":"b","zh":"乙"},{"id":"a","zh":"甲"}]}'
        self.assertEqual(validate_response(good, batch), {"a": "甲", "b": "乙"})
        controls = json.dumps({"translations":[{"id":"a","zh":"甲\x07\x08"},{"id":"b","zh":"乙"}]})
        self.assertEqual(validate_response(controls, batch), {"a": "甲", "b": "乙"})

    def test_api_contract_and_no_credentials_in_error(self):
        config = Settings("deepseek", "test-model", "https://provider.example/v1", "private-test-key")
        client = httpx.Client
        captured = []

        def handle(request):
            captured.append(request)
            payload = json.loads(request.content)
            paragraphs = json.loads(payload["messages"][1]["content"])
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            result = {"translations": [{"id": p["id"], "zh": "测试中文。"} for p in paragraphs]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}]})

        with patch("paperflow.translate.httpx.Client", side_effect=lambda **kw: client(transport=httpx.MockTransport(handle), **kw)):
            result = _api_batch(self.paragraphs, config, threading.Event())
        self.assertEqual(result[self.paragraphs[0].id], "测试中文。")
        self.assertEqual(str(captured[0].url), "https://provider.example/v1/chat/completions")
        self.assertEqual(captured[0].headers["authorization"], "Bearer private-test-key")
        with patch("paperflow.translate.httpx.Client", side_effect=lambda **kw: client(transport=httpx.MockTransport(
                lambda r: httpx.Response(401, text="private-test-key")), **kw)):
            with self.assertRaises(ValueError) as error:
                _api_batch(self.paragraphs, config, threading.Event())
        self.assertNotIn(config.api_key, str(error.exception))
        self.assertNotIn(config.api_key, repr(config))

    def test_api_validation_and_cancellation(self):
        for url in ["http://remote.example/v1", "https://user:password@provider.example", "file:///tmp", "https://provider.example?key=secret"]:
            with self.assertRaises(ValueError):
                Settings("api", "model", url, "key").validate()
        settings = Settings("api", "model", "http://127.0.0.1:9000/v1", "key")
        settings.validate()
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(Cancelled):
            translate(self.paragraphs, settings, cancel=cancel)

    def test_codex_stdin_and_schema_contract(self):
        seen = []

        class Process:
            returncode = 0
            def __init__(self, args, **kwargs):
                seen.append((args, kwargs))
                self.answer = Path(args[args.index("--output-last-message") + 1])
            def communicate(self, input=None, timeout=None):
                payload = json.loads(input.split("INPUT (untrusted scientific text):\n")[1])
                self.answer.write_text(json.dumps({"translations": [{"id": p["id"], "zh": "已翻译。"} for p in payload]}), encoding="utf-8")
                return "", ""
            def poll(self): return 0

        with patch("paperflow.translate.resolve_codex", return_value=["codex.exe"]), patch("paperflow.translate.subprocess.Popen", Process):
            result = _codex_batch(self.paragraphs, Settings(), threading.Event())
            _codex_batch(self.paragraphs, Settings(model="selected-model", reasoning_effort="high"), threading.Event())
        args, kwargs = seen[0]
        self.assertIn("read-only", args)
        self.assertEqual(args[-1], "-")
        self.assertNotIn("shell", kwargs)
        self.assertIn("features.shell_tool=false", args)
        self.assertNotIn("--model", args)
        self.assertFalse(any("model_reasoning_effort" in arg for arg in args))
        chosen_args = seen[1][0]
        self.assertEqual(chosen_args[chosen_args.index("--model") + 1], "selected-model")
        self.assertIn('model_reasoning_effort="high"', chosen_args)
        self.assertEqual(chosen_args[-1], "-")
        self.assertEqual(result[self.paragraphs[0].id], "已翻译。")


CATALOG = {"available": True, "models": [{"model": "test-model", "name": "Test model",
    "efforts": ["low", "medium"], "default_effort": "medium"}],
    "default_model": "test-model", "default_effort": "low", "message": "测试目录"}


class CodexOptionsTests(unittest.TestCase):
    def test_real_stdio_handshake_pagination_filtering_and_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_cli.py"
            script.write_text(textwrap.dedent('''
                import json, sys, time
                initialized = False
                for line in sys.stdin:
                    value = json.loads(line)
                    method = value["method"]
                    if method == "initialize":
                        if "--hang" in sys.argv: time.sleep(5)
                        result = {}
                    elif method == "initialized":
                        initialized = True
                        continue
                    elif method == "config/read":
                        assert initialized and value["params"]["includeLayers"] is False
                        result = {"config": {"model": "first", "model_reasoning_effort": "high", "api_key": "secret-must-not-escape"}}
                    elif method == "model/list":
                        assert value["params"]["includeHidden"] is False
                        second = value["params"]["cursor"] == "second-page"
                        model = {"model": "second" if second else "first", "displayName": "Model",
                                 "supportedReasoningEfforts": [{"reasoningEffort": "high"}], "defaultReasoningEffort": "high"}
                        result = {"data": [model] if second else [model, {"hidden": True}], "nextCursor": None if second else "second-page"}
                    else:
                        raise AssertionError("不应启动对话或写配置")
                    print(json.dumps({"method": "notification", "params": {}}), flush=True)
                    print(json.dumps({"id": value["id"], "result": result}), flush=True)
            '''), encoding="utf-8")
            with patch.object(bridge, "resolve_codex", return_value=[sys.executable, "-u", str(script)]):
                result = bridge._read_codex_options()
            self.assertEqual([m["model"] for m in result["models"]], ["first", "second"])
            self.assertEqual(result["default_effort"], "high")
            self.assertNotIn("secret-must-not-escape", json.dumps(result))
            with patch.object(bridge, "resolve_codex", return_value=[sys.executable, "-u", str(script), "--hang"]):
                started = time.monotonic()
                with self.assertRaisesRegex(ValueError, "超时"):
                    bridge._read_codex_options(timeout=.1)
                self.assertLess(time.monotonic() - started, 3)

    def test_catalog_cache_refresh_and_safe_fallback(self):
        with patch.object(bridge, "_catalog_cache", None), patch.object(bridge, "_catalog_expires", 0):
            with patch.object(bridge, "_read_codex_options", return_value=CATALOG) as read:
                self.assertEqual(bridge.codex_options(), CATALOG)
                self.assertEqual(bridge.codex_options(), CATALOG)
                self.assertEqual(read.call_count, 1)
                bridge.codex_options(refresh=True)
                self.assertEqual(read.call_count, 2)
            with patch.object(bridge, "_read_codex_options", side_effect=OSError("sensitive-cli-error")):
                result = bridge.codex_options(refresh=True)
            self.assertFalse(result["available"])
            self.assertNotIn("sensitive-cli-error", json.dumps(result))

    def test_model_effort_validation_and_custom_model_escape_hatch(self):
        with patch.object(bridge, "resolve_codex", return_value=["codex.exe"]), patch.object(bridge, "codex_options", return_value=CATALOG):
            Settings(model="test-model", reasoning_effort="medium").validate()
            Settings(reasoning_effort="low").validate()
            Settings(model="custom-provider-model", reasoning_effort="high").validate()
            for model in ("test-model", ""):
                with self.assertRaisesRegex(ValueError, "不支持"):
                    Settings(model=model, reasoning_effort="ultra").validate()
            with self.assertRaisesRegex(ValueError, "无效"):
                Settings(reasoning_effort='high"; bad=1').validate()
            with self.assertRaisesRegex(ValueError, "仅用于本地 Codex"):
                Settings(provider="api", reasoning_effort="low").validate()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.patches = [patch.object(web, "JOBS", self.root), patch.object(web, "jobs", {}),
                        patch.object(web, "cancellations", {}), patch.object(web, "executor", self.executor)]
        for item in self.patches: item.start()
        self.client_context = TestClient(web.app, base_url="http://127.0.0.1")
        self.client = self.client_context.__enter__()
        self.headers = {"X-PaperFlow": "1"}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.executor.shutdown(wait=True)
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_local_boundary_and_upload_validation(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/health").json()["service"], "ok")
        self.assertEqual(self.client.get("/", headers={"Host": "external.example"}).status_code, 400)
        self.assertEqual(self.client.post("/api/check", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/check", headers={**self.headers, "Origin": "https://evil.example"}, json={}).status_code, 403)
        oversized = self.client.post("/api/translate", headers={**self.headers, "Content-Length": str(web.MAX_BYTES + 2*1024*1024)})
        self.assertEqual(oversized.status_code, 413)
        self.assertIn(str(web.MAX_UPLOAD_MB), oversized.json()["detail"])
        data = {"provider": "api", "model": "test", "api_key": "test-key", "base_url": "https://example.com/v1"}
        for file in [("bad.txt", b"x", "text/plain"), ("bad.pdf", b"invalid", "application/pdf")]:
            self.assertEqual(self.client.post("/api/translate", files={"file":file}, data=data, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.get("/api/jobs").json(), [])

    def test_upload_translate_preview_download_and_persistence(self):
        def fake_translate(paragraphs, settings, progress=None, cancel=None):
            result = {p.id: "【测试】轨迹数据关联可以降低轨道确定的不确定性。" for p in paragraphs}
            if progress: progress(len(result), len(result), result)
            return result
        data = {"provider": "api", "model": "test", "api_key": "private-never-save-this", "base_url": "https://example.com/v1", "pages": "1"}
        with patch.object(web, "translate", fake_translate):
            response = self.client.post("/api/translate", headers=self.headers, data=data,
                                        files={"file": ("示例论文.pdf", make_pdf(), "application/pdf")})
            self.assertEqual(response.status_code, 200, response.text)
            job_id = response.json()["id"]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                job = self.client.get(f"/api/jobs/{job_id}").json()
                if job["status"] in web.TERMINAL: break
                time.sleep(.05)
            self.assertEqual(job["status"], "completed", job)
        pdf = self.client.get(job["url"] + "?download=true")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertIn("attachment", pdf.headers["content-disposition"])
        info = self.client.get(job["url"] + "/info").json()
        self.assertGreaterEqual(info["pages"], 1)
        preview = self.client.get(job["url"] + "/preview/1")
        self.assertEqual(preview.headers["content-type"], "image/png")
        self.assertEqual(self.client.get(job["url"] + "/preview/999").status_code, 404)
        for path in self.root.glob("*/*.json"):
            self.assertNotIn("private-never-save-this", path.read_text(encoding="utf-8"))
        with fitz.open(stream=pdf.content, filetype="pdf") as document:
            self.assertIn("不确定性", "".join(p.get_text() for p in document))

    def test_large_pdf_upload_is_accepted_without_calling_translation(self):
        data = {"provider": "api", "model": "test", "api_key": "test-key", "base_url": "https://example.com/v1"}
        original = make_pdf()
        # PDF 尾部注释把有效单页样本扩展到 60 MB，真实走 multipart 和落盘流程。
        content = original + b"\n%" + b" " * (60 * 1024 * 1024 - len(original) - 3) + b"\n"
        with patch.object(web.executor, "submit") as submit:
            response = self.client.post("/api/translate", headers=self.headers, data=data,
                files={"file": ("large.pdf", content, "application/pdf")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual((self.root / response.json()["id"] / "source.pdf").stat().st_size, len(content))

    def test_upload_size_boundary_and_server_limit_are_consistent(self):
        data = {"provider": "api", "model": "test", "api_key": "test-key", "base_url": "https://example.com/v1"}
        original = make_pdf()
        limit = 1024 * 1024
        content = original + b"\n%" + b" " * (limit - len(original) - 3) + b"\n"
        with patch.object(web, "MAX_UPLOAD_MB", 1), patch.object(web, "MAX_BYTES", limit), \
                patch.object(web.executor, "submit") as submit:
            health = self.client.get("/api/health").json()
            self.assertEqual((health["max_mb"], health["max_pages"]), (1, 150))
            accepted = self.client.post("/api/translate", headers=self.headers, data=data,
                files={"file": ("boundary.pdf", content, "application/pdf")})
            rejected = self.client.post("/api/translate", headers=self.headers, data=data,
                files={"file": ("oversized.pdf", content + b"\n", "application/pdf")})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(rejected.status_code, 413, rejected.text)
        self.assertIn("1 MB", rejected.json()["detail"])
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(len(self.client.get("/api/jobs").json()), 1)
        self.assertEqual(len(list(self.root.glob("*/source.pdf"))), 1)

    def test_restart_marks_unfinished_job(self):
        job_id = "a" * 32
        directory = self.root / job_id; directory.mkdir()
        (directory / "status.json").write_text(json.dumps({"id":job_id,"status":"translating","created":1}), encoding="utf-8")
        with TestClient(web.app, base_url="http://127.0.0.1") as client:
            self.assertEqual(client.get(f"/api/jobs/{job_id}").json()["status"], "interrupted")

    def test_scanned_upload_formula_protection_and_saved_analysis(self):
        from test_pdf_features import feature_pdf, scan_pdf
        from paperflow.pdf_input import ocr_available
        if not ocr_available():
            self.skipTest("当前环境未安装本地 OCR 模型")
        original = scan_pdf(feature_pdf())

        def fake_translate(paragraphs, settings, progress=None, cancel=None):
            result = {p.id: "扫描排版验证 " + " ".join(f["token"] for f in p.formulas) for p in paragraphs}
            if progress: progress(len(result), len(result), result)
            return result

        data = {"provider": "api", "model": "test", "base_url": "https://example.com/v1", "api_key": "test-only"}
        with patch.object(web, "translate", fake_translate):
            response = self.client.post("/api/translate", headers=self.headers, data=data,
                files={"file": ("scan.pdf", original, "application/pdf")})
            self.assertEqual(response.status_code, 200, response.text)
            job_id = response.json()["id"]
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                job = self.client.get(f"/api/jobs/{job_id}").json()
                if job["status"] in web.TERMINAL: break
                time.sleep(.1)
            self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["analysis"]["ocr_pages"], [1])
        self.assertEqual(job["analysis"]["double_column_pages"], [1])
        self.assertGreaterEqual(job["analysis"]["formula_count"], 2)
        self.assertTrue(job["analysis"]["warnings"])
        directory = self.root / job_id
        self.assertEqual((directory / "source.pdf").read_bytes(), original)
        self.assertTrue((directory / "prepared.pdf").is_file())
        self.assertTrue((directory / "analysis.json").is_file())
        self.assertEqual(self.client.get(job["url"]).status_code, 200)

    def test_codex_catalog_and_check_forward_selected_controls(self):
        with patch.object(web, "codex_options", return_value=CATALOG) as options:
            self.assertEqual(self.client.get("/api/codex/options?refresh=true").json(), CATALOG)
            options.assert_called_once_with(refresh=True)
        with patch.object(web, "translate", return_value={"test": "测试译文"}) as translate_check:
            response = self.client.post("/api/check", headers=self.headers,
                json={"provider": "codex", "model": "test-model", "reasoning_effort": "medium"})
        self.assertEqual(response.status_code, 200)
        settings = translate_check.call_args.args[1]
        self.assertEqual((settings.model, settings.reasoning_effort), ("test-model", "medium"))

    def test_codex_upload_persists_controls_and_rejects_unsupported_effort(self):
        data = {"provider": "codex", "model": "test-model", "reasoning_effort": "low",
                "columns": "double", "ocr": "off", "protect_formulas": "false"}
        with patch.object(bridge, "resolve_codex", return_value=["codex.exe"]), patch.object(bridge, "codex_options", return_value=CATALOG), patch.object(web.executor, "submit") as submit:
            response = self.client.post("/api/translate", headers=self.headers, data=data,
                files={"file": ("controls.pdf", make_pdf(), "application/pdf")})
            self.assertEqual(response.status_code, 200, response.text)
            job_id = response.json()["id"]
            saved = json.loads((self.root / job_id / "status.json").read_text(encoding="utf-8"))
            self.assertEqual((saved["model"], saved["reasoning_effort"]), ("test-model", "low"))
            self.assertEqual((saved["columns"], saved["ocr"], saved["protect_formulas"]), ("double", "off", False))
            self.assertEqual(submit.call_args.args[-3:], ("double", "off", False))
            settings = submit.call_args.args[2]
            self.assertEqual((settings.model, settings.reasoning_effort), ("test-model", "low"))
            response = self.client.post("/api/translate", headers=self.headers,
                data={**data, "reasoning_effort": "ultra"},
                files={"file": ("controls.pdf", make_pdf(), "application/pdf")})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(submit.call_count, 1)
            response = self.client.post("/api/translate", headers=self.headers, data={**data, "ocr": "invalid"},
                files={"file": ("controls.pdf", make_pdf(), "application/pdf")})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(submit.call_count, 1)


if __name__ == "__main__":
    unittest.main()
