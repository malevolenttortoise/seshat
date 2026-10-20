"""
Data Hygiene endpoints — single user-trigger that fans the 6-job
hygiene chain across every configured library.

  POST /api/discovery/hygiene/run  — start (background task)
  POST /api/discovery/hygiene/cancel — cancel an in-flight chain
  GET  /api/discovery/hygiene/status — current state snapshot

Progress is also surfaced on the unified `/api/discovery/scan-status`
endpoint so the Command Center banner picks it up alongside source
scans and library syncs without a separate poll.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from app import state
from app.discovery.hygiene import is_running as _is_running, run_all
from app.config import ui_error

_log = logging.getLogger("seshat.discovery.hygiene")

router = APIRouter(prefix="/api/discovery", tags=["hygiene"])


@router.post("/hygiene/run")
async def trigger_hygiene():
    """Spawn the Data Hygiene chain as a background task and return
    immediately. Refuses overlap so a second click doesn't kick off
    two simultaneous chains against the same per-library DBs.
    """
    if await _is_running():
        return {"status": "running", "message": "Data Hygiene is already running"}

    async def _do():
        try:
            await run_all()
        except asyncio.CancelledError:
            state._hygiene_progress.update(
                {"running": False, "status": "canceled"}
            )
            raise
        except Exception as e:
            _log.exception("hygiene task crashed")
            state._hygiene_progress.update(
                {"running": False, "status": f"error: {ui_error(e)}"}
            )

    state._hygiene_task = asyncio.create_task(_do(), name="hygiene-chain")
    return {"status": "started"}


@router.post("/hygiene/cancel")
async def cancel_hygiene():
    t = state._hygiene_task
    if not t or t.done():
        return {"status": "idle"}
    t.cancel()
    return {"status": "canceling"}


@router.get("/hygiene/status")
async def hygiene_status():
    """Direct snapshot. The Command Center prefers the unified
    `/scan-status` projector, but this endpoint exists for
    operators tailing curl during a run.
    """
    p = state._hygiene_progress
    return {
        "running": bool(p.get("running")),
        "current_job_idx": int(p.get("current_job_idx", 0)),
        "total_jobs": int(p.get("total_jobs", 0)),
        "current_job_name": p.get("current_job_name", ""),
        "current_library": p.get("current_library", ""),
        "current": int(p.get("current", 0)),
        "total": int(p.get("total", 0)),
        "status": p.get("status", "idle"),
        "jobs": p.get("jobs", []),
    }
