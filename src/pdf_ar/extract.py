"""Run an extraction engine over one PDF or a folder of PDFs and write text plus reports.

Engines:

- ``native``: the PDF's own text map, laid out by the same pipeline as the rebuild;
- ``fonts``: Arabic letters rebuilt from glyph IDs and the font files' tables;
- ``ocr``: Tesseract on the rendered page;
- ``auto``: per page, native, else fonts, else OCR, else review.

``ocr_check`` writes OCR text next to any engine's text, with their word agreement.
``font_check`` also rebuilds pages that native accepted and, where the two differ,
keeps the verified rebuild.

Output layout, per document (mirroring the input folder structure)::

    <out>/<relative folder>/<stem>/<stem>.txt        paragraph text, pages separated by \\f
    <out>/<relative folder>/<stem>/<stem>.ocr.txt    OCR text (ocr_check)
    <out>/<relative folder>/<stem>/pages/page-001.txt
    <out>/<relative folder>/<stem>/pages/page-001.lines.txt
    <out>/<relative folder>/<stem>/pages/page-001.ocr.txt, page-001.ocr.lines.txt (ocr_check)
    <out>/<relative folder>/<stem>/report.json
    <out>/summary.csv, <out>/fonts_report.csv, <out>/fonts_report.json, <out>/run.json

Engines that rebuild from fonts (``fonts``, ``auto``, ``native`` with ``font_check``)
first check every needed font against the fonts folder. ``fonts`` and ``native``
stop before extracting anything when one is missing or a different version
(``MissingFontsError``). ``auto`` goes on, reading this machine's installed files
of those fonts whatever their version: glyphs whose outlines differ stay
unverified, so their pages fall back to OCR.

Text files are UTF-8 with ``\\n`` line endings on every platform.
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pdf_ar import __version__
from pdf_ar.fonts import FontLibrary, file_version, font_key
from pdf_ar.formats import FORMATS, PageOutput, blocks_from_text, to_json, to_markdown
from pdf_ar.glyphs import FontMapper
from pdf_ar.layout import Block, rebuild
from pdf_ar.ocr import OcrOptions, OcrPage, agreement, ocr_page
from pdf_ar.pdf_adapter import PdfDocument, PdfPage
from pdf_ar.qc import PageReport, check, check_native, check_ocr, failed
from pdf_ar.required_fonts import Requirement, requirements, write_report

ENGINES = ("auto", "native", "fonts", "ocr")
SUMMARY_FIELDS = ("source", "engine", "status", "pages", "ok_pages", "review_pages", "reasons",
                  "arabic_words", "words", "seconds", "min_ocr_agreement", "mean_ocr_confidence",
                  "page_sources")


class MissingFontsError(RuntimeError):
    """Needed fonts are missing from the fonts folder, or are a different version."""

    def __init__(self, rows: list[Requirement], fonts_dir: Path,
                 all_rows: list[Requirement] | None = None) -> None:
        self.rows = rows
        self.all_rows = all_rows or rows
        self.fonts_dir = fonts_dir
        names = ", ".join(f"{r.font} {r.version or '?'} ({r.status})" for r in rows)
        super().__init__(f"{len(rows)} needed font(s) not in {fonts_dir}: {names}. Add them "
                         "and run again; nothing was extracted.")


@dataclass
class Options:
    out_dir: Path
    fonts_dir: Path
    engine: str = "fonts"
    pages: set[int] | None = None
    ocr: OcrOptions = field(default_factory=OcrOptions)
    formats: tuple[str, ...] = ("txt",)
    ocr_check: bool = False
    font_check: bool = False

    def __post_init__(self) -> None:
        if self.engine not in ENGINES:
            raise ValueError(f"engine must be one of {', '.join(ENGINES)}, not {self.engine!r}")
        unknown = set(self.formats) - set(FORMATS)
        if unknown or not self.formats:
            raise ValueError(f"formats must be some of {', '.join(FORMATS)}")

    @property
    def base(self) -> str:
        """The engine that produces the text."""
        return self.engine

    @property
    def uses_fonts(self) -> bool:
        return self.base in ("fonts", "auto") or (self.font_check and self.base == "native")

    @property
    def uses_ocr(self) -> bool:
        return self.base in ("ocr", "auto") or self.ocr_check


@dataclass
class RunResult:
    documents: list[dict] = field(default_factory=list)
    fonts: list[Requirement] = field(default_factory=list)

    @property
    def pages(self) -> int:
        return sum(d["pages"] for d in self.documents)

    @property
    def review_pages(self) -> int:
        return sum(d["review_pages"] for d in self.documents)


def find_pdfs(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")


def parse_pages(spec: str | None) -> set[int] | None:
    """'1,3-5' -> {1, 3, 4, 5}."""
    if not spec:
        return None
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            low, high = part.split("-", 1)
            pages.update(range(int(low), int(high) + 1))
        elif part:
            pages.add(int(part))
    return pages


def _safe(name: str) -> str:
    """A folder name Windows can create: no trailing spaces or dots."""
    return name.rstrip(" .") or "_"


def output_folder(out_dir: Path, relative: Path) -> Path:
    """Where a PDF's outputs go: its relative folders and stem, made safe for Windows."""
    return out_dir.joinpath(*(_safe(part) for part in relative.parent.parts),
                            _safe(relative.stem))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def check_fonts(pdfs: Iterable[Path], options: Options, root: Path | None = None,
                report_dir: Path | None = None,
                strict: bool = True) -> tuple[FontLibrary, list[Requirement]]:
    """The fonts folder checked against every font the PDFs need.

    When one is missing or a different version, ``strict`` raises ``MissingFontsError``;
    otherwise the library also reads this machine's installed files of that font, and
    the rows record how many of the font's glyphs those files match.
    """
    pdfs = list(pdfs)
    library = FontLibrary(options.fonts_dir)
    rows = requirements(pdfs, library, root, options.pages)
    blocking = [r for r in rows if r.blocking]
    if not strict and blocking:
        for row in blocking:
            row.installed = [f"{f.path} ({file_version(f) or 'no version'})"
                             for f in library.use_installed(row.font)]
        if any(row.installed for row in blocking):
            matched = {(r.font, r.version): r.verified
                       for r in requirements(pdfs, library, root, options.pages)}
            for row in blocking:
                if row.installed:
                    row.installed_matched = matched.get((row.font, row.version), 0)
    if report_dir is not None:
        write_report(rows, report_dir, options.fonts_dir)
    if blocking and strict:
        raise MissingFontsError(blocking, options.fonts_dir, rows)
    return library, rows


def _page_sources(documents: list[dict]) -> dict[str, int]:
    """Pages per engine that produced their text, over the run."""
    total: Counter = Counter()
    for document in documents:
        for item in filter(None, document.get("page_sources", "").split(";")):
            name, count = item.rsplit(":", 1)
            total[name] += int(count)
    return dict(sorted(total.items()))


def run(path: Path, options: Options,
        progress: Callable[[str], None] | None = None) -> RunResult:
    pdfs = find_pdfs(path)
    root = path if path.is_dir() else path.parent
    options.out_dir.mkdir(parents=True, exist_ok=True)
    library: FontLibrary | None = None
    rows: list[Requirement] = []
    if options.uses_fonts:
        library, rows = check_fonts(pdfs, options, root, options.out_dir,
                                    strict=options.base != "auto")
    result = RunResult(fonts=rows)
    started = time.perf_counter()
    for index, pdf in enumerate(pdfs, 1):
        if progress:
            progress(f"[{index}/{len(pdfs)}] {pdf.relative_to(root)}")
        try:
            result.documents.append(extract_document(pdf, root, library, options))
        except Exception as error:  # noqa: BLE001 - one unreadable document must not stop a run
            result.documents.append({
                "source": str(pdf.relative_to(root)).replace("\\", "/"),
                "engine": options.engine, "status": "error", "pages": 0, "ok_pages": 0,
                "review_pages": 0, "reasons": f"error:{type(error).__name__}",
                "arabic_words": 0, "words": 0, "seconds": 0, "min_ocr_agreement": "",
                "mean_ocr_confidence": ""})
    with (options.out_dir / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(result.documents)
    _write(options.out_dir / "run.json", json.dumps({
        "tool": "pdf-ar", "version": __version__, "engine": options.engine,
        "input": str(path), "documents": len(pdfs), "pages": result.pages,
        "review_pages": result.review_pages, "seconds": round(time.perf_counter() - started, 3),
        "page_sources": _page_sources(result.documents),
        "fonts_folder": library.describe() if library is not None else None,
        "options": {"pages": sorted(options.pages) if options.pages else None,
                    "formats": list(options.formats),
                    "ocr_check": options.ocr_check, "font_check": options.font_check,
                    "ocr": asdict(options.ocr) if options.uses_ocr else None},
    }, indent=2, ensure_ascii=False) + "\n")
    return result


PageResult = tuple[PageReport, str, str, list[Block]]


def _fonts_page(page: PdfPage, number: int, mapper: FontMapper, has_images: bool) -> PageResult:
    try:
        text = rebuild(page, mapper)
    except Exception as error:  # noqa: BLE001 - one bad page must not stop the run
        return failed(number, error, has_images), "", "", []
    report = check(number, text, has_images)
    report.source = "fonts"
    return report, text.paragraphs, text.lines, text.blocks


def _native_page(page: PdfPage, number: int, mapper: FontMapper, has_images: bool) -> PageResult:
    try:
        text = rebuild(page, mapper, text_source="pdf")
    except Exception as error:  # noqa: BLE001
        return failed(number, error, has_images, engine="native"), "", "", []
    report = check_native(number, text, has_images)
    report.source = "native"
    return report, text.paragraphs, text.lines, text.blocks


def _ocr_page(page: PdfPage, options: Options) -> tuple[OcrPage | None, str | None]:
    try:
        return ocr_page(page, options.ocr), None
    except Exception as error:  # noqa: BLE001 - reported on the page, never fatal
        return None, f"ocr: {type(error).__name__}: {error}"


def _ocr_result(page: PdfPage, number: int, ocr: OcrPage | None, error: str | None,
                has_images: bool, options: Options) -> PageResult:
    if ocr is None:
        return failed(number, RuntimeError(error), has_images, engine="ocr"), "", "", []
    glyphs = sum(len(span["chars"]) for span in page.spans())
    report = check_ocr(number, ocr.paragraphs, ocr.confidence, glyphs, has_images,
                       options.ocr.min_confidence)
    report.source = "ocr"
    return report, ocr.paragraphs, ocr.lines, blocks_from_text(ocr.paragraphs)


def _why(report: PageReport) -> str:
    return f"{report.source or report.engine}: {','.join(report.reasons) or 'ok'}"


def _page(page: PdfPage, number: int, mapper: FontMapper, options: Options,
          has_images: bool) -> tuple[PageResult, OcrPage | None]:
    """The page's text from the chosen engine; for ``auto``, the first engine that passes.

    Also returns the OCR reading when one was made, for ``ocr_check``.
    """
    ocr: OcrPage | None = None
    if options.base == "ocr":
        ocr, error = _ocr_page(page, options)
        return _ocr_result(page, number, ocr, error, has_images, options), ocr
    if options.base == "fonts":
        return _fonts_page(page, number, mapper, has_images), None
    result = _native_page(page, number, mapper, has_images)
    if result[0].status == "ok":
        if options.font_check and mapper.library is not None:
            rebuilt = _fonts_page(page, number, mapper, has_images)
            if rebuilt[0].status == "ok":
                result[0].native_disagrees = rebuilt[1] != result[1]
                if result[0].native_disagrees:
                    rebuilt[0].native_disagrees = True
                    rebuilt[0].tried = ["native: ok, differs from the rebuild"]
                    return rebuilt, None
        return result, None
    if options.base == "native":
        return result, None
    tried = [_why(result[0])]
    rebuilt = _fonts_page(page, number, mapper, has_images)
    if rebuilt[0].status == "ok":
        rebuilt[0].tried = tried
        return rebuilt, None
    tried.append(_why(rebuilt[0]))
    ocr, error = _ocr_page(page, options)
    read = _ocr_result(page, number, ocr, error, has_images, options)
    if read[0].status == "ok":
        read[0].tried = tried
        return read, ocr
    tried.append(_why(read[0]) if ocr is not None else f"ocr: unavailable ({error})")
    rebuilt[0].tried = tried
    return rebuilt, ocr


def extract_document(pdf: Path, root: Path, library: FontLibrary | None,
                     options: Options) -> dict:
    relative = pdf.relative_to(root)
    target = output_folder(options.out_dir, relative)
    reports: list[PageReport] = []
    texts: list[str] = []
    ocr_texts: list[str] = []
    outputs: list[PageOutput] = []
    with PdfDocument(pdf) as doc:
        mapper = FontMapper(doc, library)
        numbers = [n for n in range(1, doc.page_count + 1)
                   if options.pages is None or n in options.pages]
        fonts_used: set[str] = set()
        for number in numbers:
            page = doc.page(number)
            fonts_used.update(name.split("+")[-1] for _x, name, _e in page.fonts())
            has_images = page.has_images()
            started = time.perf_counter()
            (report, paragraphs, lines, blocks), ocr = _page(page, number, mapper, options,
                                                             has_images)
            report.seconds = round(time.perf_counter() - started, 3)
            if options.ocr_check and options.base != "ocr":
                if ocr is None:
                    ocr_started = time.perf_counter()
                    ocr, error = _ocr_page(page, options)
                    report.ocr_seconds = round(time.perf_counter() - ocr_started, 3)
                    if ocr is None:
                        report.error = error
                if ocr is not None:
                    report.ocr_confidence = ocr.confidence
                    report.ocr_agreement = agreement(paragraphs, ocr.paragraphs)
                    _write(target / "pages" / f"page-{number:03d}.ocr.txt",
                           ocr.paragraphs + "\n")
                    _write(target / "pages" / f"page-{number:03d}.ocr.lines.txt",
                           ocr.lines + "\n")
                ocr_texts.append(ocr.paragraphs if ocr else "")
            _write(target / "pages" / f"page-{number:03d}.txt", paragraphs + "\n")
            _write(target / "pages" / f"page-{number:03d}.lines.txt", lines + "\n")
            reports.append(report)
            texts.append(paragraphs)
            outputs.append(PageOutput(number, report.status, report.reasons, paragraphs,
                                      blocks))
        producer, page_count = doc.producer, doc.page_count
        fonts = {font_key(name): sorted(Path(f.path).name for f in mapper.candidates(name))
                 for name in sorted(fonts_used)} if library is not None else {}
    if "txt" in options.formats:
        _write(target / f"{target.name}.txt", "\n\f\n".join(texts) + "\n")
        if options.ocr_check and options.base != "ocr":
            _write(target / f"{target.name}.ocr.txt", "\n\f\n".join(ocr_texts) + "\n")
    if "md" in options.formats:
        _write(target / f"{target.name}.md", to_markdown(outputs))
    reasons: Counter = Counter(r for report in reports for r in report.reasons)
    status = "ok" if all(r.status == "ok" for r in reports) else "review"
    seconds = round(sum(r.seconds for r in reports), 3)
    document = {
        "tool": "pdf-ar", "version": __version__, "engine": options.engine,
        "source": str(relative).replace("\\", "/"),
        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "producer": producer,
        "page_count": page_count, "status": status, "seconds": seconds, "fonts": fonts,
    }
    _write(target / "report.json", json.dumps(
        {**document, "pages": [r.as_dict() for r in reports]},
        indent=2, ensure_ascii=False) + "\n")
    if "json" in options.formats:
        _write(target / f"{target.name}.json", json.dumps(
            to_json(document, outputs), indent=2, ensure_ascii=False) + "\n")
    agreements = [r.ocr_agreement for r in reports if r.ocr_agreement is not None]
    confidences = [r.ocr_confidence for r in reports if r.ocr_confidence is not None]
    return {
        "source": str(relative).replace("\\", "/"), "engine": options.engine, "status": status,
        "pages": len(reports), "ok_pages": sum(r.status == "ok" for r in reports),
        "review_pages": sum(r.status == "review" for r in reports),
        "reasons": ";".join(f"{k}:{v}" for k, v in sorted(reasons.items())),
        "arabic_words": sum(r.arabic_words for r in reports),
        "words": sum(r.words for r in reports), "seconds": seconds,
        "min_ocr_agreement": round(min(agreements), 4) if agreements else "",
        "mean_ocr_confidence": round(sum(confidences) / len(confidences), 4)
        if confidences else "",
        "page_sources": ";".join(f"{k}:{v}" for k, v in sorted(
            Counter(r.source or r.engine for r in reports).items())),
    }
