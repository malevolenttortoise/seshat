"""
CRUD for the `import_checks` table (2026-10 audit wave 5a, G121).

One row per ebook dropped into CWA's ingest folder. CWA reports nothing
back, so Seshat looks for the book in Calibre's metadata.db afterwards
(`app.orchestrator.import_check`): pending → confirmed, or failed after
15 minutes. Before this a drop CWA deleted unimported still read as
delivered (2026-09-29 → 10-09).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import aiosqlite

STATE_PENDING = "pending"
STATE_CONFIRMED = "confirmed"
STATE_FAILED = "failed"


@dataclass(frozen=True)
class ImportCheck:
    id: int
    review_id: Optional[int]
    grab_id: int
    pipeline_run_id: Optional[int]
    library_slug: Optional[str]
    drop_path: str
    title: str
    authors: list[str]
    dropped_at: str
    state: str
    calibre_book_id: Optional[int]
    reason: Optional[str]


async def create(
    db: aiosqlite.Connection,
    *,
    review_id: Optional[int],
    grab_id: int,
    pipeline_run_id: Optional[int],
    library_slug: Optional[str],
    drop_path: str,
    title: str,
    authors: list[str],
) -> int:
    cursor = await db.execute(
        """
        INSERT INTO import_checks
            (review_id, grab_id, pipeline_run_id, library_slug, drop_path,
             title, authors_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (review_id, grab_id, pipeline_run_id, library_slug, drop_path,
         title, json.dumps(authors, ensure_ascii=False)),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def list_pending(db: aiosqlite.Connection) -> list[ImportCheck]:
    cursor = await db.execute(
        "SELECT * FROM import_checks WHERE state = ? ORDER BY id",
        (STATE_PENDING,),
    )
    return [_row(r) for r in await cursor.fetchall()]


async def confirmed_calibre_ids(db: aiosqlite.Connection) -> set[int]:
    """Calibre records that already confirmed a drop."""
    cursor = await db.execute(
        "SELECT calibre_book_id FROM import_checks "
        "WHERE state = ? AND calibre_book_id IS NOT NULL",
        (STATE_CONFIRMED,),
    )
    return {int(r[0]) for r in await cursor.fetchall()}


async def latest_for_review(
    db: aiosqlite.Connection, review_id: int,
) -> Optional[ImportCheck]:
    cursor = await db.execute(
        "SELECT * FROM import_checks WHERE review_id = ? ORDER BY id DESC LIMIT 1",
        (review_id,),
    )
    row = await cursor.fetchone()
    return _row(row) if row else None


async def resolve(
    db: aiosqlite.Connection,
    check_id: int,
    state: str,
    *,
    calibre_book_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> None:
    await db.execute(
        "UPDATE import_checks SET state = ?, calibre_book_id = ?, reason = ?, "
        "checked_at = datetime('now') WHERE id = ?",
        (state, calibre_book_id, reason, check_id),
    )
    await db.commit()


def _row(row) -> ImportCheck:
    try:
        authors = json.loads(row["authors_json"] or "[]")
    except (ValueError, TypeError):
        authors = []
    return ImportCheck(
        id=int(row["id"]),
        review_id=row["review_id"],
        grab_id=int(row["grab_id"]),
        pipeline_run_id=row["pipeline_run_id"],
        library_slug=row["library_slug"],
        drop_path=str(row["drop_path"] or ""),
        title=str(row["title"] or ""),
        authors=[str(a) for a in authors if a],
        dropped_at=str(row["dropped_at"] or ""),
        state=str(row["state"] or ""),
        calibre_book_id=row["calibre_book_id"],
        reason=row["reason"],
    )
