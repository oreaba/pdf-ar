"""pdf-ar output formats: Markdown tables, JSON, OCR model selection."""

from __future__ import annotations

import pytest

pytest.importorskip("pymupdf")
pytest.importorskip("fontTools")

from pdf_ar.formats import PageOutput, blocks_from_text, to_json, to_markdown  # noqa: E402
from pdf_ar.layout import Block  # noqa: E402
from pdf_ar.ocr import OcrOptions  # noqa: E402


def cell(text, x0, y0, x1, y1, rtl=True):
    return Block("cell", text, [text], (x0, y0, x1, y1), rtl)


def test_arabic_table_reads_right_to_left_with_its_header():
    blocks = [Block("paragraph", "السجلات:", ["السجلات:"], (0, 0, 0, 0), True),
              cell("الرقم", 200, 0, 300, 20), cell("اسم السجل", 100, 0, 200, 20),
              cell("1", 200, 20, 300, 40), cell("محضر | الاجتماع", 100, 20, 200, 40)]
    md = to_markdown([PageOutput(1, "ok", [], "", blocks)])
    assert md == ("<!-- page 1 -->\n\nالسجلات:\n\n| الرقم | اسم السجل |\n| --- | --- |\n"
                  "| 1 | محضر \\| الاجتماع |\n")


def test_merged_cell_fills_its_first_column_and_lone_cell_is_a_paragraph():
    blocks = [cell("العنوان", 0, 0, 200, 20), cell("أ", 100, 20, 200, 40),
              cell("ب", 0, 20, 100, 40), cell("خانة منفردة", 0, 300, 100, 320)]
    md = to_markdown([PageOutput(2, "ok", [], "", blocks)])
    assert "|  | العنوان |\n| --- | --- |\n| أ | ب |" in md
    assert md.rstrip().endswith("خانة منفردة")


def test_json_keeps_pages_and_blocks():
    page = PageOutput(1, "review", ["odd_letters"], "نص", blocks_from_text("نص\n\nفقرة"))
    data = to_json({"source": "a.pdf"}, [page])
    assert data["source"] == "a.pdf"
    assert [b["text"] for b in data["pages"][0]["blocks"]] == ["نص", "فقرة"]
    assert data["pages"][0]["reasons"] == ["odd_letters"]


def test_ocr_model_selects_its_folder(tmp_path, monkeypatch):
    (tmp_path / "best").mkdir()
    monkeypatch.setenv("PDF_EXTRACT_TESSDATA", str(tmp_path))
    assert OcrOptions(model="best").model_dir() == (tmp_path / "best").as_posix()
    assert OcrOptions(model="fast").model_dir() is None
    assert OcrOptions(model="best", tessdata_dir="C:/x").model_dir() == "C:/x"
    with pytest.raises(ValueError):
        OcrOptions(model="medium")
