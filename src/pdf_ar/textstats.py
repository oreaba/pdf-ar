"""Text measures behind the native engine's check.

Every measure is a count over the page text; nothing here consults a model or a word
list. A page fails when a ratio is above its limit or a document code reads backwards.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

ARABIC_RANGES = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)
_REPLACEMENT = "\ufffd"
_WHITESPACE_CONTROLS = frozenset("\n\r\t\f\v")
_NON_PRINTABLE_CATEGORIES = frozenset({"Cc", "Co", "Cn"})
_DIACRITICS = re.compile("[\u064b-\u065f\u0670\u0640]")

_AL_PREFIX = "\u0627\u0644"  # "ال" as the first letters of an Arabic word
_LA_SUFFIX = "\u0644\u0627"  # the same letters reversed, at the end of a word
# The article followed by an alef form, extracted with the lam-alef ligature in the
# wrong order ("الإ" -> "اإل"). Correct Arabic words do not start this way.
_SWAPPED_PREFIXES = ("\u0627\u0625\u0644", "\u0627\u0623\u0644", "\u0627\u0622\u0644",
                     "\u0627\u0627\u0644")
# Dotless and Persian/Urdu letter forms that appear when a PDF font's glyphs are
# mapped to the wrong code points; they are essentially absent from Arabic text.
_NONSTANDARD_ARABIC = frozenset("\u066e\u066f\u06a1\u06ba\u06cc\u06d5")
# Alef maksura only ends a word in correct Arabic. Followed by another letter it
# marks a reversed lam-alef-maksura ligature ("عىل" for "على") or a reversed word.
_MEDIAL_ALEF_MAKSURA = re.compile("\u0649(?=[\u0621-\u064a])")
# Teh marbuta also only ends a word; at the start it marks a word reversed letter by letter.
_TEH_MARBUTA = "\u0629"
# A document code written in right-to-left extraction order, e.g. "-08DOC-ABCD"
# for "ABCD-DOC-08".
_REVERSED_CODE = re.compile(r"(?<![A-Za-z0-9])-?\d{1,3}[A-Z]{2,4}\s*-\s*[A-Z]{2,6}(?![A-Za-z0-9-])")

LIGATURE_ORDER = "ligature_order"
RTL_ORDER = "rtl_order"
BROKEN_GLYPHS = "broken_glyphs"
SPACING = "spacing"
NUMERIC_ORDER = "numeric_order"
REASONS = (LIGATURE_ORDER, RTL_ORDER, BROKEN_GLYPHS, SPACING, NUMERIC_ORDER)

PASS, FAIL, LOW_SIGNAL, BLANK = "pass", "fail", "low_signal", "blank"


def _is_arabic(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in ARABIC_RANGES)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator > 0 else None


@dataclass
class TextStats:
    """Character and token counts for one piece of text."""

    char_count: int = 0
    letter_count: int = 0
    arabic_letter_count: int = 0
    replacement_char_count: int = 0
    non_printable_count: int = 0
    alpha_token_count: int = 0
    single_char_token_count: int = 0
    arabic_token_count: int = 0
    al_prefix_count: int = 0
    la_suffix_count: int = 0
    swapped_prefix_count: int = 0
    nonstandard_arabic_count: int = 0
    medial_alef_maksura_count: int = 0
    initial_teh_marbuta_count: int = 0


def measure(text: str) -> TextStats:
    stats = TextStats()
    for char in text:
        if char.isspace():
            continue
        stats.char_count += 1
        if char == _REPLACEMENT:
            stats.replacement_char_count += 1
            continue
        if char.isalpha():
            stats.letter_count += 1
            if _is_arabic(char):
                stats.arabic_letter_count += 1
                if char in _NONSTANDARD_ARABIC:
                    stats.nonstandard_arabic_count += 1
        elif char not in _WHITESPACE_CONTROLS and (
            unicodedata.category(char) in _NON_PRINTABLE_CATEGORIES
        ):
            stats.non_printable_count += 1

    for token in text.split():
        letters = "".join(char for char in token if char.isalpha())
        if not letters:
            continue
        stats.alpha_token_count += 1
        if len(letters) == 1:
            stats.single_char_token_count += 1
        if all(_is_arabic(char) for char in letters):
            stats.arabic_token_count += 1
            word = _DIACRITICS.sub("", unicodedata.normalize("NFKC", letters))
            if len(word) >= 3:
                if word.startswith(_AL_PREFIX):
                    stats.al_prefix_count += 1
                if word.endswith(_LA_SUFFIX):
                    stats.la_suffix_count += 1
                if word.startswith(_SWAPPED_PREFIXES):
                    stats.swapped_prefix_count += 1
            if _MEDIAL_ALEF_MAKSURA.search(word):
                stats.medial_alef_maksura_count += 1
            if len(word) >= 2 and word.startswith(_TEH_MARBUTA):
                stats.initial_teh_marbuta_count += 1
    return stats


@dataclass(frozen=True)
class TextResult:
    verdict: str
    reasons: tuple[str, ...]


def _ratios(stats: TextStats, min_tokens: int) -> dict[str, tuple[float | None, str]]:
    """Each limit's measured ratio and the reason it raises. The Arabic ratios need at
    least ``min_tokens`` Arabic words; reversal needs that many article signals."""
    enough = stats.arabic_token_count >= min_tokens
    signals = stats.al_prefix_count + stats.la_suffix_count
    tokens = stats.arabic_token_count
    return {
        "max_replacement_char_ratio":
            (_ratio(stats.replacement_char_count, stats.char_count), BROKEN_GLYPHS),
        "max_non_printable_ratio":
            (_ratio(stats.non_printable_count, stats.char_count), BROKEN_GLYPHS),
        "max_single_char_token_ratio":
            (_ratio(stats.single_char_token_count, stats.alpha_token_count), SPACING),
        "max_arabic_reversal_ratio":
            (stats.la_suffix_count / signals if signals >= min_tokens else None, RTL_ORDER),
        "max_arabic_ligature_swap_ratio":
            (_ratio(stats.swapped_prefix_count, tokens) if enough else None, LIGATURE_ORDER),
        "max_nonstandard_arabic_letter_ratio":
            (_ratio(stats.nonstandard_arabic_count, stats.arabic_letter_count)
             if enough else None, BROKEN_GLYPHS),
        "max_arabic_medial_alef_maksura_ratio":
            (_ratio(stats.medial_alef_maksura_count, tokens) if enough else None,
             LIGATURE_ORDER),
        "max_arabic_initial_teh_marbuta_ratio":
            (_ratio(stats.initial_teh_marbuta_count, tokens) if enough else None, RTL_ORDER),
    }


def assess_text(text: str, limits: Mapping[str, float]) -> TextResult:
    """``fail`` with reasons, ``blank`` without letters, ``low_signal`` with too few
    Arabic words to judge, otherwise ``pass``."""
    stats = measure(text)
    min_tokens = int(limits["min_arabic_tokens"])
    reasons = set()
    if stats.char_count:
        for key, (value, reason) in _ratios(stats, min_tokens).items():
            if value is not None and value > limits[key]:
                reasons.add(reason)
    if _REVERSED_CODE.search(text):
        reasons.add(NUMERIC_ORDER)
    ordered = tuple(reason for reason in REASONS if reason in reasons)
    if ordered:
        verdict = FAIL
    elif stats.letter_count == 0:
        verdict = BLANK
    elif 0 < stats.arabic_token_count < min_tokens or (
        stats.arabic_letter_count > 0 and stats.arabic_token_count == 0
    ):
        verdict = LOW_SIGNAL
    else:
        verdict = PASS
    return TextResult(verdict, ordered)
