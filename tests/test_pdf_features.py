"""公式、分栏和本机 OCR 的行为回归；无需联网或调用翻译模型。"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from paperflow.formulas import check_formula_tokens
from paperflow.layout import collect_paragraphs, reading_regions, render_inline_pdf
from paperflow.pdf_input import ocr_available, prepare_pdf
from paperflow.translate import Cancelled, validate_response


def feature_pdf():
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((70, 45), "Formula and two column layout validation", fontsize=18)
        for x, label in [(40, "LEFT FIRST"), (320, "RIGHT SECOND")]:
            text = label + ": Observations of optical tracks reduce uncertainty. "
            text += "The proposed method links observations and estimates the orbital parameters. " * 2
            page.insert_textbox(fitz.Rect(x, 80, x + 240, 210), text, fontsize=11)
        # 不同字形与真实上下标；红色便于检查裁片是否保留。
        x = 40
        for text, font, size, offset in [("The energy relation ", "helv", 10, 0), ("E", "tiit", 12, 0),
                (" = ", "helv", 12, 0), ("mc", "tiit", 12, 0), ("2", "helv", 8, -5),
                (" is preserved exactly.", "helv", 10, 0)]:
            page.insert_text((x, 235 + offset), text, fontname=font, fontsize=size,
                             color=(.85, .05, .05) if size != 10 else (0, 0, 0))
            x += fitz.get_text_length(text, fontname=font, fontsize=size)
        page.insert_text((40, 274), "The ratio", fontsize=10)
        page.insert_text((86, 274), "x =", fontname="tiit", fontsize=12)
        page.insert_text((109, 268), "1", fontsize=8, color=(.85, .05, .05))
        page.draw_line((107, 271), (116, 271), color=(.85, .05, .05), width=.8)
        page.insert_text((109, 281), "2", fontsize=8, color=(.85, .05, .05))
        page.insert_text((122, 274), "retains numerator and denominator.", fontsize=10)
        page.draw_rect(fitz.Rect(55, 320, 545, 410), color=(.05, .6, .2), fill=(.91, .98, .92))
        page.insert_text((175, 367), "CROSS COLUMN FIGURE", fontsize=16)
        page.insert_text((55, 430), "Fig. 1 A wide figure shared by the left and right columns", fontsize=10)
        for x, label in [(40, "LOWER LEFT"), (320, "LOWER RIGHT")]:
            page.insert_textbox(fitz.Rect(x, 465, x + 240, 615), label + ": Additional observations improve "
                "the estimated parameters. The complete image remains visible after translation. " * 2, fontsize=11)
        return doc.tobytes()


def scan_pdf(native):
    with fitz.open(stream=native, filetype="pdf") as source, fitz.open() as doc:
        for original in source:
            page = doc.new_page(width=original.rect.width, height=original.rect.height)
            page.insert_image(page.rect, stream=original.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png"))
        return doc.tobytes()


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "native.pdf"
        self.source.write_bytes(feature_pdf())

    def tearDown(self):
        self.temp.cleanup()

    def test_columns_spanning_figure_and_formula_protection(self):
        with fitz.open(self.source) as doc:
            regions = reading_regions(doc[0])
            paragraphs = collect_paragraphs(doc)
            text = " ".join(p.text for p in paragraphs)
            self.assertLess(text.index("LEFT FIRST"), text.index("RIGHT SECOND"))
            self.assertLess(text.index("LOWER LEFT"), text.index("LOWER RIGHT"))
            self.assertIn("Formula and two column", text)
            figure = fitz.Rect(55, 320, 545, 410)
            self.assertEqual(sum(r.contains(figure) for r in regions), 1)
            self.assertNotIn("CROSS COLUMN FIGURE", text)
            self.assertEqual(len(reading_regions(doc[0], "single")), 1)
        energy = next(p for p in paragraphs if "energy" in p.text)
        ratio = next(p for p in paragraphs if "ratio" in p.text)
        self.assertEqual(len(energy.formulas), 1, energy)
        self.assertIn("mc", energy.formulas[0]["text"])
        self.assertEqual(len(ratio.formulas), 1, ratio)
        self.assertIn("1", ratio.formulas[0]["text"])
        self.assertIn("2", ratio.formulas[0]["text"])
        self.assertGreater(ratio.formulas[0]["bbox"][3], 280)
        translations = {p.id: "【排版验证】保留公式 " + " 与 ".join(f["token"] for f in p.formulas) + "。" for p in paragraphs}
        output = self.root / "result.pdf"
        render_inline_pdf(self.source, output, paragraphs, translations)
        with fitz.open(output) as doc:
            self.assertNotIn("[[PFM_", "".join(p.get_text() for p in doc))
            self.assertTrue(any("排版验证" in p.get_text() for p in doc))
            # 灰底译文区域必须出现红色原始公式笔画，而非 Unicode 猜测重建。
            import numpy as np
            reds = 0
            for page in doc:
                for drawing in page.get_drawings():
                    if drawing.get("fill") and .93 < drawing["fill"][0] < .95:
                        pix = page.get_pixmap(clip=drawing["rect"], matrix=fitz.Matrix(2, 2), alpha=False)
                        pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(-1, 3)
                        reds += int(((pixels[:, 0] > 150) & (pixels[:, 1] < 90) & (pixels[:, 2] < 90)).sum())
            self.assertGreater(reds, 10)

    def test_formula_tokens_checked_before_accepting_model_output_and_render(self):
        with fitz.open(self.source) as doc:
            para = next(p for p in collect_paragraphs(doc) if p.formulas)
        token = para.formulas[0]["token"]
        check_formula_tokens("公式 " + token, para)
        for text in ["无公式", token * 2, token.replace("PFM_", "PFM_changed_"), token + "[[PFM_bad]]", token + "[[PFM broken]]"]:
            with self.assertRaisesRegex(ValueError, "公式占位符"):
                validate_response(json.dumps({"translations": [{"id": para.id, "zh": text}]}), [para])
            with self.assertRaisesRegex(ValueError, "公式占位符"):
                render_inline_pdf(self.source, self.root / "invalid.pdf", [para], {para.id: text})
        with fitz.open(self.source) as doc:
            self.assertFalse(any(p.formulas for p in collect_paragraphs(doc, protect_formulas=False)))

    @unittest.skipUnless(ocr_available(), "当前环境没有已安装的 RapidOCR 本地模型")
    def test_real_ocr_auto_native_skip_scan_off_and_cancel(self):
        # 真正调用本机 OCR 模型；只隔离版面模型，以覆盖没有额外权重时的几何回退。
        with patch("paperflow.pdf_input.detect_layout", return_value=[]):
            native, report = prepare_pdf(self.source, self.root / "unused.pdf", [0])
            self.assertEqual(native, self.source)
            self.assertEqual(report["ocr_pages"], [])
            scanned = self.root / "scan.pdf"
            original_bytes = scan_pdf(self.source.read_bytes())
            scanned.write_bytes(original_bytes)
            with self.assertRaisesRegex(ValueError, "开启 OCR"):
                prepare_pdf(scanned, self.root / "off.pdf", [0], "off")
            event = threading.Event(); event.set()
            with self.assertRaises(Cancelled):
                prepare_pdf(scanned, self.root / "cancel.pdf", [0], cancel=event)
            prepared, report = prepare_pdf(scanned, self.root / "prepared.pdf", [0])
        self.assertEqual(scanned.read_bytes(), original_bytes)
        self.assertEqual(report["ocr_pages"], [1])
        self.assertGreater(len(report["pages"]["0"]["ocr_words"]), 20)
        with fitz.open(prepared) as doc:
            self.assertIn("observations", doc[0].get_text().lower())
            paragraphs = collect_paragraphs(doc, analysis=report)
        self.assertEqual(report["pages"]["0"]["columns"], 2)
        self.assertGreater(len(paragraphs), 4)

    def test_ocr_low_confidence_and_figure_exclusion(self):
        import numpy as np
        from paperflow.pdf_input import recognize_page
        results = [
            [[[100, 100], [400, 100], [400, 140], [100, 140]], "The observed optical tracks", .6],
            [[[100, 400], [400, 400], [400, 440], [100, 440]], "Figure label", .99],
        ]
        with fitz.open(self.source) as doc, patch("paperflow.pdf_input.ocr_engine", return_value=lambda *a, **kw: (results, [])):
            _, lines, _, uncertain = recognize_page(doc[0], [{"type": "figure", "bbox": [0, 120, 600, 160]}])
        self.assertEqual(len(lines), 1)
        self.assertEqual(uncertain, 1)


if __name__ == "__main__":
    unittest.main()
