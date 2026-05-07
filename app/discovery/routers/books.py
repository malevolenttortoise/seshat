"""
Book query and mutation endpoints.

Covers the main book browse (`/api/books`), the missing/upcoming
filtered views, hide/unhide/dismiss state, manual book add/edit/delete,
and the bulk-by-book scan triggers used by the BooksPage selection bar.

The shared SELECT fragment at the top of this file pre-aggregates
visible series counts so list endpoints don't fire one correlated
COUNT subquery per row — see `_SERIES_TOTAL_JOIN` for the rationale.
"""
import json
import logging
import re
from fastapi import APIRouter, Body, HTTPException, Query

from app import state
from app.discovery.database import get_db, HF, cleanup_empty_series
from app.discovery.cross_library import (
    run_across_libraries,
    sort_and_paginate,
    sort_key_for,
)
from app.discovery.source_urls import parse_url
from app.discovery.routers.series import _recompute_series_author

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
def _build_books_where(search, author_id, series_id, owned, book_type, mam_status, include_hidden, hidden_only):
    """Compose the WHERE clause + params used by every books-query path.

    Kept separate so the per-library helper (`_query_books_for_lib`)
    and the active-library code path share the same filter logic.
    """
    c: list[str] = []
    p: list = []
    if hidden_only:
        c.append("b.hidden=1")
    elif not include_hidden:
        c.append(HF)
    if search:
        c.append("(b.title LIKE ? OR a.name LIKE ? OR COALESCE(s.name,'') LIKE ?)")
        p.extend([f"%{search}%"] * 3)
    if author_id:
        c.append("b.author_id=?"); p.append(author_id)
    if series_id:
        c.append("b.series_id=?"); p.append(series_id)
    if owned is True:
        c.append("b.owned=1")
    elif owned is False:
        c.append("b.owned=0")
    if book_type == "series":
        c.append("b.series_id IS NOT NULL")
    elif book_type == "standalone":
        c.append("b.series_id IS NULL")
    if mam_status == "found":
        c.append("b.mam_status='found'")
    elif mam_status == "possible":
        c.append("b.mam_status='possible'")
    elif mam_status == "not_found":
        c.append("b.mam_status='not_found'")
    elif mam_status == "unscanned":
        c.append("b.mam_status IS NULL")
    return (" AND ".join(c) if c else "1=1"), p


_BOOKS_SELECT_X = (
    "SELECT b.*, a.name as author_name, a.sort_name as author_sort_name, "
    "s.name as series_name, "
    "COALESCE(st.series_total, 0) as series_total, "
    "COALESCE(st.mainline_total, 0) as mainline_total "
    "FROM books b "
    "JOIN authors a ON b.author_id=a.id "
    "LEFT JOIN series s ON b.series_id=s.id "
    f"{_SERIES_TOTAL_JOIN}"
)


async def _query_books_for_lib(db, where_sql: str, where_params: list, sort: str, sort_dir: str):
    """Run a books query against one library's DB.

    Returns the full row set (no LIMIT) — aggregation sorting + pagination
    happens in Python across libraries, so each library has to hand over
    every row that matches its filters. For the typical personal library
    that's still tens of thousands of rows at most.

    Uses `_BOOKS_SELECT_X` (cross-library variant) which adds
    `author_sort_name` to the projection so the Python-side sort can
    order by it without an extra per-row lookup.
    """
    d = "DESC" if sort_dir == "desc" else "ASC"
    o = {
        "title": f"b.title {d}",
        "author": f"a.sort_name {d}, b.title ASC",
        "series": f"COALESCE(s.name,'zzz') {d}, b.series_index ASC",
        "date": f"b.pub_date {d}",
        "added": f"b.first_seen_at {d}",
    }.get(sort, f"b.title {d}")
    base = f"{_BOOKS_SELECT_X} WHERE {where_sql} ORDER BY {o}"
    rows = await (await db.execute(base, where_params)).fetchall()
    return [dict(r) for r in rows]


@router.get("/books")
async def get_books(search: str = Query(None), author_id: int = Query(None), series_id: int = Query(None), owned: bool = Query(None), book_type: str = Query(None), mam_status: str = Query(None), sort: str = Query("title"), sort_dir: str = Query("asc"), page: int = Query(1, ge=1), per_page: int = Query(60, ge=1, le=5000), include_hidden: bool = Query(False), hidden_only: bool = Query(False), content_type: str = Query(None)):
    """
    List books. `content_type` selects among:
      * omitted / "" — active library only (legacy behavior)
      * "ebook" / "audiobook" — aggregate across every discovered
        library of that type
      * "all" — aggregate across EVERY discovered library regardless
        of type (mixed ebook+audiobook view)

    In cross-library mode each row gets stamped with `library_slug`,
    `library_name`, and `content_type` so the UI can render badges
    and per-library metadata without extra round-trips.
    """
    # ── Cross-library path ────────────────────────────────────
    if content_type:
        where_sql, where_params = _build_books_where(
            search, author_id, series_id, owned, book_type, mam_status,
            include_hidden, hidden_only,
        )

        async def q(db):
            return await _query_books_for_lib(db, where_sql, where_params, sort, sort_dir)

        rows = await run_across_libraries(content_type, q)
        window, total = sort_and_paginate(
            rows,
            sort_key=sort_key_for(sort),
            reverse=(sort_dir == "desc"),
            page=page, per_page=per_page,
        )
        return {
            "books": window, "total": total, "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        }

    # ── Active-library path ───────────────────────────────────
    db = await get_db()
    try:
        w, p = _build_books_where(
            search, author_id, series_id, owned, book_type, mam_status,
            include_hidden, hidden_only,
        )
        cnt = (await (await db.execute(f"SELECT COUNT(*) c FROM books b JOIN authors a ON b.author_id=a.id LEFT JOIN series s ON b.series_id=s.id WHERE {w}", p)).fetchone())["c"]
        d = "DESC" if sort_dir == "desc" else "ASC"
        o = {"title": f"b.title {d}", "author": f"a.sort_name {d}, b.title ASC", "series": f"COALESCE(s.name,'zzz') {d}, b.series_index ASC", "date": f"b.pub_date {d}", "added": f"b.first_seen_at {d}"}.get(sort, f"b.title {d}")
        off = (page-1)*per_page
        rows = await (await db.execute(f"{_BOOKS_SELECT} WHERE {w} ORDER BY {o} LIMIT ? OFFSET ?", p+[per_page, off])).fetchall()
        return {"books": [dict(r) for r in rows], "total": cnt, "page": page, "per_page": per_page, "pages": max(1, (cnt+per_page-1)//per_page)}
    finally: await db.close()


@router.get("/missing")
async def get_missing(search: str = Query(None), author_id: int = Query(None), series_id: int = Query(None), book_type: str = Query(None), mam_status: str = Query(None), sort: str = Query("title"), sort_dir: str = Query("asc"), page: int = Query(1, ge=1), per_page: int = Query(60, ge=1, le=5000), include_hidden: bool = Query(False), content_type: str = Query(None)):
    """
    List missing (unowned, non-future) books.

    When `content_type` is supplied, aggregates across libraries AND
    applies the per-author format-preference filter: books whose
    author has `tracking_mode="audiobook"` set don't surface as
    missing when they'd be ebook entries, and vice versa. Global
    default comes from `settings.audiobook_tracking_mode`.
    """
    base = await get_books(
        search=search, author_id=author_id, series_id=series_id,
        owned=False, book_type=book_type, mam_status=mam_status,
        sort=sort, sort_dir=sort_dir, page=1,
        # Pull a wide window then re-paginate after the tracking-mode
        # filter — avoids a partial page caused by filter evictions.
        per_page=5000,
        include_hidden=include_hidden, hidden_only=False,
        content_type=content_type,
    )
    if not content_type:
        # Active-library path: nothing to re-filter; hand `base` back
        # after slicing down to the requested page.
        off = (page - 1) * per_page
        all_books = base.get("books", [])
        total = base.get("total", len(all_books))
        return {
            "books": all_books[off:off + per_page], "total": total,
            "page": page, "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        }

    # Cross-library path: apply per-author tracking-mode filter.
    filtered = await _apply_tracking_mode_filter(
        base.get("books", []), content_type,
    )
    off = (page - 1) * per_page
    total = len(filtered)
    return {
        "books": filtered[off:off + per_page], "total": total,
        "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


async def _apply_tracking_mode_filter(
    books: list[dict], content_type: str,
) -> list[dict]:
    """Drop books whose author's tracking_mode excludes this format.

    Rules:
      * content_type="all" — keep only books whose author's mode
        allows the book's own content_type. An "audiobook only" author
        hides ebook rows; an "ebook only" author hides audiobook rows.
      * content_type="ebook" — keep only books whose author's mode
        includes ebook (i.e. mode == "ebook" or "both").
      * content_type="audiobook" — symmetric.

    Bulk-loads the preferences table once, then does O(1) lookups
    per book. Books with no preference row inherit the global
    default via `effective_tracking_mode`.
    """
    from app.works.normalize import normalize_author
    from app.works.preferences import list_preferences, _global_default

    prefs = {p.normalized_name: p.tracking_mode for p in await list_preferences()}
    global_mode = _global_default()

    def mode_for(author: str) -> str:
        return prefs.get(normalize_author(author), global_mode)

    out: list[dict] = []
    for b in books:
        row_type = b.get("content_type") or "ebook"
        mode = mode_for(b.get("author_name") or "")
        if mode == "both":
            out.append(b)
            continue
        if content_type == "all":
            # Book's own content_type vs mode.
            if mode == row_type:
                out.append(b)
            continue
        # content_type in {"ebook", "audiobook"} — request already
        # narrowed to one format; only the mode match has to agree.
        if mode == content_type:
            out.append(b)
    return out


@router.get("/upcoming")
async def get_upcoming(search: str = Query(None), sort: str = Query("date"), sort_dir: str = Query("asc"), mam_status: str = Query(None), page: int = Query(1, ge=1), per_page: int = Query(60, ge=1, le=5000), content_type: str = Query(None)):
    """Upcoming (unreleased) books.

    Same `content_type` semantics as `/books`: omitted reads the
    active library; "ebook" / "audiobook" / "all" aggregate.
    """
    if content_type:
        def where_params():
            c = [HF, "b.owned=0", "b.is_unreleased=1"]; p = []
            if search:
                c.append("(b.title LIKE ? OR a.name LIKE ? OR COALESCE(s.name,'') LIKE ?)")
                p.extend([f"%{search}%"] * 3)
            if mam_status == "found": c.append("b.mam_status='found'")
            elif mam_status == "possible": c.append("b.mam_status='possible'")
            elif mam_status == "not_found": c.append("b.mam_status='not_found'")
            elif mam_status == "unscanned": c.append("b.mam_status IS NULL")
            return " AND ".join(c), p

        w, p = where_params()
        d = "DESC" if sort_dir == "desc" else "ASC"
        o = {
            "date": f"COALESCE(b.expected_date, '9999') {d}",
            "title": f"b.title {d}",
            "author": f"a.sort_name {d}",
        }.get(sort, f"COALESCE(b.expected_date, '9999') {d}")

        async def q(db):
            rows = await (await db.execute(
                f"{_BOOKS_SELECT_X} WHERE {w} ORDER BY {o}",
                p,
            )).fetchall()
            return [dict(r) for r in rows]

        rows = await run_across_libraries(content_type, q)
        window, total = sort_and_paginate(
            rows, sort_key=sort_key_for(sort),
            reverse=(sort_dir == "desc"),
            page=page, per_page=per_page,
        )
        return {
            "books": window, "total": total, "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        }

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
async def _series_id_for_book(db, bid: int) -> int | None:
    """Look up a single book's series_id. Used by hide/unhide so we
    can recompute the affected series's authority after the toggle —
    `_recompute_series_author` counts visible books only, so a hide
    that drops the visible count to 1 author flips the series back
    to per-author, and an unhide that pushes it to 2+ flips it to
    shared."""
    row = await (await db.execute(
        "SELECT series_id FROM books WHERE id = ?", (bid,),
    )).fetchone()
    return row["series_id"] if row else None


@router.post("/books/{bid}/hide")
async def hide(bid: int):
    db = await get_db()
    try:
        sid = await _series_id_for_book(db, bid)
        await db.execute("UPDATE books SET hidden=1 WHERE id=?", (bid,))
        # Clear any pending/ignored suggestion — hidden books shouldn't
        # carry stale series suggestion cards if the user re-opens them.
        await db.execute("DELETE FROM book_series_suggestions WHERE book_id=?", (bid,))
        if sid is not None:
            await _recompute_series_author(db, [sid])
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.post("/books/{bid}/unhide")
async def unhide(bid: int):
    db = await get_db()
    try:
        sid = await _series_id_for_book(db, bid)
        await db.execute("UPDATE books SET hidden=0 WHERE id=?", (bid,))
        if sid is not None:
            await _recompute_series_author(db, [sid])
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.get("/books/hidden")
async def get_hidden(search: str = Query(None), sort: str = Query("title"), sort_dir: str = Query("asc"), page: int = Query(1, ge=1), per_page: int = Query(60, ge=1, le=5000), content_type: str = Query(None)):
    """Hidden books. `content_type` routes cross-library when supplied."""
    c = ["b.hidden=1"]; p: list = []
    if search:
        c.append("(b.title LIKE ? OR a.name LIKE ? OR COALESCE(s.name,'') LIKE ?)")
        p.extend([f"%{search}%"] * 3)
    w = " AND ".join(c)

    if content_type:
        async def q(db):
            return await _query_books_for_lib(db, w, p, sort, sort_dir)
        rows = await run_across_libraries(content_type, q)
        window, total = sort_and_paginate(
            rows, sort_key=sort_key_for(sort),
            reverse=(sort_dir == "desc"),
            page=page, per_page=per_page,
        )
        return {
            "books": window, "total": total, "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        }

    db = await get_db()
    try:
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
        # v2.3.4: track which fields the user actually changed so the
        # auto-flow logic in `_apply_calibre_diff` /
        # `_apply_abs_diff` skips them on the next sync. The
        # BookSidebar form re-sends every field on every save, so
        # incoming != changed-by-user — we have to diff against the
        # stored row to know what really changed. Anything the user
        # genuinely modified gets added to `books.user_edited_fields`
        # (a JSON array) and stays there until the user re-clears
        # the field or accepts a queued review-queue diff that
        # replaces their value (Metadata Manager UI flow).
        TRACKED_FIELDS = {
            "title", "description", "pub_date", "expected_date",
            "isbn", "cover_url", "series_index",
        }
        current_row = await (await db.execute(
            "SELECT title, description, pub_date, expected_date, isbn, "
            "cover_url, series_index, user_edited_fields "
            "FROM books WHERE id=?", (bid,),
        )).fetchone()
        if not current_row:
            raise HTTPException(404, f"book {bid} not found")
        edited_now: set[str] = set()

        fields = []; vals = []
        for k in ["title", "description", "pub_date", "expected_date", "isbn", "cover_url", "series_index", "source_url"]:
            if k in data:
                v = data[k]
                # series_index is REAL — empty string from the
                # BookSidebar's "blank means standalone-in-series /
                # omnibus" input would land as TEXT and break every
                # downstream `float(series_index)` (init_db dedupe,
                # cross-library sort, etc.). Coerce to None at the
                # boundary instead.
                if k == "series_index" and isinstance(v, str) and not v.strip():
                    v = None
                fields.append(f"{k}=?"); vals.append(v)
                # Track real changes (incoming != stored) for
                # user_edited_fields. source_url is NOT tracked here
                # — it's structural (per-source URL ownership) and
                # has its own dedicated editor that does its own
                # canonical-form comparison.
                if k in TRACKED_FIELDS and v != current_row[k]:
                    edited_now.add(k)
        # Handle MAM URL — validate format and update status. The
        # BookSidebar form re-sends every field on every save, so we
        # only act when the incoming value actually differs from
        # what's already stored. Two reasons:
        #   1. The MAM scan stores a `/tor/browse.php?...` search URL
        #      on `mam_status='not_found'` so the user can click
        #      through to verify. Re-validating that on every save
        #      would 400-out edits to unrelated fields (title,
        #      series, etc.) and leave the user staring at a button
        #      that "does nothing."
        #   2. An empty form field on a never-scanned row would
        #      otherwise trip the "explicitly cleared" branch and
        #      stomp `mam_status` to `'not_found'` even though the
        #      user touched nothing.
        # Diff-aware: validation + side-effects only fire when the
        # user genuinely changed the URL.
        if "mam_url" in data:
            incoming = (data["mam_url"] or "").strip()
            current_row = await (await db.execute(
                "SELECT mam_url FROM books WHERE id=?", (bid,)
            )).fetchone()
            current = ((current_row["mam_url"] if current_row else "") or "").strip()
            if incoming != current:
                if incoming:
                    mam_match = re.match(r'https?://(?:www\.)?myanonamouse\.net/t/(\d+)', incoming)
                    if not mam_match:
                        raise HTTPException(400, "Invalid MAM URL. Expected format: https://www.myanonamouse.net/t/123456")
                    torrent_id = int(mam_match.group(1))
                    fields.extend(["mam_url=?", "mam_status=?", "mam_torrent_id=?"])
                    vals.extend([incoming, "found", torrent_id])
                else:
                    # Explicitly cleared — null the URL and mark
                    # not_found so the row stops claiming it has a
                    # MAM match.
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
        # Merge edited_now into the stored user_edited_fields JSON
        # array. Idempotent on repeat saves (set-union); only writes
        # the column when the merged set actually grows. Read failures
        # (NULL or malformed JSON) treat the existing set as empty —
        # backward-compatible with rows that pre-date v2.2.14.
        if edited_now:
            try:
                raw_uef = current_row["user_edited_fields"]
                existing_uef = (
                    json.loads(raw_uef) if raw_uef else []
                )
                if not isinstance(existing_uef, list):
                    existing_uef = []
            except (json.JSONDecodeError, TypeError):
                existing_uef = []
            merged_uef = sorted(set(existing_uef) | edited_now)
            if set(merged_uef) != set(existing_uef):
                fields.append("user_edited_fields=?")
                vals.append(json.dumps(merged_uef))

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

    `content_type`: optional "ebook" / "audiobook". When set, resolves
    the selection's author names in the active library and then runs
    the per-library scan over every library of that type — mirrors the
    /authors/scan-sources pattern for cross-library scans from the
    books multi-select.
    """
    from app.discovery.lookup import lookup_author

    book_ids = data.get("book_ids", [])
    author_names_in = data.get("author_names")
    content_type = data.get("content_type")
    if not book_ids and not author_names_in:
        return {"error": "No books specified"}

    # Caller-supplied author_names skip the book→author SQL resolver
    # entirely. Same rationale as `/authors/scan-sources` — when the
    # user is in cross-library mode, book_ids are scoped to whichever
    # library each merged book row was first encountered in, and the
    # active-library JOIN can pick up unrelated books for those IDs
    # (or miss entirely). Sending pre-resolved author names sidesteps
    # the lookup. Names-mode REQUIRES content_type because the active-
    # library `lookup_author(aid, name)` path needs a real aid that we
    # don't have for synthetic rows; cross-library path uses names only.
    if author_names_in and not content_type:
        raise HTTPException(
            400,
            "author_names requires content_type — cross-library path only",
        )
    if author_names_in:
        # Synthetic rows; only the cross-library `names` list below
        # actually consumes these — the active-library code path is
        # gated off above.
        rows = [{"id": None, "name": n} for n in author_names_in if n]
    else:
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

    if content_type is None:
        async def _runner():
            nonlocal_state = {"scanned": 0, "errors": 0, "new": 0}
            for row in rows:
                aid, name = row["id"], row["name"]
                state._lookup_progress.update({"current_author": name})
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

    # Cross-library path. Resolve names from the active library (every
    # selected book_id was visible from there), then iterate every
    # library matching the requested content_type and scan each author
    # by name.
    from app.discovery.cross_library import libraries_for
    from app.discovery.database import get_active_library, set_active_library

    target_libs = libraries_for(content_type)
    if not target_libs:
        return {"status": "ok", "total": 0,
                "message": f"No {content_type} libraries found."}
    names = list({row["name"] for row in rows if row["name"]})
    if not names:
        raise HTTPException(404, "No matching authors found")

    # Pre-resolve per-library matches so the dashboard's progress total
    # reflects ACTUAL author scans rather than the optimistic
    # names×libraries product. Mirrors the same hoist in
    # /authors/scan-sources — see that handler's comment.
    pre_resolved: list[tuple[dict, list]] = []
    ph = ",".join(["?" for _ in names])
    for lib in target_libs:
        slug = lib.get("slug")
        if not slug:
            continue
        try:
            ldb = await get_db(slug)
        except Exception as e:
            logger.warning(f"books/scan-sources: cannot open lib {slug}: {e}")
            continue
        try:
            lib_rows = await ldb.execute_fetchall(
                f"SELECT id, name FROM authors WHERE name IN ({ph}) "
                f"ORDER BY sort_name",
                names,
            )
        finally:
            await ldb.close()
        pre_resolved.append((lib, list(lib_rows)))

    total_tasks = sum(len(rows_) for _, rows_ in pre_resolved)
    if total_tasks == 0:
        return {"status": "ok", "total": 0,
                "requested": len(names),
                "message": "No matching authors in target libraries."}

    async def _runner():
        original_active = get_active_library()
        nonlocal_state = {"scanned": 0, "errors": 0, "new": 0}
        try:
            for lib, lib_rows in pre_resolved:
                slug = lib.get("slug")
                if not slug:
                    continue
                if slug != get_active_library():
                    set_active_library(slug)
                for lib_row in lib_rows:
                    aid, name = lib_row[0], lib_row[1]
                    state._lookup_progress.update({"current_author": name})
                    def _on_source(running, _baseline=nonlocal_state["new"]):
                        state._lookup_progress["new_books"] = _baseline + int(running)
                    try:
                        new_books = await lookup_author(aid, name, on_progress=_on_source)
                        nonlocal_state["new"] += int(new_books or 0)
                        nonlocal_state["scanned"] += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(f"Bulk source scan error for {name} in {slug}: {e}")
                        nonlocal_state["errors"] += 1
                    state._lookup_progress.update({
                        "checked": nonlocal_state["scanned"] + nonlocal_state["errors"],
                        "new_books": nonlocal_state["new"],
                    })
        finally:
            if original_active and original_active != get_active_library():
                set_active_library(original_active)

    _spawn_lookup_task("bulk_books", total=total_tasks, runner=_runner)
    return {"status": "started", "total": total_tasks,
            "requested": len(names),
            "libraries": len(target_libs), "authors": len(names)}


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
    from app.discovery.routers.mam import _mam_ready, _get_mam_token
    if not await _mam_ready(s):
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
        # Active library's content_type routes the whole bulk scan
        # through either the ebook or audiobook MAM category.
        from app.discovery.routers.mam import _active_content_type
        ct = _active_content_type()
        format_priority = s.get("audiobook_format_priority" if ct == "audiobook" else "mam_format_priority")
        token = await _get_mam_token()
        lang_ids = _resolve_mam_languages(s.get("languages", ["English"]))
        stats = {"scanned": 0, "found": 0, "possible": 0, "not_found": 0, "errors": 0}
        results = []

        for row in rows:
            bid, btitle, aname = row["id"], row["title"], row["name"]
            try:
                check = await mam_check_book(token, btitle, aname, format_priority, delay, lang_ids=lang_ids, content_type=ct)
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
        row = await (await db.execute("SELECT id, source, owned, calibre_id, series_id FROM books WHERE id=?", (bid,))).fetchone()
        if not row:
            raise HTTPException(404, "Book not found")
        if row["calibre_id"] and row["source"] == "calibre":
            raise HTTPException(400, "Cannot delete books synced from Calibre. Remove them from Calibre instead.")
        sid = row["series_id"]
        await db.execute("DELETE FROM book_series_suggestions WHERE book_id=?", (bid,))
        await db.execute("DELETE FROM books WHERE id=?", (bid,))
        if sid is not None:
            await _recompute_series_author(db, [sid])
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


# ─── Bulk Book Actions ───────────────────────────────────────
# Used by the multi-select bar on Author Detail (and any future page
# that needs to act on a set of book IDs). Each endpoint mirrors the
# single-book equivalent above but operates on a list. `bulk-delete`
# silently skips Calibre-synced rows rather than failing the whole
# call, since a partial selection is still a useful outcome.
@router.post("/books/bulk-hide")
async def bulk_hide(data: dict = Body(...)):
    book_ids = data.get("book_ids", [])
    if not book_ids:
        return {"error": "No books specified"}
    db = await get_db()
    try:
        placeholders = ",".join(["?" for _ in book_ids])
        # Capture distinct series ids before the hide so we can
        # recompute authority on each. Bulk-hiding all of an author's
        # books in a 2-author shared series flips it back to
        # per-author for the remaining contributor.
        sid_rows = await (await db.execute(
            f"SELECT DISTINCT series_id FROM books "
            f"WHERE id IN ({placeholders}) AND series_id IS NOT NULL",
            book_ids,
        )).fetchall()
        affected_sids = [r["series_id"] for r in sid_rows]

        await db.execute(f"UPDATE books SET hidden=1 WHERE id IN ({placeholders})", book_ids)
        await db.execute(f"DELETE FROM book_series_suggestions WHERE book_id IN ({placeholders})", book_ids)
        if affected_sids:
            await _recompute_series_author(db, affected_sids)
        await db.commit()
        return {"status": "ok", "count": len(book_ids)}
    finally:
        await db.close()


@router.post("/books/bulk-dismiss")
async def bulk_dismiss(data: dict = Body(...)):
    book_ids = data.get("book_ids", [])
    if not book_ids:
        return {"error": "No books specified"}
    db = await get_db()
    try:
        placeholders = ",".join(["?" for _ in book_ids])
        await db.execute(f"UPDATE books SET is_new=0 WHERE id IN ({placeholders})", book_ids)
        await db.commit()
        return {"status": "ok", "count": len(book_ids)}
    finally:
        await db.close()


@router.post("/books/bulk-delete")
async def bulk_delete(data: dict = Body(...)):
    book_ids = data.get("book_ids", [])
    if not book_ids:
        return {"error": "No books specified"}
    db = await get_db()
    try:
        placeholders = ",".join(["?" for _ in book_ids])
        # Partition the requested IDs into deletable vs Calibre-protected
        # so we can report "deleted N, skipped M" honestly.
        rows = await (await db.execute(
            f"SELECT id, calibre_id, source, series_id FROM books WHERE id IN ({placeholders})",
            book_ids,
        )).fetchall()
        deletable = [
            r["id"] for r in rows
            if not (r["calibre_id"] is not None and r["source"] == "calibre")
        ]
        skipped = len(rows) - len(deletable)
        if not deletable:
            return {"status": "ok", "deleted": 0, "skipped": skipped}
        # Capture distinct series ids of the about-to-be-deleted rows
        # so we can recompute authority on each after the DELETE.
        deletable_set = set(deletable)
        affected_sids = list({
            r["series_id"] for r in rows
            if r["id"] in deletable_set and r["series_id"] is not None
        })
        del_placeholders = ",".join(["?" for _ in deletable])
        await db.execute(
            f"DELETE FROM book_series_suggestions WHERE book_id IN ({del_placeholders})",
            deletable,
        )
        cur = await db.execute(
            f"DELETE FROM books WHERE id IN ({del_placeholders})",
            deletable,
        )
        if affected_sids:
            await _recompute_series_author(db, affected_sids)
        await db.commit()
        return {"status": "ok", "deleted": cur.rowcount, "skipped": skipped}
    finally:
        await db.close()


# ── v2.3.2 source URL editor (book sidebar) ──────────────────────────


async def _read_source_urls(db, bid: int) -> dict:
    """Load `books.source_url` and parse the JSON dict.

    Returns `{}` on missing book (caller decides whether to 404),
    empty/null column, or malformed JSON. Malformed JSON is
    silently dropped — pre-v1.x writes used a plain string format,
    and a few legacy rows may still carry one. The editor will
    overwrite it on the next change.
    """
    row = await (await db.execute(
        "SELECT source_url FROM books WHERE id = ?", (bid,),
    )).fetchone()
    if row is None:
        return {}
    raw = row["source_url"]
    if not raw:
        return {}
    try:
        urls = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return urls if isinstance(urls, dict) else {}


@router.post("/books/{bid}/source-urls")
async def add_source_url(bid: int, data: dict = Body(...)):
    """Parse a pasted URL, canonicalize it, merge into the book's
    `source_url` JSON dict.

    Request body: `{"url": "https://..."}`.

    Behavior:
      * 404 if the book id doesn't exist.
      * 400 if the URL doesn't match any known source (so the user
        can fix the paste rather than silently store nothing).
      * Idempotent: pasting the same URL twice is a no-op write but
        still returns 200 with the current dict.
      * If the source already has a URL, the new (canonical) URL
        replaces it. The user adds a Goodreads URL, later edits and
        pastes a different Goodreads URL → second one wins. To clear
        a source's URL entirely, use the DELETE endpoint.

    Returns the updated `source_url` dict so the frontend can
    re-render without a separate GET.
    """
    raw_url = (data.get("url") or "").strip()
    if not raw_url:
        raise HTTPException(400, "url is required")
    parsed = parse_url(raw_url)
    if parsed is None:
        raise HTTPException(
            400,
            f"Could not identify a known source from URL: {raw_url!r}",
        )
    source_name, canonical = parsed

    db = await get_db()
    try:
        existing = await (await db.execute(
            "SELECT id FROM books WHERE id = ?", (bid,),
        )).fetchone()
        if existing is None:
            raise HTTPException(404, f"book {bid} not found")
        urls = await _read_source_urls(db, bid)
        urls[source_name] = canonical
        await db.execute(
            "UPDATE books SET source_url = ? WHERE id = ?",
            (json.dumps(urls), bid),
        )
        await db.commit()
        return {"source_url": urls, "added": source_name,
                "canonical_url": canonical}
    finally:
        await db.close()


@router.delete("/books/{bid}/source-urls/{source_name}")
async def remove_source_url(bid: int, source_name: str):
    """Drop a single source's URL from this book's `source_url` dict.

    Returns the updated dict. 404 if the book doesn't exist; if the
    source key isn't present, returns 200 with the unchanged dict
    (idempotent — re-clicking the X button is harmless).
    """
    db = await get_db()
    try:
        existing = await (await db.execute(
            "SELECT id FROM books WHERE id = ?", (bid,),
        )).fetchone()
        if existing is None:
            raise HTTPException(404, f"book {bid} not found")
        urls = await _read_source_urls(db, bid)
        if source_name in urls:
            del urls[source_name]
            await db.execute(
                "UPDATE books SET source_url = ? WHERE id = ?",
                (json.dumps(urls) if urls else None, bid),
            )
            await db.commit()
        return {"source_url": urls, "removed": source_name}
    finally:
        await db.close()
