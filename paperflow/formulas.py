"""将可识别的行内公式作为原文裁片保护，翻译服务只接收稳定占位符。"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace

import fitz

TOKEN = re.compile(r"\[\[PFM_[A-Za-z0-9_]+\]\]")
MATH_SYMBOL = re.compile(r"[=+<>±∓×÷∫∑∏√∞≤≥≠∈∉∂∇−]|[\u0370-\u03ff\u2070-\u209f\u2200-\u22ff\U0001d400-\U0001d7ff]")
MATH_FONT = re.compile(r"(?:CMMI|CMSY|CMEX|MTMI|MTSY|Math|Symbol|MSAM|MSBM)", re.I)
FUNCTIONS = {"sin", "cos", "tan", "log", "ln", "exp", "min", "max", "lim", "det", "diag", "arg", "Pr"}


def page_words(page):
    words = []
    for block in page.get_text("rawdict", flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"]:
        for line in block.get("lines", []):
            if abs(line["dir"][1]) > .1:
                continue
            for span in line["spans"]:
                group = []
                for char in span["chars"] + [{"c": " "}]:
                    if char["c"].isspace():
                        if group:
                            rect = fitz.Rect(group[0]["bbox"])
                            for item in group[1:]:
                                rect |= fitz.Rect(item["bbox"])
                            if rect.height > span["size"] * 1.4 and MATH_FONT.search(span["font"]):
                                # 部分符号字体声明的 descender 过大，越过了下一行正文。
                                rect.y0 = max(rect.y0, span["origin"][1] - span["size"] * .95)
                                rect.y1 = min(rect.y1, span["origin"][1] + span["size"] * .25)
                            words.append({"text": "".join(item["c"] for item in group), "bbox": list(rect),
                                          "size": span["size"], "flags": span["flags"], "font": span["font"]})
                            group = []
                    else:
                        group.append(char)
    return words


def _math_word(word):
    text = word["text"].strip(".,;:()[]{} ")
    letters = re.findall(r"[A-Za-z]+", text)
    symbol = bool(MATH_SYMBOL.search(text))
    special_font = bool(MATH_FONT.search(word["font"]))
    allowed = all(len(item) <= 1 or item in FUNCTIONS for item in letters)
    allowed |= (symbol or special_font) and sum(map(len, letters)) <= 8
    allowed |= (bool(word["flags"] & 2) or word["font"] == "OCR") and all(len(item) <= 2 and item.lower() not in
        {"as", "at", "in", "on", "of", "to", "is", "it", "we", "by", "be", "an", "or", "if", "no", "so", "us"}
        for item in letters)
    italic_variable = bool(word["flags"] & 2) and allowed and bool(letters)
    super_sub = bool(word["flags"] & 1)
    candidate = allowed and (bool(text) or bool(re.fullmatch(r"[()[\]{},.]+", word["text"])))
    return candidate, candidate and (symbol or special_font or italic_variable or super_sub)


def math_regions(words, excluded=()):
    candidates = []
    for index, word in enumerate(words):
        rect = fitz.Rect(word["bbox"])
        safe, anchor = _math_word(word)
        if safe and not any((rect & box).get_area() > rect.get_area() * .5 for box in excluded):
            candidates.append(({**word, "index": index}, rect, anchor))
    parents = list(range(len(candidates)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    # ponytail: 数学候选词按页做两两邻接，论文通常只有几十个；密集大型表格先排除。
    for i, (word, rect, _) in enumerate(candidates):
        for j in range(i):
            other_word, other, _ = candidates[j]
            size = max(word["size"], other_word["size"])
            dx = max(rect.x0 - other.x1, other.x0 - rect.x1, 0)
            dy = abs((rect.y0 + rect.y1 - other.y0 - other.y1) / 2)
            # 上下标与分子分母可并入，不能跨到正常的下一行正文。
            vertical = 1.1 if word["font"] == other_word["font"] == "OCR" else .85
            if dx <= size * 1.0 and dy < size * vertical:
                parents[root(i)] = root(j)
    groups = {}
    for i, item in enumerate(candidates):
        groups.setdefault(root(i), []).append(item)
    result = []
    for group in groups.values():
        if not any(item[2] for item in group):
            continue
        rect = fitz.Rect(group[0][1])
        for _, part, _ in group[1:]:
            rect |= part
        result.append({"bbox": list(rect), "size": max(item[0]["size"] for item in group),
                       "indices": [item[0]["index"] for item in group],
                       "text": " ".join(item[0]["text"] for item in sorted(group, key=lambda item: (item[1].y0, item[1].x0)))})
    return result


def _reading_text(parts):
    """把分子、分母与正文的多个 PDF span 恢复为行，再按水平方向组合。"""
    rows = []
    for rect, text in sorted(parts, key=lambda item: ((item[0].y0 + item[0].y1) / 2, item[0].x0)):
        center = (rect.y0 + rect.y1) / 2
        row = next((row for row in reversed(rows) if abs(row[0] - center) < min(rect.height, row[1]) * .55), None)
        if row is None:
            row = [center, rect.height, []]
            rows.append(row)
        row[2].append((rect, text))
    result = []
    for row in rows:
        line, previous = "", None
        for rect, text in sorted(row[2], key=lambda item: item[0].x0):
            adjacent = previous and rect.x0 - previous.x1 < min(rect.height, previous.height) * .18
            separator = "" if adjacent and "[[PFM_" not in text and not line.endswith("]]" ) else " "
            line += separator + text
            previous = rect
        result.append(line.strip())
    text = re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", "\n".join(result))
    return re.sub(r"\s+", " ", text).strip()


def protect_paragraphs(page, paragraphs, ocr_words=None, excluded=()):
    words = page_words(page) if ocr_words is None else ocr_words
    regions = math_regions(words, excluded)
    if ocr_words is not None:
        for item in regions:
            rect = fitz.Rect(item["bbox"])
            # 小分子或上标可能未识别；从原图局部墨迹补齐，不猜测其字符内容。
            import numpy as np
            above = .8 if rect.height > item["size"] * 1.4 else .35
            clip = (rect + (-.5, -item["size"] * above, .5, item["size"] * .2)) & page.rect
            pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(3, 3), colorspace=fitz.csGRAY, alpha=False)
            pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
            y, x = np.where(pixels < 160)
            if len(x):
                ink = fitz.Rect((pix.x + x.min()) / 3, (pix.y + y.min()) / 3,
                                 (pix.x + x.max() + 1) / 3, (pix.y + y.max() + 1) / 3)
                item["bbox"] = list(rect | ink)
    output = []
    for para in paragraphs:
        rect = fitz.Rect(para.left, para.top, para.right, para.bottom)
        used = [item for item in regions if (fitz.Rect(item["bbox"]) & rect).get_area()
                > fitz.Rect(item["bbox"]).get_area() * .4]
        if not used:
            output.append(para)
            continue
        formulas = []
        for index, item in enumerate(sorted(used, key=lambda item: (item["bbox"][1], item["bbox"][0]))):
            formula_rect = fitz.Rect(item["bbox"])
            rect |= formula_rect
            formulas.append({**item, "token": f"[[PFM_{para.id}_{index:02d}]]"})
        parts = []
        protected_indices = {index for item in formulas for index in item["indices"]}
        for index, word in enumerate(words):
            box = fitz.Rect(word["bbox"])
            if not rect.contains(fitz.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)):
                continue
            if index in protected_indices:
                continue
            parts.append((box, word["text"]))
        parts.extend((fitz.Rect(item["bbox"]), item["token"]) for item in formulas)
        text = _reading_text(parts)
        output.append(replace(para, top=rect.y0, bottom=rect.y1, left=rect.x0, right=rect.x1,
                              text=text, formulas=formulas))
    return output


def check_formula_tokens(text, paragraph):
    expected = Counter(item["token"] for item in paragraph.formulas)
    if Counter(TOKEN.findall(text)) != expected or "[[PFM" in TOKEN.sub("", text).upper():
        raise ValueError("译文中的公式占位符缺失、重复或被修改，已停止以保护原文公式；请重试此翻译任务")
