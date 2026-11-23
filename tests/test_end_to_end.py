"""pdf-ar end to end: the synthetic Word fixture and missing fonts."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

pytest.importorskip("pymupdf")
pytest.importorskip("fontTools")

from pdf_ar.cli import EXIT_MISSING_FONTS, main  # noqa: E402
from pdf_ar.extract import MissingFontsError, Options, run  # noqa: E402
from pdf_fonts import fonts_folder, system_has  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "arabic_ligatures.pdf"
EXPECTED = (ROOT / "tests" / "fixtures" / "arabic_ligatures.txt").read_text(encoding="utf-8")


@pytest.mark.skipif(not system_has("ArialMT"), reason="the fixture's font, Arial, is missing")
def test_word_fixture_rebuilds_to_its_source_sentences(tmp_path):
    fonts = fonts_folder(tmp_path / "fonts", "ArialMT")
    result = run(FIXTURE, Options(out_dir=tmp_path / "out", fonts_dir=fonts))
    page = (tmp_path / "out" / "arabic_ligatures" / "pages" / "page-001.lines.txt")
    lines = [line for line in page.read_text(encoding="utf-8").splitlines() if line]
    assert lines == EXPECTED.splitlines()
    report = json.loads((tmp_path / "out" / "arabic_ligatures" / "report.json")
                        .read_text(encoding="utf-8"))
    assert report["status"] == "ok" and result.review_pages == 0
    assert report["pages"][0]["sources"].get("font", 0) > 0
    assert report["fonts"]["ArialMT"] == ["arial.ttf"]
    assert [r.status for r in result.fonts] == ["ok"]
    run_record = json.loads((tmp_path / "out" / "run.json").read_text(encoding="utf-8"))
    assert [f["file"] for f in run_record["fonts_folder"]] == ["arial.ttf"]


@pytest.mark.parametrize("engine", ["fonts", "auto"])
def test_missing_fonts_stop_the_run_before_anything_is_extracted(tmp_path, engine):
    options = Options(out_dir=tmp_path / "out", fonts_dir=tmp_path / "fonts", engine=engine)
    with pytest.raises(MissingFontsError) as error:
        run(FIXTURE, options)
    [row] = error.value.rows
    assert (row.font, row.status, row.documents) == ("ArialMT", "missing", {"arabic_ligatures.pdf"})
    assert row.version and row.glyphs > 0 and row.pages == 1
    with (tmp_path / "out" / "fonts_report.csv").open(encoding="utf-8") as handle:
        [listed] = list(csv.DictReader(handle))
    assert (listed["font"], listed["status"], listed["documents"]) == ("ArialMT", "missing", "1")
    assert not (tmp_path / "out" / "arabic_ligatures").exists()
    assert not (tmp_path / "fonts").exists()  # nothing is ever copied or downloaded into it
    assert main(["extract", str(FIXTURE), "--engine", engine, "--out", str(tmp_path / "cli"),
                 "--fonts-dir", str(tmp_path / "fonts")]) == EXIT_MISSING_FONTS
    assert main(["fonts", str(FIXTURE), "--out", str(tmp_path / "report"),
                 "--fonts-dir", str(tmp_path / "fonts")]) == EXIT_MISSING_FONTS


def test_native_engine_needs_no_fonts(tmp_path):
    result = run(FIXTURE, Options(out_dir=tmp_path / "out", fonts_dir=tmp_path / "fonts",
                                  engine="native"))
    assert result.pages == 1 and result.fonts == []
    with pytest.raises(MissingFontsError):
        run(FIXTURE, Options(out_dir=tmp_path / "checked", fonts_dir=tmp_path / "fonts",
                             engine="native", font_check=True))
