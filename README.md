# PDF Parallel Trans · PaperFlow Local

把英文 PDF 转成一段英文、一段中文的对照 PDF。原文公式、图表和矢量文字保留，中文紧跟原文，以浅灰背景区分。

当前版本：**v0.1.0-beta.1**。本机网页应用，面向单用户；优先支持 Windows 11 x64。

## 下载与启动

普通用户请到 [Releases](https://github.com/zr-1-1/pdf_parallel_trans/releases) 下载 Windows ZIP，完整解压后双击 `PDFParallelTrans.exe`，浏览器会在服务就绪后打开。保持启动窗口开启，按 `Ctrl+C` 停止。

Windows 包包含 Python、OCR 运行库及 Noto 中文字体，无需另装 Python 或 Conda。不包含翻译账号、API Key、Codex CLI 或 DocLayout-YOLO 版面权重。包尚未进行代码签名，版本为预览版。

选择 DeepSeek 或兼容 API 时，由用户填写自己的服务地址、模型和密钥。使用 Codex 通道时，需要本机已有可用的 Codex CLI 和用户登录；程序不复制凭据。**本地界面和 OCR 不代表离线翻译：待译文本会发送到选择的模型服务，并可能消耗额度。**

## 从源码运行

先安装 Conda。在 Anaconda Prompt 或已初始化 Conda 的终端中执行：

```text
git clone https://github.com/zr-1-1/pdf_parallel_trans.git
cd pdf_parallel_trans
conda env create -f environment.yml
conda run --no-capture-output -n pdf_parallel_trans python -X utf8 -m paperflow.launcher
```

可选 OCR：

```text
conda run -n pdf_parallel_trans python -m pip install -r requirements-ocr.txt
```

基础安装支持具有文字层的 PDF；扫描件需要 OCR 依赖。未配置额外版面模型时使用几何分析。程序不会自动下载模型。

Windows 启动脚本 `run.bat` 按顺序使用 `PAPERFLOW_PYTHON`、当前激活的 Conda 环境、项目旁 `.paperflow-python` 中的一行解释器绝对路径，最后尝试 `conda run -n pdf_parallel_trans`。`.paperflow-python` 是仅本机使用的文件，不提交仓库。也可设置 `PAPERFLOW_CONDA_ENV` 指定其他环境名。源码启动要求 Conda 环境。

源码模式使用系统中文字体；缺少字体时通过 `CJK_FONT_PATH` 指定已有中文字体。Windows 发行包附带 SIL OFL 字体，不依赖中文系统字体。

## 使用流程

1. 上传英文 PDF；默认最多 200 MB、150 页，页码可填 `1-5,8`。
2. 选择自动/单栏/双栏，以及自动/关闭/强制 OCR。先预检可核对原文、阅读顺序和公式，不调用翻译服务。
3. 选择翻译连接，填写术语表和背景。可以先试译一页，再翻译全部已选页。
4. 下载对照 PDF。失败、取消或中断后可续译，复用通过校验的已完成段落。
5. 校订面板支持搜索、修改译文和字号、自动保存草稿并导出新版本。公式占位符 `[[PFM_...]]` 必须原样保留。

Codex 可沿用本机模型/推理强度，也可从动态模型目录选择；兼容 API 使用 Chat Completions 文本接口。API Key 只在浏览器和运行中任务内存中使用，不写入任务文件或浏览器持久存储。重启后续译可能需要重新输入密钥。

## 数据、配置与升级

- Windows 包：任务与日志默认保存在 `%LOCALAPPDATA%\PDFParallelTrans`。
- 源码模式：默认保存在项目的 `data/`，兼容已有本地任务。
- 升级 Windows 包：停止旧程序，在新目录解压新版本并启动，原用户数据保留。
- 源码迁移到 EXE：停止旧服务，先备份 `data/`，再将 `PAPERFLOW_DATA_DIR` 指向原数据目录。程序不会自动搬移或删除论文。
- 同一数据目录只能由一个服务使用；不要用不同端口同时处理同一组任务。
- 老式源码指纹缓存需要原版源码校验；无法校验时保留旧结果并提示重新预检。当前缓存不依赖独立 `.py` 文件。

以下环境变量仅在当前启动进程中生效，修改后须重启服务：

| 变量 | 用途 |
| --- | --- |
| `PAPERFLOW_DATA_DIR` | 任务与日志目录；建议使用绝对路径 |
| `PDF_WEB_PORT` | 本机端口，默认 `7860`，仅监听 `127.0.0.1` |
| `PAPERFLOW_MAX_UPLOAD_MB` | 正整数上传上限，默认 `200` |
| `CJK_FONT_PATH` | 自定义中文字体绝对路径 |
| `CODEX_CLI_PATH` | 已安装的 Codex CLI 路径 |
| `PAPERFLOW_LAYOUT_MODEL` | 用户自行提供且许可允许使用的 DocLayout ONNX 权重 |
| `PAPERFLOW_NO_BROWSER` | 设为 `1` 时不自动打开浏览器 |

PowerShell 示例：

```powershell
$env:PDF_WEB_PORT = '7861'
$env:PAPERFLOW_DATA_DIR = 'D:\MyPDFTasks'
.\PDFParallelTrans.exe
```

## 范围与限制

支持常规单栏、双栏和印刷体扫描论文。三栏、复杂浮动布局、旋转页、手写或低清扫描仍需人工处理；公式识别和 OCR 不能保证完整准确。图表内容和参考文献通常保留原文。不保留原 PDF 的交互链接、批注或书签。输出中文与公式均应人工核对。

当前采用单用户串行任务队列，没有账户系统或远程部署功能。上传文件、解析数据、译文和校订历史保存在数据目录，由用户管理。仓库及发行包不附带个人论文、翻译结果、浏览器记录或凭据。

## 开发与构建

```text
conda run -n pdf_parallel_trans python -X utf8 -m unittest discover -s tests -v
```

Windows 构建、发布文件与验收步骤见 [发布说明](docs/RELEASING.md)，功能变更见 [CHANGELOG](CHANGELOG.md)，后续方向见 [ROADMAP](docs/ROADMAP.md)。

## 许可证与来源

Copyright (c) 2026 wind。项目按 [GNU AGPL-3.0](LICENSE) 发布。第三方组件、字体和模型各自适用其许可证，见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。Windows 包同时附带第三方许可文本；PyMuPDF/MuPDF 和 GEOS 对应源码作为同一 Release 的附件提供。

本项目参考 [PDFMathTranslate](https://github.com/PDFMathTranslate/PDFMathTranslate) 的 PDF 版面思路及 [Reader](https://github.com/cynicalight/reader) 的本地阅读工作流，并非它们的官方分支。没有随包分发它们的应用或 DocLayout-YOLO 权重。
