"""pdf-ar engine chain (auto, native) and the two checks."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pymupdf")
pytest.importorskip("fontTools")

from pdf_ar import extract  # noqa: E402
from pdf_ar.extract import Options  # noqa: E402
from pdf_ar.glyphs import FontMapper  # noqa: E402
from pdf_ar.layout import PageText, rebuild  # noqa: E402
from pdf_ar.pdf_adapter import PdfDocument  # noqa: E402
from pdf_ar.qc import PageReport, arabic_fragment_ratio, check_native  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "arabic_ligatures.pdf"


def result(source: str, status: str, text: str, reasons=()):
    report = PageReport(page=1, engine=source, status=status, reasons=list(reasons),
                        source=source)
    return report, text, text, []


@pytest.fixture
def chain(monkeypatch, tmp_path):
    """Run ``extract._page`` with each engine's outcome set by the test."""

    def run(outcomes: dict, **options):
        calls = Counter()

        def step(name):
            def fake(*_args, **_kwargs):
                calls[name] += 1
                return outcomes[name]
            return fake

        monkeypatch.setattr(extract, "_native_page", step("native"))
        monkeypatch.setattr(extract, "_fonts_page", step("fonts"))
        monkeypatch.setattr(extract, "_ocr_page", lambda *_a: (
            (SimpleNamespace(paragraphs="ocr text", lines="ocr text", confidence=0.9), None)
            if "ocr" in outcomes else (None, "ocr: not installed")))
        monkeypatch.setattr(extract, "_ocr_result", lambda *_a: (
            calls.update(["ocr"]) or outcomes.get("ocr", result("ocr", "review", "", ["error"]))))
        opts = Options(out_dir=tmp_path, fonts_dir=tmp_path, **options)
        mapper = SimpleNamespace(library=object())
        (report, text, _lines, _blocks), _ocr = extract._page(None, 1, mapper, opts, False)
        return report, text, calls

    return run


def test_auto_takes_native_when_its_check_passes(chain):
    report, text, calls = chain({"native": result("native", "ok", "native text")},
                                engine="auto")
    assert report.source == "native" and text == "native text"
    assert calls == Counter(native=1)


def test_auto_falls_back_to_fonts_then_ocr(chain):
    native = result("native", "review", "broken", ["split_words"])
    fonts = result("fonts", "ok", "rebuilt")
    report, text, calls = chain({"native": native, "fonts": fonts}, engine="auto")
    assert report.source == "fonts" and text == "rebuilt"
    assert report.tried == ["native: split_words"]

    missing = result("fonts", "review", "", ["missing_font"])
    report, text, _ = chain({"native": native, "fonts": missing,
                             "ocr": result("ocr", "ok", "ocr text")}, engine="auto")
    assert report.source == "ocr" and text == "ocr text"
    assert report.tried == ["native: split_words", "fonts: missing_font"]


def test_auto_sends_the_page_to_review_when_every_engine_fails(chain):
    report, _, _ = chain({"native": result("native", "review", "", ["split_words"]),
                          "fonts": result("fonts", "review", "", ["missing_font"])},
                         engine="auto")
    assert report.status == "review" and report.source == "fonts"
    assert report.tried[-1].startswith("ocr: unavailable")


def test_native_engine_never_falls_back(chain):
    report, _, calls = chain({"native": result("native", "review", "", ["split_words"])},
                             engine="native")
    assert report.status == "review" and calls == Counter(native=1)


def test_font_check_keeps_the_rebuild_when_native_differs(chain):
    report, text, _ = chain({"native": result("native", "ok", "native text"),
                             "fonts": result("fonts", "ok", "rebuilt text")},
                            engine="auto", font_check=True)
    assert report.source == "fonts" and text == "rebuilt text" and report.native_disagrees

    report, text, _ = chain({"native": result("native", "ok", "same"),
                             "fonts": result("fonts", "ok", "same")},
                            engine="auto", font_check=True)
    assert report.source == "native" and report.native_disagrees is False


def test_engine_options(tmp_path):
    with pytest.raises(ValueError):
        Options(out_dir=tmp_path, fonts_dir=tmp_path, engine="both")
    fonts = Options(out_dir=tmp_path, fonts_dir=tmp_path, engine="fonts", ocr_check=True)
    assert fonts.uses_ocr and fonts.uses_fonts
    auto = Options(out_dir=tmp_path, fonts_dir=tmp_path, engine="auto")
    assert auto.uses_fonts and auto.uses_ocr


def test_native_text_source_reads_only_the_pdf_map():
    with PdfDocument(FIXTURE) as doc:
        text = rebuild(doc.page(1), FontMapper(doc, None), text_source="pdf")
    assert text.glyphs and set(text.counts) <= {"pdf", "unmapped", "hidden", "off_page"}
    with pytest.raises(ValueError):
        rebuild(None, None, text_source="llm")


def test_split_words_send_native_arabic_to_the_rebuild():
    words = "تعمل الشركة على تحسين جودة الخدمات المقدمة للعملاء في جميع الفروع "
    clean = (words * 3).strip()
    split = (words * 3 + "خ ر ت ش ع").strip()
    assert arabic_fragment_ratio(clean) == 0
    assert check_native(1, PageText(clean, clean, Counter({"pdf": 200}), 200), False).status == "ok"
    assert "split_words" in check_native(1, PageText(split, split, Counter({"pdf": 200}), 200),
                                         False).reasons
    few = "نص قصير"
    assert check_native(1, PageText(few, few, Counter({"pdf": 7}), 7),
                        False).reasons == ["too_little_arabic"]
    english = "This page has English words only and no Arabic at all."
    assert check_native(1, PageText(english, english, Counter({"pdf": 50}), 50),
                        False).status == "ok"
