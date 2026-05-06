# Seshat — production Docker image (full Calibre).
#
# Two image variants are published per push:
#   - ghcr.io/malevolenttortoise/seshat:latest        ← built from this Dockerfile
#   - ghcr.io/malevolenttortoise/seshat:latest-slim   ← built from Dockerfile.slim,
#                                               no Calibre, ~225MB. Pick this
#                                               one if you ingest via CWA, ABS,
#                                               or the file-folder sink and
#                                               don't need direct calibredb add.
#
# The full image uses Calibre's official self-contained tarball
# (~650MB) instead of `apt install calibre` (which pulled 1.27GB of
# Qt5 + Mesa stack via apt). Calibre bundles its own Python + Qt + libs,
# so apt-side we only install the few small system libraries Calibre's
# loader expects to find at runtime.
#
# Three-stage build:
#   1. node:22-alpine   compiles the React frontend (Vite + TypeScript)
#   2. python:3.12-slim downloads + extracts the Calibre tarball
#   3. python:3.12-slim is the runtime; it copies /opt/calibre from
#      stage 2 and frontend/dist from stage 1, then layers the Python
#      app. Stage 1 + 2 caches are discarded so wget/xz/node never
#      ship in the runtime layer.

# ─── Stage 1: frontend build ───────────────────────────────────
FROM node:22-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


# ─── Stage 2: Calibre fetch ────────────────────────────────────
# Pulls the official Calibre tarball and extracts it. wget + xz only
# live in this stage so they don't bloat the runtime image.
#
# CALIBRE_VERSION is bumped via Renovate's regex manager — the comment
# below is the dependency declaration. To bump manually:
#   1. Find the latest tag at https://github.com/kovidgoyal/calibre/releases
#   2. Update the value below
#   3. Verify the tarball URL responds with 200:
#      curl -sI https://download.calibre-ebook.com/<version>/calibre-<version>-x86_64.txz
FROM python:3.12-slim AS calibre-fetch
# renovate: datasource=github-releases depName=kovidgoyal/calibre extractVersion=^v(?<version>.+)$
ARG CALIBRE_VERSION=9.7.0
RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        wget \
        xz-utils \
        ca-certificates \
    && wget -nv -O /tmp/calibre.txz \
        "https://download.calibre-ebook.com/${CALIBRE_VERSION}/calibre-${CALIBRE_VERSION}-x86_64.txz" \
    && mkdir -p /opt/calibre \
    && tar xJf /tmp/calibre.txz -C /opt/calibre \
    && rm /tmp/calibre.txz


# ─── Stage 3: python runtime ───────────────────────────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Silence pip's "running as root" nag. In a Docker image the
    # container IS the isolation boundary — a venv would just add
    # indirection without any security benefit.
    PIP_ROOT_USER_ACTION=ignore \
    SESHAT_MODE=docker \
    DATA_DIR=/app/data

WORKDIR /app

# OS deps:
#   - sqlite3:        ad-hoc DB inspection during ops debugging
#   - libxcb-cursor0: Qt 6.5+ requires this for the cursor theme even
#                     in headless contexts where calibredb only loads
#                     QtCore.
#   - libfontconfig1: Calibre's font enumeration (used by the metadata
#                     cover generator) calls into fontconfig at import.
#   - libxrender1:    Qt's xcb platform plugin pulls libxrender1 even
#                     when calibredb runs without a display.
#
# We deliberately do NOT install libgl1 / libegl1 / libopengl0: those
# drag in libllvm19 (~127MB) + mesa-libgallium (~42MB) for the software
# OpenGL stack, and headless calibredb add/list don't exercise GL paths
# in any real-world test we've run. If a user hits a Qt-platform-plugin
# failure that signals the GL stack IS needed for some specific Calibre
# operation, app/sinks/calibre.py:_detect_runtime_lib_failure logs a
# structured diagnostic pointing at the issue tracker so we can collect
# data and re-add libgl1/libegl1/libopengl0 if it turns out we need to.
RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        sqlite3 \
        libxcb-cursor0 \
        libfontconfig1 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Pre-built Calibre tarball from stage 2. Symlink calibredb onto PATH
# so app/sinks/calibre.py and app/notify/digests.py find it without
# any path config.
COPY --from=calibre-fetch /opt/calibre /opt/calibre
RUN ln -s /opt/calibre/calibredb /usr/local/bin/calibredb

# Install Python runtime dependencies first so the layer cache stays
# warm across code changes. Test deps live in requirements-dev.txt
# and are deliberately NOT installed in the production image.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# App source. Tests, previous-stuff/, and the venv are all excluded
# via .dockerignore.
COPY app ./app
COPY pyproject.toml ./

# Built frontend bundle from stage 1. main.py mounts this at runtime
# from `frontend/dist` relative to the app directory.
COPY --from=frontend-build /build/dist ./frontend/dist

# Bake the build's git SHA into the image so the Settings page can
# show "Build: abc1234" and users can verify their container is on
# the version they think it is. CI passes --build-arg
# GIT_SHA=${{ github.sha }}; local builds get "unknown" by default.
ARG GIT_SHA=unknown
RUN echo "${GIT_SHA}" > /app/VERSION

# Mount targets. /app/data for settings.json + seshat.db, /calibre for
# the Calibre library, /staging for the post-download staging area.
RUN mkdir -p /app/data /calibre /staging
VOLUME ["/app/data"]

# WebUI port.
EXPOSE 8789

# Liveness probe — uses /api/health, which reports both the service
# status and whether the dispatcher singleton has been built.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8789/api/health').status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8789"]
