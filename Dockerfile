# Seshat Docker image — Phase 3 production build.
#
# Includes calibre CLI tools (~500MB) for the post-download pipeline
# (calibredb add). The calibredb binary talks directly to a Calibre
# library directory mounted as a volume — it does NOT need the
# Calibre GUI or content server running.
#
# Two-stage build: a small node:lts stage compiles the React frontend
# (Vite + TypeScript), then we copy `frontend/dist` into the Python
# stage. This keeps the runtime image free of node_modules + npm.

# ─── Stage 1: frontend build ───────────────────────────────────
FROM node:22-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


# ─── Stage 2: python runtime ───────────────────────────────────
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
#   - sqlite3: ad-hoc DB inspection during ops debugging
#   - calibre: provides calibredb CLI for the post-download pipeline.
#     ~500MB but needed for Phase 2 Calibre sink integration.
#   - wget, xdg-utils: calibre installer dependencies
#
# DEBIAN_FRONTEND=noninteractive is scoped to this RUN via the inline
# env rather than an ENV directive so it doesn't persist into the
# runtime image. Quiets debconf's "can't find Term::ReadLine / TERM is
# not set" cascade when apt has no tty — the fallback it ends at
# (Noninteractive) is what we want anyway.
RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        sqlite3 \
        calibre \
    && rm -rf /var/lib/apt/lists/*

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

# Mount targets. /app/data for settings.json + seshat.db, /calibre
# for the Calibre library, /staging for the post-download staging area.
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
