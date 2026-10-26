"""区分随程序分发的只读资源与用户任务数据。"""
import os
from pathlib import Path
import sys


def data_directory(root: Path) -> Path:
    configured = os.getenv("PAPERFLOW_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "PDFParallelTrans"
    # 源码运行保留已有任务位置；独立发行包的数据不写入程序目录。
    return root / "data"
