from __future__ import annotations

from pdf_ar.qc import NATIVE_LIMITS
from pdf_ar.textstats import BLANK, FAIL, LOW_SIGNAL, PASS, assess_text, measure

WORDS = "يعتمد المدير الإجراءات على مستوى الإدارة وفق الأنظمة واللوائح المعمول بها في الجهة"
TEXT = " ".join([WORDS] * 2)


def test_clean_arabic_passes():
    result = assess_text(TEXT, NATIVE_LIMITS)
    assert (result.verdict, result.reasons) == (PASS, ())


def test_blank_and_too_few_arabic_words():
    assert assess_text("  \n", NATIVE_LIMITS).verdict == BLANK
    assert assess_text("12 - 34", NATIVE_LIMITS).verdict == BLANK
    assert assess_text("يعتمد المدير الإجراءات", NATIVE_LIMITS).verdict == LOW_SIGNAL


def test_each_signal_names_its_reason():
    cases = {
        "ligature_order": TEXT + " اإلدارة",
        "rtl_order": " ".join(["ةرادإلا"] * 30),
        "broken_glyphs": TEXT + " " + "\ufffd" * 5,
        "spacing": "a b c d e f g h i j",
        "numeric_order": TEXT + " -08DOC-ABCD",
    }
    for reason, text in cases.items():
        result = assess_text(text, NATIVE_LIMITS)
        assert result.verdict == FAIL and reason in result.reasons, reason


def test_measure_counts_words_and_letters():
    stats = measure("الإدارة a ب")
    assert (stats.alpha_token_count, stats.single_char_token_count) == (3, 2)
    assert stats.arabic_token_count == 2 and stats.letter_count == 9
