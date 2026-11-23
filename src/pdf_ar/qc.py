"""Page quality gate.

For the font rebuild, a page is ``ok`` only when every Arabic glyph was read from
a verified font file and the text shows none of the known corruption signals.
For OCR, a page is ``ok`` when OCR read text with enough confidence and the text
shows none of those signals. Otherwise the page goes to ``review`` with reason
codes. The gate is deliberately strict: a flag is not proof of an error (a typo
in the source can trip it), but an unflagged page has passed every check.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field

from pdf_ar import bidi
from pdf_ar.fonts import is_arabic_code
from pdf_ar.layout import PageText
from pdf_ar.textstats import LOW_SIGNAL, assess_text

# Dotless, Persian and other extended letters, presentation forms and kashida: in
# rebuilt Modern Standard Arabic they mean a glyph was not resolved to its letter.
ODD_LETTERS = re.compile("[\u066e\u066f\u06a1\u06a9\u06ba\u06cc\u06d5\u0750-\u077f"
                         "\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff\u0640]")
SWAPPED_ARTICLE = ("\u0627\u0625\u0644", "\u0627\u0623\u0644", "\u0627\u0622\u0644",
                   "\u0627\u0627\u0644")
MEDIAL_ALEF_MAKSURA = re.compile("\u0649(?=[\u0621-\u064a])")
LIST_NUMBER = re.compile(r"(?m)^\s*[\d\u0621-\u064aA-Za-z]{1,3}\)(?=\s)")
DIACRITICS = re.compile("[\u064b-\u065f\u0670\u0640]")

GLYPH_REASONS = {
    "missing_font": "missing_font",
    "unverified": "unverified_glyphs",
    "unresolved": "unresolved_glyphs",
    "orphan_mark": "orphan_marks",
}


@dataclass
class PageReport:
    page: int
    engine: str = "fonts"
    status: str = "ok"
    reasons: list[str] = field(default_factory=list)
    glyphs: int = 0
    sources: dict[str, int] = field(default_factory=dict)
    words: int = 0
    arabic_words: int = 0
    odd_letters: int = 0
    swapped_article: int = 0
    medial_alef_maksura: int = 0
    initial_teh_marbuta: int = 0
    unbalanced_brackets: int = 0
    has_images: bool = False
    seconds: float = 0.0
    ocr_confidence: float | None = None
    ocr_agreement: float | None = None
    ocr_seconds: float | None = None
    error: str | None = None
    # Which engine's text the page carries, and why earlier engines of ``auto`` failed.
    source: str | None = None
    tried: list[str] = field(default_factory=list)
    # --font-check: whether the font rebuild disagreed with accepted native text.
    native_disagrees: bool | None = None

    def as_dict(self) -> dict:
        return asdict(self)


TEXT_SIGNALS = ("odd_letters", "swapped_article", "medial_alef_maksura", "initial_teh_marbuta",
                "unbalanced_brackets")


def _arabic_word(letters: str) -> bool:
    return bool(letters) and all(is_arabic_code(ord(c)) for c in letters)


def _measure_text(report: PageReport, text: str) -> None:
    for token in text.split():
        letters = "".join(c for c in token if c.isalpha())
        if not letters:
            continue
        report.words += 1
        if not _arabic_word(letters):
            continue
        report.arabic_words += 1
        word = DIACRITICS.sub("", unicodedata.normalize("NFKC", letters))
        if len(word) >= 3 and word.startswith(SWAPPED_ARTICLE):
            report.swapped_article += 1
        if MEDIAL_ALEF_MAKSURA.search(word):
            report.medial_alef_maksura += 1
        if len(word) >= 2 and word.startswith("\u0629"):
            report.initial_teh_marbuta += 1
    report.odd_letters = len(ODD_LETTERS.findall(text))
    # Over the whole page, in reading order: a bracket may open in one paragraph or
    # table cell and close in the next. List numbers such as "1)" never pair.
    report.unbalanced_brackets = bidi.unmatched(LIST_NUMBER.sub("", text))


def _finish(report: PageReport, reasons: Counter) -> PageReport:
    for name in TEXT_SIGNALS:
        if getattr(report, name):
            reasons[name] += getattr(report, name)
    report.reasons = sorted(reasons)
    report.status = "review" if reasons else "ok"
    return report


def check(number: int, page: PageText, has_images: bool) -> PageReport:
    """Gate for the font rebuild."""
    report = PageReport(page=number, glyphs=page.glyphs, has_images=has_images,
                        sources=dict(sorted(page.counts.items())))
    _measure_text(report, page.paragraphs)
    reasons: Counter = Counter()
    if page.glyphs == 0 and has_images:
        reasons["no_text_layer"] += 1
    for source, reason in GLYPH_REASONS.items():
        if page.counts.get(source):
            reasons[reason] += page.counts[source]
    return _finish(report, reasons)


# Limits for the PDF's own text, the same as the corpus profiling the tool was validated on.
NATIVE_LIMITS = {
    "min_arabic_tokens": 20,
    "max_replacement_char_ratio": 0.01, "max_non_printable_ratio": 0.02,
    "max_single_char_token_ratio": 0.30, "max_arabic_reversal_ratio": 0.50,
    "max_arabic_ligature_swap_ratio": 0.01, "max_nonstandard_arabic_letter_ratio": 0.02,
    "max_arabic_medial_alef_maksura_ratio": 0.005,
    "max_arabic_initial_teh_marbuta_ratio": 0.005, "max_unicode_map_error_ratio": 0.02,
}
# Single-letter Arabic "words" other than و: split words, the Word-export corruption the
# measures in textstats miss. Verified rebuilt text stays under 1%; broken native text is
# mostly above 2% (development sample of 41 pages).
MAX_ARABIC_FRAGMENT_RATIO = 0.02
ARABIC_LETTERS = frozenset(chr(c) for c in range(0x0621, 0x064B))


def arabic_fragment_ratio(text: str) -> float | None:
    """Share of pure Arabic words that are a single letter other than و (None under 20)."""
    words = [t for t in text.split() if all(c in ARABIC_LETTERS for c in t)]
    if len(words) < NATIVE_LIMITS["min_arabic_tokens"]:
        return None
    return sum(len(w) == 1 and w != "\u0648" for w in words) / len(words)


def check_native(number: int, page: PageText, has_images: bool) -> PageReport:
    """Gate for the PDF's own text (native engine).

    The page fails on the font rebuild's text signals, on the textstats measures
    (ligature order, reversal, broken glyphs, single-letter spacing, reversed codes),
    when more than 2% of drawn glyphs have no text in the PDF, and when more than 2%
    of Arabic words are split-off single letters. A page with Arabic but too few
    Arabic words to judge (fewer than 20) fails as ``too_little_arabic``.
    """
    report = PageReport(page=number, engine="native", glyphs=page.glyphs, has_images=has_images,
                        sources=dict(sorted(page.counts.items())))
    _measure_text(report, page.paragraphs)
    reasons: Counter = Counter()
    if page.glyphs == 0 and has_images:
        reasons["no_text_layer"] += 1
    unmapped = page.counts.get("unmapped", 0)
    if page.glyphs and unmapped / page.glyphs > NATIVE_LIMITS["max_unicode_map_error_ratio"]:
        reasons["unmapped_glyphs"] += unmapped
    result = assess_text(page.paragraphs, NATIVE_LIMITS)
    for reason in result.reasons:
        reasons[reason] += 1
    fragments = arabic_fragment_ratio(page.paragraphs)
    if fragments is not None and fragments > MAX_ARABIC_FRAGMENT_RATIO:
        reasons["split_words"] += 1
    if report.arabic_words and result.verdict == LOW_SIGNAL:
        reasons["too_little_arabic"] += 1
    return _finish(report, reasons)


def check_ocr(number: int, text: str, confidence: float | None, glyphs: int, has_images: bool,
              min_confidence: float) -> PageReport:
    """Gate for the OCR engine: empty output on a page with content, or low confidence."""
    report = PageReport(page=number, engine="ocr", glyphs=glyphs, has_images=has_images,
                        ocr_confidence=confidence)
    _measure_text(report, text)
    reasons: Counter = Counter()
    if not text.strip() and (glyphs or has_images):
        reasons["ocr_empty"] += 1
    if confidence is not None and confidence < min_confidence:
        reasons["low_ocr_confidence"] += 1
    return _finish(report, reasons)


def failed(number: int, error: Exception, has_images: bool, engine: str = "fonts") -> PageReport:
    return PageReport(page=number, engine=engine, status="review", reasons=["error"],
                      has_images=has_images, error=f"{type(error).__name__}: {error}")
