"""
CRUD for the `grabs` and `announces` tables.

Every dispatcher decision produces an `announces` audit row, and
every "yes, fetch this" decision produces a `grabs` row that tracks
state through the pipeline (fetched → submitted → completed / failed).

State machine for `grabs.state`:

    pending_queue → fetched → submitted → downloading → complete
                                       ↘ failed
                       ↘ failed_cookie_expired
                       ↘ failed_torrent_gone
                       ↘ failed_qbit_rejected
                       ↘ failed_unknown

The dispatcher writes the initial state at insert time; later
phases (the qBit poller, the post-download stages) update it as
the torrent moves through the rest of the pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import aiosqlite

from app.filter.gate import Decision

_log = logging.getLogger("seshat.storage.grabs")


# Grab states — kept as plain string constants rather than an Enum
# so SQL queries against them are obvious. The dispatcher and the
# tests both reference these by name.
STATE_PENDING_QUEUE = "pending_queue"
STATE_FETCHED = "fetched"
STATE_SUBMITTED = "submitted"
STATE_FAILED_COOKIE_EXPIRED = "failed_cookie_expired"
STATE_FAILED_TORRENT_GONE = "failed_torrent_gone"
STATE_FAILED_QBIT_REJECTED = "failed_qbit_rejected"
STATE_FAILED_UNKNOWN = "failed_unknown"
# qBit reported the torrent already exists in the client. Not a real
# failure (the torrent IS in qBit, which is what Seshat wanted),
# but the dispatcher couldn't verify the add it expected — the
# `qbit_hash` we computed via info_hash IS the existing torrent's
# hash, so the ledger entry is still meaningful. Future iteration
# could detect this as soft-success and stop counting it as failed.
STATE_DUPLICATE_IN_QBIT = "duplicate_in_qbit"

# Phase 2 post-download states.
STATE_DOWNLOADING = "downloading"
STATE_DOWNLOADED = "downloaded"
STATE_PROCESSING = "processing"
STATE_COMPLETE = "complete"


@dataclass(frozen=True)
class GrabRow:
    """One row from the `grabs` table."""

    id: int
    announce_id: Optional[int]
    mam_torrent_id: str
    torrent_name: str
    category: str
    author_blob: str
    torrent_file_path: Optional[str]
    qbit_hash: Optional[str]
    state: str
    grabbed_at: str
    submitted_at: Optional[str]
    failed_reason: Optional[str]


# ─── Announces (audit log) ───────────────────────────────────


async def record_announce(
    db: aiosqlite.Connection,
    *,
    raw: str,
    torrent_id: str,
    torrent_name: str,
    category: str,
    author_blob: str,
    decision: Decision,
    filetype: str = "",
) -> int:
    """Insert one row in the `announces` table.

    Called for EVERY announce the dispatcher sees, regardless of
    whether the filter allowed it. The `decision` field captures
    the filter outcome so the audit log + UI can show why a given
    announce was allowed or skipped.

    `filetype` is the lowercased IRC `Filetype: ( xxx )` value
    (epub / azw3 / m4b / ...). v2.9.0 persists this for audit so
    format-dedup decisions can be reviewed retroactively. Pre-v2.9.0
    callers that didn't pass it land an empty string, which the
    schema accepts via the column's NULL-able definition.
    """
    cursor = await db.execute(
        """
        INSERT INTO announces
            (raw, torrent_id, torrent_name, category, author_blob,
             decision, decision_reason, matched_author, filetype)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            raw,
            torrent_id,
            torrent_name,
            category,
            author_blob,
            decision.action,
            decision.reason,
            decision.matched_author,
            (filetype or "").lower() or None,
        ),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def update_announce_decision(
    db: aiosqlite.Connection,
    *,
    announce_id: int,
    action: str,
    reason: str,
) -> None:
    """Overwrite an existing announce row's decision + reason.

    Used by the v2.9.0 format-dedup gate, which runs AFTER the filter
    has already recorded its allow/skip verdict. When dedup overrides
    the outcome (filter said allow, dedup says skip or hold) we update
    the same audit row in-place rather than writing a second row —
    keeping one-row-per-announce makes the dashboard counts clean.
    """
    await db.execute(
        "UPDATE announces SET decision = ?, decision_reason = ? "
        "WHERE id = ?",
        (action, reason, announce_id),
    )
    await db.commit()


# ─── Grabs (lifecycle) ───────────────────────────────────────


async def create_grab(
    db: aiosqlite.Connection,
    *,
    announce_id: Optional[int],
    mam_torrent_id: str,
    torrent_name: str,
    category: str,
    author_blob: str,
    state: str,
    qbit_hash: Optional[str] = None,
    is_reingest: bool = False,
    book_format: str = "",
    dedup_key: str = "",
) -> int:
    """Insert a new row in the `grabs` table.

    Called by the dispatcher right after the filter says "allow".
    The initial state depends on what the dispatcher is about to do
    next: `STATE_FETCHED` for the immediate-submit path,
    `STATE_PENDING_QUEUE` for the queue-mode path.

    `qbit_hash` and `is_reingest` are populated up-front by the v2.8.0
    reingest path (which skips the MAM fetch + qBit submit phases —
    the file is already on disk, the hash is already known). Normal
    grab callers leave them at their defaults; `qbit_hash` gets
    stamped later via `set_state(STATE_SUBMITTED, qbit_hash=...)`.

    `book_format` and `dedup_key` are the v2.9.0 format-dedup fields.
    The dispatcher computes both at announce time (book_format from
    `announce.filetype`; dedup_key from
    `app.orchestrator.format_dedup.normalize_dedup_key`) and stamps
    them here so subsequent dedup-gate lookups can find this grab as
    an "in-flight sibling" via the `idx_grabs_dedup_key` index.
    Empty strings are written as NULL — pre-v2.9.0 grabs and reingests
    have no announce-time filetype hint.
    """
    cursor = await db.execute(
        """
        INSERT INTO grabs
            (announce_id, mam_torrent_id, torrent_name, category,
             author_blob, state, qbit_hash, is_reingest,
             book_format, dedup_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            announce_id,
            mam_torrent_id,
            torrent_name,
            category,
            author_blob,
            state,
            qbit_hash,
            1 if is_reingest else 0,
            (book_format or "").lower() or None,
            dedup_key or None,
        ),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def set_torrent_name(
    db: aiosqlite.Connection, grab_id: int, torrent_name: str,
) -> None:
    """Overwrite the human-readable torrent name on a grab row.

    Used by the review-queue edit flow when the user corrects a
    bad title (e.g. a `manual_inject_<id>` placeholder). The new
    value flows into the dashboard's Snatch Budget widget, the
    Recent Activity feed, and anywhere else that renders
    `grabs.torrent_name`.
    """
    await db.execute(
        "UPDATE grabs SET torrent_name = ? WHERE id = ?",
        (torrent_name, grab_id),
    )
    await db.commit()


async def set_state(
    db: aiosqlite.Connection,
    grab_id: int,
    state: str,
    *,
    failed_reason: Optional[str] = None,
    qbit_hash: Optional[str] = None,
    torrent_file_path: Optional[str] = None,
) -> None:
    """Transition a grab to a new state.

    Updates `state_updated_at` automatically and bumps `submitted_at`
    when transitioning to `STATE_SUBMITTED`. Optional fields are
    only written if explicitly passed (so re-calling for state-only
    updates doesn't clobber the hash or file path).
    """
    sets = ["state = ?", "state_updated_at = datetime('now')"]
    params: list = [state]

    if state == STATE_SUBMITTED:
        sets.append("submitted_at = datetime('now')")
    if failed_reason is not None:
        sets.append("failed_reason = ?")
        params.append(failed_reason)
    if qbit_hash is not None:
        sets.append("qbit_hash = ?")
        params.append(qbit_hash)
    if torrent_file_path is not None:
        sets.append("torrent_file_path = ?")
        params.append(torrent_file_path)

    params.append(grab_id)
    await db.execute(
        f"UPDATE grabs SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    await db.commit()


async def get_grab(
    db: aiosqlite.Connection, grab_id: int
) -> Optional[GrabRow]:
    """Fetch one grab by id."""
    cursor = await db.execute(
        """
        SELECT id, announce_id, mam_torrent_id, torrent_name, category,
               author_blob, torrent_file_path, qbit_hash, state, grabbed_at,
               submitted_at, failed_reason
        FROM grabs WHERE id = ?
        """,
        (grab_id,),
    )
    row = await cursor.fetchone()
    return _row_to_grab(row) if row else None


async def get_source_metadata(
    db: aiosqlite.Connection, grab_id: int
) -> Optional[str]:
    """Fetch the raw source_metadata JSON blob for a grab, if any.

    Separated from `get_grab` so the GrabRow dataclass stays narrow
    and callers that don't care about the blob (the vast majority)
    don't pay any cost. Only the pipeline's _prepare_book reads this
    column — to short-circuit the enricher when the submitter pre-
    baked metadata at submission time.
    """
    cursor = await db.execute(
        "SELECT source_metadata FROM grabs WHERE id = ?", (grab_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return row[0]  # None if column is NULL


async def find_grab_by_torrent_id(
    db: aiosqlite.Connection, mam_torrent_id: str
) -> Optional[GrabRow]:
    """Look up the most recent grab for a given MAM torrent ID.

    Used by the inject endpoint to detect "we already grabbed this
    one" before making another attempt.
    """
    cursor = await db.execute(
        """
        SELECT id, announce_id, mam_torrent_id, torrent_name, category,
               author_blob, torrent_file_path, qbit_hash, state, grabbed_at,
               submitted_at, failed_reason
        FROM grabs WHERE mam_torrent_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (mam_torrent_id,),
    )
    row = await cursor.fetchone()
    return _row_to_grab(row) if row else None


def _row_to_grab(row) -> GrabRow:
    return GrabRow(
        id=int(row["id"]),
        announce_id=row["announce_id"],
        mam_torrent_id=str(row["mam_torrent_id"] or ""),
        torrent_name=str(row["torrent_name"] or ""),
        category=str(row["category"] or ""),
        author_blob=str(row["author_blob"] or ""),
        torrent_file_path=row["torrent_file_path"],
        qbit_hash=row["qbit_hash"],
        state=str(row["state"] or ""),
        grabbed_at=str(row["grabbed_at"] or ""),
        submitted_at=row["submitted_at"],
        failed_reason=row["failed_reason"],
    )
