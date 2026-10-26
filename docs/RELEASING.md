# 构建与发布

当前发布标签：`v0.1.0-beta.1`；Python 版本标识：`0.1.0b1`。

## 环境与构建

在 Windows x64 上使用专用 Conda 环境，不在日常工作环境里打包：

```text
conda create -n pdf_parallel_trans_build python=3.11 pip
conda run -n pdf_parallel_trans_build python -m pip install -r requirements-build.txt -c constraints.txt
conda run -n pdf_parallel_trans_build python -X utf8 -m unittest discover -s tests -v
conda run -n pdf_parallel_trans_build python -X utf8 scripts/build_release.py
conda run -n pdf_parallel_trans_build python -X utf8 scripts/smoke_release.py --exe dist/PDFParallelTrans/PDFParallelTrans.exe
```

构建会下载 `release-resources.json` 中固定版本的 Noto 字体、OFL 和 PyMuPDF/MuPDF 对应源码，逐项核对 SHA-256。需要代理时使用当前终端的标准 `HTTPS_PROXY` / `HTTP_PROXY` 配置，不把代理或凭据写进仓库。

`constraints.txt` 记录本次 Windows / Python 3.11 构建环境中的精确依赖版本；它只约束被安装的依赖，不会让源码基础安装自动装上 OCR 或 PyInstaller。Python 解释器版本另在发布说明记录。升级 PyMuPDF 时必须同时更新对应源码资源和许可信息。

只生成源码包、不下载资源：

```text
conda run -n pdf_parallel_trans_build python scripts/build_release.py --source-only
```

`--source-only` 会重写校验清单，仅列出本次生成的源码包。发布 Windows 版本时应运行完整构建，不能混用旧的校验清单。

## 产物

`dist/release/` 下应有：

- `PDFParallelTrans-0.1.0b1-source.zip`：白名单源码、测试、文档与构建脚本。
- `PDFParallelTrans-0.1.0b1-windows-x64.zip`：解压即用目录包，包含 OCR 与字体。
- `pymupdf-1.25.2.tar.gz`：PyMuPDF 对应源码。
- `mupdf-1.25.2-source.tar.gz`：MuPDF 对应源码及其第三方组件。
- `geos-3.13.1.tar.bz2`：GEOS 动态库对应源码。
- `SHA256SUMS.txt`：各附件的 SHA-256。

Git 仓库不提交 `dist/`、模型、字体二进制、运行数据或论文。正式版本通过标签绑定源码，以上产物上传到同一 GitHub Release，首次版本标记为 pre-release。

## 发布验收

1. 新 Conda 环境完整回归通过；翻译服务使用替身，不消费用户额度。
2. 对生成的 EXE 执行 `smoke_release.py`：清除 Conda 和开发 PATH，使用中文临时数据目录和本地模拟 API，检查重复启动、预检、试译、重启续译、PDF/预览、校订导出、真实 OCR 及密钥隔离；将随包字体生成的 PDF 与未压缩字体参考逐页比较像素，防止只核对文本而漏掉缺字。
3. 另在没有开发环境的 Windows 11 x64 机器或虚拟机上验证解压启动、字体和 OCR。清除 PATH 的同机检查不能代替这项验证。
4. 检查源码 ZIP 白名单、许可证、对应源码、第三方声明和校验清单。
5. 确认 Git 工作区与待发布源码一致后，提交、打标签、推送并创建预览 Release。

Windows 包没有代码签名；不要把尚未完成的独立机器验证或真实付费翻译验证写成通过。
