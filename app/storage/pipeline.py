"""
CRUD for the `pipeline_runs` table.

Tracks a grab through the post-download pipeline:
  staged → extracted → metadata_done → sunk → complete

Each pipeline run corresponds to one grab that has finished downloading.
A single grab can have multiple pipeline runs if the first attempt fails
and the user retries (e.g., Calibre was offline).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import aiosqlite

_log = logging.getLogger("seshat.storage.pipeline")

# Pipeline states.
PIPE_STAGED = "staged"
PIPE_EXTRACTED = "extracted"
PIPE_METADATA_DONE = "metadata_done"
# Pipeline stopped at the manual review queue. Awaits user action.
PIPE_AWAITING_REVIEW = "awaiting_review"
PIPE_SUNK = "sunk"
PIPE_COMPLETE = "complete"
# Every book the run staged was rejected at review (2026-10 audit issue 11).
PIPE_REJECTED = "rejected"
PIPE_FAILED = "failed"


@dataclass(frozen=True)
class PipelineRow:
    """One row from the pipeline_runs table."""

    id: int
    grab_id: int
    qbit_hash: Optional[str]
    source_path: Optional[str]
    staged_path: Optional[str]
    book_filename: Optional[str]
    book_format: Optional[str]
    metadata_title: Optional[str]
    metadata_author: Optional[str]
    metadata_series: Optional[str]
    metadata_language: Optional[str]
    sink_name: Optional[str]
    sink_result: Optional[str]
    state: str
    started_at: str
    completed_at: Optional[str]
    error: Optional[str]


async def create_run(
    db: aiosqlite.Connection,
    *,
    grab_id: int,
    qbit_hash: Optional[str] = None,
    source_path: Optional[str] = None,
    state: str = PIPE_STAGED,
) -> int:
    """Insert a new pipeline run."""
    cursor = await db.execute(
        """
        INSERT INTO pipeline_runs (grab_id, qbit_hash, source_path, state)
        VALUES (?, ?, ?, ?)
        """,
        (grab_id, qbit_hash, source_path, state),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def set_state(
    db: aiosqlite.Connection,
    run_id: int,
    state: str,
    *,
    staged_path: Optional[str] = None,
    book_filename: Optional[str] = None,
    book_format: Optional[str] = None,
    metadata_title: Optional[str] = None,
    metadata_author: Optional[str] = None,
    metadata_series: Optional[str] = None,
    metadata_language: Optional[str] = None,
    sink_name: Optional[str] = None,
    sink_result: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Transition a pipeline run to a new state with optional field updates."""
    sets = ["state = ?", "state_updated_at = datetime('now')"]
    params: list = [state]

    if state in (PIPE_COMPLETE, PIPE_REJECTED):
        sets.append("completed_at = datetime('now')")

    for field_name, value in [
        ("staged_path", staged_path),
        ("book_filename", book_filename),
        ("book_format", book_format),
        ("metadata_title", metadata_title),
        ("metadata_author", metadata_author),
        ("metadata_series", metadata_series),
        ("metadata_language", metadata_language),
        ("sink_name", sink_name),
        ("sink_result", sink_result),
        ("error", error),
    ]:
        if value is not None:
            sets.append(f"{field_name} = ?")
            params.append(value)

    params.append(run_id)
    await db.execute(
        f"UPDATE pipeline_runs SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    await db.commit()


async def get_run(
    db: aiosqlite.Connection, run_id: int
) -> Optional[PipelineRow]:
    """Fetch one pipeline run by id."""
    cursor = await db.execute(
        """
        SELECT id, grab_id, qbit_hash, source_path, staged_path,
               book_filename, book_format,
               metadata_title, metadata_author, metadata_series,
               metadata_language, sink_name, sink_result,
               state, started_at, completed_at, error
        FROM pipeline_runs WHERE id = ?
        """,
        (run_id,),
    )
    row = await cursor.fetchone()
    return _row_to_pipeline(row) if row else None


async def find_by_grab_id(
    db: aiosqlite.Connection, grab_id: int
) -> Optional[PipelineRow]:
    """Find the most recent pipeline run for a grab."""
    cursor = await db.execute(
        """
        SELECT id, grab_id, qbit_hash, source_path, staged_path,
               book_filename, book_format,
               metadata_title, metadata_author, metadata_series,
               metadata_language, sink_name, sink_result,
               state, started_at, completed_at, error
        FROM pipeline_runs WHERE grab_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (grab_id,),
    )
    row = await cursor.fetchone()
    return _row_to_pipeline(row) if row else None


async def delete_run(
    db: aiosqlite.Connection, run_id: int,
) -> None:
    """Delete a pipeline_run row.

    Used by the download watcher's auto-retry flow: when a
    `no file matching` failure is detected and eligible for retry
    (cooldown elapsed, under retry limit, qBit still has the
    torrent), the old failed run is deleted so `check_for_completions`
    can recreate a fresh one on the same grab. The FK cascade in
    `book_review_queue.pipeline_run_id` uses ON DELETE SET NULL,
    so approval rows aren't orphaned — though in practice a failed
    run never produces a review row anyway.
    """
    await db.execute("DELETE FROM pipeline_runs WHERE id = ?", (run_id,))
    await db.commit()


async def find_by_state(
    db: aiosqlite.Connection, state: str
) -> list[PipelineRow]:
    """Find all pipeline runs in a given state."""
    cursor = await db.execute(
        """
        SELECT id, grab_id, qbit_hash, source_path, staged_path,
               book_filename, book_format,
               metadata_title, metadata_author, metadata_series,
               metadata_language, sink_name, sink_result,
               state, started_at, completed_at, error
        FROM pipeline_runs WHERE state = ?
        ORDER BY id ASC
        """,
        (state,),
    )
    rows = await cursor.fetchall()
    return [_row_to_pipeline(r) for r in rows]


def _row_to_pipeline(row) -> PipelineRow:
    return PipelineRow(
        id=int(row["id"]),
        grab_id=int(row["grab_id"]),
        qbit_hash=row["qbit_hash"],
        source_path=row["source_path"],
        staged_path=row["staged_path"],
        book_filename=row["book_filename"],
        book_format=row["book_format"],
        metadata_title=row["metadata_title"],
        metadata_author=row["metadata_author"],
        metadata_series=row["metadata_series"],
        metadata_language=row["metadata_language"],
        sink_name=row["sink_name"],
        sink_result=row["sink_result"],
        state=str(row["state"] or ""),
        started_at=str(row["started_at"] or ""),
        completed_at=row["completed_at"],
        error=row["error"],
    )
