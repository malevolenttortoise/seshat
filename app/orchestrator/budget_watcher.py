"""
Snatch budget watcher loop.

Periodically:
  1. Polls qBittorrent for the current state of every torrent in
     the watch category
  2. Calls `ledger.reconcile_with_qbit()` to update seedtimes and
     release rows that have hit the threshold (or vanished from qBit)
  3. As long as the ledger has freed-up budget, pops grabs from
     `pending_queue` and submits their SAVED .torrent bytes to qBit,
     recording each in the ledger — never re-fetching from MAM
     (snatch safety, ADR-0022)

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

from app.clients.base import AddResult, TorrentClient
from app.mam.torrent_meta import BencodeError, info_hash
from app.orchestrator import torrent_store
from app.orchestrator.dispatch import DispatcherDeps, add_to_client
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
                        cwa_min_inter_book_seconds=deps.cwa_min_inter_book_seconds,
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
                        ebook_format_priority=deps.ebook_format_priority,
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
    # Queued grabs carry their .torrent bytes on disk (`torrent_store`),
    # so a pop never touches MAM's download endpoint. Before the bytes
    # go to qBit, one search-API call checks the torrent is still on
    # MAM: removed → fail the grab (in qBit it would sit at 0% forever);
    # check unreachable → HOLD the whole queue until the next tick (Mark,
    # Phase 0 kickoff) — the next grab would hit the same outage. A
    # missing saved file fails the grab loudly; it is never re-fetched.
    pops_attempted = 0
    pops_submitted = 0
    pops_failed = 0

    while True:
        budget_used = await ledger_mod.count_effective(db)
        if budget_used >= deps.budget_cap:
            break

        head = await queue_mod.peek_next(db)
        if head is None:
            break
        grab = await grabs_storage.get_grab(db, head.grab_id)
        if grab is None:
            _log.warning(
                "budget watcher: queued grab_id=%d not found in grabs table",
                head.grab_id,
            )
            await queue_mod.take(db, head.grab_id)
            continue

        torrent_bytes = torrent_store.load(grab.torrent_file_path)
        if torrent_bytes is None:
            if await queue_mod.take(db, grab.id):
                pops_attempted += 1
                pops_failed += 1
                await torrent_store.fail_missing_file(db, grab)
            continue

        liveness = await torrent_store.check_still_on_mam(
            grab.mam_torrent_id, deps.live_mam_token(),
        )
        if liveness == "unreachable":
            _log.info(
                "budget watcher: holding the queue — couldn't confirm "
                "tid=%s (grab_id=%d) is still on MAM; retrying next tick",
                grab.mam_torrent_id, grab.id,
            )
            break

        # Someone else (a delayed-rotation during an inject) may have
        # taken this grab while the liveness check was out.
        if not await queue_mod.take(db, grab.id):
            continue
        pops_attempted += 1

        if liveness == "removed":
            detail = (
                f"torrent {grab.mam_torrent_id} was removed from MAM while "
                "queued; not sent to qBit"
            )
            _log.warning("budget watcher: grab_id=%d %s", grab.id, detail)
            await grabs_storage.set_state(
                db, grab.id, grabs_storage.STATE_FAILED_TORRENT_GONE,
                failed_reason=detail,
            )
            torrent_store.discard(grab.torrent_file_path)
            await torrent_store.notify_grab_failed(grab.id, detail)
            pops_failed += 1
            continue

        outcome = await _resubmit_queued_grab(
            deps, db, grab, head, torrent_bytes,
        )
        if outcome == "submitted":
            pops_submitted += 1
        elif outcome == "held":
            break
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
    deps: DispatcherDeps,
    db,
    grab: grabs_storage.GrabRow,
    item: queue_mod.QueuedGrab,
    torrent_bytes: bytes,
) -> str:
    """Submit a popped grab's saved bytes to qBit.

    Returns "submitted", "held" (qBit unreachable: the grab is put back
    exactly where it was in the queue, bytes kept, and the drain stops
    for this tick), or "failed" (a terminal state; the saved file is
    deleted). The caller has already taken the grab out of the queue.
    """
    try:
        qbit_hash = info_hash(torrent_bytes)
    except BencodeError as e:
        await grabs_storage.set_state(
            db,
            grab.id,
            grabs_storage.STATE_FAILED_QBIT_REJECTED,
            failed_reason=f"unparseable torrent file: {e}",
        )
        torrent_store.discard(grab.torrent_file_path)
        return "failed"

    # Queued retries operate on raw IRC announce data — series +
    # title aren't known at this point. Template-mode segments
    # referencing {series}/{title} drop out as empty per the
    # template renderer's contract; {author} resolves normally.
    add_result = await add_to_client(
        deps,
        grab_id=grab.id,
        torrent_bytes=torrent_bytes,
        author_blob=grab.author_blob,
    )

    if not add_result.success:
        if add_result.failure_kind in ("auth_failed", "network_error"):
            await queue_mod.restore(db, item)
            _log.info(
                "budget watcher: qBit unreachable for queued grab_id=%d "
                "(%s); kept in the queue",
                grab.id, add_result.failure_kind,
            )
            return "held"
        await grabs_storage.set_state(
            db,
            grab.id,
            _add_failure_state(add_result),
            failed_reason=add_result.failure_detail,
            qbit_hash=qbit_hash,
        )
        torrent_store.discard(grab.torrent_file_path)
        _log.debug(
            f"budget watcher: queued grab_id={grab.id} qBit submit failed "
            f"({add_result.failure_kind}: {add_result.failure_detail})"
        )
        return "failed"

    await grabs_storage.set_state(
        db,
        grab.id,
        grabs_storage.STATE_SUBMITTED,
        qbit_hash=qbit_hash,
    )
    await ledger_mod.record_grab(db, grab.id, qbit_hash)
    torrent_store.discard(grab.torrent_file_path)
    _log.debug(
        f"budget watcher: queued grab_id={grab.id} submitted to qBit "
        f"(hash={qbit_hash})"
    )
    return "submitted"


def _add_failure_state(result: AddResult) -> str:
    """Map a permanent AddResult failure to a `grabs.state` value."""
    if result.failure_kind == "rejected":
        return grabs_storage.STATE_FAILED_QBIT_REJECTED
    if result.failure_kind == "duplicate":
        return grabs_storage.STATE_DUPLICATE_IN_QBIT
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
