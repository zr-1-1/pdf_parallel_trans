"""复用本机模型进行 OCR 和版面识别；所有处理均在本地，不下载模型。"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
from functools import lru_cache
from pathlib import Path

import fitz

from .layout import find_cjk_font


def layout_model_path():
    if any(importlib.util.find_spec(module) is None for module in ("onnxruntime", "numpy", "cv2")):
        return None
    configured = os.getenv("PAPERFLOW_LAYOUT_MODEL")
    candidates = [Path(configured)] if configured else [
        Path.home() / ".cache/babeldoc/models/doclayout_yolo_docstructbench_imgsz1024.onnx"]
    return next((path for path in candidates if path.is_file()), None)


def ocr_available():
    spec = importlib.util.find_spec("rapidocr_onnxruntime")
    return bool(spec and spec.origin and len(list((Path(spec.origin).parent / "models").glob("*.onnx"))) >= 3)


@lru_cache(maxsize=1)
def ocr_engine():
    if not ocr_available():
        raise ValueError("本 Conda 环境缺少 RapidOCR 或本地模型，请配置后重试；程序不会自动安装或下载")
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1, max_side_len=2400)


@lru_cache(maxsize=1)
def layout_engine():
    path = layout_model_path()
    if path is None:
        return None
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads, options.inter_op_num_threads = 2, 1
    session = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
    names = ast.literal_eval(session.get_modelmeta().custom_metadata_map["names"])
    return session, names


def detect_layout(page):
    """沿用本机 BabelDOC 的 DocLayout-YOLO 权重，直接读取 ONNX 输出。"""
    engine = layout_engine()
    if engine is None:
        return []
    import cv2
    import numpy as np
    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), colorspace=fitz.csRGB, alpha=False)
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    scale = min(1024 / pix.height, 1024 / pix.width)
    height, width = round(pix.height * scale), round(pix.width * scale)
    pad_h, pad_w = (1024 - height) % 32, (1024 - width) % 32
    image = cv2.resize(image, (width, height))
    image = cv2.copyMakeBorder(image, pad_h // 2, pad_h - pad_h // 2,
                              pad_w // 2, pad_w - pad_w // 2, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    tensor = image.transpose(2, 0, 1)[None].astype(np.float32) / 255
    session, names = engine
    predictions = session.run(None, {session.get_inputs()[0].name: tensor})[0].reshape(-1, 6)
    output = []
    for x0, y0, x1, y1, score, category in predictions:
        if score < .4:
            continue
        rect = fitz.Rect((x0 - pad_w // 2) / scale * page.rect.width / pix.width,
                         (y0 - pad_h // 2) / scale * page.rect.height / pix.height,
                         (x1 - pad_w // 2) / scale * page.rect.width / pix.width,
                         (y1 - pad_h // 2) / scale * page.rect.height / pix.height) & page.rect
        if not rect.is_empty:
            output.append({"type": names[int(category)], "bbox": list(rect), "score": round(float(score), 3)})
    return output


def _rect(points, scale_x, scale_y):
    return fitz.Rect(min(p[0] for p in points) * scale_x, min(p[1] for p in points) * scale_y,
                     max(p[0] for p in points) * scale_x, max(p[1] for p in points) * scale_y)


def recognize_page(page, detections):
    import cv2
    import numpy as np
    zoom = min(3.0, 2400 / max(page.rect.width, page.rect.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    result, _ = ocr_engine()(image[:, :, ::-1].copy(), return_word_box=True, text_score=.3)
    # 文字之外的连通轮廓补充模型容易漏掉的简单图框、表格；不把整页扫描背景当一张图。
    mask = (cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) < 190).astype(np.uint8) * 255
    for item in result or []:
        quad = np.asarray(item[0], dtype=np.int32)
        cv2.fillPoly(mask, [quad], 0)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        rect = fitz.Rect(x * page.rect.width / pix.width, y * page.rect.height / pix.height,
                         (x + w) * page.rect.width / pix.width, (y + h) * page.rect.height / pix.height)
        if rect.width > 80 and rect.height > 40 and rect.get_area() < page.rect.get_area() * .85:
            if not any(fitz.Rect(item["bbox"]).contains(rect) for item in detections if item["type"] in {"figure", "table"}):
                detections.append({"type": "figure", "bbox": list(rect), "source": "scan_contour"})
    protected = [fitz.Rect(item["bbox"]) for item in detections
                 if item["type"] in {"figure", "table", "isolate_formula"}]
    sx, sy = page.rect.width / pix.width, page.rect.height / pix.height
    lines, words, uncertain = [], [], 0
    for item in result or []:
        rect = _rect(item[0], sx, sy)
        if rect.is_empty or not str(item[1]).strip():
            continue
        if any((rect & box).get_area() > rect.get_area() * .6 for box in protected):
            continue
        # 明显竖排/倾斜行不猜测阅读顺序，保留图像并记录提示。
        if abs(item[0][1][1] - item[0][0][1]) > max(5, (item[0][2][1] - item[0][0][1]) * .4):
            uncertain += 1
            continue
        text, score = str(item[1]), float(item[2])
        uncertain += score < .8 or bool(re.search(r"[A-Za-z]{25,}", text))
        lines.append({"text": text, "bbox": list(rect), "score": score})
        if len(item) >= 5 and len(item[3]) == len(item[4]):
            # RapidOCR 1.4 返回英文字母级框；按识别文本中的词重新组合，不能把整行字母都当变量。
            pieces = [(re.sub(r"\s", "", str(word)), _rect(box, sx, sy)) for box, word in zip(item[3], item[4])]
            if "".join(word for word, _ in pieces) == re.sub(r"\s", "", text):
                ranges, offset = [], 0
                for word, box in pieces:
                    ranges.append((offset, offset + len(word), box))
                    offset += len(word)
                offset = 0
                tokens = text.split()
                for token_no, word in enumerate(tokens):
                    boxes = [box for start, end, box in ranges if start < offset + len(word) and end > offset]
                    # OCR 有时漏掉变量前的空格；仅当原图字符框确有间隙且后接运算符时拆开。
                    last = next((box for start, end, box in ranges if start <= offset + len(word) - 1 < end), None)
                    before = next((box for start, end, box in ranges if start <= offset + len(word) - 2 < end), None)
                    split_variable = (len(word) > 2 and token_no + 1 < len(tokens)
                                      and re.match(r"[=<>±+−]", tokens[token_no + 1])
                                      and re.fullmatch(r"[A-Za-z]+", word)
                                      and last is not None and before is not None
                                      and last.x0 - before.x1 > rect.height * .18)
                    offset += len(word)
                    if not boxes:
                        continue
                    word_rect = fitz.Rect(boxes[0])
                    for box in boxes[1:]:
                        word_rect |= box
                    if split_variable:
                        prefix = fitz.Rect(word_rect.x0, word_rect.y0, before.x1, word_rect.y1)
                        words.append({"text": word[:-1], "bbox": list(prefix), "size": rect.height * .8, "font": "OCR", "flags": 0})
                        word, word_rect = word[-1], last
                    words.append({"text": word, "bbox": list(word_rect), "size": rect.height * .8, "font": "OCR", "flags": 0})
    return pix, lines, words, uncertain


def prepare_pdf(source_path, output_path, pages, ocr="auto", progress=None, cancel=None):
    """原文件不变；需要 OCR 时为工作副本添加不可见文字层。"""
    if ocr not in {"auto", "off", "force"}:
        raise ValueError("OCR 模式无效")
    report = {"pages": {}, "ocr_pages": [], "warnings": [], "layout_model": layout_model_path() is not None}
    with fitz.open(source_path) as source, fitz.open() as prepared:
        for pno in range(len(source)):
            page = source[pno]
            if pno not in pages:
                prepared.insert_pdf(source, from_page=pno, to_page=pno)
                continue
            if cancel and cancel.is_set():
                from .translate import Cancelled
                raise Cancelled("任务已取消")
            if page.rotation:
                raise ValueError(f"第 {pno + 1} 页存在页面旋转，请先转正后重试")
            if progress:
                progress(pno, "正在检查版面与文字层")
            detections = detect_layout(page)
            native = page.get_text("text").strip()
            images = page.get_image_info()
            image_area = sum((fitz.Rect(info["bbox"]) & page.rect).get_area() for info in images)
            use_ocr = ocr == "force" or (ocr == "auto" and len(native) < 120 and image_area > page.rect.get_area() * .4)
            info = {"ocr": use_ocr, "detections": detections}
            report["pages"][str(pno)] = info
            if use_ocr:
                if progress:
                    progress(pno, "正在本地 OCR 识别")
                pix, lines, words, uncertain = recognize_page(page, detections)
                if not lines:
                    raise ValueError(f"第 {pno + 1} 页 OCR 未识别到正文，请检查清晰度和方向；未发起翻译")
                target = prepared.new_page(width=page.rect.width, height=page.rect.height)
                target.insert_image(target.rect, stream=pix.tobytes("png"))
                font_path = find_cjk_font(os.getenv("CJK_FONT_PATH"))
                font = fitz.Font(fontfile=font_path)
                target.insert_font(fontname="PFOCR", fontfile=font_path)
                for line in lines:
                    rect = fitz.Rect(line["bbox"])
                    size = rect.height / (font.ascender - font.descender)
                    width = font.text_length(line["text"], fontsize=size)
                    if width <= 0:
                        continue
                    origin = fitz.Point(rect.x0, rect.y0 + font.ascender * size)
                    target.insert_text(origin, line["text"], fontsize=size, fontname="PFOCR", render_mode=3,
                                       morph=(origin, fitz.Matrix(rect.width / width, 1)))
                info.update(ocr_words=words, uncertain_lines=uncertain)
                report["ocr_pages"].append(pno + 1)
                if uncertain:
                    report["warnings"].append(f"第 {pno + 1} 页有 {uncertain} 行低置信度、疑似空格缺失或倾斜文字，需对照原图核对")
                if not report["layout_model"]:
                    report["warnings"].append(f"第 {pno + 1} 页未使用版面模型，扫描图表和跨栏边界需人工核对")
            else:
                if len(native) < 30 and image_area > page.rect.get_area() * .4:
                    raise ValueError(f"第 {pno + 1} 页是扫描页，请开启 OCR 后重试；未发起翻译")
                prepared.insert_pdf(source, from_page=pno, to_page=pno)
        prepared.set_metadata(source.metadata)
        if report["ocr_pages"]:
            prepared.save(output_path, garbage=4, deflate=True)
            return Path(output_path), report
    return Path(source_path), report
