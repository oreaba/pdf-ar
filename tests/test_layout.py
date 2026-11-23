"""Ordering, spacing, paragraph and table rules of pdf-ar, on synthetic glyphs."""

from __future__ import annotations

import pytest

pytest.importorskip("pymupdf")
pytest.importorskip("fontTools")

from pdf_ar import bidi  # noqa: E402
from pdf_ar.layout import _continues, _table_cells, drop_spurious_spaces  # noqa: E402
from pdf_ar.pdf_adapter import Drawing  # noqa: E402


def word(text: str) -> list[dict]:
    return [{"text": char} for char in text]


def test_rtl_line_keeps_a_latin_run_in_left_to_right_order():
    # Right-to-left visual order: the Arabic word, then "2015" and "ISO" reversed.
    words = [word("مرحبا"), word("5102"), word("OSI")]
    assert bidi.rtl_logical(words) == "مرحبا ISO 2015"


def test_rtl_list_number_keeps_its_dot():
    assert bidi.rtl_logical([word("1."), word("يفضل")]) == "1. يفضل"


def test_wrapped_closing_bracket_line_reads_right_to_left():
    assert bidi.rtl_logical([word("1)")]) == "1)"


def test_ltr_line_reverses_arabic_glyphs_and_word_order():
    # Left-to-right visual order of "نظام إدارة" followed by a Latin word.
    words = [word("ةرادإ"), word("ماظن"), word("ISO")]
    assert bidi.ltr_logical(words) == "نظام إدارة ISO"


def test_brackets_are_unmirrored_only_when_they_close_first():
    assert bidi.unmirror(")حسب الاقتضاء(") == "(حسب الاقتضاء)"
    assert bidi.unmirror("(راجع النموذج 1)") == "(راجع النموذج 1)"


def test_list_number_does_not_trigger_bracket_swap():
    text = "1) في حال كان الطلب\n\nعلى غرار (ومن دون أن يقتصر على) ما يلي"
    assert bidi.unmirror(text) == text


def test_bracket_gate_reads_the_whole_page():
    from collections import Counter

    from pdf_ar.layout import PageText
    from pdf_ar.qc import check

    def reasons(text):
        return check(1, PageText(text, text, Counter({"font": 5}), 5), False).reasons

    assert reasons("نص (مثل\n\nالسكن والنقل) نهاية") == []
    assert reasons("1) بند أول\n\n2) بند ثان") == []
    assert reasons("نص )مثل( نهاية") == ["unbalanced_brackets"]


def test_strong_direction():
    assert bidi.strong("ISO مرحبا") == "rtl"
    assert bidi.strong("1)") is None


def glyph(text, x0, x1, joins=False, size=10.0):
    return {"text": text, "x0": x0, "x1": x1, "joins": joins, "size": size,
            "space": text.isspace(), "joiner": text == ""}


def test_space_after_a_joining_form_is_dropped():
    ordered = [glyph("ب", 20, 25, joins=True), glyph(" ", 15, 18), glyph("ت", 10, 15)]
    assert [g["text"] for g in drop_spurious_spaces(ordered, rtl=True)] == ["ب", "ت"]


def test_space_without_room_between_advance_boxes_is_dropped():
    ordered = [glyph("ر", 10, 15), glyph(" ", 10, 12), glyph("ا", 5, 9.9)]
    assert [g["text"] for g in drop_spurious_spaces(ordered, rtl=True)] == ["ر", "ا"]


def test_space_before_a_word_final_letter_is_dropped():
    ordered = [glyph("ا", 288.0, 290.1), glyph(" ", 285.9, 288.4), glyph("ء", 282.3, 286.4)]
    assert [g["text"] for g in drop_spurious_spaces(ordered, rtl=True)] == ["ا", "ء"]


def test_arabic_and_latin_letters_never_share_a_word():
    from pdf_ar.layout import _script_change

    assert _script_change({"text": "م"}, {"text": "n"})
    assert not _script_change({"text": "م"}, {"text": "2"})


def test_real_space_is_kept():
    ordered = [glyph("ر", 10, 15), glyph(" ", 7, 10), glyph("ا", 3, 7)]
    assert len(drop_spurious_spaces(ordered, rtl=True)) == 3


def test_space_next_to_latin_is_kept():
    ordered = [glyph("ر", 10, 15), glyph(" ", 10, 12), glyph("A", 5, 9.9)]
    assert len(drop_spurious_spaces(ordered, rtl=True)) == 3


def line(text, y, x0=100.0, x1=500.0, container=("text", 0), size=11.0, first_width=20.0,
         block_width=400.0, font="Calibri", rtl=True):
    return {"text": text, "y": y, "x0": x0, "x1": x1, "container": container, "size": size,
            "first_width": first_width, "block_width": block_width, "font": font, "rtl": rtl,
            "dir": (1.0, 0.0)}


def test_full_line_continues_into_next_line():
    assert _continues(line("نص طويل", 100), line("تكملة", 113, x1=300), rules=[])


def test_short_line_ends_its_paragraph():
    assert not _continues(line("نص", 100, x0=400), line("فقرة جديدة", 113), rules=[])


def test_list_item_and_colon_start_new_paragraphs():
    assert not _continues(line("نص طويل", 100), line("2. بند", 113), rules=[])
    assert not _continues(line("الغرض:", 100), line("نص", 113), rules=[])


def test_ruled_line_between_lines_separates_them():
    assert not _continues(line("نص طويل", 100), line("تكملة", 113), rules=[(103.0, 0.0, 600.0)])


def test_lines_in_one_box_join_and_boxes_never_join_each_other():
    box = ("shape", 1)
    assert _continues(line("مراجعة", 100, container=box), line("الموضوعات", 112, container=box),
                      rules=[])
    assert not _continues(line("مراجعة", 100, container=box),
                          line("الموضوعات", 112, container=("shape", 2)), rules=[])


def test_mark_glyph_with_two_marks_is_kept():
    from pdf_ar.shaping import ShapingModel

    model = object.__new__(ShapingModel)
    model.text = {"beh": "\u0628", "shadda_fatha": "\u0651\u064e"}
    model.dotted, model.signature, model.connectors = {}, {}, set()
    assert model.letter("beh", ["shadda_fatha"]) == "\u0628\u0651\u064e"


def test_shadda_is_not_taken_for_a_dot():
    from pdf_ar.shaping import ShapingModel

    model = object.__new__(ShapingModel)
    model.text = {"yeh.skeleton": "\u064a", "shadda": "\u0651", "two_dots": "",
                  "small_v": ""}
    model.dotted = {"yeh.skeleton": {"two_dots": "\u064a", "small_v": "\u0756"}}
    model.signature = {"shadda": (1, "above"), "small_v": (1, "above"), "two_dots": (2, "below")}
    model.connectors = set()
    assert model.letter("yeh.skeleton", ["shadda", "two_dots"]) == "\u064a\u0651"
    assert model.letter("yeh.skeleton", ["shadda"]) == "\u064a\u0651"


def test_verified_space_wins_over_a_wrong_pdf_map():
    from collections import Counter

    from pdf_ar.glyphs import glyph_text
    from pdf_ar.shaping import ShapingModel

    model = object.__new__(ShapingModel)
    model.marks, model.connectors, model.ligature_parts = set(), set(), {}
    model.text, model.dotted, model.signature = {"space": " "}, {}, {}
    item = {"model": model, "name": "space", "pdf": "\u0625", "gid": 3, "has_file": True,
            "marks": {}}
    assert glyph_text(item, Counter()) == " "


def test_connecting_stroke_without_pdf_text_is_a_joiner():
    from collections import Counter

    from pdf_ar.glyphs import glyph_text
    from pdf_ar.shaping import ShapingModel

    model = object.__new__(ShapingModel)
    model.marks, model.connectors = set(), {"stroke"}
    counts = Counter()
    item = {"model": model, "name": "stroke", "pdf": " ", "gid": 5, "has_file": True,
            "marks": {}}
    assert glyph_text(item, counts) == "" and counts["kashida"] == 1


def test_superscript_ordinal_joins_its_number():
    from pdf_ar.layout import _lines

    def g(text, x0, x1, y, size, span, seq):
        return {"text": text, "x0": x0, "x1": x1, "y": y, "size": size, "span": span,
                "seq": seq, "space": text.isspace(), "joiner": False, "joins": False}

    glyphs = [g("b", 0, 5, 100, 11, 0, 0), g("y", 5, 10, 100, 11, 0, 1),
              g(" ", 10, 13, 100, 11, 0, 2), g("2", 13, 19, 100, 11, 0, 3),
              g("1", 19, 25, 100, 11, 0, 4), g("s", 25, 28, 96, 7, 1, 5),
              g("t", 28, 31, 96, 7, 1, 6), g(" ", 31, 34, 100, 11, 2, 7),
              g("o", 34, 40, 100, 11, 2, 8), g("f", 40, 44, 100, 11, 2, 9)]
    lines = _lines(glyphs)
    assert len(lines) == 1
    assert ["".join(c["text"] for c in w) for w in lines[0][1]] == ["by", "21st", "of"]


def test_codes_and_numbers_are_not_words_for_row_direction():
    from pdf_ar.layout import _arabic_word, _is_word

    assert _is_word(word("ISO")) and _is_word(word("نظام"))
    assert not _is_word(word("R/I")) and not _is_word(word("1A")) and not _is_word(word("2026"))
    assert _arabic_word(word("نظام")) and not _arabic_word(word("ISO"))


def test_span_wholly_outside_the_page_is_off_page():
    from types import SimpleNamespace

    from pdf_ar.glyphs import _off_page

    page = SimpleNamespace(width=612.0, height=792.0)
    assert _off_page({"bbox": (754, 720, 845, 730)}, page)
    assert not _off_page({"bbox": (600, 720, 650, 730)}, page)


def test_table_cells_from_thin_filled_rectangles():
    def bar(x0, y0, x1, y1):
        return Drawing(rect=(x0, y0, x1, y1), closed=False, filled=True,
                       items=(("re", (x0, y0, x1, y1)),))

    drawings = [bar(0, 0, 200, 0.5), bar(0, 50, 200, 50.5), bar(0, 100, 200, 100.5),
                bar(0, 0, 0.5, 100), bar(100, 0, 100.5, 100), bar(200, 0, 200.5, 100)]
    cells = {tuple(round(v) for v in c) for c in _table_cells(drawings)}
    assert {(0, 0, 100, 50), (100, 0, 200, 50), (0, 50, 100, 100), (100, 50, 200, 100)} <= cells
