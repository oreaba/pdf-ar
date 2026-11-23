"""Visual to logical order for lines that mix Arabic with Latin text, digits and codes.

Glyphs arrive in visual order. Arabic pieces read right to left and left-to-right
pieces (Latin, digits, codes) read left to right inside them; these functions
turn words of glyphs, in reading-direction visual order, into logical text.
"""

from __future__ import annotations

import unicodedata

ARABIC_PUNCTUATION = frozenset("\u060c\u061b\u061f\u066a\u066b\u066c\u06d4")
MIRRORED = str.maketrans("()[]{}", ")(][}{")


def strong(text: str) -> str | None:
    """'rtl' or 'ltr' by majority of strong letters, None when there are none."""
    rtl = sum(1 for c in text if unicodedata.bidirectional(c) in ("R", "AL"))
    ltr = sum(1 for c in text if unicodedata.bidirectional(c) == "L")
    if rtl == ltr == 0:
        return None
    return "rtl" if rtl > ltr else "ltr"


def kind(text: str) -> str:
    """'L' for text that reads left to right, 'R' for right to left, 'N' for neutral."""
    for char in text:
        if char in ARABIC_PUNCTUATION:
            return "R"
        bidi = unicodedata.bidirectional(char)
        if bidi in ("R", "AL"):
            return "R"
        if bidi in ("L", "EN", "AN"):
            return "L"
    return "N"


def tokens(word: list[dict]) -> list[tuple[str, str]]:
    """Split one word into left-to-right and other pieces; neutrals between L glyphs stay L."""
    kinds = [kind(g["text"]) for g in word]
    resolved = list(kinds)
    for index, value in enumerate(kinds):
        if value == "N":
            before = next((k for k in reversed(kinds[:index]) if k != "N"), None)
            after = next((k for k in kinds[index + 1:] if k != "N"), None)
            resolved[index] = "L" if before == after == "L" else "R"
    out: list[tuple[str, str]] = []
    for glyph, value in zip(word, resolved, strict=False):
        if out and out[-1][0] == value:
            out[-1] = (value, out[-1][1] + glyph["text"])
        else:
            out.append((value, glyph["text"]))
    return out


def _join_runs(pieces: list[tuple[str, str, bool]], run_kind: str) -> str:
    """Join pieces, reversing each run of ``run_kind`` pieces separated only by spaces."""
    result: list[str] = []
    run: list[tuple[str, bool]] = []

    def flush() -> None:
        if not run:
            return
        texts = [text for text, _ in reversed(run)]
        spaced = [space for _, space in reversed(run)]
        joined = texts[0]
        for text, space in zip(texts[1:], spaced[:-1], strict=False):
            joined += (" " if space else "") + text
        result.append((" " if run[0][1] else "") + joined)
        run.clear()

    for value, text, space in pieces:
        if value == run_kind:
            run.append((text, space))
            continue
        flush()
        result.append((" " if space else "") + text)
    flush()
    return "".join(result).strip()


def ltr_logical(words: list[list[dict]]) -> str:
    """Logical text of a left-to-right line from words in left-to-right visual order.

    Arabic pieces arrive in visual order; their glyphs are reversed back (never their
    characters, which keeps ligatures intact), and a run of Arabic words separated
    only by spaces is reversed as a whole.
    """
    pieces: list[tuple[str, str, bool]] = []
    for word in words:
        kinds = [kind(g["text"]) for g in word]
        resolved = list(kinds)
        for index, value in enumerate(kinds):
            if value == "N":
                before = next((k for k in reversed(kinds[:index]) if k != "N"), None)
                after = next((k for k in kinds[index + 1:] if k != "N"), None)
                resolved[index] = "R" if before == after == "R" else "L"
        groups: list[tuple[str, list[dict]]] = []
        for glyph, value in zip(word, resolved, strict=False):
            if groups and groups[-1][0] == value:
                groups[-1][1].append(glyph)
            else:
                groups.append((value, [glyph]))
        for position, (value, glyphs) in enumerate(groups):
            ordered = reversed(glyphs) if value == "R" else glyphs
            text = "".join(g["text"] for g in ordered)
            pieces.append((value, text.translate(MIRRORED) if value == "R" else text,
                           position == 0))
    return _join_runs(pieces, "R")


def rtl_logical(words: list[list[dict]]) -> str:
    """Logical text of a right-to-left line from words in right-to-left visual order.

    Left-to-right pieces arrive reversed; each piece is reversed back, and a run of
    pieces separated only by spaces keeps its left-to-right order. Arabic
    punctuation ends a run, as it does in Word's layout.
    """
    pieces: list[tuple[str, str, bool]] = []  # kind, text, space before
    for word in words:
        for position, (value, text) in enumerate(tokens(word)):
            pieces.append((value, text[::-1] if value == "L" else text, position == 0))
    return _join_runs(pieces, "L")


def unmatched(text: str) -> int:
    """Brackets that close with nothing open, plus brackets left open."""
    depth = unmatched = 0
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth:
                depth -= 1
            else:
                unmatched += 1
    return unmatched + depth


def unmirror(text: str) -> str:
    """Swap the brackets of a paragraph when that pairs them better.

    Word sometimes draws right-to-left brackets as their mirrored glyphs. That can
    only be judged over a whole paragraph (a wrapped line may hold just one half),
    and only by comparison: a list marker such as "1)" is unmatched either way.
    """
    swapped = text.translate(MIRRORED)
    return swapped if unmatched(swapped) < unmatched(text) else text
