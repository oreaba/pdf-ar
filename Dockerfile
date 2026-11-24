# syntax=docker/dockerfile:1.7
# pdf-ar: deterministic Arabic and English PDF text extraction, CPU only.
#
# Build from the repository root:
#   docker build -t pdf-ar:0.1 .
# Usage: see README.md ("Docker").

FROM python:3.13-slim-bookworm

# Tesseract 5 for the ocr and auto engines and --ocr-check. native and fonts do not need it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Tesseract 4.1.0 models, pinned by SHA-256. --chmod also applies to the folders ADD
# creates, so it must be 755 for the non-root user to enter them; setting it here avoids
# a chmod step that would copy the models again.
ARG TESSDATA=https://raw.githubusercontent.com/tesseract-ocr
ADD --chmod=755 --checksum=sha256:e3206d3dc87fd50c24a0fb9f01838615911d25168f4e64415244b67d2bb3e729 \
    ${TESSDATA}/tessdata_fast/4.1.0/ara.traineddata /opt/tessdata/fast/ara.traineddata
ADD --chmod=755 --checksum=sha256:7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2 \
    ${TESSDATA}/tessdata_fast/4.1.0/eng.traineddata /opt/tessdata/fast/eng.traineddata
ADD --chmod=755 --checksum=sha256:ab9d157d8e38ca00e7e39c7d5363a5239e053f5b0dbdb3167dde9d8124335896 \
    ${TESSDATA}/tessdata_best/4.1.0/ara.traineddata /opt/tessdata/best/ara.traineddata
ADD --chmod=755 --checksum=sha256:8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba \
    ${TESSDATA}/tessdata_best/4.1.0/eng.traineddata /opt/tessdata/best/eng.traineddata
ADD --chmod=755 --checksum=sha256:9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff \
    ${TESSDATA}/tessdata_fast/4.1.0/osd.traineddata /opt/tessdata/fast/osd.traineddata
ADD --chmod=755 --checksum=sha256:9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff \
    ${TESSDATA}/tessdata_fast/4.1.0/osd.traineddata /opt/tessdata/best/osd.traineddata

# The same library versions as the validated runs, so output is byte-identical. PyMuPDF
# 2.1 also pulls in its layout and LLM add-ons (onnxruntime, numpy, networkx and their
# dependencies); pdf-ar uses none of them, so they are removed, and so is pip.
WORKDIR /opt/pdf-ar
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --root-user-action=ignore ".[ocr]" \
        "pymupdf==2.1" "fonttools==4.68.0" "pytesseract==0.3.13" "pillow==13.0.0" \
    && pip uninstall -y -q --root-user-action=ignore onnxruntime numpy networkx \
        pymupdf_layout pymupdf4llm flatbuffers protobuf tabulate \
    && python -m pdf_ar --help > /dev/null \
    && pip uninstall -y -q --root-user-action=ignore pip

ENV PDF_EXTRACT_TESSDATA=/opt/tessdata \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Fonts are licensed and are never baked in: mount a folder that holds fonts/.
RUN useradd --create-home --uid 1000 extract
USER extract
WORKDIR /data
ENTRYPOINT ["python", "-m", "pdf_ar"]
CMD ["--help"]
