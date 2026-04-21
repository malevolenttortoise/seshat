"""
Series endpoints — list and detail.

  GET /api/series         — every series the user has at least one
                            visible book for, with owned/missing
                            counts and multi-author flag
  GET /api/series/{sid}   — full series detail with the ordered
                            book list and per-book ownership state

Both endpoints honor the global hidden-book filter so the totals
shown in the UI match what the user actually sees on book pages.
"""
import logging
from fastapi import APIRouter, HTTPException, Query

from app.discovery.database import get_db, HF

logger = logging.getLogger("seshat.discovery")

router = APIRouter(prefix="/api/discovery", tags=["series"])


@router.get("/series/{sid}")
async def get_series(sid: int, slug: str | None = None):
    """Return a series detail with its ordered book list.

    `slug=X` overrides which library's DB holds this series. Without
    it we use the active library. Needed for the cross-library author
    detail page: series ids from ABS don't mean anything in Calibre,
    so fetching books for an ABS-sourced series must go straight to
    the ABS DB. Same failure mode as the authors endpoint before the
    slug fix — ABS series 2 could be a totally different series in
    Calibre with the same id.

    Every returned book row is stamped with `library_slug` so the
    frontend's `coverSrcFor` picks the per-library cover URL. Without
    it the Calibre cover endpoint was serving a completely unrelated
    book's cover for each ABS book id.
    """
    # Active library fallback resolved explicitly so we can stamp
    # library_slug on every book row even when the caller didn't pass
    # one (single-library installs still benefit from correct metadata).
    from app.discovery.database import get_active_library as _get_active
    effective_slug = slug or _get_active() or ""
    db = await get_db(slug)
    try:
        r = await (await db.execute("SELECT s.*, a.name as author_name FROM series s LEFT JOIN authors a ON s.author_id=a.id WHERE s.id=?", (sid,))).fetchone()
        if not r:
            raise HTTPException(404)
        s = dict(r)
        # Pre-aggregated series_total via LEFT JOIN (same refactor as
        # routers/books.py) — avoids a correlated COUNT firing per returned
        # row. For this endpoint all returned rows share the same
        # series_id (the query is WHERE b.series_id=?), so every row's
        # series_total is identical — the old code computed it N times.
        # Content type looked up once from the library config — used
        # to stamp each row alongside library_slug so the frontend can
        # render audiobook badges and route cover requests properly.
        from app import state
        content_type = next(
            (l.get("content_type", "ebook") for l in state._discovered_libraries
             if l.get("slug") == effective_slug),
            "ebook",
        )
        rows = [
            {**dict(b), "library_slug": effective_slug, "content_type": content_type}
            for b in await (await db.execute(f"""
                SELECT b.*, a.name as author_name, sr.name as series_name,
                    COALESCE(st.series_total, 0) as series_total,
                    COALESCE(st.mainline_total, 0) as mainline_total
                FROM books b
                JOIN authors a ON b.author_id=a.id
                LEFT JOIN series sr ON b.series_id=sr.id
                LEFT JOIN (
                    SELECT series_id,
                           COUNT(*) AS series_total,
                           SUM(CASE WHEN series_index IS NOT NULL
                                     AND series_index >= 1
                                     AND series_index = CAST(series_index AS INTEGER)
                                    THEN 1 ELSE 0 END) AS mainline_total
                    FROM books
                    WHERE hidden=0 AND series_id IS NOT NULL
                    GROUP BY series_id
                ) st ON st.series_id = b.series_id
                WHERE b.series_id=? AND {HF}
                ORDER BY COALESCE(b.series_index,999), b.pub_date ASC
            """, (sid,))).fetchall()
        ]
        s["books"] = await _stamp_work_siblings(rows, effective_slug)
        return s
    finally:
        await db.close()


async def _stamp_work_siblings(
    books: list[dict], slug: str,
) -> list[dict]:
    """Attach cross-format sibling info to each book in a list.

    Looks up the pipeline DB's `work_links` table in bulk and, for
    every book with a cross-library twin, sets `work_siblings` to a
    list of `{library_slug, book_id, content_type}` dicts (excluding
    self). Books without a work row or without cross-library twins
    come back unchanged. Empty slug or empty list short-circuit.
    """
    if not slug or not books:
        return books
    from app.works.storage import get_siblings_for_books
    ids = [int(b["id"]) for b in books if b.get("id") is not None]
    if not ids:
        return books
    sib_map = await get_siblings_for_books(slug, ids)
    for b in books:
        s = sib_map.get(int(b["id"]))
        if s:
            b["work_id"] = s[0].work_id
            b["work_siblings"] = [
                {"library_slug": w.library_slug, "book_id": w.book_id,
                 "content_type": w.content_type}
                for w in s
            ]
    return books


@router.get("/series")
async def list_series(search: str = Query(None), sort: str = Query("name"), sort_dir: str = Query("asc"), has_missing: bool = Query(None)):
    db = await get_db()
    try:
        q = f"""SELECT s.*, a.name as author_name,
            COUNT(DISTINCT CASE WHEN {HF} THEN b.id END) as book_count,
            SUM(CASE WHEN b.owned=1 AND {HF} THEN 1 ELSE 0 END) as owned_count,
            SUM(CASE WHEN b.owned=0 AND {HF} THEN 1 ELSE 0 END) as missing_count,
            CASE WHEN COUNT(DISTINCT b.author_id) > 1 THEN 1 ELSE 0 END as multi_author
            FROM series s LEFT JOIN authors a ON s.author_id=a.id LEFT JOIN books b ON s.id=b.series_id"""
        p = []
        c = []
        if search:
            c.append("(s.name LIKE ? OR a.name LIKE ?)")
            p.extend([f"%{search}%"] * 2)
        if c:
            q += " WHERE " + " AND ".join(c)
        q += " GROUP BY s.id"
        if has_missing:
            q += " HAVING missing_count > 0"
        d = "DESC" if sort_dir == "desc" else "ASC"
        q += {"missing": f" ORDER BY missing_count {d}", "author": f" ORDER BY a.sort_name {d}"}.get(sort, f" ORDER BY s.name {d}")
        return {"series": [dict(r) for r in await (await db.execute(q, p)).fetchall()]}
    finally:
        await db.close()
