"""单进程、仅监听 loopback 的本地网页应用。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import fitz
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from .layout import Paragraph, clean_text_controls, collect_paragraphs, find_cjk_font, parse_pages, render_inline_pdf
from .formulas import check_formula_tokens
from .translate import SYSTEM, SCHEMA, Cancelled, Settings, codex_options, resolve_codex, translate
from .pdf_input import layout_model_path, ocr_available, prepare_pdf
from . import __version__
from .runtime import data_directory

ROOT = Path(__file__).resolve().parent.parent
JOBS = data_directory(ROOT) / "jobs"
try:
    MAX_UPLOAD_MB = int(os.getenv("PAPERFLOW_MAX_UPLOAD_MB", "200"))
    if MAX_UPLOAD_MB < 1:
        raise ValueError
except ValueError:
    raise ValueError("PAPERFLOW_MAX_UPLOAD_MB 必须为正整数（单位 MB）") from None
MAX_BYTES, MAX_PAGES = MAX_UPLOAD_MB * 1024 * 1024, 150
TERMINAL = {"completed", "error", "cancelled", "interrupted", "ready", "trial_ready"}
# 仅在存储结构或翻译提示的语义不再兼容时升级，排版及诊断调整不影响已有译文。
CACHE_SCHEMA = 2
PROMPT_VERSION = 1
jobs, cancellations = {}, {}
mutex = threading.RLock()
# ponytail: 单用户本机串行处理，避免同时翻译大量论文消耗额度；多用户时再改队列。
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paperflow")


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def update_job(job_id, **values):
    with mutex:
        if jobs[job_id].get("started"):
            values["elapsed_seconds"] = round(time.time() - jobs[job_id]["started"], 1)
        if values.get("status") in TERMINAL:
            values["finished"] = time.time()
        jobs[job_id].update(values)
        write_json(JOBS / job_id / "status.json", jobs[job_id])


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cache_fingerprint(settings, pages, columns, ocr, protect_formulas, *, legacy=False):
    value = {"provider": settings.provider, "model": settings.model, "base_url": settings.base_url,
             "reasoning_effort": settings.reasoning_effort, "glossary": settings.glossary,
             "context": settings.context, "pages": pages, "columns": columns,
             "ocr": ocr, "protect_formulas": protect_formulas}
    if legacy:
        value["code"] = [file_hash(ROOT / "paperflow" / name) for name in
                         ("translate.py", "layout.py", "formulas.py", "pdf_input.py")]
    else:
        value.update(schema=CACHE_SCHEMA, prompt_version=PROMPT_VERSION, system=SYSTEM, response_schema=SCHEMA)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def load_paragraphs(directory):
    return [Paragraph(**value) for value in read_json(directory / "paragraphs.json")]


def cached_inputs(directory, fingerprint=None, *, legacy=None):
    manifest = read_json(directory / "cache.json")
    version = manifest.get("schema", 1)
    if version not in {1, CACHE_SCHEMA}:
        raise ValueError("缓存结构不兼容，请重新预检；旧结果仍保留")
    if version == 1 and fingerprint is not None and callable(legacy):
        try:
            legacy = legacy()
        except OSError as exc:
            raise ValueError("此旧缓存需要原版源码进行校验，请在原源码环境续译或重新预检；旧结果仍保留") from exc
    if fingerprint is not None and manifest["fingerprint"] != (legacy if version == 1 else fingerprint):
        raise ValueError("任务设置或翻译提示已变化，不能安全复用旧译文；请重新预检，旧结果仍保留")
    if not isinstance(manifest.get("files"), dict) or not {"source.pdf", "paragraphs.json", "analysis.json"} <= set(manifest["files"]):
        raise ValueError("缓存文件清单不完整，请重新预检")
    for name, expected in manifest["files"].items():
        if name not in {"source.pdf", "prepared.pdf", "paragraphs.json", "analysis.json"}:
            raise ValueError("缓存文件清单无效")
        if not (directory / name).is_file() or file_hash(directory / name) != expected:
            raise ValueError("原文或解析缓存已改变，不能安全续译；请重新上传，旧结果仍保留")
    source = directory / ("prepared.pdf" if "prepared.pdf" in manifest["files"] else "source.pdf")
    if version == 1 and fingerprint is not None:
        # 旧格式只有在原校验也通过时迁移，保留原清单供核对。
        with mutex:
            if not (directory / "cache-v1.json").exists():
                write_json(directory / "cache-v1.json", manifest)
            write_json(directory / "cache.json", {**manifest, "schema": CACHE_SCHEMA, "fingerprint": fingerprint})
    return source, read_json(directory / "analysis.json"), load_paragraphs(directory)


def error_hint(stage, message):
    lower = message.lower()
    if "429" in lower or "限流" in lower:
        return "服务商限制请求频率；稍后点击续译，已完成的批次会复用。"
    if any(word in lower for word in ("timeout", "超时", "连接", "network")):
        return "检查当前连接与代理，再点击续译；已完成的批次会复用。"
    if any(word in lower for word in ("401", "403", "登录", "鉴权", "api key")):
        return "Codex 请检查当前用户的 CLI 登录；API 请重新填写该服务的密钥。"
    if "公式" in message:
        return "保留原段落所有 [[PFM_...]] 占位符；校订时不要修改、删除或重复它们。"
    return {"parsing": "检查页码、分栏和 OCR 设置；需要调整设置时重新上传。",
            "translating": "检查模型、推理强度与连接。续译沿用原设置，更换设置时重新上传。",
            "rendering": "已完成译文仍保留；检查字体与磁盘空间后重试导出。"}.get(stage, "检查提示后重试。")


@asynccontextmanager
async def lifespan(app):
    JOBS.mkdir(parents=True, exist_ok=True)
    for path in JOBS.glob("*/status.json"):
        if not re.fullmatch(r"[a-f0-9]{32}", path.parent.name):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value["id"] != path.parent.name:
                continue
            if value["status"] not in TERMINAL:
                exporting = value.get("operation") == "export" and (path.parent / "bilingual.pdf").is_file()
                value.update(status="completed" if exporting else "interrupted",
                             message="服务重启，重新导出已中断；仍保留上次成品" if exporting else
                                     "服务重启，任务已中断；可点击续译，已完成的批次译文保留在任务目录",
                             operation="", finished=time.time())
                write_json(path, value)
            jobs[value["id"]] = value
        except (OSError, ValueError, KeyError):
            continue
    yield
    for event in list(cancellations.values()):
        event.set()


app = FastAPI(title="PaperFlow Local", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])


@app.middleware("http")
async def local_requests(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.netloc.lower() != request.headers.get("host", "").lower():
            return JSONResponse({"detail": "仅允许从本机应用页面操作"}, status_code=403)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("x-paperflow") != "1" or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "请从 PaperFlow 网页提交操作"}, status_code=403)
    try:
        if int(request.headers.get("content-length", "0")) > MAX_BYTES + 1024 * 1024:
            return JSONResponse({"detail": f"文件最大 {MAX_UPLOAD_MB} MB"}, status_code=413)
    except ValueError:
        return JSONResponse({"detail": "请求长度无效"}, status_code=400)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'; object-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'self'"
    return response


@app.get("/")
def home():
    return FileResponse(ROOT / "paperflow" / "static" / "index.html")


@app.get("/api/health")
def health():
    try:
        resolve_codex()
        codex = True
    except ValueError:
        codex = False
    try:
        find_cjk_font(os.getenv("CJK_FONT_PATH"))
        font = True
    except ValueError:
        font = False
    return {"service": "ok", "application": "paperflow-local", "workspace": str(ROOT.resolve()),
            "version": __version__, "data_dir": str(JOBS.parent),
            "codex_found": codex, "font_found": font,
            "ocr_available": ocr_available(), "layout_model": layout_model_path() is not None,
            "sample": (ROOT / "output" / "pdf" / "reference-inline.pdf").is_file(),
            "max_mb": MAX_UPLOAD_MB, "max_pages": MAX_PAGES}


class Connection(BaseModel):
    provider: str = Field(default="codex", max_length=20)
    model: str = Field(default="", max_length=120)
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=4096, repr=False)
    reasoning_effort: str = Field(default="", max_length=24)


class ResumeRequest(BaseModel):
    api_key: str = Field(default="", max_length=4096, repr=False)


class ExportRequest(BaseModel):
    translations: dict[str, str] = Field(default_factory=dict, max_length=20000)
    font_size: float = Field(default=10.5, ge=8, le=14)
    restore: bool = False
    revision: int | None = Field(default=None, ge=0)
    draft_version: int | None = Field(default=None, ge=0)


class DraftRequest(BaseModel):
    translations: dict[str, str] = Field(max_length=20000)
    font_size: float = Field(default=10.5, ge=8, le=14)
    revision: int = Field(ge=0)
    draft_version: int = Field(default=0, ge=0)


class StartRequest(Connection):
    glossary: str = Field(default="", max_length=12000)
    context: str = Field(default="", max_length=4000)
    trial_page: int | None = Field(default=None, ge=1, le=MAX_PAGES)


def pin_codex_defaults(settings):
    if settings.provider == "codex":
        settings.base_url = ""
    if settings.provider != "codex" or (settings.model and settings.reasoning_effort):
        return
    catalog = codex_options()
    if catalog.get("available"):
        settings.model = settings.model or catalog.get("default_model", "")
        known = next((m for m in catalog.get("models", []) if m["model"] == settings.model), {})
        settings.reasoning_effort = (settings.reasoning_effort or catalog.get("default_effort", "")
                                     or known.get("default_effort", ""))


def validate_edits(paragraphs, translations):
    if set(translations) != {p.id for p in paragraphs}:
        raise ValueError("译文段落 ID 不完整或包含未知段落，请重新打开校订面板")
    result = {}
    for paragraph in paragraphs:
        text = translations[paragraph.id]
        if not isinstance(text, str) or not text.strip() or len(text) > 30000:
            raise ValueError(f"段落 {paragraph.id} 的译文不能为空且不得超过 30000 字符")
        text = clean_text_controls(text).strip()
        if not text:
            raise ValueError(f"段落 {paragraph.id} 的译文不能为空")
        check_formula_tokens(text, paragraph)
        result[paragraph.id] = text
    return result


def load_translations(directory, paragraphs):
    path = directory / "translations.json"
    cached = read_json(path) if path.is_file() else {}
    if not isinstance(cached, dict) or not set(cached) <= {p.id for p in paragraphs}:
        raise ValueError("续译缓存包含未知段落，已停止以避免错配")
    return validate_edits([p for p in paragraphs if p.id in cached], cached)


@app.get("/api/codex/options")
def get_codex_options(refresh: bool = False):
    return codex_options(refresh=refresh)


@app.post("/api/check")
def check_connection(connection: Connection):
    settings = Settings(**connection.model_dump(), timeout=120)
    try:
        result = translate([Paragraph("test", 0, 0, 1, 0, 1,
            "Data association reduces the uncertainty of orbit determination.")], settings)
        return {"ok": True, "translation": result["test"]}
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc).replace(settings.api_key, "[已隐藏]") if settings.api_key else str(exc)) from exc
    finally:
        settings.api_key = ""


def worker(job_id, settings, pages, fontsize, columns="auto", ocr="auto", protect_formulas=True, *,
           resume=False, preflight=False, trial_page=None):
    directory = JOBS / job_id
    cancel = cancellations[job_id]
    stage = "parsing"
    try:
        if cancel.is_set():
            raise Cancelled("任务已取消")
        update_job(job_id, status="parsing", message="正在识别正文段落…", progress=3,
                   started=time.time(), finished=None, error_stage="", error_hint="", run_translation_complete=False,
                   operation="preflight" if preflight else "translate")
        def parsing_progress(pno, message):
            update_job(job_id, message=f"第 {pno + 1} 页 · {message}",
                       progress=3 + round(pages.index(pno) / len(pages) * 15))

        fingerprint = cache_fingerprint(settings, pages, columns, ocr, protect_formulas)
        cached = {}
        if resume and (directory / "cache.json").is_file():
            source, analysis, paragraphs = cached_inputs(directory, None if preflight else fingerprint,
                legacy=lambda: cache_fingerprint(settings, pages, columns, ocr, protect_formulas, legacy=True))
            cached = load_translations(directory, paragraphs)
        else:
            if resume and (directory / "translations.json").is_file():
                raise ValueError("此旧任务缺少缓存校验信息，无法安全续译；请重新上传，旧译文仍保留")
            source, analysis = prepare_pdf(directory / "source.pdf", directory / "prepared.pdf",
                                           pages, ocr, parsing_progress, cancel)
            with fitz.open(source) as document:
                paragraphs = collect_paragraphs(document, pages, columns=columns, analysis=analysis,
                                                protect_formulas=protect_formulas)
            write_json(directory / "analysis.json", analysis)
            write_json(directory / "paragraphs.json", [vars(p) for p in paragraphs])
            names = ["source.pdf", "paragraphs.json", "analysis.json"]
            if source.name == "prepared.pdf":
                names.append("prepared.pdf")
            write_json(directory / "cache.json", {"schema": CACHE_SCHEMA, "fingerprint": fingerprint,
                       "files": {name: file_hash(directory / name) for name in names}})
        if cancel.is_set():
            raise Cancelled("任务已取消")
        summary = {"ocr_pages": analysis["ocr_pages"],
                   "double_column_pages": [int(pno) + 1 for pno, info in analysis["pages"].items() if info["columns"] == 2],
                   "formula_count": sum(len(p.formulas) for p in paragraphs), "warnings": analysis["warnings"]}
        update_job(job_id, analysis=summary, document_total=len(paragraphs),
                   characters=sum(len(p.text) for p in paragraphs))
        if not paragraphs:
            raise ValueError("没有识别到英文正文。请检查页码、分栏选择及扫描件清晰度；未发起翻译")
        if preflight:
            update_job(job_id, status="ready", message="预检完成；请核对原文，再选择单页试译或翻译全部选定页",
                       total=len(paragraphs), done=0, progress=100, operation="")
            return
        all_paragraphs = paragraphs
        if trial_page is not None:
            paragraphs = [p for p in paragraphs if p.page == trial_page - 1]
            if not paragraphs:
                raise ValueError("所选页面没有待译正文，请选择其他页")
        selected_cached = {p.id: cached[p.id] for p in paragraphs if p.id in cached}
        stage = "translating"
        update_job(job_id, status=stage, message=f"准备翻译 {len(paragraphs)} 段，复用 {len(selected_cached)} 段…",
                   total=len(paragraphs), done=len(selected_cached), cached=len(selected_cached), progress=20)

        def progress(done, total, result):
            write_json(directory / "translations.json", {**cached, **result})
            update_job(job_id, message=f"正在翻译 {done} / {total} 段", done=done,
                       document_done=len({**cached, **result}), progress=20 + round(done / total * 70))

        if selected_cached and len(selected_cached) == len(paragraphs):
            translated = validate_edits(paragraphs, selected_cached)
            progress(len(translated), len(paragraphs), translated)
        else:
            translated = translate(paragraphs, settings, progress, cancel, **({"cached": selected_cached} if resume else {}))
        merged = {**cached, **translated}
        write_json(directory / "translations.json", merged)
        if cancel.is_set():
            raise Cancelled("任务已取消")
        stage = "rendering"
        update_job(job_id, status=stage, message="正在排版，保留原文、公式和图表…", progress=94,
                   translation_complete=len(merged) == len(all_paragraphs), run_translation_complete=True)
        output = directory / ("trial.pdf" if trial_page is not None else "bilingual.pdf")
        temp_output = directory / ("trial.tmp.pdf" if trial_page is not None else "bilingual.tmp.pdf")
        render_inline_pdf(source, temp_output, paragraphs, translated, [trial_page - 1] if trial_page is not None else pages,
                          os.getenv("CJK_FONT_PATH"), fontsize, columns=columns, analysis=analysis)
        if cancel.is_set():
            temp_output.unlink(missing_ok=True)
            raise Cancelled("任务已取消")
        with fitz.open(temp_output) as document:
            count = len(document)
        temp_output.replace(output)
        update_job(job_id, status="trial_ready" if trial_page is not None else "completed",
                   message=f"原文第 {trial_page} 页试译完成；可继续翻译全部选定页" if trial_page is not None else
                           f"已完成 · {count} 页中英逐段对照", progress=100,
                   output_pages=count, done=len(paragraphs), document_done=len(merged),
                   url=f"/api/jobs/{job_id}/trial/pdf" if trial_page is not None else f"/api/jobs/{job_id}/pdf", operation="")
    except Cancelled:
        update_job(job_id, status="cancelled", message="任务已取消；可点击续译，已完成的批次译文保留在任务目录", operation="")
    except Exception as exc:
        message = str(exc)
        if settings.api_key:
            message = message.replace(settings.api_key, "[已隐藏]")
        update_job(job_id, status="error", message=message[:600], error_stage=stage,
                   error_hint=error_hint(stage, message), operation="")
    finally:
        settings.api_key = ""
        with mutex:
            if cancellations.get(job_id) is cancel:
                cancellations.pop(job_id, None)


@app.post("/api/translate")
async def submit(file: UploadFile = File(...), provider: str = Form("codex"), model: str = Form(""),
                 base_url: str = Form(""), api_key: str = Form(""), pages: str = Form(""),
                 fontsize: float = Form(10.5), reasoning_effort: str = Form(""),
                 columns: str = Form("auto"), ocr: str = Form("auto"), protect_formulas: bool = Form(True),
                 glossary: str = Form("", max_length=12000), context: str = Form("", max_length=4000),
                 preflight: bool = Form(False)):
    settings = Settings() if preflight else Settings(provider, model, base_url, api_key,
                        reasoning_effort=reasoning_effort, glossary=glossary, context=context)
    try:
        if not preflight:
            await run_in_threadpool(pin_codex_defaults, settings)
            await run_in_threadpool(settings.validate)
        find_cjk_font(os.getenv("CJK_FONT_PATH"))
        if not 8 <= fontsize <= 14:
            raise ValueError("译文字号必须在 8 到 14 之间")
        if columns not in {"auto", "single", "double"} or ocr not in {"auto", "off", "force"}:
            raise ValueError("分栏或 OCR 模式无效")
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise ValueError("请上传 .pdf 文件")
    except ValueError as exc:
        await file.close()
        raise HTTPException(400, str(exc)) from exc
    job_id = uuid.uuid4().hex
    directory = JOBS / job_id
    with mutex:
        if sum(j["status"] not in TERMINAL for j in jobs.values()) >= 2:
            await file.close()
            raise HTTPException(429, "已有两个任务在处理，请稍后再试")
        directory.mkdir(parents=True)
        jobs[job_id] = {"id": job_id, "status": "uploading", "message": "正在接收文件…", "progress": 0,
                        "file": file.filename.replace("\\", "/").split("/")[-1][:180],
                        "provider": "pending" if preflight else settings.provider,
                        "model": settings.model, "base_url": settings.base_url,
                        "glossary": settings.glossary, "context": settings.context,
                        "reasoning_effort": settings.reasoning_effort, "created": time.time(),
                        "columns": columns, "ocr": ocr, "protect_formulas": protect_formulas, "fontsize": fontsize,
                        "preflight": preflight, "settings_locked": not preflight,
                        "run_mode": "preflight" if preflight else "full"}
        cancellations[job_id] = threading.Event()
    src = directory / "source.pdf"
    try:
        size = 0
        with src.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, f"文件最大 {MAX_UPLOAD_MB} MB")
                stream.write(chunk)
        with fitz.open(src) as probe:
            if not probe.is_pdf or probe.needs_pass or not 1 <= len(probe) <= MAX_PAGES:
                raise ValueError(f"请上传未加密、1 到 {MAX_PAGES} 页的 PDF")
            selected = parse_pages(pages, len(probe))
        update_job(job_id, status="queued", message="任务已加入队列", pages=[p + 1 for p in selected])
        executor.submit(worker, job_id, settings, selected, fontsize, columns, ocr, protect_formulas,
                        **({"preflight": True} if preflight else {}))
        return {"id": job_id}
    except Exception as exc:
        src.unlink(missing_ok=True)
        with mutex:
            jobs.pop(job_id, None)
            cancellations.pop(job_id, None)
        settings.api_key = ""
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(400, f"PDF 无法处理：{str(exc)[:300]}") from exc
    finally:
        await file.close()


@app.get("/api/jobs")
def list_jobs():
    with mutex:
        recent = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)[:40]
        return [get_job(j["id"]) for j in recent]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with mutex:
        if job_id not in jobs:
            raise HTTPException(404, "未找到任务")
        value = dict(jobs[job_id])
        if value["status"] not in TERMINAL and value.get("started"):
            value["elapsed_seconds"] = round(time.time() - value["started"], 1)
        return value


@app.get("/api/jobs/{job_id}/preflight")
def preflight_report(job_id: str):
    job = get_job(job_id)
    if not job.get("preflight") or job["status"] not in TERMINAL:
        raise HTTPException(409, "请先完成本地预检")
    try:
        _, analysis, paragraphs = cached_inputs(JOBS / job_id)
        translated = load_translations(JOBS / job_id, paragraphs)
        return {"pages": [{"page": pno, "paragraphs": sum(p.page == pno - 1 for p in paragraphs),
                           "translated": sum(p.id in translated for p in paragraphs if p.page == pno - 1),
                           "columns": analysis["pages"].get(str(pno - 1), {}).get("columns", 1),
                           "ocr": pno in analysis["ocr_pages"],
                           "formulas": sum(len(p.formulas) for p in paragraphs if p.page == pno - 1)}
                          for pno in job["pages"]],
                "paragraphs": [{"id": p.id, "page": p.page + 1, "text": p.text} for p in paragraphs],
                "warnings": analysis["warnings"], "characters": sum(len(p.text) for p in paragraphs)}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)[:400]) from exc


@app.get("/api/jobs/{job_id}/source/preview/{page_no}")
def source_preview(job_id: str, page_no: int):
    job = get_job(job_id)
    if page_no not in job.get("pages", []):
        raise HTTPException(404, "页码不在选定范围内")
    with fitz.open(JOBS / job_id / "source.pdf") as document:
        image = document[page_no - 1].get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False)
        return Response(image.tobytes("png"), media_type="image/png")


@app.post("/api/jobs/{job_id}/start")
def start_prepared(job_id: str, request: StartRequest):
    job = get_job(job_id)
    if job["status"] not in {"ready", "trial_ready"}:
        raise HTTPException(409, "请等待预检或试译完成")
    directory = JOBS / job_id
    settings = Settings(**request.model_dump(exclude={"trial_page"}))
    try:
        _, _, paragraphs = cached_inputs(directory)
        pages = [p - 1 for p in job["pages"]]
        target = [p for p in paragraphs if request.trial_page is None or p.page == request.trial_page - 1]
        if not target:
            raise ValueError("所选页面没有待译正文，请选择其他页")
        cached = load_translations(directory, paragraphs)
        if not all(p.id in cached for p in target):
            pin_codex_defaults(settings)
            settings.validate()
        fingerprint = cache_fingerprint(settings, pages, job["columns"], job["ocr"], job["protect_formulas"])
        if job.get("settings_locked"):
            cached_inputs(directory, fingerprint)
            if cached and settings.provider == "codex" and not settings.model and not all(p.id in cached for p in target):
                raise ValueError("未记录试译的实际模型，请显式选择模型并重新预检以保持译文一致")
        with mutex:
            if jobs[job_id]["status"] not in {"ready", "trial_ready"} or job_id in cancellations:
                raise HTTPException(409, "任务状态已变化，请刷新后重试")
            if sum(j["status"] not in TERMINAL for j in jobs.values()) >= 2:
                raise HTTPException(429, "已有两个任务在处理，请稍后再试")
            manifest = read_json(directory / "cache.json")
            write_json(directory / "cache.json", {**manifest, "schema": CACHE_SCHEMA, "fingerprint": fingerprint})
            cancellations[job_id] = threading.Event()
            update_job(job_id, status="queued", progress=0, message="单页试译已加入队列" if request.trial_page else "翻译已加入队列，将复用试译结果",
                       provider=settings.provider, model=settings.model, reasoning_effort=settings.reasoning_effort,
                       base_url=settings.base_url, glossary=settings.glossary, context=settings.context,
                       settings_locked=True, run_mode="trial" if request.trial_page else "full", trial_page=request.trial_page,
                       started=None, finished=None, elapsed_seconds=0, error_stage="", error_hint="")
            executor.submit(worker, job_id, settings, pages, job["fontsize"], job["columns"], job["ocr"],
                            job["protect_formulas"], resume=True, trial_page=request.trial_page)
        return {"id": job_id}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        message = str(exc).replace(settings.api_key, "[已隐藏]") if settings.api_key else str(exc)
        settings.api_key = ""
        raise HTTPException(400, message[:600]) from exc


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str, request: ResumeRequest):
    job = get_job(job_id)
    if job["status"] not in {"error", "cancelled", "interrupted"}:
        raise HTTPException(409, "仅失败、取消或中断的任务可以续译")
    directory = JOBS / job_id
    preflight = job.get("run_mode") == "preflight"
    settings = Settings(provider="codex" if preflight else job["provider"], model=job.get("model", ""),
                        base_url=job.get("base_url", ""), api_key=request.api_key,
                        reasoning_effort=job.get("reasoning_effort", ""),
                        glossary=job.get("glossary", ""), context=job.get("context", ""))
    try:
        if not preflight and settings.provider != "codex" and not settings.base_url:
            raise ValueError("旧任务未保存 API 地址，无法安全续译；请重新上传并填写连接")
        if not (directory / "source.pdf").is_file():
            raise ValueError("原文副本缺失，请重新上传")
        pages = [p - 1 for p in job["pages"]]
        columns, ocr, protect = job.get("columns", "auto"), job.get("ocr", "auto"), job.get("protect_formulas", True)
        complete_cache = False
        if (directory / "cache.json").is_file():
            _, _, paragraphs = cached_inputs(directory,
                None if preflight else cache_fingerprint(settings, pages, columns, ocr, protect),
                legacy=lambda: cache_fingerprint(settings, pages, columns, ocr, protect, legacy=True))
            cached = load_translations(directory, paragraphs)
            target = [p for p in paragraphs if job.get("trial_page") is None or p.page == job["trial_page"] - 1]
            complete_cache = bool(target) and all(p.id in cached for p in target)
            if not preflight and not complete_cache and settings.provider == "codex" and not settings.model:
                raise ValueError("此任务未能记录本机默认模型，无法保证续译模型一致；请重新上传并显式选择模型")
        elif (directory / "translations.json").is_file():
            raise ValueError("此旧任务缺少缓存校验信息，无法安全续译；请重新上传，旧译文仍保留")
        if not preflight and not complete_cache:
            settings.validate()
        with mutex:
            if jobs[job_id]["status"] not in {"error", "cancelled", "interrupted"} or job_id in cancellations:
                raise HTTPException(409, "任务状态已变化，请稍后刷新重试")
            if sum(j["status"] not in TERMINAL for j in jobs.values()) >= 2:
                raise HTTPException(429, "已有两个任务在处理，请稍后再试")
            cancellations[job_id] = threading.Event()
            update_job(job_id, status="queued", message="续译已加入队列，将复用已完成批次", progress=0,
                       started=None, finished=None, elapsed_seconds=0, error_stage="", error_hint="")
            executor.submit(worker, job_id, settings, pages, job.get("fontsize", 10.5), columns, ocr, protect,
                            resume=True, preflight=preflight, trial_page=job.get("trial_page"))
        return {"id": job_id}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        message = str(exc).replace(settings.api_key, "[已隐藏]") if settings.api_key else str(exc)
        settings.api_key = ""
        raise HTTPException(400, message[:600]) from exc


@app.get("/api/jobs/{job_id}/editor")
def get_editor(job_id: str):
    job = get_job(job_id)
    if job["status"] != "completed":
        raise HTTPException(409, "请等待 PDF 生成完成后校订")
    directory = JOBS / job_id
    try:
        paragraphs = load_paragraphs(directory)
        revision = job.get("revision", 0)
        translations = read_json(directory / (f"edits-r{revision}.json" if revision else "translations.json"))
        draft = read_json(directory / "draft.json") if (directory / "draft.json").is_file() else {}
        pending = draft.get("base_revision") == revision
        if pending:
            translations = draft["translations"]
        return {"paragraphs": [{"id": p.id, "page": p.page + 1, "text": p.text,
                                "zh": translations[p.id], "formulas": p.formulas} for p in paragraphs],
                "revision": revision, "draft": pending, "draft_version": draft.get("version", 0) if pending else 0,
                "saved_at": draft.get("saved_at") if pending else None,
                "font_size": draft["font_size"] if pending else job.get("export_fontsize", job.get("fontsize", 10.5))}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise HTTPException(400, "此任务的段落或译文数据不完整，仍可下载已生成的 PDF") from exc


def draft_version(directory, revision):
    path = directory / "draft.json"
    draft = read_json(path) if path.is_file() else {}
    return draft.get("version", 0) if draft.get("base_revision") == revision else 0


@app.post("/api/jobs/{job_id}/draft")
def save_draft(job_id: str, request: DraftRequest):
    with mutex:
        job = get_job(job_id)
        if job["status"] != "completed" or job_id in cancellations:
            raise HTTPException(409, "任务正在导出，请稍后重新打开校订面板")
        directory = JOBS / job_id
        if request.revision != job.get("revision", 0) or request.draft_version != draft_version(directory, request.revision):
            raise HTTPException(409, "另一个窗口已保存新版本；当前修改仍保留在浏览器，请重新打开校订面板后核对")
        paragraphs = load_paragraphs(directory)
        if set(request.translations) != {p.id for p in paragraphs}:
            raise HTTPException(400, "草稿段落不完整，请重新打开校订面板")
        if any(len(value) > 30000 for value in request.translations.values()):
            raise HTTPException(400, "每段草稿最多允许 30000 个字符")
        # 编辑途中可以暂时为空或缺少公式标记；正式导出仍执行严格校验。
        version, saved_at = request.draft_version + 1, time.time()
        write_json(directory / "draft.json", {"base_revision": request.revision, "version": version,
                   "translations": request.translations, "font_size": request.font_size, "saved_at": saved_at})
        return {"draft_version": version, "saved_at": saved_at}


def export_worker(job_id, paragraphs, translations, fontsize, revision):
    directory = JOBS / job_id
    cancel = cancellations[job_id]
    job = get_job(job_id)
    output = directory / f"bilingual-r{revision}.pdf"
    temp = directory / f"bilingual-r{revision}.tmp.pdf"
    try:
        if cancel.is_set():
            raise Cancelled("重新导出已取消")
        update_job(job_id, status="rendering", started=time.time(), progress=94,
                   message="正在用校订译文重新排版，不调用翻译模型…")
        analysis = read_json(directory / "analysis.json") if (directory / "analysis.json").is_file() else None
        source = directory / ("prepared.pdf" if analysis and analysis.get("ocr_pages") else "source.pdf")
        render_inline_pdf(source, temp, paragraphs, translations, [p - 1 for p in job["pages"]],
                          os.getenv("CJK_FONT_PATH"), fontsize, columns=job.get("columns", "auto"), analysis=analysis)
        if cancel.is_set():
            raise Cancelled("重新导出已取消")
        with fitz.open(temp) as document:
            count = len(document)
        write_json(directory / f"edits-r{revision}.json", translations)
        temp.replace(output)
        update_job(job_id, status="completed", progress=100, message=f"校订版已导出 · {count} 页 · 第 {revision} 版",
                   revision=revision, export_fontsize=fontsize, output_pages=count, operation="", export_error=False,
                   url=f"/api/jobs/{job_id}/pdf", error_stage="", error_hint="")
    except Exception as exc:
        cancelled = isinstance(exc, Cancelled)
        message = "重新导出已取消" if cancelled else f"重新导出失败：{str(exc)[:400]}"
        update_job(job_id, status="completed", progress=100, message=message + "；仍保留上次成品与译文",
                   operation="", export_error=True, error_stage="rendering",
                   error_hint="可继续校订或再次导出；上次成品未被覆盖。")
    finally:
        temp.unlink(missing_ok=True)
        with mutex:
            if cancellations.get(job_id) is cancel:
                cancellations.pop(job_id, None)


@app.post("/api/jobs/{job_id}/export")
def export_job(job_id: str, request: ExportRequest):
    with mutex:
        job = get_job(job_id)
        if job["status"] != "completed" or job_id in cancellations:
            raise HTTPException(409, "请等待当前任务结束后重新导出")
        if request.revision is not None and request.revision != job.get("revision", 0):
            raise HTTPException(409, "译文版本已变化，请重新打开校订面板")
        if sum(j["status"] not in TERMINAL for j in jobs.values()) >= 2:
            raise HTTPException(429, "已有两个任务在处理，请稍后再试")
        directory = JOBS / job_id
        current_draft = draft_version(directory, job.get("revision", 0))
        if request.draft_version is not None and request.draft_version != current_draft:
            raise HTTPException(409, "另一个窗口已更新草稿，请重新打开校订面板后核对")
        try:
            paragraphs = load_paragraphs(directory)
            translations = read_json(directory / "translations.json") if request.restore else request.translations
            translations = validate_edits(paragraphs, translations)
            find_cjk_font(os.getenv("CJK_FONT_PATH"))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)[:600]) from exc
        revision = job.get("revision", 0) + 1
        # 重启可能留下尚未切换的输出版本；不覆盖这些文件。
        while (directory / f"bilingual-r{revision}.pdf").exists() or (directory / f"edits-r{revision}.json").exists():
            revision += 1
        write_json(directory / "draft.json", {"base_revision": job.get("revision", 0),
                   "translations": translations, "font_size": request.font_size,
                   "version": current_draft + 1, "saved_at": time.time()})
        cancellations[job_id] = threading.Event()
        update_job(job_id, status="queued", message="校订导出已加入队列，不调用翻译模型", operation="export",
                   progress=0, started=None, finished=None, elapsed_seconds=0, error_stage="", error_hint="", export_error=False)
        executor.submit(export_worker, job_id, paragraphs, translations, request.font_size, revision)
    return {"id": job_id}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = get_job(job_id)
    with mutex:
        if job["status"] not in TERMINAL and job_id in cancellations:
            cancellations[job_id].set()
            update_job(job_id, message="正在取消；API 请求会在返回或超时后停止…")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/pdf")
def download(job_id: str, download: bool = False):
    job = get_job(job_id)
    path = pdf_path(job_id)
    return FileResponse(path, media_type="application/pdf", filename=f"{Path(job['file']).stem}-bilingual.pdf",
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/sample")
def sample(download: bool = False):
    path = ROOT / "output" / "pdf" / "reference-inline.pdf"
    if not path.is_file():
        raise HTTPException(404, "尚未生成参考样例")
    return FileResponse(path, media_type="application/pdf", filename="PaperFlow-reference.pdf",
                        content_disposition_type="attachment" if download else "inline")


def pdf_path(job_id=None):
    if job_id is None:
        path = ROOT / "output" / "pdf" / "reference-inline.pdf"
    else:
        job = get_job(job_id)
        if job["status"] != "completed" and job.get("operation") != "export":
            raise HTTPException(404, "PDF 尚未生成")
        revision = job.get("revision", 0)
        path = JOBS / job_id / (f"bilingual-r{revision}.pdf" if revision else "bilingual.pdf")
    if not path.is_file():
        raise HTTPException(404, "未找到 PDF")
    return path


def trial_path(job_id):
    get_job(job_id)
    path = JOBS / job_id / "trial.pdf"
    if not path.is_file():
        raise HTTPException(404, "试译 PDF 尚未生成")
    return path


@app.get("/api/jobs/{job_id}/trial/pdf")
def trial_download(job_id: str, download: bool = False):
    job = get_job(job_id)
    return FileResponse(trial_path(job_id), media_type="application/pdf", filename=f"{Path(job['file']).stem}-trial.pdf",
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/jobs/{job_id}/trial/pdf/info")
def trial_info(job_id: str):
    with fitz.open(trial_path(job_id)) as document:
        return {"pages": len(document)}


@app.get("/api/jobs/{job_id}/trial/pdf/preview/{page_no}")
def trial_preview(job_id: str, page_no: int):
    return render_preview(trial_path(job_id), page_no)


@app.get("/api/sample/info")
@app.get("/api/jobs/{job_id}/pdf/info")
def pdf_info(job_id: str | None = None):
    with fitz.open(pdf_path(job_id)) as document:
        return {"pages": len(document)}


@app.get("/api/sample/preview/{page_no}")
@app.get("/api/jobs/{job_id}/pdf/preview/{page_no}")
def preview_page(page_no: int, job_id: str | None = None):
    return render_preview(pdf_path(job_id), page_no)


def render_preview(path, page_no):
    with fitz.open(path) as document:
        if not 1 <= page_no <= len(document):
            raise HTTPException(404, "页码不存在")
        image = document[page_no - 1].get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
        return Response(image.tobytes("png"), media_type="image/png")


app.mount("/static", StaticFiles(directory=ROOT / "paperflow" / "static"), name="static")


def main():
    import uvicorn
    import webbrowser
    port = int(os.getenv("PDF_WEB_PORT", "7860"))
    if not 1 <= port <= 65535:
        raise ValueError("PDF_WEB_PORT 必须在 1 到 65535 之间")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info"))
    stopped = threading.Event()
    if os.getenv("PAPERFLOW_NO_BROWSER") != "1":
        def open_when_ready():
            deadline = time.monotonic() + 30
            while not stopped.wait(.1) and time.monotonic() < deadline:
                if server.started and not stopped.is_set():
                    webbrowser.open(f"http://127.0.0.1:{port}")
                    return
        threading.Thread(target=open_when_ready, daemon=True, name="paperflow-browser").start()
    try:
        server.run()
        if not server.started:
            raise RuntimeError("本地服务未能就绪，请检查上方启动日志。")
    finally:
        stopped.set()


if __name__ == "__main__":
    main()
