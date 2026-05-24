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
from app.orchestrator.active_replacement import (
    EnactmentResult,
    enact_opportunity,
    library_replacement_status,
    restore_enactment,
)
from app.quality import backfill as backfill_mod
from app.quality.enactments import latest_active_enactment
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


# ─── Phase 5b — enact + restore HTTP surface ─────────────────


# Map orchestrator EnactmentResult statuses to HTTP codes. The shape
# of the JSON body is identical regardless of HTTP code so the UI can
# render the toast from `detail` without branching.
#
#   200  enacted / restored          (success)
#   404  not_found                   (opportunity / enactment missing,
#                                     or owned book directory gone)
#   409  blocked                     (gate off, wrong status, original
#                                     path now occupied, etc.)
#   503  no_sink                     (slim image with no CWA config,
#                                     or unsupported app_type)
#   500  failed                      (sink call failed AFTER the soft-
#                                     delete; rollback ran. Operator
#                                     can retry once the sink is back)
_STATUS_TO_HTTP: dict[str, int] = {
    "enacted":   200,
    "restored":  200,
    "not_found": 404,
    "blocked":   409,
    "no_sink":   503,
    "failed":    500,
}


def _enactment_result_to_body(
    result: EnactmentResult,
    *,
    opportunity: Optional[dict] = None,
) -> dict[str, Any]:
    """Serialize an EnactmentResult into the response body shape.

    `opportunity` is the refreshed opportunity row (post-status-flip
    on success; the pre-call snapshot on failure paths) so the UI can
    patch its local state without a follow-up GET.
    """
    return {
        "status": result.status,
        "opportunity_id": result.opportunity_id,
        "enactment_id": result.enactment_id,
        "detail": result.detail,
        "error": result.error,
        "opportunity": opportunity,
    }


@router.post("/replacement-opportunities/{opportunity_id}/enact")
async def enact_replacement_opportunity(
    opportunity_id: int,
) -> dict[str, Any]:
    """Perform the destructive file-swap for one detected opportunity.

    See `app/orchestrator/active_replacement.py::enact_opportunity`
    for the full flow. Body is always EnactmentResult-shaped (see
    `_enactment_result_to_body`) plus the refreshed opportunity row.

    HTTP code derives from the orchestrator's status — see
    `_STATUS_TO_HTTP`.
    """
    db = await get_db()
    try:
        result = await enact_opportunity(
            db, opportunity_id, acted_by="user",
        )
        await db.commit()
        # Always re-fetch the opportunity so the UI sees the post-call
        # state (status=enacted on success; still status=detected on
        # rollback/blocked/etc).
        opp = await get_opportunity(db, opportunity_id)
    finally:
        await db.close()

    body = _enactment_result_to_body(result, opportunity=opp)
    http_code = _STATUS_TO_HTTP.get(result.status, 500)
    if http_code != 200:
        # FastAPI's HTTPException carries the dict through as JSON.
        raise HTTPException(status_code=http_code, detail=body)
    return body


class _BulkEnactBody(BaseModel):
    """POST body for bulk enact: list of opportunity ids to attempt.

    The route always returns HTTP 200 with per-item results — the UI
    handles mixed-outcome rendering (some succeeded, some blocked,
    etc.) without per-row HTTP code interpretation. Operators bulk-
    selecting 30 rows shouldn't have one failure 500 the whole batch.
    """
    ids: list[int]


@router.post("/replacement-opportunities/enact-bulk")
async def enact_replacement_opportunities_bulk(
    body: _BulkEnactBody,
) -> dict[str, Any]:
    """Attempt to enact multiple opportunities in one call.

    Per design Decision 7: mixed-safety is handled per-item. The
    orchestrator's re-check of `is_replacement_allowed` runs for
    every id, so OVERLAP-gated libraries silently produce a 'blocked'
    result in their slot rather than 500'ing the whole request.

    Response shape:
        {
            "results": [<EnactmentResult-shape>, ...],
            "counts":  {"enacted": N, "blocked": M, ...},
        }

    Always HTTP 200. Each item carries its own status; the UI
    summarises ("3 enacted, 1 blocked, 1 failed — see details").
    """
    if not body.ids:
        return {"results": [], "counts": {}}

    db = await get_db()
    counts: dict[str, int] = {}
    results: list[dict[str, Any]] = []
    try:
        for opp_id in body.ids:
            try:
                result = await enact_opportunity(
                    db, opp_id, acted_by="user",
                )
                await db.commit()
                opp = await get_opportunity(db, opp_id)
                results.append(_enactment_result_to_body(result, opportunity=opp))
                counts[result.status] = counts.get(result.status, 0) + 1
            except Exception as e:
                _log.exception(
                    "bulk enact: opportunity %s raised", opp_id,
                )
                results.append({
                    "status": "failed",
                    "opportunity_id": opp_id,
                    "enactment_id": None,
                    "detail": f"unhandled exception: {type(e).__name__}",
                    "error": f"{type(e).__name__}: {e}",
                    "opportunity": None,
                })
                counts["failed"] = counts.get("failed", 0) + 1
    finally:
        await db.close()

    return {"results": results, "counts": counts}


@router.post("/replacement-opportunities/{opportunity_id}/restore")
async def restore_replacement_opportunity(
    opportunity_id: int,
) -> dict[str, Any]:
    """Reverse the most recent active enactment for one opportunity.

    "Active" = an enactment with `failed_at IS NULL AND
    restored_at IS NULL`. The route resolves the latest such row
    internally so the UI surface stays opportunity-centric — the
    operator picks an upgrade to undo, not an audit log entry.

    Returns 404 when no active enactment exists (opportunity was
    never enacted, OR all prior enactments have been restored /
    failed).
    """
    db = await get_db()
    try:
        # Confirm opportunity exists so we return a useful 404 reason.
        existing = await get_opportunity(db, opportunity_id)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "status": "not_found",
                    "opportunity_id": opportunity_id,
                    "enactment_id": None,
                    "detail": f"opportunity {opportunity_id} does not exist",
                    "error": None,
                    "opportunity": None,
                },
            )

        active = await latest_active_enactment(db, opportunity_id)
        if active is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "status": "not_found",
                    "opportunity_id": opportunity_id,
                    "enactment_id": None,
                    "detail": (
                        f"no active enactment for opportunity "
                        f"{opportunity_id} (never enacted, or all prior "
                        f"enactments are restored / failed)"
                    ),
                    "error": None,
                    "opportunity": existing,
                },
            )

        result = await restore_enactment(
            db, int(active["id"]), restored_by="user",
        )
        await db.commit()
        opp = await get_opportunity(db, opportunity_id)
    finally:
        await db.close()

    body = _enactment_result_to_body(result, opportunity=opp)
    http_code = _STATUS_TO_HTTP.get(result.status, 500)
    if http_code != 200:
        raise HTTPException(status_code=http_code, detail=body)
    return body
