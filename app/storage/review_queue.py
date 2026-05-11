"""
CRUD for the `book_review_queue` table.

Every book that finishes downloading lands here for mandatory
manual review before being delivered to the Calibre/CWA sink.
Power users cannot skip review — but the auto-add timeout job
(see `app/orchestrator/review_timeout.py`) promotes undecided
items to Calibre with bare title+author metadata after
`metadata_review_timeout_days` have passed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import aiosqlite

_log = logging.getLogger("seshat.storage.review_queue")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_TIMEOUT = "timeout"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"
# Sink delivery was attempted but the sink was unreachable. The book
# stays in staging and the review-timeout job retries on its next tick.
STATUS_SINK_PENDING = "sink_pending"


@dataclass(frozen=True)
class ReviewRow:
    id: int
    grab_id: int
    pipeline_run_id: Optional[int]
    staged_path: str
    book_filename: str
    book_format: Optional[str]
    metadata: dict
    cover_path: Optional[str]
    status: str
    created_at: str
    decided_at: Optional[str]
    decision_note: Optional[str]
    # v2.7.0 bundle awareness. Single-book grabs:
    # bundle_total=1, bundle_index=0, bundle_group_id="grab-<id>",
    # bundle_parent_grab_id=None — indistinguishable from pre-v2.7
    # rows after the migration backfill. Bundle children carry
    # bundle_total>=2 plus a bundle_parent_grab_id pointing at the
    # parent grab.
    bundle_group_id: Optional[str] = None
    bundle_index: int = 0
    bundle_total: int = 1
    library_slug: Optional[str] = None
    bundle_parent_grab_id: Optional[int] = None


async def create_entry(
    db: aiosqlite.Connection,
    *,
    grab_id: int,
    pipeline_run_id: Optional[int],
    staged_path: str,
    book_filename: str,
    book_format: Optional[str],
    metadata: dict,
    cover_path: Optional[str] = None,
    bundle_group_id: Optional[str] = None,
    bundle_index: int = 0,
    bundle_total: int = 1,
    library_slug: Optional[str] = None,
    bundle_parent_grab_id: Optional[int] = None,
) -> int:
    # Default the bundle_group_id to the per-grab deterministic value
    # when the caller doesn't specify one. Keeps legacy callers (test
    # code, internal helpers) shape-compatible.
    if bundle_group_id is None:
        bundle_group_id = f"grab-{grab_id}"
    cursor = await db.execute(
        """
        INSERT INTO book_review_queue
            (grab_id, pipeline_run_id, staged_path, book_filename,
             book_format, metadata_json, cover_path, status,
             bundle_group_id, bundle_index, bundle_total,
             library_slug, bundle_parent_grab_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            grab_id,
            pipeline_run_id,
            staged_path,
            book_filename,
            book_format,
            json.dumps(metadata, ensure_ascii=False),
            cover_path,
            STATUS_PENDING,
            bundle_group_id,
            bundle_index,
            bundle_total,
            library_slug,
            bundle_parent_grab_id,
        ),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def get_entry(
    db: aiosqlite.Connection, entry_id: int
) -> Optional[ReviewRow]:
    cursor = await db.execute(
        "SELECT * FROM book_review_queue WHERE id = ?", (entry_id,)
    )
    row = await cursor.fetchone()
    return _row_to_review(row) if row else None


async def list_pending(
    db: aiosqlite.Connection, *, limit: int = 200
) -> list[ReviewRow]:
    cursor = await db.execute(
        """
        SELECT * FROM book_review_queue
        WHERE status = ?
        ORDER BY created_at ASC, bundle_group_id, bundle_index
        LIMIT ?
        """,
        (STATUS_PENDING, limit),
    )
    rows = await cursor.fetchall()
    return [_row_to_review(r) for r in rows]


async def list_sink_pending(
    db: aiosqlite.Connection,
) -> list[ReviewRow]:
    """Items where the sink was unreachable and needs retry."""
    cursor = await db.execute(
        """
        SELECT * FROM book_review_queue
        WHERE status = ?
        ORDER BY created_at ASC
        """,
        (STATUS_SINK_PENDING,),
    )
    rows = await cursor.fetchall()
    return [_row_to_review(r) for r in rows]


async def list_stale_pending(
    db: aiosqlite.Connection, *, older_than_days: int
) -> list[ReviewRow]:
    """Pending items created more than `older_than_days` ago."""
    cursor = await db.execute(
        """
        SELECT * FROM book_review_queue
        WHERE status = ?
          AND created_at <= datetime('now', ?)
        ORDER BY created_at ASC
        """,
        (STATUS_PENDING, f"-{int(older_than_days)} days"),
    )
    rows = await cursor.fetchall()
    return [_row_to_review(r) for r in rows]


async def set_status(
    db: aiosqlite.Connection,
    entry_id: int,
    status: str,
    *,
    decision_note: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    sets = ["status = ?", "decided_at = datetime('now')"]
    params: list[Any] = [status]
    if decision_note is not None:
        sets.append("decision_note = ?")
        params.append(decision_note)
    if metadata is not None:
        sets.append("metadata_json = ?")
        params.append(json.dumps(metadata, ensure_ascii=False))
    params.append(entry_id)
    await db.execute(
        f"UPDATE book_review_queue SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    await db.commit()


async def count_by_status(db: aiosqlite.Connection, status: str) -> int:
    cursor = await db.execute(
        "SELECT COUNT(*) FROM book_review_queue WHERE status = ?", (status,)
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


def _row_to_review(row) -> ReviewRow:
    try:
        meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
    except (ValueError, TypeError):
        meta = {}
    # Bundle columns are optional in row factories that select a
    # subset of columns; tolerate KeyError and fall back to the
    # single-book defaults. aiosqlite.Row supports __getitem__ but
    # raises IndexError for missing names; check with `.keys()`.
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    grab_id_val = int(row["grab_id"])
    return ReviewRow(
        id=int(row["id"]),
        grab_id=grab_id_val,
        pipeline_run_id=row["pipeline_run_id"],
        staged_path=str(row["staged_path"] or ""),
        book_filename=str(row["book_filename"] or ""),
        book_format=row["book_format"],
        metadata=meta,
        cover_path=row["cover_path"],
        status=str(row["status"] or ""),
        created_at=str(row["created_at"] or ""),
        decided_at=row["decided_at"],
        decision_note=row["decision_note"],
        bundle_group_id=(
            row["bundle_group_id"]
            if "bundle_group_id" in keys else f"grab-{grab_id_val}"
        ),
        bundle_index=int(row["bundle_index"]) if "bundle_index" in keys else 0,
        bundle_total=int(row["bundle_total"]) if "bundle_total" in keys else 1,
        library_slug=row["library_slug"] if "library_slug" in keys else None,
        bundle_parent_grab_id=(
            row["bundle_parent_grab_id"]
            if "bundle_parent_grab_id" in keys else None
        ),
    )
