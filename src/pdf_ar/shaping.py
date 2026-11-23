"""Reverse Arabic shaping from a font's own tables.

Shaping turns letters into glyphs: joining forms (init, medi, fina, isol),
ligatures, and, in fonts such as Calibri, dotless shapes plus separate dot marks.
Reading the same GSUB, GPOS and GDEF tables backwards turns glyphs into letters.
Nothing here is specific to one font.
"""

from __future__ import annotations

import unicodedata
from functools import cache

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import DecomposingRecordingPen, RecordingPen
from fontTools.ttLib import TTFont


@cache
def load_font(path: str, number: int = 0) -> TTFont:
    return TTFont(path, fontNumber=number)


@cache
def shaping_model(path: str, number: int = 0) -> ShapingModel:
    return ShapingModel(path, number)


def _subtables(lookup):
    for table in lookup.SubTable:
        yield table.ExtSubTable if lookup.LookupType == 7 else table


@cache
def glyph_text_map(path: str, number: int = 0) -> dict[str, str]:
    """Glyph name -> logical text, from cmap and then GSUB single/alternate/ligature lookups."""
    font = load_font(path, number)
    text: dict[str, str] = {}
    cmap = sorted((font.getBestCmap() or {}).items())

    def rank(code: int) -> int:
        """Standard Arabic letters and ASCII first, other letters next, presentation forms last."""
        if code < 0x80 or 0x0621 <= code <= 0x065F or 0x0660 <= code <= 0x0669:
            return 0
        if 0xFB50 <= code <= 0xFEFF:
            return 2
        return 1

    lookups = font["GSUB"].table.LookupList.Lookup if "GSUB" in font else []
    marks = _marks(font)
    for level in (0, 1, 2):
        for code, name in cmap:
            if rank(code) == level and name not in text:
                text[name] = chr(code)
        _close(text, lookups, marks)
    return text


def _marks(font: TTFont) -> set[str]:
    gdef = font["GDEF"].table.GlyphClassDef if "GDEF" in font else None
    return {name for name, cls in (gdef.classDefs if gdef else {}).items() if cls == 3}


def _close(text: dict[str, str], lookups, marks: set[str]) -> None:
    """Give every glyph reachable by substitution from a known glyph the known glyph's text."""
    changed = True
    while changed:
        changed = False
        for lookup in lookups:
            for table in _subtables(lookup):
                kind = table.LookupType
                if kind == 1:
                    for source, target in table.mapping.items():
                        if source in text and target not in text:
                            text[target] = text[source]
                            changed = True
                elif kind == 2:
                    # A letter replaced by one shape, or split into its shape and
                    # connecting strokes: the shape carries the letter. Splits into a
                    # shape and marks (dots, madda) are resolved from the marks instead
                    # (ShapingModel.dotted).
                    for source, targets in table.mapping.items():
                        if (source in text and targets and targets[0] not in text
                                and not any(t in marks for t in targets[1:])):
                            text[targets[0]] = text[source]
                            changed = True
                elif kind == 3:
                    for source, targets in table.alternates.items():
                        for target in targets:
                            if source in text and target not in text:
                                text[target] = text[source]
                                changed = True
                elif kind == 4:
                    for first, ligatures in table.ligatures.items():
                        for ligature in ligatures:
                            parts = [first, *ligature.Component]
                            if ligature.LigGlyph not in text and all(p in text for p in parts):
                                text[ligature.LigGlyph] = "".join(text[p] for p in parts)
                                changed = True


def _letter_rank(text: str) -> int:
    code = ord(text[0]) if text else 0
    if 0x0621 <= code <= 0x064A:
        return 0
    if 0xFB50 <= code <= 0xFEFF:
        return 2
    return 1


def _mark_signature(font: TTFont, name: str) -> tuple[int, str]:
    """Number of contours (dots) and whether the mark sits above or below its anchor."""
    glyphs = font.getGlyphSet()
    pen = RecordingPen()
    glyphs[name].draw(pen)
    contours = sum(1 for op, _ in pen.value if op == "closePath")
    bounds = BoundsPen(glyphs)
    glyphs[name].draw(bounds)
    if bounds.bounds is None:
        return 0, "none"
    return contours, "above" if (bounds.bounds[1] + bounds.bounds[3]) / 2 > 100 else "below"


class ShapingModel:
    """Reverse of the font's Arabic shaping: ligature parts, dot marks, and mark anchors."""

    def __init__(self, path: str, number: int = 0) -> None:
        font = load_font(path, number)
        self.font = font
        self.upem = font["head"].unitsPerEm
        self.text = glyph_text_map(path, number)
        self.marks = _marks(font)
        self.ligature_parts: dict[str, list[str]] = {}
        self.dotted: dict[str, dict[str, str]] = {}
        # Glyphs a multiple substitution appends after a letter that are not marks.
        # Some are connecting strokes (Sakkal Majalla after seen and sheen), others
        # real characters; the PDF's text tells them apart (see glyphs.glyph_text).
        self.connectors: set[str] = set()
        gsub = font["GSUB"].table.LookupList.Lookup if "GSUB" in font else []
        for lookup in gsub:
            for table in _subtables(lookup):
                if table.LookupType == 4:
                    for first, ligatures in table.ligatures.items():
                        for ligature in ligatures:
                            self.ligature_parts.setdefault(ligature.LigGlyph,
                                                           [first, *ligature.Component])
                elif table.LookupType == 2:
                    for source, targets in table.mapping.items():
                        if source in self.text and len(targets) == 2 and targets[1] in self.marks:
                            letters = self.dotted.setdefault(targets[0], {})
                            new = self.text[source]
                            old = letters.get(targets[1])
                            if old is None or _letter_rank(new) < _letter_rank(old):
                                letters[targets[1]] = new
                        elif len(targets) >= 2 and source in self.text:
                            self.connectors.update(t for t in targets[1:] if t not in self.marks)
        self.signature = {name: _mark_signature(font, name) for name in self.marks}
        self.form = self._forms()
        # Mark classes are numbered per subtable, so anchors are kept per subtable.
        self.attachments: list[tuple[dict, dict, dict]] = []
        gpos = font["GPOS"].table.LookupList.Lookup if "GPOS" in font else []
        for lookup in gpos:
            for table in (t.ExtSubTable if lookup.LookupType == 9 else t for t in lookup.SubTable):
                kind = table.LookupType
                if kind not in (4, 5):
                    continue
                marks = {name: (record.Class, (record.MarkAnchor.XCoordinate,
                                               record.MarkAnchor.YCoordinate))
                         for name, record in zip(table.MarkCoverage.glyphs,
                                                 table.MarkArray.MarkRecord, strict=False)}
                bases: dict[str, dict[int, tuple[float, float]]] = {}
                ligatures: dict[str, list[dict[int, tuple[float, float]]]] = {}
                if kind == 4:
                    for name, record in zip(table.BaseCoverage.glyphs, table.BaseArray.BaseRecord,
                                            strict=False):
                        bases[name] = {cls: (a.XCoordinate, a.YCoordinate)
                                       for cls, a in enumerate(record.BaseAnchor) if a is not None}
                else:
                    for name, attach in zip(table.LigatureCoverage.glyphs,
                                            table.LigatureArray.LigatureAttach, strict=False):
                        ligatures[name] = [
                            {cls: (a.XCoordinate, a.YCoordinate)
                             for cls, a in enumerate(component.LigatureAnchor) if a is not None}
                            for component in attach.ComponentRecord]
                self.attachments.append((marks, bases, ligatures))
        self._ink: dict[str, tuple[float, float] | None] = {}

    def name(self, gid: int) -> str:
        return self.font.getGlyphName(gid)

    def ink(self, name: str) -> tuple[float, float] | None:
        """Horizontal ink extent above the baseline, in font units (descending tails ignored)."""
        if name not in self._ink:
            glyphs = self.font.getGlyphSet()
            pen = DecomposingRecordingPen(glyphs)
            glyphs[name].draw(pen)
            xs = [point[0] for _, points in pen.value for point in points
                  if isinstance(point, tuple) and len(point) == 2 and point[1] >= 0]
            self._ink[name] = (min(xs), max(xs)) if xs else None
        return self._ink[name]

    def joins_left(self, name: str) -> bool:
        """True for initial and medial forms (and ligatures ending in one): they connect onward."""
        parts = self.parts(name)
        return self.form.get(parts[-1] if name in self.ligature_parts else name) in ("init",
                                                                                    "medi")

    def _forms(self) -> dict[str, str]:
        """Joining form of each glyph, from the font's init/medi/fina/isol features."""
        form: dict[str, str] = {}
        if "GSUB" not in self.font:
            return form
        table = self.font["GSUB"].table
        lookups = table.LookupList.Lookup
        for record in table.FeatureList.FeatureRecord:
            tag = record.FeatureTag
            if tag not in ("init", "medi", "fina", "isol"):
                continue
            for index in record.Feature.LookupListIndex:
                for sub in _subtables(lookups[index]):
                    if sub.LookupType == 1:
                        for source, target in sub.mapping.items():
                            # A glyph mapped to itself (Tahoma's initial ط is its
                            # isolated ط) says nothing about its form.
                            if source != target:
                                form.setdefault(target, tag)
        for code, name in (self.font.getBestCmap() or {}).items():
            label = unicodedata.name(chr(code), "")
            for tag, suffix in (("init", "INITIAL FORM"), ("medi", "MEDIAL FORM"),
                                ("fina", "FINAL FORM"), ("isol", "ISOLATED FORM")):
                if label.endswith(suffix):
                    form.setdefault(name, tag)
            # The nominal glyph of an Arabic letter is its isolated form; without this
            # it can inherit "init" through substitution chains (Tahoma's ط).
            if 0x0620 <= code <= 0x06FF and chr(code).isalpha():
                form.setdefault(name, "isol")
        changed = True
        while changed:
            changed = False
            for lookup in lookups:
                for sub in _subtables(lookup):
                    if sub.LookupType == 1:
                        for source, target in sub.mapping.items():
                            if source in form and target not in form:
                                form[target] = form[source]
                                changed = True
                    elif sub.LookupType == 2:
                        for source, targets in sub.mapping.items():
                            if source in form and targets and targets[0] not in form:
                                form[targets[0]] = form[source]
                                changed = True
        return form

    def parts(self, name: str) -> list[str]:
        return self.ligature_parts.get(name, [name])

    def attach_positions(self, base: str, part: int, mark: str) -> list[tuple[float, float]]:
        """Offsets from the base glyph origin, in font units, at which ``mark`` can be drawn."""
        offsets = []
        for marks, bases, ligatures in self.attachments:
            if mark not in marks:
                continue
            cls, (mx, my) = marks[mark]
            if base in ligatures:
                components = ligatures[base]
                if part < len(components) and cls in components[part]:
                    bx, by = components[part][cls]
                    offsets.append((bx - mx, by - my))
            elif part == 0 and base in bases and cls in bases[base]:
                bx, by = bases[base][cls]
                offsets.append((bx - mx, by - my))
        return offsets

    def _vowel(self, mark: str) -> bool:
        text = self.text.get(mark)
        return bool(text) and all(unicodedata.category(c) == "Mn" for c in text)

    def letter(self, part: str, marks: list[str]) -> str | None:
        """The part's letter, dotted by an attached dot mark, followed by attached vowel marks.

        Dots are matched exactly first. Only then may a mark with the same shape stand
        in for a dot, and never a vowel mark: a shadda looks like the small v of a
        letter such as U+0756.
        """
        letter = self.text.get(part)
        options = self.dotted.get(part, {})
        dot_marks = set()
        exact = next((m for m in marks if m in options), None)
        if exact is not None:
            letter = options[exact]
            dot_marks.add(exact)
        else:
            for mark in marks:
                if self._vowel(mark):
                    continue
                same = [m for m in options if self.signature.get(m) == self.signature.get(mark)]
                if same and self.signature.get(mark, (0,))[0] > 0:
                    letter = options[same[0]]
                    dot_marks.add(mark)
                    break
        if letter is None:
            return None
        # A mark glyph can stand for several marks (shadda with fatha), so every
        # character is checked.
        vowels = "".join(self.text[m] for m in marks
                         if m not in dot_marks and m not in options and self._vowel(m))
        return letter + vowels
