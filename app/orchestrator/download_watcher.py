"""
Post-download completion detector.

`check_for_completions()` scans the qBit snapshot for grabs in
`submitted` state whose torrent has finished downloading (state is
no longer "downloading", "metaDL", "stalledDL", etc.). For each
newly-completed grab, it:

  1. Transitions the grab to `downloaded`
  2. Creates a `pipeline_runs` row in `staged` state
  3. Returns the list of newly-detected completions

It also retires grabs whose torrent has disappeared from qBit. The
snatch ledger already treats absence as a release
(`released_reason=removed_from_qbit`), but that verdict was never
mirrored into the grab state machine — so a torrent the user deleted
left its grab in `submitted` forever, re-checked on every tick. Once
`qbit_missing_grab_grace_hours` have passed the grab is moved to
`failed_torrent_gone`. See `check_for_completions` for the guards that
keep a transient empty snapshot from failing healthy grabs.

This module is called by the budget watcher's tick — not as a
separate polling loop — because both need the same qBit snapshot.
No extra HTTP round-trips.

qBit download states (torrent is still downloading):
  - downloading, forcedDL, metaDL, stalledDL, checkingDL,
    queuedDL, allocating, moving

qBit post-download states (torrent has finished):
  - uploading, forcedUP, pausedUP, stalledUP, checkingUP,
    queuedUP, stoppedUP

We treat any state NOT in the "downloading" set as "download complete".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

from app.config import load_settings
from app.storage import grabs as grabs_storage
from app.storage import pipeline as pipeline_storage

_log = logging.getLogger("seshat.orchestrator.download_watcher")

def _missing_duration_hours(grab, now: datetime) -> float | None:
    """Hours since `grab` was submitted, or None if that can't be determined.

    `submitted_at` is written by SQLite's `datetime('now')`, which is
    naive UTC — so it's compared against a UTC `now`, not local time.
    Returning None when the stamp is missing or unparseable means the
    caller leaves the grab alone: a grab we can't age is a grab we
    have no business failing.
    """
    raw = getattr(grab, "submitted_at", None)
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace(" ", "T"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (now - stamp).total_seconds() / 3600.0)


# qBit states that mean "still downloading / not yet complete".
_DOWNLOADING_STATES = frozenset({
    "downloading",
    "forcedDL",
    "metaDL",
    "stalledDL",
    "checkingDL",
    "queuedDL",
    "allocating",
    "moving",
    # qBit v5+ uses "stopped" generically
    "stoppedDL",
})


# ─── File-race auto-retry state ───────────────────────────────
#
# Race: qBit reports `seeding` (file transfer done) before it has
# finished moving the torrent into `save_path`. The pipeline runs,
# `_prepare_book` can't locate the expected filename, and
# `_fail()` marks the pipeline_run as failed with
# `"torrent files unavailable from client; no file matching ..."`.
# Seconds later qBit finishes the move, but the failed pipeline_run
# row is what `check_for_completions` sees — its existence is the
# gate that prevents reprocessing.
#
# Auto-retry: on each watcher tick, scan for pipeline_runs in that
# exact failure shape and past a short cooldown, delete the old
# failed run and re-create a fresh one so the pipeline re-tries
# against the now-moved files. A module-level counter keeps us from
# runaway-retrying a grab when the underlying issue isn't transient
# (e.g. torrent actually missing from qBit's mount). The counter
# resets on process restart by design — at that point an operator
# has restarted the container and is presumably watching for the
# next attempt anyway.
_FILE_RACE_RETRY_COOLDOWN_SECONDS = 30
_FILE_RACE_MAX_RETRIES = 2
_file_race_retry_counts: dict[int, int] = {}  # grab_id → count, process-local


@dataclass(frozen=True)
class CompletionEvent:
    """One detected download completion."""

    grab_id: int
    qbit_hash: str
    torrent_name: str
    save_path: str
    pipeline_run_id: int


async def adopt_orphan_torrents(
    db: aiosqlite.Connection,
    qbit_torrents: list,
    *,
    adoption_cutoff: float,
) -> int:
    """Create grab rows for torrents qBit has but Seshat doesn't know about.

    Handles the manual-add workflow: user downloads a .torrent from
    MAM and drops it into qBit directly (or any other non-Seshat
    tool puts a torrent in the watched category). Without a grabs
    row the download watcher silently ignores the completion, so
    manually-added books never flow through the pipeline.

    We insert one row per unknown qBit hash with state=submitted and
    qbit_hash set. `check_for_completions()` on the same tick picks
    up any that are already done; ones still downloading get caught
    on a future tick.

    `adoption_cutoff` is a Unix timestamp — only torrents with
    `added_on >= adoption_cutoff` are considered. This is load-
    bearing: without it, the first tick after deploying the adopter
    code re-adopts EVERY pre-existing torrent in the watch category
    (which on a long-running qBit instance can be thousands of
    already-processed books), flooding the review queue and
    re-staging files that were delivered months ago. The caller
    seeds the cutoff on first run to the current time, meaning only
    freshly-added torrents after the upgrade get adopted. Pass 0 to
    disable the time filter entirely (tests only).

    MAM-safe: zero outbound traffic. Pure local observation.

    `mam_torrent_id` is intentionally blank — we didn't fetch this
    from MAM, so there's no authoritative ID to record. Callers that
    need provenance can filter on `category = 'manual_add'`.
    """
    if not qbit_torrents:
        return 0

    cursor = await db.execute(
        "SELECT qbit_hash FROM grabs WHERE qbit_hash IS NOT NULL"
    )
    rows = await cursor.fetchall()
    known = {row["qbit_hash"] for row in rows if row["qbit_hash"]}

    adopted = 0
    for t in qbit_torrents:
        h = getattr(t, "hash", "") or ""
        if not h or h in known:
            continue
        added_on = int(getattr(t, "added_on", 0) or 0)
        if adoption_cutoff and added_on < adoption_cutoff:
            # Pre-existing torrent — predates the adopter feature
            # being enabled for this deployment. Silently skip so
            # long-standing qBit snapshots don't flood the pipeline.
            continue
        name = getattr(t, "name", "") or f"manual_{h[:12]}"
        grab_id = await grabs_storage.create_grab(
            db,
            announce_id=None,
            mam_torrent_id="",
            torrent_name=name,
            category="manual_add",
            author_blob="",
            state=grabs_storage.STATE_SUBMITTED,
        )
        await grabs_storage.set_state(
            db, grab_id, grabs_storage.STATE_SUBMITTED, qbit_hash=h,
        )
        adopted += 1
        _log.info(
            "download watcher: adopted orphan torrent grab_id=%d hash=%s name=%r",
            grab_id, h[:16], name,
        )

    return adopted


async def check_for_completions(
    db: aiosqlite.Connection,
    qbit_snapshot: dict[str, "TorrentSnap"],
) -> list[CompletionEvent]:
    """Detect grabs whose downloads have completed.

    Args:
        db: Open database connection.
        qbit_snapshot: Map of qbit_hash → TorrentSnap from the latest
                       qBit poll. Each snap has .state and .save_path.

    Returns list of CompletionEvent for newly-detected completions.
    """
    # Find all grabs in "submitted" state that have a qbit_hash.
    cursor = await db.execute(
        """
        SELECT id, announce_id, mam_torrent_id, torrent_name, category,
               author_blob, torrent_file_path, qbit_hash, state, grabbed_at,
               submitted_at, failed_reason
        FROM grabs
        WHERE state = ? AND qbit_hash IS NOT NULL
        """,
        (grabs_storage.STATE_SUBMITTED,),
    )
    rows = await cursor.fetchall()

    events: list[CompletionEvent] = []

    if rows:
        _log.debug(
            "download watcher: checking %d submitted grabs against %d qBit torrents",
            len(rows), len(qbit_snapshot),
        )

    # Give-up threshold for grabs whose torrent has vanished from qBit.
    # Read per-call rather than frozen at startup so turning it off
    # takes effect on the next tick — `load_settings()` is mtime-cached.
    #
    # Two guards keep this from failing healthy grabs:
    #
    #   * An EMPTY snapshot is never treated as evidence of removal. A
    #     qBit outage raises out of `list_torrents` before we get here,
    #     but a successful call returning zero torrents (wrong category,
    #     qBit mid-restore) would otherwise condemn every submitted grab
    #     at once. Absence only counts when qBit is reporting *something*.
    #   * `0` disables the sweep entirely, so there's an off switch that
    #     doesn't require a code change.
    grace_hours = 0.0
    if qbit_snapshot:
        try:
            grace_hours = float(
                load_settings().get("qbit_missing_grab_grace_hours", 0) or 0
            )
        except (TypeError, ValueError):
            grace_hours = 0.0
    now = datetime.now(timezone.utc)

    for row in rows:
        grab = grabs_storage._row_to_grab(row)
        if not grab.qbit_hash:
            continue

        snap = qbit_snapshot.get(grab.qbit_hash)
        if snap is None:
            missing_for = _missing_duration_hours(grab, now)
            if grace_hours > 0 and missing_for is not None and missing_for >= grace_hours:
                await grabs_storage.set_state(
                    db,
                    grab.id,
                    grabs_storage.STATE_FAILED_TORRENT_GONE,
                    failed_reason=(
                        f"torrent absent from qBit for {missing_for:.1f}h "
                        f"(grace {grace_hours:.1f}h)"
                    ),
                )
                _log.info(
                    "download watcher: grab_id=%d hash=%s gone from qBit for "
                    "%.1fh — marking %s",
                    grab.id, grab.qbit_hash[:16], missing_for,
                    grabs_storage.STATE_FAILED_TORRENT_GONE,
                )
            else:
                _log.debug(
                    "download watcher: grab_id=%d hash=%s not in qBit snapshot",
                    grab.id, grab.qbit_hash[:16],
                )
            continue

        if snap.state in _DOWNLOADING_STATES:
            _log.debug(
                "download watcher: grab_id=%d still downloading (state=%s)",
                grab.id, snap.state,
            )
            continue

        # Check if we already have a pipeline run for this grab.
        existing = await pipeline_storage.find_by_grab_id(db, grab.id)
        if existing is not None:
            # Already detected and pipeline started — skip.
            continue

        # Download is complete! Transition the grab and start the pipeline.
        await grabs_storage.set_state(
            db, grab.id, grabs_storage.STATE_DOWNLOADED
        )

        run_id = await pipeline_storage.create_run(
            db,
            grab_id=grab.id,
            qbit_hash=grab.qbit_hash,
            source_path=snap.save_path,
        )

        event = CompletionEvent(
            grab_id=grab.id,
            qbit_hash=grab.qbit_hash,
            torrent_name=grab.torrent_name,
            save_path=snap.save_path,
            pipeline_run_id=run_id,
        )
        events.append(event)
        _log.info(
            "download complete: grab_id=%d %s (hash=%s, path=%s)",
            grab.id, grab.torrent_name, grab.qbit_hash, snap.save_path,
        )

    # Second pass: retry pipeline runs stuck on the qBit file-move
    # race. Returns fresh CompletionEvents so the budget watcher
    # processes them in the same loop as new completions.
    retry_events = await _collect_file_race_retries(db, qbit_snapshot)
    events.extend(retry_events)

    return events


async def _collect_file_race_retries(
    db: aiosqlite.Connection,
    qbit_snapshot: dict[str, "TorrentSnap"],
) -> list[CompletionEvent]:
    """Scan for pipeline_runs that failed with the 'no file matching'
    error and re-queue them for another attempt.

    Gates: cooldown elapsed since the failure (>30s), retry count
    under limit (≤2), and qBit still has the torrent in a non-
    downloading state. Each eligible row has its failed pipeline_run
    deleted, a fresh one created, and a CompletionEvent emitted.

    The grab's state is unchanged — it's already in STATE_DOWNLOADED
    from the first attempt and stays there through the retry.
    """
    cursor = await db.execute(
        """
        SELECT g.id AS grab_id, g.qbit_hash, g.torrent_name,
               pr.id AS pr_id, pr.state_updated_at AS failed_at
        FROM grabs g
        JOIN pipeline_runs pr ON pr.grab_id = g.id
        WHERE g.state = ?
          AND pr.state = ?
          AND pr.error LIKE '%no file matching%'
        """,
        (grabs_storage.STATE_DOWNLOADED, pipeline_storage.PIPE_FAILED),
    )
    rows = await cursor.fetchall()
    events: list[CompletionEvent] = []
    if not rows:
        return events

    now = datetime.now(tz=timezone.utc)
    for row in rows:
        grab_id = row["grab_id"]
        qbit_hash = row["qbit_hash"]
        if not qbit_hash:
            continue

        # Cooldown check. SQLite's `datetime('now')` is UTC; parse
        # the stored `YYYY-MM-DD HH:MM:SS` string as UTC too.
        try:
            failed_at = datetime.fromisoformat(
                str(row["failed_at"]).replace(" ", "T")
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue  # malformed timestamp, skip defensively
        if (now - failed_at).total_seconds() < _FILE_RACE_RETRY_COOLDOWN_SECONDS:
            continue

        # Retry-count gate. Resets on process restart; see module
        # comment for the rationale.
        if _file_race_retry_counts.get(grab_id, 0) >= _FILE_RACE_MAX_RETRIES:
            continue

        # qBit snapshot gate. If qBit doesn't have the torrent any
        # more (deleted, moved, re-adding) there's nothing to retry
        # against. Also skip if it's somehow back in a downloading
        # state.
        snap = qbit_snapshot.get(qbit_hash)
        if snap is None or snap.state in _DOWNLOADING_STATES:
            continue

        # Eligible — bump counter, delete the failed run, emit a
        # fresh CompletionEvent. Log at INFO so operators can see
        # the retry happening during a file-race episode.
        _file_race_retry_counts[grab_id] = (
            _file_race_retry_counts.get(grab_id, 0) + 1
        )
        await pipeline_storage.delete_run(db, row["pr_id"])
        run_id = await pipeline_storage.create_run(
            db,
            grab_id=grab_id,
            qbit_hash=qbit_hash,
            source_path=snap.save_path,
        )
        events.append(CompletionEvent(
            grab_id=grab_id,
            qbit_hash=qbit_hash,
            torrent_name=row["torrent_name"],
            save_path=snap.save_path,
            pipeline_run_id=run_id,
        ))
        _log.info(
            "download watcher: retrying file-race failure grab_id=%d "
            "(attempt %d of %d, cooldown %.0fs elapsed)",
            grab_id, _file_race_retry_counts[grab_id],
            _FILE_RACE_MAX_RETRIES, (now - failed_at).total_seconds(),
        )
    return events


@dataclass(frozen=True)
class TorrentSnap:
    """Minimal snapshot of one qBit torrent for completion detection."""

    state: str
    save_path: str
