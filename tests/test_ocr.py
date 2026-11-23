"""pdf-ar OCR engine, the engine switch and --ocr-check."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("pymupdf")
pytest.importorskip("fontTools")

from pdf_ar.extract import Options, run  # noqa: E402
from pdf_ar.ocr import OcrOptions, agreement, assemble  # noqa: E402
from pdf_ar.qc import check_ocr  # noqa: E402
from pdf_fonts import fonts_folder, system_has  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "arabic_ligatures.pdf"
EXPECTED = (ROOT / "tests" / "fixtures" / "arabic_ligatures.txt").read_text(encoding="utf-8")


def _tesseract() -> OcrOptions | None:
    command = shutil.which("tesseract") or "C:/Program Files/Tesseract-OCR/tesseract.exe"
    if not Path(command).is_file():
        return None
    candidates = [os.environ.get("PDF_EXTRACT_TESSDATA"),
                  str(Path.home() / "AppData" / "Local" / "pdftext-tools" / "tessdata" / "fast")]
    for folder in filter(None, candidates):
        if (Path(folder) / "ara.traineddata").is_file():
            return OcrOptions(tesseract_cmd=command, tessdata_dir=folder.replace("\\", "/"))
    return None


TESSERACT = _tesseract()
needs_tesseract = pytest.mark.skipif(
    TESSERACT is None or pytest.importorskip("pytesseract") is None,
    reason="Tesseract with the Arabic model is not installed")
needs_arial = pytest.mark.skipif(not system_has("ArialMT"),
                                 reason="the fixture's font, Arial, is missing")


def test_assemble_builds_paragraphs_lines_and_confidence():
    data = {"page_num": [1, 1, 1, 1, 1], "block_num": [1, 1, 1, 1, 2],
            "par_num": [1, 1, 1, 1, 1], "line_num": [1, 1, 2, 2, 1],
            "text": ["نص", "أول", "", "تكملة", "فقرة"], "conf": [90, 80, -1, 70, 100]}
    page = assemble(data)
    assert page.lines == "نص أول\nتكملة\n\nفقرة"
    assert page.paragraphs == "نص أول تكملة\n\nفقرة"
    assert page.confidence == pytest.approx(0.85) and page.words == 4


def test_agreement_counts_shared_words():
    assert agreement("نظام إدارة الجودة", "نظام إدارة الجوده") == pytest.approx(2 / 3, abs=1e-4)
    assert agreement("", "نص") is None


def test_ocr_gate():
    assert check_ocr(1, "", None, glyphs=10, has_images=False, min_confidence=0.75).reasons == [
        "ocr_empty"]
    assert check_ocr(1, "نص", 0.5, glyphs=10, has_images=False,
                     min_confidence=0.75).reasons == ["low_ocr_confidence"]
    assert check_ocr(1, "نص", 0.9, glyphs=10, has_images=False, min_confidence=0.75).status == "ok"


def test_output_folders_are_safe_on_windows(tmp_path):
    from pdf_ar.extract import output_folder

    folder = output_folder(tmp_path, Path("Sector ") / "Policy in Field Surveys .pdf")
    assert folder == tmp_path / "Sector" / "Policy in Field Surveys"


def test_unknown_engine_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        Options(out_dir=tmp_path, fonts_dir=tmp_path, engine="llm")


@needs_tesseract
def test_ocr_engine_needs_no_fonts(tmp_path):
    result = run(FIXTURE, Options(out_dir=tmp_path / "out", fonts_dir=tmp_path / "fonts",
                                  engine="ocr", ocr=TESSERACT))
    folder = tmp_path / "out" / "arabic_ligatures"
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert report["engine"] == "ocr" and report["fonts"] == {}
    assert not (tmp_path / "fonts").exists()
    text = (folder / "pages" / "page-001.txt").read_text(encoding="utf-8")
    assert agreement(EXPECTED, text) >= 0.6
    assert result.pages == 1


@needs_tesseract
@needs_arial
def test_ocr_check_writes_ocr_text_next_to_the_rebuild(tmp_path):
    fonts = fonts_folder(tmp_path / "fonts", "ArialMT")
    run(FIXTURE, Options(out_dir=tmp_path / "out", fonts_dir=fonts, engine="fonts",
                         ocr_check=True, ocr=TESSERACT))
    folder = tmp_path / "out" / "arabic_ligatures"
    lines = (folder / "pages" / "page-001.lines.txt").read_text(encoding="utf-8")
    assert [line for line in lines.splitlines() if line] == EXPECTED.splitlines()
    assert (folder / "pages" / "page-001.ocr.txt").read_text(encoding="utf-8").strip()
    assert (folder / "arabic_ligatures.ocr.txt").is_file()
    page = json.loads((folder / "report.json").read_text(encoding="utf-8"))["pages"][0]
    assert page["engine"] == "fonts" and page["ocr_agreement"] is not None
    assert page["ocr_confidence"] is not None
