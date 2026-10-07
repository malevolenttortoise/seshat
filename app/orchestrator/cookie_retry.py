"""
Cookie-rotation retry loop.

When a grab fails with `failed_cookie_expired`, the torrent wasn't
fetched — the MAM session cookie was invalid at the time of the
attempt. Once the cookie has been rotated (either automatically via
the keep-alive loop, or manually by the user pasting a new cookie),
this job re-attempts every grab stuck in that state.

It retries a grab only when the live cookie differs from the one the
download was refused with (`grabs.failed_with_cookie_id`), never under
dry run, and through the dispatcher's own placement: the snatch budget
decides submit / queue / wait before anything is fetched, qBit adds
are staggered, and a queued grab keeps its bytes (ADR-0022).

Same shape as the budget watcher and cookie keep-alive: a `tick()`
function that does one cycle, plus a `run_loop()` wrapper for the
supervised-task lifespan pattern. Tests target `tick()` directly.

Default interval: 5 minutes. The job is a no-op when there are no
failed-cookie-expired grabs, so the interval mostly affects how
quickly Seshat retries after a cookie rotation.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from app.mam.cookie import fingerprint as cookie_fingerprint
from app.mam.grab import GrabResult
from app.mam.torrent_meta import BencodeError, info_hash
from app.orchestrator import dispatch
from app.orchestrator.dispatch import (
    DispatcherDeps,
    grab_claim_lock,
    release_write_lock,
)
from app.rate_limit import decide_grab_action
from app.rate_limit import ledger as ledger_mod
from app.rate_limit import queue as queue_mod
from app.storage import grabs as grabs_storage

_log = logging.getLogger("seshat.orchestrator.cookie_retry")


@dataclass(frozen=True)
class RetryResult:
    """Outcome of one cookie-retry cycle."""

    found: int
    retried: int
    succeeded: int
    failed_again: int
    error: Optional[str] = None


async def tick(deps: DispatcherDeps) -> RetryResult:
    """Re-attempt every grab stuck in `failed_cookie_expired`.

    For each one whose cookie has changed since it failed:
      1. Ask the snatch budget: submit, queue, or (both full) wait
      2. Re-fetch the .torrent file with the current cookie
      3. Submit to qBit or queue it with its bytes

    Nothing is fetched while dry run is on. All errors are caught so
    the supervised loop never raises.
    """
    live = dispatch._live_kill_switch_state()
    if live["dry_run"] or deps.dry_run:
        return RetryResult(found=0, retried=0, succeeded=0, failed_again=0)
    db = await deps.db_factory()
    try:
        return await _tick_inner(deps, db)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _log.exception("cookie retry tick failed")
        return RetryResult(
            found=0, retried=0, succeeded=0, failed_again=0,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        await db.close()


async def _tick_inner(deps: DispatcherDeps, db) -> RetryResult:
    rows = await _find_cookie_expired_grabs(db)
    if not rows:
        return RetryResult(found=0, retried=0, succeeded=0, failed_again=0)

    _log.info("cookie retry: found %d failed_cookie_expired grabs", len(rows))

    retried = 0
    succeeded = 0
    failed_again = 0

    for grab, failed_with in rows:
        ok = await _retry_grab(deps, db, grab, failed_with_cookie_id=failed_with)
        if ok is None:
            continue
        retried += 1
        if ok:
            succeeded += 1
        else:
            failed_again += 1

    _log.info(
        "cookie retry: retried=%d succeeded=%d failed=%d",
        retried, succeeded, failed_again,
    )
    return RetryResult(
        found=len(rows),
        retried=retried,
        succeeded=succeeded,
        failed_again=failed_again,
    )


async def _find_cookie_expired_grabs(
    db,
) -> list[tuple[grabs_storage.GrabRow, Optional[int]]]:
    """Every failed_cookie_expired grab, with the fingerprint of the
    cookie it failed with (None on rows from before it was recorded)."""
    cursor = await db.execute(
        """
        SELECT id, announce_id, mam_torrent_id, torrent_name, category,
               author_blob, torrent_file_path, qbit_hash, state, grabbed_at,
               submitted_at, failed_reason, failed_with_cookie_id
        FROM grabs
        WHERE state = ?
        ORDER BY id ASC
        """,
        (grabs_storage.STATE_FAILED_COOKIE_EXPIRED,),
    )
    rows = await cursor.fetchall()
    return [
        (grabs_storage._row_to_grab(r), r["failed_with_cookie_id"])
        for r in rows
    ]


async def _retry_grab(
    deps: DispatcherDeps,
    db,
    grab: grabs_storage.GrabRow,
    *,
    failed_with_cookie_id: Optional[int],
) -> Optional[bool]:
    """Re-fetch and place one previously-failed grab.

    Returns True when the grab reached qBit or the queue, False when it
    failed again, and None when it wasn't attempted: the live cookie is
    the one MAM already refused, or the snatch budget and the queue are
    both full. An unattempted row stays `failed_cookie_expired` for the
    next tick, and nothing is sent to MAM for it. On failure, the
    grab's state is updated to reflect the new failure mode (which may
    differ from cookie_expired if, e.g., the torrent has since been
    removed from MAM).

    Snatch safety: a row only gets re-fetched if MAM never served it.
    A `qbit_hash` means an earlier fetch succeeded (pre-Phase-0, a
    queued grab whose pop-time RE-fetch hit the expired cookie), and a
    newer blocking grab for the same torrent ID means the user already
    re-grabbed it. Either way a fetch here would be a second download,
    so the row is retired instead. The row is claimed (set to
    `fetched`) under `grab_claim_lock`, so a concurrent inject of the
    same ID sees it as in flight and backs off.
    """
    token = deps.live_mam_token()
    if (
        failed_with_cookie_id is not None
        and cookie_fingerprint(token) == failed_with_cookie_id
    ):
        _log.debug(
            "cookie retry: grab_id=%d waits for a new cookie", grab.id,
        )
        return None
    rate_decision = decide_grab_action(
        budget_used=await ledger_mod.count_effective(db),
        budget_cap=deps.budget_cap,
        queue_size=await queue_mod.size(db),
        queue_max=deps.queue_max,
        queue_mode_enabled=deps.queue_mode_enabled,
    )
    if rate_decision.action == "drop":
        _log.debug(
            "cookie retry: grab_id=%d waits for budget (%s)",
            grab.id, rate_decision.reason,
        )
        return None

    await release_write_lock(db)
    async with grab_claim_lock():
        reason = None
        if grab.qbit_hash:
            reason = (
                "not retried: MAM already served this torrent once; "
                "fetching it again would be a second download"
            )
        else:
            blocker = await grabs_storage.find_blocking_grab(
                db, grab.mam_torrent_id, exclude_grab_id=grab.id,
            )
            if blocker is not None:
                reason = f"not retried: superseded by grab #{blocker.id}"
        if reason is not None:
            await grabs_storage.set_state(
                db, grab.id, grabs_storage.STATE_FAILED_UNKNOWN,
                failed_reason=reason,
            )
            _log.info("cookie retry: grab_id=%d %s", grab.id, reason)
            return False
        await grabs_storage.set_state(
            db, grab.id, grabs_storage.STATE_FETCHED,
        )

    fetch_result: GrabResult = await deps.fetch_torrent(
        grab.mam_torrent_id, token
    )

    if not fetch_result.success:
        new_state = _grab_failure_state(fetch_result)
        await grabs_storage.set_state(
            db,
            grab.id,
            new_state,
            failed_reason=fetch_result.failure_detail,
            failed_with_cookie_id=dispatch.failed_cookie_id(new_state, token),
        )
        _log.info(
            "cookie retry: grab_id=%d re-fetch failed (%s: %s)",
            grab.id, fetch_result.failure_kind, fetch_result.failure_detail,
        )
        return False

    torrent_bytes = fetch_result.torrent_bytes or b""
    try:
        qbit_hash = info_hash(torrent_bytes)
    except BencodeError as e:
        await grabs_storage.set_state(
            db,
            grab.id,
            grabs_storage.STATE_FAILED_QBIT_REJECTED,
            failed_reason=f"unparseable torrent file: {e}",
        )
        return False

    # The dispatcher's own tail: staggered qBit add with the configured
    # save path and tags, or queue with the bytes saved (ADR-0022).
    placed = await dispatch._place_torrent(
        deps, db,
        grab_id=grab.id,
        announce_id=grab.announce_id or 0,
        action=rate_decision.action,
        rate_reason=rate_decision.reason,
        torrent_bytes=torrent_bytes,
        qbit_hash=qbit_hash,
        torrent_name=grab.torrent_name,
        author_blob=grab.author_blob,
        category=grab.category,
    )
    if placed.action == "submit" and placed.reason == "ok":
        _log.info(
            "cookie retry: grab_id=%d submitted to qBit (hash=%s)",
            grab.id, qbit_hash,
        )
        return True
    if placed.action == "queue" and placed.reason != "queue_save_failed":
        _log.info("cookie retry: grab_id=%d queued (%s)", grab.id, placed.reason)
        return True
    _log.info(
        "cookie retry: grab_id=%d qBit submit failed (%s: %s)",
        grab.id, placed.reason.removeprefix("client_failed:"), placed.error,
    )
    return False


def _grab_failure_state(result: GrabResult) -> str:
    """Map a GrabResult.failure_kind to a grabs.state value."""
    kind = result.failure_kind
    if kind == "cookie_expired":
        return grabs_storage.STATE_FAILED_COOKIE_EXPIRED
    if kind == "torrent_not_found":
        return grabs_storage.STATE_FAILED_TORRENT_GONE
    return grabs_storage.STATE_FAILED_UNKNOWN


# ─── The supervised loop ─────────────────────────────────────


async def run_loop(
    get_deps: Callable[[], Optional[DispatcherDeps]],
    *,
    interval_seconds: float = 300.0,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Long-running loop that retries cookie-expired grabs periodically.

    Default interval: 300s (5 minutes). The job is a no-op when there
    are no failed grabs, so this mostly affects latency between cookie
    rotation and automatic retry.

    `get_deps` is called at the start of every tick (main.py passes
    `lambda: state.dispatcher`), so a settings save that rebuilds the
    dispatcher reaches the next tick. None (shutdown) skips the tick.
    """
    _log.info("cookie retry loop started (interval=%.0fs)", interval_seconds)
    while True:
        deps = get_deps()
        if deps is None:
            await asyncio.sleep(interval_seconds)
            continue
        result = await tick(deps)
        if result.retried:
            _log.info(
                "cookie retry tick: found=%d retried=%d "
                "succeeded=%d failed=%d",
                result.found, result.retried,
                result.succeeded, result.failed_again,
            )
        elif result.error:
            _log.warning("cookie retry tick error: %s", result.error)

        if stop_event is not None and stop_event.is_set():
            _log.info("cookie retry stop_event signaled, exiting loop")
            return

        try:
            if stop_event is not None:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=interval_seconds
                )
                _log.info("cookie retry stop_event during sleep, exiting loop")
                return
            else:
                await asyncio.sleep(interval_seconds)
        except asyncio.TimeoutError:
            continue
