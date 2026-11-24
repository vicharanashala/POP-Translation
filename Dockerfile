FROM python:3.11-slim

# ============================================
# Install runtime dependencies
# ============================================
# pandoc: HTML -> DOCX, the one external binary the translation pipeline runs
#   (convert_html_to_docx in scripts/run_pop_to_docx_updated_pagewise_docx.py).
# curl + xz-utils: download and unpack the s6-overlay release below, which ships
#   as .tar.xz. The slim base has no xz; it used to arrive only as a side effect
#   of build-essential (dpkg-dev depends on xz-utils), so it is named here.
# Deliberately absent: tesseract-ocr, its -dev headers and build-essential.
# They were here to compile `tesserocr` for the upload-time duplicate check,
# which no longer runs in the server (dashboard/dedup.py is gone). Every
# package in requirements.lock ships a prebuilt wheel, so nothing here needs
# a compiler. OCR for translation is the separate OCR_URL service.
RUN apt-get update && apt-get install -y --no-install-recommends \
    pandoc \
    curl \
    xz-utils \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ============================================
# Install s6-overlay for process management
# ============================================
ARG S6_OVERLAY_VERSION=3.2.3.0
RUN curl -sL https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz -o /tmp/s6-overlay-noarch.tar.xz && \
    tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz && \
    curl -sL https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz -o /tmp/s6-overlay-x86_64.tar.xz && \
    tar -C / -Jxpf /tmp/s6-overlay-x86_64.tar.xz && \
    rm /tmp/s6-overlay-*.tar.xz

# Create s6 service directory
RUN mkdir -p /etc/services.d/python

WORKDIR /app

COPY requirements.lock .
# Pure PyPI, wheels only: --only-binary makes a missing wheel fail the build
# loudly instead of quietly compiling from source (which would need the
# compiler this image no longer has).
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.lock

COPY . .

RUN mkdir -p pop-data/POP_Work/Data pop-data/POP_Work/Workdir

# Copy the s6 service script
COPY s6-scripts/python-run /etc/services.d/python/run

# Make it executable
RUN chmod +x /etc/services.d/python/run

VOLUME ["/app/pop-data"]

EXPOSE 8032

# Use s6-overlay init binary as entrypoint
ENTRYPOINT ["/init"]