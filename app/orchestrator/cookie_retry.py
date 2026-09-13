"""
Cookie-rotation retry loop.

When a grab fails with `failed_cookie_expired`, the torrent wasn't
fetched — the MAM session cookie was invalid at the time of the
attempt. Once the cookie has been rotated (either automatically via
the keep-alive loop, or manually by the user pasting a new cookie),
this job re-attempts every grab stuck in that state.

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
from typing import Optional

from app.mam.grab import GrabResult
from app.mam.torrent_meta import BencodeError, info_hash
from app.orchestrator.dispatch import DispatcherDeps
from app.rate_limit import ledger as ledger_mod
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

    For each one:
      1. Re-fetch the .torrent file with the current cookie
      2. If successful, parse the info_hash and submit to qBit
      3. Update the grab's state accordingly

    All errors are caught so the supervised loop never raises.
    """
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

    for grab in rows:
        retried += 1
        ok = await _retry_grab(deps, db, grab)
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


async def _find_cookie_expired_grabs(db) -> list[grabs_storage.GrabRow]:
    """Find all grabs in failed_cookie_expired state."""
    cursor = await db.execute(
        """
        SELECT id, announce_id, mam_torrent_id, torrent_name, category,
               author_blob, torrent_file_path, qbit_hash, state, grabbed_at,
               submitted_at, failed_reason
        FROM grabs
        WHERE state = ?
        ORDER BY id ASC
        """,
        (grabs_storage.STATE_FAILED_COOKIE_EXPIRED,),
    )
    rows = await cursor.fetchall()
    return [grabs_storage._row_to_grab(r) for r in rows]


async def _retry_grab(
    deps: DispatcherDeps, db, grab: grabs_storage.GrabRow
) -> bool:
    """Re-fetch and submit one previously-failed grab.

    Returns True on successful qBit submission, False otherwise.
    On failure, the grab's state is updated to reflect the new
    failure mode (which may differ from cookie_expired if, e.g.,
    the torrent has since been removed from MAM).
    """
    fetch_result: GrabResult = await deps.fetch_torrent(
        grab.mam_torrent_id, deps.live_mam_token()
    )

    if not fetch_result.success:
        new_state = _grab_failure_state(fetch_result)
        await grabs_storage.set_state(
            db,
            grab.id,
            new_state,
            failed_reason=fetch_result.failure_detail,
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

    add_result = await deps.qbit.add_torrent(
        torrent_bytes, category=deps.qbit_category
    )

    if not add_result.success:
        failed_state = (
            grabs_storage.STATE_DUPLICATE_IN_QBIT
            if add_result.failure_kind == "duplicate"
            else grabs_storage.STATE_FAILED_QBIT_REJECTED
            if add_result.failure_kind == "rejected"
            else grabs_storage.STATE_FAILED_UNKNOWN
        )
        await grabs_storage.set_state(
            db,
            grab.id,
            failed_state,
            failed_reason=add_result.failure_detail,
            qbit_hash=qbit_hash,
        )
        _log.info(
            "cookie retry: grab_id=%d qBit submit failed (%s: %s)",
            grab.id, add_result.failure_kind, add_result.failure_detail,
        )
        return False

    await grabs_storage.set_state(
        db,
        grab.id,
        grabs_storage.STATE_SUBMITTED,
        qbit_hash=qbit_hash,
    )
    await ledger_mod.record_grab(db, grab.id, qbit_hash)
    _log.info(
        "cookie retry: grab_id=%d submitted to qBit (hash=%s)",
        grab.id, qbit_hash,
    )
    return True


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
    deps: DispatcherDeps,
    *,
    interval_seconds: float = 300.0,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Long-running loop that retries cookie-expired grabs periodically.

    Default interval: 300s (5 minutes). The job is a no-op when there
    are no failed grabs, so this mostly affects latency between cookie
    rotation and automatic retry.
    """
    _log.info("cookie retry loop started (interval=%.0fs)", interval_seconds)
    while True:
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
