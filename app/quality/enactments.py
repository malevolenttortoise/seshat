"""
Storage layer for `replacement_enactments` (v2.27.0 — Bundle A.2 Phase 5b).

Audit trail for every attempted file swap, regardless of outcome.
One INSERT on enact attempt; UPDATEs for `restored_at` (when the
operator restores from `.seshat-replaced/`) or `failed_at` (when
sink-remove failed and the soft-delete was rolled back).

Lifecycle (a single enactment row):
  * INSERT  on enact start (failed_at / restored_at both NULL)
  * UPDATE  failed_at + failed_reason  on rollback path
  * UPDATE  restored_at + restored_by  on user restore

Schema defined in `app/database.py` — `replacement_enactments`.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import aiosqlite

_log = logging.getLogger("seshat.quality.enactments")


async def record_enactment(
    db: aiosqlite.Connection,
    *,
    opportunity_id: int,
    acted_by: Optional[str],
    library_slug: str,
    owned_book_id_before: Optional[int],
    owned_path_before: Optional[str],
    owned_path_after: Optional[str],
    owned_size_bytes: Optional[int],
    candidate_path: Optional[str],
    candidate_size_bytes: Optional[int],
    sink_result: Optional[str],
) -> int:
    """Insert one enactment audit row at start of an enact attempt.

    Returns the new row id. Caller owns the surrounding commit.

    `owned_path_after` is the destination inside `.seshat-replaced/`
    (set at soft-delete time; pre-sink-remove). `failed_at` and
    `restored_at` are NULL on insert and patched via the update
    helpers below.
    """
    cursor = await db.execute(
        """
        INSERT INTO replacement_enactments (
            opportunity_id, enacted_at, acted_by, library_slug,
            owned_book_id_before, owned_path_before, owned_path_after,
            owned_size_bytes, candidate_path, candidate_size_bytes,
            sink_result
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            opportunity_id, time.time(), acted_by, library_slug,
            owned_book_id_before, owned_path_before, owned_path_after,
            owned_size_bytes, candidate_path, candidate_size_bytes,
            sink_result,
        ),
    )
    return int(cursor.lastrowid or 0)


async def mark_enactment_failed(
    db: aiosqlite.Connection,
    enactment_id: int,
    *,
    reason: str,
) -> bool:
    """Stamp `failed_at` + `failed_reason` on an existing enactment row.

    Used by the rollback path: when sink-remove fails after the
    soft-delete already happened, we move the file back, then call
    this to record what went wrong. The opportunity status stays
    `detected` so the operator can retry.
    """
    cursor = await db.execute(
        "UPDATE replacement_enactments "
        "SET failed_at = ?, failed_reason = ? "
        "WHERE id = ?",
        (time.time(), reason, enactment_id),
    )
    return (cursor.rowcount or 0) > 0


async def mark_enactment_restored(
    db: aiosqlite.Connection,
    enactment_id: int,
    *,
    restored_by: Optional[str],
) -> bool:
    """Stamp `restored_at` + `restored_by` on an existing enactment row.

    Called by the restore flow after the file has been moved back
    and the sink has been re-told about it. The opportunity status
    flips back to `detected`.
    """
    cursor = await db.execute(
        "UPDATE replacement_enactments "
        "SET restored_at = ?, restored_by = ? "
        "WHERE id = ?",
        (time.time(), restored_by, enactment_id),
    )
    return (cursor.rowcount or 0) > 0


async def get_enactment(
    db: aiosqlite.Connection,
    enactment_id: int,
) -> Optional[dict]:
    """Fetch one enactment row by id, or None."""
    cursor = await db.execute(
        "SELECT * FROM replacement_enactments WHERE id = ?",
        (enactment_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


async def latest_active_enactment(
    db: aiosqlite.Connection,
    opportunity_id: int,
) -> Optional[dict]:
    """Return the most-recent non-failed, non-restored enactment for an
    opportunity, or None.

    "Active" = `failed_at IS NULL AND restored_at IS NULL` — i.e., the
    enactment is currently in effect (file in `.seshat-replaced/`,
    library row removed). The restore flow targets this row.
    """
    cursor = await db.execute(
        "SELECT * FROM replacement_enactments "
        "WHERE opportunity_id = ? "
        "  AND failed_at IS NULL "
        "  AND restored_at IS NULL "
        "ORDER BY enacted_at DESC LIMIT 1",
        (opportunity_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


async def list_enactments(
    db: aiosqlite.Connection,
    *,
    opportunity_id: Optional[int] = None,
    library_slug: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """List enactments, newest first.

    Both filter args are optional; pass neither to get the full
    audit log (capped at `limit`).
    """
    where: list[str] = []
    params: list = []
    if opportunity_id is not None:
        where.append("opportunity_id = ?")
        params.append(opportunity_id)
    if library_slug is not None:
        where.append("library_slug = ?")
        params.append(library_slug)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    cursor = await db.execute(
        f"SELECT * FROM replacement_enactments "
        f"{where_clause} "
        f"ORDER BY enacted_at DESC LIMIT ?",
        params,
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]
