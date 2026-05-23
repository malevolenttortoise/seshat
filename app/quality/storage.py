"""
Storage layer for `torrent_quality_metadata`.

Keyed by mam_torrent_id. Joins to books via:

    torrent_quality_metadata
        ↑ mam_torrent_id
    grabs.mam_torrent_id
        ↑ id
    book_grab_links.grab_id
        → (library_slug, book_id)

Reads are tolerant of missing rows (returns None); the upstream
consumer (Bundle A scoring, future replacement-engine, UI displays)
treats "no quality data yet" as a neutral signal rather than an
error condition.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Optional

import aiosqlite

from app.quality.extract import QualitySnapshot

_log = logging.getLogger("seshat.quality.storage")


async def upsert_quality(
    db: aiosqlite.Connection,
    snapshot: QualitySnapshot,
) -> None:
    """Insert or replace a quality row keyed by mam_torrent_id.

    Always overwrites — re-extractions on later visits supersede old
    data (e.g., if a torrent's seeder count drops, the latest reading
    wins, and a re-grab with better mediainfo upgrades a 'tags' source
    row to 'mediainfo').

    Caller is responsible for the surrounding commit.
    """
    fields = asdict(snapshot)
    fields["extracted_at"] = time.time()

    cols = list(fields.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    values = [fields[c] for c in cols]

    sql = (
        f"INSERT INTO torrent_quality_metadata ({col_list}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(mam_torrent_id) DO UPDATE SET "
        + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "mam_torrent_id")
    )
    await db.execute(sql, values)


async def get_quality(
    db: aiosqlite.Connection,
    mam_torrent_id: str,
) -> Optional[dict]:
    """Fetch the quality row for one torrent, or None if not yet extracted.

    Returns a dict shaped like QualitySnapshot's fields. None when no
    extraction has run for this torrent yet (legitimate state — the
    backfill worker hasn't gotten to it, OR the torrent pre-dates the
    feature, OR extraction failed and was never retried).
    """
    cursor = await db.execute(
        "SELECT * FROM torrent_quality_metadata WHERE mam_torrent_id = ?",
        (mam_torrent_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


async def get_quality_for_book(
    db: aiosqlite.Connection,
    library_slug: str,
    book_id: int,
) -> Optional[dict]:
    """Resolve quality data for an owned book via the grabs join.

    Walks book_grab_links → grabs.mam_torrent_id → torrent_quality_metadata.
    Returns None if the book has no grab link (not owned via Seshat) OR
    the grab's torrent hasn't been extracted yet.
    """
    cursor = await db.execute(
        """
        SELECT q.*
        FROM torrent_quality_metadata q
        JOIN grabs g ON g.mam_torrent_id = q.mam_torrent_id
        JOIN book_grab_links l ON l.grab_id = g.id
        WHERE l.library_slug = ? AND l.book_id = ?
        LIMIT 1
        """,
        (library_slug, book_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


async def list_missing_quality_torrent_ids(
    db: aiosqlite.Connection,
    limit: int = 100,
) -> list[str]:
    """Find mam_torrent_ids of grabbed torrents lacking a quality row.

    Used by the backfill worker. Restricts to torrents linked to at
    least one owned book — we don't care about un-linked grabs
    (failed, abandoned, etc.).

    Ordered oldest-grab-first so the backfill makes steady progress
    rather than re-targeting the newest torrents on every tick.
    """
    cursor = await db.execute(
        """
        SELECT DISTINCT g.mam_torrent_id
        FROM grabs g
        JOIN book_grab_links l ON l.grab_id = g.id
        LEFT JOIN torrent_quality_metadata q
            ON q.mam_torrent_id = g.mam_torrent_id
        WHERE g.mam_torrent_id IS NOT NULL
          AND g.mam_torrent_id != ''
          AND q.mam_torrent_id IS NULL
        ORDER BY g.id ASC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows if r[0]]


async def quality_coverage_stats(
    db: aiosqlite.Connection,
) -> dict:
    """Return aggregate coverage stats for the Settings/UI surface.

    Shape:
        {
            "linked_torrents":    int,  # grabs joined to owned books
            "extracted":          int,  # have a quality row
            "missing":            int,  # need backfill
            "by_source": {
                "mediainfo":   int,
                "description": int,
                "tags":        int,
                "mixed":       int,
                "none":        int,
            },
        }
    """
    cursor = await db.execute(
        """
        SELECT COUNT(DISTINCT g.mam_torrent_id)
        FROM grabs g
        JOIN book_grab_links l ON l.grab_id = g.id
        WHERE g.mam_torrent_id IS NOT NULL AND g.mam_torrent_id != ''
        """
    )
    linked = (await cursor.fetchone())[0] or 0

    cursor = await db.execute(
        """
        SELECT COUNT(DISTINCT q.mam_torrent_id)
        FROM torrent_quality_metadata q
        JOIN grabs g ON g.mam_torrent_id = q.mam_torrent_id
        JOIN book_grab_links l ON l.grab_id = g.id
        """
    )
    extracted = (await cursor.fetchone())[0] or 0

    cursor = await db.execute(
        """
        SELECT q.source, COUNT(*)
        FROM torrent_quality_metadata q
        JOIN grabs g ON g.mam_torrent_id = q.mam_torrent_id
        JOIN book_grab_links l ON l.grab_id = g.id
        GROUP BY q.source
        """
    )
    by_source = {row[0]: row[1] for row in await cursor.fetchall()}

    return {
        "linked_torrents": linked,
        "extracted": extracted,
        "missing": max(0, linked - extracted),
        "by_source": {
            "mediainfo":   by_source.get("mediainfo", 0),
            "description": by_source.get("description", 0),
            "tags":        by_source.get("tags", 0),
            "mixed":       by_source.get("mixed", 0),
            "none":        by_source.get("none", 0),
        },
    }
