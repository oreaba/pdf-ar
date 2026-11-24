"""Which fonts and versions a set of PDFs needs, and whether the fonts folder has them.

A glyph needs a full font file when it is visible, drawn from a font the PDF embeds,
and Arabic or mapped to no character: the rebuild reads such glyphs from the font
file. English-only pages need no font file. Every needed glyph is checked against
the folder the way the rebuild uses it: only an identical outline counts.

One row per font and embedded version, with one of these statuses:

- ``ok``: every glyph the PDFs let us read matches a file in the folder (the same
  version, or another version whose outlines are identical);
- ``partial``: the folder has the same version and at least ``PARTIAL_MATCH`` of the
  glyphs match: the PDFs were made with another build of that version, which may not
  be obtainable; pages with the other glyphs go to review;
- ``wrong_version``: the folder has a file for the font, but some glyphs match none;
- ``missing``: no file for the font, by name or, for a font whose name the PDF does
  not record, by glyph shapes;
- ``unreadable``: the PDFs' copies of the font cannot be read, so no file could help.

``ok``, ``partial`` and ``unreadable`` never stop a run; the other two stop the
``fonts`` engine (``blocking``). The ``auto`` engine goes on with this machine's
installed files of a blocking font (``installed``), whatever their version.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pdf_ar.fonts import (
    FontLibrary,
    file_version,
    font_key,
    installed_font_index,
    is_arabic_code,
    name_candidates,
    open_font_registry,
    pinned_file_name,
    same_version,
)
from pdf_ar.glyphs import FontMapper, page_xrefs, unnamed, visible_spans
from pdf_ar.pdf_adapter import PdfDocument

BLOCKING = ("missing", "wrong_version")
PARTIAL_MATCH = 0.9
CSV_FIELDS = ("font", "version", "status", "documents", "pages", "glyphs", "matched",
              "unreadable", "files", "matched_by", "hint", "installed", "installed_matched")


@dataclass
class Requirement:
    font: str
    version: str | None
    documents: set[str] = field(default_factory=set)
    pages: int = 0
    glyphs: int = 0
    verified: int = 0
    unreadable: int = 0
    files: set[str] = field(default_factory=set)
    candidates: set[str] = field(default_factory=set)
    by_shape: bool = False
    status: str = ""
    hint: str = ""
    # auto: this machine's files read in place of the missing ones, "path (version)",
    # and how many of the glyphs they match.
    installed: list[str] = field(default_factory=list)
    installed_matched: int | None = None

    @property
    def blocking(self) -> bool:
        return self.status in BLOCKING

    @property
    def checkable(self) -> int:
        return self.glyphs - self.unreadable

    def as_dict(self) -> dict:
        return {"font": self.font, "version": self.version or "", "status": self.status,
                "documents": len(self.documents), "pages": self.pages, "glyphs": self.glyphs,
                "matched": self.verified, "unreadable": self.unreadable,
                "files": ";".join(sorted(self.files or self.candidates)),
                "matched_by": ("shape" if self.by_shape else "name") if self.candidates else "",
                "hint": self.hint, "installed": ";".join(self.installed),
                "installed_matched": "" if self.installed_matched is None
                else self.installed_matched}


def _needs_file(code: int) -> bool:
    return is_arabic_code(code) or code <= 0


def requirements(pdfs: Iterable[Path], library: FontLibrary, root: Path | None = None,
                 pages: set[int] | None = None) -> list[Requirement]:
    """The fonts the PDFs' Arabic glyphs need, per font and version, checked against
    the folder."""
    rows: dict[tuple, Requirement] = {}
    for pdf in pdfs:
        name = str(pdf.relative_to(root) if root else pdf).replace("\\", "/")
        try:
            doc = PdfDocument(pdf)
        except Exception:  # noqa: BLE001 - an unreadable PDF is reported by the extraction
            continue
        with doc:
            mapper = FontMapper(doc, library)
            seen: set[tuple] = set()
            fonts: dict[tuple, str] = {}
            for number in range(1, doc.page_count + 1):
                if pages is not None and number not in pages:
                    continue
                page = doc.page(number)
                xrefs = page_xrefs(page)
                on_page: set[tuple] = set()
                for _index, span, reason in visible_spans(page):
                    font = span["font"].split("+")[-1]
                    if reason or not any(mapper._embedded(x) is not None
                                         for x in xrefs.get(font, [])):
                        continue
                    for code, gid, _origin, _bbox in span["chars"]:
                        if gid < 0 or not _needs_file(code):
                            continue
                        key = (font_key(font), mapper.embedded_version(font, xrefs))
                        row = rows.setdefault(key, Requirement(*key))
                        row.documents.add(name)
                        on_page.add(key)
                        fonts[key] = font
                        if (key, gid) in seen:
                            continue
                        seen.add((key, gid))
                        row.glyphs += 1
                        if not mapper.glyph_outlines(font, gid, xrefs):
                            row.unreadable += 1
                            continue
                        match = mapper.verified(font, gid, xrefs)
                        if match is not None:
                            row.verified += 1
                            row.files.add(Path(match[0].path).name)
                for key in on_page:
                    rows[key].pages += 1
            for key, font in fonts.items():
                files = mapper.candidates(font)
                rows[key].candidates.update(Path(f.path).name for f in files)
                rows[key].by_shape |= font in mapper.shape_matched
    out = sorted(rows.values(), key=lambda r: (r.font, r.version or ""))
    for row in out:
        versions = [file_version(f) for f in library.files()
                    if Path(f.path).name in row.candidates]
        row.status = status_of(row, any(same_version(v, row.version) for v in versions))
    _add_hints([r for r in out if r.blocking], library)
    for row in out:
        if row.status == "partial":
            row.hint = (f"{row.checkable - row.verified} glyphs differ from the same version in "
                        f"the folder; their pages go to review")
    return out


def status_of(row: Requirement, same_version_in_folder: bool) -> str:
    if row.checkable == 0:
        return "unreadable"
    if not row.candidates:
        return "missing"
    if row.verified >= row.checkable:
        return "ok"
    if same_version_in_folder and row.verified >= PARTIAL_MATCH * row.checkable:
        return "partial"
    return "wrong_version"


def _add_hints(rows: list[Requirement], library: FontLibrary) -> None:
    """Where each blocking font can be found: never fetched, only pointed to."""
    if not rows:
        return
    registry = open_font_registry()
    machine = installed_font_index()
    for row in rows:
        version = row.version or "the version the PDFs use"
        hints = []
        if row.status == "wrong_version":
            files = [f for f in library.files() if Path(f.path).name in row.candidates]
            have = ", ".join(sorted({f"{Path(f.path).name} ({file_version(f) or 'no version'})"
                                     for f in files}))
            differ = row.checkable - row.verified
            if any(same_version(file_version(f), row.version) for f in files):
                hints.append(f"the fonts folder has {have}: the same version, but {differ} "
                             f"glyphs differ, so the PDFs were made with another build of "
                             f"version {version}; add that build")
            else:
                hints.append(f"the fonts folder has {have}, whose glyphs differ in {differ} "
                             f"places; add version {version}")
        if unnamed(row.font):
            hints.append("the PDF does not record this font's name; a file is matched to it "
                         "by glyph shapes")
        else:
            local = name_candidates(machine, row.font)
            same = [f.path for f in local if same_version(file_version(f), row.version)]
            if same:
                hints.append(f"on this machine: {same[0]}")
            elif local:
                versions = sorted({file_version(f) or "?" for f in local})
                hints.append(f"this machine has version {', '.join(versions)}, not {version}")
            pinned = registry.get(row.font) or registry.get(row.font.split(",", 1)[0])
            for entry in pinned or []:
                where = f"open font ({entry['license']}): {entry['url']}"
                if "instance" in entry:
                    axes = " ".join(f"{k}={v}" for k, v in entry["instance"].items())
                    where += (f", then build {pinned_file_name(entry)} from {entry['member']}: "
                              f"fonttools varLib.instancer \"{Path(entry['member']).name}\" "
                              f"{axes} -o {pinned_file_name(entry)}")
                hints.append(where)
            if not same and not pinned:
                hints.append(f"obtain {row.font} version {version} from its vendor or a "
                             f"machine that has it")
        row.hint = "; ".join(hints)


def write_report(rows: list[Requirement], folder: Path, fonts_dir: Path) -> None:
    """``fonts_report.csv`` (one row per font and version) and ``fonts_report.json``
    (the same, with the documents of each row)."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "fonts_report.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, CSV_FIELDS)
        writer.writeheader()
        writer.writerows(r.as_dict() for r in rows)
    (folder / "fonts_report.json").write_text(json.dumps({
        "fonts_dir": str(fonts_dir), "blocking": sum(r.blocking for r in rows),
        "fonts": [{**r.as_dict(), "documents": sorted(r.documents)} for r in rows],
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def table(rows: list[Requirement]) -> str:
    """The requirements as a text table, blocking rows first, with what to add."""
    ordered = sorted(rows, key=lambda r: (not r.blocking, r.font, r.version or ""))
    lines = [f"{'font':32} {'version':9} {'status':13} {'docs':>5} {'pages':>6} "
             f"{'glyphs matched':>15}  files"]
    for row in ordered:
        matched = f"{row.verified}/{row.checkable}"
        files = ", ".join(sorted(row.files or row.candidates)) or "-"
        if row.by_shape:
            files += " (by glyph shapes)"
        lines.append(f"{row.font:32} {row.version or '?':9} {row.status:13} "
                     f"{len(row.documents):5d} {row.pages:6d} {matched:>15}  {files}")
    blocking = [r for r in ordered if r.blocking]
    if blocking:
        lines += ["", "To add to the fonts folder:"]
        for row in blocking:
            lines.append(f"- {row.font} {row.version or '?'} ({row.status}): {row.hint}")
            if row.installed:
                line = (f"  meanwhile auto reads this machine's {', '.join(row.installed)}: "
                        f"{row.installed_matched}/{row.checkable} glyphs match")
                if row.installed_matched == row.checkable:
                    line += "; copying them into the fonts folder works too"
                lines.append(line)
    return "\n".join(lines)
