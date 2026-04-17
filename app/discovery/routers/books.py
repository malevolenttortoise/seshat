"""
Book query and mutation endpoints.

Covers the main book browse (`/api/books`), the missing/upcoming
filtered views, hide/unhide/dismiss state, manual book add/edit/delete,
and the bulk-by-book scan triggers used by the BooksPage selection bar.

The shared SELECT fragment at the top of this file pre-aggregates
visible series counts so list endpoints don't fire one correlated
COUNT subquery per row — see `_SERIES_TOTAL_JOIN` for the rationale.
"""
import logging
import re
from fastapi import APIRouter, Body, HTTPException, Query

from app import state
from app.discovery.database import get_db, HF, cleanup_empty_series

logger = logging.getLogger("seshat.discovery")

router = APIRouter(prefix="/api/discovery", tags=["books"])

# ─── Shared SELECT fragments ────────────────────────────────
# `_SERIES_TOTAL_JOIN` pre-aggregates visible-book counts per series in
# one subquery, then the row-level SELECT LEFT JOINs against the
# result. The naive alternative — `(SELECT COUNT(*) ... WHERE
# series_id=b.series_id)` per row — fires once per returned book,
# turning a 60-row page into 60 sub-COUNTs.
#
# The inner filter is fixed at `hidden=0`: `series_total` always means
# "visible books in this series" even when the outer query has
# `include_hidden=True`. Standalone books (series_id IS NULL) come back
# as `series_total=0` via the COALESCE in the projection below.
#
# `mainline_total` counts only whole-numbered entries ≥ 1 (1, 2, 3…),
# excluding novellas/prequels at 0.5, 1.5, etc. The frontend uses this
# for the "#X of Y" series-position label so book #4 of a 4-mainline +
# 1-prequel series reads "#4 of 4" and the prequel reads "#0.5 of 4".
# `series_total` continues to count every visible non-omnibus book
# (5 in that example) for the overall "how many books in the series"
# count shown on author/series browse views.
_SERIES_TOTAL_JOIN = """
LEFT JOIN (
    SELECT series_id,
           COUNT(*) AS series_total,
           SUM(CASE WHEN series_index IS NOT NULL
                     AND series_index >= 1
                     AND series_index = CAST(series_index AS INTEGER)
                    THEN 1 ELSE 0 END) AS mainline_total
    FROM books
    WHERE hidden=0 AND series_id IS NOT NULL AND COALESCE(is_omnibus,0)=0
    GROUP BY series_id
) st ON st.series_id = b.series_id
""".strip()

_BOOKS_SELECT = (
    "SELECT b.*, a.name as author_name, s.name as series_name, "
    "COALESCE(st.series_total, 0) as series_total, "
    "COALESCE(st.mainline_total, 0) as mainline_total "
    "FROM books b "
    "JOIN authors a ON b.author_id=a.id "
    "LEFT JOIN series s ON b.series_id=s.id "
    f"{_SERIES_TOTAL_JOIN}"
)


# ─── Books ───────────────────────────────────────────────────
@router.get("/books")
async def get_books(search: str = Query(None), author_id: int = Query(None), series_id: int = Query(None), owned: bool = Query(None), book_type: str = Query(None), mam_status: str = Query(None), sort: str = Query("title"), sort_dir: str = Query("asc"), page: int = Query(1, ge=1), per_page: int = Query(60, ge=1, le=5000), include_hidden: bool = Query(False), hidden_only: bool = Query(False)):
    db = await get_db()
    try:
        c = []; p = []
        if hidden_only: c.append("b.hidden=1")
        elif not include_hidden: c.append(HF)
        if search: c.append("(b.title LIKE ? OR a.name LIKE ? OR COALESCE(s.name,'') LIKE ?)"); p.extend([f"%{search}%"]*3)
        if author_id: c.append("b.author_id=?"); p.append(author_id)
        if series_id: c.append("b.series_id=?"); p.append(series_id)
        if owned is True: c.append("b.owned=1")
        elif owned is False: c.append("b.owned=0")
        if book_type == "series": c.append("b.series_id IS NOT NULL")
        elif book_type == "standalone": c.append("b.series_id IS NULL")
        if mam_status == "found": c.append("b.mam_status='found'")
        elif mam_status == "possible": c.append("b.mam_status='possible'")
        elif mam_status == "not_found": c.append("b.mam_status='not_found'")
        elif mam_status == "unscanned": c.append("b.mam_status IS NULL")
        w = " AND ".join(c) if c else "1=1"
        cnt = (await (await db.execute(f"SELECT COUNT(*) c FROM books b JOIN authors a ON b.author_id=a.id LEFT JOIN series s ON b.series_id=s.id WHERE {w}", p)).fetchone())["c"]
        d = "DESC" if sort_dir == "desc" else "ASC"
        o = {"title": f"b.title {d}", "author": f"a.sort_name {d}, b.title ASC", "series": f"COALESCE(s.name,'zzz') {d}, b.series_index ASC", "date": f"b.pub_date {d}", "added": f"b.first_seen_at {d}"}.get(sort, f"b.title {d}")
        off = (page-1)*per_page
        rows = await (await db.execute(f"{_BOOKS_SELECT} WHERE {w} ORDER BY {o} LIMIT ? OFFSET ?", p+[per_page, off])).fetchall()
        return {"books": [dict(r) for r in rows], "total": cnt, "page": page, "per_page": per_page, "pages": max(1, (cnt+per_page-1)//per_page)}
    finally: await db.close()


@router.get("/missing")
async def get_missing(search: str = Query(None), author_id: int = Query(None), series_id: int = Query(None), book_type: str = Query(None), mam_status: str = Query(None), sort: str = Query("title"), sort_dir: str = Query("asc"), page: int = Query(1, ge=1), per_page: int = Query(60, ge=1, le=5000), include_hidden: bool = Query(False)):
    return await get_books(search=search, author_id=author_id, series_id=series_id, owned=False, book_type=book_type, mam_status=mam_status, sort=sort, sort_dir=sort_dir, page=page, per_page=per_page, include_hidden=include_hidden, hidden_only=False)


@router.get("/upcoming")
async def get_upcoming(search: str = Query(None), sort: str = Query("date"), sort_dir: str = Query("asc"), mam_status: str = Query(None), page: int = Query(1, ge=1), per_page: int = Query(60, ge=1, le=5000)):
    db = await get_db()
    try:
        c = [HF, "b.owned=0", "b.is_unreleased=1"]; p = []
        if search: c.append("(b.title LIKE ? OR a.name LIKE ? OR COALESCE(s.name,'') LIKE ?)"); p.extend([f"%{search}%"]*3)
        if mam_status == "found": c.append("b.mam_status='found'")
        elif mam_status == "possible": c.append("b.mam_status='possible'")
        elif mam_status == "not_found": c.append("b.mam_status='not_found'")
        elif mam_status == "unscanned": c.append("b.mam_status IS NULL")
        w = " AND ".join(c)
        cnt = (await (await db.execute(f"SELECT COUNT(*) c FROM books b JOIN authors a ON b.author_id=a.id LEFT JOIN series s ON b.series_id=s.id WHERE {w}", p)).fetchone())["c"]
        d = "DESC" if sort_dir == "desc" else "ASC"
        o = {"date": f"COALESCE(b.expected_date, '9999') {d}", "title": f"b.title {d}", "author": f"a.sort_name {d}"}.get(sort, f"COALESCE(b.expected_date, '9999') {d}")
        off = (page-1)*per_page
        rows = await (await db.execute(f"{_BOOKS_SELECT} WHERE {w} ORDER BY {o} LIMIT ? OFFSET ?", p+[per_page, off])).fetchall()
        return {"books": [dict(r) for r in rows], "total": cnt, "page": page, "per_page": per_page, "pages": max(1, (cnt+per_page-1)//per_page)}
    finally: await db.close()


# ─── Book Actions ────────────────────────────────────────────
@router.post("/books/{bid}/hide")
async def hide(bid: int):
    db = await get_db()
    try:
        await db.execute("UPDATE books SET hidden=1 WHERE id=?", (bid,))
        # Clear any pending/ignored suggestion — hidden books shouldn't
        # carry stale series suggestion cards if the user re-opens them.
        await db.execute("DELETE FROM book_series_suggestions WHERE book_id=?", (bid,))
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.post("/books/{bid}/unhide")
async def unhide(bid: int):
    db = await get_db()
    try:
        await db.execute("UPDATE books SET hidden=0 WHERE id=?", (bid,))
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.get("/books/hidden")
async def get_hidden(search: str = Query(None), sort: str = Query("title"), sort_dir: str = Query("asc"), page: int = Query(1, ge=1), per_page: int = Query(60, ge=1, le=5000)):
    db = await get_db()
    try:
        c = ["b.hidden=1"]; p = []
        if search: c.append("(b.title LIKE ? OR a.name LIKE ? OR COALESCE(s.name,'') LIKE ?)"); p.extend([f"%{search}%"]*3)
        w = " AND ".join(c)
        cnt = (await (await db.execute(f"SELECT COUNT(*) c FROM books b JOIN authors a ON b.author_id=a.id LEFT JOIN series s ON b.series_id=s.id WHERE {w}", p)).fetchone())["c"]
        d = "DESC" if sort_dir == "desc" else "ASC"
        o = {"title": f"b.title {d}", "author": f"a.sort_name {d}, b.title ASC", "series": f"COALESCE(s.name,'zzz') {d}, b.series_index ASC", "date": f"b.pub_date {d}", "added": f"b.first_seen_at {d}"}.get(sort, f"b.title {d}")
        off = (page-1)*per_page
        rows = await (await db.execute(f"{_BOOKS_SELECT} WHERE {w} ORDER BY {o} LIMIT ? OFFSET ?", p+[per_page, off])).fetchall()
        return {"books": [dict(r) for r in rows], "total": cnt, "page": page, "per_page": per_page, "pages": max(1, (cnt+per_page-1)//per_page)}
    finally: await db.close()


@router.post("/books/{bid}/dismiss")
async def dismiss(bid: int):
    db = await get_db()
    try:
        await db.execute("UPDATE books SET is_new=0 WHERE id=?", (bid,))
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.put("/books/{bid}")
async def update_book(bid: int, data: dict = Body(...)):
    db = await get_db()
    try:
        fields = []; vals = []
        for k in ["title", "description", "pub_date", "expected_date", "isbn", "cover_url", "series_index", "source_url"]:
            if k in data:
                fields.append(f"{k}=?"); vals.append(data[k])
        # Handle MAM URL — validate format and update status
        if "mam_url" in data:
            mam_url = (data["mam_url"] or "").strip()
            if mam_url:
                mam_match = re.match(r'https?://(?:www\.)?myanonamouse\.net/t/(\d+)', mam_url)
                if not mam_match:
                    raise HTTPException(400, "Invalid MAM URL. Expected format: https://www.myanonamouse.net/t/123456")
                torrent_id = int(mam_match.group(1))
                fields.extend(["mam_url=?", "mam_status=?", "mam_torrent_id=?"])
                vals.extend([mam_url, "found", torrent_id])
            else:
                # Explicitly cleared → mark as "not_found" (not just null)
                fields.extend(["mam_url=?", "mam_status=?", "mam_torrent_id=?"])
                vals.extend([None, "not_found", None])
        if "is_unreleased" in data:
            fields.append("is_unreleased=?"); vals.append(1 if data["is_unreleased"] else 0)
        # Handle series assignment — find or create series by name
        if "series_name" in data:
            series_name = (data["series_name"] or "").strip()
            if series_name:
                # Get the book's author_id for series scoping
                book_row = await (await db.execute(
                    "SELECT author_id FROM books WHERE id=?", (bid,)
                )).fetchone()
                if book_row:
                    aid = book_row["author_id"]
                    # Case-insensitive lookup for existing series
                    srow = await (await db.execute(
                        "SELECT id FROM series WHERE LOWER(name) = LOWER(?) AND author_id = ?",
                        (series_name, aid),
                    )).fetchone()
                    if srow:
                        sid = srow["id"]
                    else:
                        cur = await db.execute(
                            "INSERT INTO series (name, author_id) VALUES (?, ?)",
                            (series_name, aid),
                        )
                        sid = cur.lastrowid
                        logger.info(f"Created new series '{series_name}' (id={sid}) for author_id={aid}")
                    fields.append("series_id=?"); vals.append(sid)
            else:
                # Empty series name → remove from series (make standalone)
                fields.append("series_id=?"); vals.append(None)
        if not fields:
            return {"status": "no changes"}
        vals.append(bid)
        await db.execute(f"UPDATE books SET {', '.join(fields)} WHERE id=?", vals)
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.post("/books/add")
async def add_book(data: dict = Body(...)):
    """Manually add a missing/upcoming book."""
    db = await get_db()
    try:
        title = data.get("title", "").strip()
        author_name = data.get("author_name", "").strip()
        if not title or not author_name:
            raise HTTPException(400, "Title and author are required")
        # Find or create author
        row = await (await db.execute("SELECT id FROM authors WHERE name=?", (author_name,))).fetchone()
        if row:
            aid = row["id"]
        else:
            cur = await db.execute("INSERT INTO authors (name, sort_name) VALUES (?, ?)", (author_name, author_name))
            aid = cur.lastrowid
        # Find series if specified
        sid = None
        if data.get("series_name"):
            srow = await (await db.execute("SELECT id FROM series WHERE name=? AND author_id=?", (data["series_name"], aid))).fetchone()
            if srow:
                sid = srow["id"]
            else:
                cur = await db.execute("INSERT INTO series (name, author_id) VALUES (?, ?)", (data["series_name"], aid))
                sid = cur.lastrowid
        is_unreleased = 1 if data.get("is_unreleased") else 0
        src = data.get("source", "manual")
        cur = await db.execute(
            "INSERT INTO books (title, author_id, series_id, series_index, pub_date, expected_date, is_unreleased, description, isbn, cover_url, source, source_url, owned, is_new) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,1)",
            (title, aid, sid, data.get("series_index"), data.get("pub_date"), data.get("expected_date"), is_unreleased, data.get("description"), data.get("isbn"), data.get("cover_url"), src, data.get("source_url"))
        )
        await db.commit()
        return {"status": "ok", "book_id": cur.lastrowid}
    finally:
        await db.close()


@router.post("/books/dismiss-all")
async def dismiss_all():
    db = await get_db()
    try:
        await db.execute("UPDATE books SET is_new=0 WHERE is_new=1")
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.post("/books/clear-scan-data")
async def clear_book_scan_data(data: dict = Body(...)):
    """Clear source and/or MAM scan data for a list of books.

    Used by the multi-select bar on Library/Missing/Upcoming/MAM pages.

    - clear_source: deletes the book if it was discovered (owned=0 and
      calibre_id IS NULL); for owned books, just clears source_url.
    - clear_mam: resets all mam_* columns on the book.
    """
    book_ids = data.get("book_ids", [])
    clear_source = data.get("clear_source", False)
    clear_mam = data.get("clear_mam", False)
    if not book_ids:
        return {"error": "No books specified"}
    if not clear_source and not clear_mam:
        return {"error": "Nothing to clear — specify clear_source and/or clear_mam"}

    db = await get_db()
    try:
        placeholders = ",".join(["?" for _ in book_ids])
        deleted = 0
        if clear_source:
            count_row = await (await db.execute(
                f"SELECT COUNT(*) c FROM books WHERE id IN ({placeholders}) AND owned=0 AND calibre_id IS NULL",
                book_ids,
            )).fetchone()
            deleted = count_row["c"] if count_row else 0
            await db.execute(
                f"DELETE FROM books WHERE id IN ({placeholders}) AND owned=0 AND calibre_id IS NULL",
                book_ids,
            )
            await db.execute(
                f"UPDATE books SET source_url=NULL WHERE id IN ({placeholders}) AND owned=1",
                book_ids,
            )
        if clear_mam:
            await db.execute(
                f"UPDATE books SET mam_url=NULL, mam_status=NULL, mam_formats=NULL, "
                f"mam_torrent_id=NULL, mam_has_multiple=0, mam_my_snatched=0 "
                f"WHERE id IN ({placeholders})",
                book_ids,
            )
        await db.commit()
        if clear_source and deleted > 0:
            await cleanup_empty_series(db)
        logger.info(f"Cleared scan data for {len(book_ids)} books (source={clear_source}, mam={clear_mam}), {deleted} deleted")
        return {"status": "ok", "books_cleared": len(book_ids), "books_deleted": deleted}
    finally:
        await db.close()


@router.post("/books/scan-sources")
async def scan_books_sources(data: dict = Body(...)):
    """Run a source-plugin lookup for the unique authors of the given books.

    Source plugins (Goodreads, Hardcover, Kobo) work at the
    author level — there's no per-book source lookup. So when the user picks
    book IDs from a Books-page selection, we resolve them to the distinct set
    of author IDs and run lookup_author on each. The frontend tooltip warns
    the user that this scans whole authors, not individual books.
    """
    from app.discovery.lookup import lookup_author

    book_ids = data.get("book_ids", [])
    if not book_ids:
        return {"error": "No books specified"}

    db = await get_db()
    try:
        placeholders = ",".join(["?" for _ in book_ids])
        rows = await (await db.execute(
            f"SELECT DISTINCT a.id, a.name FROM books b JOIN authors a ON b.author_id=a.id "
            f"WHERE b.id IN ({placeholders})",
            book_ids,
        )).fetchall()
    finally:
        await db.close()
    if not rows:
        raise HTTPException(404, "No matching authors found")

    # Same background-task pattern as /authors/scan-sources. Lazy
    # import keeps books.py from pulling in the whole authors router
    # at module load time.
    import asyncio
    from app.routers.authors import _spawn_lookup_task

    async def _runner():
        nonlocal_state = {"scanned": 0, "errors": 0, "new": 0}
        for row in rows:
            aid, name = row["id"], row["name"]
            state._lookup_progress.update({"current_author": name})
            # Capture cumulative-so-far baseline at closure-creation
            # time so the live new_books count climbs in real time
            # instead of jumping per-author. See routers/authors.py for
            # the same pattern with the same default-arg trick.
            def _on_source(running, _baseline=nonlocal_state["new"]):
                state._lookup_progress["new_books"] = _baseline + int(running)
            try:
                new_books = await lookup_author(aid, name, on_progress=_on_source)
                nonlocal_state["new"] += int(new_books or 0)
                nonlocal_state["scanned"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Bulk source scan error for author {aid} ({name}): {e}")
                nonlocal_state["errors"] += 1
            state._lookup_progress.update({
                "checked": nonlocal_state["scanned"] + nonlocal_state["errors"],
                "new_books": nonlocal_state["new"],
            })

    _spawn_lookup_task("bulk_books", total=len(rows), runner=_runner)
    return {"status": "started", "total": len(rows)}


@router.post("/books/scan-mam")
async def scan_books_mam(data: dict = Body(...)):
    """Run a MAM scan against the given list of book IDs.

    Used by the multi-select bar on book listing pages and by the
    individual Re-scan MAM button in BookSidebar (which sends a list of one).
    Re-scans even books that already have a mam_status — the user clicked
    the button to refresh stale results.
    """
    from app.config import load_settings
    from app.discovery.sources.mam import check_book as mam_check_book, _resolve_mam_languages
    from app import state

    book_ids = data.get("book_ids", [])
    if not book_ids:
        return {"error": "No books specified"}

    s = load_settings()
    if not s.get("mam_enabled") or not s.get("mam_session_id"):
        return {"error": "MAM not configured or not enabled"}
    if not s.get("mam_scanning_enabled", True):
        return {"error": "MAM scanning is disabled — enable it in Settings"}
    if state._mam_scan_progress.get("running"):
        return {"error": "A MAM scan is already running"}

    db = await get_db()
    try:
        placeholders = ",".join(["?" for _ in book_ids])
        rows = await (await db.execute(
            f"SELECT b.id, b.title, a.name FROM books b JOIN authors a ON b.author_id=a.id "
            f"WHERE b.id IN ({placeholders})",
            book_ids,
        )).fetchall()
        if not rows:
            return {"error": "No matching books found"}

        delay = s.get("rate_mam", 2)
        format_priority = s.get("mam_format_priority")
        token = s["mam_session_id"]
        lang_ids = _resolve_mam_languages(s.get("languages", ["English"]))
        stats = {"scanned": 0, "found": 0, "possible": 0, "not_found": 0, "errors": 0}
        results = []

        for row in rows:
            bid, btitle, aname = row["id"], row["title"], row["name"]
            try:
                check = await mam_check_book(token, btitle, aname, format_priority, delay, lang_ids=lang_ids)
            except Exception as e:
                logger.error(f"Bulk MAM scan error on book {bid} ({btitle[:40]}): {e}")
                stats["errors"] += 1
                continue
            await db.execute("""
                UPDATE books SET mam_url=?, mam_status=?, mam_formats=?,
                       mam_torrent_id=?, mam_has_multiple=?, mam_my_snatched=?
                WHERE id=?
            """, (
                check["mam_url"], check["status"], check["mam_formats"],
                check["mam_torrent_id"],
                1 if check["mam_has_multiple"] else 0,
                1 if check.get("mam_my_snatched") else 0,
                bid,
            ))
            stats["scanned"] += 1
            if check["status"] == "found":
                stats["found"] += 1
            elif check["status"] == "possible":
                stats["possible"] += 1
            elif check["status"] == "not_found":
                stats["not_found"] += 1
            results.append({"id": bid, "status": check["status"], "match_pct": check.get("match_pct")})
        await db.commit()
        return {"status": "complete", **stats, "results": results}
    finally:
        await db.close()


@router.delete("/books/{bid}")
async def delete_book(bid: int):
    """Delete a book entry — only non-Calibre (discovered/imported) books can be deleted."""
    db = await get_db()
    try:
        row = await (await db.execute("SELECT id, source, owned, calibre_id FROM books WHERE id=?", (bid,))).fetchone()
        if not row:
            raise HTTPException(404, "Book not found")
        if row["calibre_id"] and row["source"] == "calibre":
            raise HTTPException(400, "Cannot delete books synced from Calibre. Remove them from Calibre instead.")
        await db.execute("DELETE FROM book_series_suggestions WHERE book_id=?", (bid,))
        await db.execute("DELETE FROM books WHERE id=?", (bid,))
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()
