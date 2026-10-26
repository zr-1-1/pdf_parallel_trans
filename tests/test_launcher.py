"""启动回归：批处理编码、重复启动识别与错误保留。"""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from paperflow import launcher
from paperflow.runtime import data_directory


class LauncherTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows 双击启动入口")
    def test_batch_missing_interpreter_reports_error_without_misparsing(self):
        batch = launcher.ROOT / "run.bat"
        content = batch.read_bytes()
        content.decode("ascii")
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
        env = dict(os.environ, PAPERFLOW_PYTHON=str(launcher.ROOT / "missing-python.exe"))
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", str(batch)], input="", text=True,
            capture_output=True, encoding="utf-8", env=env, timeout=10,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Conda interpreter was not found", result.stdout)
        self.assertNotIn("not recognized", result.stderr)

    def test_health_requires_same_application_and_workspace(self):
        for workspace, application, expected in (
            (str(launcher.ROOT), "paperflow-local", True),
            (str(launcher.ROOT / "other"), "paperflow-local", False),
            (str(launcher.ROOT), "other-service", False),
        ):
            with self.subTest(workspace=workspace, application=application):
                payload = json.dumps({"application": application, "workspace": workspace,
                    "version": launcher.__version__, "data_dir": str(data_directory(launcher.ROOT))}).encode()
                with patch("paperflow.launcher.urllib.request.build_opener") as opener:
                    opener.return_value.open.return_value = io.BytesIO(payload)
                    self.assertEqual(launcher.same_service(7860), expected)

    def test_duplicate_launch_reuses_existing_service(self):
        with patch("paperflow.launcher.port_in_use", return_value=True), \
                patch("paperflow.launcher.same_service", return_value=True), \
                patch("paperflow.launcher.webbrowser.open") as browser, \
                patch.dict(os.environ, {"PAPERFLOW_NO_BROWSER": "0", "PDF_WEB_PORT": "7860"}):
            self.assertEqual(launcher.main(), 0)
            browser.assert_called_once_with("http://127.0.0.1:7860")

    def test_invalid_port_and_foreign_service_write_diagnostics(self):
        with tempfile.TemporaryDirectory(dir=launcher.ROOT) as directory, \
                patch.object(launcher, "ROOT", Path(directory)), \
                patch("paperflow.launcher.webbrowser.open") as browser:
            with patch.dict(os.environ, {"PDF_WEB_PORT": "not-a-port"}):
                self.assertEqual(launcher.main(), 1)
            with patch.dict(os.environ, {"PDF_WEB_PORT": "7860"}), \
                    patch("paperflow.launcher.port_in_use", return_value=True), \
                    patch("paperflow.launcher.same_service", return_value=False):
                self.assertEqual(launcher.main(), 1)
            browser.assert_not_called()
            log = (Path(directory) / "data" / "launcher.log").read_text(encoding="utf-8")
            self.assertIn("PDF_WEB_PORT", log)
            self.assertIn("端口 7860 已被占用", log)

    def test_frozen_launch_uses_user_data_without_conda(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("sys.frozen", True, create=True), \
                patch("sys.prefix", directory), \
                patch.dict(os.environ, {"LOCALAPPDATA": directory, "PAPERFLOW_NO_BROWSER": "1", "PAPERFLOW_DATA_DIR": ""}), \
                patch("paperflow.launcher.port_in_use", return_value=False), \
                patch("paperflow.app.main") as run:
            self.assertEqual(data_directory(launcher.ROOT), Path(directory) / "PDFParallelTrans")
            self.assertEqual(launcher.main(), 0)
            run.assert_called_once()
            with patch.dict(os.environ, {"PAPERFLOW_DATA_DIR": directory}):
                self.assertEqual(data_directory(launcher.ROOT), Path(directory).resolve())

    @unittest.skipUnless(os.name == "nt", "Windows 双击启动入口")
    def test_batch_uses_active_conda_without_machine_specific_path(self):
        import sys
        env = dict(os.environ, CONDA_PREFIX=sys.prefix, PAPERFLOW_NO_BROWSER="1", PDF_WEB_PORT="invalid")
        env.pop("PAPERFLOW_PYTHON", None)
        with tempfile.TemporaryDirectory() as directory:
            env["PAPERFLOW_DATA_DIR"] = directory
            result = subprocess.run(["cmd.exe", "/d", "/c", str(launcher.ROOT / "run.bat")],
                input="", text=True, capture_output=True, encoding="utf-8", env=env, timeout=10)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("PDF_WEB_PORT", result.stderr)
            self.assertTrue((Path(directory) / "launcher.log").is_file())

    def test_browser_waits_for_server_readiness_and_skips_failed_start(self):
        from paperflow import app as web

        with patch("uvicorn.Server") as constructor, \
                patch("webbrowser.open") as browser, \
                patch.dict(os.environ, {"PAPERFLOW_NO_BROWSER": "0", "PDF_WEB_PORT": "7860"}):
            server = constructor.return_value
            server.started = False
            opened = threading.Event()
            browser.side_effect = lambda url: opened.set()

            def run():
                self.assertFalse(opened.wait(.2), "服务未就绪时不应打开浏览器")
                server.started = True
                self.assertTrue(opened.wait(2), "服务就绪后应打开浏览器")

            server.run.side_effect = run
            web.main()
            browser.assert_called_once_with("http://127.0.0.1:7860")
            browser.reset_mock()
            server.started = False
            server.run.side_effect = RuntimeError("startup failed")
            with self.assertRaises(RuntimeError):
                web.main()
            browser.assert_not_called()
            server.run.side_effect = None
            with self.assertRaisesRegex(RuntimeError, "未能就绪"):
                web.main()
            browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
