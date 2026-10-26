# 第三方组件与分发说明

对应源码附件与构建说明：[v0.1.0-beta.1 Release](https://github.com/zr-1-1/pdf_parallel_trans/releases/tag/v0.1.0-beta.1)。

本项目代码：Copyright (c) 2026 wind，GNU Affero General Public License version 3（AGPL-3.0）。完整条款见 LICENSE。其他组件不因本项目许可证而改变自身许可。

## 核心与运行时

| 组件 | 许可 / 官方来源 |
| --- | --- |
| Python | PSF；https://www.python.org/psf/license/ |
| PyMuPDF / MuPDF 1.25.2 对应发行 | AGPL-3.0；https://github.com/pymupdf/PyMuPDF/tree/1.25.2 |
| FastAPI | MIT；https://github.com/fastapi/fastapi |
| Starlette | BSD-3-Clause；https://github.com/encode/starlette |
| Uvicorn | BSD-3-Clause；https://github.com/encode/uvicorn |
| HTTPX / HTTPCore | BSD-3-Clause；https://github.com/encode/httpx |
| python-multipart | Apache-2.0；https://github.com/Kludex/python-multipart |
| Pydantic / pydantic-core | MIT；https://github.com/pydantic/pydantic |
| FontTools | MIT；https://github.com/fonttools/fonttools |
| PyInstaller bootloader | GPL-2.0-or-later，附带允许分发生成程序的例外；https://pyinstaller.org/en/stable/license.html |

PyMuPDF/MuPDF 采用开源 AGPL 路径，没有附带商业授权。Windows Release 同时提供 `pymupdf-1.25.2.tar.gz` 与 `mupdf-1.25.2-source.tar.gz` 对应源码，以及本项目同版本源码、依赖约束和构建脚本。修改并分发时也须履行相应许可要求，不能只复制 EXE 而遗漏许可证及对应源码安排。

## OCR 与字体

- RapidOCR / rapidocr-onnxruntime 1.4.4：Apache-2.0，https://github.com/RapidAI/RapidOCR 。随包模型来自该官方发行包的 `models/`，仅用于本地 OCR。
- ONNX Runtime：MIT，https://github.com/microsoft/onnxruntime 。
- NumPy：BSD-3-Clause，https://github.com/numpy/numpy 。
- OpenCV：Apache-2.0，https://github.com/opencv/opencv 。保留原包附带的第三方声明。构建时不包含仅用于视频的 `opencv_videoio_ffmpeg` 插件，应用不提供视频功能。
- Shapely / GEOS、Pillow、pyclipper、PyYAML 等间接依赖保留各自许可文本；准确版本见 `constraints.txt` 和 Windows 包内 `licenses/build-environment.json`。其中 GEOS 3.13.1 适用 LGPL，随包保留其动态库和许可，允许保留文件名并替换该独立动态库，以调试和使用修改后的 GEOS；同一 Release 提供 `geos-3.13.1.tar.bz2` 对应源码。
- Noto Sans CJK SC Regular：Copyright 2014-2021 Adobe，SIL Open Font License 1.1，https://github.com/notofonts/noto-cjk 。保留字体原名和原版 OFL；固定下载版本及 SHA-256 见 `release-resources.json`。

构建脚本将已安装发行包附带的 LICENSE、COPYING、NOTICE、COPYRIGHT 文件集中复制到 Windows 包 `licenses/`。该目录的环境清单也包含构建工具，未必每项都会进入运行时。

## 未随包分发的内容

不分发 Windows 系统字体、用户论文、翻译结果、账号凭据、Codex CLI 或 DocLayout-YOLO 权重。可选版面模型由用户自行提供并确认其许可。源码模式可使用本机已有字体；不因此获得重新分发系统字体的权利。

参考项目 PDFMathTranslate 和 Reader 不作为运行依赖或随包应用。
