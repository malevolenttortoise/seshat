"""
Snatch budget watcher loop.

Periodically:
  1. Polls qBittorrent for the current state of every torrent in
     the watch category
  2. Calls `ledger.reconcile_with_qbit()` to update seedtimes and
     release rows that have hit the threshold (or vanished from qBit)
  3. As long as the ledger has freed-up budget, pops grabs from
     `pending_queue` and submits them to qBit, recording each in
     the ledger

This is the function that turns the static "park grabs in a queue
when budget is full" logic into a real flow that actually drains
the queue when MAM seedtime catches up. Without it, queued grabs
would sit forever — the dispatcher only ever ENQUEUES.

The loop is designed for the same supervised-task wrapper as the
IRC listener: it's an infinite `while not stop.is_set()` body that
sleeps between iterations, can be cancelled cleanly, and never
raises out of the body (everything is logged and the loop continues).

The loop body is split into a separate `tick()` function so tests
can drive one cycle at a time without dealing with timers.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from app.clients.base import TorrentClient
from app.mam.grab import GrabResult
from app.mam.torrent_meta import BencodeError, info_hash
from app.orchestrator.dispatch import DispatcherDeps
from app.orchestrator.download_folders import translate_path
from app.orchestrator.download_watcher import (
    TorrentSnap,
    adopt_orphan_torrents,
    check_for_completions,
)
from app.orchestrator.pipeline import process_completion
from app.rate_limit import ledger as ledger_mod
from app.rate_limit import queue as queue_mod
from app.storage import grabs as grabs_storage

_log = logging.getLogger("seshat.orchestrator.budget_watcher")


@dataclass(frozen=True)
class TickResult:
    """Outcome of one budget watcher cycle.

    Used by both the dashboard mirror and the test suite. The
    counters describe what happened in this iteration only — the
    long-running loop accumulates them as it goes.
    """

    qbit_torrents_seen: int
    seedtime_released: int
    removed_released: int
    queue_pops_attempted: int
    queue_pops_submitted: int
    queue_pops_failed: int
    error: Optional[str] = None
    # True iff the qBit call this tick returned successfully with
    # an authenticated session. Used by the SSE `client-status`
    # publisher in `run_loop`.
    qbit_reachable: bool = True


async def tick(deps: DispatcherDeps) -> TickResult:
    """Run one full budget-watcher cycle.

    Splits cleanly into three phases:
      1. Snapshot qBit (`qbit.list_torrents`)
      2. Reconcile the ledger (`ledger.reconcile_with_qbit`)
      3. Drain the queue while budget has room

    All errors are caught and stuffed into `TickResult.error` so the
    outer supervised loop never raises out of `tick`.
    """
    db = await deps.db_factory()
    try:
        return await _tick_inner(deps, db)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _log.exception("budget watcher tick failed")
        return TickResult(
            qbit_torrents_seen=0,
            seedtime_released=0,
            removed_released=0,
            queue_pops_attempted=0,
            queue_pops_submitted=0,
            queue_pops_failed=0,
            error=f"{type(e).__name__}: {e}",
            qbit_reachable=False,
        )
    finally:
        await db.close()


async def _tick_inner(deps: DispatcherDeps, db) -> TickResult:
    # ── Phase 1: snapshot qBit ──────────────────────────────
    qbit_torrents = await deps.qbit.list_torrents(category=deps.qbit_category)
    qbit_seen = len(qbit_torrents)
    snapshot = {t.hash: t.seeding_seconds for t in qbit_torrents if t.hash}

    # Fan out `torrent-progress` events to any SSE subscribers. Only
    # torrents whose progress/state/dlspeed actually changed since the
    # last tick get an event (first tick after process start emits all
    # active torrents — fine, that paints the initial UI). Guarded
    # against raising out of the tick; SSE delivery is best-effort.
    try:
        from app.orchestrator import sse_publishers
        await sse_publishers.publish_torrent_progress(qbit_torrents)
    except Exception:
        _log.exception("torrent-progress SSE publish failed (non-fatal)")

    # Count manual/Autobrr adds — any torrent in the watched category
    # that doesn't have a ledger row AND hasn't yet reached the
    # seedtime threshold. Torrents that have seeded past the threshold
    # are "released" from MAM's perspective and don't count against
    # the snatch budget, even if Seshat didn't submit them.
    try:
        known_hashes = {row.qbit_hash for row in await ledger_mod.list_active(db)}
        extras = 0
        for t in qbit_torrents:
            if t.hash and t.hash not in known_hashes:
                if t.seeding_seconds < deps.seed_seconds_required:
                    extras += 1
        from app import state as _state
        _state._snatch_budget["qbit_extras"] = extras
        _state._snatch_budget["last_updated_at"] = None  # touched; UI mirror
    except Exception:
        _log.exception("manual qBit extras count failed (non-fatal)")

    # ── Phase 1b: check for download completions ────────────
    # Adopt orphan torrents first so manually-added books get a grab
    # row in time for `check_for_completions` on this same tick. The
    # adopt pass creates state=submitted rows; the check pass then
    # sees each one and fires the pipeline if the download is done.
    #
    # `qbit_orphan_adoption_since` is the grandfather line — only
    # torrents added to qBit AFTER this Unix timestamp get adopted.
    # See DEFAULT_SETTINGS for why this filter exists (early Phase 6
    # build adopted every pre-existing torrent in the watch category
    # on first tick, flooding the review queue).
    try:
        adopted = await adopt_orphan_torrents(
            db, qbit_torrents,
            adoption_cutoff=deps.qbit_orphan_adoption_since,
        )
        if adopted:
            _log.info(
                "budget watcher: adopted %d orphan qBit torrent(s) "
                "into the grabs table", adopted,
            )
    except Exception:
        _log.exception("orphan adoption failed (non-fatal)")

    # Build the richer snapshot that the download watcher needs.
    # Translate qBit's save_path from qBit's container namespace
    # to Seshat's container namespace so the pipeline can find files.
    dl_snapshot = {
        t.hash: TorrentSnap(
            state=t.state,
            save_path=translate_path(
                t.save_path, deps.qbit_path_prefix, deps.local_path_prefix
            ),
        )
        for t in qbit_torrents if t.hash
    }
    try:
        completions = await check_for_completions(db, dl_snapshot)
        if completions:
            _log.debug(
                "budget watcher: %d new download completion(s) detected",
                len(completions),
            )
            for event in completions:
                # Ask qBit for the exact file list before handing the
                # event to the pipeline. Without this, the pipeline
                # was forced to guess the on-disk filename from the
                # announce torrent_name — which breaks whenever qBit /
                # MAM writes a different name (`Infinite Warship`
                # announce → `Infinite_Warship_-_Scott_Bartlett.epub`
                # on disk) or a multi-file torrent drops loose files
                # into the save_path. Empty list = client couldn't
                # introspect; pipeline falls back to the old heuristic.
                try:
                    torrent_files = await deps.qbit.list_torrent_files(
                        event.qbit_hash
                    )
                except Exception:
                    _log.exception(
                        "budget watcher: qbit.list_torrent_files failed "
                        "for grab_id=%d (non-fatal)", event.grab_id,
                    )
                    torrent_files = []

                try:
                    await process_completion(
                        db, event,
                        staging_path=deps.staging_path,
                        default_sink=deps.default_sink,
                        calibre_library_path=deps.calibre_library_path,
                        folder_sink_path=deps.folder_sink_path,
                        audiobookshelf_library_path=deps.audiobookshelf_library_path,
                        abs_base_url=deps.abs_base_url,
                        abs_api_key=deps.abs_api_key,
                        abs_library_id=deps.abs_library_id,
                        cwa_ingest_path=deps.cwa_ingest_path,
                        category_routing=deps.category_routing,
                        ntfy_url=deps.ntfy_url,
                        ntfy_topic=deps.ntfy_topic,
                        auto_train_enabled=deps.auto_train_enabled,
                        review_queue_enabled=deps.review_queue_enabled,
                        review_staging_path=deps.review_staging_path,
                        per_event_notifications=deps.per_event_notifications,
                        metadata_enricher=deps.metadata_enricher,
                        torrent_files=torrent_files,
                        audiobook_format_priority=deps.audiobook_format_priority,
                    )
                except Exception:
                    _log.exception(
                        "pipeline processing failed for grab_id=%d (non-fatal)",
                        event.grab_id,
                    )
    except Exception:
        _log.exception("download completion check failed (non-fatal)")

    # ── Phase 2: reconcile the ledger ───────────────────────
    summary = await ledger_mod.reconcile_with_qbit(
        db, snapshot, seed_seconds_required=deps.seed_seconds_required
    )

    # ── Phase 3: drain the queue while budget has room ──────
    pops_attempted = 0
    pops_submitted = 0
    pops_failed = 0

    while True:
        budget_used = await ledger_mod.count_effective(db)
        if budget_used >= deps.budget_cap:
            break

        next_grab = await queue_mod.pop_next(db)
        if next_grab is None:
            break

        pops_attempted += 1
        ok = await _resubmit_queued_grab(deps, db, next_grab.grab_id)
        if ok:
            pops_submitted += 1
        else:
            pops_failed += 1

    return TickResult(
        qbit_torrents_seen=qbit_seen,
        seedtime_released=summary["released_seedtime"],
        removed_released=summary["released_removed"],
        queue_pops_attempted=pops_attempted,
        queue_pops_submitted=pops_submitted,
        queue_pops_failed=pops_failed,
        # Session is live IFF the client flagged itself authenticated
        # during this tick. Empty-category installs will still report
        # reachable=True because list_torrents doesn't reset the flag
        # on a successful 200.
        qbit_reachable=bool(getattr(deps.qbit, "_logged_in", True)),
    )


async def _resubmit_queued_grab(
    deps: DispatcherDeps, db, grab_id: int
) -> bool:
    """Re-fetch a queued grab's .torrent file and submit to qBit.

    Phase 1 design choice: queued grabs do NOT persist their
    .torrent bytes to disk (see the dispatcher comment for the
    rationale). The budget watcher re-fetches at pop time. The
    cost is one extra MAM HTTP request per pop; the benefit is
    that a Seshat crash never leaves orphan .torrent files lying
    around in the data dir.

    Returns True on successful submission, False on any failure.
    Failed grabs are marked with the appropriate `failed_*` state
    so the cookie-rotation retry job can find them.
    """
    grab = await grabs_storage.get_grab(db, grab_id)
    if grab is None:
        _log.warning(
            f"budget watcher: queued grab_id={grab_id} not found in grabs table"
        )
        return False

    fetch_result: GrabResult = await deps.fetch_torrent(
        grab.mam_torrent_id, deps.mam_token
    )

    if not fetch_result.success:
        failed_state = _grab_failure_state(fetch_result)
        await grabs_storage.set_state(
            db,
            grab_id,
            failed_state,
            failed_reason=fetch_result.failure_detail,
        )
        _log.debug(
            f"budget watcher: queued grab_id={grab_id} fetch failed "
            f"({fetch_result.failure_kind}: {fetch_result.failure_detail})"
        )
        return False

    torrent_bytes = fetch_result.torrent_bytes or b""
    try:
        qbit_hash = info_hash(torrent_bytes)
    except BencodeError as e:
        await grabs_storage.set_state(
            db,
            grab_id,
            grabs_storage.STATE_FAILED_QBIT_REJECTED,
            failed_reason=f"unparseable torrent file: {e}",
        )
        return False

    # Compute the save path based on the folder structure setting —
    # same logic as the dispatcher's submit path. Without this, queued
    # grabs land in the bare download root instead of the organized
    # subfolder, and the pipeline scans the entire root for files.
    save_path = None
    if deps.qbit_download_path:
        from app.orchestrator.download_folders import (
            compute_download_folder,
            ensure_folder_exists,
            translate_path,
        )
        save_path = compute_download_folder(
            deps.qbit_download_path,
            deps.download_folder_structure,
            author_name=grab.author_blob if grab else "",
        )
        if save_path:
            local_save_path = translate_path(
                save_path, deps.qbit_path_prefix, deps.local_path_prefix
            )
            ensure_folder_exists(local_save_path)

    add_result = await deps.qbit.add_torrent(
        torrent_bytes, category=deps.qbit_category,
        save_path=save_path,
        tags=deps.qbit_tags or None,
    )

    if not add_result.success:
        failed_state = (
            grabs_storage.STATE_FAILED_QBIT_REJECTED
            if add_result.failure_kind == "rejected"
            else grabs_storage.STATE_FAILED_UNKNOWN
        )
        await grabs_storage.set_state(
            db,
            grab_id,
            failed_state,
            failed_reason=add_result.failure_detail,
            qbit_hash=qbit_hash,
        )
        _log.debug(
            f"budget watcher: queued grab_id={grab_id} qBit submit failed "
            f"({add_result.failure_kind}: {add_result.failure_detail})"
        )
        return False

    await grabs_storage.set_state(
        db,
        grab_id,
        grabs_storage.STATE_SUBMITTED,
        qbit_hash=qbit_hash,
    )
    await ledger_mod.record_grab(db, grab_id, qbit_hash)
    _log.debug(
        f"budget watcher: queued grab_id={grab_id} submitted to qBit "
        f"(hash={qbit_hash})"
    )
    return True


def _grab_failure_state(result: GrabResult) -> str:
    """Map a GrabResult.failure_kind to a `grabs.state` value.

    Same logic as the dispatcher's helper. Duplicated rather than
    imported because the dispatcher's version is private and
    pulling it across module boundaries would couple the watcher
    to dispatch internals more tightly than is healthy.
    """
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
    interval_seconds: float = 60.0,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Long-running loop that calls `tick()` on a fixed interval.

    Designed to be wrapped in `app.state.supervised_task()` from the
    main lifespan. Cancellation propagates cleanly via the
    `asyncio.CancelledError` re-raise inside `tick()`.

    `stop_event` is an opt-in early-exit signal — useful for the
    smoke test that wants to run exactly N ticks then bail. The
    real lifespan doesn't pass one and lets `supervised_task` cancel
    the surrounding asyncio task at shutdown instead.
    """
    _log.info(f"budget watcher started (interval={interval_seconds}s)")
    consecutive_auth_failures = 0
    while True:
        result = await tick(deps)
        # Push the client-status transition BEFORE the rest of the loop
        # body so even a `continue` (auth backoff) still notifies the UI
        # that qBit went offline. The publisher transition-gates itself
        # so steady-state reachable=True isn't re-broadcast every tick.
        try:
            from app.orchestrator import sse_publishers
            await sse_publishers.publish_client_status(result.qbit_reachable)
        except Exception:
            _log.exception("client-status SSE publish failed (non-fatal)")

        if result.queue_pops_submitted or result.seedtime_released or result.removed_released:
            _log.info(
                f"budget watcher tick: qbit_seen={result.qbit_torrents_seen} "
                f"released_seedtime={result.seedtime_released} "
                f"released_removed={result.removed_released} "
                f"pops={result.queue_pops_submitted}/{result.queue_pops_attempted}"
            )
            consecutive_auth_failures = 0
        elif result.error:
            _log.warning(f"budget watcher tick error: {result.error}")
            # Back off exponentially on auth failures (403 ban, wrong creds)
            # to avoid hammering qBit and extending the IP ban.
            if "auth" in result.error.lower() or "403" in result.error or "banned" in result.error.lower():
                consecutive_auth_failures += 1
                backoff = min(interval_seconds * (2 ** consecutive_auth_failures), 3600)
                _log.warning(
                    f"budget watcher: qBit auth failure #{consecutive_auth_failures}, "
                    f"backing off {backoff:.0f}s (check qBit credentials in Settings)"
                )
                await asyncio.sleep(backoff)
                continue
            else:
                consecutive_auth_failures = 0

        if stop_event is not None and stop_event.is_set():
            _log.info("budget watcher stop_event signaled, exiting loop")
            return

        try:
            if stop_event is not None:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=interval_seconds
                )
                _log.info("budget watcher stop_event during sleep, exiting loop")
                return
            else:
                await asyncio.sleep(interval_seconds)
        except asyncio.TimeoutError:
            continue  # interval elapsed normally
