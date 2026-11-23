"""Glyphs of a page: collection, font verification, dot-mark attachment and glyph text.

A glyph's letter comes from the full font file only when the embedded glyph's
outline is identical to the font file's glyph, so a wrong or different font
version can never silently change the text; such glyphs keep the PDF's own
ToUnicode text and are counted for the quality gate.
"""

from __future__ import annotations

import io
import re
import unicodedata
from collections import Counter
from functools import cache

from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTFont

from pdf_ar.fonts import FontFile, FontLibrary, font_version, is_arabic_code
from pdf_ar.pdf_adapter import PdfDocument, PdfPage
from pdf_ar.shaping import ShapingModel, load_font, shaping_model

KASHIDA = "\u0640"


def _outline(font: TTFont, name: str) -> list:
    pen = RecordingPen()
    font.getGlyphSet()[name].draw(pen)
    return pen.value


def _frozen(outline: list) -> tuple:
    return tuple((op, tuple(points)) for op, points in outline)


@cache
def outline_index(file: FontFile) -> dict[tuple, int]:
    """Non-empty outline -> glyph ID of the full font, for subsets that renumber glyphs."""
    font = load_font(file.path, file.number)
    index: dict[tuple, int] = {}
    duplicates = set()
    for gid, name in enumerate(font.getGlyphOrder()):
        try:
            outline = _frozen(_outline(font, name))
        except Exception:  # noqa: BLE001
            continue
        if not outline:
            continue
        if outline in index:
            duplicates.add(outline)
        index[outline] = gid
    return {k: v for k, v in index.items() if k not in duplicates}


SHAPE_MATCH = 0.5


def unnamed(font_name: str) -> bool:
    """A PDF font whose name was stripped to a number ("9", "10,Bold")."""
    return re.search(r"[A-Za-z]", font_name.split(",", 1)[0]) is None


class FontMapper:
    """Map (embedded font, glyph id) to a verified glyph of a full font file."""

    def __init__(self, doc: PdfDocument, library: FontLibrary | None) -> None:
        self.doc = doc
        self.library = library
        self.embedded: dict[int, TTFont | None] = {}
        self.cache: dict[tuple, tuple[FontFile, int] | None] = {}
        self.ranks: dict[tuple, list[FontFile]] = {}
        self.files: dict[str, list[FontFile]] = {}
        self.shape_matched: set[str] = set()
        self.stats: Counter = Counter()
        self._xrefs: dict[str, list[int]] | None = None

    def candidates(self, font_name: str) -> list[FontFile]:
        """The folder's files for a font: by name, or for an unnamed font by glyph shapes."""
        if font_name not in self.files:
            files = self.library.candidates(font_name) if self.library else []
            if not files and self.library is not None and unnamed(font_name):
                files = self._shape_matches(font_name)
                if files:
                    self.shape_matched.add(font_name)
            self.files[font_name] = files
        return self.files[font_name]

    def _document_xrefs(self, font_name: str) -> list[int]:
        if self._xrefs is None:
            self._xrefs = {}
            for number in range(1, self.doc.page_count + 1):
                for name, refs in page_xrefs(self.doc.page(number)).items():
                    known = self._xrefs.setdefault(name, [])
                    known += [x for x in refs if x not in known]
        return self._xrefs.get(font_name, [])

    def _embedded_outlines(self, xrefs: list[int]) -> list[tuple]:
        outlines = []
        for xref in xrefs:
            embedded = self._embedded(xref)
            if embedded is None:
                continue
            for name in embedded.getGlyphOrder():
                try:
                    outline = _frozen(_outline(embedded, name))
                except Exception:  # noqa: BLE001
                    continue
                if outline:
                    outlines.append(outline)
        return outlines

    def _shape_matches(self, font_name: str) -> list[FontFile]:
        """Folder files holding at least half of the embedded font's glyph outlines."""
        outlines = self._embedded_outlines(self._document_xrefs(font_name))
        if not outlines:
            return []
        scores = {f: sum(o in self._outline_index(f) for o in outlines)
                  for f in self.library.files()}
        good = [f for f, score in scores.items() if score >= SHAPE_MATCH * len(outlines)]
        return sorted(good, key=lambda f: (-scores[f], f.path, f.number))

    def embedded_version(self, font_name: str, xrefs: dict[str, list[int]]) -> str | None:
        """The version recorded in the PDF's embedded copy of the font."""
        for xref in xrefs.get(font_name, []):
            embedded = self._embedded(xref)
            if embedded is not None:
                version = font_version(embedded)
                if version:
                    return version
        return None

    def font_file(self, font_name: str) -> FontFile | None:
        files = self.candidates(font_name)
        return files[0] if files else None

    def _ranked(self, font_name: str, xrefs: dict[str, list[int]]) -> list[FontFile]:
        """The font's versions, best first: by how many embedded glyphs each matches.

        Each glyph tries the best version first, so a document's letters and dots come
        from one version wherever it can; marks only attach within one version.
        """
        files = self.candidates(font_name)
        if len(files) < 2:
            return files
        key = (font_name, tuple(xrefs.get(font_name, [])))
        if key not in self.ranks:
            outlines = self._embedded_outlines(list(key[1]))
            scores = {f: sum(o in self._outline_index(f) for o in outlines) for f in files}
            self.ranks[key] = sorted(files, key=lambda f: -scores[f])
        return self.ranks[key]

    def _embedded(self, xref: int) -> TTFont | None:
        if xref not in self.embedded:
            program = self.doc.font_program(xref)
            try:
                self.embedded[xref] = TTFont(io.BytesIO(program)) if program else None
            except Exception:  # noqa: BLE001 - unparsable programs fall back to ToUnicode
                self.embedded[xref] = None
        return self.embedded[xref]

    def _outline_index(self, file: FontFile) -> dict[tuple, int]:
        return outline_index(file)

    def verified(self, font_name: str, gid: int,
                 xrefs: dict[str, list[int]]) -> tuple[FontFile, int] | None:
        """(font file, glyph ID in it) when this embedded glyph's outline matches the file.

        The same glyph ID is tried first (Word keeps the original IDs). Producers that
        renumber their subsets are matched by identical, unambiguous outline instead.
        """
        key = (font_name, gid)
        if key in self.cache:
            return self.cache[key]
        self.cache[key] = None
        files = self._ranked(font_name, xrefs)
        if not files or gid < 0:
            return None
        embedded_outlines = self.glyph_outlines(font_name, gid, xrefs)
        for file in files:
            system = load_font(file.path, file.number)
            try:
                reference = _outline(system, system.getGlyphName(gid))
            except Exception:  # noqa: BLE001
                reference = None
            if any(outline == reference for outline in embedded_outlines):
                self.cache[key] = (file, gid)
                return self.cache[key]
        for file in files:
            index = None
            for outline in embedded_outlines:
                if not outline:
                    continue
                index = index if index is not None else self._outline_index(file)
                match = index.get(_frozen(outline))
                if match is not None:
                    self.cache[key] = (file, match)
                    return self.cache[key]
        return None

    def glyph_outlines(self, font_name: str, gid: int,
                       xrefs: dict[str, list[int]]) -> list[list]:
        """The glyph's outline in each embedded copy of the font that holds it; empty when
        the PDF's font programs cannot be read, so no font file could ever verify it."""
        outlines = []
        for xref in xrefs.get(font_name, []):
            embedded = self._embedded(xref)
            if embedded is None or gid < 0 or gid >= len(embedded.getGlyphOrder()):
                continue
            try:
                outlines.append(_outline(embedded, embedded.getGlyphName(gid)))
            except Exception:  # noqa: BLE001
                continue
        return outlines


def page_xrefs(page: PdfPage) -> dict[str, list[int]]:
    xrefs: dict[str, list[int]] = {}
    for xref, basefont, _embedded in page.fonts():
        names = xrefs.setdefault(basefont.split("+")[-1], [])
        if xref not in names:
            names.append(xref)
    return xrefs


def _occluders(page: PdfPage) -> list[tuple[int, tuple]]:
    """(painting order, box) of opaque filled rectangles that could cover text."""
    out = []
    for drawing in page.drawings:
        if not drawing.opaque_fill or drawing.seqno < 0:
            continue
        for item in drawing.items:
            if item[0] in ("re", "qu"):
                x0, y0, x1, y1 = item[1]
                if x1 - x0 > 2 and y1 - y0 > 2:
                    out.append((drawing.seqno, item[1]))
    return out


def _covered(span: dict, occluders: list[tuple[int, tuple]]) -> bool:
    """Whether an opaque rectangle painted after the span hides all of it."""
    seqno = span.get("seqno", -1)
    x0, y0, x1, y1 = span["bbox"]
    tol = 0.5
    return any(order > seqno and box[0] - tol <= x0 and box[1] - tol <= y0
               and box[2] + tol >= x1 and box[3] + tol >= y1 for order, box in occluders)


def _off_page(span: dict, page: PdfPage) -> bool:
    x0, y0, x1, y1 = span["bbox"]
    return x1 <= 0 or y1 <= 0 or x0 >= page.width or y0 >= page.height


def visible_spans(page: PdfPage):
    """(span index, span, None) for visible spans, or a reason: ``off_page`` or ``hidden``."""
    occluders = _occluders(page)
    for span_index, span in enumerate(page.spans()):
        reason = None
        if _off_page(span, page):
            reason = "off_page"
        elif span.get("type") == 3 or span.get("opacity", 1) == 0 or (
                occluders and _covered(span, occluders)):
            reason = "hidden"
        yield span_index, span, reason


def collect(page: PdfPage, mapper: FontMapper, xrefs: dict[str, list[int]],
            skipped: Counter | None = None, use_fonts: bool = True) -> list[dict]:
    """Every visible drawn glyph once, with its verified shaping model and the PDF's own text.

    Invisible text and text painted over by a later opaque box (a second-language
    copy of a flowchart under the visible one, for example) are skipped and counted
    in ``skipped["hidden"]``; text drawn wholly outside the page (a table cut off at
    the page edge) in ``skipped["off_page"]``. With ``use_fonts`` off, no font file
    is consulted and every glyph keeps the PDF's own text (the native engine).
    """
    items: list[dict] = []
    seen: set = set()
    for span_index, span, reason in visible_spans(page):
        if reason:
            if skipped is not None:
                skipped[reason] += sum(1 for c in span["chars"] if c[1] >= 0)
            continue
        font = span["font"].split("+")[-1]
        size = span["size"]
        direction = (round(span["dir"][0], 2), round(span["dir"][1], 2))
        previous = None
        for code, gid, origin, bbox in span["chars"]:
            char = chr(code) if code > 0 else ""
            if gid < 0 and previous is not None:
                previous["pdf"] += char
                continue
            key = (font, gid, round(origin[0], 1), round(origin[1], 1))
            if key in seen:
                previous = None
                continue
            seen.add(key)
            match = mapper.verified(font, gid, xrefs) if use_fonts else None
            model = shaping_model(match[0].path, match[0].number) if match else None
            previous = {"gid": gid, "pdf": char, "origin": origin, "bbox": bbox, "size": size,
                        "model": model, "name": model.name(match[1]) if model else None,
                        "marks": {}, "dir": direction, "span": span_index, "index": len(items),
                        "font": font, "native": not use_fonts,
                        "has_file": use_fonts and mapper.font_file(font) is not None}
            items.append(previous)
    return items


def attach_marks(items: list[dict]) -> None:
    """Attach every dot mark to the ligature part whose anchor predicts its position best."""
    bases = [item for item in items if item["model"] and item["name"] not in item["model"].marks]
    for mark in items:
        model = mark["model"]
        if model is None or mark["name"] not in model.marks:
            continue
        size = mark["size"]
        mx, my = mark["origin"]
        best = None
        for base in bases:
            if base["model"] is not model:
                continue
            bx, by = base["origin"]
            if abs(by - my) > 3 * size or abs(bx - mx) > 3 * size:
                continue
            scale = base["size"] / model.upem
            dx, dy = base["dir"]
            for part in range(len(model.parts(base["name"]))):
                for ox, oy in model.attach_positions(base["name"], part, mark["name"]):
                    px = bx + scale * (ox * dx + oy * dy)
                    py = by + scale * (ox * dy - oy * dx)
                    distance = abs(px - mx) + abs(py - my)
                    if best is None or distance < best[0]:
                        best = (distance, base, part)
        if best is not None and best[0] < 0.6 * size:
            best[1]["marks"].setdefault(best[2], []).append(mark["name"])
            mark["attached"] = True


def _arabic(text: str) -> bool:
    return any(is_arabic_code(ord(c)) for c in text)


def glyph_text(item: dict, counts: Counter) -> str | None:
    """Final text of a glyph: '' for a kashida joiner, None to drop the glyph (dot marks).

    ``counts`` records where each glyph's text came from, for the quality gate:
    ``font`` (font tables), ``pdf`` (ToUnicode, non-Arabic, or every glyph in the native
    engine, where ``unmapped`` counts glyphs the PDF gives no text), and for glyphs that
    draw Arabic but could not be read from a verified font: ``missing_font``,
    ``unverified`` (outline differs from the font file) and ``unresolved`` (the
    font tables do not name the glyph). Unattached dot marks count as ``orphan_mark``.
    """
    model: ShapingModel | None = item["model"]
    pdf = item["pdf"]
    if model is None:
        if item.get("native"):
            # A drawn glyph the PDF maps to nothing (or to U+FFFD) has lost its letter.
            counts["unmapped" if not pdf or "\ufffd" in pdf else "pdf"] += 1
        elif _arabic(pdf):
            counts["unverified" if item["has_file"] else "missing_font"] += 1
        elif pdf == "" and item["has_file"]:
            counts["unverified"] += 1
        else:
            counts["pdf"] += 1
        return pdf
    if item["name"] in model.marks:
        counts["mark" if item.get("attached") else "orphan_mark"] += 1
        return None
    if item["name"] in model.connectors and not pdf.strip():
        # A stroke the font appends to join letters; Word gives it no text.
        counts["kashida"] += 1
        return ""
    letters = [model.letter(part, item["marks"].get(index, []))
               for index, part in enumerate(model.parts(item["name"]))]
    if any(letter is None for letter in letters):
        counts["unresolved" if _arabic(pdf) else "pdf"] += 1
        return pdf
    font_text = unicodedata.normalize("NFKC", "".join(letters))
    if font_text == KASHIDA:
        counts["kashida"] += 1
        return ""
    # A verified space glyph is a space, even where the PDF's map says otherwise (one
    # Calibri Bold subset maps its space to إ).
    if _arabic_block(font_text) or not pdf.strip() or (font_text.isspace() and pdf != font_text):
        counts["font"] += 1
        return font_text.replace(KASHIDA, "")
    counts["pdf"] += 1
    return pdf


def _arabic_block(text: str) -> bool:
    return any("\u0600" <= c <= "\u06ff" for c in text)
