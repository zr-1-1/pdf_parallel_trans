# 新机器开发交接

更新时间：2026-10-27。适用目标：Windows 11 x64。先验证已发布的 Windows 包，再配置 Conda 开发环境，最后继续下一版工作。

## 1. 接手时先看这些

| 项目 | 当前状态 |
| --- | --- |
| 仓库 | <https://github.com/zr-1-1/pdf_parallel_trans> |
| 已发布版本 | [v0.1.0-beta.1](https://github.com/zr-1-1/pdf_parallel_trans/releases/tag/v0.1.0-beta.1)，GitHub 预览版本 |
| 程序版本 | `0.1.0b1`，定义于 `paperflow/__init__.py` |
| 发布源码基线 | `e44a539ed09002ec36fe368b8b9ed73bda753cd9` |
| 后续开发起点 | 克隆最新 `main`；本文在首次发布之后加入，不在上述标签内 |
| 许可安排 | AGPL-3.0，保留第三方许可及对应源码，见 [第三方声明](../THIRD_PARTY_NOTICES.md) |
| 最先补齐的验证 | 另一台没有开发环境的 Windows 机器上的实际验收 |

这是一个本机单用户 PDF 英中对照工具：FastAPI + Uvicorn 提供本地网页，PyMuPDF 负责读取与输出，RapidOCR 处理扫描页。当前支持预检、单页试译、全文翻译、缓存续译、人工校订和版本导出。前端是静态 HTML/CSS/JavaScript，无需 Node 构建。

Windows 包已包含 Python、OCR 运行库及其随包模型、Noto 中文字体；不包含 Codex CLI、用户账号、API Key 或额外 DocLayout-YOLO 权重。基础功能和边界见 [README](../README.md)。

源码、依赖清单和发布附件足以恢复开发环境。旧电脑上的 Conda 环境、绝对路径、代理配置和临时发布脚本都不是项目依赖，不要直接照搬。

## 2. 阶段一：保持机器干净，先验收 EXE

本阶段在安装 Conda、Python、Git、Node 等开发工具之前进行。若机器已有开发环境，请如实记录；可以用新的 Windows 虚拟机补做独立验证。

1. 从 [Release 页面](https://github.com/zr-1-1/pdf_parallel_trans/releases/tag/v0.1.0-beta.1) 下载 `PDFParallelTrans-0.1.0b1-windows-x64.zip` 和 `SHA256SUMS.txt`。
2. 用 PowerShell 校验 ZIP，然后解压到当前用户可写的目录，建议目录名带中文。
3. 保留完整 `PDFParallelTrans` 文件夹及 `_internal` 子目录，双击 `PDFParallelTrans.exe`。不要只复制 EXE。
4. 检查浏览器打开 `http://127.0.0.1:7860`，页面资源正常，运行状态能识别中文字体和 OCR。Codex CLI 不可用、额外版面模型缺失在这台机器上是预期状态。
5. 使用自制或允许使用的普通文字 PDF 和印刷体扫描 PDF，各执行一次预检，核对原文、阅读顺序、公式及 OCR 结果。预检不需要翻译账号。
6. 再次双击 EXE，确认复用已有服务；关闭服务后重新启动，确认已有任务仍可查看。退出时关闭运行窗口或在控制台按 `Ctrl+C`，只关闭浏览器不会停止服务。
7. 记录机器、操作系统、路径、检查结果和错误。启动失败时查看数据目录内的 `launcher.log`，分享前先检查是否含个人路径或文本。

假设 ZIP 在当前目录，校验命令如下：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath '.\PDFParallelTrans-0.1.0b1-windows-x64.zip'
```

该版本 Windows ZIP 的 SHA-256 应为：

```text
2c597e7b7b3346bd0a2db6abdd81c651482a484a805dbc0cc7011f1443670158
```

Windows 包尚未代码签名，记录系统提示即可，不把关闭系统安全保护作为验收步骤。没有翻译账号时，先完成启动和预检；中文输出、续译和导出可在阶段二用本地模拟 API 验证。实际翻译服务的检查单独进行，需要用户自己的账号、可外发的文本及费用授权。

## 3. 阶段二：恢复源码开发环境

### 3.1 克隆仓库与创建 Conda 环境

完成上面的干净机器记录后，再安装 Git 和 Conda。在 Anaconda Prompt 或已初始化 Conda 的终端执行；工作目录和 Conda 安装位置由新机器决定。

```text
git clone https://github.com/zr-1-1/pdf_parallel_trans.git
cd pdf_parallel_trans
git switch -c work/clean-machine-check
git status --short --branch
conda env create -f environment.yml
conda run -n pdf_parallel_trans python -m pip install -r requirements-ocr.txt
conda run -n pdf_parallel_trans python -m pip check
conda run -n pdf_parallel_trans python -c "import sys; print(sys.executable); print(sys.version)"
```

本项目只使用 Conda 管理 Python 环境，不用系统 Python、裸 `pip` 或在 `base` 中开发。若由助手代执行，创建环境和安装依赖前应按新机器的工作约定获得授权。

`environment.yml` 创建 Python 3.11 环境并安装基础依赖；OCR 对使用文字 PDF 的普通用户是可选项，但完整开发回归需要安装。`constraints.txt` 固定已验证的依赖版本，各 requirements 文件已经引用它，先复现，不顺手升级依赖。首次 Windows 包使用的 Python 为 **3.11.17**。

如果 `conda run` 在 Windows 上失败，先用 `conda env list` 确认环境位置，再使用该环境的 `python.exe` 绝对路径。不要抄旧机器上的盘符和环境路径。

### 3.2 准备中文字体

源码默认查找 `paperflow/fonts/NotoSansCJKsc-Regular.otf`，再查找系统中文字体。中文 Windows 通常已有可用字体。应用也支持 `CJK_FONT_PATH`，但部分测试直接调用字体查找函数，而且冒烟脚本会清除该变量，因此完整回归不能只依赖它。

为使用与发行包相同的字体，可从阶段一解压目录复制以下文件到源码目录：

```text
来源：PDFParallelTrans/_internal/paperflow/fonts/NotoSansCJKsc-Regular.otf
目标：paperflow/fonts/NotoSansCJKsc-Regular.otf
```

`paperflow/fonts/` 已在 `.gitignore` 中排除。字体是本地运行资源，不提交二进制；随包字体许可见发行包 `licenses/Noto-OFL.txt`。构建脚本会另行下载并校验正式构建所需字体，不依赖这份手动复制。

### 3.3 回归与启动

在仓库根目录运行：

```text
conda run -n pdf_parallel_trans python -X utf8 -m unittest discover -s tests -v
conda run -n pdf_parallel_trans python -X utf8 scripts/smoke_release.py
conda run --no-capture-output -n pdf_parallel_trans python -X utf8 -m paperflow.launcher
```

首次发布基线共有 **50 个单元测试**。记录实际通过、失败和跳过数量，OCR 被跳过不能当作完整验收。`smoke_release.py` 使用自制 PDF、本地模拟翻译 API、临时数据目录和临时端口，不需要 API Key、不消费翻译额度；成功时输出包含 `"result": "PASS"` 的 JSON，日志和结果位于 `output/release-smoke/`。

验证已下载 EXE 的完整工作流时，先停止手动运行的 EXE，再把参数替换为新机器上实际的绝对路径：

```text
conda run -n pdf_parallel_trans python -X utf8 scripts/smoke_release.py --exe "C:\Apps\PDFParallelTrans\PDFParallelTrans.exe"
```

这项检查包括真实 OCR、试译、重启续译、校订导出及字体像素比较；它在装好开发环境后执行，与阶段一的独立机器手动检查分别记录。

`run.bat` 也可启动源码。解释器选择顺序是 `PAPERFLOW_PYTHON` → 已激活的 `CONDA_PREFIX` → `.paperflow-python` 中的一行绝对路径 → `conda run -n pdf_parallel_trans`。不要迁移旧机器的 `.paperflow-python`；优先使用上面的显式 Conda 命令。

## 4. 用户数据与翻译连接

| 运行方式 | 默认数据根目录 |
| --- | --- |
| Windows EXE | `%LOCALAPPDATA%\PDFParallelTrans` |
| 源码 | 仓库下的 `data/` |
| 设置 `PAPERFLOW_DATA_DIR` | 使用指定的数据根目录，建议绝对路径 |

继续开发不必迁移历史任务。如果确实要保留旧论文、译文和校订记录：

1. 停止两端服务，备份旧数据根目录。
2. 通过私人传输渠道复制完整数据根目录，保留 `jobs/` 内的原文、缓存、状态、草稿和导出版本，不只复制最终 PDF。
3. 在新机设置 `PAPERFLOW_DATA_DIR` 指向复制后的**数据根目录**，不是 `jobs/` 子目录，再启动并核对任务。
4. 首次续译可能需要重新填写 API Key；旧式缓存无法校验时按提示重新预检，保留备份，不手工篡改缓存清单。

**当前没有跨进程的数据目录互斥。** 同一个数据目录只能由一个服务使用，不要通过不同端口让源码和 EXE 同时处理同一组任务。日常开发、自动测试和真实用户任务应各用独立目录。

API Key 不写入任务文件或浏览器持久存储。新机使用 Codex 通道时自行安装和登录 Codex CLI；DeepSeek 或兼容 API 由用户重新填写连接信息。不要搬运 `.codex` 登录文件、浏览器凭据、整套 Conda 环境或旧机代理设置。选择真实翻译服务会发送待译文本，并可能收费；本地 OCR 和本地网页不代表离线翻译。

Git 不包含 `data/`、`tmp/`、`output/`、`build/`、`dist/`、日志或个人解释器路径。交接时也不要把这些目录整体提交到公开仓库。

## 5. 构建下一次 Windows 包

使用单独的构建环境。以下命令用于复现已有依赖；在更新版本号前生成的文件仍带 `0.1.0b1`，只用于本地验证，不能覆盖已发布附件。

```text
conda create -n pdf_parallel_trans_build -c conda-forge python=3.11.17 pip
conda run -n pdf_parallel_trans_build python -m pip install -r requirements-build.txt
conda run -n pdf_parallel_trans_build python -m pip check
conda run -n pdf_parallel_trans_build python -X utf8 -m unittest discover -s tests -v
conda run -n pdf_parallel_trans_build python -X utf8 scripts/build_release.py
conda run -n pdf_parallel_trans_build python -X utf8 scripts/smoke_release.py --exe dist/PDFParallelTrans/PDFParallelTrans.exe
```

构建依赖网络下载，资源地址和 SHA-256 固定在 `release-resources.json`。产物在 `dist/release/`：Windows ZIP、白名单源码 ZIP、PyMuPDF/MuPDF/GEOS 对应源码及 `SHA256SUMS.txt`。构建日志、依赖许可和环境清单应随验收保留。具体步骤见 [RELEASING](RELEASING.md)。

当前构建脚本只生成本地产物，仓库尚无自动发布 GitHub Release 的 CI。旧机使用的临时发布脚本不在仓库，新机不依赖它。真正发布下一版时，更新程序版本和 [CHANGELOG](../CHANGELOG.md)，完成回归，提交源码，再使用新标签和 GitHub Release 上传同批产物；保留已发布标签及附件不变。

发布时注意：

- PyMuPDF 与 MuPDF 必须匹配对应源码。当前固定为 `1.25.2`；PyMuPDF 源码包本身不包含完整 MuPDF 源码。
- GEOS 对应源码单独随 Release 提供，保留第三方许可文本和字体 OFL；不要打包 Windows 系统字体。
- 新增需要随源码分发的文档或脚本时，更新 `scripts/build_release.py` 的 `SOURCE_FILES` 白名单。
- `--source-only` 会重写 `SHA256SUMS.txt`，清单只包含源码 ZIP；不能把它当作已有 Windows 附件的完整校验清单。
- Windows 包为目录式 PyInstaller 产物，必须带上 `_internal`。目前没有代码签名，也没有已验证的 macOS/Linux 发行包。

## 6. 代码入口和不能丢掉的修复

| 文件 | 主要职责 |
| --- | --- |
| `paperflow/app.py` | HTTP 接口、任务队列、预检/试译/续译、缓存、校订与导出 |
| `paperflow/layout.py` | 阅读顺序、段落布局、原文裁片、中文字体与 PDF 排版 |
| `paperflow/pdf_input.py` | PDF 输入、OCR、可选版面模型及几何降级 |
| `paperflow/formulas.py` | 公式识别和占位符保护 |
| `paperflow/translate.py` | Codex/API 连接、翻译批次、响应校验 |
| `paperflow/launcher.py`、`runtime.py` | 启动、端口复用、错误日志、数据目录 |
| `paperflow/static/` | 本地网页界面 |
| `tests/`、`scripts/smoke_release.py` | 单元回归和真实进程端到端验证 |
| `PDFParallelTrans.spec`、`scripts/build_release.py` | 冻结打包、资源校验、许可收集和源码白名单 |

以下是首次发布时已经修复、后续修改需保留的行为：

1. **Noto OTF 中文缺字。** MuPDF 1.25.2 对部分 OTF/CFF 字体执行原生子集压缩会导致输出缺字，但文本提取仍可能正常。`layout.py` 对 `.otf` 使用 `subset_fonts(fallback=True)`，TrueType 继续使用原生路径。不要为提速直接删除回退；字体或 PyMuPDF 升级后，应继续运行 EXE 冒烟测试中的逐页像素比较，并查看生成 PDF。
2. **冻结包缓存续译。** EXE 中没有独立源码文件。当前缓存版本为 `CACHE_SCHEMA = 2`、提示版本为 `PROMPT_VERSION = 1`；仅在处理 v1 缓存时惰性计算旧源码指纹。不要恢复无条件读取源码文件，也不要绕过缓存指纹或公式校验。
3. **无额外版面权重也能运行。** 默认使用几何分析，OCR 模型已随包提供；DocLayout 权重另由用户提供。缺权重属于正常降级，不应改成启动时自动下载或强制失败。
4. **原文公式占位符要保留。** 人工校订及翻译响应中的 `[[PFM_...]]` 用于恢复公式，不可擅自删除或重编号。回归时不仅看文字通顺，也要核对公式和图表。

## 7. 已验证范围与尚未验证范围

交接前旧机器上的记录如下，新机器应另记结果：

| 项目 | 交接前状态 |
| --- | --- |
| 原开发环境、新建构建 Conda 环境 | 各完成 50 项单元测试；新环境依赖检查通过 |
| 最后一次字体修复 | 又通过 23 项相关核心测试和冻结包冒烟检查 |
| 真实 ZIP 解压后的 EXE | 中文安装目录、移除开发 PATH 后通过启动、预检、试译、续译、预览、校订导出、真实 OCR 和密钥隔离检查 |
| 字体输出 | 随包 Noto 输出与未压缩参考逐页像素一致，并已查看渲染图 |
| 发布产物 | ZIP 完整性、白名单、SHA-256 及 6 个公开 Release 附件核对完成 |
| Codex CLI | 旧机能够获取模型目录；这不等于真实翻译已验收 |
| 另一台干净 Windows 机器 | **待本次接手补做** |
| 真实服务的翻译闭环 | 发布回归使用本地模拟服务；真实账号调用另行验证 |
| Windows 代码签名、GitHub 自动构建发布 | 尚未实现 |

测试通过不代表所有论文布局都正确。复杂三栏、浮动布局、旋转页、手写及低清扫描仍需人工处理；PDF 交互链接、批注和书签不在当前保留范围。

## 8. 后续优先级

下面是建议顺序，不是已经实现的功能，也不代表需要在一次任务中全部完成。

| 顺序 | 工作 | 验收重点 |
| --- | --- | --- |
| 1 / beta.2 | 完成独立机器验收并记录问题 | 安装前 EXE 检查、Conda 完整回归、真实 ZIP 工作流各自有结果 |
| 2 / beta.2 | 同一数据目录的跨进程互斥 | 不同端口仍不能同时写同一目录；正常退出和异常中断后可恢复 |
| 3 / beta.2 | 用户可导出的脱敏诊断 | 能定位启动/任务错误，不带 API Key、论文全文或未脱敏个人信息 |
| 4 / v0.2 | 预检段落纠错、单段重译、校订版本恢复 | 明确保留人工修改，避免重译覆盖已校订内容 |
| 持续 | 10–20 份可公开样例的质量基准 | 阅读顺序、漏段、公式、OCR 错误、人工校订时间、各阶段耗时 |
| 发布流程 | GitHub Actions 构建与校验 | 固定依赖、完整回归、对应源码和 SHA-256 同批生成 |

先用基准找瓶颈，再考虑更高并发、字体性能或更重的版面模型。[ROADMAP](ROADMAP.md) 保留了早期规划和当时状态；判断当前发布状态以本文、README、CHANGELOG 和实际代码为准。

## 9. 新机器验收记录模板

可复制到本地记录；需要公开反馈时先脱敏。

```text
日期 / 操作者：
Windows 版本、系统语言、架构：
安装开发工具之前是否验收：是 / 否（原因）
Release 标签 / ZIP SHA-256：
Git 分支 / commit：
EXE 启动、重复启动、退出重启：
文字 PDF 预检 / 扫描 OCR：
中文字体 / PDF 输出人工查看：
Conda 环境名 / Python 版本：
pip check：
单元测试：通过 __；失败 __；跳过 __
源码 smoke / EXE smoke：
真实服务调用：未执行 / 已获授权并执行（不要记录密钥）
数据迁移：未迁移 / 已备份并核对
问题、最小复现步骤、脱敏日志：
下一步：
```

## 10. 可直接交给新会话的任务说明

> 请接手 `pdf_parallel_trans`，先阅读 `docs/HANDOFF.md`、`README.md`、`docs/RELEASING.md` 和当前机器适用的 AGENTS.md，检查 Git 状态并保留已有修改。当前已发布 `v0.1.0-beta.1`，从最新 main 继续；不要重写已发布标签或附件。
>
> 先确认这台机器是否已完成安装开发工具之前的 Windows EXE 验收，缺项如实记录。然后按交接文档恢复 Conda 环境；创建环境和安装依赖前遵守本机授权要求，不使用系统 Python，不沿用旧机器绝对路径。运行完整单元测试和源码/EXE 冒烟检查，记录实际结果。默认用本地模拟 API，真实账号调用或付费翻译另行获得授权。
>
> 本次先完成环境复现和验收记录；遇到问题先定位，再在授权范围内最小修复。后续优先考虑同一数据目录的跨进程互斥和脱敏诊断。保留 OTF 字体回退、冻结包续译和公式校验；不要将论文、译文、凭据、运行数据或构建缓存提交到公开仓库。
