"""
Pending-holds storage (v2.9.0 format-priority dedup).

A "hold" is a parked announce. When a disabled-format announce arrives
with no in-flight or owned sibling of the same book, the dispatcher
doesn't grab it immediately — instead it inserts a row here with
`release_at = now + format_dedup_hold_seconds`. The `hold_release`
scheduler tick wakes expired holds and re-evaluates them:

  * Still no blocking sibling → `inject_grab` and mark released.
  * A higher-priority sibling appeared during the window → mark dropped.

Holds also get dropped synchronously by the dispatcher whenever a
higher-priority arrival preempts them (the Delves case: AZW3 sits in
a hold, EPUB arrives 57s later, AZW3 hold dies before its timer fires).

Invariant: at most one row per `dedup_key` is in `state = 'pending'`
at any time. The dispatcher enforces this by preempting any existing
lower-priority hold for the same dedup_key when inserting a new one.
The DB schema doesn't enforce uniqueness because a held row + several
resolved rows (released / dropped) for the same dedup_key over time
is a legitimate audit history.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import aiosqlite

_log = logging.getLogger("seshat.storage.holds")


STATE_PENDING = "pending"
STATE_RELEASED = "released"
STATE_DROPPED = "dropped"


@dataclass(frozen=True)
class HoldRow:
    """One row from `pending_holds`, hydrated for the scheduler."""
    id: int
    announce_id: Optional[int]
    dedup_key: str
    media_type: str
    book_format: str
    torrent_id: str
    torrent_name: str
    category: Optional[str]
    author_blob: Optional[str]
    release_at: str
    state: str


def _utc_now_iso() -> str:
    """ISO-8601 UTC stamp matching SQLite's `datetime('now')` output.

    SQLite stores `datetime('now')` as "YYYY-MM-DD HH:MM:SS" in UTC.
    We compute release_at in Python (so we can add hold_seconds) and
    match that exact format for lexicographic comparisons in WHERE
    clauses to be correct.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _utc_future_iso(seconds: int) -> str:
    """ISO-8601 UTC stamp `seconds` in the future, same shape as above."""
    when = datetime.now(timezone.utc) + timedelta(seconds=int(seconds))
    return when.strftime("%Y-%m-%d %H:%M:%S")


async def create_hold(
    db: aiosqlite.Connection,
    *,
    announce_id: Optional[int],
    dedup_key: str,
    media_type: str,
    book_format: str,
    torrent_id: str,
    torrent_name: str,
    category: str,
    author_blob: str,
    hold_seconds: int,
) -> int:
    """Insert a `pending_holds` row scheduled to release in `hold_seconds`.

    Returns the new row id. The caller is responsible for serializing
    this insert with the sibling-lookup it relies on (use BEGIN
    IMMEDIATE around the lookup + insert) so two concurrent announces
    for the same dedup_key can't both create a hold.
    """
    release_at = _utc_future_iso(hold_seconds)
    cursor = await db.execute(
        """
        INSERT INTO pending_holds
            (announce_id, dedup_key, media_type, book_format,
             torrent_id, torrent_name, category, author_blob,
             release_at, state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            announce_id,
            dedup_key,
            media_type,
            (book_format or "").lower(),
            torrent_id,
            torrent_name,
            category or None,
            author_blob or None,
            release_at,
            STATE_PENDING,
        ),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def drop_holds(
    db: aiosqlite.Connection,
    hold_ids: Iterable[int],
    *,
    reason: str,
) -> int:
    """Mark a batch of pending holds as state='dropped'.

    Used by:
      * the dispatcher when a higher-priority arrival preempts a
        lower-priority hold (synchronous preempt, reason like
        "preempted_by_grab_<n>").
      * the scheduler tick when a hold's re-evaluation says skip
        (reason="released_blocked_by_sibling" or similar).

    Returns the number of rows actually marked. Idempotent — holds
    already in a terminal state are a no-op.
    """
    ids = [int(i) for i in hold_ids if i is not None]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cursor = await db.execute(
        f"""
        UPDATE pending_holds
        SET state = '{STATE_DROPPED}',
            resolved_at = datetime('now'),
            resolution_reason = ?
        WHERE id IN ({placeholders})
          AND state = '{STATE_PENDING}'
        """,
        (reason, *ids),
    )
    await db.commit()
    return cursor.rowcount or 0


async def mark_released(
    db: aiosqlite.Connection,
    hold_id: int,
    *,
    reason: str = "timer_fired",
) -> None:
    """Mark a hold as state='released' (its timer fired and the
    scheduler successfully injected the grab).
    """
    await db.execute(
        f"""
        UPDATE pending_holds
        SET state = '{STATE_RELEASED}',
            resolved_at = datetime('now'),
            resolution_reason = ?
        WHERE id = ?
          AND state = '{STATE_PENDING}'
        """,
        (reason, hold_id),
    )
    await db.commit()


async def list_due(
    db: aiosqlite.Connection, *, now_iso: Optional[str] = None,
) -> list[HoldRow]:
    """Return every `pending_holds` row whose timer has fired.

    A "due" hold is one with `state='pending'` and `release_at <= now`.
    The string comparison works because we store both columns in the
    same fixed-width ISO-8601 format SQLite produces from `datetime('now')`.

    `now_iso` is overridable for tests so they can drive the clock
    without `freezegun`. Production passes None to use the SQLite-side
    `datetime('now')`.
    """
    if now_iso is None:
        now_iso = _utc_now_iso()
    cursor = await db.execute(
        f"""
        SELECT id, announce_id, dedup_key, media_type, book_format,
               torrent_id, torrent_name, category, author_blob,
               release_at, state
        FROM pending_holds
        WHERE state = '{STATE_PENDING}' AND release_at <= ?
        ORDER BY release_at ASC
        """,
        (now_iso,),
    )
    rows = await cursor.fetchall()
    return [
        HoldRow(
            id=r["id"],
            announce_id=r["announce_id"],
            dedup_key=r["dedup_key"],
            media_type=r["media_type"],
            book_format=r["book_format"],
            torrent_id=r["torrent_id"],
            torrent_name=r["torrent_name"],
            category=r["category"],
            author_blob=r["author_blob"],
            release_at=r["release_at"],
            state=r["state"],
        )
        for r in rows
    ]
