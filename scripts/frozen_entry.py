"""Windows 发行包入口；构建在 Conda 中完成，用户无需另装 Python。"""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from paperflow.launcher import main
    code = main()
    if code:
        try:
            input("启动失败，按 Enter 关闭窗口。")
        except (EOFError, OSError):
            pass
    raise SystemExit(code)
