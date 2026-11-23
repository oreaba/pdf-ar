"""Fonts folder and required fonts: the folder is only read; needs are checked per version."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("pymupdf")
pytest.importorskip("fontTools")

from fontTools.fontBuilder import FontBuilder  # noqa: E402
from fontTools.pens.ttGlyphPen import TTGlyphPen  # noqa: E402
from fontTools.ttLib import TTFont  # noqa: E402

from pdf_ar.fonts import (  # noqa: E402
    FontLibrary,
    font_key,
    font_version,
    open_font_registry,
    same_version,
)
from pdf_ar.glyphs import unnamed  # noqa: E402
from pdf_ar.required_fonts import Requirement, requirements, status_of, table  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "arabic_ligatures.pdf"


def make_font(path, name="TestFont-Regular", family="TestFont", version="Version 1.00"):
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "A"])
    builder.setupCharacterMap({0x41: "A"})
    pen = TTGlyphPen(None)
    pen.moveTo((0, 0))
    pen.lineTo((0, 500))
    pen.lineTo((500, 0))
    pen.closePath()
    builder.setupGlyf({".notdef": TTGlyphPen(None).glyph(), "A": pen.glyph()})
    builder.setupHorizontalMetrics({".notdef": (500, 0), "A": (500, 0)})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": family, "styleName": "Regular", "psName": name,
                            "version": version})
    builder.setupOS2()
    builder.setupPost()
    builder.save(str(path))


def test_font_key_removes_subset_prefix_and_spaces():
    assert font_key("BCDEEE+Sakkal Majalla") == "SakkalMajalla"
    assert font_key("Calibri-Bold") == "Calibri-Bold"


def test_registry_entries_are_pinned_hints():
    registry = open_font_registry()
    assert {"Almarai-Regular", "NotoKufiArabic-Regular"} <= set(registry)
    for versions in registry.values():
        for entry in versions:
            assert entry["url"].startswith("https://")
            assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            assert entry["license"] == "OFL-1.1"
            assert ("instance" not in entry) or ("member" in entry and "file" in entry)


def test_versions_are_compared_as_numbers(tmp_path):
    make_font(tmp_path / "a.ttf", version="Version 6.85;O365")
    assert font_version(TTFont(str(tmp_path / "a.ttf"))) == "6.85"
    assert same_version("1.1", "1.100") and same_version("7.00", "7.0")
    assert not same_version("6.85", "6.86") and not same_version(None, "1.0")


def test_every_version_of_a_font_is_a_candidate(tmp_path):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    make_font(fonts / "a_new.ttf", version="Version 2.00")
    make_font(fonts / "b_old.ttf", version="Version 1.00")
    library = FontLibrary(fonts)
    assert [Path(f.path).name for f in library.candidates("ABCDEF+TestFont-Regular")] == [
        "a_new.ttf", "b_old.ttf"]
    assert [d["version"] for d in library.describe()] == ["2.00", "1.00"]


def test_simulated_style_uses_the_plain_font_file(tmp_path):
    make_font(tmp_path / "testfont.ttf")
    library = FontLibrary(tmp_path)
    assert [Path(f.path).name for f in library.candidates("TestFont-Regular,Bold")] == [
        "testfont.ttf"]


def test_the_fonts_folder_is_never_created_or_filled(tmp_path):
    library = FontLibrary(tmp_path / "fonts")
    assert library.files() == [] and library.candidates("ArialMT") == []
    assert requirements([FIXTURE], library)[0].status == "missing"
    assert not (tmp_path / "fonts").exists()


def test_a_different_font_under_the_needed_name_is_the_wrong_version(tmp_path):
    make_font(tmp_path / "fake-arial.ttf", name="ArialMT", family="Arial", version="Version 9.99")
    [row] = requirements([FIXTURE], FontLibrary(tmp_path))
    assert (row.font, row.status, row.verified) == ("ArialMT", "wrong_version", 0)
    assert row.candidates == {"fake-arial.ttf"}
    assert "fake-arial.ttf (9.99)" in row.hint and f"add version {row.version}" in row.hint
    assert "wrong_version" in table([row]) and "To add to the fonts folder" in table([row])


def test_status_rules():
    row = Requirement("F", "1.1", glyphs=100, verified=100, candidates={"f.ttf"})
    assert status_of(row, same_version_in_folder=False) == "ok"
    row.verified = 92
    assert status_of(row, same_version_in_folder=True) == "partial"
    assert status_of(row, same_version_in_folder=False) == "wrong_version"
    row.verified = 50
    assert status_of(row, same_version_in_folder=True) == "wrong_version"
    assert status_of(Requirement("F", "1.1", glyphs=5), False) == "missing"
    assert status_of(Requirement("F", "1.1", glyphs=5, unreadable=5), False) == "unreadable"
    assert not Requirement("F", None, status="partial").blocking
    assert Requirement("F", None, status="missing").blocking


def test_unnamed_fonts():
    assert unnamed("9") and unnamed("10,Bold")
    assert not unnamed("SakkalMajalla,Bold") and not unnamed("Arial-BoldMT")
