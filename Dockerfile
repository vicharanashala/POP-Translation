FROM python:3.11-slim

# ============================================
# Install runtime dependencies
# ============================================
# tesseract-ocr: runtime OCR engine (used by pytesseract + tesserocr).
# libtesseract-dev/libleptonica-dev/pkg-config/build-essential: headers and a
# compiler needed to build the tesserocr C extension at pip-install time.
# No tesseract-ocr-<lang> packages -- OCR always points at the bundled
# tessdata_best/ models (see scripts/hash_and_embed_report_true.py), never
# the system tessdata dir.
# tailscale curl: for tailscale VPN connectivity
RUN apt-get update && apt-get install -y --no-install-recommends \
    pandoc \
    tesseract-ocr \
    libtesseract-dev \
    libleptonica-dev \
    pkg-config \
    build-essential \
    curl \
    iptables \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ============================================
# Install Tailscale
# ============================================
RUN curl -fsSL https://pkgs.tailscale.com/stable/tailscale_1.78.1_amd64.tgz | tar -xz -C /usr/local
RUN ldconfig

# Create essential runtime directories for the Tailscale daemon
RUN mkdir -p /var/run/tailscale /var/lib/tailscale /dev/net && \
    touch /dev/net/tun && \
    chmod 600 /dev/net/tun

# ============================================
# Install s6-overlay for process management
# ============================================
ARG S6_OVERLAY_VERSION=3.2.3.0
RUN curl -sL https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz -o /tmp/s6-overlay-noarch.tar.xz && \
    tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz && \
    curl -sL https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz -o /tmp/s6-overlay-x86_64.tar.xz && \
    tar -C / -Jxpf /tmp/s6-overlay-x86_64.tar.xz && \
    rm /tmp/s6-overlay-*.tar.xz

# Create s6 service directories
RUN mkdir -p /etc/services.d/tailscale /etc/services.d/python

WORKDIR /app

COPY requirements.lock .
# --extra-index-url: torch is pinned to the CPU-only build (see
# pyproject.toml's [tool.uv.sources]/[[tool.uv.index]] for the matching uv
# config) -- only download.pytorch.org hosts the "+cpu" tagged wheel.
RUN pip install --no-cache-dir -r requirements.lock \
    --extra-index-url https://download.pytorch.org/whl/cpu

COPY . .

RUN mkdir -p pop-data/POP_Work/Data pop-data/POP_Work/Workdir

# Copy s6 service scripts
COPY s6-scripts/tailscale-run /etc/services.d/tailscale/run
COPY s6-scripts/python-run /etc/services.d/python/run

# Make scripts executable
RUN chmod +x /etc/services.d/tailscale/run /etc/services.d/python/run

VOLUME ["/app/pop-data"]

EXPOSE 8032

# Use s6-overlay init binary as entrypoint
ENTRYPOINT ["/init"]