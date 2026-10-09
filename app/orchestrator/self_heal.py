"""
Stranded-grab self-heal (2026-10 audit wave 5a, G116–G120).

Two ways a grab could stop with nothing left to move it on:

  * **A restart mid-pipeline.** `process_completion` runs inside the budget
    watcher's tick, so a restart (a Force Update) or a cancelled tick leaves
    the grab's run in staged / extracted / metadata_done. The watcher only
    starts runs for `submitted` grabs and skips a grab that has any run, so
    nobody resumes it (grabs 4667–4669, 2026-10-09).
  * **A failed qBit submit whose torrent qBit has anyway.** The grab sits in
    `failed_unknown` / `duplicate_in_qbit` with its hash; the watcher skips the
    state and the orphan adopter skips the known hash (2026-07-13).

`sweep` runs on every budget-watcher tick, before `check_for_completions`,
against the same qBit snapshot:

  * A stranded run (not driven by this process, last change > 10 min ago):
    within 7 days → kept as `failed` ("interrupted by a restart …"), its
    half-staged review rows and staged copies removed, and a fresh run
    returned as a `CompletionEvent` for the watcher to process; at most two
    such re-runs per grab, then it's left failed and notified. Older than 7
    days → closed `failed`, never re-run (run 4304, 09-13, owned).
  * A `failed_unknown` / `duplicate_in_qbit` grab under 7 days old, no run,
    no other grab on its hash, which qBit lists complete → `submitted` plus
    its snatch-ledger row; `check_for_completions` takes it on the same tick.

Nothing here fetches a .torrent, adds one to qBit or talks to MAM: every
heal works from what qBit already has (ADR-0022). `sunk` runs are never
touched (the sink already has the book; a re-run would deliver it twice).
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiosqlite

from app.orchestrator.download_watcher import (
    _DOWNLOADING_STATES,
    CompletionEvent,
    TorrentSnap,
)
from app.orchestrator.file_copier import BOOK_EXTENSIONS, _safe_dirname
from app.rate_limit import ledger as ledger_mod
from app.storage import grabs as grabs_storage
from app.storage import pipeline as pipe_storage
from app.storage import review_queue as review_storage

_log = logging.getLogger("seshat.orchestrator.self_heal")

STRANDED_STATES = (
    pipe_storage.PIPE_STAGED,
    pipe_storage.PIPE_EXTRACTED,
    pipe_storage.PIPE_METADATA_DONE,
)
HEALABLE_GRAB_STATES = (
    grabs_storage.STATE_FAILED_UNKNOWN,
    grabs_storage.STATE_DUPLICATE_IN_QBIT,
)
MAX_AGE = timedelta(days=7)
MIN_IDLE = timedelta(minutes=10)
MAX_RERUNS = 2
INTERRUPTED = "interrupted by a restart"

ListFiles = Callable[[str], Awaitable[list[str]]]


async def sweep(
    db: aiosqlite.Connection,
    qbit_snapshot: dict[str, TorrentSnap],
    *,
    list_files: ListFiles,
    staging_path: str,
    now: Optional[datetime] = None,
) -> list[CompletionEvent]:
    """Heal what a restart or a failed submit stranded. Returns the fresh
    runs' events for the caller to push through the pipeline."""
    now = now or datetime.now(timezone.utc)
    events = await _rerun_stranded(
        db, qbit_snapshot, list_files=list_files,
        staging_path=staging_path, now=now,
    )
    await _heal_failed_submits(db, qbit_snapshot, now=now)
    return events


def _utc(stamp: str) -> Optional[datetime]:
    """SQLite's `datetime('now')` text (naive UTC) as an aware datetime."""
    try:
        parsed = datetime.fromisoformat(str(stamp).replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _rerun_stranded(
    db: aiosqlite.Connection,
    qbit_snapshot: dict[str, TorrentSnap],
    *,
    list_files: ListFiles,
    staging_path: str,
    now: datetime,
) -> list[CompletionEvent]:
    from app.orchestrator.pipeline import runs_in_flight

    events: list[CompletionEvent] = []
    driving = runs_in_flight()
    for run, changed_at in await pipe_storage.find_in_states(db, STRANDED_STATES):
        if run.id in driving:
            continue
        changed = _utc(changed_at)
        if changed is None or now - changed < MIN_IDLE:
            continue
        grab = await grabs_storage.get_grab(db, run.grab_id)
        if grab is None:
            continue
        if now - changed > MAX_AGE:
            await _close(
                db, run, f"{INTERRUPTED}; too old to re-run (stranded at "
                f"{run.state} since {changed_at} UTC)",
            )
            continue
        # An empty snapshot is no evidence the torrent left qBit (the same
        # guard `check_for_completions` keeps); try again next tick.
        if not qbit_snapshot:
            continue
        reruns = await pipe_storage.count_failed_with_error_prefix(
            db, run.grab_id, INTERRUPTED,
        )
        if reruns >= MAX_RERUNS:
            await _close(
                db, run, f"{INTERRUPTED} again; gave up after {MAX_RERUNS} "
                f"re-runs", notify=grab.torrent_name,
            )
            continue
        qbit_hash = run.qbit_hash or grab.qbit_hash or ""
        snap = qbit_snapshot.get(qbit_hash)
        if snap is None or snap.state in _DOWNLOADING_STATES:
            await _close(
                db, run, f"{INTERRUPTED}; its torrent isn't complete in qBit, "
                f"so it can't be re-run",
            )
            continue
        reviews = await review_storage.list_for_run(db, run.id)
        if any(r.status != review_storage.STATUS_PENDING for r in reviews):
            await _close(
                db, run, f"{INTERRUPTED} after part of it was reviewed; "
                f"finish it by hand",
            )
            continue

        for review in reviews:
            _remove_tree(review.staged_path)
            await review_storage.delete_entry(db, review.id)
        await _remove_staged_copies(
            run, grab.torrent_name, qbit_hash,
            list_files=list_files, staging_path=staging_path,
        )
        await pipe_storage.set_state(
            db, run.id, pipe_storage.PIPE_FAILED,
            error=f"{INTERRUPTED} at {run.state}; re-run "
            f"{reruns + 1} of {MAX_RERUNS}",
        )
        new_run = await pipe_storage.create_run(
            db, grab_id=run.grab_id, qbit_hash=qbit_hash,
            source_path=snap.save_path,
        )
        _log.info(
            "self-heal: grab_id=%d %s was stranded at %s (run %d) — re-running "
            "as run %d (%d of %d), from the files qBit already has",
            run.grab_id, grab.torrent_name, run.state, run.id, new_run,
            reruns + 1, MAX_RERUNS,
        )
        events.append(CompletionEvent(
            grab_id=run.grab_id,
            qbit_hash=qbit_hash,
            torrent_name=grab.torrent_name,
            save_path=snap.save_path,
            pipeline_run_id=new_run,
        ))
    return events


async def _close(
    db: aiosqlite.Connection,
    run: pipe_storage.PipelineRow,
    reason: str,
    *,
    notify: Optional[str] = None,
) -> None:
    """Mark a stranded run failed for good. The grab is left as it is."""
    await pipe_storage.set_state(db, run.id, pipe_storage.PIPE_FAILED, error=reason)
    _log.info(
        "self-heal: closed run %d (grab_id=%d, was %s): %s",
        run.id, run.grab_id, run.state, reason,
    )
    if notify is None:
        return
    try:
        from app.notifications import bus, events
        await bus.emit(
            events.PIPELINE_ERROR,
            title="Pipeline error",
            message=f"{notify}\n{reason}",
        )
    except Exception:
        _log.exception("pipeline.error bus emit failed (non-fatal)")


def _remove_tree(path: str) -> None:
    """Remove a half-staged review folder, and its grab-N parent once empty."""
    if not path:
        return
    target = Path(path)
    shutil.rmtree(target, ignore_errors=True)
    parent = target.parent
    if parent.name.startswith("grab-"):
        try:
            parent.rmdir()  # only succeeds when empty
        except OSError:
            pass


async def _remove_staged_copies(
    run: pipe_storage.PipelineRow,
    torrent_name: str,
    qbit_hash: str,
    *,
    list_files: ListFiles,
    staging_path: str,
) -> None:
    """Delete the copies this torrent left in its staging folder, so the
    re-run stages them under their own names (not `…_1.epub` beside them).

    Only this torrent's book files (qBit's file list), and only inside the
    configured staging root: with staging off, `staged_path` is qBit's own
    download folder, the seeding files.
    """
    if not staging_path or not staging_path.strip():
        return
    root = Path(staging_path).resolve()
    folder = Path(run.staged_path) if run.staged_path else root / _safe_dirname(torrent_name)
    folder = folder.resolve()
    if folder == root or root not in folder.parents:
        return
    try:
        rel_paths = await list_files(qbit_hash)
    except Exception:
        _log.exception("self-heal: qBit file list failed for %s", qbit_hash[:16])
        rel_paths = []
    removed = 0
    for rel in rel_paths:
        name = Path(rel).name
        if Path(name).suffix.lstrip(".").lower() not in BOOK_EXTENSIONS:
            continue
        copy = folder / name
        if copy.is_file():
            copy.unlink()
            removed += 1
    if removed:
        _log.info("self-heal: removed %d staged copy(ies) in %s", removed, folder)


async def _heal_failed_submits(
    db: aiosqlite.Connection,
    qbit_snapshot: dict[str, TorrentSnap],
    *,
    now: datetime,
) -> None:
    """A failed submit whose torrent qBit lists complete goes back to
    `submitted`, so the watcher runs the pipeline on it (G118)."""
    if not qbit_snapshot:
        return
    cutoff = (now - MAX_AGE).strftime("%Y-%m-%d %H:%M:%S")
    marks = ",".join("?" * len(HEALABLE_GRAB_STATES))
    cursor = await db.execute(
        f"""
        SELECT g.id, g.qbit_hash, g.state, g.torrent_name FROM grabs g
        WHERE g.state IN ({marks}) AND g.qbit_hash IS NOT NULL
          AND g.grabbed_at >= ?
          AND NOT EXISTS (SELECT 1 FROM pipeline_runs pr WHERE pr.grab_id = g.id)
          AND NOT EXISTS (SELECT 1 FROM grabs o
                          WHERE o.qbit_hash = g.qbit_hash AND o.id <> g.id)
        """,  # nosec B608 — placeholders only
        (*HEALABLE_GRAB_STATES, cutoff),
    )
    for row in await cursor.fetchall():
        snap = qbit_snapshot.get(row["qbit_hash"])
        if snap is None or snap.state in _DOWNLOADING_STATES:
            continue
        await grabs_storage.set_state(db, row["id"], grabs_storage.STATE_SUBMITTED)
        await ledger_mod.record_grab(db, row["id"], row["qbit_hash"])
        _log.info(
            "self-heal: grab_id=%d %s was %s but qBit has its torrent "
            "complete — back to submitted for the pipeline",
            row["id"], row["torrent_name"], row["state"],
        )
