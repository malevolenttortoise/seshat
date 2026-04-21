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
        a["series"] = [dict(s) for s in await (await db.execute(
            f"""SELECT s.*,
                COUNT(DISTINCT CASE WHEN {HF} AND COALESCE(b.is_omnibus,0)=0 THEN b.id END) as book_count,
                COUNT(DISTINCT CASE WHEN b.author_id=? AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN b.id END) as author_book_count,
                SUM(CASE WHEN b.owned=1 AND b.author_id=? AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN 1 ELSE 0 END) as owned_count,
                SUM(CASE WHEN b.owned=0 AND b.author_id=? AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN 1 ELSE 0 END) as missing_count,
                CASE WHEN COUNT(DISTINCT b.author_id) > 1 THEN 1 ELSE 0 END as multi_author
            FROM series s
            JOIN books b ON s.id=b.series_id
            WHERE s.id IN (SELECT DISTINCT series_id FROM books WHERE author_id=? AND series_id IS NOT NULL)
            GROUP BY s.id ORDER BY s.name""",
            (aid, aid, aid, aid)
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
    (and listens for the `athenascout:scan-started` window event)
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
        except asyncio.CancelledError:
            # User clicked Stop on the Dashboard widget. Mark cancelled
            # and let the exception propagate so any further `await` in
            # the runner unwinds cleanly.
            state._lookup_progress.update({"running": False, "status": "cancelled"})
            raise
        except Exception as e:
            logger.error(f"Author scan task error: {e}", exc_info=True)
            state._lookup_progress.update({"running": False, "status": f"error: {e}"})

    state._lookup_task = asyncio.create_task(_do())


@router.post("/authors/{aid}/lookup")
async def trigger_author_lookup(aid: int):
    s = load_settings()
    if not s.get("author_scanning_enabled", True):
        raise HTTPException(400, "Author scanning is disabled — enable it in Settings")
    db = await get_db()
    try:
        r = await (await db.execute("SELECT * FROM authors WHERE id=?", (aid,))).fetchone()
        if not r:
            raise HTTPException(404)
    finally:
        await db.close()
    name = dict(r)["name"]

    async def _runner():
        state._lookup_progress.update({"current_author": name})
        # Surface running new_books count after each source so the
        # widget climbs in real time instead of jumping 0 → final.
        def _on_source(running):
            state._lookup_progress["new_books"] = int(running)
        new_books = await lookup_author(aid, name, on_progress=_on_source)
        state._lookup_progress.update({
            "checked": 1, "new_books": int(new_books or 0),
        })

    _spawn_lookup_task("single_author", total=1, runner=_runner)
    return {"status": "started", "author": name}


@router.post("/authors/{aid}/full-rescan")
async def trigger_author_full_rescan(aid: int):
    """Full re-scan for a single author."""
    s = load_settings()
    if not s.get("author_scanning_enabled", True):
        raise HTTPException(400, "Author scanning is disabled — enable it in Settings")
    db = await get_db()
    try:
        r = await (await db.execute("SELECT * FROM authors WHERE id=?", (aid,))).fetchone()
        if not r:
            raise HTTPException(404)
    finally:
        await db.close()
    name = dict(r)["name"]

    async def _runner():
        state._lookup_progress.update({"current_author": name})
        def _on_source(running):
            state._lookup_progress["new_books"] = int(running)
        new_books = await lookup_author(aid, name, full_scan=True, on_progress=_on_source)
        state._lookup_progress.update({
            "checked": 1, "new_books": int(new_books or 0),
        })

    _spawn_lookup_task("single_author_full", total=1, runner=_runner)
    return {"status": "started", "author": name}


@router.post("/authors/clear-scan-data")
async def clear_author_scan_data(data: dict = Body(...)):
    """Clear source and/or MAM scan data for specified authors."""
    author_ids = data.get("author_ids", [])
    clear_source = data.get("clear_source", False)
    clear_mam = data.get("clear_mam", False)
    if not author_ids:
        return {"error": "No authors specified"}
    if not clear_source and not clear_mam:
        return {"error": "Nothing to clear — specify clear_source and/or clear_mam"}
    db = await get_db()
    try:
        placeholders = ",".join(["?" for _ in author_ids])
        affected = 0
        if clear_source:
            # Count books that will be deleted
            count_row = await db.execute_fetchall(
                f"SELECT COUNT(*) FROM books WHERE author_id IN ({placeholders}) AND owned=0 AND calibre_id IS NULL",
                author_ids
            )
            affected = count_row[0][0] if count_row else 0
            # Delete non-owned books (discovered by source scans) for these authors
            await db.execute(
                f"DELETE FROM books WHERE author_id IN ({placeholders}) AND owned=0 AND calibre_id IS NULL",
                author_ids
            )
            # Reset source URLs on owned books (keep source='calibre' intact)
            await db.execute(
                f"UPDATE books SET source_url=NULL WHERE author_id IN ({placeholders}) AND owned=1",
                author_ids
            )
            await db.execute(
                f"UPDATE authors SET last_lookup_at=NULL WHERE id IN ({placeholders})",
                author_ids
            )
        if clear_mam:
            await db.execute(
                f"UPDATE books SET mam_url=NULL, mam_status=NULL, mam_formats=NULL, mam_torrent_id=NULL, mam_has_multiple=0, mam_my_snatched=0 WHERE author_id IN ({placeholders})",
                author_ids
            )
        await db.commit()
        if clear_source and affected > 0:
            cleaned = await cleanup_empty_series(db)
            if cleaned:
                logger.info(f"  Empty series cleanup: removed {cleaned} orphaned series")
        logger.info(f"Cleared scan data for {len(author_ids)} authors (source={clear_source}, mam={clear_mam}), {affected} books deleted")
        return {"status": "ok", "authors_cleared": len(author_ids), "books_deleted": affected}
    finally:
        await db.close()


@router.post("/authors/scan-sources")
async def scan_authors_sources(data: dict = Body(...)):
    """Run a source-plugin lookup for each of the given authors.

    Used by the Authors page bulk-select bar. Loops sequentially because
    lookup_author is rate-limited per-source and parallelizing would just
    queue up against the existing semaphores.
    """
    author_ids = data.get("author_ids", [])
    if not author_ids:
        return {"error": "No authors specified"}

    db = await get_db()
    try:
        placeholders = ",".join(["?" for _ in author_ids])
        rows = await db.execute_fetchall(
            f"SELECT id, name FROM authors WHERE id IN ({placeholders})",
            author_ids,
        )
    finally:
        await db.close()
    if not rows:
        raise HTTPException(404, "No matching authors found")

    # Runs as a background task tracked by state._lookup_task so the
    # Dashboard Stop button can cancel mid-stream. The endpoint
    # returns immediately after spawning; the frontend polls
    # /api/scan-status and listens for athenascout:scan-started to
    # refresh the widget without polling lag.
    async def _runner():
        nonlocal_state = {"scanned": 0, "errors": 0, "new": 0}
        for row in rows:
            aid, name = row[0], row[1]
            state._lookup_progress.update({"current_author": name})
            # Capture the cumulative-so-far baseline at closure-creation
            # time. The default-arg trick freezes the value per author —
            # without it, every closure would share the live `nonlocal_state["new"]`
            # and the running widget count would be wrong.
            def _on_source(running, _baseline=nonlocal_state["new"]):
                state._lookup_progress["new_books"] = _baseline + int(running)
            try:
                new_books = await lookup_author(aid, name, on_progress=_on_source)
                nonlocal_state["new"] += int(new_books or 0)
                nonlocal_state["scanned"] += 1
                # Per-author granular ping. Gated by ntfy_on_new_books
                # so users who only want the bulk-summary can suppress
                # these without losing the final aggregate notification.
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


@router.post("/authors/scan-mam")
async def scan_authors_mam(data: dict = Body(...)):
    """Run a MAM scan for every un-scanned book belonging to the given authors.

    Runs as a background task with progress tracked in state._mam_scan_progress
    so the Dashboard scan widget shows progress in real time.
    """
    from app.discovery.sources.mam import check_book as mam_check_book, _resolve_mam_languages
    from app import state

    author_ids = data.get("author_ids", [])
    if not author_ids:
        return {"error": "No authors specified"}

    s = load_settings()
    if not s.get("mam_enabled") or not s.get("mam_session_id"):
        return {"error": "MAM not configured or not enabled"}
    if not s.get("mam_scanning_enabled", True):
        return {"error": "MAM scanning is disabled — enable it in Settings"}
    if state._mam_scan_progress.get("running"):
        return {"error": "A MAM scan is already running"}

    db = await get_db()
    try:
        placeholders = ",".join(["?" for _ in author_ids])
        book_rows = await db.execute_fetchall(
            f"SELECT b.id, b.title, a.name FROM books b JOIN authors a ON b.author_id=a.id "
            f"WHERE b.author_id IN ({placeholders}) AND b.mam_status IS NULL "
            f"AND b.is_unreleased=0 AND b.hidden=0 ORDER BY a.sort_name, b.title",
            author_ids,
        )
    finally:
        await db.close()

    if not book_rows:
        return {"status": "complete", "message": "No un-scanned books for these authors",
                "scanned": 0, "found": 0, "possible": 0, "not_found": 0}

    total = len(book_rows)
    delay = s.get("rate_mam", 2)
    format_priority = s.get("mam_format_priority")
    token = s["mam_session_id"]
    lang_ids = _resolve_mam_languages(s.get("languages", ["English"]))

    # Track progress via state so Dashboard widget renders
    state._mam_scan_progress.update({
        "running": True, "scanned": 0, "total": total,
        "found": 0, "possible": 0, "not_found": 0, "errors": 0,
        "status": "scanning", "type": "multi_author",
        "current_book": "",
    })

    async def _do():
        db2 = await get_db()
        try:
            for row in book_rows:
                if not state._mam_scan_progress.get("running"):
                    state._mam_scan_progress.update({"status": "cancelled"})
                    break
                bid, btitle, aname = row[0], row[1], row[2]
                state._mam_scan_progress["current_book"] = btitle[:60]
                try:
                    check = await mam_check_book(token, btitle, aname, format_priority, delay, lang_ids=lang_ids)
                except Exception as e:
                    logger.error(f"Bulk author MAM scan error on book {bid} ({btitle[:40]}): {e}")
                    state._mam_scan_progress["errors"] = state._mam_scan_progress.get("errors", 0) + 1
                    continue
                await db2.execute("""
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
                state._mam_scan_progress["scanned"] = state._mam_scan_progress.get("scanned", 0) + 1
                if check["status"] == "found":
                    state._mam_scan_progress["found"] = state._mam_scan_progress.get("found", 0) + 1
                elif check["status"] == "possible":
                    state._mam_scan_progress["possible"] = state._mam_scan_progress.get("possible", 0) + 1
                elif check["status"] == "not_found":
                    state._mam_scan_progress["not_found"] = state._mam_scan_progress.get("not_found", 0) + 1
            await db2.commit()
            state._mam_scan_progress.update({"running": False, "status": "complete", "current_book": ""})
        except Exception as e:
            logger.error(f"Bulk author MAM scan error: {e}")
            state._mam_scan_progress.update({"running": False, "status": f"error: {e}", "current_book": ""})
        finally:
            await db2.close()

    state._mam_scan_task = asyncio.create_task(_do())
    return {"status": "started", "total": total}


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
