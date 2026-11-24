"""Command line: ``pdf-ar fonts | extract | report``."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
from pathlib import Path

from pdf_ar import __version__
from pdf_ar.extract import (
    ENGINES,
    MissingFontsError,
    Options,
    check_fonts,
    find_pdfs,
    parse_pages,
    run,
)
from pdf_ar.ocr import OCR_MODELS, OcrOptions
from pdf_ar.required_fonts import table

EXIT_MISSING_FONTS = 2


def _font_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", type=Path, help="a PDF file or a folder searched recursively")
    parser.add_argument("--fonts-dir", type=Path, default=Path("fonts"),
                        help="folder that holds the full font files you add (default: ./fonts); "
                             "fonts are never downloaded or copied into it")
    parser.add_argument("--out", type=Path, default=Path("artifacts/pdf-ar"),
                        help="output folder (default: artifacts/pdf-ar)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf-ar",
        description="Arabic and English text extraction from PDFs: the PDF's own text, a "
                    "deterministic rebuild from glyph IDs and the fonts' shaping tables, or "
                    "Tesseract OCR, chosen per page by auto. No language model is used.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    fonts = commands.add_parser("fonts", help="list the fonts and versions the PDFs need and "
                                              "whether the fonts folder has them; writes "
                                              "fonts_report.csv and .json")
    _font_args(fonts)

    extract = commands.add_parser("extract", help="extract text and write outputs")
    _font_args(extract)
    extract.add_argument("--engine", choices=ENGINES, default="fonts",
                         help="auto: per page, native if its check passes, else fonts, else "
                              "ocr, else review; never stops for missing fonts, and reads "
                              "this machine's installed versions of fonts the folder lacks; "
                              "native: the PDF's own text map, no fonts; "
                              "fonts: Arabic rebuilt from glyph IDs and font tables (default); "
                              "ocr: Tesseract on the rendered page")
    extract.add_argument("--ocr-check", action="store_true",
                         help="also run OCR on every page and record its word agreement; "
                              "OCR text is written next to the page text, which is unchanged")
    extract.add_argument("--font-check", action="store_true",
                         help="engines auto and native: also rebuild pages native accepted; "
                              "where the two differ, keep the rebuild (native_disagrees)")
    extract.add_argument("--format", default="txt",
                         help="document outputs, comma-separated: txt (plain text), md "
                              "(Markdown with tables), json (pages and blocks with positions); "
                              "default txt. Page files and report.json are always written")
    extract.add_argument("--pages", help="pages to extract, e.g. 1,3-5 (default: all)")
    ocr = extract.add_argument_group("OCR (engines ocr and auto, --ocr-check)")
    ocr.add_argument("--tesseract-cmd", help="path to tesseract.exe")
    ocr.add_argument("--ocr-model", choices=OCR_MODELS, default="fast",
                     help="fast: tessdata_fast (quicker); best: tessdata_best (slower, slightly "
                          "more accurate). Read from <tessdata root>/<model>; the root is "
                          "PDF_EXTRACT_TESSDATA or %%LOCALAPPDATA%%/pdftext-tools/tessdata")
    ocr.add_argument("--tessdata-dir",
                     help="exact Tesseract model folder, overriding --ocr-model (no spaces)")
    ocr.add_argument("--ocr-lang", default="ara+eng")
    ocr.add_argument("--ocr-dpi", type=int, default=300)
    ocr.add_argument("--ocr-psm", type=int, default=3, help="Tesseract page segmentation mode")
    ocr.add_argument("--min-ocr-confidence", type=float, default=0.75,
                     help="engine ocr: pages with a lower mean word confidence (0-1) go to "
                          "review")

    report = commands.add_parser("report", help="summarize an output folder")
    report.add_argument("out", type=Path)
    return parser


def _missing(error: MissingFontsError, out: Path) -> int:
    print(f"\n{len(error.rows)} needed font(s) are missing or a different version. Add them "
          f"to {error.fonts_dir} and run again; nothing was extracted. Full table: "
          f"{out / 'fonts_report.csv'}", file=sys.stderr)
    return EXIT_MISSING_FONTS


def _cmd_fonts(args) -> int:
    root = args.path if args.path.is_dir() else args.path.parent
    options = Options(out_dir=args.out, fonts_dir=args.fonts_dir)
    try:
        _library, rows = check_fonts(find_pdfs(args.path), options, root, args.out)
    except MissingFontsError as error:
        print(table(error.all_rows), flush=True)
        return _missing(error, args.out)
    print(table(rows))
    print(f"\nEvery needed font is in {args.fonts_dir}.")
    return 0


def _cmd_extract(args) -> int:
    ocr = OcrOptions(args.tesseract_cmd, args.tessdata_dir, args.ocr_lang, args.ocr_dpi,
                     args.ocr_psm, args.min_ocr_confidence, args.ocr_model)
    formats = tuple(dict.fromkeys(f.strip() for f in args.format.split(",") if f.strip()))
    options = Options(out_dir=args.out, fonts_dir=args.fonts_dir, engine=args.engine,
                      pages=parse_pages(args.pages), ocr=ocr, formats=formats,
                      ocr_check=args.ocr_check, font_check=args.font_check)
    try:
        result = run(args.path, options, progress=lambda line: print(line, file=sys.stderr))
    except MissingFontsError as error:
        print(table(error.all_rows), flush=True)
        return _missing(error, args.out)
    blocking = [r for r in result.fonts if r.blocking]
    if blocking:
        print(table(result.fonts))
        print(f"\n{len(blocking)} needed font(s) are not in {args.fonts_dir}. auto read this "
              "machine's installed versions where it had them; pages with glyphs those "
              "do not match went to OCR, or to review. Add the fonts above to "
              f"{args.fonts_dir} for the full rebuild. Full table: "
              f"{args.out / 'fonts_report.csv'}\n")
    print(f"{len(result.documents)} documents, {result.pages} pages, "
          f"{result.review_pages} pages for review. Output: {args.out}")
    return 0


def _cmd_report(args) -> int:
    with (args.out / "summary.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    pages = sum(int(r["pages"]) for r in rows)
    review = sum(int(r["review_pages"]) for r in rows)
    reasons: Counter = Counter()
    for row in rows:
        for item in filter(None, row["reasons"].split(";")):
            name, count = item.rsplit(":", 1)
            reasons[name] += int(count)
    print(f"documents {len(rows)}  pages {pages}  ok {pages - review}  review {review}")
    print("pages per reason:")
    for name, count in reasons.most_common():
        print(f"  {name:22} {count}")
    worst = sorted((r for r in rows if int(r["review_pages"])),
                   key=lambda r: -int(r["review_pages"]) / max(1, int(r["pages"])))[:20]
    if worst:
        print("documents with the largest share of review pages:")
        for row in worst:
            print(f"  {row['review_pages']:>4}/{row['pages']:<4} {row['source']}  {row['reasons']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    args = _parser().parse_args(argv)
    handler = {"fonts": _cmd_fonts, "extract": _cmd_extract, "report": _cmd_report}[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
