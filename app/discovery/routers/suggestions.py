"""
Series suggestion review and disposition endpoints.

Source-consensus suggestion rows are written by
`lookup.py:_compute_series_suggestions` during author scans whenever
2+ sources agree on a series name or index that differs from what's
currently stored on the book. This router is the user-facing surface
for those rows: list pending suggestions, apply one (write the
consensus value back to the book), ignore one (suppress re-suggestion
of the same value), or delete a row entirely.

Drift detection: each suggestion stores a snapshot of the book's
series state at the time it was created. The list endpoint also
returns the LIVE series state via a JOIN, so the frontend can flag
when the two have diverged — typically because the user manually
edited the book between suggestion creation and review, in which
case the diff may no longer reflect a real disagreement.
"""
import json
import logging
import time

from fastapi import APIRouter, HTTPException

from app.discovery.database import get_db, HF
from app.discovery.cross_library import run_across_libraries

logger = logging.getLogger("seshat.discovery")

router = APIRouter(prefix="/api/discovery", tags=["suggestions"])


@router.get("/series-suggestions")
async def list_series_suggestions(status: str = "pending", content_type: str = None):
    """List series suggestions filtered by status (default: pending).

    Joins to books/authors/series so the UI can render a complete row
    without N+1 follow-up queries. The `current_series_name` column on
    the suggestion is a snapshot from when the suggestion was created
    — we ALSO return the live join value so the UI can detect drift
    (e.g., the book's series was updated between suggestion creation
    and review, in which case the suggestion may no longer be accurate).
    """
    if status not in ("pending", "applied", "ignored", "all"):
        raise HTTPException(400, "status must be one of: pending, applied, ignored, all")

    # Shared query shape used by both the active-library path and the
    # per-library pass inside the cross-library aggregator.
    def _build_sql() -> tuple[str, list]:
        where_clauses = [HF]
        params: list = []
        if status != "all":
            where_clauses.append("sug.status = ?")
            params.append(status)
        where = " AND ".join(where_clauses)
        sql = f"""
            SELECT
                sug.id, sug.book_id, sug.suggested_series_name,
                sug.suggested_series_index, sug.sources_agreeing,
                sug.current_series_name AS snapshot_series_name,
                sug.current_series_index AS snapshot_series_index,
                sug.status, sug.created_at, sug.updated_at,
                b.title AS book_title, bpa.author_id, b.owned,
                b.series_index AS live_series_index,
                a.name AS author_name,
                s.name AS live_series_name
            FROM book_series_suggestions sug
            JOIN books b ON b.id = sug.book_id
            JOIN book_authors bpa ON bpa.book_id = b.id AND bpa.position = 0
            JOIN authors a ON a.id = bpa.author_id
            LEFT JOIN series s ON s.id = b.series_id
            WHERE {where}
            ORDER BY sug.updated_at DESC NULLS LAST, sug.created_at DESC
        """
        return sql, params

    def _finalize(row: dict) -> dict:
        try:
            row["sources_agreeing"] = json.loads(row["sources_agreeing"] or "[]")
        except (json.JSONDecodeError, TypeError):
            row["sources_agreeing"] = []
        snapshot_name = (row["snapshot_series_name"] or "")
        snapshot_idx = row["snapshot_series_index"]
        live_name = (row["live_series_name"] or "")
        live_idx = row["live_series_index"]
        row["drifted"] = (snapshot_name != live_name or snapshot_idx != live_idx)
        return row

    # Cross-library path — aggregate suggestions from every library of
    # the requested content_type. The suggestion table lives in each
    # per-library discovery DB, so we iterate.
    if content_type:
        sql, params = _build_sql()

        async def q(db):
            rs = await (await db.execute(sql, params)).fetchall()
            return [dict(r) for r in rs]

        rows = await run_across_libraries(content_type, q)
        # Sort by (updated_at DESC NULLS LAST, created_at DESC) —
        # Python stable sort, descending. None → -inf via a tuple key.
        rows.sort(
            key=lambda r: (
                -(r.get("updated_at") or 0.0),
                -(r.get("created_at") or 0.0),
            ),
        )
        result = [_finalize(r) for r in rows]
        return {"suggestions": result, "count": len(result)}

    # Active-library path (legacy).
    db = await get_db()
    try:
        sql, params = _build_sql()
        rows = await (await db.execute(sql, params)).fetchall()
        result = [_finalize(dict(r)) for r in rows]
        return {"suggestions": result, "count": len(result)}
    finally:
        await db.close()


@router.get("/series-suggestions/by-book/{book_id}")
async def get_suggestion_for_book(book_id: int):
    """Return the pending suggestion for a specific book, or null.

    Used by BookSidebar.jsx to show an inline notice when the user
    opens a book with a pending suggestion. Only pending suggestions
    are returned — ignored ones stay in the DB (so the consensus system
    respects the ignore) but don't clutter the sidebar. Users can manage
    ignored suggestions from the dedicated Suggestions page. Returns 200
    with `null` rather than 404 when no suggestion exists, so the
    frontend doesn't have to differentiate "no suggestion" from
    "endpoint error" in its loading logic.
    """
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT id, suggested_series_name, suggested_series_index, "
            "sources_agreeing, current_series_name, current_series_index, "
            "status, created_at, updated_at "
            "FROM book_series_suggestions "
            "WHERE book_id = ? AND status = 'pending'",
            (book_id,),
        )).fetchone()
        if not row:
            return {"suggestion": None}
        d = dict(row)
        try:
            d["sources_agreeing"] = json.loads(d["sources_agreeing"] or "[]")
        except (json.JSONDecodeError, TypeError):
            d["sources_agreeing"] = []
        return {"suggestion": d}
    finally:
        await db.close()


@router.get("/series-suggestions/count")
async def count_pending_suggestions():
    """Lightweight count for the Dashboard badge — no joins, no decoding."""
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT COUNT(*) c FROM book_series_suggestions sug "
            "JOIN books b ON b.id = sug.book_id "
            f"WHERE sug.status='pending' AND {HF}"
        )).fetchone()
        return {"pending": row["c"] if row else 0}
    finally:
        await db.close()


@router.post("/series-suggestions/{sid}/apply")
async def apply_series_suggestion(sid: int):
    """Apply a pending suggestion: write its values to the book row.

    The series row is upserted by name (case-insensitive match against
    existing series for the same author, mirroring the lookup.py merge
    logic). After the book is updated, the suggestion is marked
    'applied' rather than deleted so we have an audit trail and so
    repeat scans don't immediately re-create it.
    """
    db = await get_db()
    try:
        sug = await (await db.execute(
            "SELECT sug.*, "
            "(SELECT author_id FROM book_authors WHERE book_id=b.id AND position=0) AS author_id "
            "FROM book_series_suggestions sug "
            "JOIN books b ON b.id = sug.book_id WHERE sug.id = ?",
            (sid,),
        )).fetchone()
        if not sug:
            raise HTTPException(404, "Suggestion not found")
        if sug["status"] != "pending":
            raise HTTPException(409, f"Suggestion is {sug['status']}, not pending")

        suggested_name = sug["suggested_series_name"]
        suggested_idx = sug["suggested_series_index"]
        book_id = sug["book_id"]
        author_id = sug["author_id"]

        # Resolve the target series_id. If the suggestion is "standalone"
        # (suggested_series_name is None), we set series_id to NULL.
        # Otherwise upsert by exact case-insensitive name match scoped
        # to this author — same lookup.py:519 logic.
        if suggested_name:
            row = await (await db.execute(
                "SELECT id FROM series WHERE LOWER(name) = LOWER(?) AND author_id = ?",
                (suggested_name, author_id),
            )).fetchone()
            if row:
                series_id = row["id"]
            else:
                cur = await db.execute(
                    "INSERT INTO series (name, author_id, last_lookup_at) VALUES (?, ?, ?)",
                    (suggested_name, author_id, time.time()),
                )
                series_id = cur.lastrowid
        else:
            series_id = None

        # Update the book row. series_index is set even when None
        # (clearing it for a standalone) so the UI shows a clean state.
        await db.execute(
            "UPDATE books SET series_id = ?, series_index = ? WHERE id = ?",
            (series_id, suggested_idx, book_id),
        )
        await db.execute(
            "UPDATE book_series_suggestions SET status = 'applied', updated_at = ? WHERE id = ?",
            (time.time(), sid),
        )
        await db.commit()

        logger.info(
            f"Series suggestion {sid} applied: book_id={book_id} → "
            f"series_id={series_id} ({suggested_name!r}) #{suggested_idx}"
        )
        return {"status": "ok", "applied_to_book_id": book_id, "new_series_id": series_id}
    finally:
        await db.close()


@router.post("/series-suggestions/{sid}/ignore")
async def ignore_series_suggestion(sid: int):
    """Mark a pending suggestion as ignored.

    Ignoring is specific to the suggestion's CURRENT (suggested_name,
    suggested_index) tuple. If a future scan produces a DIFFERENT
    consensus, _compute_series_suggestions() will reset the row back
    to pending — see the status-lifecycle doc in that function.
    """
    db = await get_db()
    try:
        sug = await (await db.execute(
            "SELECT status FROM book_series_suggestions WHERE id = ?",
            (sid,),
        )).fetchone()
        if not sug:
            raise HTTPException(404, "Suggestion not found")
        if sug["status"] == "applied":
            raise HTTPException(409, "Cannot ignore an already-applied suggestion")

        await db.execute(
            "UPDATE book_series_suggestions SET status = 'ignored', updated_at = ? WHERE id = ?",
            (time.time(), sid),
        )
        await db.commit()
        logger.info(f"Series suggestion {sid} marked ignored")
        return {"status": "ok"}
    finally:
        await db.close()


@router.delete("/series-suggestions/{sid}")
async def delete_series_suggestion(sid: int):
    """Hard-delete a suggestion row. The next scan may recreate it if
    the consensus still holds — use ignore instead if you want
    permanent suppression of the same (name, index) tuple.
    """
    db = await get_db()
    try:
        result = await db.execute(
            "DELETE FROM book_series_suggestions WHERE id = ?", (sid,),
        )
        await db.commit()
        if result.rowcount == 0:
            raise HTTPException(404, "Suggestion not found")
        logger.info(f"Series suggestion {sid} deleted")
        return {"status": "ok"}
    finally:
        await db.close()
