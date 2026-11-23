"""Page layout: containers, lines, words, spurious spaces and paragraphs.

Lines are built inside each drawn box or ruled table cell (no line crosses a
border) and page-wide elsewhere; wrapped lines are then joined into paragraphs.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from pdf_ar import bidi
from pdf_ar.glyphs import FontMapper, attach_marks, collect, glyph_text, page_xrefs
from pdf_ar.pdf_adapter import Box, Drawing, PdfPage

WORD_GAP_EM = 0.18
LINE_TOLERANCE_EM = 0.35
# A space is real when its neighbours leave at least this share of the space's own
# width (fonts differ: Calibri's space is 0.23 em, Sakkal Majalla's 0.15 em), or,
# for space glyphs drawn with almost no width, this many em.
SPURIOUS_SPACE_SHARE = 0.15
# Letters that only end an Arabic word (lone hamza, teh marbuta, alef maksura): a
# space before one of them, after an Arabic letter, cannot be a word boundary.
WORD_FINAL_ONLY = frozenset("\u0621\u0629\u0649")
SPURIOUS_SPACE_GAP_EM = 0.04
LIST_MARKER = re.compile(r"^([▪•·◦\-–]|\(?\d+[.)]|\(?[\u0621-\u064aA-Za-z][.)])\s+\S")
PARAGRAPH_END = tuple(".:؟?!؛")


@dataclass
class Block:
    """One paragraph in reading order, with the container it sits in.

    ``kind`` is ``paragraph`` (running text), ``cell`` (a ruled table cell) or
    ``box`` (a drawn shape such as a flowchart box). ``bbox`` is the container's box
    for cells and boxes, and the paragraph's own extent otherwise (page points).
    """

    kind: str
    text: str
    lines: list[str]
    bbox: tuple[float, float, float, float]
    rtl: bool


@dataclass
class PageText:
    paragraphs: str
    lines: str
    counts: Counter = field(default_factory=Counter)
    glyphs: int = 0
    blocks: list[Block] = field(default_factory=list)


def _width(box: Box) -> float:
    return box[2] - box[0]


def _height(box: Box) -> float:
    return box[3] - box[1]


TEXT_SOURCES = ("fonts", "pdf")


def rebuild(page: PdfPage, mapper: FontMapper, text_source: str = "fonts") -> PageText:
    """Paragraph text and line-by-line text of one page, with glyph provenance counts.

    ``text_source`` ``fonts`` reads Arabic letters from the verified font files;
    ``pdf`` keeps every glyph's own PDF text (the native engine). Layout is the same.
    """
    if text_source not in TEXT_SOURCES:
        raise ValueError(f"text_source must be one of {TEXT_SOURCES}, not {text_source!r}")
    xrefs = page_xrefs(page)
    skipped: Counter = Counter()
    items = collect(page, mapper, xrefs, skipped, use_fonts=text_source == "fonts")
    attach_marks(items)
    text_blocks = page.text_blocks
    cells = _table_cells(page.drawings)
    boxes = _shapes(page, cells)
    shapes = boxes + cells
    shape_kinds = ["box"] * len(boxes) + ["cell"] * len(cells)
    containers: dict[tuple, Box] = {("shape", i): r for i, r in enumerate(shapes)}
    containers.update({("text", i): b for i, b in enumerate(text_blocks)})
    containers[("none", 0)] = (0.0, 0.0, 0.0, 0.0)

    areas = [_width(r) * _height(r) for r in shapes]

    def block_of(origin: tuple[float, float]) -> tuple:
        """The smallest drawn shape (flowchart box) holding the glyph, else its text block."""
        x, y = origin
        inside = [i for i, r in enumerate(shapes) if r[0] <= x <= r[2] and r[1] <= y <= r[3]]
        if inside:
            return ("shape", min(inside, key=areas.__getitem__))
        for index, (x0, y0, x1, y1) in enumerate(text_blocks):
            if x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1:
                return ("text", index)
        return ("none", 0)

    counts: Counter = Counter(skipped)
    # Each text span goes to the container most of its glyph centres fall in: a
    # letter at a tight box's edge, or a word a border passes through, stays with
    # its own text instead of being cut off.
    votes: dict[int, Counter] = {}
    for item in items:
        x0, y0, x1, y1 = item["bbox"]
        votes.setdefault(item["span"], Counter())[block_of(((x0 + x1) / 2,
                                                            (y0 + y1) / 2))] += 1
    span_block = {span: tally.most_common(1)[0][0] for span, tally in votes.items()}
    groups: dict[tuple, list[dict]] = {}
    for item in items:
        text = glyph_text(item, counts)
        if text is None:
            continue
        dx, dy = item["dir"]
        x0, y0, x1, y1 = item["bbox"]
        along = [x * dx + y * dy for x in (x0, x1) for y in (y0, y1)]
        ox, oy = item["origin"]
        start = ox * dx + oy * dy
        model = item["model"]
        ink = model.ink(item["name"]) if model and item["name"] else None
        scale = item["size"] / model.upem if model else 0
        block = span_block[item["span"]]
        group = groups.setdefault((item["dir"], block if block[0] == "shape" else None), [])
        group.append({"text": text, "x0": min(along), "x1": max(along), "ox": start,
                      "y": -ox * dy + oy * dx, "size": item["size"],
                      "seq": item["index"], "span": item["span"],
                      "space": text.isspace(), "joiner": text == "",
                      "ink0": start + ink[0] * scale if ink else min(along),
                      "ink1": start + ink[1] * scale if ink else max(along),
                      "joins": bool(model and item["name"] and model.joins_left(item["name"])),
                      "block": block, "font": item["font"]})

    # Lines are built per drawn box or table cell, since no line crosses a border;
    # elsewhere they are built page-wide, so no word is ever split, and whole words
    # are then grouped by the text block holding most of their glyphs.
    by_block: dict[tuple, list[tuple[int, bool, list[list[dict]]]]] = {}
    page_tally: Counter = Counter()
    for (direction, _cell), glyphs in groups.items():
        for number, (rtl, words) in enumerate(_lines(glyphs)):
            per_block: dict[tuple, list[list[dict]]] = {}
            for word in words:
                votes: dict[tuple, int] = {}
                for glyph in word:
                    votes[glyph["block"]] = votes.get(glyph["block"], 0) + 1
                block = max(votes, key=votes.get) if votes else ("none", 0)
                per_block.setdefault(block, []).append(word)
            for block, block_words in per_block.items():
                by_block.setdefault((direction, block), []).append((number, rtl, block_words))
                if any(_is_word(word) for word in block_words):
                    page_tally[rtl] += 1
    # Boxes side by side are read right to left when most of them hold Arabic words, and
    # left to right when most hold only English ones (an English table in an Arabic
    # document). Words have two letters in a row, so numbers and codes such as "1A" or
    # a RACI "R/I" do not count. A tie (a term beside its Arabic translation) is a bilingual Arabic
    # layout and reads right to left; rows without words take the page's direction.
    page_rtl = page_tally[True] >= page_tally[False]
    starts: dict[tuple, bool | None] = {}
    for key, entries in by_block.items():
        words = [word for _number, _rtl, line in entries for word in line if _is_word(word)]
        starts[key] = any(_arabic_word(word) for word in words) if words else None
    # Vertical text (a swimlane's name) is read where it stands, before the lane's
    # boxes; other rotated text (a diagonal stamp) comes last.
    rows: dict[tuple, list[tuple]] = {}
    for key in by_block:
        direction, block = key
        last = direction not in ((1.0, 0.0), (0.0, 1.0), (0.0, -1.0))
        rows.setdefault((last, round(containers[block][1] / 4)), []).append(key)
    def is_box(key: tuple) -> bool:
        return key[1][0] == "shape" and shape_kinds[key[1][1]] == "box"

    def beside(small: Box, big: Box) -> bool:
        """``small`` stands next to ``big``, within its height, and is not much shorter."""
        return (big[1] <= small[1] and small[3] <= big[3]
                and _height(big) <= 3 * _height(small)
                and (small[2] <= big[0] or big[2] <= small[0]))

    def terminal(key: tuple) -> bool:
        """A flowchart terminal or connector: a drawn box of at most two words."""
        return is_box(key) and sum(len(line) for _n, _rtl, line in by_block[key]) <= 2

    # A row of terminals that each stand beside a box of the row before (a short Start
    # oval next to a taller step) belongs to that row. Table cells never move.
    merged: list[list[tuple]] = []
    for row in sorted(rows):
        keys = rows[row]
        if merged and not row[0] and all(terminal(k) for k in keys):
            above = [containers[k[1]] for k in merged[-1] if is_box(k)]
            if all(any(beside(containers[k[1]], b) for b in above) for k in keys):
                merged[-1].extend(keys)
                continue
        merged.append(list(keys))
    reading_order: list[tuple] = []
    for keys in merged:
        tally = Counter(starts[key] for key in keys if starts[key] is not None)
        rtl = page_rtl if not tally else tally[True] >= tally[False]
        keys.sort(key=lambda k: -containers[k[1]][2] if rtl else containers[k[1]][0])
        reading_order.extend(keys)

    blocks_out: list[list[dict]] = []
    for key in reading_order:
        lines = []
        for _number, rtl, words in sorted(by_block[key], key=lambda entry: entry[0]):
            text = (bidi.rtl_logical(words) if rtl else bidi.ltr_logical(words)).strip()
            if text:
                lines.append(_line_record(text, rtl, words, key[0]))
                lines[-1]["container"] = key[1]
        if lines:
            width = max(line["x1"] - line["x0"] for line in lines)
            for line in lines:
                line["block_width"] = width
            blocks_out.append(lines)
    rules = _horizontal_rules(page.drawings)
    paragraphs: list[list[dict]] = []
    previous = None
    for line in (line for block in blocks_out for line in block):
        if previous is not None and _continues(previous, line, rules):
            paragraphs[-1].append(line)
        else:
            paragraphs.append([line])
        previous = line
    for paragraph in paragraphs:
        joined = " ".join(line["text"] for line in paragraph)
        if any(line["rtl"] for line in paragraph) and bidi.unmirror(joined) != joined:
            for line in paragraph:
                line["text"] = line["text"].translate(bidi.MIRRORED)
    line_view = "\n\n".join("\n".join(line["text"] for line in block) for block in blocks_out)
    paragraph_view = "\n\n".join(" ".join(line["text"] for line in p) for p in paragraphs)
    structure = []
    for paragraph in paragraphs:
        container = paragraph[0]["container"]
        if container[0] == "shape":
            kind, bbox = shape_kinds[container[1]], containers[container]
        else:
            kind = "paragraph"
            ys = [line["y"] for line in paragraph]
            size = max(line["size"] for line in paragraph)
            bbox = (min(line["x0"] for line in paragraph), min(ys) - size,
                    max(line["x1"] for line in paragraph), max(ys) + 0.3 * size)
        structure.append(Block(kind, " ".join(line["text"] for line in paragraph),
                               [line["text"] for line in paragraph],
                               tuple(round(v, 2) for v in bbox),
                               sum(line["rtl"] for line in paragraph) * 2 >= len(paragraph)))
    return PageText(paragraph_view, line_view, counts, len(items), structure)


def _line_record(text: str, rtl: bool, words: list[list[dict]], direction: tuple) -> dict:
    glyphs = [g for word in words for g in word]
    sizes = sorted(g["size"] for g in glyphs)
    fonts: dict[str, int] = {}
    for g in glyphs:
        if not g["space"]:
            fonts[g["font"]] = fonts.get(g["font"], 0) + 1
    first = words[0]
    return {"text": text, "rtl": rtl, "dir": direction,
            "x0": min(g["x0"] for g in glyphs), "x1": max(g["x1"] for g in glyphs),
            "y": sorted(g["y"] for g in glyphs)[len(glyphs) // 2],
            "size": sizes[len(sizes) // 2],
            "font": max(fonts, key=fonts.get) if fonts else "",
            "first_width": max(g["x1"] for g in first) - min(g["x0"] for g in first)}


def _inside(inner: Box, outer: Box, tol: float = 1.5) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def _shapes(page: PdfPage, cells: list[Box]) -> list[Box]:
    """Bounding boxes of drawn closed shapes that can hold text (flowchart boxes, callouts).

    A fill without an outline inside a table cell is the cell's shading (Word also
    shades each paragraph separately), not a box of its own.
    """
    area = page.width * page.height
    shapes = []
    for path in page.drawings:
        rect = path.rect
        if _width(rect) < 20 or _height(rect) < 10 or _width(rect) * _height(rect) > 0.25 * area:
            continue
        if not path.stroked and any(_inside(rect, cell) for cell in cells):
            continue
        if path.closed or path.filled or any(item[0] in ("re", "qu") for item in path.items):
            shapes.append(rect)
    return shapes


def _table_cells(drawings: list[Drawing]) -> list[Box]:
    """Cells of ruled tables, built from drawn horizontal and vertical rules.

    Word draws table borders as thin filled rectangles, which generic table finders
    miss. A cell is a span between two vertical rules that both cover the interval
    between two horizontal rules, which both cover the span's middle.
    """
    tol = 1.5
    horizontal, vertical = [], []
    for path in drawings:
        for item in path.items:
            if item[0] == "re":
                x0, y0, x1, y1 = item[1]
                if y1 - y0 < tol and x1 - x0 > tol:
                    horizontal.append(((y0 + y1) / 2, x0, x1))
                elif x1 - x0 < tol and y1 - y0 > tol:
                    vertical.append(((x0 + x1) / 2, y0, y1))
            elif item[0] == "l":
                (px, py), (qx, qy) = item[1], item[2]
                if abs(py - qy) < tol and abs(px - qx) > tol:
                    horizontal.append(((py + qy) / 2, min(px, qx), max(px, qx)))
                elif abs(px - qx) < tol and abs(py - qy) > tol:
                    vertical.append(((px + qx) / 2, min(py, qy), max(py, qy)))
    if not horizontal or not vertical:
        return []

    levels: list[float] = []
    for y, _, _ in sorted(horizontal):
        if not levels or y - levels[-1] > tol:
            levels.append(y)
    columns: list[float] = []
    for x, _, _ in sorted(vertical):
        if not columns or x - columns[-1] > tol:
            columns.append(x)
    at_level = {y: [(x0, x1) for hy, x0, x1 in horizontal if abs(hy - y) < tol] for y in levels}
    at_column = {x: sorted((a, b) for vx, a, b in vertical if abs(vx - x) < tol)
                 for x in columns}

    def covered_h(y: float, x: float) -> bool:
        return any(x0 - tol <= x <= x1 + tol for x0, x1 in at_level[y])

    def vertical_span(x: float, y0: float, y1: float) -> float:
        """How much of [y0, y1] the vertical rules at x cover."""
        total, end = 0.0, y0
        for a, b in at_column[x]:
            if a >= y1:
                break
            a, b = max(a, y0), min(b, y1)
            if b > end:
                total += b - max(a, end)
                end = b
        return total

    cells = []
    for i, top in enumerate(levels):
        # A column that fails to cover [top, bottom] cannot cover a taller span, so
        # the candidates only shrink as bottom moves down.
        active = list(columns)
        for bottom in levels[i + 1:]:
            if bottom - top < 4:
                continue
            active = [x for x in active
                      if vertical_span(x, top, bottom) >= bottom - top - 2 * tol]
            if len(active) < 2:
                break
            for left, right in zip(active, active[1:], strict=False):
                middle = (left + right) / 2
                if right - left > 4 and covered_h(top, middle) and covered_h(bottom, middle):
                    cells.append((left, top, right, bottom))
    return cells


def _horizontal_rules(drawings: list[Drawing]) -> list[tuple[float, float, float]]:
    """Horizontal strokes and rectangle edges (table borders, boxes) as (y, x0, x1)."""
    rules = []
    for path in drawings:
        for item in path.items:
            if item[0] == "l":
                (px, py), (qx, qy) = item[1], item[2]
                if abs(py - qy) < 1:
                    rules.append((py, min(px, qx), max(px, qx)))
            elif item[0] == "re":
                x0, y0, x1, y1 = item[1]
                if y1 - y0 < 2:
                    rules.append(((y0 + y1) / 2, x0, x1))
                else:
                    rules.extend([(y0, x0, x1), (y1, x0, x1)])
            elif item[0] == "qu":
                x0, y0, x1, y1 = item[1]
                rules.extend([(y0, x0, x1), (y1, x0, x1)])
    return rules


def _continues(previous: dict, line: dict, rules: list[tuple[float, float, float]]) -> bool:
    """Whether ``line`` continues the paragraph that ``previous`` wrapped out of.

    Inside a box or table cell, any next line continues unless it starts a list
    item. Elsewhere it must share direction, font and size, sit on the next
    baseline of the same column with no ruled line between them, not start a list
    item, and its first word must not have fitted on the previous line.
    """
    if previous["dir"] != line["dir"]:
        return False
    horizontal = line["dir"] == (1.0, 0.0)
    if {previous["container"][0], line["container"][0]} & {"shape"}:
        return (previous["container"] == line["container"]
                and 0 < line["y"] - previous["y"] <= 2.5 * line["size"]
                and not LIST_MARKER.match(line["text"]))
    if previous["rtl"] != line["rtl"] or previous["font"] != line["font"]:
        return False
    size = line["size"]
    if abs(previous["size"] - size) > 0.1 * size:
        return False
    if not 0.8 * size <= line["y"] - previous["y"] <= 1.7 * size:
        return False
    overlap = min(previous["x1"], line["x1"]) - max(previous["x0"], line["x0"])
    if overlap < 0.5 * min(previous["x1"] - previous["x0"], line["x1"] - line["x0"]):
        return False
    if previous["text"].endswith(PARAGRAPH_END) or LIST_MARKER.match(line["text"]):
        return False
    top, bottom = previous["y"] + 0.15 * size, line["y"] - 0.8 * size
    for y, x0, x1 in rules if horizontal else ():
        if top < y < bottom and min(x1, line["x1"]) - max(x0, line["x0"]) > 0:
            return False
    width = max(previous["block_width"], line["block_width"])
    used = previous["x1"] - previous["x0"]
    return used + 0.25 * size + line["first_width"] > 0.97 * width


def _is_word(word: list[dict]) -> bool:
    """Two letters in a row: a word, not a number or a code such as "1A" or "R/I"."""
    run = 0
    for glyph in word:
        letter = bool(glyph["text"]) and unicodedata.bidirectional(glyph["text"][:1]) in (
            "L", "R", "AL")
        run = run + 1 if letter else 0
        if run >= 2:
            return True
    return False


def _arabic_word(word: list[dict]) -> bool:
    return any(unicodedata.bidirectional(g["text"][:1]) in ("R", "AL") for g in word if g["text"])


def _script_change(previous: dict, glyph: dict) -> bool:
    """Arabic and Latin letters never join, so a letter-to-letter script change ends a word."""
    a, b = previous["text"][-1:], glyph["text"][:1]
    if not (a.isalpha() and b.isalpha()):
        return False
    return {bidi.kind(a), bidi.kind(b)} == {"L", "R"}


def _lines(glyphs: list[dict]) -> list[tuple[bool, list[list[dict]]]]:
    """Lines of one writing direction: (right-to-left?, words in visual reading order)."""
    glyphs = sorted(glyphs, key=lambda g: g["y"])
    lines: list[list[dict]] = []
    for glyph in glyphs:
        if lines and abs(glyph["y"] - lines[-1][0]["y"]) <= LINE_TOLERANCE_EM * glyph["size"]:
            lines[-1].append(glyph)
        else:
            lines.append([glyph])
    lines = _attach_scripts(lines)
    out = []
    # Spans that carry space glyphs (Word exports) mark word boundaries with them;
    # gaps only separate words where a producer omits spaces. Inside such a span a
    # gap is the natural space after a non-joining letter (alef before hamza).
    spaced_spans = {g["span"] for g in glyphs if g["space"]}
    # A line of only digits and punctuation (a wrapped "1)") takes the direction of
    # the line before it.
    direction = bidi.strong("".join(g["text"] for g in glyphs)) or "ltr"
    for line in lines:
        direction = bidi.strong("".join(g["text"] for g in line)) or direction
        rtl = direction == "rtl"
        ordered = drop_spurious_spaces(_visual_order(line, rtl), rtl)
        words: list[list[dict]] = [[]]
        previous = None
        for glyph in ordered:
            if glyph["space"]:
                words.append([])
                previous = None
                continue
            if previous is not None:
                gap = (previous["x0"] - glyph["x1"]) if rtl else (glyph["x0"] - previous["x1"])
                same_spaced_span = (previous["span"] == glyph["span"]
                                    and glyph["span"] in spaced_spans)
                if (gap > WORD_GAP_EM * glyph["size"] and not same_spaced_span) or \
                        _script_change(previous, glyph):
                    words.append([])
            words[-1].append(glyph)
            previous = glyph
        words = [word for word in words if word]
        if words:
            out.append((rtl, words))
    return out


def _attach_scripts(lines: list[list[dict]]) -> list[list[dict]]:
    """Superscripts (the "st" of 21st, a footnote mark) join the line they are raised from.

    A superscript is a run of at most four glyphs, at most 0.8 of the host line's size,
    with its baseline 0.15 to 0.6 em above the host's, that starts or ends within
    0.3 em of a host glyph. Anything else small on a nearby baseline (a label in a
    smaller font) stays a line of its own.
    """
    def size(line: list[dict]) -> float:
        sizes = sorted(g["size"] for g in line)
        return sizes[len(sizes) // 2]

    sizes = [size(line) for line in lines]
    moves: list[tuple[int, list[dict], int]] = []
    for i, small in enumerate(lines):
        hosts = [j for j in range(len(lines)) if j != i and sizes[i] <= 0.8 * sizes[j]
                 and 0.15 * sizes[j] <= lines[j][0]["y"] - small[0]["y"] <= 0.6 * sizes[j]]
        if not hosts:
            continue
        runs: list[list[dict]] = []
        for glyph in sorted(small, key=lambda g: g["x0"]):
            if runs and glyph["x0"] - runs[-1][-1]["x1"] <= 0.5 * glyph["size"]:
                runs[-1].append(glyph)
            else:
                runs.append([glyph])
        for run in runs:
            if sum(not g["space"] for g in run) > 4:
                continue
            x0, x1 = run[0]["x0"], max(g["x1"] for g in run)
            for j in hosts:
                reach = 0.3 * sizes[j]
                if any(abs(x0 - g["x1"]) <= reach or abs(g["x0"] - x1) <= reach
                       for g in lines[j] if not g["space"]):
                    moves.append((i, run, j))
                    break
    for i, run, j in moves:
        moved = {id(g) for g in run}
        lines[i] = [g for g in lines[i] if id(g) not in moved]
        lines[j].extend(run)
    return [line for line in lines if line]


def _visual_order(line: list[dict], rtl: bool) -> list[dict]:
    """Glyphs in reading-direction visual order.

    Inside one span, Word writes glyphs strictly left to right, so the span's own
    order is used there (kerned glyphs can overlap, which makes positions unreliable).
    Spans are ordered by position.
    """
    spans: dict[int, list[dict]] = {}
    for glyph in line:
        spans.setdefault(glyph["span"], []).append(glyph)
    pieces = []
    for glyphs in spans.values():
        glyphs.sort(key=lambda g: g["seq"])
        pieces.append(glyphs)
    if rtl:
        pieces.sort(key=lambda p: -max(g["x1"] for g in p))
        return [g for piece in pieces for g in reversed(piece)]
    pieces.sort(key=lambda p: min(g["x0"] for g in p))
    return [g for piece in pieces for g in piece]


def drop_spurious_spaces(ordered: list[dict], rtl: bool) -> list[dict]:
    """Remove space glyphs that fall inside a word, where Word drew no visible gap.

    Neighbours are taken in reading order. An Arabic letter in an initial or medial
    form cannot end a word, so a space after it is spurious. Otherwise, next to
    Arabic, the space is real only if its neighbours' advance boxes leave visible
    room (ink alone misleads: tails below the baseline are not counted).
    """
    kept: list[dict] = []
    for index, glyph in enumerate(ordered):
        if not glyph["space"]:
            kept.append(glyph)
            continue
        before = next((g for g in reversed(ordered[:index])
                       if not g["space"] and not g["joiner"]), None)
        after = next((g for g in ordered[index + 1:]
                      if not g["space"] and not g["joiner"]), None)
        if before is None or after is None:
            kept.append(glyph)
            continue
        kinds = {bidi.kind(before["text"]), bidi.kind(after["text"])}
        if "L" in kinds or "R" not in kinds:
            kept.append(glyph)
            continue
        # Arabic reads right to left: in a left-to-right line the visual order of an
        # Arabic run is reversed, so the letter that follows the space is ``before``.
        # The joining-form test is reliable only in right-to-left lines.
        next_letter = after if rtl else before
        if kinds == {"R"} and ((rtl and before["joins"])
                               or next_letter["text"] in WORD_FINAL_ONLY):
            continue
        right, left = (before, after) if rtl else (after, before)
        gap = right["x0"] - left["x1"]
        width = glyph["x1"] - glyph["x0"]
        needed = (SPURIOUS_SPACE_SHARE * width if width >= 0.1 * glyph["size"]
                  else SPURIOUS_SPACE_GAP_EM * glyph["size"])
        if gap >= needed:
            kept.append(glyph)
    return kept
