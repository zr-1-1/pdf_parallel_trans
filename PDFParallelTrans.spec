# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH)
datas = [(str(root / "paperflow/static"), "paperflow/static"),
         (str(root / "build/resources/fonts"), "paperflow/fonts")]
datas += collect_data_files("rapidocr_onnxruntime")
datas += copy_metadata("rapidocr-onnxruntime")
a = Analysis([str(root / "scripts/frozen_entry.py")], pathex=[str(root)],
             binaries=[], datas=datas, hiddenimports=[], hookspath=[],
             runtime_hooks=[], excludes=["tkinter", "matplotlib", "pytest", "IPython"], noarchive=False)
# 只处理静态图像，不分发 OpenCV 的可选视频编解码插件。
a.binaries = [entry for entry in a.binaries if "opencv_videoio_ffmpeg" not in entry[0].lower()]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PDFParallelTrans",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PDFParallelTrans")
