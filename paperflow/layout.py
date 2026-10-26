"""从 PaperFlow MVP 的矢量裁切排版扩展，保留原文、公式和图表。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from itertools import groupby
from pathlib import Path
from statistics import median

import fitz

from .formulas import TOKEN, check_formula_tokens, protect_paragraphs


@dataclass(frozen=True)
class Paragraph:
    id: str
    page: int
    top: float
    bottom: float
    left: float
    right: float
    text: str
    region: int = 0
    formulas: list[dict] = field(default_factory=list)


def parse_pages(value: str, total: int) -> list[int]:
    if not value.strip():
        return list(range(total))
    pages = set()
    for part in value.replace("，", ",").split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not match:
            raise ValueError("页码格式应为 1-5,8；留空表示全部页")
        start, end = int(match[1]), int(match[2] or match[1])
        if not 1 <= start <= end <= total:
            raise ValueError(f"页码必须在 1 到 {total} 之间，起始页不能大于结束页")
        pages.update(range(start - 1, end))
    return sorted(pages)


def clean_text_controls(text: str) -> str:
    """PDF 字体编码有时混入控制字符，不能把它们当作可打印数学符号。"""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)


def _clean(text: str) -> str:
    text = clean_text_controls(text)
    text = re.sub(r"(?<=[a-z])-\s*\n(?=[a-z])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _prose(text: str) -> bool:
    words = re.findall(r"[A-Za-z]{2,}", text)
    return (len(words) >= 3 and len("".join(words)) > len(text) * .35) or (len(words) >= 2 and bool(re.match(r"^\d+[\d.]*\s", text))) or bool(
        re.fullmatch(r"(?:[\d.]+\s+)?(?:Abstract|Introduction|Conclusion[s]?|References|Keywords)", text, re.I)
    )


def page_bounds(page: fitz.Page) -> tuple[float, float]:
    """只去掉明确的短页眉、页脚；不裁掉靠近边缘的正文。"""
    top, bottom = 0.0, page.rect.height
    for block in page.get_text("blocks"):
        if block[6] != 0:
            continue
        text = _clean(str(block[4]))
        if re.fullmatch(r"\d{1,4}", text) and block[3] < page.rect.height * .075:
            top = max(top, block[3] + 2)
        if re.fullmatch(r"\d{1,4}", text) and block[1] > page.rect.height * .94:
            bottom = min(bottom, block[1] - 2)
    return top, bottom


def algorithm_regions(page: fitz.Page) -> list[fitz.Rect]:
    blocks = [b for b in page.get_text("blocks") if b[6] == 0]
    if not any(re.match(r"Algorithm\s+\d+", str(b[4]).strip(), re.I) for b in blocks):
        return []
    numbered = [fitz.Rect(b[:4]) for b in blocks if re.search(r"(?m)^\d{1,3}:\s", str(b[4]))]
    if not numbered:
        return []
    bounds = numbered[0]
    for rect in numbered[1:]:
        bounds |= rect
    return [bounds]


def caption_units(page: fitz.Page, info=None) -> list[tuple[fitz.Rect, fitz.Rect]]:
    """将多子图与图题、三线表与表题关联，分页时保持完整。"""
    captions = sorted((b for b in page.get_text("blocks") if b[6] == 0
                       and re.match(r"(?:Fig\.|Table|Algorithm)\s*\d", str(b[4]).strip())), key=lambda b: b[1])
    graphics = [r for r in page.cluster_drawings() if r.width > 35 and r.height > 25]
    graphics += [fitz.Rect(info["bbox"]) for info in page.get_image_info()
                 if fitz.Rect(info["bbox"]).get_area() < page.rect.get_area() * .85]
    graphics += [fitz.Rect(item["bbox"]) for item in (info or {}).get("detections", []) if item["type"] == "figure"]
    tables = [fitz.Rect(item["bbox"]) for item in (info or {}).get("detections", []) if item["type"] == "table"]
    # 零高度的横线在 Rect 并集运算中被视为空，先扩展到实际线宽。
    rules = [fitz.Rect(d["rect"].x0, d["rect"].y0 - .5, d["rect"].x1, d["rect"].y1 + .5)
             for d in page.get_drawings() if d["rect"].width > page.rect.width * .2 and d["rect"].height < 1]
    units = []
    previous_end = page_bounds(page)[0]
    for index, block in enumerate(captions):
        caption = fitz.Rect(block[:4])
        text = str(block[4]).strip()
        next_top = captions[index + 1][1] if index + 1 < len(captions) else page_bounds(page)[1]
        if text.startswith("Fig."):
            pieces = [r for r in graphics if r.y0 >= previous_end - 3 and r.y0 < caption.y1
                      and min(r.x1, caption.x1) - max(r.x0, caption.x0) > min(r.width, caption.width) * .5]
        elif text.startswith("Table"):
            pieces = [r for r in rules if caption.y1 - 1 <= r.y0 < next_top
                      and min(r.x1, caption.x1) - max(r.x0, caption.x0) > min(r.width, caption.width) * .5]
            if len(pieces) < 2:
                pieces = []
            pieces += [r for r in tables if caption.y1 - 2 <= r.y0 < min(next_top, caption.y1 + 60)
                       and min(r.x1, caption.x1) - max(r.x0, caption.x0) > min(r.width, caption.width) * .5]
        else:
            pieces = algorithm_regions(page)
        if pieces:
            unit = fitz.Rect(caption)
            for rect in pieces:
                unit |= rect
            if text.startswith("Fig."):
                unit.y0 = max(previous_end, unit.y0 - 8)  # 轴标及图例可能稍高于图框。
            units.append((caption, unit))
            previous_end = unit.y1
        else:
            previous_end = caption.y1
    return units


def protected_regions(page, info=None):
    """图、表、独立公式和算法整体保留，避免把内部标注混入正文。"""
    rects = [fitz.Rect(item["bbox"]) for item in (info or {}).get("detections", [])
             if item["type"] in {"figure", "table", "isolate_formula"}]
    rects += [r for r in page.cluster_drawings() if r.width > 80 and r.height > 40]
    rects += [fitz.Rect(item["bbox"]) for item in page.get_image_info()
              if fitz.Rect(item["bbox"]).get_area() < page.rect.get_area() * .85]
    for caption, unit in caption_units(page, info):
        if unit.y0 < caption.y0 - 5:
            rects.append(fitz.Rect(unit.x0, unit.y0, unit.x1, caption.y0))
        elif unit.y1 > caption.y1 + 5:
            rects.append(fitz.Rect(unit.x0, caption.y1, unit.x1, unit.y1))
    return rects + algorithm_regions(page)


def reading_regions(page, columns="auto", info=None):
    """常规双栏按左、右顺序阅读；跨栏标题、图表所在横带保持完整。"""
    if columns not in {"auto", "single", "double"}:
        raise ValueError("分栏模式无效")
    top, bottom = page_bounds(page)
    whole = fitz.Rect(0, top, page.rect.width, bottom)
    if columns == "single":
        return [whole]
    protected = protected_regions(page, info)
    lines = [line for block in page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"]
             for line in block.get("lines", []) if abs(line["dir"][1]) < .1]
    prose = [fitz.Rect(line["bbox"]) for line in lines
             if len(re.findall(r"[A-Za-z]{2,}", "".join(s["text"] for s in line["spans"]))) >= 3
             and not any(r.contains(fitz.Rect(line["bbox"])) for r in protected)]
    width, mid = page.rect.width, page.rect.width / 2
    left = [r for r in prose if r.width > width * .19 and r.x1 < width * .54]
    right = [r for r in prose if r.width > width * .19 and r.x0 > width * .46]
    evidence = (len(left) >= 3 and len(right) >= 3
                and min(max(r.y1 for r in left), max(r.y1 for r in right))
                - max(min(r.y0 for r in left), min(r.y0 for r in right)) > 30)
    if columns == "auto" and not evidence:
        return [whole]
    if left and right:
        left_edge = median(sorted((r.x1 for r in left), reverse=True)[:5])
        right_edge = median(sorted(r.x0 for r in right)[:5])
        if right_edge - left_edge >= 6:
            mid = (left_edge + right_edge) / 2
    spans = [fitz.Rect(line["bbox"]) for line in lines
             if line["bbox"][0] < mid - 8 and line["bbox"][2] > mid + 8]
    spans += protected + [unit for _, unit in caption_units(page, info)]
    bands = []
    for rect in sorted(spans, key=lambda r: r.y0):
        if rect.x0 >= mid - 8 or rect.x1 <= mid + 8 or rect.y1 <= top or rect.y0 >= bottom:
            continue
        a, b = max(top, rect.y0 - 1), min(bottom, rect.y1 + 1)
        if bands and a <= bands[-1][1] + 2:
            bands[-1][1] = max(bands[-1][1], b)
        else:
            bands.append([a, b])
    regions, cursor = [], top
    for a, b in bands + [[bottom, bottom]]:
        if a - cursor > 1:
            regions.extend([fitz.Rect(0, cursor, mid, a), fitz.Rect(mid, cursor, width, a)])
        if b > a:
            regions.append(fitz.Rect(0, a, width, b))
        cursor = b
    return regions or [whole]


def _region_paragraphs(page, region, region_no, output, in_references, graphics, info):
    """在一个阅读区域内恢复段落；区域间不会合并同一高度的左右栏。"""
    pno = page.number
    blocks = [b for b in page.get_text("dict", clip=region,
              flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"] if b["type"] == 0]
    # 分数或字体切换可把同一行拆成多个 block，必须在筛选正文前合并。
    merged = []
    for block in sorted(blocks, key=lambda b: b["bbox"][1]):
        rect = fitz.Rect(block["bbox"])
        lines = list(block["lines"])
        for other in list(merged):
            box = fitz.Rect(other["bbox"])
            gap = max(rect.x0 - box.x1, box.x0 - rect.x1, 0)
            if min(rect.y1, box.y1) - max(rect.y0, box.y0) > 1 and gap < 30:
                rect |= box
                lines.extend(other["lines"])
                merged.remove(other)
        merged.append({"bbox": list(rect), "lines": sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))})
    blocks = merged
    if info.get("ocr"):
        # OCR 的每行通常独立成块，先按相邻行恢复段落，避免短行被当成非正文丢掉。
        merged = []
        for block in sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0])):
            rect = fitz.Rect(block["bbox"])
            if merged:
                previous = merged[-1]
                box = fitz.Rect(previous["bbox"])
                size = median(s["size"] for line in block["lines"] for s in line["spans"])
                if (0 <= rect.y0 - box.y1 < size * 1.4 and
                        min(rect.x1, box.x1) - max(rect.x0, box.x0) > min(rect.width, box.width) * .6):
                    previous["bbox"] = list(box | rect)
                    previous["lines"].extend(block["lines"])
                    continue
            merged.append(block)
        blocks = merged
    last_end = -1.0
    for block in sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0])):
        block_text = "\n".join("".join(s["text"] for s in line["spans"]) for line in block["lines"])
        if len(re.findall(r"(?m)^\d{1,3}:\s", block_text)) >= 2:
            continue
        rows = []
        for line in block["lines"]:
            if abs(line["dir"][1]) > .1:
                continue
            txt = "".join(s["text"] for s in line["spans"])
            rect = fitz.Rect(line["bbox"])
            size = max((s["size"] for s in line["spans"]), default=10)
            if not txt.strip() or any((rect & r).get_area() > rect.get_area() * .8 for r in graphics):
                continue
            if rows and rect.y0 < rows[-1][0].y1 - min(size, rows[-1][2]) * .45:
                prev, old_text, old_size = rows[-1]
                rows[-1] = (prev | rect, old_text + " " + txt, max(size, old_size))
            else:
                rows.append((rect, txt, size))
        if not rows:
            continue
        left, right = min(r[0].x0 for r in rows), max(r[0].x1 for r in rows)
        size = median(r[2] for r in rows)
        groups, current = [], []
        for row in rows:
            if current:
                prev = current[-1][0]
                indent = row[0].x0 > left + size * .65
                short_end = (prev.x1 < right - max(size * 3, (right - left) * .15)
                             and bool(re.search(r"[.!?:;]$", current[-1][1].strip())))
                gap = row[0].y0 - prev.y1 > size * .65
                if gap or (indent and row[0].x0 < left + size * 3) or short_end:
                    groups.append(current)
                    current = []
            current.append(row)
        groups.append(current)
        for group in groups:
            text = _clean("\n".join(r[1] for r in group))
            if re.fullmatch(r"(?:[\d.]+\s*)?(References|Bibliography)", text, re.I):
                in_references = True
            ocr_sentence = info.get("ocr") and len(re.findall(r"[A-Za-z]", text)) >= 25
            if in_references or (not _prose(text) and not ocr_sentence):
                continue
            rect = fitz.Rect(group[0][0])
            for row in group[1:]:
                rect |= row[0]
            heading = bool(re.match(r"^\d+[\d.]*\s", text)) or text.lower() in {"abstract", "introduction", "conclusions"}
            if rect.width < region.width * .35 and not heading:
                continue
            if rect.y0 < last_end - 3:
                previous = output[-1]
                overlap = min(previous.right, rect.x1) - max(previous.left, rect.x0)
                if overlap > min(previous.right - previous.left, rect.width) * .65:
                    output[-1] = replace(previous, top=min(previous.top, rect.y0),
                        bottom=max(previous.bottom, rect.y1), left=min(previous.left, rect.x0),
                        right=max(previous.right, rect.x1), text=previous.text + " " + text)
                    last_end = max(last_end, rect.y1)
                    continue
                raise ValueError(f"第 {pno + 1} 页正文有重叠，请尝试手动选择分栏模式或缩小页码范围")
            last_end = rect.y1
            output.append(Paragraph(f"p{pno + 1:03d}_{len(output):04d}", pno,
                                    rect.y0, rect.y1, rect.x0, rect.x1, text, region_no))
    return in_references


def collect_paragraphs(doc: fitz.Document, pages: list[int] | None = None, *,
                       columns="auto", analysis=None, protect_formulas=True) -> list[Paragraph]:
    output = []
    in_references = False
    for pno in pages if pages is not None else range(len(doc)):
        page = doc[pno]
        if page.rotation:
            raise ValueError(f"第 {pno + 1} 页有旋转。请先把页面转正后再上传")
        info = (analysis or {}).get("pages", {}).get(str(pno), {})
        regions = reading_regions(page, columns, info)
        info["regions"] = [list(r) for r in regions]
        info["columns"] = 2 if any(r.width < page.rect.width * .7 for r in regions) else 1
        graphics = protected_regions(page, info)
        graphics += [fitz.Rect(item["bbox"]) for item in info.get("detections", []) if item["type"] == "abandon"]
        start = len(output)
        for region_no, region in enumerate(regions):
            in_references = _region_paragraphs(page, region, region_no, output, in_references, graphics, info)
        if protect_formulas:
            output[start:] = protect_paragraphs(page, output[start:], info.get("ocr_words"), graphics)
    return output


def find_cjk_font(explicit: str | None = None) -> str:
    candidates = [explicit] if explicit else [str(Path(__file__).parent / "fonts" / "NotoSansCJKsc-Regular.otf"),
        r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/System/Library/Fonts/PingFang.ttc"]
    for path in candidates:
        if path and Path(path).is_file():
            return str(Path(path).resolve())
    raise ValueError("未找到中文字体，请设置 CJK_FONT_PATH 为中文 .ttf/.ttc 字体的完整路径")


def wrap_lines(text: str, font: fitz.Font, fontsize: float, width: float,
               fallback: fitz.Font | None = None) -> list[str]:
    result = []
    closing = set("。，！？：；）》」』%、,.!?;:)]}")
    for paragraph in text.replace("\r", "").split("\n"):
        line, used = "", 0.0
        for char, advance in zip(paragraph, font.char_lengths(paragraph, fontsize=fontsize)):
            if fallback and not font.has_glyph(ord(char)):
                advance = fallback.text_length(char, fontsize=fontsize)
            if line and used + advance > width:
                if char in closing and len(line) > 1:
                    result.append(line[:-1].rstrip())
                    line = line[-1]
                    chosen = fallback if fallback and not font.has_glyph(ord(line)) else font
                    used = chosen.text_length(line, fontsize=fontsize)
                else:
                    result.append(line.rstrip())
                    line, used = "", 0
            if not line and char.isspace():
                continue
            line += char
            used += advance
        result.append(line.rstrip())
    return result


class PageComposer:
    def __init__(self, output, source, font_path, fontsize=10.5):
        self.output, self.source = output, source
        self.font_path = font_path
        self.font = fitz.Font(fontfile=font_path)
        math_path = Path(r"C:\Windows\Fonts\cambria.ttc")
        self.math_font = fitz.Font(fontfile=str(math_path)) if math_path.is_file() else fitz.Font("china-s")
        self.fontsize, self.line_h = fontsize, fontsize * 1.55
        self.width, self.height = fitz.paper_size("a4")
        self.margin, self.top, self.bottom = 42.0, 40.0, self.height - 38
        self.page = None
        self.y = self.top
        self.new_page()

    def new_page(self):
        self.page = self.output.new_page(width=self.width, height=self.height)
        self.page.insert_font(fontname="ZHCJK", fontfile=self.font_path)
        self.math_font_added = False
        self.y = self.top

    def translation_font(self, use_primary):
        if use_primary:
            return "ZHCJK"
        if not self.math_font_added:
            self.page.insert_font(fontname="PFMath", fontbuffer=self.math_font.buffer)
            self.math_font_added = True
        return "PFMath"

    def original(self, pno, upper, lower, bounds, obstacles):
        """在行间空白处分页；单个超高图表缩放到一页，不从中切开。"""
        x0, x1 = bounds
        scale = min(1.25, (self.width - 2 * self.margin) / (x1 - x0))
        left = (self.width - (x1 - x0) * scale) / 2
        while lower - upper > .2:
            if self.bottom - self.y < 14:
                self.new_page()
            end = min(lower, upper + (self.bottom - self.y) / scale)
            if end < lower - .2:
                # 反复回退，直至裁切线不穿过文字、图片或矢量图。
                while True:
                    crossing = [a for a, b in obstacles if a - 1 < end < b + 1 and b > upper + .2]
                    earlier = min(crossing, default=end + 1) - 1
                    if earlier >= end - .1:
                        break
                    end = earlier
                if end <= upper + 1:
                    if self.y > self.top + 1:
                        self.new_page()
                        continue
                    ends = [b for a, b in obstacles if a <= upper + 2 and b > upper]
                    end = min(lower, max(ends, default=lower) + 1)
                    local_scale = min(scale, (self.bottom - self.y) / (end - upper))
                else:
                    local_scale = scale
            else:
                local_scale = scale
            clip = fitz.Rect(x0, upper, x1, end)
            dest = fitz.Rect(left, self.y, left + clip.width * local_scale, self.y + clip.height * local_scale)
            self.page.show_pdf_page(dest, self.source, pno, clip=clip, keep_proportion=False)
            self.y += dest.height
            upper = end
        return left, left + (x1 - x0) * scale

    def formula_translation(self, text, left, right, formulas, pno):
        """公式直接引用原 PDF 裁片，保留分数线、上下标及数学字体。"""
        pad, width = 8, right - left - 16
        lookup = {item["token"]: item for item in formulas}
        lines, line, used, height = [], [], 0.0, self.line_h
        pieces = re.split(f"({TOKEN.pattern})", text)
        for piece in pieces:
            if piece in lookup:
                item = lookup[piece]
                box = fitz.Rect(item["bbox"]) + (-.3, -.3, .3, .3)
                box &= self.source[pno].rect
                scale = min(self.fontsize / item["size"], width / box.width,
                            (self.bottom - self.top - 2 * pad) / box.height)
                atoms = [(box, box.width * scale, box.height * scale + 2)]
            else:
                atoms = []
                for char in piece:
                    font = self.font if self.font.has_glyph(ord(char)) else self.math_font
                    atoms.append((char, font.text_length(char, fontsize=self.fontsize), self.line_h))
            for atom, advance, atom_height in atoms:
                if atom == "\n" or (line and used + advance > width):
                    lines.append((line, height))
                    line, used, height = [], 0.0, self.line_h
                if atom == "\n" or (not line and isinstance(atom, str) and atom.isspace()):
                    continue
                line.append((atom, advance, atom_height))
                used += advance
                height = max(height, atom_height)
        if line:
            lines.append((line, height))
        while lines:
            available = self.bottom - self.y - 2 * pad
            take, chunk_height = 0, 0.0
            for _, height in lines:
                if chunk_height + height > available:
                    break
                take += 1
                chunk_height += height
            if not take:
                self.new_page()
                continue
            chunk, lines = lines[:take], lines[take:]
            self.page.draw_rect(fitz.Rect(left, self.y, right, self.y + chunk_height + pad * 2),
                                color=None, fill=(.94, .945, .95))
            y = self.y + pad
            for atoms, height in chunk:
                x = left + pad
                for atom, advance, atom_height in atoms:
                    if isinstance(atom, fitz.Rect):
                        top = y + (height - atom_height + 2) / 2
                        self.page.show_pdf_page(fitz.Rect(x, top, x + advance, top + atom_height - 2),
                                                self.source, pno, clip=atom, keep_proportion=False)
                    else:
                        fontname = self.translation_font(self.font.has_glyph(ord(atom)))
                        baseline = y + (height - self.line_h) / 2 + self.fontsize * 1.06
                        self.page.insert_text((x, baseline), atom, fontsize=self.fontsize,
                                              fontname=fontname, color=(.14, .17, .2))
                    x += advance
                y += height
            self.y += chunk_height + 2 * pad + 7
            if lines:
                self.new_page()

    def translation(self, text, left, right, formulas=None, pno=None):
        if formulas:
            self.formula_translation(text, left, right, formulas, pno)
            return
        pad = 8
        lines = wrap_lines(text, self.font, self.fontsize, right - left - pad * 2, self.math_font)
        while lines:
            available = int((self.bottom - self.y - pad * 2) / self.line_h)
            if available < min(2, len(lines)):
                self.new_page()
                continue
            take = min(available, len(lines))
            # 避免把译文最后一行单独挤到下一页。
            if len(lines) - take == 1 and take > 2:
                take -= 1
            chunk, lines = lines[:take], lines[take:]
            height = len(chunk) * self.line_h + 2 * pad
            self.page.draw_rect(fitz.Rect(left, self.y, right, self.y + height), color=None, fill=(.94, .945, .95))
            baseline = self.y + pad + self.fontsize * 1.06
            for line in chunk:
                x = left + pad
                for use_primary, letters in groupby(line, key=lambda c: bool(self.font.has_glyph(ord(c)))):
                    part = "".join(letters)
                    font = self.font if use_primary else self.math_font
                    self.page.insert_text((x, baseline), part, fontsize=self.fontsize,
                                          fontname=self.translation_font(use_primary), color=(.14, .17, .2))
                    x += font.text_length(part, fontsize=self.fontsize)
                baseline += self.line_h
            self.y += height + 7
            if lines:
                self.new_page()


def render_inline_pdf(src_path: Path, dst_path: Path, paragraphs: list[Paragraph],
                      translations: dict[str, str], pages=None, font_path=None, fontsize=10.5, *,
                      columns="auto", analysis=None):
    font_path = find_cjk_font(font_path)
    if set(translations) != {p.id for p in paragraphs} or any(not t.strip() for t in translations.values()):
        raise ValueError("译文与原文段落不一一对应，已停止生成")
    for para in paragraphs:
        check_formula_tokens(translations[para.id], para)
    with fitz.open(src_path) as source, fitz.open() as output:
        composer = PageComposer(output, source, font_path, fontsize)
        for pno in pages if pages is not None else range(len(source)):
            page = source[pno]
            algorithms = algorithm_regions(page)
            info = (analysis or {}).get("pages", {}).get(str(pno), {})
            units = caption_units(page, info)
            regions = [fitz.Rect(r) for r in info["regions"]] if "regions" in info else reading_regions(page, columns, info)
            data = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
            page_rects = []
            for block in data["blocks"]:
                page_rects.extend(fitz.Rect(item["bbox"]) for item in block.get("lines", []))
            page_rects.extend(r for r in page.cluster_drawings() if r.height > 3 and r.width > 3)
            page_rects.extend(protected_regions(page, info))
            page_rects.extend(rect for _, rect in units)
            if not info.get("ocr"):
                page_rects.extend(fitz.Rect(item["bbox"]) for item in page.get_image_info())
            for region_no, region in enumerate(regions):
                rects = [r & region for r in page_rects if r.intersects(region)]
                obstacles = [(r.y0, r.y1) for r in rects]
                if info.get("ocr"):
                    # 按扫描图的实际墨迹裁去空白；未被 OCR 识别的笔画仍保留。
                    import numpy as np
                    pix = page.get_pixmap(clip=region, colorspace=fitz.csGRAY, alpha=False)
                    pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
                    y, x = np.where(pixels < 235)
                    if not len(x):
                        continue
                    rects.append(fitz.Rect(pix.x + int(x.min()), pix.y + int(y.min()),
                                           pix.x + int(x.max()) + 1, pix.y + int(y.max()) + 1) & region)
                if not rects:
                    continue
                x0, x1 = max(region.x0, min(r.x0 for r in rects) - 2), min(region.x1, max(r.x1 for r in rects) + 2)
                top, bottom = max(region.y0, min(r.y0 for r in rects) - 2), min(region.y1, max(r.y1 for r in rects) + 2)
                current = top
                for para in [p for p in paragraphs if p.page == pno and p.region == region_no]:
                    para_rect = fitz.Rect(para.left, para.top, para.right, para.bottom)
                    if any(r.contains(para_rect) for r in algorithms):
                        continue
                    unit = next((unit for caption, unit in units if caption.intersects(para_rect)), None)
                    scale = min(1.25, (composer.width - 2 * composer.margin) / (x1 - x0))
                    required = (para.bottom - current) * scale + composer.line_h * 2 + 23
                    if unit is not None:
                        lines = wrap_lines(clean_text_controls(translations[para.id]), composer.font, fontsize,
                                           (x1 - x0) * scale - 16, composer.math_font)
                        required = (max(unit.y1, para.bottom) - current) * scale + len(lines) * composer.line_h + 23
                    elif region.width == page.rect.width and region.height < 40:
                        required += 65  # 跨栏标题下方留出正文起始行的位置。
                    if composer.bottom - composer.y < required <= composer.bottom - composer.top:
                        composer.new_page()
                    end = min(bottom, para.bottom + 1)
                    left, right = composer.original(pno, current, max(current, end), (x0, x1), obstacles)
                    composer.translation(clean_text_controls(translations[para.id]), left, right, para.formulas, pno)
                    current = max(current, end)
                composer.original(pno, current, bottom, (x0, x1), obstacles)
                composer.y += 6
            composer.y += 12
        title = source.metadata.get("title") or src_path.stem
        # 最后绘制页眉页脚，避免扫描背景与原文白色图形遮住它们。
        header_font = fitz.Font("helv").buffer
        for number, page in enumerate(output, 1):
            # 独立嵌入名称，避免与原文裁片中的 helv 字体资源冲突。
            page.insert_font(fontname="PFHeader", fontbuffer=header_font)
            page.draw_rect(fitz.Rect(0, 0, composer.width, 32), color=None, fill=(1, 1, 1))
            page.draw_rect(fitz.Rect(0, composer.height - 30, composer.width, composer.height), color=None, fill=(1, 1, 1))
            page.insert_text((composer.margin, 24), "PaperFlow  /  EN + ZH", fontname="PFHeader", fontsize=8, color=(.45, .49, .5))
            page.insert_text((composer.width - 58, composer.height - 20), str(number), fontname="PFHeader", fontsize=8, color=(.45, .49, .5))
        output.set_metadata({"title": f"{title} | 中英逐段对照", "creator": "PaperFlow Local"})
        # MuPDF 1.25 的原生 CFF 子集会损坏部分 OTF 汉字，改用其 FontTools 回退。
        # TrueType 字体仍走原生压缩；备用数学字体只在实际使用时嵌入。
        output.subset_fonts(fallback=Path(font_path).suffix.lower() == ".otf")
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        output.save(dst_path, garbage=4, deflate=True)
