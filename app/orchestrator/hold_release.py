"""
Pending-hold release scheduler (v2.9.0 format-priority dedup).

Every `interval_seconds` (default 60), this loop wakes any
`pending_holds` row whose `release_at` has passed and re-evaluates
the dedup decision against the *current* state of the world:

  * If a higher-priority sibling appeared during the hold window
    (in flight or owned), the held grab is no longer wanted —
    mark dropped.
  * If nothing arrived, the hold gets its grab — call inject_grab
    with `apply_format_dedup=False` (we ARE the dedup decision
    releasing) and mark released.

Re-evaluation matters because the world can change during the 10-min
hold: a manual Calibre add, an automated sync that flips an owned
flag, or another announce we missed earlier could all turn an
allow-at-release into a skip. Re-checking is cheap (one sibling
lookup, one pure-function gate evaluation).

The scheduler is a `supervised_task` registered from main.py's
lifespan, mirroring `app/orchestrator/review_timeout.py` and
`app/orchestrator/download_watcher.py`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.database import get_db
from app.filter.gate import Announce
from app.orchestrator.dispatch import DispatcherDeps, inject_grab
from app.orchestrator.format_dedup import (
    evaluate_format_dedup,
    lookup_dedup_siblings,
)
from app.storage import holds as holds_storage

_log = logging.getLogger("seshat.orchestrator.hold_release")


async def tick(deps: DispatcherDeps) -> int:
    """Process every due hold once. Returns the count of holds resolved
    (released or dropped) this tick.

    Tests drive this directly; the supervised loop wraps it.
    """
    db = await get_db()
    try:
        due = await holds_storage.list_due(db)
    finally:
        await db.close()

    if not due:
        return 0

    _log.info("hold_release: %d hold(s) due for re-evaluation", len(due))
    resolved = 0

    for hold in due:
        try:
            siblings_raw = await lookup_dedup_siblings(
                dedup_key=hold.dedup_key,
                media_type=hold.media_type,
            )
        except Exception:
            _log.exception(
                "hold_release: sibling lookup failed for hold_id=%s",
                hold.id,
            )
            continue

        # The hold's own row would appear in the lookup result as a
        # held sibling at the same priority. Filter it out before
        # re-evaluating so we don't see ourselves as a blocker.
        siblings = [s for s in siblings_raw if s.hold_id != hold.id]

        # Synthesize an Announce shape matching what the original
        # IRC dispatch saw — the gate function is announce-shaped,
        # not hold-shaped, and re-running it is the cleanest way to
        # apply identical rules.
        synthetic = Announce(
            torrent_id=hold.torrent_id,
            torrent_name=hold.torrent_name,
            category=hold.category or "",
            author_blob=hold.author_blob or "",
            title=hold.torrent_name,
            filetype=hold.book_format,
        )
        decision = evaluate_format_dedup(
            announce=synthetic,
            format_priority=deps.format_priority,
            hold_seconds=deps.format_dedup_hold_seconds,
            siblings=siblings,
        )

        if decision.action == "skip":
            # A blocking sibling arrived during the window — exactly
            # the case the hold was designed to defend against.
            db = await get_db()
            try:
                await holds_storage.drop_holds(
                    db, [hold.id],
                    reason=f"released_blocked_by_sibling:{decision.reason}",
                )
            finally:
                await db.close()
            _log.info(
                "hold_release: dropped hold_id=%s (reason=%s) — "
                "blocking sibling arrived during window",
                hold.id, decision.reason,
            )
            resolved += 1
            continue

        # action == "allow" OR action == "hold". At release time, a
        # "hold" return means "no blocker materialized, lone disabled
        # format" — which is Scenario 3 from the spec: grab. The hold
        # already paid its time penalty waiting; we shouldn't restart
        # the timer. Both "allow" and "hold" → release-by-grabbing.
        # apply_format_dedup=False because WE are the dedup decision
        # releasing; the gate's already run.
        try:
            result = await inject_grab(
                deps,
                torrent_id=hold.torrent_id,
                torrent_name=hold.torrent_name,
                category=hold.category or "",
                author_blob=hold.author_blob or "",
                filetype=hold.book_format,
                raw_line=f"hold_release:{hold.id}",
                apply_format_dedup=False,
            )
        except Exception:
            _log.exception(
                "hold_release: inject_grab failed for hold_id=%s",
                hold.id,
            )
            continue

        db = await get_db()
        try:
            await holds_storage.mark_released(
                db, hold.id,
                reason=f"timer_fired:grab_{result.grab_id}",
            )
        finally:
            await db.close()
        _log.info(
            "hold_release: released hold_id=%s as grab_id=%s "
            "(action=%s)",
            hold.id, result.grab_id, result.action,
        )
        resolved += 1

    return resolved


async def run_loop(
    deps: DispatcherDeps,
    *,
    interval_seconds: int = 60,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Supervised-task entry point. Sleeps `interval_seconds` between
    ticks; cancellation via `supervised_task`'s wrapper or
    `stop_event.set()` (the latter is for unit tests).

    A 60s default is fine for 10-minute holds — the resolution
    granularity is "within a minute of the release_at timestamp",
    not "instant". Per-tick cost is one indexed query + one decision
    per due hold, so any reasonable interval scales.
    """
    _log.info("hold_release loop started (interval=%ss)", interval_seconds)
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            await tick(deps)
        except Exception:
            _log.exception("hold_release: tick raised; sleeping and retrying")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break
