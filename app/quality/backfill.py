"""
Quality-metadata backfill worker.

A single in-process background task that walks every
`book_grab_links`-linked torrent missing a `torrent_quality_metadata`
row and runs the extraction pipeline against each. Used to catch up
existing libraries when v2.25.0 first ships; new grabs auto-populate
via the inline hook in `acquisition_linkback.py`.

State is in-process (no DB persistence) — backfill is operator-driven
and short-lived. Restarting the container during a backfill loses
progress but the work resumes from the same missing-list on next
start because `list_missing_quality_torrent_ids` is order-by-grab-id
deterministic.

Rate limiting: defaults to 5s between MAM calls. The existing
torrent_info module already caches for 120s in process, so re-asking
the same torrent inside the backfill is free, but new IDs each hit
MAM. 5s spacing is well under MAM's per-IP throttle envelope and is
the same pacing the v2.21.0 Amazon worker uses for its scans.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from app.database import get_db
from app.quality.pipeline import extract_for_torrent
from app.quality.storage import (
    list_missing_quality_torrent_ids,
    quality_coverage_stats,
)

_log = logging.getLogger("seshat.quality.backfill")


@dataclass
class BackfillState:
    """Mutable in-memory state for the running backfill task.

    A singleton — only one backfill can run at a time. State persists
    across the task's lifetime so the status endpoint has something to
    report between ticks.
    """
    running: bool = False
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    total_at_start: int = 0
    current_torrent_id: Optional[str] = None
    last_error: Optional[str] = None
    cancel_requested: bool = False
    _task: Optional[asyncio.Task] = field(default=None, repr=False)


# Module-level singleton. The web process owns this; tests reset it.
state = BackfillState()


# Rate-limit between MAM calls during backfill. Configurable via
# `quality_backfill_interval_s` setting; default 5.0. Lower bounds
# enforced so a misconfiguration doesn't burst MAM.
_MIN_INTERVAL_S = 1.0
_DEFAULT_INTERVAL_S = 5.0


def _interval_seconds() -> float:
    """Read pacing config live so an operator can tune mid-run."""
    from app.config import load_settings
    s = load_settings()
    raw = s.get("quality_backfill_interval_s", _DEFAULT_INTERVAL_S)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_INTERVAL_S
    return max(_MIN_INTERVAL_S, value)


async def _run() -> None:
    """The actual backfill loop. Drives `state` for the status endpoint.

    Loop semantics:
      1. Fetch the next batch of missing torrent IDs.
      2. For each ID:
         - Run extract_for_torrent.
         - Increment processed / skipped / failed counters.
         - Update current_torrent_id for the status display.
         - Sleep `_interval_seconds()` between successful MAM hits.
         - Honor cancel_requested at every step.
      3. Refresh the batch. If empty, finish.

    Errors are logged but never raised — one bad torrent ID shouldn't
    halt a 500-torrent backfill.
    """
    try:
        # Snapshot the total at start so the UI can show a progress
        # estimate that doesn't drift as new grabs land mid-run.
        db = await get_db()
        try:
            stats = await quality_coverage_stats(db)
            state.total_at_start = stats["missing"]
        finally:
            await db.close()

        while not state.cancel_requested:
            db = await get_db()
            try:
                batch = await list_missing_quality_torrent_ids(db, limit=20)
            finally:
                await db.close()

            if not batch:
                _log.info("quality backfill: queue empty, finishing")
                break

            for tid in batch:
                if state.cancel_requested:
                    break
                state.current_torrent_id = tid
                processed_this_iter = False
                try:
                    db = await get_db()
                    try:
                        snap = await extract_for_torrent(
                            db, tid, force_refresh=False,
                        )
                    finally:
                        await db.close()
                    if snap is None:
                        # Either already-extracted (race) or extraction
                        # gracefully bailed (logged downstream).
                        state.skipped += 1
                    else:
                        state.processed += 1
                        processed_this_iter = True
                except Exception as e:
                    state.failed += 1
                    state.last_error = (
                        f"tid={tid}: {type(e).__name__}: {e}"[:200]
                    )
                    _log.warning(
                        "quality backfill: extract failed for %s: %s",
                        tid, e,
                    )

                # Honor pacing only after a real MAM hit. If we just
                # skipped (already extracted), don't sleep — that's
                # wasted wall time on a bundle-dispatch dedup pass.
                if processed_this_iter:
                    await asyncio.sleep(_interval_seconds())
    finally:
        state.running = False
        state.finished_at = time.time()
        state.current_torrent_id = None


def start() -> bool:
    """Kick off the backfill background task.

    Returns True if a task was started, False if one was already
    running. Idempotent — calling twice doesn't spawn duplicates.
    """
    if state.running:
        return False
    state.running = True
    state.started_at = time.time()
    state.finished_at = None
    state.processed = 0
    state.skipped = 0
    state.failed = 0
    state.cancel_requested = False
    state.last_error = None
    state.current_torrent_id = None
    state._task = asyncio.create_task(_run())
    return True


def request_cancel() -> bool:
    """Set the cancel flag; the loop checks it at every step.

    Returns True if a running task was signaled, False if there was
    nothing to cancel. The task may take up to one `_interval_seconds()`
    sleep to notice.
    """
    if not state.running:
        return False
    state.cancel_requested = True
    return True


def status() -> dict:
    """Snapshot the current state for the status endpoint."""
    return {
        "running": state.running,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "processed": state.processed,
        "skipped": state.skipped,
        "failed": state.failed,
        "total_at_start": state.total_at_start,
        "current_torrent_id": state.current_torrent_id,
        "last_error": state.last_error,
        "cancel_requested": state.cancel_requested,
    }
