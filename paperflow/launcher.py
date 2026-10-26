"""双击启动入口：复用当前项目服务，并保留可读的启动错误。"""
import datetime
import json
import os
from pathlib import Path
import socket
import sys
import traceback
import urllib.request
import webbrowser

from . import __version__
from .runtime import data_directory


ROOT = Path(__file__).resolve().parent.parent


def port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def same_service(port: int) -> bool:
    # 本地探测不经过用户配置的远程代理，也不跟随重定向。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            info = json.loads(response.read(8192))
        return (
            isinstance(info, dict)
            and info.get("application") == "paperflow-local"
            and isinstance(info.get("workspace"), str)
            and os.path.normcase(info["workspace"]) == os.path.normcase(str(ROOT))
            and info.get("version") == __version__
            and os.path.normcase(info.get("data_dir", "")) == os.path.normcase(str(data_directory(ROOT)))
        )
    except (OSError, ValueError):
        return False


def fail(message: str, details: str = "") -> int:
    print(f"\n启动失败：{message}", file=sys.stderr)
    log = data_directory(ROOT) / "launcher.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(f"\n[{datetime.datetime.now().isoformat(timespec='seconds')}] {message}\n{details}")
        print(f"启动诊断已记录到：{log}", file=sys.stderr)
    except OSError:
        print("无法写入启动日志，请查看本窗口错误信息。", file=sys.stderr)
    return 1


def main() -> int:
    try:
        if not getattr(sys, "frozen", False) and not (Path(sys.prefix) / "conda-meta").is_dir():
            return fail("PAPERFLOW_PYTHON 必须指向 Conda 环境中的 python.exe，请检查 run.bat。")
        try:
            port = int(os.getenv("PDF_WEB_PORT", "7860"))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            return fail("PDF_WEB_PORT 必须是 1 到 65535 之间的整数。")

        url = f"http://127.0.0.1:{port}"
        if port_in_use(port):
            if not same_service(port):
                return fail(
                    f"端口 {port} 已被占用，且无法确认为本项目服务。"
                    "请关闭此前启动的本项目窗口，或在 PowerShell 中设置 "
                    "$env:PDF_WEB_PORT='7861' 后重新启动程序。"
                )
            print(f"本项目服务已经启动，继续访问：{url}")
            if os.getenv("PAPERFLOW_NO_BROWSER") != "1":
                webbrowser.open(url)
            return 0

        from .app import main as run_app

        print(f"正在启动 PaperFlow {__version__}：{url}\n任务数据：{data_directory(ROOT)}\n保持此窗口开启，按 Ctrl+C 停止服务。", flush=True)
        run_app()
        return 0
    except KeyboardInterrupt:
        return 0
    except SystemExit as exc:
        return fail(f"服务未能启动或异常退出（退出码 {exc.code}），请查看上方服务日志。") if exc.code else 0
    except ModuleNotFoundError as exc:
        return fail(f"当前 Conda 环境缺少依赖 {exc.name}，请按 README.md 核对项目依赖。", traceback.format_exc())
    except Exception:
        return fail("启动出现异常，请查看 launcher.log 中的详细信息。", traceback.format_exc())


if __name__ == "__main__":
    raise SystemExit(main())
