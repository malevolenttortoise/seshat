"""
MyAnonamouse integration endpoints.

This router orchestrates every MAM scan path the UI exposes:

  - Manual batched scan (/api/mam/scan)            — chips through every
    book missing MAM data, in 150-book batches with a 1-minute pause
    between each. Snapshots eligible IDs at start so a concurrent
    author scan adding new books does NOT grow this scan's queue.
  - Single-book rescan (/api/mam/scan-book/{id})   — used by the
    "Re-scan MAM" button in BookSidebar to refresh a stale match.
  - Single-author scan (/api/mam/scan-author/{id}) — runs as a
    background task tracked through the unified scan widget so the
    user can Stop it mid-run.
  - Full library scan (/api/mam/full-scan)         — long-running,
    400 books per batch with a 5-minute pause. State lives in the
    `mam_scan_log` table so it survives process restarts.

All scan paths share `_mam_scan_progress` in app.state and serialize
through the same "one MAM scan at a time" lock so the unified widget
always reflects exactly one in-flight MAM job.

Other endpoints in this router: /validate (test session), /status
(stats + scan health), /books (paged book list for the MAM page),
/toggle (feature flag), /reset (wipe all mam_* fields).
"""
import asyncio
import logging
import time
from fastapi import APIRouter, HTTPException, Query

from app.config import load_settings, save_settings
from app.discovery.database import get_db, attach_contributors
from app.discovery.sources.mam import (
    _NEEDS_SCAN_BASIC_BARE,
    validate_connection as mam_validate,
    scan_books_batch as mam_scan_batch,
    start_full_scan as mam_start_full_scan,
    run_full_scan_batch as mam_run_full_scan_batch,
    cancel_full_scan as mam_cancel_full_scan,
    get_full_scan_status as mam_get_full_scan_status,
    get_mam_stats,
    check_book as mam_check_book,
    _resolve_mam_languages,
    _recent_scan_cutoff_seconds,
    _recent_scan_skip_clause,
    _recent_scan_order_clause,
)
from app import state


def _active_content_type(slug: str | None = None) -> str:
    """Return the content_type of the target library, for MAM scan
    routing. Falls back to the active library when no slug is given,
    and to "ebook" when no match is found in _discovered_libraries.
    Threading this through every scan entry point ensures audiobook
    libraries search MAM's audiobook category instead of being
    silently filtered out by the ebook main_cat default.
    """
    from app.discovery.database import get_active_library as _active
    target = slug or _active()
    for lib in state._discovered_libraries:
        if lib.get("slug") == target:
            return lib.get("content_type", "ebook")
    return "ebook"

logger = logging.getLogger("seshat.discovery")

router = APIRouter(prefix="/api/discovery/mam", tags=["mam"])


async def _notify_mam_done() -> None:
    """Fire the MAM-scan-complete notification using the current
    `_mam_scan_progress` snapshot. Best-effort — logs and swallows
    any failure so notification problems can never break a scan."""
    try:
        from app.discovery.notify import notify_mam_scan_complete
        await notify_mam_scan_complete(
            scanned=int(state._mam_scan_progress.get("scanned", 0)),
            found=int(state._mam_scan_progress.get("found", 0)),
            possible=int(state._mam_scan_progress.get("possible", 0)),
            not_found=int(state._mam_scan_progress.get("not_found", 0)),
        )
    except Exception:
        logger.debug("MAM scan notify failed", exc_info=True)


async def _get_mam_token() -> str:
    """Get the active MAM token from the best source.

    Thin wrapper around `app.mam.cookie.get_active_token()` —
    kept for backwards compat with this module's existing callers.
    See that helper for the canonical resolution priority.
    """
    from app.mam.cookie import get_active_token
    return await get_active_token()


async def _mam_ready(s: dict) -> bool:
    """Is MAM enabled AND has a usable token in any of the three sources?

    Use this for endpoint gating instead of `s.get("mam_session_id")`,
    which is always empty after the Sprint 6 encrypted-store migration.
    """
    if not s.get("mam_enabled"):
        return False
    return bool(await _get_mam_token())


@router.post("/validate")
async def mam_validate_endpoint():
    """Test MAM session ID — runs IP registration + search auth."""
    s = load_settings()
    session_id = await _get_mam_token()
    if not session_id:
        return {"success": False, "message": "No MAM session ID configured"}
    skip_ip = s.get("mam_skip_ip_update", False)
    result = await mam_validate(session_id, skip_ip)
    if result["success"]:
        s["mam_enabled"] = True
        s["last_mam_validated_at"] = time.time()
        s["mam_validation_ok"] = True
    else:
        s["mam_validation_ok"] = False
    save_settings(s)
    return result


@router.get("/status")
async def mam_status_endpoint():
    """Get MAM integration status and stats."""
    s = load_settings()
    enabled = await _mam_ready(s)
    if not enabled:
        return {"enabled": False, "stats": None, "full_scan": None}
    db = await get_db()
    try:
        stats = await get_mam_stats(db)
        scan_status = await mam_get_full_scan_status(db)
        return {"enabled": True, "stats": stats, "full_scan": scan_status,
                "validation_ok": s.get("mam_validation_ok", True),
                "last_validated_at": s.get("last_mam_validated_at")}
    finally:
        await db.close()


@router.post("/scan")
async def mam_scan_endpoint(limit: int = Query(None, ge=1)):
    """Scan books missing MAM data across ALL discovered libraries.

    Snapshots eligible book IDs from each library's DB at start (the
    "snapshot guarantee": new books added mid-scan wait for the next
    run). Then iterates each library, batching at 150 books with a
    1-min pause between batches and a content_type-aware MAM lookup
    per library (ebook vs audiobook).

    `limit`, if provided, caps the TOTAL across libraries — earlier
    libraries fill first, later ones get whatever's left.
    """
    s = load_settings()
    if not await _mam_ready(s):
        return {"error": "MAM not configured or not enabled"}
    if not s.get("mam_scanning_enabled", True):
        return {"error": "MAM scanning is disabled — enable it in Settings"}
    if state._mam_scan_progress.get("running"):
        return {"error": "A MAM scan is already running"}

    # Per-library snapshots. List of (lib_dict, [book_id, ...]) so the
    # async task below has everything it needs without re-querying.
    # Iteration order matches `_discovered_libraries` (configured order).
    per_lib_snapshots: list[tuple[dict, list[int]]] = []
    remaining_budget = limit if limit else None
    # Apply the recently-scanned skip + oldest-first ordering so the
    # Command Center's manual MAM scan honors the same filters as the
    # full-scan and live-eligibility paths. Without this the snapshot
    # would re-include every Possible/Not Found book regardless of
    # when it was last scanned, defeating the whole point of the
    # mam_recent_scan_skip_days setting (Mark's UAT 2026-05-09:
    # canceled scan came back with the same 1,932 books even though
    # 1,127 of them had been freshly stamped).
    cutoff = _recent_scan_cutoff_seconds(s)
    skip_clause = _recent_scan_skip_clause(cutoff)
    order_clause = _recent_scan_order_clause()
    for lib in state._discovered_libraries:
        if remaining_budget is not None and remaining_budget <= 0:
            break
        ldb = await get_db(slug=lib["slug"])
        try:
            id_rows = await ldb.execute_fetchall(
                f"SELECT id FROM books WHERE {_NEEDS_SCAN_BASIC_BARE}"
                f"{skip_clause} ORDER BY {order_clause}"
            )
            ids = [r[0] for r in id_rows]
        finally:
            await ldb.close()
        if not ids:
            continue
        if remaining_budget is not None:
            ids = ids[:remaining_budget]
            remaining_budget -= len(ids)
        per_lib_snapshots.append((lib, ids))

    scan_total = sum(len(ids) for _, ids in per_lib_snapshots)
    if scan_total == 0:
        return {"status": "complete", "message": "No books need scanning — all already have MAM data"}

    state._mam_scan_progress = {"running": True, "scanned": 0, "total": scan_total,
                          "found": 0, "possible": 0, "not_found": 0, "errors": 0,
                          "current_book": "", "current_library": "",
                          "status": "scanning", "type": "manual"}

    async def _wait_for_other_writers():
        """Yield to a running Calibre sync before grabbing the write lock.

        ONLY blocks on Calibre sync — author/source scans are allowed to
        run concurrently. The asymmetry is deliberate: Calibre sync does
        massive bulk inserts inside big transactions and can hold the
        SQLite write lock for tens of seconds, longer than busy_timeout
        is willing to wait. Author scans, by contrast, do small per-row
        UPDATEs with sub-100ms write windows that WAL mode + the 30s
        busy_timeout absorb cleanly. Blocking on author scans here would
        let one long Sanderson source scan stall the entire MAM queue,
        which is the symptom that motivated splitting these out.
        """
        if state._library_sync_in_progress:
            state._mam_scan_progress["status"] = "waiting (library sync running)"
            logger.info("MAM scan waiting for library sync to finish...")
            while state._library_sync_in_progress:
                await asyncio.sleep(5)
            logger.info("Library sync finished — MAM scan resuming")
        state._mam_scan_progress["status"] = "scanning"

    async def _do_scan():
        batch_num = 0
        for lib, snapshot_ids in per_lib_snapshots:
            slug = lib["slug"]
            lib_name = lib.get("display_name") or lib.get("name") or slug
            ct = lib.get("content_type", "ebook")
            state._mam_scan_progress["current_library"] = lib_name
            cursor = 0  # index into THIS library's snapshot
            lib_total = len(snapshot_ids)
            logger.info(
                f"MAM scan: starting '{lib_name}' "
                f"({ct}, {lib_total} books)"
            )
            while True:
                # Wait for Calibre sync (only) before starting next batch
                await _wait_for_other_writers()
                cs = load_settings()
                cs_token = await _get_mam_token()
                if not cs.get("mam_enabled") or not cs_token:
                    state._mam_scan_progress.update({"status": "stopped (MAM disabled)", "running": False, "current_library": ""})
                    return
                db = await get_db(slug=slug)
                try:
                    # Capture per-batch baselines so the progress closure
                    # can add this batch's incremental stats onto the
                    # totals carried over from prior batches AND prior
                    # libraries. The variables must be defined OUTSIDE
                    # the closure (rather than read from state inside it)
                    # so each batch's running totals don't double-count.
                    base_scanned = state._mam_scan_progress["scanned"]
                    base_found = state._mam_scan_progress["found"]
                    base_possible = state._mam_scan_progress["possible"]
                    base_not_found = state._mam_scan_progress["not_found"]
                    base_errors = state._mam_scan_progress["errors"]
                    def _progress(stats):
                        state._mam_scan_progress.update({
                            "scanned": base_scanned + stats["scanned"],
                            "found": base_found + stats["found"],
                            "possible": base_possible + stats["possible"],
                            "not_found": base_not_found + stats["not_found"],
                            "errors": base_errors + stats["errors"],
                            # Forward the in-flight book title from the
                            # source layer up to the unified scan widget.
                            "current_book": stats.get("current_book", ""),
                        })
                    # Slice the next batch out of the frozen per-library
                    # snapshot. The snapshot guarantee: only IDs captured
                    # at scan start are processed, ever. 150 books per
                    # batch is a balance between throughput and staying
                    # clear of MAM rate limits.
                    batch_ids = snapshot_ids[cursor:cursor + 150]
                    if not batch_ids:
                        # Library exhausted — fall through to the
                        # next library in the outer loop.
                        await db.close()
                        break
                    result = await mam_scan_batch(
                        db, session_id=cs_token, limit=len(batch_ids),
                        delay=cs.get("rate_mam", 2), skip_ip_update=True,
                        format_priority=cs.get("audiobook_format_priority" if ct == "audiobook" else "mam_format_priority"),
                        on_progress=_progress,
                        lang_ids=_resolve_mam_languages(cs.get("languages", ["English"])),
                        book_ids=batch_ids,
                        content_type=ct,
                    )
                    if result.get("error"):
                        # Connection-level errors abort the whole
                        # cross-library scan; per-book errors are
                        # already tallied via on_progress and don't
                        # show up here.
                        state._mam_scan_progress.update({"status": f"error: {result['error']}", "running": False, "current_library": ""})
                        return
                    cursor += len(batch_ids)
                    await db.execute(
                        "INSERT INTO sync_log (sync_type, started_at, finished_at, status, books_found, books_new) VALUES (?,?,?,?,?,?)",
                        ("mam", time.time(), time.time(), "complete",
                         result.get("scanned", 0), result.get("found", 0))
                    )
                    await db.commit()
                except Exception as e:
                    logger.error(f"MAM scan batch error: {e}", exc_info=True)
                    state._mam_scan_progress.update({"status": f"error: {e}", "running": False, "current_library": ""})
                    return
                finally:
                    await db.close()
                if cursor >= lib_total:
                    # This library's snapshot is exhausted — break
                    # to the outer loop so we move on to the next
                    # library (or finish if this was the last).
                    break
                batch_num += 1
                state._mam_scan_progress["status"] = "paused"
                # 1-minute pause between batches.
                logger.info(f"MAM scan batch {batch_num} done ({state._mam_scan_progress['scanned']}/{state._mam_scan_progress['total']}), pausing 1 min")
                await asyncio.sleep(60)
                state._mam_scan_progress["status"] = "scanning"
                # Wait for Calibre sync (only) to finish before resuming
                await _wait_for_other_writers()

        # All libraries exhausted.
        state._mam_scan_progress.update({"status": "complete", "running": False, "current_library": ""})
        logger.info(
            f"MAM scan complete: {state._mam_scan_progress['scanned']}/"
            f"{scan_total} scanned, {state._mam_scan_progress['found']} found "
            f"across {len(per_lib_snapshots)} libraries"
        )
        await _notify_mam_done()

    state._mam_scan_task = asyncio.create_task(_do_scan())
    return {"status": "started", "total": scan_total,
            "libraries": [lib["slug"] for lib, _ in per_lib_snapshots]}


@router.post("/scan/cancel")
async def mam_scan_cancel():
    """Cancel the currently running MAM scan.

    Three kinds of MAM scan can be active at once (in theory), each
    in a different task, so the cancel plumbing has three paths:

      - Manual scan (POST /mam/scan):  runs as `_mam_scan_task` and
        is cancelled via `task.cancel()` — the CancelledError
        propagates through the await and the task's own handler
        flips `running` to False.
      - Scheduled scan (via `_mam_scheduler`): runs inline inside
        the long-lived scheduler loop, NOT as a discrete task, so
        `task.cancel()` would kill the whole scheduler. Instead we
        set `_scheduled_mam_cancel_requested` — the scheduler's
        `mam_scan_batch` call receives it through a `cancel_check`
        closure and aborts at the next per-book boundary. The
        scheduler also bumps `last_scan_at` after the cancel so
        its next-tick re-trigger doesn't fire a new scan immediately
        (you'd see a cancel-then-instant-restart otherwise).
      - Full scan (POST /mam/full-scan): has its own endpoint at
        /mam/full-scan/cancel — this endpoint explicitly doesn't
        touch `_mam_full_scan_task` to avoid overlap.

    Pre-v1.1.9 this endpoint only handled the manual case, so
    clicking Stop on a scheduled scan silently did nothing and
    the misleading "No MAM scan running" fallback response made
    it look like the UI was desynced.
    """
    if state._mam_scan_task and not state._mam_scan_task.done():
        state._mam_scan_task.cancel()
        state._mam_scan_progress.update({"running": False, "status": "cancelled"})
        logger.info("MAM scan cancelled by user")
        return {"status": "ok", "message": "MAM scan cancelled"}
    # Scheduled-scan path: the scheduler owns the task, not us.
    # Flip the cooperative-cancel flag and let the per-book loop
    # inside mam_scan_batch notice it at the next boundary (≤2s).
    if (state._mam_scan_progress.get("running")
            and state._mam_scan_progress.get("type") == "scheduled"):
        state._scheduled_mam_cancel_requested = True
        logger.info("MAM scheduled scan cancel requested")
        return {"status": "ok", "message": "MAM scan cancelled"}
    return {"status": "ok", "message": "No MAM scan running"}


@router.get("/scan/status")
async def mam_scan_status_endpoint():
    """Get progress of any active MAM scan (manual, scheduled, or full)."""
    if state._mam_scan_progress.get("running"):
        return dict(state._mam_scan_progress)
    if state._mam_full_scan_task and not state._mam_full_scan_task.done():
        db = await get_db()
        try:
            fs = await mam_get_full_scan_status(db)
            if fs.get("active"):
                return {"running": True, "scanned": fs.get("scanned", 0),
                        "total": fs.get("total_books", 0), "found": 0,
                        "possible": 0, "not_found": 0, "errors": 0,
                        "status": "scanning", "type": "full_scan",
                        "progress_pct": fs.get("progress_pct", 0)}
        finally:
            await db.close()
    return dict(state._mam_scan_progress)


@router.post("/test-scan")
async def mam_test_scan():
    """Run a quick test scan of 10 books and return results inline."""
    s = load_settings()
    token = await _get_mam_token()
    if not s.get("mam_enabled") or not token:
        return {"error": "MAM not configured or not enabled"}
    if not s.get("mam_scanning_enabled", True):
        return {"error": "MAM scanning is disabled — enable it in Settings"}
    if state._mam_scan_task and not state._mam_scan_task.done():
        return {"error": "A MAM scan is already running — wait for it to finish"}
    db = await get_db()
    try:
        _ct = _active_content_type()
        result = await mam_scan_batch(
            db, session_id=token, limit=10,
            delay=s.get("rate_mam", 2),
            skip_ip_update=True,
            format_priority=s.get("audiobook_format_priority" if _ct == "audiobook" else "mam_format_priority"),
            lang_ids=_resolve_mam_languages(s.get("languages", ["English"])),
            content_type=_ct,
        )
        return result
    finally:
        await db.close()


@router.post("/full-scan")
async def mam_full_scan_start():
    """Start a full MAM library scan (400 books/batch, 5-min pause between).

    v2.3.7 — multi-library aware. Iterates every discovered library
    sequentially; each library has its own `mam_scan_log` row so
    snapshotting + resume work per-library. Pre-v2.3.7 only the
    active library was scanned, leaving the other library's MAM data
    silently stale. Calibre/ABS deployments needed two manual flips
    of the active library to cover both content types.

    Long-running and persistent: state lives in `mam_scan_log` (including
    the snapshotted book ID list) so the scan survives a process restart
    and can resume from where it left off. Like the manual scan, only
    blocks on a concurrent Calibre sync, not on author scans — see the
    `_wait_for_other_writers` rationale in /api/mam/scan above.
    """

    # Snapshot which libraries we'll scan + start a per-library
    # scan_log entry for each. Each call to mam_start_full_scan
    # writes its own (book_ids_snapshot) tied to that library's DB.
    libs_to_scan: list[dict] = list(state._discovered_libraries)
    if not libs_to_scan:
        return {"error": "No libraries discovered"}

    started: list[tuple[dict, dict]] = []  # (lib, start_result)
    grand_total = 0
    for lib in libs_to_scan:
        slug = lib.get("slug")
        try:
            db = await get_db(slug) if slug else await get_db()
        except Exception as e:
            logger.warning(f"full-scan: cannot open lib {slug}: {e}")
            continue
        try:
            sr = await mam_start_full_scan(db)
        finally:
            await db.close()
        if "error" in sr:
            # "scan already running" on one library shouldn't abort
            # the whole multi-library start — log + skip that lib.
            logger.info(f"full-scan: skipping {slug}: {sr['error']}")
            continue
        started.append((lib, sr))
        grand_total += sr.get("total_books", 0)

    if not started:
        return {"error": "No libraries had scannable books"}

    async def _full_scan_loop():
        state._mam_scan_progress = {
            "running": True, "scanned": 0,
            "total": grand_total,
            "found": 0, "possible": 0, "not_found": 0,
            "errors": 0, "current_book": "", "current_library": "",
            "status": "scanning", "type": "full_scan",
        }
        try:
            for lib, start_result in started:
                slug = lib.get("slug")
                lib_name = lib.get("display_name") or lib.get("name") or slug or "active"
                lib_ct = lib.get("content_type") or "ebook"
                state._mam_scan_progress["current_library"] = lib_name
                while True:
                    try:
                        db = await get_db(slug) if slug else await get_db()
                    except Exception as e:
                        logger.error(
                            f"full-scan: cannot reopen {lib_name} mid-loop: {e}"
                        )
                        break
                    try:
                        cs = load_settings()
                        cs_token = await _get_mam_token()
                        base_scanned = state._mam_scan_progress["scanned"]
                        base_found = state._mam_scan_progress["found"]
                        base_possible = state._mam_scan_progress["possible"]
                        base_not_found = state._mam_scan_progress["not_found"]
                        base_errors = state._mam_scan_progress["errors"]

                        def _on_book(title: str) -> None:
                            state._mam_scan_progress["current_book"] = title or ""

                        def _on_progress(stats: dict) -> None:
                            state._mam_scan_progress.update({
                                "scanned":   base_scanned + stats["scanned"],
                                "found":     base_found + stats["found"],
                                "possible":  base_possible + stats["possible"],
                                "not_found": base_not_found + stats["not_found"],
                                "errors":    base_errors + stats["errors"],
                                "current_book": stats.get("current_book", ""),
                            })

                        result = await mam_run_full_scan_batch(
                            db, session_id=cs_token,
                            skip_ip_update=True,
                            delay=cs.get("rate_mam", 2),
                            format_priority=cs.get(
                                "audiobook_format_priority"
                                if lib_ct == "audiobook"
                                else "mam_format_priority"
                            ),
                            lang_ids=_resolve_mam_languages(cs.get("languages", ["English"])),
                            on_book=_on_book,
                            content_type=lib_ct,
                            on_progress=_on_progress,
                        )
                        fs = await mam_get_full_scan_status(db)
                        state._mam_scan_progress.update({
                            "scanned": (
                                fs.get("scanned", base_scanned + result.get("scanned", 0))
                            ),
                            "status": (
                                "scanning" if result["status"] == "batch_complete"
                                else result["status"]
                            ),
                        })
                    finally:
                        await db.close()
                    if result["status"] in ("scan_complete", "error", "no_scan"):
                        # This library is done — move to the next.
                        break
                    elif result["status"] == "batch_complete":
                        state._mam_scan_progress["status"] = "paused"
                        logger.info(
                            f"Full MAM scan ({lib_name}): batch done, waiting 5 min"
                        )
                        await asyncio.sleep(300)
                        state._mam_scan_progress["status"] = "scanning"

            state._mam_scan_progress.update({
                "running": False, "status": "scan_complete",
                "current_library": "", "current_book": "",
            })
            await _notify_mam_done()
        except asyncio.CancelledError:
            state._mam_scan_progress.update({
                "running": False, "status": "cancelled",
                "current_library": "", "current_book": "",
            })
            logger.info("Full MAM scan cancelled by user")
            raise

    state._mam_full_scan_task = asyncio.create_task(_full_scan_loop())
    return {
        "status": "started",
        "scan_ids": [sr["id"] for _, sr in started],
        "total_books": grand_total,
        "libraries": [lib.get("slug") for lib, _ in started if lib.get("slug")],
    }


@router.get("/full-scan/status")
async def mam_full_scan_status():
    """Get progress of the current/most recent full MAM scan."""
    db = await get_db()
    try:
        return await mam_get_full_scan_status(db)
    finally:
        await db.close()


@router.post("/full-scan/cancel")
async def mam_full_scan_cancel():
    """Cancel a running full MAM scan."""
    db = await get_db()
    try:
        result = await mam_cancel_full_scan(db)
    finally:
        await db.close()
    if state._mam_full_scan_task and not state._mam_full_scan_task.done():
        state._mam_full_scan_task.cancel()
    return result


@router.post("/toggle")
async def mam_toggle():
    """Toggle MAM features on/off (only works if session ID exists)."""
    s = load_settings()
    if not await _get_mam_token():
        return {"error": "No MAM session ID configured"}
    s["mam_enabled"] = not s.get("mam_enabled", False)
    save_settings(s)
    return {"enabled": s["mam_enabled"]}


@router.get("/books")
async def mam_books_endpoint(section: str = "upload", search: str = "",
                              sort: str = "title", page: int = 1, per_page: int = 50,
                              slug: str | None = None):
    """Get books for the MAM page, filtered by section.

    `slug=X` targets a specific library DB — used by the MAM page's
    library selector so the user can view ebook-library results
    independently from audiobook-library results. Omitted falls back
    to the active library. Cross-library badges + per-library cover
    paths work because each returned book is stamped with
    `library_slug` + `content_type`.

    NOTE: Audiobook MAM scanning is still gated on the hardcoded
    main_cat in _mam_search (`EBOOK_CATEGORY`) — this endpoint
    serves whatever the DB already holds. Audiobook entries will
    mostly show as unscanned until the search path is extended to
    accept audiobook main_cat IDs.
    """
    from app import state as _state
    effective_slug = slug or None  # get_db(None) falls back to active
    db = await get_db(effective_slug)
    try:
        if section == "upload":
            where = "b.owned=1 AND b.mam_status='not_found' AND b.hidden=0"
        elif section == "download":
            where = "b.owned=0 AND b.mam_status IN ('found','possible') AND b.is_unreleased=0 AND b.hidden=0"
        elif section == "missing_everywhere":
            where = "b.owned=0 AND b.mam_status='not_found' AND b.is_unreleased=0 AND b.hidden=0"
        else:
            return {"error": f"Unknown section: {section}"}

        params = []
        if search:
            where += " AND (b.title LIKE ? OR a.name LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])

        sort_map = {"title": "b.title ASC", "author": "a.name ASC",
                    "date": "b.pub_date DESC", "series": "s.name ASC, b.series_index ASC"}
        order = sort_map.get(sort, "b.title ASC")

        count_sql = f"SELECT COUNT(*) FROM books b JOIN authors a ON b.author_id=a.id LEFT JOIN series s ON b.series_id=s.id WHERE {where}"
        count_row = await db.execute_fetchall(count_sql, params)
        total = count_row[0][0] if count_row else 0

        offset = (page - 1) * per_page
        # Pre-aggregated series_total (same refactor as routers/books.py) —
        # replaces a correlated COUNT(*) that fired once per returned row.
        data_sql = f"""SELECT b.*, a.name as author_name, s.name as series_name,
            COALESCE(st.series_total, 0) as series_total,
            COALESCE(st.mainline_total, 0) as mainline_total
            FROM books b JOIN authors a ON b.author_id=a.id
            LEFT JOIN series s ON b.series_id=s.id
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
            WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?"""
        rows = await db.execute_fetchall(data_sql, params + [per_page, offset])
        # Resolve content_type for this library once so each book
        # carries it — the frontend uses it to pick the audiobook
        # pill color and route to per-library cover endpoints.
        resolved_slug = effective_slug
        if not resolved_slug:
            from app.discovery.database import get_active_library as _active
            resolved_slug = _active() or ""
        content_type = next(
            (l.get("content_type", "ebook") for l in _state._discovered_libraries
             if l.get("slug") == resolved_slug),
            "ebook",
        )
        books = [
            {**dict(r), "library_slug": resolved_slug, "content_type": content_type}
            for r in rows
        ]
        # v3.0.0 Phase 7 — multi-author byline on the MAM-status book cards.
        await attach_contributors(db, books)

        return {"books": books, "total": total, "page": page, "per_page": per_page,
                "total_pages": (total + per_page - 1) // per_page}
    finally:
        await db.close()


@router.post("/scan-book/{book_id}")
async def mam_scan_single_book(book_id: int, slug: str | None = Query(None)):
    """Re-scan a single book against MAM, ignoring its existing mam_status.

    Legacy single-book endpoint. The BookSidebar's Re-scan button uses
    `/discovery/books/scan-mam` with a one-element book_ids array
    (which honors slug too, v2.3.7); this endpoint is kept for any
    integration that still calls it directly.

    `slug` query param routes the read+write to a specific library.
    Without slug the active library is used — same cross-library
    id-collision risk as `update_book` (v2.3.4.4 UAT canary). The
    library's content_type is used for MAM category routing so an
    audiobook rescan from a non-active context doesn't search the
    ebook MAM main_cat.
    """
    s = load_settings()
    token = await _get_mam_token()
    if not s.get("mam_enabled") or not token:
        return {"error": "MAM not configured or not enabled"}
    if not s.get("mam_scanning_enabled", True):
        return {"error": "MAM scanning is disabled — enable it in Settings"}

    db = await get_db(slug)
    try:
        # series JOIN required so series_name reaches check_book →
        # Fix E (series-bundle promote) can fire. UAT 2026-05-11
        # round 4 — see books.py:scan_books_mam comment for full
        # rationale.
        rows = await db.execute_fetchall(
            "SELECT b.id, b.title, a.name, s.name AS series_name "
            "FROM books b JOIN authors a ON b.author_id=a.id "
            "LEFT JOIN series s ON b.series_id = s.id "
            "WHERE b.id=?",
            (book_id,),
        )
        if not rows:
            return {"error": f"Book {book_id} not found"}
        _, title, author, series = rows[0]

        # Prefer requested library's content_type when slug is set.
        if slug:
            lib_ct = next(
                (l.get("content_type") for l in state._discovered_libraries
                 if l.get("slug") == slug),
                None,
            )
            ct = lib_ct or "ebook"
        else:
            ct = _active_content_type()
        from app.discovery.cover_phash import ensure_cover_phash
        seshat_phash = await ensure_cover_phash(db, book_id, token=token)
        check = await mam_check_book(
            token, title, author,
            format_priority=s.get("audiobook_format_priority" if ct == "audiobook" else "mam_format_priority"),
            delay=s.get("rate_mam", 2),
            lang_ids=_resolve_mam_languages(s.get("languages", ["English"])),
            series_name=series or "",
            content_type=ct,
            seshat_cover_phash=seshat_phash,
        )
        # Stamp mam_last_scanned_at on successful scans only — see
        # scan_books_batch for the auth_error-skip rationale.
        await db.execute("""
            UPDATE books SET mam_url=?, mam_status=?, mam_formats=?,
                   mam_torrent_id=?, mam_has_multiple=?, mam_my_snatched=?,
                   mam_is_bundle=?,
                   mam_last_scanned_at=CASE
                       WHEN ? = 'auth_error' THEN mam_last_scanned_at
                       ELSE ?
                   END
            WHERE id=?
        """, (
            check["mam_url"], check["status"], check["mam_formats"],
            check["mam_torrent_id"],
            1 if check["mam_has_multiple"] else 0,
            1 if check.get("mam_my_snatched") else 0,
            1 if check.get("mam_is_bundle") else 0,
            check["status"],
            time.time(),
            book_id,
        ))
        await db.commit()
        return {
            "status": check["status"],
            "mam_url": check["mam_url"],
            "mam_torrent_id": check["mam_torrent_id"],
            "mam_title": check.get("mam_title"),
            "mam_formats": check["mam_formats"],
            "mam_has_multiple": check["mam_has_multiple"],
            "mam_my_snatched": check.get("mam_my_snatched", False),
            "mam_is_bundle": check.get("mam_is_bundle", False),
            "match_pct": check.get("match_pct"),
            "best_format": check.get("best_format"),
            "passes_tried": check.get("passes_tried", []),
        }
    finally:
        await db.close()


@router.post("/scan-author/{author_id}")
async def mam_scan_single_author(author_id: int, slug: str | None = None):
    """Scan all of an author's missing/un-scanned books against MAM.

    `slug=X` scans the author in a specific library (needed for the
    cross-library author detail page — the author id only resolves
    correctly in its origin library). Temporarily flips the active
    library for the scan's duration so `get_db()` inside the worker
    uses the right DB, then restores on finish.

    Spawned as a background asyncio task tracked through
    `state._mam_scan_task` / `state._mam_scan_progress` so the unified
    Dashboard widget shows live progress and the Stop button can cancel
    mid-run. Honors the same one-MAM-scan-at-a-time lock as the manual
    batch scan.
    """
    s = load_settings()
    token = await _get_mam_token()
    if not s.get("mam_enabled") or not token:
        raise HTTPException(400, "MAM not configured or not enabled")
    if not s.get("mam_scanning_enabled", True):
        raise HTTPException(400, "MAM scanning is disabled — enable it in Settings")
    if state._mam_scan_progress.get("running"):
        raise HTTPException(409, "A MAM scan is already running")
    if state._mam_scan_task and not state._mam_scan_task.done():
        raise HTTPException(409, "A MAM scan is already running")

    from app.discovery.database import get_active_library as _active, set_active_library as _set_active
    original_slug = _active()
    target_slug = slug or original_slug
    flip = bool(slug and slug != original_slug)
    db = await get_db(target_slug)
    try:
        rows = await db.execute_fetchall(
            "SELECT name FROM authors WHERE id=?", (author_id,),
        )
        if not rows:
            raise HTTPException(404, f"Author {author_id} not found")
        author_name = rows[0][0]

        # series JOIN required so series_name reaches check_book →
        # Fix E (series-bundle promote) can fire. UAT 2026-05-11
        # round 4 — see books.py:scan_books_mam comment.
        from app.discovery.sources.mam import _NEEDS_SCAN_BASIC_ALIASED
        book_rows = await db.execute_fetchall(
            f"SELECT b.id, b.title, s.name AS series_name "
            f"FROM books b LEFT JOIN series s ON b.series_id = s.id "
            f"WHERE b.author_id=? AND {_NEEDS_SCAN_BASIC_ALIASED} "
            "ORDER BY b.title",
            (author_id,),
        )
    finally:
        await db.close()

    if not book_rows:
        # Nothing to scan — surface as a benign idle status (the unified
        # widget will render this as a "complete" row that auto-clears).
        state._mam_scan_progress = {
            "running": False, "scanned": 0, "total": 0,
            "found": 0, "possible": 0, "not_found": 0, "errors": 0,
            "current_book": "",
            "status": "complete", "type": "manual",
        }
        return {"status": "complete", "message": "No un-scanned books for this author",
                "scanned": 0, "found": 0, "possible": 0, "not_found": 0}

    state._mam_scan_progress = {
        "running": True, "scanned": 0, "total": len(book_rows),
        "found": 0, "possible": 0, "not_found": 0, "errors": 0,
        "current_book": "",
        "status": "scanning", "type": "manual",
    }

    delay = s.get("rate_mam", 2)
    # Route format priority + main_cat by the target library's content_type.
    scan_ct = _active_content_type(target_slug)
    format_priority = s.get("audiobook_format_priority" if scan_ct == "audiobook" else "mam_format_priority")
    # `token` already resolved above via _get_mam_token() and gated on
    lang_ids = _resolve_mam_languages(s.get("languages", ["English"]))

    async def _do_scan():
        if flip:
            _set_active(slug)
        bdb = await get_db()
        try:
            from app.discovery.cover_phash import ensure_cover_phash
            for bid, btitle, bseries in book_rows:
                # Surface the title BEFORE the network call so the
                # widget shows what we're waiting on, not what we just
                # finished.
                state._mam_scan_progress["current_book"] = btitle
                seshat_phash = await ensure_cover_phash(bdb, bid, token=token)
                try:
                    check = await mam_check_book(token, btitle, author_name, format_priority, delay, lang_ids=lang_ids, series_name=bseries or "", content_type=scan_ct, seshat_cover_phash=seshat_phash)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Author scan error on book {bid} ({btitle[:40]}): {e}")
                    state._mam_scan_progress["errors"] += 1
                    state._mam_scan_progress["scanned"] += 1
                    continue
                await bdb.execute("""
                    UPDATE books SET mam_url=?, mam_status=?, mam_formats=?,
                           mam_torrent_id=?, mam_has_multiple=?, mam_my_snatched=?,
                           mam_is_bundle=?
                    WHERE id=?
                """, (
                    check["mam_url"], check["status"], check["mam_formats"],
                    check["mam_torrent_id"],
                    1 if check["mam_has_multiple"] else 0,
                    1 if check.get("mam_my_snatched") else 0,
                    1 if check.get("mam_is_bundle") else 0,
                    bid,
                ))
                state._mam_scan_progress["scanned"] += 1
                if check["status"] == "found":
                    state._mam_scan_progress["found"] += 1
                elif check["status"] == "possible":
                    state._mam_scan_progress["possible"] += 1
                elif check["status"] == "not_found":
                    state._mam_scan_progress["not_found"] += 1
            await bdb.commit()
            state._mam_scan_progress.update({"running": False, "status": "complete"})
            await _notify_mam_done()
        except asyncio.CancelledError:
            state._mam_scan_progress.update({"running": False, "status": "cancelled"})
            raise
        except Exception as e:
            logger.error(f"MAM single-author scan failed: {e}", exc_info=True)
            state._mam_scan_progress.update({"running": False, "status": f"error: {e}"})
        finally:
            await bdb.close()
            if flip and original_slug:
                _set_active(original_slug)

    state._mam_scan_task = asyncio.create_task(_do_scan())
    return {"status": "started", "author": author_name, "total": len(book_rows)}


@router.post("/reset")
async def mam_reset_scans():
    """Reset all MAM scan data — clears all mam_* fields on all books."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE books SET mam_url=NULL, mam_status=NULL, mam_formats=NULL, "
            "mam_torrent_id=NULL, mam_has_multiple=0, mam_my_snatched=0"
        )
        await db.execute("DELETE FROM mam_scan_log")
        await db.commit()
        return {"status": "ok", "message": "All MAM scan data cleared"}
    finally:
        await db.close()
