"use strict";
const $ = (id) => document.getElementById(id);
const terminal = new Set(["completed", "error", "cancelled", "interrupted", "ready", "trial_ready"]);
const providerNames = {codex: "Codex", deepseek: "DeepSeek", api: "兼容 API", pending: "本地预检"};
const effortNames = {none: "关闭 · none", minimal: "最少 · minimal", low: "低 · low", medium: "中 · medium", high: "高 · high", xhigh: "更高 · xhigh", max: "最高 · max", ultra: "超高 · ultra"};
let provider = "codex", activeJob = null, activeStatus = null, pollTimer = null, health = null;
let previewUrl = "", previewPage = 1, previewPages = 1, previewRevision = 0;
let editor = null;
let preflightData = null;
let tabId = crypto.randomUUID();
try { tabId = sessionStorage.getItem("paperflow.tab") || tabId; sessionStorage.setItem("paperflow.tab", tabId); } catch { /* 服务端仍提供版本保护。 */ }
let catalog = {models: [], default_model: "", default_effort: ""};
const configs = {codex: {model: "", reasoning_effort: "", base_url: "", api_key: ""}, deepseek: {model: "deepseek-flash", base_url: "https://api.deepseek.com", api_key: ""}, api: {model: "", base_url: "", api_key: ""}};

async function request(url, options = {}) {
  let response;
  try { response = await fetch(url, {...options, headers: {"X-PaperFlow": "1", ...options.headers}}); }
  catch { throw new Error("连接不到本地服务，请检查启动窗口；服务恢复后可继续已有任务。"); }
  let data;
  try { data = await response.json(); } catch { throw new Error("服务返回异常，请检查本地服务是否正在运行。"); }
  if (!response.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : "提交内容无效，请检查填写的信息。");
    error.status = response.status; throw error;
  }
  return data;
}
function codexModel() { return $("codex-model").value === "__custom__" ? $("model").value.trim() : $("codex-model").value; }
function settings() {
  return {provider, model: provider === "codex" ? codexModel() : $("model").value.trim(),
    reasoning_effort: provider === "codex" ? $("reasoning-effort").value : "",
    base_url: $("base-url").value.trim(), api_key: $("api-key").value.trim()};
}
function connectionLabel(config) {
  const parts = [providerNames[config.provider] || config.provider];
  if (config.model) parts.push(config.model);
  else if (config.provider === "codex" && "reasoning_effort" in config) parts.push("本机默认模型");
  if (config.provider === "codex" && "reasoning_effort" in config) parts.push(config.reasoning_effort || "CLI 默认强度");
  return parts.join(" · ");
}
function renderEfforts(value, changedModel = false) {
  const model = catalog.models.find((item) => item.model === (codexModel() || catalog.default_model));
  const efforts = model ? model.efforts : Object.keys(effortNames);
  const inherited = !codexModel() && catalog.default_effort ? `（${catalog.default_effort}）` : "";
  $("reasoning-effort").replaceChildren(new Option("沿用 CLI 配置" + inherited, ""));
  efforts.forEach((effort) => $("reasoning-effort").add(new Option(
    effortNames[effort] + (model?.default_effort === effort ? "（模型默认）" : ""), effort)));
  // 切换到明确模型时优先选它的默认强度，避免继承另一个模型不支持的配置。
  if ((changedModel && !value) || (value && !efforts.includes(value))) value = model?.default_effort || "";
  if (changedModel && !codexModel()) value = "";
  $("reasoning-effort").value = efforts.includes(value) ? value : "";
  $("effort-note").textContent = model ? "仅显示此模型支持的强度。更高强度通常耗时更长，不保证译文更好。" : "未取得此模型的能力信息；自定义强度请先测试连接。更高强度通常耗时更长。";
}
function renderCodex(config) {
  if (provider !== "codex") return;
  const inherited = catalog.default_model ? `（${catalog.default_model}）` : "";
  $("codex-model").replaceChildren(new Option("沿用本机配置" + inherited, ""));
  catalog.models.forEach((model) => $("codex-model").add(new Option(model.name, model.model)));
  $("codex-model").add(new Option("自定义模型…", "__custom__"));
  const known = catalog.models.some((model) => model.model === config.model);
  $("codex-model").value = config.model ? (known ? config.model : "__custom__") : "";
  $("model").value = config.model;
  $("model-field").hidden = $("codex-model").value !== "__custom__";
  $("model").required = !$("model-field").hidden;
  renderEfforts(config.reasoning_effort || "");
}
async function loadModels(refresh = false) {
  $("refresh-models").disabled = true;
  $("catalog-status").textContent = "正在读取本机 Codex 模型…";
  try {
    const result = await request("/api/codex/options" + (refresh ? "?refresh=true" : ""));
    const selected = provider === "codex" ? settings() : configs.codex;
    catalog = result; renderCodex(selected);
    $("catalog-status").textContent = result.message;
  } catch (error) { $("catalog-status").textContent = "模型列表读取失败；仍可使用本机默认或自定义模型。"; }
  finally { $("refresh-models").disabled = false; }
}
$("refresh-models").addEventListener("click", () => loadModels(true));
$("codex-model").addEventListener("change", () => {
  $("model-field").hidden = $("codex-model").value !== "__custom__";
  $("model").required = !$("model-field").hidden;
  renderEfforts($("reasoning-effort").value, true);
  $("connection-result").hidden = true;
});
$("model").addEventListener("input", () => { if (provider === "codex") renderEfforts($("reasoning-effort").value); });
["model", "reasoning-effort", "base-url", "api-key"].forEach((id) => $(id).addEventListener("input", () => { $("connection-result").hidden = true; }));
function showError(error) { $("form-error").textContent = error.message; $("form-error").hidden = false; }
for (const id of ["glossary", "context"]) {
  try { $(id).value = (localStorage.getItem("paperflow." + id) || "").slice(0, $(id).maxLength); } catch { /* 浏览器禁用存储时仍可使用。 */ }
  $(id).addEventListener("input", () => {
    try {
      if ($(id).value) localStorage.setItem("paperflow." + id, $(id).value);
      else localStorage.removeItem("paperflow." + id);
    } catch { /* 存储不可用不影响提交。 */ }
  });
}
function setProvider(next) {
  configs[provider] = settings();
  provider = next;
  $("model").value = configs[next].model;
  $("base-url").value = configs[next].base_url;
  $("api-key").value = configs[next].api_key;
  $("api-fields").hidden = next === "codex";
  $("codex-model-field").hidden = next !== "codex";
  $("codex-options").hidden = next !== "codex";
  $("model-field").hidden = false; $("model").required = true;
  $("model-label").textContent = next === "codex" ? "自定义模型名称" : "模型名称";
  $("model").placeholder = "填写服务商提供的模型名称";
  renderCodex(configs[next]);
  $("base-url").placeholder = next === "deepseek" ? "https://api.deepseek.com" : "https://your-provider.example/v1";
  $("provider-note").textContent = next === "codex" ? "使用本机 Codex CLI 的登录状态与配置，无需在此填写密钥。" : "通过 Chat Completions 接口翻译。密钥仅在内存中使用，刷新网页后需重新填写。";
  $("engine-state").textContent = next === "codex" ? (health?.codex_found ? "CLI 已检测到" : "未检测到 CLI") : "API 连接";
  $("connection-result").hidden = true;
  document.querySelectorAll("[data-provider]").forEach((button) => {
    button.classList.toggle("active", button.dataset.provider === next);
    button.setAttribute("aria-pressed", String(button.dataset.provider === next));
  });
}
document.querySelectorAll("[data-provider]").forEach((button) => button.addEventListener("click", () => setProvider(button.dataset.provider)));

function selectedFile() {
  const file = $("file").files[0];
  $("file-label").textContent = file ? file.name : "拖入 PDF，或点击选择";
  $("file-detail").textContent = file ? `${(file.size / 1024 / 1024).toFixed(2)} MB · 已选择，随时可更换`
    : health ? `单栏、双栏或扫描英文论文 · 最大 ${health.max_mb} MB、${health.max_pages} 页` : "正在读取上传限制…";
}
$("file").addEventListener("change", selectedFile);
for (const event of ["dragenter", "dragover"]) $("dropzone").addEventListener(event, (e) => {e.preventDefault(); $("dropzone").classList.add("drag");});
for (const event of ["dragleave", "drop"]) $("dropzone").addEventListener(event, () => $("dropzone").classList.remove("drag"));
$("dropzone").addEventListener("drop", (event) => {
  event.preventDefault();
  if (event.dataTransfer.files.length) {
    const transfer = new DataTransfer(); transfer.items.add(event.dataTransfer.files[0]);
    $("file").files = transfer.files; selectedFile();
  }
});

$("check").addEventListener("click", async () => {
  if (!$("model").reportValidity()) return;
  const checkedSettings = settings();
  $("check").disabled = true;
  $("connection-result").hidden = false;
  $("connection-result").textContent = "正在发送测试文本，请稍候…";
  try {
    const result = await request("/api/check", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(checkedSettings)});
    if (JSON.stringify(settings()) === JSON.stringify(checkedSettings)) $("connection-result").textContent = "连接成功 · " + connectionLabel(checkedSettings) + " · " + result.translation;
  } catch (error) { if (JSON.stringify(settings()) === JSON.stringify(checkedSettings)) $("connection-result").textContent = error.message; }
  finally { $("check").disabled = false; }
});

async function preview(url, title, note, revision = 0) {
  previewUrl = url; previewPage = 1; previewRevision = revision;
  $("paper-area").hidden = false;
  $("pdf-preview").hidden = false;
  $("empty-preview").hidden = true;
  $("preview-title").textContent = title;
  $("preview-tag").textContent = "中英逐段对照";
  $("download").href = url + "?download=true";
  $("download").hidden = false;
  $("preview-note").textContent = note;
  $("open-pdf").href = url;
  try {
    const info = await request(url + "/info");
    if (previewUrl !== url) return;
    previewPages = info.pages; updatePage();
    $("page-navigation").hidden = false;
  } catch (error) { showError(error); }
}
function updatePage() {
  $("pdf-preview").src = `${previewUrl}/preview/${previewPage}?revision=${previewRevision}`;
  $("pdf-preview").alt = `中英逐段对照 PDF，第 ${previewPage} 页，共 ${previewPages} 页`;
  $("page-counter").textContent = `${previewPage} / ${previewPages}`;
  $("previous-page").disabled = previewPage === 1;
  $("next-page").disabled = previewPage === previewPages;
}
$("previous-page").addEventListener("click", () => { if (previewPage > 1) { previewPage--; updatePage(); } });
$("next-page").addEventListener("click", () => { if (previewPage < previewPages) { previewPage++; updatePage(); } });
$("pdf-preview").addEventListener("error", () => showError(new Error("预览加载失败，可通过下载 PDF 查看文件。")));
function showSample() {
  if (!discardDraft()) return;
  clearTimeout(pollTimer); activeJob = null; activeStatus = null; $("progress-panel").hidden = true;
  editor = null; $("editor").hidden = true; $("edit-translation").hidden = true; $("preflight-panel").hidden = true;
  preview("/api/sample", "参考网页 · 排版样例", "来自你提供的双语网页节选 · 沿用已有译文，未重新翻译");
}
$("show-sample").addEventListener("click", showSample);

function discardDraft() { return !editor?.unsaved || window.confirm(editor.localSaved ? "当前修改尚未同步，已保留浏览器草稿。继续切换？" : "当前修改尚未保存，继续切换可能丢失修改。继续？"); }
function editorError(error) { $("editor-error").textContent = error.message; $("editor-error").hidden = false; }
function draftPrefix(current) { return `paperflow.draft.${current.job}.${current.revision}.`; }
function rememberDraft(current) {
  try {
    localStorage.setItem(draftPrefix(current) + tabId, JSON.stringify({revision:current.revision,
      draft_version:current.draftVersion, translations:current.translations, font_size:current.fontSize, saved_at:Date.now()}));
    current.localSaved = true;
  } catch { current.localSaved = false; }
}
function forgetDraft(current) { try { localStorage.removeItem(draftPrefix(current) + tabId); } catch { /* 不影响已经保存的服务端草稿。 */ } }
function browserDraft(current) {
  try {
    const prefix = draftPrefix(current), own = localStorage.getItem(prefix + tabId);
    if (own) return {...JSON.parse(own), own:true};
    const keys = Object.keys(localStorage).filter(key => key.startsWith(prefix));
    return keys.map(key => JSON.parse(localStorage.getItem(key))).sort((a, b) => b.saved_at - a.saved_at)[0];
  } catch { return null; }
}
async function persistDraft(current) {
  if (current.pending) { await current.pending; return persistDraft(current); }
  if (!current.unsaved || current.conflict || current.exporting) return;
  const sequence = current.sequence;
  const payload = {translations:{...current.translations}, font_size:current.fontSize,
    revision:current.revision, draft_version:current.draftVersion};
  current.pending = (async () => {
    try {
      const result = await request(`/api/jobs/${current.job}/draft`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
      current.draftVersion = result.draft_version;
      current.unsaved = current.sequence !== sequence;
      if (current.unsaved) rememberDraft(current); else forgetDraft(current);
      if (editor === current) $("editor-status").textContent = current.unsaved ? "正在同步新修改…" : "草稿已自动保存到本机，导出后更新 PDF。";
    } catch (error) {
      current.conflict = error.status === 409;
      rememberDraft(current);
      if (editor === current) {
        editorError(error);
        $("editor-status").textContent = current.localSaved ? "尚未同步，浏览器草稿已保留。" : "草稿尚未保存，请保持页面开启并检查本地服务。";
      }
    } finally {
      current.pending = null;
      if (editor === current && current.unsaved && !current.conflict && !current.exporting) {
        clearTimeout(current.timer); current.timer = setTimeout(() => persistDraft(current), 3000);
      }
    }
  })();
  await current.pending;
}
function markEditorDirty() {
  if (!editor) return;
  editor.dirty = true; editor.unsaved = true; editor.sequence++;
  editor.fontSize = Number($("editor-font-size").value);
  rememberDraft(editor);
  clearTimeout(editor.timer);
  if (editor.conflict) {
    $("editor-status").textContent = editor.localSaved ? "草稿版本冲突，修改已保留；请重新加载草稿后核对并恢复。" : "草稿版本冲突，浏览器备份失败；请先复制修改内容，再重新加载草稿。";
    return;
  }
  const current = editor;
  current.timer = setTimeout(() => persistDraft(current), 800);
  $("editor-error").hidden = true;
  $("editor-status").textContent = "正在自动保存草稿…";
}
function saveCurrentParagraph() {
  if (editor?.selected) editor.translations[editor.selected] = $("editor-translation").value;
}
function showParagraph(id) {
  if (!editor) return;
  const paragraph = editor.paragraphs.find((item) => item.id === id);
  editor.selected = paragraph?.id || null;
  $("editor-content").hidden = !paragraph;
  $("editor-paragraph").value = paragraph?.id || "";
  const visible = editor.filtered.indexOf(id);
  $("previous-paragraph").disabled = visible <= 0;
  $("next-paragraph").disabled = visible < 0 || visible === editor.filtered.length - 1;
  $("editor-count").textContent = paragraph ? `第 ${visible + 1} / ${editor.filtered.length} 个匹配段落 · 共 ${editor.paragraphs.length} 段 · 原文第 ${paragraph.page} 页` : "没有匹配段落，请调整关键词。";
  if (!paragraph) return;
  $("editor-source").textContent = paragraph.text;
  $("editor-translation").value = editor.translations[id];
  const tokens = paragraph.text.match(/\[\[PFM_[^\]]+\]\]/g) || [];
  $("editor-formulas").textContent = tokens.length ? `本段包含 ${tokens.length} 处公式。请原样保留 ${tokens.join("、")}，导出时会恢复为公式图形。` : "本段没有受保护的公式标记。";
}
function filterParagraphs() {
  if (!editor) return;
  saveCurrentParagraph();
  const search = $("editor-search").value.trim().toLocaleLowerCase();
  const matches = editor.paragraphs.filter((item) => !search || `${item.text}\n${editor.translations[item.id]}`.toLocaleLowerCase().includes(search));
  editor.filtered = matches.map((item) => item.id);
  $("editor-paragraph").replaceChildren(...matches.map((item) => new Option(`第 ${item.page} 页 · ${item.text.slice(0, 72)}`, item.id)));
  showParagraph(editor.filtered.includes(editor.selected) ? editor.selected : editor.filtered[0]);
}
$("editor-search").addEventListener("input", filterParagraphs);
$("editor-paragraph").addEventListener("change", () => { saveCurrentParagraph(); showParagraph($("editor-paragraph").value); });
for (const [id, offset] of [["previous-paragraph", -1], ["next-paragraph", 1]]) {
  $(id).addEventListener("click", () => {
    if (!editor) return;
    saveCurrentParagraph();
    const next = editor.filtered.indexOf(editor.selected) + offset;
    if (next >= 0 && next < editor.filtered.length) showParagraph(editor.filtered[next]);
  });
}
$("editor-translation").addEventListener("input", () => { saveCurrentParagraph(); markEditorDirty(); });
$("editor-font-size").addEventListener("change", markEditorDirty);
$("close-editor").addEventListener("click", () => { $("editor").hidden = true; });
window.addEventListener("beforeunload", (event) => { if (editor?.unsaved) { rememberDraft(editor); event.preventDefault(); event.returnValue = ""; } });
$("edit-translation").addEventListener("click", async () => {
  if (!activeJob || activeStatus?.status !== "completed") return;
  if (editor?.job === activeJob) { $("editor").hidden = false; return; }
  const id = activeJob;
  $("edit-translation").disabled = true;
  try {
    const result = await request(`/api/jobs/${id}/editor`);
    if (activeJob !== id) return;
    editor = {job:id, revision:result.revision || 0, draftVersion:result.draft_version || 0,
      paragraphs:result.paragraphs, translations:Object.fromEntries(result.paragraphs.map((item) => [item.id, item.zh])),
      filtered:[], selected:null, dirty:Boolean(result.draft), unsaved:false, sequence:0, fontSize:result.font_size || 10.5};
    $("editor-search").value = "";
    const size = String(result.font_size || 10.5);
    if (![...$("editor-font-size").options].some((option) => option.value === size)) $("editor-font-size").add(new Option(`${size} pt`, size));
    $("editor-font-size").value = size;
    $("editor-status").textContent = result.draft ? "已恢复保存的校订草稿；可继续修改并导出。" : `当前版本 ${result.revision || 0} · 修改后自动保存草稿，导出后更新 PDF。`;
    $("editor-error").hidden = true;
    $("editor").hidden = false;
    filterParagraphs();
    const backup = browserDraft(editor);
    const ids = editor.paragraphs.map(item => item.id);
    editor.backup = backup && backup.translations && Object.keys(backup.translations).length === ids.length
      && ids.every(key => typeof backup.translations[key] === "string") ? backup : null;
    $("restore-local-draft").hidden = !editor.backup;
    if (editor.backup?.own && editor.backup.draft_version === editor.draftVersion) restoreBrowserDraft();
  } catch (error) { showError(error); }
  finally { $("edit-translation").disabled = false; }
});
function restoreBrowserDraft() {
  if (!editor?.backup) return;
  editor.translations = {...editor.backup.translations};
  editor.fontSize = editor.backup.font_size;
  const size = String(editor.fontSize);
  if (![...$("editor-font-size").options].some(option => option.value === size)) $("editor-font-size").add(new Option(`${size} pt`, size));
  $("editor-font-size").value = size;
  editor.selected = null; filterParagraphs(); markEditorDirty();
  $("restore-local-draft").hidden = true;
}
$("restore-local-draft").addEventListener("click", () => {
  if (window.confirm("用浏览器保留的草稿替换当前编辑内容并保存？请先核对其他窗口的版本。")) restoreBrowserDraft();
});
$("reload-editor").addEventListener("click", () => {
  if (editor?.unsaved) rememberDraft(editor);
  if (editor) clearTimeout(editor.timer);
  editor = null; $("edit-translation").click();
});
async function exportEdits(restore = false) {
  if (!editor) return;
  if (restore && !window.confirm("恢复此任务的机器译文并导出新版本？当前草稿将被替换，已导出的旧版本会保留。")) return;
  saveCurrentParagraph(); $("editor-error").hidden = true;
  const current = editor;
  const translations = restore ? {} : {...current.translations};
  if (!restore) {
    for (const paragraph of current.paragraphs) {
      const value = translations[paragraph.id];
      const tokens = (text) => (text.match(/\[\[PFM_[^\]]+\]\]/g) || []).sort().join("\n");
      if (!value.trim() || tokens(value) !== tokens(paragraph.text)) {
        $("editor-search").value = ""; filterParagraphs(); showParagraph(paragraph.id);
        return editorError(new Error(!value.trim() ? "译文不能为空，请补全当前段落。" : "当前段落的公式标记有增减或修改，请参照原文恢复后再导出。"));
      }
    }
  }
  $("save-export").disabled = true; $("restore-translation").disabled = true;
  $("editor-translation").readOnly = true; $("editor-font-size").disabled = true;
  $("editor-status").textContent = "正在提交校订内容…";
  try {
    clearTimeout(current.timer);
    await persistDraft(current);
    if (current.unsaved || current.conflict) throw new Error("请先完成草稿同步或重新加载草稿，再导出。浏览器中的修改仍保留。");
    current.exporting = true;
    await request(`/api/jobs/${current.job}/export`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({translations, font_size:current.fontSize, restore, revision:current.revision, draft_version:current.draftVersion})});
    current.dirty = false; forgetDraft(current);
    if (editor === current) { editor = null; $("editor").hidden = true; await track(current.job); }
    else await refreshHistory();
  } catch (error) {
    if (editor === current) { editorError(error); $("editor-status").textContent = "尚未导出，校订内容仍保留在此页面。"; }
    else showError(error);
  }
  finally {
    current.exporting = false;
    $("save-export").disabled = false; $("restore-translation").disabled = false;
    $("editor-translation").readOnly = false; $("editor-font-size").disabled = false;
  }
}
$("save-export").addEventListener("click", () => exportEdits());
$("restore-translation").addEventListener("click", () => exportEdits(true));
function duration(job) {
  const seconds = job.started ? Math.max(0, Math.round((job.finished || Date.now() / 1000) - job.started)) : Math.round(job.elapsed_seconds || 0);
  if (!seconds) return "";
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
async function resumeJob(job) {
  if (!discardDraft()) return;
  $("form-error").hidden = true;
  let apiKey = "";
  if (job.provider !== "codex" && job.run_mode !== "preflight" && !job.translation_complete && !job.run_translation_complete) {
    if (provider !== job.provider) return showError(new Error(`请先切换到 ${providerNames[job.provider] || job.provider}，填写此任务使用的 API Key，再点击继续。`));
    const current = settings();
    const endpoint = (url) => url.trim().replace(/\/+$/, "");
    if (!job.base_url || endpoint(current.base_url) !== endpoint(job.base_url)) return showError(new Error(`请将 API Base URL 设为原任务地址 ${job.base_url || "（旧任务未记录地址，请重新上传）"}，再填写对应密钥。`));
    if (!current.api_key) return showError(new Error("续译需要重新填写此服务的 API Key；密钥不会从历史记录中读取。"));
    apiKey = current.api_key;
  }
  $("resume").disabled = true;
  try {
    await request(`/api/jobs/${job.id}/resume`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({api_key:apiKey})});
    editor = null; $("editor").hidden = true;
    await track(job.id);
  } catch (error) { showError(error); }
  finally { $("resume").disabled = false; }
}
$("resume").addEventListener("click", () => { if (activeStatus) resumeJob(activeStatus); });

function showPreflightPage() {
  if (!preflightData) return;
  const pno = Number($("preflight-page").value);
  const info = preflightData.pages.find(item => item.page === pno);
  if (!info) return;
  $("source-preview").src = `/api/jobs/${preflightData.job}/source/preview/${pno}`;
  $("preflight-page-info").textContent = `${info.columns} 栏 · ${info.ocr ? "OCR 识别" : "原生文字"} · ${info.paragraphs} 段 · ${info.formulas} 处受保护公式`;
  $("preflight-text").replaceChildren(...preflightData.paragraphs.filter(item => item.page === pno).map(item => {
    const li = document.createElement("li"); li.textContent = item.text; return li;
  }));
  $("trial-start").disabled = !info.paragraphs;
}
$("preflight-page").addEventListener("change", showPreflightPage);
async function showPreflight(job) {
  const result = await request(`/api/jobs/${job.id}/preflight`);
  if (activeJob !== job.id) return;
  const selected = preflightData?.job === job.id ? Number($("preflight-page").value) : job.trial_page;
  preflightData = {...result, job:job.id};
  $("preflight-page").replaceChildren(...result.pages.map(item => new Option(`原文第 ${item.page} 页 · ${item.paragraphs} 段`, item.page)));
  const target = result.pages.find(item => item.page === selected) || result.pages.find(item => item.paragraphs) || result.pages[0];
  $("preflight-page").value = target.page;
  $("preflight-summary").textContent = `选定 ${result.pages.length} 页 · ${result.paragraphs.length} 段 · 约 ${result.characters.toLocaleString()} 字符${job.document_done ? ` · 已保存译文 ${job.document_done} 段` : ""}。请先核对识别结果，再开始翻译。`;
  $("preflight-settings").textContent = job.settings_locked ? `继续沿用已确定的 ${connectionLabel(job)}、术语及背景；更换设置请重新预检。试译结果会复用。` : "开始翻译时使用左侧连接、术语和背景设置。试译结果会在继续翻译时复用。";
  $("preflight-panel").hidden = false;
  showPreflightPage();
}
async function startPrepared(trial) {
  const job = activeStatus;
  if (!job || !["ready", "trial_ready"].includes(job.status)) return;
  $("form-error").hidden = true;
  let config = {...settings(), glossary:$("glossary").value, context:$("context").value};
  if (job.settings_locked) {
    const page = preflightData?.pages.find(item => item.page === Number($("preflight-page").value));
    const cached = trial ? page?.translated === page?.paragraphs : job.translation_complete;
    let apiKey = "";
    if (job.provider !== "codex" && !cached) {
      if (provider !== job.provider || settings().base_url.replace(/\/+$/, "") !== job.base_url.replace(/\/+$/, "")) {
        return showError(new Error(`请切换到 ${providerNames[job.provider]} 并使用原地址 ${job.base_url}，再填写对应密钥。`));
      }
      apiKey = settings().api_key;
      if (!apiKey) return showError(new Error("请重新填写原服务的 API Key。"));
    }
    config = {provider:job.provider, model:job.model, reasoning_effort:job.reasoning_effort,
      base_url:job.base_url, api_key:apiKey, glossary:job.glossary, context:job.context};
  }
  $("trial-start").disabled = true; $("full-start").disabled = true;
  try {
    await request(`/api/jobs/${job.id}/start`, {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({...config, trial_page:trial ? Number($("preflight-page").value) : null})});
    $("preflight-panel").hidden = true;
    await track(job.id);
  } catch (error) { showError(error); }
  finally { $("trial-start").disabled = false; $("full-start").disabled = false; }
}
$("trial-start").addEventListener("click", () => startPrepared(true));
$("full-start").addEventListener("click", () => startPrepared(false));

async function refreshHistory() {
  const list = await request("/api/jobs");
  const container = $("history"); container.replaceChildren();
  if (!list.length) { const p = document.createElement("p"); p.className = "empty-history"; p.textContent = "完成的论文会显示在这里。"; container.append(p); }
  for (const job of list) {
    const entry = document.createElement("div"); entry.className = "history-entry";
    const button = document.createElement("button"); button.type = "button"; button.className = "history-item";
    const icon = document.createElement("span"); icon.className = "history-icon"; icon.textContent = "PDF";
    const info = document.createElement("span"); info.className = "history-info";
    const name = document.createElement("span"); name.className = "history-name"; name.textContent = job.file;
    const state = document.createElement("span"); state.className = "history-status";
    const labels = {completed: "已完成", ready:"待开始翻译", trial_ready:"试译完成", error: "失败", cancelled: "已取消", interrupted: "已中断", queued: "排队中", uploading: "上传中", parsing: "读取中", translating: "翻译中", rendering: "排版中"};
    state.textContent = `${connectionLabel(job)} · ${labels[job.status] || job.status}${job.done ? ` · ${job.done} 段` : ""} · ${new Date(job.created * 1000).toLocaleString("zh-CN", {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"})}`;
    info.append(name, state); button.append(icon, info); entry.append(button); container.append(entry);
    button.addEventListener("click", () => track(job.id));
    if (["error", "cancelled", "interrupted"].includes(job.status)) {
      const resume = document.createElement("button"); resume.type = "button"; resume.className = "text-button history-resume";
      resume.textContent = "继续任务"; resume.setAttribute("aria-label", `继续任务：${job.file}`);
      resume.addEventListener("click", async () => { resume.disabled = true; try { await resumeJob(job); } finally { resume.disabled = false; } });
      entry.append(resume);
    }
  }
  return list;
}
async function track(id) {
  if (activeJob !== id) {
    if (!discardDraft()) return;
    editor = null; $("editor").hidden = true; $("edit-translation").hidden = true;
    $("preflight-panel").hidden = true;
    $("form-error").hidden = true;
  }
  activeJob = id; clearTimeout(pollTimer);
  $("progress-panel").hidden = false;
  try {
    const job = await request(`/api/jobs/${id}`);
    if (activeJob !== id) return;
    activeStatus = job;
    if (!job.url) {
      previewUrl = "";
      $("preview-title").textContent = job.file;
      $("preview-tag").textContent = job.preflight ? "本地预检" : "处理中";
      for (const item of ["download", "page-navigation", "paper-area"]) $(item).hidden = true;
      $("preview-note").textContent = job.status === "ready" ? "预检完成，尚未生成译文 PDF。" : "此任务尚未生成译文 PDF。";
    }
    $("progress-message").textContent = job.message;
    $("progress").value = job.progress || 0;
    const elapsed = duration(job);
    $("progress-detail").textContent = `${job.file} · ${connectionLabel(job)}${job.pages ? " · 原文 " + job.pages.length + " 页" : ""}${job.total ? ` · ${job.done || 0}/${job.total} 段` : ""}${job.cached ? ` · 复用 ${job.cached} 段` : ""}${elapsed ? ` · 用时 ${elapsed}` : ""}`;
    const analysis = job.analysis;
    $("analysis-detail").textContent = analysis ? `识别双栏 ${analysis.double_column_pages?.length || 0} 页 · OCR ${analysis.ocr_pages?.length || 0} 页 · 保护公式 ${analysis.formula_count || 0} 处` : "";
    $("analysis-warnings").textContent = analysis?.warnings?.join("；") || "";
    $("analysis-warnings").hidden = !analysis?.warnings?.length;
    $("error-hint").textContent = job.error_hint || "";
    $("error-hint").hidden = !job.error_hint;
    $("cancel").hidden = terminal.has(job.status);
    $("cancel").disabled = false;
    $("resume").hidden = !["error", "cancelled", "interrupted"].includes(job.status);
    $("edit-translation").hidden = job.status !== "completed";
    if (["ready", "trial_ready"].includes(job.status)) await showPreflight(job);
    else $("preflight-panel").hidden = true;
    if (job.status === "trial_ready") preview(job.url, job.file, `仅原文第 ${job.trial_page} 页试译 · 可在上方继续翻译全部选定页`);
    if (job.status === "completed") preview(job.url, job.file, `已生成 ${job.output_pages} 页 · ${job.revision ? `校订版本 ${job.revision}` : "机器译文"} · 浅灰底中文译文`, job.revision || 0);
    if (!terminal.has(job.status)) pollTimer = setTimeout(() => track(id), 1500);
    else await refreshHistory();
  } catch (error) {
    if (activeJob !== id) return;
    $("progress-message").textContent = "状态读取失败，将自动重试…";
    $("progress-detail").textContent = error.message;
    pollTimer = setTimeout(() => track(id), 4000);
  }
}
$("cancel").addEventListener("click", async () => {
  if (!activeJob) return;
  $("cancel").disabled = true;
  try { await request(`/api/jobs/${activeJob}/cancel`, {method:"POST"}); await track(activeJob); }
  catch (error) { showError(error); $("cancel").disabled = false; }
});
$("translate-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("form-error").hidden = true;
  const file = $("file").files[0];
  if (!file || !file.name.toLowerCase().endsWith(".pdf")) return showError(new Error("请先选择 PDF 文件。"));
  if (!health) return showError(new Error("尚未取得上传限制，请等待本地服务就绪后重试。"));
  if (file.size > health.max_mb * 1024 * 1024) return showError(new Error(`PDF 最大 ${health.max_mb} MB。`));
  const config = settings();
  const preflight = event.submitter?.id === "preflight";
  if (!preflight && provider !== "codex" && (!config.api_key || !config.model || !config.base_url)) return showError(new Error("请填写 API 地址、模型与密钥。"));
  const body = new FormData(event.currentTarget);
  body.set("protect_formulas", String($("protect-formulas").checked));
  body.set("preflight", String(preflight));
  if (!preflight) Object.entries(config).forEach(([key, value]) => body.set(key, value));
  $("submit").disabled = true; $("preflight").disabled = true;
  try {
    const job = await request("/api/translate", {method:"POST", body});
    await refreshHistory(); await track(job.id);
  } catch (error) { showError(error); }
  finally { $("submit").disabled = false; $("preflight").disabled = false; }
});

(async () => {
  try {
    health = await request("/api/health"); selectedFile(); setProvider("codex");
    $("parsing-capabilities").textContent = `${health.ocr_available ? "本地 OCR 已就绪" : "未检测到本地 OCR 模型"} · ${health.layout_model ? "版面模型已就绪" : "使用几何分栏识别"}`;
    loadModels();
    if (!health.font_found) showError(new Error("缺少中文字体，请配置 CJK_FONT_PATH。"));
    if (health.sample) { showSample(); $("show-sample").hidden = false; }
    const list = await refreshHistory();
    const running = list.find((job) => !terminal.has(job.status));
    if (running) track(running.id);
  } catch (error) { $("engine-state").textContent = "服务不可用"; showError(error); }
})();
