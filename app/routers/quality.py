"""
Quality-metadata HTTP surface.

Endpoints under `/api/quality`:

  Coverage + backfill (v2.25.0):
    GET  /api/quality/stats              — coverage stats for the dashboard
    POST /api/quality/backfill/start     — kick off the backfill worker
    GET  /api/quality/backfill/status    — poll the running backfill
    POST /api/quality/backfill/stop      — request cancellation

  Bundle A.2 — replacement opportunities + safety (v2.26.0):
    GET   /api/quality/library-safety              — per-library safety badges
    GET   /api/quality/replacement-opportunities   — list with filters
    PATCH /api/quality/replacement-opportunities/{id} — dismiss
                                                        (status = 'dismissed')
    GET   /api/quality/replacement-opportunities/counts — per-status totals

The backfill itself runs in-process via `app/quality/backfill.py`.
This router just exposes start/poll/stop and the coverage stats.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app import state
from app.config import load_settings
from app.database import get_db
from app.orchestrator.active_replacement import library_replacement_status
from app.quality import backfill as backfill_mod
from app.quality.opportunities import (
    get_opportunity,
    list_opportunities,
    opportunity_counts,
    update_status,
)
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


# ─── Bundle A.2 — library safety + replacement opportunities ─


@router.get("/library-safety")
async def get_library_safety() -> dict[str, Any]:
    """Per-library replacement-safety status for the Settings UI.

    Returns one entry per discovered library with the safety badge
    (`safe` / `overlap` / `unknown`), the per-library opt-in bool,
    and the effective gate value (what the Phase 5 detector actually
    checks). The UI uses this to render the toggle row + warning
    badge under each library in Settings → Active Replacement.
    """
    settings = load_settings()
    libs = list(state._discovered_libraries)
    return {
        "libraries": [
            library_replacement_status(lib, settings) for lib in libs
        ],
    }


@router.get("/replacement-opportunities")
async def get_replacement_opportunities(
    status: Optional[str] = Query(
        "detected",
        description=(
            "Filter by status: 'detected' (default), 'enacted', 'dismissed', "
            "or omit/empty for all statuses."
        ),
    ),
    library_slug: Optional[str] = Query(
        None,
        description="Optional per-library narrowing.",
    ),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """List replacement opportunities for the UI.

    Default returns the live `detected` queue, newest first. Pass
    `?status=` (empty string) to include every status — useful for
    the audit-history view.
    """
    effective_status: Optional[str]
    if status is None or status == "":
        effective_status = None
    else:
        effective_status = status
    db = await get_db()
    try:
        rows = await list_opportunities(
            db,
            status=effective_status,
            library_slug=library_slug,
            limit=limit,
        )
        counts = await opportunity_counts(db)
    finally:
        await db.close()
    return {"opportunities": rows, "counts": counts}


@router.get("/replacement-opportunities/counts")
async def get_replacement_opportunity_counts() -> dict[str, Any]:
    """Compact per-status counts for sidebar badges / dashboard tiles."""
    db = await get_db()
    try:
        return await opportunity_counts(db)
    finally:
        await db.close()


class _PatchOpportunityBody(BaseModel):
    """PATCH body: only `status` mutation is exposed today.

    Accepted values: 'dismissed' (user marked the opportunity as not
    interesting) and 'detected' (un-dismiss back to the live queue).
    'enacted' is intentionally NOT exposed via this endpoint — Phase 5b
    will own that transition when the file-swap path lands.
    """
    status: str


@router.patch("/replacement-opportunities/{opportunity_id}")
async def patch_replacement_opportunity(
    opportunity_id: int,
    body: _PatchOpportunityBody,
) -> dict[str, Any]:
    if body.status not in ("dismissed", "detected"):
        raise HTTPException(
            status_code=400,
            detail=(
                "status must be 'dismissed' or 'detected'; 'enacted' is "
                "reserved for the Phase 5b file-swap path."
            ),
        )
    db = await get_db()
    try:
        existing = await get_opportunity(db, opportunity_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="not_found")
        changed = await update_status(
            db, opportunity_id, status=body.status, acted_by="user",
        )
        await db.commit()
        if not changed:
            # update_status returns False only when the row vanished
            # between get + update (concurrent delete). Treat as 404.
            raise HTTPException(status_code=404, detail="not_found")
        updated = await get_opportunity(db, opportunity_id)
    finally:
        await db.close()
    return updated or {}
