"""The missing-books cleanup (discovery fix pass S8, G209): Preview → Apply.

  POST /api/discovery/missing-cleanup/preview  — start a preview (background)
  POST /api/discovery/missing-cleanup/apply    — {"preview_id": …} start Apply
  GET  /api/discovery/missing-cleanup/status   — progress + the last preview /
                                                 apply: counts and samples
  GET  /api/discovery/missing-cleanup/report   — ?mode=preview|apply: every row

No source requests: the steps read the library DBs and the caches only.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Body, HTTPException

from app import state
from app.discovery import missing_cleanup

_log = logging.getLogger("seshat.discovery.missing_cleanup")

router = APIRouter(prefix="/api/discovery", tags=["missing-cleanup"])


def _start(mode: str, preview_id: str | None = None) -> dict:
    # Refused, not queued: the page says why and the operator runs it again.
    blocker = missing_cleanup.blocked_by()
    if blocker:
        raise HTTPException(409, f"{blocker} is running")

    async def _do():
        try:
            await missing_cleanup.run(mode, preview_id)
        except Exception:
            _log.exception("missing cleanup %s crashed", mode)

    state._missing_cleanup_task = asyncio.create_task(_do(), name=f"missing-cleanup-{mode}")
    return {"status": "started", "mode": mode}


@router.post("/missing-cleanup/preview")
async def start_preview():
    """Run every step on copies of the DBs; nothing in the library changes."""
    return _start("preview")


@router.post("/missing-cleanup/apply")
async def start_apply(preview_id: str = Body(..., embed=True)):
    """Run every step for real. Needs the id of a preview from the last hour."""
    last = missing_cleanup.last_report("preview")
    if not last or last["id"] != preview_id or not last.get("finished_at"):
        raise HTTPException(409, "Run a preview first: Apply follows the preview you looked at.")
    if (time.time() - last["finished_at"]) > missing_cleanup.PREVIEW_TTL_S:
        raise HTTPException(409, "That preview is over an hour old: run it again before applying.")
    if missing_cleanup.was_applied(preview_id):
        raise HTTPException(409, "That preview was applied: run a new one before applying again.")
    return _start("apply", preview_id)


@router.get("/missing-cleanup/status")
async def cleanup_status():
    return missing_cleanup.status()


@router.get("/missing-cleanup/report")
async def cleanup_report(mode: str = "preview"):
    if mode not in ("preview", "apply"):
        raise HTTPException(400, "mode is preview or apply")
    report = missing_cleanup.last_report(mode)
    if report is None:
        raise HTTPException(404, f"No {mode} has run since Seshat started")
    return report
