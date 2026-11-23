"""The only module that talks to the PDF library.

Everything else works on plain Python values (tuples, dicts, bytes), so PyMuPDF
(AGPL or commercial licence) can be replaced by another library that exposes
glyph IDs, glyph positions, vector drawings and embedded font programs.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import pymupdf

pymupdf.TOOLS.mupdf_display_errors(False)

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class Drawing:
    """One vector path: its bounding box, whether it is closed or filled, and its items.

    Items are ``("re", box)``, ``("qu", box)`` or ``("l", (x0, y0), (x1, y1))``.
    """

    rect: Box
    closed: bool
    filled: bool
    items: tuple
    stroked: bool = True
    seqno: int = -1  # painting order, comparable with text spans' ``seqno``
    opaque_fill: bool = False  # filled with full opacity


class PdfPage:
    def __init__(self, page: pymupdf.Page) -> None:
        self._page = page
        self.number = page.number + 1
        self.width = page.rect.width
        self.height = page.rect.height

    def spans(self) -> list[dict]:
        """Glyph traces: per span its font, size, direction, type, opacity and chars.

        Each char is ``(code, glyph_id, origin, bbox)``; a glyph ID of -1 marks an
        extra character of the previous glyph (the second letter of a ligature).
        ``seqno`` gives the span's painting order.
        """
        return self._page.get_texttrace()

    def fonts(self) -> list[tuple[int, str, bool]]:
        """(xref, base font name, embedded) of every font the page uses."""
        out = []
        for xref, ext, _type, basefont, *_ in self._page.get_fonts(full=True):
            out.append((xref, basefont, ext not in ("", "n/a")))
        return out

    @cached_property
    def drawings(self) -> list[Drawing]:
        out = []
        for path in self._page.get_drawings():
            items = []
            for item in path["items"]:
                kind = item[0]
                if kind == "re":
                    items.append(("re", tuple(item[1])))
                elif kind == "qu":
                    items.append(("qu", tuple(item[1].rect)))
                elif kind == "l":
                    items.append(("l", (item[1].x, item[1].y), (item[2].x, item[2].y)))
            out.append(Drawing(rect=tuple(path["rect"]), closed=bool(path.get("closePath")),
                               filled=path.get("fill") is not None, items=tuple(items),
                               stroked="s" in (path.get("type") or "s"),
                               seqno=path.get("seqno", -1),
                               opaque_fill=path.get("fill") is not None
                               and (path.get("fill_opacity") or 1.0) >= 0.99))
        return out

    @cached_property
    def text_blocks(self) -> list[Box]:
        return [tuple(b["bbox"]) for b in self._page.get_text("dict")["blocks"]
                if b.get("type") == 0]

    def has_images(self) -> bool:
        return bool(self._page.get_images(full=False))

    def render_rgb(self, dpi: int) -> tuple[int, int, bytes]:
        pixmap = self._page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
        return pixmap.width, pixmap.height, pixmap.samples


class PdfDocument:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._doc = pymupdf.open(stream=path.read_bytes(), filetype="pdf")
        self.page_count = self._doc.page_count
        self.producer = (self._doc.metadata or {}).get("producer") or ""

    def page(self, number: int) -> PdfPage:
        return PdfPage(self._doc[number - 1])

    def font_program(self, xref: int) -> bytes | None:
        """The embedded font file (often a subset), or None if the font is not embedded."""
        try:
            buffer = self._doc.extract_font(xref)[3]
        except Exception:  # noqa: BLE001 - unreadable font objects count as not embedded
            return None
        return buffer or None

    def close(self) -> None:
        self._doc.close()

    def __enter__(self) -> PdfDocument:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
