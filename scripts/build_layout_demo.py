"""使用固定样例与预置中文验证三项解析能力，不请求任何翻译服务。"""
import json
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_pdf_features import feature_pdf, scan_pdf
from paperflow.layout import collect_paragraphs, render_inline_pdf
from paperflow.pdf_input import prepare_pdf


def main():
    temporary = ROOT / "tmp" / "layout-qa"
    temporary.mkdir(parents=True, exist_ok=True)
    source = temporary / "demo-source.pdf"
    with fitz.open() as doc:
        for data in (feature_pdf(), scan_pdf(feature_pdf())):
            with fitz.open(stream=data, filetype="pdf") as part:
                doc.insert_pdf(part)
        doc.set_metadata({"title": "原生双栏与扫描件 · 功能验证样例（预置中文）"})
        doc.save(source)
    work, report = prepare_pdf(source, temporary / "demo-prepared.pdf", [0, 1])
    with fitz.open(work) as doc:
        paragraphs = collect_paragraphs(doc, analysis=report)
    translations = {}
    for p in paragraphs:
        text = p.text.lower().replace(" ", "")
        prefix = "【原生 PDF】" if p.page == 0 else "【扫描 PDF · 本地 OCR】"
        formulas = " 与 ".join(item["token"] for item in p.formulas)
        if "energy" in text:
            chinese = f"能量关系 {formulas} 保留原始字形，上标从原文裁片还原。"
        elif "ratio" in text:
            chinese = f"分数 {formulas} 保留原始分子、分数线和分母；扫描件请对照原图核查。"
        elif text.startswith("fig"):
            chinese = "图 1：跨栏图形及其图题整体保留。"
        elif "layoutvalidation" in text:
            chinese = "公式、双栏与 OCR 功能验证。以下中文为预置样例，不是本次模型翻译结果。"
        elif "lowerleft" in text:
            chinese = "下半页左栏。补充观测改善参数估计，翻译后仍保留完整原图。"
        elif "lowerright" in text:
            chinese = "下半页右栏。按先左后右的顺序阅读，图表与正文不会交错。"
        elif "leftfirst" in text:
            chinese = "上半页左栏。光学轨迹观测降低不确定性，该方法关联观测并估计轨道参数。"
        elif "rightsecond" in text:
            chinese = "上半页右栏。关联光学观测并估计轨道参数；译文紧随本栏对应英文。"
        else:
            chinese = "排版验证段落。" + formulas
        translations[p.id] = prefix + chinese
    output = ROOT / "output" / "pdf" / "layout-capabilities-demo.pdf"
    render_inline_pdf(work, output, paragraphs, translations, analysis=report)
    report["paragraphs"] = [vars(p) for p in paragraphs]
    (temporary / "demo-analysis.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with fitz.open(output) as doc:
        for i, page in enumerate(doc):
            page.get_pixmap(matrix=fitz.Matrix(1.4, 1.4)).save(temporary / f"demo-page-{i + 1}.png")
        print(json.dumps({"output": str(output), "pages": len(doc), "ocr_pages": report["ocr_pages"],
                          "formulas": sum(len(p.formulas) for p in paragraphs), "warnings": report["warnings"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
