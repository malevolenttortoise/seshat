"""
Snatch ledger — tracks every torrent that's currently counting against
the MAM snatch budget.

Lifecycle of a ledger row:

  1. **`record_grab(grab_id, qbit_hash)`** — called by the orchestration
     layer right after a successful submission to qBittorrent.
     Inserts a row with `seeding_seconds=0`, `released_at=NULL`.
  2. **`update_seeding(qbit_hash, seeding_seconds)`** — called
     periodically by the budget watcher with fresh data from qBit's
     `/api/v2/torrents/info` response. Updates the row's seedtime
     and timestamps the check.
  3. **`mark_released(grab_id, reason)`** — flips `released_at` to
     now. Called either when a row hits the seed-time threshold
     (reason `"seedtime_reached"`) or when the torrent is no longer
     present in qBit (reason `"removed_from_qbit"`).

The "currently in budget" count is `count(* WHERE released_at IS NULL)` —
that's the number the queue manager and the dashboard read.

Every function in this module takes an `aiosqlite.Connection` parameter
explicitly. We don't open or close connections here — that's the
caller's responsibility, partly because the `init_db()` flow opens its
own connection and partly because the test fixture wants to verify
state across multiple operations on the same connection.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import aiosqlite

_log = logging.getLogger("seshat.rate_limit.ledger")


@dataclass(frozen=True)
class LedgerRow:
    """One row from the snatch_ledger table, mapped to a dataclass."""

    grab_id: int
    qbit_hash: str
    seeding_seconds: int
    last_check_at: Optional[str]
    released_at: Optional[str]
    released_reason: Optional[str]


# ─── Inserts and updates ─────────────────────────────────────


async def record_grab(
    db: aiosqlite.Connection, grab_id: int, qbit_hash: str
) -> None:
    """Insert a new ledger row for a freshly-submitted torrent.

    The row starts with `seeding_seconds=0` and `released_at=NULL`,
    so it immediately counts against the active budget. The
    `INSERT OR REPLACE` makes the operation idempotent — calling
    `record_grab` twice for the same grab_id (e.g. after a Seshat
    crash + restart) just resets the row to its initial state.
    """
    await db.execute(
        """
        INSERT OR REPLACE INTO snatch_ledger
            (grab_id, qbit_hash, seeding_seconds, last_check_at,
             released_at, released_reason)
        VALUES (?, ?, 0, NULL, NULL, NULL)
        """,
        (grab_id, qbit_hash),
    )
    await db.commit()


async def update_seeding(
    db: aiosqlite.Connection,
    qbit_hash: str,
    seeding_seconds: int,
) -> None:
    """Refresh `seeding_seconds` and `last_check_at` for one row.

    Called by the budget watcher with the value qBit just reported.
    Skips rows that are already released — once a torrent has hit
    the threshold or been removed, we don't bother updating it.
    """
    await db.execute(
        """
        UPDATE snatch_ledger
        SET seeding_seconds = ?,
            last_check_at = datetime('now')
        WHERE qbit_hash = ? AND released_at IS NULL
        """,
        (seeding_seconds, qbit_hash),
    )
    await db.commit()


async def mark_released(
    db: aiosqlite.Connection, grab_id: int, reason: str
) -> None:
    """Mark a row as released and stop it counting against budget."""
    await db.execute(
        """
        UPDATE snatch_ledger
        SET released_at = datetime('now'),
            released_reason = ?
        WHERE grab_id = ? AND released_at IS NULL
        """,
        (reason, grab_id),
    )
    await db.commit()


# ─── Queries ─────────────────────────────────────────────────


async def count_active(db: aiosqlite.Connection) -> int:
    """Return the number of rows currently counting against the budget."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM snatch_ledger WHERE released_at IS NULL"
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def count_effective(db: aiosqlite.Connection) -> int:
    """Budget count INCLUDING manual qBit torrents Seshat didn't submit.

    Reads the ledger-active count and adds `state._snatch_budget["qbit_extras"]`,
    which the budget watcher refreshes on every tick by diffing the
    current qBit snapshot against the ledger. Between ticks the
    cached number is a lower bound that drifts upward as new manual
    adds arrive — safer than ignoring them entirely because the
    budget watcher always reconciles on its next pass.

    MAM's own unsatisfied count is the floor when Seshat has a fresh
    read of it (`mam_floor`, 2026-10 audit issue 09): MAM keeps counting
    torrents Seshat can no longer see (removed from qBit before 72h).
    """
    active = await count_active(db)
    try:
        from app import state as _state
        extras = int(_state._snatch_budget.get("qbit_extras", 0) or 0)
    except Exception:
        extras = 0
    from app.rate_limit import mam_floor
    return mam_floor.floor_count(active + max(0, extras))


def effective_cap(setting_cap: int) -> int:
    """The cap every budget check uses: the setting, or MAM's limit if lower."""
    from app.rate_limit import mam_floor
    return mam_floor.effective_cap(setting_cap)


async def list_active(db: aiosqlite.Connection) -> list[LedgerRow]:
    """Every row that's still counting against the budget, oldest first.

    Used by the dashboard's snatch budget view. Sorted by `last_check_at`
    so the user can see which torrents are closest to releasing
    (those with the highest `seeding_seconds` are also the ones most
    recently updated).
    """
    cursor = await db.execute(
        """
        SELECT grab_id, qbit_hash, seeding_seconds, last_check_at,
               released_at, released_reason
        FROM snatch_ledger
        WHERE released_at IS NULL
        ORDER BY grab_id ASC
        """
    )
    rows = await cursor.fetchall()
    return [_row_to_ledger(r) for r in rows]


async def get_row(
    db: aiosqlite.Connection, grab_id: int
) -> Optional[LedgerRow]:
    """Fetch one row by grab_id, or None if no such row exists."""
    cursor = await db.execute(
        """
        SELECT grab_id, qbit_hash, seeding_seconds, last_check_at,
               released_at, released_reason
        FROM snatch_ledger
        WHERE grab_id = ?
        """,
        (grab_id,),
    )
    row = await cursor.fetchone()
    return _row_to_ledger(row) if row else None


# ─── Reconciliation: bridge between qBit poll and the ledger ─


async def reconcile_with_qbit(
    db: aiosqlite.Connection,
    qbit_torrents: dict[str, int],
    seed_seconds_required: int,
    never_seen_grace_seconds: int = 0,
) -> dict[str, int]:
    """Synchronize ledger state with the latest qBit snapshot.

    `qbit_torrents` is a dict mapping `qbit_hash → seeding_seconds`,
    derived from a `qbit.list_torrents(category=watch_category)` call.
    `seed_seconds_required` is the budget threshold (default 72*3600).

    For every active ledger row:
      - if the hash is in qbit_torrents → update seeding_seconds.
        If seeding_seconds >= threshold → mark released, reason
        `seedtime_reached`.
      - if the hash is NOT in qbit_torrents → the user removed it
        (or qBit lost it); mark released, reason `removed_from_qbit`.
        Except a row qBit has never listed (`last_check_at` NULL):
        that is usually a torrent qBit hasn't shown yet, seconds after
        the add (100 rows on prod were released 5–50s after their grab,
        2026-10 audit issue 09). It keeps counting until
        `never_seen_grace_seconds` have passed since the grab.

    Returns a small `dict[str, int]` summary the budget watcher can
    log: `{"updated": N, "released_seedtime": N, "released_removed": N}`.
    Pure read-then-write — no other state is touched.
    """
    summary = {"updated": 0, "released_seedtime": 0, "released_removed": 0}

    active = await list_active(db)

    for row in active:
        if (
            row.qbit_hash not in qbit_torrents
            and row.last_check_at is None
            and never_seen_grace_seconds > 0
            and await _seconds_since_grab(db, row.grab_id) < never_seen_grace_seconds
        ):
            continue
        if row.qbit_hash in qbit_torrents:
            new_seconds = qbit_torrents[row.qbit_hash]
            await update_seeding(db, row.qbit_hash, new_seconds)
            summary["updated"] += 1
            if new_seconds >= seed_seconds_required:
                await mark_released(db, row.grab_id, "seedtime_reached")
                summary["released_seedtime"] += 1
                _log.info(
                    f"snatch released by seedtime: grab_id={row.grab_id} "
                    f"hash={row.qbit_hash} seconds={new_seconds}"
                )
        else:
            await mark_released(db, row.grab_id, "removed_from_qbit")
            summary["released_removed"] += 1
            _log.info(
                f"snatch released by removal: grab_id={row.grab_id} "
                f"hash={row.qbit_hash} (no longer in qBit)"
            )

    return summary


# ─── Helpers ─────────────────────────────────────────────────


async def _seconds_since_grab(db: aiosqlite.Connection, grab_id: int) -> float:
    """Seconds since the grab was submitted (or grabbed).

    Unknown → infinity, so a row with no readable grab time is released
    as before rather than held forever.
    """
    cursor = await db.execute(
        "SELECT (julianday('now') - julianday(COALESCE(submitted_at, grabbed_at)))"
        " * 86400.0 FROM grabs WHERE id = ?",
        (grab_id,),
    )
    row = await cursor.fetchone()
    if row is None or row[0] is None:
        return float("inf")
    return float(row[0])


def _row_to_ledger(row) -> LedgerRow:
    return LedgerRow(
        grab_id=int(row["grab_id"]),
        qbit_hash=str(row["qbit_hash"] or ""),
        seeding_seconds=int(row["seeding_seconds"] or 0),
        last_check_at=row["last_check_at"],
        released_at=row["released_at"],
        released_reason=row["released_reason"],
    )
