"""续译、人工校订和保留旧成品的离线集成回归。"""
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import fitz
from fastapi.testclient import TestClient

from paperflow import app as web
from paperflow import pdf_input
from paperflow.translate import translate
from test_core import make_pdf
from test_pdf_features import feature_pdf


class JobWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary_root = web.ROOT / "output" / "test-temp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="job-workflows-", dir=temporary_root)
        self.root = Path(self.temp.name)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.patches = [patch.object(web, "JOBS", self.root), patch.object(web, "jobs", {}),
                        patch.object(web, "cancellations", {}), patch.object(web, "executor", self.executor),
                        patch.object(pdf_input, "layout_model_path", return_value=None)]
        for item in self.patches:
            item.start()
        self.client_context = TestClient(web.app, base_url="http://127.0.0.1")
        self.client = self.client_context.__enter__()
        self.headers = {"X-PaperFlow": "1"}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.executor.shutdown(wait=True)
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def submit(self, pdf=None, **options):
        data = {"provider": "api", "model": "test-model", "base_url": "https://example.com/v1",
                "api_key": "first-key-must-not-persist", "columns": "double", "ocr": "off",
                "glossary": "track=轨迹", "context": "轨道确定", **options}
        response = self.client.post("/api/translate", headers=self.headers, data=data,
                                    files={"file": ("workflow.pdf", pdf or make_pdf(True), "application/pdf")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def wait_terminal(self, job_id):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            if job["status"] in web.TERMINAL and job_id not in web.cancellations:
                return job
            time.sleep(.03)
        self.fail(f"任务未结束：{job}")

    @staticmethod
    def machine_translation(paragraphs, settings, progress=None, cancel=None, *, cached=None):
        result = dict(cached or {})
        result.update({p.id: "机器原始译文，轨迹关联。 " + " ".join(f["token"] for f in p.formulas)
                       for p in paragraphs if p.id not in result})
        if progress:
            progress(len(result), len(paragraphs), result)
        return result

    @staticmethod
    def fail_after_first(paragraphs, settings, progress=None, cancel=None, *, cached=None):
        result = {paragraphs[0].id: "已保存的第一段译文。"}
        if progress:
            progress(1, len(paragraphs), result)
        raise ValueError("API timeout " + settings.api_key)

    def completed_job(self, pdf=None):
        with patch.object(web, "translate", self.machine_translation):
            job_id = self.submit(pdf)
            job = self.wait_terminal(job_id)
            self.assertEqual(job["status"], "completed", job)
        return job_id

    def editor(self, job_id):
        response = self.client.get(f"/api/jobs/{job_id}/editor")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def export(self, job_id, payload):
        response = self.client.post(f"/api/jobs/{job_id}/export", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], job_id)
        return self.wait_terminal(job_id)

    def pdf(self, job_id):
        response = self.client.get(f"/api/jobs/{job_id}/pdf?download=true")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("attachment", response.headers["content-disposition"])
        return response.content

    @staticmethod
    def pdf_text(content):
        with fitz.open(stream=content, filetype="pdf") as document:
            return "".join(page.get_text() for page in document)

    def test_resume_reuses_finished_paragraphs_and_never_saves_either_key(self):
        with patch.object(web, "translate", self.fail_after_first):
            job_id = self.submit()
            initial = self.wait_terminal(job_id)
        self.assertEqual(initial["status"], "error", initial)
        self.assertEqual(initial["done"], 1)
        self.assertIn("[已隐藏]", initial["message"])
        directory = self.root / job_id
        cached = json.loads((directory / "translations.json").read_text(encoding="utf-8"))
        all_paragraphs = json.loads((directory / "paragraphs.json").read_text(encoding="utf-8"))
        self.assertGreater(len(all_paragraphs), len(cached))
        requested = []

        def provider(batch, settings, cancel):
            self.assertEqual(settings.api_key, "second-key-must-not-persist")
            self.assertEqual((settings.glossary, settings.context), ("track=轨迹", "轨道确定"))
            requested.extend(p.id for p in batch)
            return {p.id: "续译补充段落。" for p in batch}

        # 发行包通常没有独立 .py 文件，新版缓存不能触发旧源码指纹读取。
        with patch.object(web, "translate", translate), patch("paperflow.translate._api_batch", side_effect=provider), \
                patch.object(web, "ROOT", self.root / "missing-source-code"):
            response = self.client.post(f"/api/jobs/{job_id}/resume", headers=self.headers,
                                        json={"api_key": "second-key-must-not-persist"})
            self.assertEqual(response.status_code, 200, response.text)
            finished = self.wait_terminal(job_id)
        self.assertEqual(finished["status"], "completed", finished)
        self.assertEqual(set(requested), {p["id"] for p in all_paragraphs} - set(cached))
        self.assertEqual(finished["cached"], 1)
        translated = json.loads((directory / "translations.json").read_text(encoding="utf-8"))
        for key, value in cached.items():
            self.assertEqual(translated[key], value)
        self.assertIn("续译补充段落", self.pdf_text(self.pdf(job_id)))
        for path in directory.rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("first-key-must-not-persist", text)
            self.assertNotIn("second-key-must-not-persist", text)

    def test_editor_export_and_restore_preserve_machine_files(self):
        job_id = self.completed_job()
        directory = self.root / job_id
        original_pdf = (directory / "bilingual.pdf").read_bytes()
        original_translations = (directory / "translations.json").read_bytes()
        editor = self.editor(job_id)
        self.assertTrue(all(p["page"] >= 1 for p in editor["paragraphs"]))
        self.assertTrue(all("text" in p and "formulas" in p for p in editor["paragraphs"]))
        translations = {p["id"]: p["zh"] for p in editor["paragraphs"]}
        changed_id = editor["paragraphs"][0]["id"]
        translations[changed_id] = "人工校订后的观测弧段关联，核对完成。"
        with patch.object(web, "translate") as provider:
            job = self.export(job_id, {"translations": translations, "font_size": 11,
                                       "revision": editor["revision"]})
            self.assertEqual(job["status"], "completed", job)
            self.assertIn("人工校订后的观测弧段关联", self.pdf_text(self.pdf(job_id)))
            edited = self.editor(job_id)
            self.assertEqual(edited["font_size"], 11)
            self.assertEqual(next(p["zh"] for p in edited["paragraphs"] if p["id"] == changed_id), translations[changed_id])
            stale = self.client.post(f"/api/jobs/{job_id}/export", headers=self.headers,
                                      json={"translations": translations, "revision": editor["revision"]})
            self.assertEqual(stale.status_code, 409, stale.text)
            restored = self.export(job_id, {"restore": True, "revision": edited["revision"]})
            self.assertEqual(restored["status"], "completed", restored)
            self.assertIn("机器原始译文", self.pdf_text(self.pdf(job_id)))
            self.assertNotIn("人工校订后的", self.pdf_text(self.pdf(job_id)))
            provider.assert_not_called()
        self.assertEqual((directory / "bilingual.pdf").read_bytes(), original_pdf)
        self.assertEqual((directory / "translations.json").read_bytes(), original_translations)

    def test_modified_source_or_glossary_prevents_cache_reuse(self):
        for changed in ("source", "glossary"):
            with self.subTest(changed=changed), patch.object(web, "translate", self.fail_after_first):
                job_id = self.submit()
                self.assertEqual(self.wait_terminal(job_id)["status"], "error")
            directory = self.root / job_id
            before = (directory / "translations.json").read_bytes()
            if changed == "source":
                with (directory / "source.pdf").open("ab") as stream:
                    stream.write(b"\n% changed source\n")
            else:
                web.update_job(job_id, glossary="track=跟踪")
            with patch.object(web, "translate") as provider:
                response = self.client.post(f"/api/jobs/{job_id}/resume", headers=self.headers,
                                            json={"api_key": "second-key-must-not-persist"})
                if response.status_code == 200:
                    job = self.wait_terminal(job_id)
                    self.assertEqual(job["status"], "error", job)
                    message = job["message"]
                else:
                    self.assertIn(response.status_code, (400, 409), response.text)
                    message = response.json()["detail"]
                self.assertRegex(message, "改变|变化|缓存")
                provider.assert_not_called()
            self.assertEqual((directory / "translations.json").read_bytes(), before)

    def test_formula_removal_is_rejected_without_changing_pdf(self):
        job_id = self.completed_job(feature_pdf())
        original = self.pdf(job_id)
        editor = self.editor(job_id)
        protected = next(p for p in editor["paragraphs"] if p["formulas"])
        translations = {p["id"]: p["zh"] for p in editor["paragraphs"]}
        translations[protected["id"]] = "不小心删除了公式。"
        response = self.client.post(f"/api/jobs/{job_id}/export", headers=self.headers,
                                    json={"translations": translations, "revision": editor["revision"]})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("公式", response.json()["detail"])
        self.assertEqual(self.pdf(job_id), original)

    def test_failed_export_keeps_previous_pdf_and_retains_unsaved_draft(self):
        job_id = self.completed_job()
        original = self.pdf(job_id)
        editor = self.editor(job_id)
        translations = {p["id"]: "不应覆盖旧成品。" for p in editor["paragraphs"]}
        with patch.object(web, "render_inline_pdf", side_effect=OSError("模拟磁盘写入失败")), \
                patch.object(web, "translate") as provider:
            job = self.export(job_id, {"translations": translations, "revision": editor["revision"]})
            provider.assert_not_called()
        self.assertEqual(self.pdf(job_id), original)
        self.assertIn("失败", job["message"])
        current = self.editor(job_id)
        self.assertEqual(current["revision"], editor["revision"])
        self.assertTrue(current["draft"])
        self.assertEqual({p["id"]: p["zh"] for p in current["paragraphs"]}, translations)

    def test_complete_translation_cache_can_resume_rendering_without_key(self):
        with patch.object(web, "translate", self.machine_translation), \
                patch.object(web, "render_inline_pdf", side_effect=OSError("模拟首次排版失败")):
            job_id = self.submit()
            initial = self.wait_terminal(job_id)
        self.assertEqual(initial["status"], "error", initial)
        self.assertEqual(initial["error_stage"], "rendering")
        self.assertEqual(initial["done"], initial["total"])
        with patch.object(web, "translate") as provider:
            response = self.client.post(f"/api/jobs/{job_id}/resume", headers=self.headers, json={})
            self.assertEqual(response.status_code, 200, response.text)
            finished = self.wait_terminal(job_id)
            self.assertEqual(finished["status"], "completed", finished)
            provider.assert_not_called()
        self.assertIn("机器原始译文", self.pdf_text(self.pdf(job_id)))

    def test_codex_defaults_are_frozen_and_irrelevant_credentials_not_saved(self):
        catalog = {"available": True, "default_model": "test-model", "default_effort": "high",
                   "models": [{"model": "test-model", "efforts": ["high"], "default_effort": "high"}]}
        with patch.object(web, "codex_options", return_value=catalog), \
                patch("paperflow.translate.codex_options", return_value=catalog), \
                patch("paperflow.translate.resolve_codex", return_value=["codex.exe"]), \
                patch.object(self.executor, "submit"):
            job_id = self.submit(provider="codex", model="", reasoning_effort="",
                                 base_url="https://example.com/?key=irrelevant-secret")
        job = self.client.get(f"/api/jobs/{job_id}").json()
        self.assertEqual((job["model"], job["reasoning_effort"]), ("test-model", "high"))
        self.assertEqual(job["base_url"], "")
        saved = (self.root / job_id / "status.json").read_text(encoding="utf-8")
        self.assertNotIn("irrelevant-secret", saved)
        self.assertNotIn("first-key-must-not-persist", saved)

    def test_autosave_preserves_unfinished_text_and_rejects_stale_window(self):
        job_id = self.completed_job(feature_pdf())
        original_pdf = self.pdf(job_id)
        editor = self.editor(job_id)
        values = {p["id"]: p["zh"] for p in editor["paragraphs"]}
        protected = next(p for p in editor["paragraphs"] if p["formulas"])
        values[protected["id"]] = ""
        payload = {"translations": values, "font_size": 12, "revision": 0, "draft_version": 0}
        response = self.client.post(f"/api/jobs/{job_id}/draft", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["draft_version"], 1)
        loaded = self.editor(job_id)
        self.assertTrue(loaded["draft"])
        self.assertEqual(loaded["draft_version"], 1)
        self.assertEqual(next(p["zh"] for p in loaded["paragraphs"] if p["id"] == protected["id"]), "")
        conflict = self.client.post(f"/api/jobs/{job_id}/draft", headers=self.headers, json=payload)
        self.assertEqual(conflict.status_code, 409)
        exported = self.client.post(f"/api/jobs/{job_id}/export", headers=self.headers,
                                   json={**payload, "draft_version": 1})
        self.assertEqual(exported.status_code, 400)
        self.assertEqual(self.pdf(job_id), original_pdf)

    def test_cache_uses_semantic_versions_and_safely_migrates_legacy(self):
        job_id = self.completed_job()
        job = web.get_job(job_id)
        directory = self.root / job_id
        settings = web.Settings(provider=job["provider"], model=job["model"], base_url=job["base_url"],
                                glossary=job["glossary"], context=job["context"])
        args = (settings, [0], "double", "off", True)
        fingerprint = web.cache_fingerprint(*args)
        with patch.object(web, "file_hash") as hash_code:
            self.assertEqual(web.cache_fingerprint(*args), fingerprint)
            hash_code.assert_not_called()
        with patch.object(web, "PROMPT_VERSION", web.PROMPT_VERSION + 1):
            self.assertNotEqual(web.cache_fingerprint(*args), fingerprint)
        manifest = web.read_json(directory / "cache.json")
        legacy = web.cache_fingerprint(*args, legacy=True)
        manifest.pop("schema")
        manifest["fingerprint"] = legacy
        web.write_json(directory / "cache.json", manifest)
        web.cached_inputs(directory, fingerprint, legacy=legacy)
        self.assertEqual(web.read_json(directory / "cache.json")["schema"], web.CACHE_SCHEMA)
        self.assertEqual(web.read_json(directory / "cache-v1.json"), manifest)
        settings.glossary = "track=其他术语"
        with self.assertRaisesRegex(ValueError, "变化"):
            web.cached_inputs(directory, web.cache_fingerprint(*args))

    def test_preflight_trial_then_full_translation_reuses_trial_without_model_in_preflight(self):
        with fitz.open(stream=make_pdf(), filetype="pdf") as source, fitz.open() as document:
            document.insert_pdf(source)
            document.insert_pdf(source)
            original = document.tobytes()
        with patch.object(web, "translate") as translate_call, patch.object(web, "pin_codex_defaults") as pin:
            job_id = self.submit(original, preflight="true", api_key="", model="", base_url="", columns="single")
            ready = self.wait_terminal(job_id)
            self.assertEqual(ready["status"], "ready", ready)
            translate_call.assert_not_called()
            pin.assert_not_called()
        report = self.client.get(f"/api/jobs/{job_id}/preflight").json()
        self.assertEqual([p["page"] for p in report["pages"]], [1, 2])
        self.assertTrue(all(p["paragraphs"] > 0 for p in report["pages"]))
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}/source/preview/1").headers["content-type"], "image/png")
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}/source/preview/3").status_code, 404)
        settings = {"provider": "api", "model": "test-model", "base_url": "https://example.com/v1",
                    "api_key": "trial-key-not-saved", "glossary": "track=轨迹"}
        requested = []
        def provider(batch, options, cancel):
            requested.extend(p.id for p in batch)
            return {p.id: "试译与续译验证。 " + " ".join(f["token"] for f in p.formulas) for p in batch}
        with patch("paperflow.translate._api_batch", side_effect=provider):
            result = self.client.post(f"/api/jobs/{job_id}/start", headers=self.headers, json={**settings, "trial_page": 1})
            self.assertEqual(result.status_code, 200, result.text)
            trial = self.wait_terminal(job_id)
            self.assertEqual(trial["status"], "trial_ready", trial)
            trial_pdf = self.client.get(trial["url"]).content
            self.assertTrue(trial_pdf.startswith(b"%PDF"))
            self.assertEqual(self.client.get(trial["url"] + "/preview/1").headers["content-type"], "image/png")
            first_requested = list(requested)
            changed = self.client.post(f"/api/jobs/{job_id}/start", headers=self.headers, json={**settings, "model": "changed"})
            self.assertEqual(changed.status_code, 400, changed.text)
            result = self.client.post(f"/api/jobs/{job_id}/start", headers=self.headers, json=settings)
            self.assertEqual(result.status_code, 200, result.text)
            final = self.wait_terminal(job_id)
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(len(requested), len(set(requested)))
        self.assertEqual(final["cached"], len(first_requested))
        self.assertEqual(final["document_done"], len(report["paragraphs"]))
        self.assertEqual((self.root / job_id / "source.pdf").read_bytes(), original)
        self.assertEqual((self.root / job_id / "trial.pdf").read_bytes(), trial_pdf)
        for path in (self.root / job_id).glob("*.json"):
            self.assertNotIn("trial-key-not-saved", path.read_text(encoding="utf-8"))

    def test_preflight_can_resume_without_credentials_and_unknown_codex_complete_cache_can_render(self):
        with patch.object(web, "prepare_pdf", side_effect=ValueError("模拟预检失败")):
            job_id = self.submit(preflight="true", api_key="")
            self.assertEqual(self.wait_terminal(job_id)["status"], "error")
        with patch.object(web, "translate") as provider, patch.object(web.Settings, "validate") as validate:
            response = self.client.post(f"/api/jobs/{job_id}/resume", headers=self.headers, json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.wait_terminal(job_id)["status"], "ready")
            provider.assert_not_called()
            validate.assert_not_called()
        with patch.object(web, "codex_options", return_value={"available": False}), \
                patch("paperflow.translate.resolve_codex", return_value=["codex.exe"]), \
                patch.object(web, "translate", self.machine_translation), \
                patch.object(web, "render_inline_pdf", side_effect=OSError("排版失败")):
            job_id = self.submit(provider="codex", model="", api_key="", glossary="")
            self.assertEqual(self.wait_terminal(job_id)["status"], "error")
        with patch.object(web.Settings, "validate") as validate, patch.object(web, "translate") as provider:
            response = self.client.post(f"/api/jobs/{job_id}/resume", headers=self.headers, json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.wait_terminal(job_id)["status"], "completed")
            validate.assert_not_called()
            provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
