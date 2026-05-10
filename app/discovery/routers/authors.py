"""
Author endpoints — list, detail, scan triggers, and reset operations.

The author scan triggers in this file all funnel through
`_spawn_lookup_task`, which manages the single-author / single-author-
full-rescan / bulk-authors paths as background asyncio tasks tracked
through `state._lookup_task` + `state._lookup_progress`. This is what
makes the Dashboard widget's "Stop" button work uniformly regardless
of where the scan was kicked off from.

Endpoints:
  GET  /api/authors                       — paginated list with filters
  GET  /api/authors/{aid}                 — detail with series & standalone
  POST /api/authors/{aid}/lookup          — single-author source scan
  POST /api/authors/{aid}/full-rescan     — single-author full re-scan
  POST /api/authors/clear-scan-data       — wipe source/MAM data per author set
  POST /api/authors/scan-sources          — bulk source scan
  POST /api/authors/scan-mam              — bulk MAM scan
  POST /api/sources/reset                 — global source-scan reset
"""
import asyncio
import logging
from typing import Any, Optional
from fastapi import APIRouter, Body, HTTPException, Query

from app import state
from app.config import load_settings
from app.discovery.database import get_db, get_active_library, HF, cleanup_empty_series
from app.discovery.lookup import lookup_author
from app.discovery.cross_library import (
    libraries_for,
    run_across_libraries,
    sort_and_paginate,
    sort_key_for,
)

logger = logging.getLogger("seshat.discovery")

router = APIRouter(prefix="/api/discovery", tags=["authors"])


def _build_authors_sql(search, has_missing, book_type, include_orphans, sort, sort_dir):
    q = (
        f"SELECT a.*, "
        f"COUNT(DISTINCT CASE WHEN {HF} AND COALESCE(b.is_omnibus,0)=0 THEN b.id END) as total_books, "
        f"SUM(CASE WHEN b.owned=1 AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN 1 ELSE 0 END) as owned_count, "
        f"SUM(CASE WHEN b.owned=0 AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN 1 ELSE 0 END) as missing_count, "
        f"SUM(CASE WHEN b.is_new=1 AND b.owned=0 AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN 1 ELSE 0 END) as new_count, "
        f"COUNT(DISTINCT b.series_id) as series_count, "
        f"(SELECT COUNT(*) FROM pen_name_links pl "
        f" WHERE pl.canonical_author_id=a.id OR pl.alias_author_id=a.id) as link_count "
        f"FROM authors a LEFT JOIN books b ON a.id=b.author_id"
    )
    p: list = []; c: list[str] = []
    if search:
        c.append("a.name LIKE ?"); p.append(f"%{search}%")
    if book_type == "series":
        c.append("b.series_id IS NOT NULL")
    elif book_type == "standalone":
        c.append("b.series_id IS NULL")
    if c:
        q += " WHERE " + " AND ".join(c)
    q += " GROUP BY a.id"
    having = []
    if not include_orphans:
        having.append("total_books > 0")
    if has_missing:
        having.append("missing_count > 0")
    if having:
        q += " HAVING " + " AND ".join(having)
    d = "DESC" if sort_dir == "desc" else "ASC"
    q += {
        "missing": f" ORDER BY missing_count {d}, a.sort_name ASC",
        "new": f" ORDER BY new_count {d}, a.sort_name ASC",
        "total": f" ORDER BY total_books {d}, a.sort_name ASC",
    }.get(sort, f" ORDER BY a.sort_name {d}")
    return q, p


@router.get("/authors")
async def get_authors(search: str = Query(None), sort: str = Query("name"), sort_dir: str = Query("asc"), has_missing: bool = Query(None), book_type: str = Query(None), include_orphans: bool = Query(False), content_type: str = Query(None)):
    """List authors.

    `content_type` selects active-library (omitted) vs. cross-library
    aggregation ("ebook" / "audiobook" / "all"). In cross-library
    mode, authors with the same normalized name across libraries get
    their per-library stats merged so a user with Calibre + ABS sees
    one "Pierce Brown" row with owned/missing counts summed — not one
    row per library.

    By default, "orphan" authors with zero linked book rows are hidden.
    `?include_orphans=true` shows everything.
    """
    if content_type:
        sql, params = _build_authors_sql(
            search, has_missing, book_type, include_orphans, sort, sort_dir,
        )

        async def q(db):
            rows = await (await db.execute(sql, params)).fetchall()
            return [dict(r) for r in rows]

        rows = await run_across_libraries(content_type, q)
        # Merge per-normalized-name so Pierce Brown in Calibre and
        # Pierce Brown in ABS collapse to one row with summed stats.
        from app.works.normalize import normalize_author
        merged: dict[str, dict] = {}
        for r in rows:
            key = normalize_author(r.get("name", ""))
            if not key:
                continue
            if key in merged:
                base = merged[key]
                for counter in ("total_books", "owned_count", "missing_count",
                                "new_count", "series_count"):
                    base[counter] = (base.get(counter) or 0) + (r.get(counter) or 0)
                # Track which libraries + per-library ids the author
                # appears in — frontend uses these to navigate into
                # the right library's author-detail page.
                base["library_slugs"].append(r["library_slug"])
                base["author_ids_by_slug"][r["library_slug"]] = r.get("id")
            else:
                merged[key] = {
                    **r,
                    "library_slugs": [r["library_slug"]],
                    "author_ids_by_slug": {r["library_slug"]: r.get("id")},
                }
        sort_fn = {
            "missing": lambda x: (-(x.get("missing_count") or 0), (x.get("sort_name") or "").lower()),
            "new": lambda x: (-(x.get("new_count") or 0), (x.get("sort_name") or "").lower()),
            "total": lambda x: (-(x.get("total_books") or 0), (x.get("sort_name") or "").lower()),
        }.get(sort, lambda x: ((x.get("sort_name") or x.get("name") or "").lower(),))
        reverse = sort_dir == "desc" and sort not in ("missing", "new", "total")
        authors = sorted(merged.values(), key=sort_fn, reverse=reverse)
        return {"authors": authors}

    db = await get_db()
    try:
        sql, p = _build_authors_sql(
            search, has_missing, book_type, include_orphans, sort, sort_dir,
        )
        return {"authors": [dict(r) for r in await (await db.execute(sql, p)).fetchall()]}
    finally:
        await db.close()


async def _author_detail_for_slug(slug: str, aid: int) -> Optional[dict]:
    """Fetch the full author detail (author + series + standalone) from a specific library.

    Returns None when the author id isn't in that library. Used by the
    cross-library fan-out below so the detail page can show both
    ebook and audiobook sections of a merged author.

    Books returned under `standalone_books` are stamped with
    `library_slug` + `content_type` so the frontend's
    `coverSrcFor` picks the per-library cover endpoint. Without this
    the cover-src fell back to the active-library path and served
    unrelated books' covers.
    """
    content_type = next(
        (l.get("content_type", "ebook") for l in state._discovered_libraries
         if l.get("slug") == slug),
        "ebook",
    )
    db = await get_db(slug)
    try:
        r = await (await db.execute("SELECT * FROM authors WHERE id=?", (aid,))).fetchone()
        if not r:
            return None
        a = dict(r)
        # The HAVING filter drops series where every book by this
        # author is hidden — without it the tile still renders as
        # "(0/0)" after a user hides the last visible book in a
        # source-scan-populated series. The count uses
        # `author_visible_count` (omnibus included) so a series whose
        # only book by this author is an omnibus still renders; the
        # IS section surfaces the omnibus under "Omnibus / Collections".
        # `author_book_count` (omnibus EXCLUDED) is what the count
        # badge displays so progress reflects actual entries, not
        # collections.
        a["series"] = [dict(s) for s in await (await db.execute(
            f"""SELECT s.*,
                COUNT(DISTINCT CASE WHEN {HF} AND COALESCE(b.is_omnibus,0)=0 THEN b.id END) as book_count,
                COUNT(DISTINCT CASE WHEN b.author_id=? AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN b.id END) as author_book_count,
                COUNT(DISTINCT CASE WHEN b.author_id=? AND {HF} THEN b.id END) as author_visible_count,
                COUNT(DISTINCT CASE WHEN b.author_id=? AND {HF} AND COALESCE(b.is_omnibus,0)=1 THEN b.id END) as author_omnibus_count,
                SUM(CASE WHEN b.owned=1 AND b.author_id=? AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN 1 ELSE 0 END) as owned_count,
                SUM(CASE WHEN b.owned=0 AND b.author_id=? AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN 1 ELSE 0 END) as missing_count,
                CASE WHEN COUNT(DISTINCT b.author_id) > 1 THEN 1 ELSE 0 END as multi_author
            FROM series s
            JOIN books b ON s.id=b.series_id
            WHERE s.id IN (SELECT DISTINCT series_id FROM books WHERE author_id=? AND series_id IS NOT NULL)
            GROUP BY s.id
            HAVING author_visible_count > 0
            ORDER BY s.name""",
            (aid, aid, aid, aid, aid, aid)
        )).fetchall()]
        standalone = [
            {**dict(b), "library_slug": slug, "content_type": content_type}
            for b in await (await db.execute(
                f"SELECT b.*, a2.name as author_name FROM books b JOIN authors a2 ON b.author_id=a2.id "
                f"WHERE b.author_id=? AND b.series_id IS NULL AND {HF} ORDER BY b.pub_date ASC, b.title ASC",
                (aid,)
            )).fetchall()
        ]
        # Cross-format sibling info so the UI can render "also
        # available as audiobook" badges on ebook cards and vice
        # versa. Series books get stamped by the series endpoint
        # on its own fetch; here we only need to cover the
        # standalone list.
        from app.works.storage import get_siblings_for_books
        ids = [int(b["id"]) for b in standalone if b.get("id") is not None]
        if slug and ids:
            sib_map = await get_siblings_for_books(slug, ids)
            for b in standalone:
                s = sib_map.get(int(b["id"]))
                if s:
                    b["work_id"] = s[0].work_id
                    b["work_siblings"] = [
                        {"library_slug": w.library_slug, "book_id": w.book_id,
                         "content_type": w.content_type}
                        for w in s
                    ]
        a["standalone_books"] = standalone
        return a
    finally:
        await db.close()


@router.get("/authors/{aid}")
async def get_author(aid: int, include_cross_library: bool = False, slug: Optional[str] = None):
    """Return an author's detail (series + standalone + stats).

    `slug=X` overrides which library the `aid` belongs to. Without it
    we fall back to the active library. This matters when the user
    clicks a merged author row whose `id` came from a non-active
    library — e.g. Troy Denning's id 5 in ABS is Jack Bryce's id 5
    in Calibre, so the frontend MUST pass the source slug or we
    resolve the wrong person.

    `include_cross_library=1` additionally looks up the author in every
    OTHER discovered library by normalized name and returns those
    library's detail under `cross_library` keyed by slug. The frontend
    uses this to render Ebook / Audiobook tabs on the merged authors
    detail view. Single-library installs or unmatched names return
    an empty `cross_library` dict — callers should treat its presence
    as the signal to show tabs, not absence.
    """
    primary_slug = slug or get_active_library()
    a = await _author_detail_for_slug(primary_slug, aid)
    if a is None:
        raise HTTPException(404)

    if include_cross_library:
        from app.works.normalize import normalize_author
        target_norm = normalize_author(a["name"])
        cross: dict[str, Any] = {}
        if target_norm:
            for lib in state._discovered_libraries:
                if lib["slug"] == primary_slug:
                    continue
                # Find an author in the other library whose normalized
                # name matches. We pull every author row and compare
                # in Python because SQLite lacks a portable
                # equivalent to our Python-side normalize_author —
                # author counts per library are small (low thousands)
                # so this is fine.
                other_db = await get_db(lib["slug"])
                try:
                    rows = await (await other_db.execute(
                        "SELECT id, name FROM authors WHERE id IN "
                        "(SELECT DISTINCT author_id FROM books)"
                    )).fetchall()
                finally:
                    await other_db.close()
                match_id = None
                for row in rows:
                    if normalize_author(row["name"]) == target_norm:
                        match_id = row["id"]
                        break
                if match_id is None:
                    continue
                detail = await _author_detail_for_slug(lib["slug"], match_id)
                if detail is None:
                    continue
                cross[lib["slug"]] = {
                    "library_name": lib.get("display_name") or lib.get("name") or lib["slug"],
                    "content_type": lib.get("content_type", "ebook"),
                    "app_type": lib.get("app_type", ""),
                    "author": detail,
                }
        a["cross_library"] = cross
        a["active_library_slug"] = primary_slug
        a["active_content_type"] = next(
            (l.get("content_type", "ebook") for l in state._discovered_libraries
             if l["slug"] == primary_slug),
            "ebook",
        )
    return a


def _spawn_lookup_task(scan_type: str, total: int, runner) -> None:
    """Spawn `runner` as a background asyncio task tracked by state._lookup_task.

    Single-author and bulk-author scans run as real background tasks
    so the Dashboard's Stop button can cancel them via the standard
    `/lookup/cancel` endpoint — that endpoint only knows about
    `_lookup_task`, so any scan that doesn't register itself there
    silently dodges the user's cancel request.

    Endpoints that call this return immediately with
    `{"status": "started"}`. The frontend polls `/api/scan-status`
    (and listens for the `seshat:scan-started` window event)
    to surface progress and completion.

    `runner` is a zero-arg async callable that returns when the work
    is done. Exceptions inside it are caught and stored in
    `_lookup_progress["status"]` so the unified widget can surface
    them.
    """
    if state._lookup_progress.get("running"):
        raise HTTPException(409, "An author scan is already running")
    if state._lookup_task and not state._lookup_task.done():
        raise HTTPException(409, "An author scan is already running")

    state._lookup_progress = {
        "running": True, "checked": 0, "total": total, "current_author": "",
        "current_book": "",
        "new_books": 0, "status": "scanning", "type": scan_type,
    }

    async def _do():
        try:
            await runner()
            state._lookup_progress.update({"running": False, "status": "complete"})
            try:
                from app.discovery.notify import notify_scan_complete
                # Pick a friendly label per scan_type. For single-author
                # scans, the runner already wrote `current_author` into
                # state — use it so the notification reads
                # "Scan complete: William D. Arand" rather than
                # "Author Scan complete".
                if scan_type in ("single_author", "single_author_full"):
                    label = state._lookup_progress.get("current_author") or "Author"
                    authors_total = 1
                else:
                    label = {
                        "bulk_authors": "Bulk Author Scan",
                        "bulk_books":   "Bulk Book Scan",
                    }.get(scan_type, "Author Scan")
                    authors_total = int(state._lookup_progress.get("total", 0) or 1)
                await notify_scan_complete(
                    label=label,
                    new_books=int(state._lookup_progress.get("new_books", 0)),
                    authors_total=authors_total,
                )
            except Exception:
                logger.debug("author-scan notify failed", exc_info=True)
            # In-browser toast parallel to the ntfy push. The
            # `trigger_lookup` endpoint in scan.py has its own toast
            # for the Dashboard-originated full scan — this covers the
            # other code path (single-author lookup, full-rescan, bulk
            # scans triggered from the author detail page, etc.) which
            # all route through _spawn_lookup_task.
            try:
                from app.orchestrator.sse_publishers import publish_toast
                new_books = int(state._lookup_progress.get("new_books", 0))
                await publish_toast(
                    "success",
                    f"{label} scan complete: {new_books} new book(s)",
                )
            except Exception:
                logger.debug("author-scan toast failed", exc_info=True)
        except asyncio.CancelledError:
            # User clicked Stop on the Dashboard widget. Mark cancelled
            # and let the exception propagate so any further `await` in
            # the runner unwinds cleanly.
            state._lookup_progress.update({"running": False, "status": "cancelled"})
            raise
        except Exception as e:
            logger.error(f"Author scan task error: {e}", exc_info=True)
            state._lookup_progress.update({"running": False, "status": f"error: {e}"})
            try:
                from app.orchestrator.sse_publishers import publish_toast
                await publish_toast("error", f"Author scan failed: {e}")
            except Exception:
                logger.debug("author-scan error toast failed", exc_info=True)

    state._lookup_task = asyncio.create_task(_do())


@router.post("/authors/{aid}/lookup")
async def trigger_author_lookup(aid: int, slug: Optional[str] = None):
    """Run a source scan for one author in a specific library.

    `slug=X` temporarily sets the active library to X for the duration
    of the scan, then restores the original on finish. Needed because
    `lookup_author` + every downstream helper calls `get_db()` with no
    slug argument — threading slug through every site is invasive
    and `_lookup_task` already serializes scan work. The primary-
    author ID in the URL refers to THIS library's author id, so the
    flip is required or we resolve the wrong person.
    """
    s = load_settings()
    if not s.get("author_scanning_enabled", True):
        raise HTTPException(400, "Author scanning is disabled — enable it in Settings")
    from app.discovery.database import set_active_library as _set_active
    original_slug = get_active_library()
    target_slug = slug or original_slug
    db = await get_db(target_slug)
    try:
        r = await (await db.execute("SELECT * FROM authors WHERE id=?", (aid,))).fetchone()
        if not r:
            raise HTTPException(404)
    finally:
        await db.close()
    name = dict(r)["name"]
    flip = bool(slug and slug != original_slug)

    async def _runner():
        if flip:
            _set_active(slug)
        try:
            state._lookup_progress.update({"current_author": name})
            # Surface running new_books count after each source so the
            # widget climbs in real time instead of jumping 0 → final.
            def _on_source(running):
                state._lookup_progress["new_books"] = int(running)
            new_books = await lookup_author(aid, name, on_progress=_on_source)
            state._lookup_progress.update({
                "checked": 1, "new_books": int(new_books or 0),
            })
        finally:
            if flip and original_slug:
                _set_active(original_slug)

    _spawn_lookup_task("single_author", total=1, runner=_runner)
    return {"status": "started", "author": name}


@router.post("/authors/{aid}/full-rescan")
async def trigger_author_full_rescan(aid: int, slug: Optional[str] = None):
    """Full re-scan for a single author. `slug` scoped the same as /lookup."""
    s = load_settings()
    if not s.get("author_scanning_enabled", True):
        raise HTTPException(400, "Author scanning is disabled — enable it in Settings")
    from app.discovery.database import set_active_library as _set_active
    original_slug = get_active_library()
    target_slug = slug or original_slug
    db = await get_db(target_slug)
    try:
        r = await (await db.execute("SELECT * FROM authors WHERE id=?", (aid,))).fetchone()
        if not r:
            raise HTTPException(404)
    finally:
        await db.close()
    name = dict(r)["name"]
    flip = bool(slug and slug != original_slug)

    async def _runner():
        if flip:
            _set_active(slug)
        try:
            state._lookup_progress.update({"current_author": name})
            def _on_source(running):
                state._lookup_progress["new_books"] = int(running)
            new_books = await lookup_author(aid, name, full_scan=True, on_progress=_on_source)
            state._lookup_progress.update({
                "checked": 1, "new_books": int(new_books or 0),
            })
        finally:
            if flip and original_slug:
                _set_active(original_slug)

    _spawn_lookup_task("single_author_full", total=1, runner=_runner)
    return {"status": "started", "author": name}


async def _clear_authors_in_library(
    db, author_ids: list[int], clear_source: bool, clear_mam: bool,
) -> int:
    """Run the clear-scan-data SQL against one library's DB.

    Returns the number of books deleted (only set when `clear_source`).
    Caller owns commit + connection lifecycle.
    """
    placeholders = ",".join(["?" for _ in author_ids])
    affected = 0
    if clear_source:
        count_row = await db.execute_fetchall(
            f"SELECT COUNT(*) FROM books WHERE author_id IN ({placeholders}) "
            f"AND owned=0 AND calibre_id IS NULL",
            author_ids,
        )
        affected = count_row[0][0] if count_row else 0
        await db.execute(
            f"DELETE FROM books WHERE author_id IN ({placeholders}) "
            f"AND owned=0 AND calibre_id IS NULL",
            author_ids,
        )
        await db.execute(
            f"UPDATE books SET source_url=NULL WHERE author_id IN "
            f"({placeholders}) AND owned=1",
            author_ids,
        )
        await db.execute(
            f"UPDATE authors SET last_lookup_at=NULL WHERE id IN ({placeholders})",
            author_ids,
        )
    if clear_mam:
        await db.execute(
            f"UPDATE books SET mam_url=NULL, mam_status=NULL, mam_formats=NULL, "
            f"mam_torrent_id=NULL, mam_has_multiple=0, mam_my_snatched=0, "
            f"mam_is_bundle=0 "
            f"WHERE author_id IN ({placeholders})",
            author_ids,
        )
    return affected


@router.post("/authors/clear-scan-data")
async def clear_author_scan_data(data: dict = Body(...)):
    """Clear source and/or MAM scan data for specified authors.

    `content_type` optional: "ebook" or "audiobook" scopes the clear
    to libraries of that type. "all" or omitted clears across every
    discovered library — matches how the cross-library Authors view
    aggregates, so a Clear from that view actually wipes every copy
    of the author. Without this split, a user on the cross-library
    view clicks Clear Source and only the currently-active library
    gets touched, silently leaving the other copy's data behind.

    `author_names`: optional list of author display names. When
    provided AND content_type is set, each library resolves names to
    its OWN local author IDs, sidestepping the cross-library ID
    collision class — see /authors/scan-sources docstring for the
    Touko Amekawa / Roger Black canary explanation. The author_ids
    parameter is still accepted as a fallback for callers that don't
    have names handy.
    """
    author_ids = data.get("author_ids", [])
    author_names = data.get("author_names")
    clear_source = data.get("clear_source", False)
    clear_mam = data.get("clear_mam", False)
    content_type = data.get("content_type")
    if not author_ids and not author_names:
        return {"error": "No authors specified"}
    if not clear_source and not clear_mam:
        return {"error": "Nothing to clear — specify clear_source and/or clear_mam"}

    # Build the library list. `content_type=None` (active-lib only) is
    # the pre-refactor behavior and remains the default so callers that
    # don't know about libraries keep working.
    if content_type is None:
        target_libs = None  # signal: use active library via get_db()
    else:
        target_libs = libraries_for(content_type)
        if not target_libs:
            return {"status": "ok", "authors_cleared": 0, "books_deleted": 0,
                    "message": f"No {content_type} libraries found."}

    total_deleted = 0
    libs_touched = 0
    if target_libs is None:
        db = await get_db()
        try:
            total_deleted += await _clear_authors_in_library(
                db, author_ids, clear_source, clear_mam,
            )
            await db.commit()
            if clear_source and total_deleted > 0:
                cleaned = await cleanup_empty_series(db)
                if cleaned:
                    logger.info(f"  Empty series cleanup: removed {cleaned} orphaned series")
            libs_touched = 1
        finally:
            await db.close()
    else:
        # Cross-library clear. When author_names was supplied, each
        # library resolves its OWN local author IDs by name — the
        # POSTed `author_ids` are merged-response IDs that mean nothing
        # outside the originating library, so we can't use them across
        # the iteration. Names are the only reliable cross-library
        # identifier (and they're already the merge key the cross-
        # library Authors view uses to aggregate rows).
        for lib in target_libs:
            slug = lib.get("slug")
            if not slug:
                continue
            db = await get_db(slug)
            try:
                if author_names:
                    ph = ",".join(["?" for _ in author_names])
                    name_rows = await db.execute_fetchall(
                        f"SELECT id FROM authors WHERE name IN ({ph})",
                        list(author_names),
                    )
                    lib_ids = [r[0] for r in name_rows]
                    if not lib_ids:
                        continue
                else:
                    # Legacy callers — pass IDs as-is. Subject to the
                    # cross-library ID-collision bug; new clients
                    # should send author_names.
                    lib_ids = author_ids
                deleted = await _clear_authors_in_library(
                    db, lib_ids, clear_source, clear_mam,
                )
                await db.commit()
                if clear_source and deleted > 0:
                    cleaned = await cleanup_empty_series(db)
                    if cleaned:
                        logger.info(
                            f"  [{slug}] empty series cleanup: removed {cleaned} orphaned series"
                        )
                total_deleted += deleted
                libs_touched += 1
            finally:
                await db.close()

    n_authors = len(author_names) if author_names else len(author_ids)
    logger.info(
        f"Cleared scan data for {n_authors} authors across {libs_touched} "
        f"libraries (content_type={content_type or 'active'}, "
        f"source={clear_source}, mam={clear_mam}), {total_deleted} books deleted"
    )
    return {"status": "ok", "authors_cleared": n_authors,
            "books_deleted": total_deleted, "libraries_touched": libs_touched}


async def _resolve_names_for_ids(
    author_ids: list[int],
) -> list[str]:
    """Look up author display names for the given IDs, trying every
    library until they resolve. Used when cross-library scanning needs
    to find the same-named author in libraries other than the active
    one.
    """
    needed = set(author_ids)
    names: dict[int, str] = {}
    # Active first (fast path for the common single-library case).
    db = await get_db()
    try:
        placeholders = ",".join(["?" for _ in needed])
        rows = await db.execute_fetchall(
            f"SELECT id, name FROM authors WHERE id IN ({placeholders})",
            list(needed),
        )
        for r in rows:
            names[r[0]] = r[1]
    finally:
        await db.close()
    # Any unresolved IDs? Fan out to other libraries. This covers the
    # cross-library selection case where a row came from library X but
    # the active lib is Y — row.id is X-scoped and won't be in Y's DB.
    unresolved = needed - set(names.keys())
    if unresolved:
        for lib in state._discovered_libraries:
            if not unresolved:
                break
            slug = lib.get("slug")
            if not slug or slug == get_active_library():
                continue
            try:
                db2 = await get_db(slug)
            except Exception:
                continue
            try:
                ph = ",".join(["?" for _ in unresolved])
                rows = await db2.execute_fetchall(
                    f"SELECT id, name FROM authors WHERE id IN ({ph})",
                    list(unresolved),
                )
                for r in rows:
                    if r[0] in unresolved:
                        names[r[0]] = r[1]
                        unresolved.discard(r[0])
            finally:
                await db2.close()
    # Deduplicate names so a user picking the same author in two
    # libraries doesn't scan them twice per-library.
    seen: set[str] = set()
    out: list[str] = []
    for aid in author_ids:
        n = names.get(aid)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


@router.post("/authors/scan-sources")
async def scan_authors_sources(data: dict = Body(...)):
    """Run a source-plugin lookup for each of the given authors.

    Used by the Authors page bulk-select bar. Loops sequentially because
    lookup_author is rate-limited per-source and parallelizing would just
    queue up against the existing semaphores.

    `content_type`: optional "ebook" / "audiobook". When set, iterate
    libraries of that type and scan each selected author by NAME in
    every matching library. Omitted → legacy active-library-only path.

    `author_names`: optional list of pre-resolved author display names.
    When provided alongside `content_type`, the backend uses those names
    directly and skips the ID→name resolver. This is the safe path for
    cross-library selections: the merged Authors view returns each
    author with an `id` from whichever library was first encountered,
    so an audiobook-only author (e.g. Touko Amekawa) ends up with the
    audiobook library's ID. If that ID happens to collide with a
    different author's ID in the active ebook library (Roger Black's
    ebook id was 17, Touko's audiobook id was 17), the resolver's
    active-library lookup picks up the WRONG name and the wrong author
    gets scanned. Sending names sidesteps the collision entirely. The
    frontend reads names from its merged Authors response and POSTs
    them; the resolver fallback path remains for older callers that
    only have IDs.
    """
    author_ids = data.get("author_ids", [])
    author_names = data.get("author_names")
    content_type = data.get("content_type")
    if not author_ids and not author_names:
        return {"error": "No authors specified"}

    if content_type is None:
        # Active-library scan — legacy path.
        db = await get_db()
        try:
            placeholders = ",".join(["?" for _ in author_ids])
            # ORDER BY sort_name so the scan progresses alphabetically
            # by last name (e.g. "Anderson, J" → "Brown, K"), matching
            # what the user sees in the Authors page list. Without the
            # ORDER BY, SQLite returns rows in physical (rowid) order,
            # which is insertion order from initial Calibre sync — not
            # at all what users expect when they multi-select an A-letter
            # batch and watch the dashboard scan progress.
            rows = await db.execute_fetchall(
                f"SELECT id, name FROM authors WHERE id IN ({placeholders}) "
                f"ORDER BY sort_name",
                author_ids,
            )
        finally:
            await db.close()
        if not rows:
            raise HTTPException(404, "No matching authors found")

        async def _runner():
            nonlocal_state = {"scanned": 0, "errors": 0, "new": 0}
            for row in rows:
                aid, name = row[0], row[1]
                state._lookup_progress.update({"current_author": name})
                def _on_source(running, _baseline=nonlocal_state["new"]):
                    state._lookup_progress["new_books"] = _baseline + int(running)
                try:
                    new_books = await lookup_author(aid, name, on_progress=_on_source)
                    nonlocal_state["new"] += int(new_books or 0)
                    nonlocal_state["scanned"] += 1
                    if new_books:
                        try:
                            from app.discovery.notify import notify_new_books
                            await notify_new_books(name, int(new_books))
                        except Exception:
                            logger.debug("per-author notify failed", exc_info=True)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Bulk source scan error for author {aid} ({name}): {e}")
                    nonlocal_state["errors"] += 1
                state._lookup_progress.update({
                    "checked": nonlocal_state["scanned"] + nonlocal_state["errors"],
                    "new_books": nonlocal_state["new"],
                })

        _spawn_lookup_task("bulk_authors", total=len(rows), runner=_runner)
        return {"status": "started", "total": len(rows)}

    # Cross-library scan by content_type — iterate matching libraries
    # and scan each selected author by NAME in each. Prefer
    # caller-supplied names (the safe path against ID collisions —
    # see the docstring); fall back to the legacy ID resolver for
    # older callers / direct API users.
    target_libs = libraries_for(content_type)
    if not target_libs:
        return {"status": "ok", "total": 0,
                "message": f"No {content_type} libraries found."}
    if author_names:
        # Preserve caller order while deduping.
        seen: set[str] = set()
        names = []
        for n in author_names:
            if n and n not in seen:
                seen.add(n)
                names.append(n)
    else:
        names = await _resolve_names_for_ids(author_ids)
    if not names:
        raise HTTPException(404, "No matching authors found")

    # Pre-resolve per-library matches so the dashboard's progress
    # total reflects ACTUAL author scans, not the optimistic
    # names×libraries product. A name in `names` that isn't an
    # author in a given library (e.g. an audiobook-only author
    # appearing in an ebook-content_type scan) is filtered here
    # — the scan loop later sees only matched rows and the user-
    # facing count matches what'll actually run. Same SQL the
    # loop used to do per-iteration; we just hoist it.
    pre_resolved: list[tuple[dict, list]] = []
    ph = ",".join(["?" for _ in names])
    for lib in target_libs:
        slug = lib.get("slug")
        if not slug:
            continue
        try:
            ldb = await get_db(slug)
        except Exception as e:
            logger.warning(f"scan-sources: cannot open lib {slug}: {e}")
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

    total_tasks = sum(len(rows) for _, rows in pre_resolved)
    if total_tasks == 0:
        return {"status": "ok", "total": 0,
                "requested": len(names),
                "message": "No matching authors in target libraries."}

    async def _runner():
        from app.discovery.database import set_active_library
        original_active = get_active_library()
        nonlocal_state = {"scanned": 0, "errors": 0, "new": 0}
        try:
            for lib, lib_rows in pre_resolved:
                slug = lib.get("slug")
                if not slug:
                    continue
                if slug != get_active_library():
                    set_active_library(slug)
                for row in lib_rows:
                    aid, name = row[0], row[1]
                    state._lookup_progress.update({"current_author": name})
                    def _on_source(running, _baseline=nonlocal_state["new"]):
                        state._lookup_progress["new_books"] = _baseline + int(running)
                    try:
                        new_books = await lookup_author(aid, name, on_progress=_on_source)
                        nonlocal_state["new"] += int(new_books or 0)
                        nonlocal_state["scanned"] += 1
                        if new_books:
                            try:
                                from app.discovery.notify import notify_new_books
                                await notify_new_books(name, int(new_books))
                            except Exception:
                                logger.debug("per-author notify failed", exc_info=True)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(
                            f"Bulk source scan error for {name} in {slug}: {e}"
                        )
                        nonlocal_state["errors"] += 1
                    state._lookup_progress.update({
                        "checked": nonlocal_state["scanned"] + nonlocal_state["errors"],
                        "new_books": nonlocal_state["new"],
                    })
        finally:
            if original_active and original_active != get_active_library():
                set_active_library(original_active)

    _spawn_lookup_task("bulk_authors", total=total_tasks, runner=_runner)
    return {"status": "started", "total": total_tasks,
            "requested": len(names),
            "libraries": len(target_libs), "authors": len(names)}


async def _skip_mam_for_author_ids_in_library(
    db, author_ids: list[int],
) -> int:
    """UPDATE every book under the given author_ids to mam_status=
    'not_applicable', clearing the URL/torrent_id/formats so a stale
    prior match doesn't linger on a row the user just declared
    irrelevant. Returns affected row count. Caller owns commit."""
    placeholders = ",".join(["?" for _ in author_ids])
    cur = await db.execute(
        f"UPDATE books SET mam_url=NULL, mam_status='not_applicable', "
        f"mam_formats=NULL, mam_torrent_id=NULL, mam_has_multiple=0, "
        f"mam_my_snatched=0, mam_is_bundle=0 WHERE author_id IN ({placeholders})",
        author_ids,
    )
    return cur.rowcount or 0


@router.post("/authors/skip-mam")
async def skip_authors_mam(data: dict = Body(...)):
    """Bulk Skip MAM — mark every book belonging to the given authors
    as `mam_status='not_applicable'` so the rescan loop never visits
    them again. Driven by the Authors page multi-select dropdown.

    Same cross-library + author_names contract as
    `/authors/clear-scan-data` (see that handler for the
    cross-library ID-collision rationale). content_type=None reads
    the active library; 'ebook'/'audiobook'/'all' iterate libraries
    of that type, resolving author names per-library to sidestep ID
    collisions.

    Snekguy-style use case: an author whose works are free on the
    public web and almost never end up on MAM. Pre-skip wipes them
    from the rescan queue so the v2.3.6 widened predicate doesn't
    keep retrying never-findable rows every tick.
    """
    author_ids = data.get("author_ids", [])
    author_names = data.get("author_names")
    content_type = data.get("content_type")
    if not author_ids and not author_names:
        return {"error": "No authors specified"}

    if content_type is None:
        target_libs = None
    else:
        target_libs = libraries_for(content_type)
        if not target_libs:
            return {"status": "ok", "authors_skipped": 0,
                    "books_skipped": 0, "libraries_touched": 0,
                    "message": f"No {content_type} libraries found."}

    total_skipped = 0
    libs_touched = 0
    if target_libs is None:
        db = await get_db()
        try:
            ids = author_ids
            if not ids and author_names:
                ph = ",".join(["?" for _ in author_names])
                name_rows = await db.execute_fetchall(
                    f"SELECT id FROM authors WHERE name IN ({ph})",
                    list(author_names),
                )
                ids = [r[0] for r in name_rows]
            if ids:
                total_skipped += await _skip_mam_for_author_ids_in_library(db, ids)
                await db.commit()
                libs_touched = 1
        finally:
            await db.close()
    else:
        for lib in target_libs:
            slug = lib.get("slug")
            if not slug:
                continue
            db = await get_db(slug)
            try:
                if author_names:
                    ph = ",".join(["?" for _ in author_names])
                    name_rows = await db.execute_fetchall(
                        f"SELECT id FROM authors WHERE name IN ({ph})",
                        list(author_names),
                    )
                    lib_ids = [r[0] for r in name_rows]
                    if not lib_ids:
                        continue
                else:
                    lib_ids = author_ids
                deleted = await _skip_mam_for_author_ids_in_library(db, lib_ids)
                await db.commit()
                total_skipped += deleted
                libs_touched += 1
            finally:
                await db.close()

    n_authors = len(author_names) if author_names else len(author_ids)
    logger.info(
        f"Skip MAM: marked {total_skipped} books not_applicable across "
        f"{libs_touched} libraries for {n_authors} authors "
        f"(content_type={content_type or 'active'})"
    )
    return {
        "status": "ok",
        "authors_skipped": n_authors,
        "books_skipped": total_skipped,
        "libraries_touched": libs_touched,
    }


@router.post("/authors/scan-mam")
async def scan_authors_mam(data: dict = Body(...)):
    """Run a MAM scan for every un-scanned book belonging to the given authors.

    v2.3.7 — multi-library aware. Accepts `content_type` (None | "ebook"
    | "audiobook" | "all") and `author_names` matching the
    `/authors/clear-scan-data` + `/authors/scan-sources` contract.
    Iterates each matching library, resolves author_names locally to
    sidestep cross-library ID collisions (Roger Black id=17 in Calibre
    vs Touko Amekawa id=17 in ABS), and routes per-library MAM
    category (ebook/audiobook) by the lib's own content_type.

    Pre-v2.3.7 this only touched the active library, which silently
    left the unselected library's matching rows unscanned — Mark's
    Snekguy bulk-skip use case exposed this when a "Scan MAM" on a
    cross-library author list missed half the books.

    Runs as a background task with progress tracked in
    state._mam_scan_progress so the Dashboard scan widget shows
    progress in real time.
    """
    from app.discovery.sources.mam import (
        _NEEDS_SCAN_BASIC_ALIASED,
        check_book as mam_check_book,
        _resolve_mam_languages,
    )
    from app.discovery.cross_library import libraries_for
    from app import state

    author_ids = data.get("author_ids", [])
    author_names = data.get("author_names")
    content_type = data.get("content_type")
    if not author_ids and not author_names:
        return {"error": "No authors specified"}

    s = load_settings()
    from app.discovery.routers.mam import _mam_ready, _get_mam_token
    if not await _mam_ready(s):
        return {"error": "MAM not configured or not enabled"}
    if not s.get("mam_scanning_enabled", True):
        return {"error": "MAM scanning is disabled — enable it in Settings"}
    if state._mam_scan_progress.get("running"):
        return {"error": "A MAM scan is already running"}

    # Build target library list. content_type=None preserves the
    # pre-v2.3.7 active-library-only behavior for callers that haven't
    # been updated; otherwise iterate per content_type.
    if content_type is None:
        target_libs = None
    else:
        target_libs = libraries_for(content_type)
        if not target_libs:
            return {
                "status": "complete",
                "message": f"No {content_type} libraries found.",
                "scanned": 0, "found": 0, "possible": 0, "not_found": 0,
            }

    delay = s.get("rate_mam", 2)
    token = await _get_mam_token()
    lang_ids = _resolve_mam_languages(s.get("languages", ["English"]))
    ebook_fp = s.get("mam_format_priority")
    audio_fp = s.get("audiobook_format_priority")

    # Per-library snapshot of scannable books — taken upfront so a
    # concurrent author scan adding new books mid-run doesn't inflate
    # this scan's queue (matches the manual /scan endpoint's pattern).
    per_lib: list[tuple[dict, list[tuple[int, str, str]]]] = []
    libs_iter = target_libs if target_libs is not None else [None]
    for lib in libs_iter:
        slug = lib.get("slug") if lib else None
        try:
            lib_db = await get_db(slug) if slug else await get_db()
        except Exception as e:
            logger.warning(f"authors/scan-mam: cannot open lib {slug}: {e}")
            continue
        try:
            # Resolve author identity per-library. With author_names we
            # look up local IDs to dodge the cross-library collision
            # class (see /authors/scan-sources). Without names, fall
            # back to the supplied IDs (legacy single-library callers).
            if author_names:
                ph = ",".join(["?" for _ in author_names])
                name_rows = await lib_db.execute_fetchall(
                    f"SELECT id FROM authors WHERE name IN ({ph})",
                    list(author_names),
                )
                lib_aids = [r[0] for r in name_rows]
            else:
                lib_aids = author_ids
            if not lib_aids:
                continue
            placeholders = ",".join(["?" for _ in lib_aids])
            rows = await lib_db.execute_fetchall(
                f"SELECT b.id, b.title, a.name FROM books b "
                f"JOIN authors a ON b.author_id=a.id "
                f"WHERE b.author_id IN ({placeholders}) "
                f"AND {_NEEDS_SCAN_BASIC_ALIASED} "
                f"ORDER BY a.sort_name, b.title",
                lib_aids,
            )
            books = [(r[0], r[1], r[2]) for r in rows]
            if books:
                per_lib.append((lib or {"slug": None, "content_type": "ebook"}, books))
        finally:
            await lib_db.close()

    total = sum(len(books) for _, books in per_lib)
    if total == 0:
        return {
            "status": "complete",
            "message": "No un-scanned books for these authors",
            "scanned": 0, "found": 0, "possible": 0, "not_found": 0,
        }

    state._mam_scan_progress.update({
        "running": True, "scanned": 0, "total": total,
        "found": 0, "possible": 0, "not_found": 0, "errors": 0,
        "status": "scanning", "type": "multi_author",
        "current_book": "", "current_library": "",
    })

    async def _do():
        try:
            for lib, books in per_lib:
                if not state._mam_scan_progress.get("running"):
                    state._mam_scan_progress.update({"status": "cancelled"})
                    break
                slug = lib.get("slug")
                lib_name = lib.get("display_name") or lib.get("name") or slug or "active"
                ct = lib.get("content_type") or "ebook"
                format_priority = audio_fp if ct == "audiobook" else ebook_fp
                state._mam_scan_progress["current_library"] = lib_name
                try:
                    db2 = await get_db(slug) if slug else await get_db()
                except Exception as e:
                    logger.error(f"authors/scan-mam: cannot open lib {slug} for write: {e}")
                    state._mam_scan_progress["errors"] = (
                        state._mam_scan_progress.get("errors", 0) + len(books)
                    )
                    continue
                try:
                    from app.discovery.cover_phash import ensure_cover_phash
                    for bid, btitle, aname in books:
                        if not state._mam_scan_progress.get("running"):
                            state._mam_scan_progress.update({"status": "cancelled"})
                            break
                        state._mam_scan_progress["current_book"] = btitle[:60]
                        try:
                            seshat_phash = await ensure_cover_phash(db2, bid, token=token)
                            check = await mam_check_book(
                                token, btitle, aname, format_priority,
                                delay, lang_ids=lang_ids, content_type=ct,
                                seshat_cover_phash=seshat_phash,
                            )
                        except Exception as e:
                            logger.error(
                                f"Bulk author MAM scan error on book {bid} "
                                f"({btitle[:40]}) in {lib_name}: {e}"
                            )
                            state._mam_scan_progress["errors"] = (
                                state._mam_scan_progress.get("errors", 0) + 1
                            )
                            continue
                        await db2.execute(
                            """
                            UPDATE books SET mam_url=?, mam_status=?, mam_formats=?,
                                   mam_torrent_id=?, mam_has_multiple=?, mam_my_snatched=?,
                                   mam_is_bundle=?
                            WHERE id=?
                            """,
                            (
                                check["mam_url"], check["status"], check["mam_formats"],
                                check["mam_torrent_id"],
                                1 if check["mam_has_multiple"] else 0,
                                1 if check.get("mam_my_snatched") else 0,
                                1 if check.get("mam_is_bundle") else 0,
                                bid,
                            ),
                        )
                        state._mam_scan_progress["scanned"] = (
                            state._mam_scan_progress.get("scanned", 0) + 1
                        )
                        st = check["status"]
                        if st in ("found", "possible", "not_found"):
                            state._mam_scan_progress[st] = (
                                state._mam_scan_progress.get(st, 0) + 1
                            )
                    await db2.commit()
                finally:
                    await db2.close()
            state._mam_scan_progress.update({
                "running": False, "status": "complete",
                "current_book": "", "current_library": "",
            })
        except Exception as e:
            logger.error(f"Bulk author MAM scan error: {e}", exc_info=True)
            state._mam_scan_progress.update({
                "running": False, "status": f"error: {e}",
                "current_book": "", "current_library": "",
            })

    state._mam_scan_task = asyncio.create_task(_do())
    return {
        "status": "started",
        "total": total,
        "libraries": [lib.get("slug") for lib, _ in per_lib if lib.get("slug")],
    }


@router.post("/sources/reset")
async def reset_all_source_scan_data():
    """Reset all source scan data across the entire library.

    Deletes every non-Calibre, non-owned book (i.e. books discovered by source
    scans), clears source_url on owned/Calibre books, and resets last_lookup_at
    on every author so future scans treat all authors as never-scanned.
    MAM data is left untouched.
    """
    db = await get_db()
    try:
        # Count discovered books that will be deleted
        count_row = await db.execute_fetchall(
            "SELECT COUNT(*) FROM books WHERE owned=0 AND calibre_id IS NULL"
        )
        affected = count_row[0][0] if count_row else 0
        # Delete all non-owned discovered books
        await db.execute("DELETE FROM books WHERE owned=0 AND calibre_id IS NULL")
        # Clear source URLs on owned books
        await db.execute("UPDATE books SET source_url=NULL WHERE owned=1")
        # Reset every author's last_lookup_at so the next scheduled scan picks them all up
        await db.execute("UPDATE authors SET last_lookup_at=NULL")
        await db.commit()
        cleaned = await cleanup_empty_series(db)
        if cleaned:
            logger.info(f"  Empty series cleanup: removed {cleaned} orphaned series")
        logger.info(f"Reset all source scan data: {affected} discovered books deleted")
        return {"status": "ok", "books_deleted": affected, "series_cleaned": cleaned}
    finally:
        await db.close()


# ─── Pen-Name Linking ───────────────────────────────────────

VALID_LINK_TYPES = {"pen_name", "co_author"}


@router.get("/authors/{aid}/pen-names")
async def get_pen_name_links(aid: int):
    """Get all author-link rows for an author (both directions).

    Endpoint is named pen-names for backward compat; the rows now carry
    a `link_type` discriminator (`pen_name` | `co_author`). The backend
    treats both identically — they only differ in the UI label.
    """
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT p.id, p.canonical_author_id, p.alias_author_id, p.link_type, "
            "a1.name as canonical_name, a2.name as alias_name "
            "FROM pen_name_links p "
            "JOIN authors a1 ON p.canonical_author_id = a1.id "
            "JOIN authors a2 ON p.alias_author_id = a2.id "
            "WHERE p.canonical_author_id = ? OR p.alias_author_id = ?",
            (aid, aid),
        )).fetchall()
        return {"links": [dict(r) for r in rows]}
    finally:
        await db.close()


@router.post("/authors/link-pen-names")
async def link_pen_names(data: dict = Body(...)):
    """Link two authors so source scans treat them as one identity.

    The canonical_author_id is the 'primary' identity; alias_author_id
    is the linked author. Source scans for either one check owned books
    under BOTH for dedup and series matching. The `link_type` field
    (default `pen_name`) only controls the UI label — backend dedup
    behavior is identical for both link types.
    """
    canonical_id = data.get("canonical_author_id")
    alias_id = data.get("alias_author_id")
    link_type = (data.get("link_type") or "pen_name").lower()
    if link_type not in VALID_LINK_TYPES:
        raise HTTPException(400, f"link_type must be one of {sorted(VALID_LINK_TYPES)}")
    if not canonical_id or not alias_id:
        raise HTTPException(400, "Both canonical_author_id and alias_author_id required")
    if canonical_id == alias_id:
        raise HTTPException(400, "Cannot link an author to themselves")
    db = await get_db()
    try:
        # Verify both authors exist
        for aid in (canonical_id, alias_id):
            row = await (await db.execute("SELECT id FROM authors WHERE id=?", (aid,))).fetchone()
            if not row:
                raise HTTPException(404, f"Author {aid} not found")
        # Check for existing link (either direction). If found, update
        # the link_type to the new value rather than creating a duplicate
        # — lets the user reclassify a pen-name link as co-author.
        existing = await (await db.execute(
            "SELECT id, link_type FROM pen_name_links WHERE "
            "(canonical_author_id=? AND alias_author_id=?) OR "
            "(canonical_author_id=? AND alias_author_id=?)",
            (canonical_id, alias_id, alias_id, canonical_id),
        )).fetchone()
        if existing:
            if existing["link_type"] != link_type:
                await db.execute(
                    "UPDATE pen_name_links SET link_type=? WHERE id=?",
                    (link_type, existing["id"]),
                )
                await db.commit()
                logger.info(
                    f"Reclassified author link {existing['id']}: "
                    f"{existing['link_type']} → {link_type}"
                )
                return {"status": "updated", "link_id": existing["id"], "link_type": link_type}
            return {"status": "already_linked", "link_id": existing["id"], "link_type": link_type}
        cur = await db.execute(
            "INSERT INTO pen_name_links (canonical_author_id, alias_author_id, link_type) "
            "VALUES (?, ?, ?)",
            (canonical_id, alias_id, link_type),
        )
        await db.commit()
        logger.info(
            f"Linked authors as {link_type}: {canonical_id} ↔ {alias_id}"
        )
        return {"status": "ok", "link_id": cur.lastrowid, "link_type": link_type}
    finally:
        await db.close()


@router.delete("/authors/pen-name-link/{link_id}")
async def unlink_pen_names(link_id: int):
    """Remove a pen-name link."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM pen_name_links WHERE id=?", (link_id,))
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()
