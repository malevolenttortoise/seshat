"""
Storage layer for `replacement_opportunities` (v2.26.0 — A.2 Phase 5a).

Detection happens in `app/quality/replacement_detector.py` and writes
rows here. Phase 6 UI reads via `list_opportunities`; Phase 5b
destructive enactment will read + mutate the `status` column.

Status lifecycle:
  'detected' (default on insert)
    → 'enacted'   (Phase 5b: file swap completed)
    → 'dismissed' (user marked as not interesting)

Idempotency: the UNIQUE(candidate_grab_id, owned_library_slug,
owned_book_id) constraint means inserting the same opportunity twice
silently no-ops via INSERT OR IGNORE — the detector can rerun without
producing duplicate rows.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

import aiosqlite

_log = logging.getLogger("seshat.quality.opportunities")


# ─── Write path ──────────────────────────────────────────────


async def record_opportunity(
    db: aiosqlite.Connection,
    *,
    candidate_grab_id: int,
    candidate_mam_torrent_id: str,
    candidate_format: Optional[str],
    candidate_score: tuple[int, ...],
    owned_library_slug: str,
    owned_book_id: int,
    owned_mam_torrent_id: Optional[str],
    owned_format: Optional[str],
    owned_score: Optional[tuple[int, ...]],
    media_type: str,
) -> bool:
    """Insert one detected opportunity row.

    Returns True when a new row was inserted, False when the UNIQUE
    constraint rejected a duplicate (the detector running twice for
    the same grab+owned combination). Either outcome is fine — caller
    treats both as "opportunity is recorded."

    Caller owns the surrounding commit.
    """
    cursor = await db.execute(
        """
        INSERT OR IGNORE INTO replacement_opportunities (
            detected_at,
            candidate_grab_id, candidate_mam_torrent_id,
            candidate_format, candidate_score,
            owned_library_slug, owned_book_id, owned_mam_torrent_id,
            owned_format, owned_score,
            media_type, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'detected')
        """,
        (
            time.time(),
            candidate_grab_id, candidate_mam_torrent_id,
            candidate_format, json.dumps(list(candidate_score)),
            owned_library_slug, owned_book_id, owned_mam_torrent_id,
            owned_format,
            json.dumps(list(owned_score)) if owned_score is not None else None,
            media_type,
        ),
    )
    return (cursor.rowcount or 0) > 0


# ─── Read path ───────────────────────────────────────────────


async def list_opportunities(
    db: aiosqlite.Connection,
    *,
    status: Optional[str] = "detected",
    library_slug: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """List opportunities, newest first.

    `status` defaults to 'detected' (the live queue); pass None to
    include all statuses. `library_slug` narrows to one library. Score
    tuples are JSON-decoded back into lists for caller convenience.
    """
    where: list[str] = []
    params: list = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if library_slug is not None:
        where.append("owned_library_slug = ?")
        params.append(library_slug)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    cursor = await db.execute(
        f"""
        SELECT * FROM replacement_opportunities
        {where_clause}
        ORDER BY detected_at DESC
        LIMIT ?
        """,
        params,
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    out: list[dict] = []
    for row in rows:
        d = dict(zip(cols, row))
        for k in ("candidate_score", "owned_score"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (TypeError, ValueError):
                    d[k] = None
        out.append(d)
    return out


async def get_opportunity(
    db: aiosqlite.Connection,
    opportunity_id: int,
) -> Optional[dict]:
    """Fetch one opportunity by id, or None if it doesn't exist."""
    cursor = await db.execute(
        "SELECT * FROM replacement_opportunities WHERE id = ?",
        (opportunity_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    d = dict(zip(cols, row))
    for k in ("candidate_score", "owned_score"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                d[k] = None
    return d


async def update_status(
    db: aiosqlite.Connection,
    opportunity_id: int,
    *,
    status: str,
    acted_by: Optional[str] = None,
) -> bool:
    """Mark an opportunity as 'enacted' or 'dismissed'.

    Sets `acted_at` to the current timestamp + `acted_by` to whatever
    the caller passes (typically 'user' for UI dismissals, 'auto' for
    Phase 5b file-swap completions). Returns True on a row touched.
    """
    if status not in ("detected", "enacted", "dismissed"):
        raise ValueError(f"invalid opportunity status: {status!r}")
    cursor = await db.execute(
        "UPDATE replacement_opportunities "
        "SET status = ?, acted_at = ?, acted_by = ? "
        "WHERE id = ?",
        (status, time.time(), acted_by, opportunity_id),
    )
    return (cursor.rowcount or 0) > 0


# ─── Aggregate stats (Phase 6 UI consumption) ────────────────


async def opportunity_counts(db: aiosqlite.Connection) -> dict:
    """Return per-status counts of replacement opportunities.

    Shape: {"detected": int, "enacted": int, "dismissed": int}.
    Statuses with zero rows are still present in the dict.
    """
    cursor = await db.execute(
        "SELECT status, COUNT(*) FROM replacement_opportunities "
        "GROUP BY status"
    )
    by_status = {row[0]: row[1] for row in await cursor.fetchall()}
    return {
        "detected":  by_status.get("detected",  0),
        "enacted":   by_status.get("enacted",   0),
        "dismissed": by_status.get("dismissed", 0),
    }
