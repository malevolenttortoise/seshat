"""
Series endpoints — list, detail, and v2.3 Series Manager mutations.

  GET    /api/series                — every series the user has at
                                      least one visible book for, with
                                      owned/missing counts and
                                      multi-author flag
  GET    /api/series/{sid}          — full series detail with the
                                      ordered book list and per-book
                                      ownership state
  GET    /api/series/{sid}/authors  — distinct author list for a
                                      series with per-author book
                                      counts (drives the v2.3.3
                                      Manage Members modal)
  POST   /api/series/promote        — internal/escape hatch: merge
                                      2+ per-author rows into one
                                      shared row. No longer surfaced
                                      in the v2.3.3 Series Manager
                                      UI; auto-flip happens via the
                                      author-level endpoints below.
  POST   /api/series/{sid}/demote   — internal/escape hatch: split a
                                      shared row into per-author rows.
                                      Same status as promote — kept
                                      for the calibre_sync auto-detect
                                      path and as a recovery tool.
  POST   /api/series/{sid}/authors  — assign one author's books to
                                      this series; auto-flips
                                      authority on the destination
                                      and on every source series the
                                      books moved off of
  DELETE /api/series/{sid}/authors/{author_id}
                                    — detach all of one author's
                                      books from this series; books
                                      fall back to standalone
  PATCH  /api/series/{sid}          — rename a series
  DELETE /api/series/{sid}          — delete; books fall back to
                                      standalone (series_id=NULL)
  POST   /api/series/{sid}/books    — bulk-add books to this series
  DELETE /api/series/{sid}/books/{book_id} — detach a single book

The mutation endpoints are the v2.3 Series Manager backend. They
exist in addition to (not in place of) the auto-detect path in
calibre_sync.py — which handles the common case (Calibre-organized
shared series like Halo) without user intervention. The mutations
cover edge cases: source-discovered books that aren't yet in
Calibre, manual relabeling, undoing an auto-decision the user
disagreed with.

Authority auto-flip (v2.3.3): every membership-mutating endpoint
calls `_recompute_series_author` on the series whose membership
changed (and on every source series for cross-series moves). The
rule: 1 distinct author → series.author_id = that author;
2+ distinct authors → NULL (shared); 0 books → no-op. So the user
no longer thinks in "promote/demote" verbs — they manage author
membership and authority follows.

Both list/detail endpoints honor the global hidden-book filter so
the totals shown in the UI match what the user actually sees on
book pages.
"""
import logging
import sqlite3
from typing import Iterable
from fastapi import APIRouter, Body, HTTPException, Query

from app.discovery.database import get_db, HF, attach_contributors

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
        # v3.0.0 Phase 7 — multi-author byline on series book cards.
        await attach_contributors(db, rows)
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
async def list_series(
    search: str = Query(None),
    sort: str = Query("name"),
    sort_dir: str = Query("asc"),
    has_missing: bool = Query(None),
    shared: bool = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_empty: bool = Query(False),
):
    """List series with author info, owned/missing counts, multi-author
    flag, plus a `cover_book_id` per row for thumbnail rendering.

    Search (`search=q`) matches series name, primary author name, OR
    any book title in the series. Book-title matches let users find a
    series by remembering an entry rather than the series name itself
    ("the book with the colorful pictures..."). Implemented as a
    subquery against the books table so per-series counts stay
    correct (a row-level WHERE on b.title would shrink the COUNT to
    only the matching books).

    `cover_book_id`: the most cover-worthy book in the series (prefers
    books that have a cover_path / cover_url / audiobookshelf_id over
    those that don't, then falls back to series order). NULL when the
    series has no books. The frontend hits
    `/api/discovery/covers/{cover_book_id}` directly.

    `shared=true` filters to shared rows only (`series.author_id IS NULL`).
    `shared=false` filters to per-author rows. Omit to return both.

    Pagination via `limit` (default 50, max 200) + `offset`. Response
    shape: `{"series": [...], "total": N, "limit": L, "offset": O}`.
    `total` is the count of series matching all filters before
    pagination; the frontend uses it to render "showing X–Y of N".
    """
    db = await get_db()
    try:
        # Per-row cover pick: prefer books with any cover signal, then
        # series_index, then pub_date, then id. Correlated subquery on
        # the books table — fine for our scale (hundreds of series).
        cover_subq = (
            "(SELECT id FROM books WHERE series_id = s.id AND hidden = 0 "
            "ORDER BY CASE WHEN cover_path IS NOT NULL "
            "OR cover_url IS NOT NULL "
            "OR audiobookshelf_id IS NOT NULL THEN 0 ELSE 1 END, "
            "COALESCE(series_index, 9999) ASC, "
            "pub_date ASC, id ASC LIMIT 1)"
        )
        select_cols = (
            f"SELECT s.*, a.name as author_name, "
            f"COUNT(DISTINCT CASE WHEN {HF} THEN b.id END) as book_count, "
            f"SUM(CASE WHEN b.owned=1 AND {HF} THEN 1 ELSE 0 END) as owned_count, "
            f"SUM(CASE WHEN b.owned=0 AND {HF} THEN 1 ELSE 0 END) as missing_count, "
            # v3.0.0 Phase 6 (ADR-0010): flags read the stored author_mode
            # discriminator. contributor_count counts distinct CONTRIBUTORS
            # via book_authors — as a scalar subquery, NOT a join, so it
            # doesn't fan out the SUM() aggregates above.
            f"CASE WHEN s.author_mode = 'multi_author' THEN 1 ELSE 0 END as multi_author, "
            f"CASE WHEN s.author_mode = 'shared' THEN 1 ELSE 0 END as is_shared, "
            f"(SELECT COUNT(DISTINCT ba.author_id) FROM book_authors ba "
            f" JOIN books b2 ON b2.id = ba.book_id "
            f" WHERE b2.series_id = s.id AND b2.hidden = 0) as contributor_count, "
            f"{cover_subq} as cover_book_id"
        )
        from_join = (
            " FROM series s "
            "LEFT JOIN authors a ON s.author_id=a.id "
            "LEFT JOIN books b ON s.id=b.series_id"
        )
        where_params: list = []
        where_clauses: list[str] = []
        if search:
            # Book-title match goes through a subquery so the row-level
            # filter doesn't poison the per-series book_count aggregation.
            where_clauses.append(
                "(s.name LIKE ? OR a.name LIKE ? OR s.id IN ("
                "SELECT series_id FROM books "
                "WHERE title LIKE ? AND series_id IS NOT NULL))"
            )
            where_params.extend([f"%{search}%"] * 3)
        if shared is True:
            where_clauses.append("s.author_id IS NULL")
        elif shared is False:
            where_clauses.append("s.author_id IS NOT NULL")
        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        # v2.3.4.2: hide series with zero VISIBLE books from the
        # default list — captures both fully-hidden series ("2B
        # Trilogy" — Mark hid all 3 books, the row stayed at
        # book_count=0) and genuinely-orphaned series with no
        # books at all (auto-detect or rename leftovers). Pass
        # `include_empty=true` to surface them for cleanup.
        # `has_missing` already implies book_count > 0 since
        # missing_count counts visible books too — keep it as the
        # tighter filter when set.
        having_clauses: list[str] = []
        if has_missing:
            having_clauses.append("missing_count > 0")
        elif not include_empty:
            having_clauses.append("book_count > 0")
        having_sql = (" HAVING " + " AND ".join(having_clauses)) if having_clauses else ""
        d = "DESC" if sort_dir == "desc" else "ASC"
        order_sql = {
            "missing": f" ORDER BY missing_count {d}",
            "author": f" ORDER BY a.sort_name {d}",
        }.get(sort, f" ORDER BY s.name {d}")

        # Total count of matching series (pre-pagination). We re-select
        # s.id from the same FROM/WHERE/GROUP BY/HAVING and count the
        # outer rows. Using GROUP BY in a subquery + COUNT(*) outer
        # gives an accurate post-HAVING count.
        count_sql = (
            f"SELECT COUNT(*) AS n FROM ("
            f"SELECT s.id, "
            f"COUNT(DISTINCT CASE WHEN {HF} THEN b.id END) as book_count, "
            f"SUM(CASE WHEN b.owned=0 AND {HF} THEN 1 ELSE 0 END) as missing_count"
            f"{from_join}{where_sql} GROUP BY s.id{having_sql})"
        )
        total = (await (await db.execute(
            count_sql, where_params,
        )).fetchone())["n"]

        # Paginated rows.
        rows_sql = (
            f"{select_cols}{from_join}{where_sql}"
            f" GROUP BY s.id{having_sql}{order_sql}"
            f" LIMIT ? OFFSET ?"
        )
        rows = await (await db.execute(
            rows_sql, [*where_params, limit, offset],
        )).fetchall()

        return {
            "series": [dict(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        await db.close()


# ── v2.3 Series Manager mutations ────────────────────────────────────


async def _series_or_404(db, sid: int) -> dict:
    row = await (await db.execute(
        "SELECT id, name, author_id FROM series WHERE id = ?", (sid,)
    )).fetchone()
    if not row:
        raise HTTPException(404, f"series {sid} not found")
    return dict(row)


async def _recompute_series_author(db, sids: Iterable[int]) -> None:
    """Recompute `series.author_mode` + `series.author_id` from current
    membership for each series id passed in (v3.0.0 Phase 6, ADR-0010).

    Computes over the **intersection** `I` of the series' visible books'
    contributor sets (`book_authors`) — the authors present in EVERY
    book (the "owner set"):

      - `|I| == 1` → `author_mode='per_author'`, `author_id` = that owner.
      - `|I| >= 2` → `author_mode='multi_author'`, `author_id` = a
        deterministic anchor from `I` (most-common primary across the
        books; tiebreak lowest author_id). Non-NULL.
      - `|I| == 0` → `author_mode='shared'`, `author_id = NULL`.
      - 0 visible contributor-bearing books → no-op (orphaned or
        fully-hidden series; leave shape alone so a freshly-emptied row
        doesn't change before the caller can delete it).

    Hidden books are excluded — "hidden = ignore". A per-author Alice
    series with one hidden Bob book stays per-author Alice (the un-hide
    endpoint re-runs this helper so authority catches up).

    Keying on the intersection (not distinct primaries) is what makes a
    co-authored-team series multi_author (Galaxy's Edge → owners Chaney +
    Anspach) while a guest co-author on a single book stays incidental
    (the series stays per_author its through-line author).

    `author_id` coexists as an owner pointer so `is_shared =
    (author_id IS NULL)` keeps meaning shared-only; `author_mode` is
    what distinguishes per_author from multi_author (both non-NULL).

    Callers pass every affected series id (destination + every source
    series). Duplicates are fine — deduped inside. Does NOT commit —
    caller owns the transaction.
    """
    seen: set[int] = set()
    for sid in sids:
        if sid is None or sid in seen:
            continue
        seen.add(sid)
        # Visible books + their book_authors contributor sets, plus each
        # book's legacy primary (for the multi_author anchor tiebreak).
        rows = await (await db.execute(
            "SELECT b.id AS book_id, b.author_id AS primary_author_id, "
            "       ba.author_id AS contributor_id "
            "FROM books b "
            "LEFT JOIN book_authors ba ON ba.book_id = b.id "
            "WHERE b.series_id = ? AND b.hidden = 0",
            (sid,),
        )).fetchall()
        per_book: dict[int, set[int]] = {}
        primary_of: dict[int, int] = {}
        for r in rows:
            bid = r["book_id"]
            per_book.setdefault(bid, set())
            if r["contributor_id"] is not None:
                per_book[bid].add(r["contributor_id"])
            if r["primary_author_id"] is not None:
                primary_of[bid] = r["primary_author_id"]
        # Only books that actually carry contributor links count toward
        # the intersection (an empty set would zero it spuriously).
        book_sets = [s for s in per_book.values() if s]
        if not book_sets:
            continue  # nothing to compute authority from — leave as-is
        intersection = set.intersection(*book_sets)

        if len(intersection) == 1:
            mode = "per_author"
            owner = next(iter(intersection))
        elif len(intersection) >= 2:
            mode = "multi_author"
            # Anchor = most-common primary among the intersection;
            # tiebreak lowest author_id (deterministic).
            primary_counts: dict[int, int] = {}
            for bid, pa in primary_of.items():
                if pa in intersection:
                    primary_counts[pa] = primary_counts.get(pa, 0) + 1
            owner = max(
                intersection,
                key=lambda a: (primary_counts.get(a, 0), -a),
            )
        else:
            mode = "shared"
            owner = None

        if owner is None:
            # NULL is distinct in SQLite UNIQUE — never collides.
            await db.execute(
                "UPDATE series SET author_mode = 'shared', author_id = NULL "
                "WHERE id = ?",
                (sid,),
            )
        else:
            # A series with the same (name, author_id) might already
            # exist — UNIQUE would fire. Degrade gracefully: still record
            # author_mode, leave author_id unchanged, and log.
            try:
                await db.execute(
                    "UPDATE series SET author_mode = ?, author_id = ? WHERE id = ?",
                    (mode, owner, sid),
                )
            except sqlite3.IntegrityError:
                logger.warning(
                    "series %s flip to author_mode=%s (author_id=%s) "
                    "blocked by UNIQUE(name, author_id); recording mode "
                    "only, leaving author_id unchanged",
                    sid, mode, owner,
                )
                await db.execute(
                    "UPDATE series SET author_mode = ? WHERE id = ?",
                    (mode, sid),
                )


@router.post("/series/promote")
async def promote_series(payload: dict = Body(...)):
    """Promote 2+ per-author series rows into a single shared row.

    Request body:
      {
        "series_ids": [10, 11, 12, ...],   # required, at least 2
        "name": "Halo"                      # optional override; if
                                            # omitted, uses the name
                                            # from the first series_id
      }

    Behavior:
      1. All listed series IDs must currently exist and have
         author_id IS NOT NULL (already shared rows can't be promoted
         again).
      2. Pick (or accept) the canonical shared name.
      3. UPSERT the shared row keyed on (LOWER(name), author_id IS NULL).
         Re-uses an existing shared row by that name if one exists,
         otherwise INSERTs a fresh one.
      4. UPDATE every book pointing at any of the source rows to
         point at the shared row instead.
      5. DELETE the source rows.

    Idempotent on accidental re-runs: a second promote with the same
    series_ids 404s on the now-deleted rows. Wrap in a single
    transaction so partial failure doesn't leave a half-merged state.
    """
    series_ids = payload.get("series_ids") or []
    if not isinstance(series_ids, list) or len(series_ids) < 2:
        raise HTTPException(400, "series_ids must be a list of 2+ ids")

    db = await get_db()
    try:
        # Validate all rows + collect names. Reject if any is already
        # shared (author_id IS NULL) — the user should pick a different
        # action.
        ph = ",".join("?" * len(series_ids))
        rows = await (await db.execute(
            f"SELECT id, name, author_id FROM series WHERE id IN ({ph})",
            series_ids,
        )).fetchall()
        rows = [dict(r) for r in rows]
        if len(rows) != len(series_ids):
            found = {r["id"] for r in rows}
            missing = [sid for sid in series_ids if sid not in found]
            raise HTTPException(404, f"series not found: {missing}")
        already_shared = [r["id"] for r in rows if r["author_id"] is None]
        if already_shared:
            raise HTTPException(
                400,
                f"already-shared series cannot be promoted: {already_shared}",
            )

        canonical_name = (payload.get("name") or rows[0]["name"]).strip()
        if not canonical_name:
            raise HTTPException(400, "name must not be empty")

        # Find or create the shared row.
        shared_row = await (await db.execute(
            "SELECT id FROM series WHERE LOWER(name) = LOWER(?) "
            "AND author_id IS NULL",
            (canonical_name,),
        )).fetchone()
        if shared_row:
            shared_id = shared_row["id"]
        else:
            cur = await db.execute(
                "INSERT INTO series (name, author_id, author_mode) "
                "VALUES (?, NULL, 'shared')",
                (canonical_name,),
            )
            shared_id = cur.lastrowid
        # v3.0.0 Phase 6 (ADR-0010): a promoted row is shared by user
        # intent — keep author_mode consistent with author_id=NULL.
        # (Membership changes re-run _recompute_series_author, and the
        # startup backfill recomputes the accurate mode after restart.)
        await db.execute(
            "UPDATE series SET author_mode = 'shared' WHERE id = ?",
            (shared_id,),
        )

        # Re-link books from every source row to the shared row, then
        # delete the source rows. Skip the shared_id itself if it
        # somehow appeared in the input list.
        old_ids = [r["id"] for r in rows if r["id"] != shared_id]
        if not old_ids:
            await db.commit()
            return {"shared_id": shared_id, "promoted_from": [],
                    "books_moved": 0}
        ph_old = ",".join("?" * len(old_ids))
        cur = await db.execute(
            f"UPDATE books SET series_id = ? WHERE series_id IN ({ph_old})",
            (shared_id, *old_ids),
        )
        books_moved = cur.rowcount or 0
        await db.execute(
            f"DELETE FROM series WHERE id IN ({ph_old})", old_ids,
        )
        await db.commit()

        return {
            "shared_id": shared_id,
            "promoted_from": old_ids,
            "books_moved": books_moved,
        }
    finally:
        await db.close()


@router.post("/series/{sid}/demote")
async def demote_series(sid: int):
    """Split a shared series row into per-author rows.

    For each distinct author whose books currently point at this
    shared row:
      1. UPSERT a per-author row with the same name (matching the
         author-scoped lookup that lookup.py and calibre_sync use).
      2. UPDATE that author's books to point at the per-author row.
    Then DELETE the shared row.

    400 if the row isn't shared (author_id IS NOT NULL).
    400 if the shared row has no books — there's nothing to split,
    just call DELETE instead.
    """
    db = await get_db()
    try:
        row = await _series_or_404(db, sid)
        if row["author_id"] is not None:
            raise HTTPException(
                400, "series is not shared (author_id is not NULL)"
            )

        author_rows = await (await db.execute(
            "SELECT DISTINCT author_id FROM books "
            "WHERE series_id = ? AND author_id IS NOT NULL",
            (sid,),
        )).fetchall()
        author_ids = [r["author_id"] for r in author_rows]
        if not author_ids:
            raise HTTPException(
                400, "shared series has no books to split"
            )

        new_series_ids = []
        books_moved_total = 0
        for aid in author_ids:
            # Re-use an existing per-author row by name if one happens
            # to exist (it shouldn't normally, but be safe).
            existing = await (await db.execute(
                "SELECT id FROM series WHERE LOWER(name) = LOWER(?) "
                "AND author_id = ?",
                (row["name"], aid),
            )).fetchone()
            if existing:
                new_id = existing["id"]
            else:
                # v3.0.0 Phase 6 (ADR-0010): demote splits by primary
                # author → the new rows are per_author by intent.
                cur = await db.execute(
                    "INSERT INTO series (name, author_id, author_mode) "
                    "VALUES (?, ?, 'per_author')",
                    (row["name"], aid),
                )
                new_id = cur.lastrowid
            cur = await db.execute(
                "UPDATE books SET series_id = ? "
                "WHERE series_id = ? AND author_id = ?",
                (new_id, sid, aid),
            )
            books_moved_total += cur.rowcount or 0
            new_series_ids.append(new_id)

        await db.execute("DELETE FROM series WHERE id = ?", (sid,))
        await db.commit()

        return {
            "demoted_from": sid,
            "new_series_ids": new_series_ids,
            "books_moved": books_moved_total,
        }
    finally:
        await db.close()


@router.patch("/series/{sid}")
async def rename_series(sid: int, payload: dict = Body(...)):
    """Rename a series.

    Request body: {"name": "New Name"}

    Conflict behavior: if another series row already has the same
    (name, author_id) — including (name, NULL) for shared — return
    409 with the conflicting row's id so the caller can offer a
    "merge into existing" affordance instead of forcing a duplicate.
    """
    new_name = (payload.get("name") or "").strip()
    if not new_name:
        raise HTTPException(400, "name must not be empty")

    db = await get_db()
    try:
        row = await _series_or_404(db, sid)
        if new_name == row["name"]:
            return {"id": sid, "name": new_name, "noop": True}

        # Conflict check uses the same composite as the UNIQUE
        # constraint: (LOWER(name), author_id) where NULL is matched
        # explicitly via IS.
        if row["author_id"] is None:
            conflict_row = await (await db.execute(
                "SELECT id FROM series WHERE LOWER(name) = LOWER(?) "
                "AND author_id IS NULL AND id != ?",
                (new_name, sid),
            )).fetchone()
        else:
            conflict_row = await (await db.execute(
                "SELECT id FROM series WHERE LOWER(name) = LOWER(?) "
                "AND author_id = ? AND id != ?",
                (new_name, row["author_id"], sid),
            )).fetchone()
        if conflict_row:
            raise HTTPException(
                409,
                {"message": "another series row already uses this name",
                 "conflict_id": conflict_row["id"]},
            )

        await db.execute(
            "UPDATE series SET name = ? WHERE id = ?", (new_name, sid),
        )
        await db.commit()
        return {"id": sid, "name": new_name}
    finally:
        await db.close()


@router.delete("/series/{sid}")
async def delete_series(sid: int):
    """Delete a series row. Books pointing at it fall back to
    standalone (series_id=NULL, series_index=NULL).

    Use cases: a bogus series the auto-detect created from a parser
    bug, or cleaning up after a manual mistake. For the common case
    of "this series row is wrong, here's the correct one" prefer
    promote/demote/membership-edit instead.
    """
    db = await get_db()
    try:
        await _series_or_404(db, sid)
        cur = await db.execute(
            "UPDATE books SET series_id = NULL, series_index = NULL "
            "WHERE series_id = ?", (sid,),
        )
        books_orphaned = cur.rowcount or 0
        await db.execute("DELETE FROM series WHERE id = ?", (sid,))
        await db.commit()
        return {"deleted": sid, "books_orphaned": books_orphaned}
    finally:
        await db.close()


@router.post("/series/{sid}/books")
async def add_books_to_series(sid: int, payload: dict = Body(...)):
    """Bulk-add books to a series.

    Request body:
      {
        "book_ids": [1, 2, 3],
        "indices": {"1": 1.0, "2": 2.0}   # optional per-book indices,
                                           # keyed as string for JSON
      }

    Books not listed in `indices` keep their existing series_index
    (which may have been carried over from a previous series). The
    caller can omit `indices` entirely to add books without setting
    indices.

    Auto-flips authority on the destination series and on every
    source series the books moved off of (a 2-author shared series
    that loses its only book by author B flips back to per-author A).
    """
    book_ids = payload.get("book_ids") or []
    if not isinstance(book_ids, list) or not book_ids:
        raise HTTPException(400, "book_ids must be a non-empty list")
    indices = payload.get("indices") or {}

    db = await get_db()
    try:
        await _series_or_404(db, sid)
        # Capture the source series of every moving book BEFORE the
        # update so we can recompute their authority too.
        ph = ",".join("?" * len(book_ids))
        prev_rows = await (await db.execute(
            f"SELECT DISTINCT series_id FROM books "
            f"WHERE id IN ({ph}) AND series_id IS NOT NULL",
            book_ids,
        )).fetchall()
        affected_sids = {sid} | {r["series_id"] for r in prev_rows}

        added = 0
        for bid in book_ids:
            idx = indices.get(str(bid))
            if idx is not None:
                await db.execute(
                    "UPDATE books SET series_id = ?, series_index = ? "
                    "WHERE id = ?",
                    (sid, idx, bid),
                )
            else:
                await db.execute(
                    "UPDATE books SET series_id = ? WHERE id = ?",
                    (sid, bid),
                )
            added += 1
        await _recompute_series_author(db, affected_sids)
        await db.commit()
        return {"added": added, "series_id": sid}
    finally:
        await db.close()


@router.delete("/series/{sid}/books/{book_id}")
async def remove_book_from_series(sid: int, book_id: int):
    """Detach a book from this series. Book becomes standalone
    (series_id=NULL, series_index=NULL). 404 if the book isn't
    actually on this series.

    Auto-flips authority on `sid` after the detach (e.g. if removing
    this book leaves a single distinct author behind, the series
    flips from shared to per-author).
    """
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT id FROM books WHERE id = ? AND series_id = ?",
            (book_id, sid),
        )).fetchone()
        if not row:
            raise HTTPException(
                404, f"book {book_id} is not on series {sid}"
            )
        await db.execute(
            "UPDATE books SET series_id = NULL, series_index = NULL "
            "WHERE id = ?", (book_id,),
        )
        await _recompute_series_author(db, [sid])
        await db.commit()
        return {"removed": book_id, "series_id": sid}
    finally:
        await db.close()


# ── v2.3.3 author-level membership endpoints ─────────────────────────


def _authority_label(author_id) -> str:
    """Human-readable string for the series authority; mirrors the
    is_shared flag the list endpoint surfaces."""
    return "shared" if author_id is None else "per_author"


@router.get("/series/{sid}/authors")
async def list_series_authors(sid: int):
    """Distinct author list for a series with per-author book counts.

    Drives the v2.3.3 Manage Members modal. Hidden books are excluded
    (hidden=ignore per Mark's mental model) — an author whose only
    contributions to this series are hidden books will not appear
    here. The book picker in the modal also excludes hidden books
    (`/discovery/books?include_hidden=false` is the default), so the
    modal stays internally consistent.

    Per-author book_count counts visible books only — matches what
    the user sees on the row.
    """
    db = await get_db()
    try:
        await _series_or_404(db, sid)
        # v3.0.0 Phase 7 — surface the stored author_mode (ADR-0010) so the
        # modal header shows the accurate 3-way label (per_author /
        # multi_author / shared) instead of guessing from the author count.
        # The current-authors list below stays grouped by PRIMARY author_id
        # (the Remove affordance detaches by primary) — surfacing pure
        # co-authors here would dead-end Remove (404) or, made
        # contributor-aware, would nuke a whole co-authored series; that
        # membership-management question is deliberately out of Phase 7.
        srow = await (await db.execute(
            "SELECT author_mode FROM series WHERE id = ?", (sid,)
        )).fetchone()
        rows = await (await db.execute(
            "SELECT a.id AS author_id, a.name AS name, "
            "COUNT(b.id) AS book_count "
            "FROM books b JOIN authors a ON a.id = b.author_id "
            "WHERE b.series_id = ? AND b.hidden = 0 "
            "GROUP BY a.id, a.name "
            "ORDER BY a.name COLLATE NOCASE ASC",
            (sid,),
        )).fetchall()
        return {
            "series_id": sid,
            "author_mode": srow["author_mode"] if srow else None,
            "authors": [dict(r) for r in rows],
        }
    finally:
        await db.close()


@router.post("/series/{sid}/authors")
async def add_author_to_series(sid: int, payload: dict = Body(...)):
    """Assign one author's books to this series.

    Request body:
      {
        "author_id": 42,
        "book_ids": [1, 2, 3]   # required, all must belong to author_id
      }

    Behavior:
      1. Validate every book in `book_ids` belongs to `author_id`.
      2. Capture the books' current series_id values BEFORE the
         update — those are the source series we'll need to recompute
         authority on after the move.
      3. UPDATE books.series_id = sid for every listed book. We also
         clear series_index (the index is series-scoped; carrying it
         over to a different series produces gibberish ordering).
      4. Recompute authority on `{sid} | source_series_ids`. The
         destination flips to shared if it now has 2+ distinct
         authors; sources may flip from shared to per-author if the
         move was their last book by this author.

    400 if `book_ids` is empty, `author_id` is missing, or any book
    doesn't belong to `author_id` (the latter rejects the whole
    request rather than silently dropping mismatches).
    """
    author_id = payload.get("author_id")
    book_ids = payload.get("book_ids") or []
    if not isinstance(author_id, int):
        raise HTTPException(400, "author_id (int) is required")
    if not isinstance(book_ids, list) or not book_ids:
        raise HTTPException(400, "book_ids must be a non-empty list")
    if not all(isinstance(b, int) for b in book_ids):
        raise HTTPException(400, "book_ids must be a list of ints")

    db = await get_db()
    try:
        await _series_or_404(db, sid)
        # Validate the author exists. (Authors table is in the same
        # discovery DB; FK constraint isn't enforced by SQLite by
        # default so we check explicitly to give a clean 404.)
        author_row = await (await db.execute(
            "SELECT id, name FROM authors WHERE id = ?", (author_id,),
        )).fetchone()
        if not author_row:
            raise HTTPException(404, f"author {author_id} not found")

        # Validate every book belongs to this author. Reject the whole
        # request on any mismatch — partial moves leave the user in a
        # confusing state.
        ph = ",".join("?" * len(book_ids))
        rows = await (await db.execute(
            f"SELECT id, author_id, series_id FROM books "
            f"WHERE id IN ({ph})",
            book_ids,
        )).fetchall()
        rows = [dict(r) for r in rows]
        if len(rows) != len(book_ids):
            found = {r["id"] for r in rows}
            missing = [b for b in book_ids if b not in found]
            raise HTTPException(404, f"books not found: {missing}")
        # v3.0.0 Phase 6 (ADR-0010): a book "belongs to" the author if the
        # author is ANY contributor (book_authors), not just the legacy
        # primary `books.author_id`. This is the dropdown fix — a
        # co-author (e.g. Anspach on a Chaney-primary co-authored book)
        # can now be associated with the series.
        contrib_rows = await (await db.execute(
            f"SELECT DISTINCT book_id FROM book_authors "
            f"WHERE author_id = ? AND book_id IN ({ph})",
            (author_id, *book_ids),
        )).fetchall()
        contributor_books = {r["book_id"] for r in contrib_rows}
        wrong_author = [bid for bid in book_ids if bid not in contributor_books]
        if wrong_author:
            raise HTTPException(
                400,
                f"books not by author {author_id}: {wrong_author}",
            )

        affected_sids = {sid} | {
            r["series_id"] for r in rows if r["series_id"] is not None
        }

        # Move books into the destination series and clear stale
        # series_index values (the new series's caller can re-set
        # indices via POST /series/{sid}/books with `indices` if they
        # care; the modal flow doesn't, so leaving them at NULL is
        # the right default).
        await db.execute(
            f"UPDATE books SET series_id = ?, series_index = NULL "
            f"WHERE id IN ({ph})",
            (sid, *book_ids),
        )
        await _recompute_series_author(db, affected_sids)

        # Re-read the destination row to report the post-flip state.
        dest = await _series_or_404(db, sid)
        await db.commit()
        return {
            "series_id": sid,
            "added": len(book_ids),
            "authority": _authority_label(dest["author_id"]),
            "source_series_recomputed": sorted(affected_sids - {sid}),
        }
    finally:
        await db.close()


@router.delete("/series/{sid}/authors/{author_id}")
async def remove_author_from_series(sid: int, author_id: int):
    """Detach every book by `author_id` from `sid`.

    Books fall back to standalone (series_id=NULL, series_index=NULL).
    404 if no books on this series belong to that author — protects
    against silent typos in the URL.

    Auto-flips authority on `sid` after the detach. The 2→1 case is
    the common one: a 2-author shared series whose Bob is removed
    flips back to per-author Alice.
    """
    db = await get_db()
    try:
        await _series_or_404(db, sid)
        # Verify there's something to remove.
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM books "
            "WHERE series_id = ? AND author_id = ?",
            (sid, author_id),
        )
        n = (await cur.fetchone())["n"]
        if not n:
            raise HTTPException(
                404,
                f"no books by author {author_id} on series {sid}",
            )

        await db.execute(
            "UPDATE books SET series_id = NULL, series_index = NULL "
            "WHERE series_id = ? AND author_id = ?",
            (sid, author_id),
        )
        await _recompute_series_author(db, [sid])

        # Re-read; the series may now have 0 books, in which case the
        # helper left author_id alone (orphaned). UI can decide whether
        # to prompt for delete on its own.
        dest = await _series_or_404(db, sid)
        await db.commit()
        return {
            "series_id": sid,
            "removed": n,
            "author_id": author_id,
            "authority": _authority_label(dest["author_id"]),
        }
    finally:
        await db.close()
