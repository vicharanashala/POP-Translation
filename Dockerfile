FROM python:3.11-slim

# tesseract-ocr: runtime OCR engine (used by pytesseract + tesserocr).
# libtesseract-dev/libleptonica-dev/pkg-config/build-essential: headers and a
# compiler needed to build the tesserocr C extension at pip-install time.
# No tesseract-ocr-<lang> packages -- OCR always points at the bundled
# tessdata_best/ models (see scripts/hash_and_embed_report_true.py), never
# the system tessdata dir.
RUN apt-get update && apt-get install -y --no-install-recommends \
    pandoc \
    tesseract-ocr \
    libtesseract-dev \
    libleptonica-dev \
    pkg-config \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock .
# --extra-index-url: torch is pinned to the CPU-only build (see
# pyproject.toml's [tool.uv.sources]/[[tool.uv.index]] for the matching uv
# config) -- only download.pytorch.org hosts the "+cpu" tagged wheel.
RUN pip install --no-cache-dir -r requirements.lock \
    --extra-index-url https://download.pytorch.org/whl/cpu

COPY . .

RUN mkdir -p pop-data/POP_Work/Data pop-data/POP_Work/Workdir

VOLUME ["/app/pop-data"]

EXPOSE 8032

CMD ["uvicorn", "pop_server:app", "--host", "0.0.0.0", "--port", "8032"]
