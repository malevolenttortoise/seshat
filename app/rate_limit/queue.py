"""
Pending grabs queue.

When the snatch budget is full and queue mode is enabled, an incoming
announce gets fetched (so we have the .torrent bytes locally) and
parked in `pending_queue` instead of being submitted to qBittorrent.
A budget watcher pops the highest-priority pending grab whenever a
`snatch_ledger` row releases.

The queue is FIFO by default — older queued grabs win — but every
row also has a `priority` integer column that overrides FIFO when
non-zero. Manually-injected and discovery-sent grabs can set higher
priority; the IRC dispatcher uses priority 0.

Pop ordering: highest priority first, then oldest queued_at first.
The `idx_pending_queue_priority` index in the schema makes this a
single-row indexed read.

Like `ledger.py`, every function takes an `aiosqlite.Connection`
explicitly. We don't open or close connections here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import aiosqlite

_log = logging.getLogger("seshat.rate_limit.queue")


@dataclass(frozen=True)
class QueuedGrab:
    """One row from the pending_queue table."""

    grab_id: int
    priority: int
    queued_at: str


# ─── Inserts and updates ─────────────────────────────────────


async def enqueue(
    db: aiosqlite.Connection,
    grab_id: int,
    priority: int = 0,
) -> None:
    """Park a grab in the pending queue.

    The grab row in the `grabs` table should already be in
    `pending_queue` state when this is called — the queue table is
    only the priority-ordering view, not the canonical state. The
    `INSERT OR REPLACE` makes re-queueing the same grab safe (the
    new priority and queued_at win).
    """
    await db.execute(
        """
        INSERT OR REPLACE INTO pending_queue (grab_id, priority, queued_at)
        VALUES (?, ?, datetime('now'))
        """,
        (grab_id, priority),
    )
    await db.commit()


async def remove(db: aiosqlite.Connection, grab_id: int) -> None:
    """Remove a grab from the queue (after popping or cancelling)."""
    await db.execute(
        "DELETE FROM pending_queue WHERE grab_id = ?",
        (grab_id,),
    )
    await db.commit()


async def take(db: aiosqlite.Connection, grab_id: int) -> bool:
    """Remove one specific grab; True only if THIS call removed it.

    The budget watcher peeks, runs a network liveness check, then takes
    — and a concurrent delayed-rotation may have evicted the same grab
    in between. False means someone else got it first: leave it alone.
    """
    cursor = await db.execute(
        "DELETE FROM pending_queue WHERE grab_id = ?",
        (grab_id,),
    )
    await db.commit()
    return (cursor.rowcount or 0) > 0


async def restore(db: aiosqlite.Connection, item: QueuedGrab) -> None:
    """Put a taken grab back exactly where it was (same priority and
    queued_at), for a submit that failed transiently."""
    await db.execute(
        """
        INSERT OR REPLACE INTO pending_queue (grab_id, priority, queued_at)
        VALUES (?, ?, ?)
        """,
        (item.grab_id, item.priority, item.queued_at),
    )
    await db.commit()


# ─── Queries ─────────────────────────────────────────────────


async def size(db: aiosqlite.Connection) -> int:
    """How many grabs are currently waiting in the queue?"""
    cursor = await db.execute("SELECT COUNT(*) FROM pending_queue")
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def peek_next(
    db: aiosqlite.Connection,
) -> Optional[QueuedGrab]:
    """Look at the next grab to pop without removing it.

    Used when we want to know "what would come next" without
    committing to popping it (e.g. for the dashboard preview).
    Ordering: highest priority first, then oldest queued_at first.
    """
    cursor = await db.execute(
        """
        SELECT grab_id, priority, queued_at
        FROM pending_queue
        ORDER BY priority DESC, queued_at ASC
        LIMIT 1
        """
    )
    row = await cursor.fetchone()
    return _row_to_queued(row) if row else None


async def pop_next(db: aiosqlite.Connection) -> Optional[QueuedGrab]:
    """Atomically remove and return the next grab.

    Returns None if the queue is empty. The atomicity matters: the
    budget watcher loop pops a grab, then submits it to qBit; we
    don't want a concurrent worker (the inject endpoint, say) to
    pop the same row.

    SQLite's WAL mode + the implicit transaction wrapping a single
    statement gives us this for free — DELETE...RETURNING is one
    statement, atomic against other writers waiting on busy_timeout.
    """
    cursor = await db.execute(
        """
        DELETE FROM pending_queue
        WHERE grab_id = (
            SELECT grab_id FROM pending_queue
            ORDER BY priority DESC, queued_at ASC
            LIMIT 1
        )
        RETURNING grab_id, priority, queued_at
        """
    )
    row = await cursor.fetchone()
    await db.commit()
    return _row_to_queued(row) if row else None


async def list_all(db: aiosqlite.Connection) -> list[QueuedGrab]:
    """Every queued grab, in pop order. Used by the dashboard."""
    cursor = await db.execute(
        """
        SELECT grab_id, priority, queued_at
        FROM pending_queue
        ORDER BY priority DESC, queued_at ASC
        """
    )
    rows = await cursor.fetchall()
    return [_row_to_queued(r) for r in rows]


# ─── Helpers ─────────────────────────────────────────────────


def _row_to_queued(row) -> QueuedGrab:
    return QueuedGrab(
        grab_id=int(row["grab_id"]),
        priority=int(row["priority"] or 0),
        queued_at=str(row["queued_at"] or ""),
    )
