"""Tesseract OCR engine: page text from the rendered page image.

Used as an extraction engine of its own (``--engine ocr``), as ``auto``'s last
fallback, or next to another engine (``--ocr-check``), where the share of its words
that OCR also reads ranks pages for review. Requires ``pytesseract``, Pillow and a Tesseract
installation with the requested language models.
"""

from __future__ import annotations

import os
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pdf_ar.pdf_adapter import PdfPage

STRIP = ".,:;()[]/،؛؟!\"'«»-–▪•"


OCR_MODELS = ("fast", "best")


def default_tessdata_root() -> Path:
    """Folder holding one Tesseract model folder per model: ``fast`` and ``best``."""
    configured = os.environ.get("PDF_EXTRACT_TESSDATA")
    if configured:
        return Path(configured)
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "pdftext-tools" / "tessdata"


@dataclass(frozen=True)
class OcrOptions:
    tesseract_cmd: str | None = None
    tessdata_dir: str | None = None  # an exact model folder; overrides ``model``
    lang: str = "ara+eng"
    dpi: int = 300
    psm: int = 3
    min_confidence: float = 0.75
    # tessdata_fast is quicker; tessdata_best is slower and slightly more accurate.
    model: str = "fast"

    def __post_init__(self) -> None:
        if self.model not in OCR_MODELS:
            raise ValueError(f"OCR model must be one of {', '.join(OCR_MODELS)}")

    def model_dir(self) -> str | None:
        if self.tessdata_dir:
            return self.tessdata_dir
        folder = default_tessdata_root() / self.model
        return folder.as_posix() if folder.is_dir() else None


@dataclass
class OcrPage:
    paragraphs: str
    lines: str
    confidence: float | None  # mean word confidence, 0..1
    words: int


def assemble(data: dict[str, list]) -> OcrPage:
    """Paragraph and line text from Tesseract's ``image_to_data`` dictionary output."""
    paragraphs: dict[tuple, dict[int, list[str]]] = {}
    confidences: list[float] = []
    for index, text in enumerate(data["text"]):
        text = (text or "").strip()
        if not text:
            continue
        key = (data["page_num"][index], data["block_num"][index], data["par_num"][index])
        paragraphs.setdefault(key, {}).setdefault(data["line_num"][index], []).append(text)
        confidence = float(data["conf"][index])
        if confidence >= 0:
            confidences.append(confidence / 100)
    blocks = [[" ".join(words) for _n, words in sorted(lines.items())]
              for _k, lines in sorted(paragraphs.items())]
    return OcrPage(
        paragraphs="\n\n".join(" ".join(lines) for lines in blocks),
        lines="\n\n".join("\n".join(lines) for lines in blocks),
        confidence=round(sum(confidences) / len(confidences), 4) if confidences else None,
        words=len(confidences),
    )


def ocr_page(page: PdfPage, options: OcrOptions) -> OcrPage:
    import pytesseract
    from PIL import Image

    if options.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = options.tesseract_cmd
    width, height, samples = page.render_rgb(options.dpi)
    image = Image.frombytes("RGB", (width, height), samples)
    config = f"--psm {options.psm}"
    model_dir = options.model_dir()
    if model_dir:
        config += f" --tessdata-dir {model_dir}"
    data = pytesseract.image_to_data(image, lang=options.lang, config=config,
                                     output_type=pytesseract.Output.DICT)
    return assemble(data)


def _words(text: str) -> Counter:
    out: Counter = Counter()
    for token in text.split():
        token = unicodedata.normalize("NFKC", token)
        token = "".join(c for c in token if not unicodedata.category(c).startswith("M"))
        token = token.replace("\u0640", "").strip(STRIP)
        if token:
            out[token] += 1
    return out


def agreement(rebuilt: str, ocr: str) -> float | None:
    """Share of rebuilt words (as a multiset) that OCR also produced."""
    ours = _words(rebuilt)
    total = sum(ours.values())
    return round(sum((ours & _words(ocr)).values()) / total, 4) if total else None
