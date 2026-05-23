"""
Quality-metadata HTTP surface.

Three endpoints under `/api/quality`:

  - GET  /api/quality/stats          — coverage stats for the dashboard
  - POST /api/quality/backfill/start — kick off the backfill worker
  - GET  /api/quality/backfill/status — poll the running backfill
  - POST /api/quality/backfill/stop  — request cancellation

The backfill itself runs in-process via `app/quality/backfill.py`.
This router just exposes start/poll/stop and the coverage stats.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from app.database import get_db
from app.quality import backfill as backfill_mod
from app.quality.storage import quality_coverage_stats

_log = logging.getLogger("seshat.routers.quality")

router = APIRouter(prefix="/api/quality", tags=["quality"])


@router.get("/stats")
async def get_stats() -> dict[str, Any]:
    """Return coverage stats: how many linked torrents are extracted,
    how many are missing, and which extraction tiers won.

    Always returns a 200; an empty-stats DB returns zeroes.
    """
    db = await get_db()
    try:
        return await quality_coverage_stats(db)
    finally:
        await db.close()


@router.post("/backfill/start")
async def start_backfill() -> dict[str, Any]:
    """Kick off the backfill worker. Idempotent — re-clicking while
    a backfill is running returns the same `running: true` status.
    """
    started = backfill_mod.start()
    snap = backfill_mod.status()
    return {"started": started, **snap}


@router.get("/backfill/status")
async def get_backfill_status() -> dict[str, Any]:
    """Poll the current backfill state. Safe to call at any cadence.

    Returns `running: false` when no backfill has been triggered yet
    or when the last one has finished.
    """
    return backfill_mod.status()


@router.post("/backfill/stop")
async def stop_backfill() -> dict[str, Any]:
    """Request cancellation. The loop checks the flag at every step
    so the task takes up to one rate-limit interval to notice.
    """
    requested = backfill_mod.request_cancel()
    return {"requested": requested, **backfill_mod.status()}
