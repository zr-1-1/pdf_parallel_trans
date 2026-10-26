"""隔离用户数据与翻译凭据，验证源码或已构建 EXE 的真实 HTTP 工作流。"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

import fitz
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_core import make_pdf
from test_pdf_features import feature_pdf, scan_pdf


class MockAPI(BaseHTTPRequestHandler):
    requests = 0

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        paragraphs = json.loads(request["messages"][1]["content"])
        result = {"translations": [{"id": p["id"], "zh": "发行包验证：保留原文公式与图表。 " +
            " ".join(re.findall(r"\[\[PFM_[^\]]+\]\]", p["english"]))} for p in paragraphs]}
        content = json.dumps({"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]}).encode()
        MockAPI.requests += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path)
    args = parser.parse_args()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    output = ROOT / "output/release-smoke"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="中文路径-", dir=output) as temporary:
        data = Path(temporary) / "用户数据"
        env = dict(os.environ, PAPERFLOW_DATA_DIR=str(data), PDF_WEB_PORT=str(port), PAPERFLOW_NO_BROWSER="1",
            PAPERFLOW_LAYOUT_MODEL=str(Path(temporary) / "missing.onnx"), CODEX_CLI_PATH=str(Path(temporary) / "missing.exe"))
        for name in ("CONDA_PREFIX", "CONDA_DEFAULT_ENV", "PYTHONHOME", "PYTHONPATH", "PAPERFLOW_PYTHON", "CJK_FONT_PATH"):
            env.pop(name, None)
        if args.exe:
            # 不允许从开发环境 PATH 补齐发行包遗漏的 DLL 或外部命令。
            env["PATH"] = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
        command = [str(args.exe.resolve())] if args.exe else [sys.executable, "-B", "-X", "utf8", "-m", "paperflow.launcher"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        log_path = output / ("frozen.log" if args.exe else "source.log")
        with log_path.open("w", encoding="utf-8") as log, httpx.Client(base_url=f"http://127.0.0.1:{port}",
                trust_env=False, timeout=60, headers={"X-PaperFlow": "1"}) as client:
            process = None

            def start():
                nonlocal process
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, creationflags=flags)
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError(f"程序启动失败，查看 {log_path}")
                    try:
                        health = client.get("/api/health").json()
                        if health.get("service") == "ok":
                            assert Path(health["data_dir"]) == data
                            assert health["font_found"] and health["ocr_available"]
                            assert not health["codex_found"] and not health["layout_model"]
                            return health
                    except httpx.RequestError:
                        pass
                    time.sleep(.2)
                raise AssertionError("服务启动超时")

            def stop():
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)

            def post(path, **kwargs):
                response = client.post(path, **kwargs)
                assert response.status_code == 200, response.text
                return response.json()

            def finished(job_id, expected):
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    job = client.get(f"/api/jobs/{job_id}").json()
                    assert job["status"] not in {"error", "cancelled"}, job
                    if job["status"] == expected and not job.get("operation"):
                        return job
                    # 预检和试译保留 operation，终态本身足以表明工作已完成。
                    if job["status"] == expected and expected in {"ready", "trial_ready"}:
                        return job
                    time.sleep(.1)
                raise AssertionError(f"任务超时：{job}")

            try:
                health = start()
                assert client.get("/").status_code == 200
                assert client.get("/static/app.js").status_code == 200
                duplicate = subprocess.run(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                    capture_output=True, timeout=30, creationflags=flags)
                assert duplicate.returncode == 0, duplicate.stderr
                with fitz.open(stream=make_pdf(), filetype="pdf") as source, fitz.open() as doc:
                    doc.insert_pdf(source)
                    doc.insert_pdf(source)
                    original = doc.tobytes()
                job_id = post("/api/translate", data={"preflight": "true", "ocr": "off", "columns": "single"},
                    files={"file": ("原文.pdf", original, "application/pdf")})["id"]
                finished(job_id, "ready")
                assert MockAPI.requests == 0
                options = {"provider": "api", "base_url": f"http://127.0.0.1:{mock.server_port}/v1",
                    "model": "local-smoke", "api_key": "smoke-key-must-not-persist"}
                post(f"/api/jobs/{job_id}/start", json={**options, "trial_page": 1})
                finished(job_id, "trial_ready")
                requests_before = MockAPI.requests
                stop()
                start()
                post(f"/api/jobs/{job_id}/start", json=options)
                job = finished(job_id, "completed")
                assert job["cached"] > 0 and MockAPI.requests > requests_before
                pdf = client.get(f"/api/jobs/{job_id}/pdf").content
                (output / ("frozen.pdf" if args.exe else "source.pdf")).write_bytes(pdf)
                if args.exe:
                    from unittest.mock import patch
                    from paperflow.layout import Paragraph, render_inline_pdf
                    directory = data / "jobs" / job_id
                    paragraphs = [Paragraph(**item) for item in json.loads((directory / "paragraphs.json").read_text(encoding="utf-8"))]
                    translations = json.loads((directory / "translations.json").read_text(encoding="utf-8"))
                    analysis = json.loads((directory / "analysis.json").read_text(encoding="utf-8"))
                    font = args.exe.resolve().parent / "_internal/paperflow/fonts/NotoSansCJKsc-Regular.otf"
                    reference = output / "unsubset-reference.pdf"
                    # 对照未压缩字体的像素；仅检查文本会漏掉 CFF 子集产生的缺字。
                    with patch.object(fitz.Document, "subset_fonts"):
                        render_inline_pdf(directory / "source.pdf", reference, paragraphs, translations,
                            font_path=str(font), columns="single", analysis=analysis)
                    with fitz.open(reference) as expected, fitz.open(stream=pdf, filetype="pdf") as actual:
                        assert len(expected) == len(actual)
                        assert all(a.get_pixmap().samples == b.get_pixmap().samples for a, b in zip(expected, actual))
                with fitz.open(stream=pdf, filetype="pdf") as doc:
                    assert "发行包验证" in unicodedata.normalize("NFC", "".join(page.get_text() for page in doc))
                    if args.exe:
                        assert any("NotoSansCJK" in font[3].replace(" ", "") for page in doc for font in page.get_fonts())
                assert client.get(f"/api/jobs/{job_id}/pdf/preview/1").headers["content-type"] == "image/png"
                editor = client.get(f"/api/jobs/{job_id}/editor").json()
                translations = {item["id"]: item["zh"] for item in editor["paragraphs"]}
                first = next(iter(translations))
                translations[first] = "校订验证。" + translations[first]
                post(f"/api/jobs/{job_id}/export", json={"translations": translations, "font_size": 10.5,
                    "revision": editor["revision"], "draft_version": editor["draft_version"]})
                finished(job_id, "completed")
                scan_id = post("/api/translate", data={"preflight": "true", "ocr": "auto"},
                    files={"file": ("扫描.pdf", scan_pdf(feature_pdf()), "application/pdf")})["id"]
                finished(scan_id, "ready")
                report = json.loads((data / "jobs" / scan_id / "analysis.json").read_text(encoding="utf-8"))
                assert report["ocr_pages"] == [1]
                for path in data.rglob("*.json"):
                    assert options["api_key"] not in path.read_text(encoding="utf-8")
                print(json.dumps({"result": "PASS", "version": health["version"], "frozen": bool(args.exe),
                    "checks": ["startup", "duplicate", "static", "preflight", "trial", "restart", "resume", "pdf", "preview", "edit-export", "ocr", "key-isolation"]}))
            finally:
                stop()
                mock.shutdown()
                mock.server_close()


if __name__ == "__main__":
    main()
