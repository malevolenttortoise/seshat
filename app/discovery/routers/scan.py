"""
Scan-orchestration endpoints.

Three scan kinds run through this router:
  - Library sync: imports the user's curated library (Calibre today,
    other backends in the future) into the discovery DB.
  - Author/source lookup: hits Goodreads/Hardcover/Kobo for each author.
  - Full re-scan: same as lookup but visits every book page to refresh
    metadata, ignoring the cache window.

Plus the unified `/scan-status` endpoint that the Dashboard polls so it
can render every active scan side-by-side, regardless of which router
actually started the scan. The lookup-specific and MAM-specific status
endpoints still exist (consumed by the MAMPage and SettingsPage), so
this file *projects* the underlying state dicts into a uniform shape
rather than restructuring them.

Endpoints:
  /api/sync/library, /api/sync, /api/sync/calibre     — manual library sync
  /api/sync/lookup, /api/lookup                       — start author scan
  /api/lookup/cancel, /api/lookup/status              — control + status
  /api/sync/full-rescan                               — full re-scan
  /api/scan-status                                    — unified Dashboard feed
  /api/scanning/{author,mam}/toggle                   — feature on/off
"""
import asyncio
import logging
import os
import time
from fastapi import APIRouter, HTTPException

from app.discovery.calibre_sync import sync_calibre
from app.config import load_settings, save_settings
from app.discovery.database import get_active_library, get_db, set_active_library
from app.library_apps import get_app
from app.discovery.lookup import run_full_lookup, run_full_rescan
from app import state

logger = logging.getLogger("seshat.discovery")

router = APIRouter(prefix="/api/discovery", tags=["scan"])


# ─── Library Sync ────────────────────────────────────────────
@router.post("/sync/library")
async def trigger_sync(slug: str | None = None):
    """Manual sync of a specific library (defaults to the active one).

    `slug` targets a specific discovered library so the Command Center
    can offer per-library sync buttons (Calibre + ABS) without flipping
    the user's active library. Sync functions read the active slug via
    `get_active_library()`, so we temporarily switch it for the sync
    and restore the original afterward — same pattern scheduled_jobs
    uses for its iteration.

    Routes through the target library backend's `sync()` method via
    its `LibraryApp` adapter. Flagged through
    `state._library_sync_in_progress` so MAM and other write-heavy
    background tasks yield to us cleanly.
    """
    original_active = get_active_library()
    target_slug = slug or original_active
    lib = next((l for l in state._discovered_libraries if l["slug"] == target_slug), None)
    if slug and not lib:
        raise HTTPException(404, f"Library '{slug}' not found")
    # Flag the sync so background writers (MAM scanner) yield to us
    # instead of racing on the write lock. Always cleared in finally.
    state._library_sync_in_progress = True
    try:
        if lib:
            if slug and slug != original_active:
                set_active_library(slug)
            app_instance = get_app(lib.get("app_type", "calibre"))
            if app_instance:
                result = await app_instance.sync(lib)
            else:
                result = await sync_calibre(lib["source_db_path"], lib["library_path"])
            # Update mtime after successful manual sync
            from app.discovery import sync_state as _sync_state
            s = load_settings()
            current_mtime = (
                await app_instance.get_mtime(lib) if app_instance
                else os.path.getmtime(lib["source_db_path"])
            )
            _sync_state.record_completion(
                s, target_slug,
                mtime=current_mtime,
                mode=(result or {}).get("mode", "full"),
            )
            save_settings(s)
            try:
                from app.discovery.notify import notify_library_sync
                await notify_library_sync(
                    lib.get("display_name") or lib.get("name") or "Library",
                    int((result or {}).get("books_new", 0)),
                    int((result or {}).get("books_updated", 0)),
                )
            except Exception:
                logger.debug("library-sync notify failed", exc_info=True)
        else:
            result = await sync_calibre()
        state._last_library_sync_check["at"] = time.time()
        state._last_library_sync_check["synced"] = True

        # Refresh cross-library links after a manual sync. Non-fatal —
        # matcher failures don't break the sync response.
        try:
            from app.works.matcher import rebuild_matches
            await rebuild_matches()
        except Exception:
            logger.debug("works matcher post-sync failed", exc_info=True)

        return {"status": "ok", **result}
    except Exception as e:
        try:
            from app.discovery import sync_state as _sync_state
            s = load_settings()
            _sync_state.record_failure(s, target_slug)
            save_settings(s)
        except Exception:
            logger.debug("record_failure crashed", exc_info=True)
        raise HTTPException(500, str(e))
    finally:
        state._library_sync_in_progress = False
        if slug and slug != original_active and original_active:
            set_active_library(original_active)


# Back-compat aliases. /sync/calibre is what the original public release
# documented; /sync is the legacy short alias. Both still hit the same
# handler so any older client / saved bookmark keeps working.
@router.post("/sync/calibre")
async def trigger_sync_calibre_alias():
    return await trigger_sync()


@router.post("/sync")
async def trigger_sync_alias():
    return await trigger_sync()


# ─── Author Lookup ───────────────────────────────────────────
async def _count_due_authors(cutoff: float) -> int:
    """Count authors in the currently-active library due for a source scan.

    Mirrors the iteration inside `run_full_lookup` — skip orphan authors
    with no linked books so the pre-flight "due count" doesn't overstate
    what the scan loop will actually visit.
    """
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT COUNT(*) c FROM authors WHERE COALESCE(last_lookup_at,0) < ? AND id IN (SELECT DISTINCT author_id FROM book_authors)",
            (cutoff,),
        )).fetchone()
        return row["c"] if row else 0
    finally:
        await db.close()


@router.post("/sync/lookup")
async def trigger_lookup(content_type: str | None = None):
    """Start an author source scan.

    `content_type=audiobook` (or `ebook`) fans the scan across every
    discovered library of that type — the Dashboard's "Scan Audiobooks"
    button passes it so audiobook libraries get visited even when the
    user's active library is an ebook one. Without it we scan only the
    active library (historical default).
    """
    s = load_settings()
    if not s.get("author_scanning_enabled", True):
        return {"error": "Author scanning is disabled — enable it in Settings"}
    if state._lookup_task and not state._lookup_task.done():
        return {"error": "An author scan is already running"}

    # Pick target libraries. `None` sentinel means "whatever the active
    # library resolves to" — preserves pre-multi-library behavior on
    # installs that haven't populated `_discovered_libraries` yet.
    if content_type is not None:
        target_slugs: list[str | None] = [
            l["slug"] for l in state._discovered_libraries
            if l.get("content_type") == content_type and l.get("slug")
        ]
        if not target_slugs:
            return {"status": "ok", "due": 0,
                    "message": f"No {content_type} libraries found."}
    else:
        active = get_active_library()
        target_slugs = [active] if active else [None]

    # Pre-flight: sum due-counts across every target library so the
    # progress bar starts with a correct total instead of jumping as
    # each library's scan kicks off. Switches active library per-read
    # and restores on the way out — same pattern as `trigger_sync`.
    cache_sec = s.get("lookup_interval_days", 3) * 86400
    cutoff = time.time() - cache_sec
    per_lib_due: list[tuple[str | None, int]] = []
    total_due = 0
    original_active = get_active_library()
    try:
        for slug in target_slugs:
            if slug is not None and slug != get_active_library():
                set_active_library(slug)
            count = await _count_due_authors(cutoff)
            per_lib_due.append((slug, count))
            total_due += count
    finally:
        if original_active and original_active != get_active_library():
            set_active_library(original_active)

    if total_due == 0:
        state._lookup_progress = {
            "running": False, "checked": 0, "total": 0, "current_author": "",
            "current_book": "",
            "new_books": 0, "type": "lookup",
            "status": f"no authors due (cache window: {s.get('lookup_interval_days', 3)} days)",
        }
        return {"status": "ok", "due": 0,
                "message": "No authors due for scanning within the current cache window."}

    state._lookup_progress = {"running": True, "checked": 0, "total": total_due, "current_author": "",
                        "current_book": "",
                        "new_books": 0, "status": "scanning", "type": "lookup"}

    async def _do():
        cumulative_checked = 0
        cumulative_new = 0
        aggregated_timeouts: dict[str, int] = {}
        original = get_active_library()
        try:
            for slug, count in per_lib_due:
                if count == 0:
                    continue
                if slug is not None and slug != get_active_library():
                    set_active_library(slug)

                def _progress(data, _base_checked=cumulative_checked, _base_new=cumulative_new):
                    state._lookup_progress.update({
                        "checked": _base_checked + int(data.get("checked", 0)),
                        "total": total_due,
                        "current_author": data.get("current_author", ""),
                        "new_books": _base_new + int(data.get("new_books", 0)),
                    })

                try:
                    result = await run_full_lookup(on_progress=_progress)
                except Exception as e:
                    logger.error(f"Author scan error on library '{slug}': {e}")
                    continue
                cumulative_checked += int(result.get("authors_checked", 0))
                cumulative_new += int(result.get("new_books", 0))
                for src, n in (result.get("source_timeouts") or {}).items():
                    aggregated_timeouts[src] = aggregated_timeouts.get(src, 0) + int(n)

            state._lookup_progress.update({
                "running": False, "status": "complete",
                "source_timeouts": aggregated_timeouts,
                "checked": cumulative_checked,
                "new_books": cumulative_new,
            })
            try:
                from app.discovery.notify import notify_scan_complete
                await notify_scan_complete(
                    label="Source Scan",
                    new_books=cumulative_new,
                    authors_total=cumulative_checked or 1,
                )
            except Exception:
                logger.debug("source-scan notify failed", exc_info=True)
            try:
                from app.orchestrator.sse_publishers import publish_toast
                await publish_toast(
                    "success",
                    f"Source scan complete: {cumulative_new} new books "
                    f"across {cumulative_checked} author(s)",
                )
            except Exception:
                logger.debug("source-scan toast failed", exc_info=True)
        except Exception as e:
            logger.error(f"Author scan error: {e}")
            state._lookup_progress.update({"running": False, "status": f"error: {e}"})
            try:
                from app.orchestrator.sse_publishers import publish_toast
                await publish_toast("error", f"Source scan failed: {e}")
            except Exception:
                logger.debug("source-scan error toast failed", exc_info=True)
        finally:
            if original and original != get_active_library():
                set_active_library(original)

    state._lookup_task = asyncio.create_task(_do())
    return {"status": "started", "due": total_due}


@router.post("/lookup")
async def trigger_lookup_alias(content_type: str | None = None):
    return await trigger_lookup(content_type=content_type)


@router.post("/lookup/cancel")
async def lookup_cancel():
    """Cancel the currently running author scan."""
    if state._lookup_task and not state._lookup_task.done():
        state._lookup_task.cancel()
        state._lookup_progress.update({"running": False, "status": "cancelled"})
        logger.info("Author scan cancelled by user")
        return {"status": "ok", "message": "Author scan cancelled"}
    return {"status": "ok", "message": "No author scan running"}


@router.get("/lookup/status")
async def lookup_status():
    """Get progress of the current/most recent author scan."""
    return dict(state._lookup_progress)


# ─── Unified scan status ─────────────────────────────────────
# Each kind of scan stores its progress in a different state dict with
# different field names (`checked`/`total`, `scanned`/`total`, etc.).
# The `_project_*` helpers below normalize them into a uniform shape:
#
#   { kind, type, label, running, current, total,
#     current_label, current_book, status, extra }
#
# The frontend maps over the resulting `scans` array and renders one row
# per active scan, so multiple scans can show side-by-side when MAM,
# author lookup, and Calibre sync are all running concurrently.
def _label_for(kind: str, scan_type: str) -> str:
    """Human-readable label for a (kind, type) pair."""
    if kind == "lookup":
        return {
            "lookup":             "Source Scan",
            "full_rescan":        "Full Re-Scan",
            "scheduled_lookup":   "Scheduled Source Scan",
            "single_author":      "Author Scan",
            "single_author_full": "Author Full Re-Scan",
            "bulk_authors":       "Bulk Author Scan",
            "bulk_books":         "Bulk Book Scan",
        }.get(scan_type, "Source Scan")
    if kind == "mam":
        return {
            "manual":    "MAM Scan",
            "scheduled": "Scheduled MAM Scan",
            "full_scan": "MAM Full Scan",
        }.get(scan_type, "MAM Scan")
    if kind == "library":
        # When a non-Calibre backend lands, the projection passes
        # `display_name` through here as `scan_type`, so the label
        # reads "Audiobookshelf Sync" naturally instead of always
        # saying "Calibre Sync".
        return f"{scan_type} Sync" if scan_type and scan_type != "none" else "Library Sync"
    if kind == "hygiene":
        return "Data Hygiene"
    return scan_type or kind


def _stamp_completed(p: dict) -> float | None:
    """Lazily stamp completed_at when a scan first appears as not-running.

    Returns the timestamp if the scan has finished, None if still running
    or never ran. The stamp is written back into the progress dict so
    subsequent calls return the same value.
    """
    running = bool(p.get("running"))
    status = p.get("status", "idle")
    if running:
        p.pop("completed_at", None)
        return None
    if status in ("idle", "none"):
        return None
    if "completed_at" not in p:
        p["completed_at"] = time.time()
    return p["completed_at"]


def _project_lookup() -> dict:
    """Project _lookup_progress into the unified shape."""
    p = state._lookup_progress
    return {
        "kind": "lookup",
        "type": p.get("type", "none"),
        "label": _label_for("lookup", p.get("type", "none")),
        "running": bool(p.get("running")),
        "current": p.get("checked", 0),
        "total": p.get("total", 0),
        "current_label": p.get("current_author", "") or None,
        # In-flight book title the source scan is currently fetching.
        # Goodreads/Kobo/Hardcover write to this via the `_on_book`
        # closure that lookup.py stashes on each source instance, and
        # only for work that actually does something — DETAIL fetches
        # and URL-backfill matches. Filter-noise skips don't reach
        # this field, so the user-visible feed never flickers through
        # foreign-language / set-collection / contributor-only noise.
        "current_book": p.get("current_book", "") or None,
        "status": p.get("status", "idle"),
        "completed_at": _stamp_completed(p),
        "extra": {
            "new_books": p.get("new_books", 0),
            # Per-source author-timeout counts from the most recent bulk
            # scan. Dashboard surfaces this so a primary source (Goodreads)
            # silently under-scanning a batch of authors doesn't get lost
            # in the logs. Empty dict when nothing timed out.
            "source_timeouts": p.get("source_timeouts") or {},
        },
    }


def _project_mam() -> dict:
    """Project _mam_scan_progress into the unified shape."""
    p = state._mam_scan_progress
    return {
        "kind": "mam",
        "type": p.get("type", "none"),
        "label": _label_for("mam", p.get("type", "none")),
        "running": bool(p.get("running")),
        "current": p.get("scanned", 0),
        "total": p.get("total", 0),
        "current_label": None,
        # In-flight book MAM is currently checking. Unlike source scans,
        # MAM shows EVERY attempt — there's no filter-noise to hide here.
        "current_book": p.get("current_book", "") or None,
        "status": p.get("status", "idle"),
        "completed_at": _stamp_completed(p),
        "extra": {
            "found":     p.get("found", 0),
            "possible":  p.get("possible", 0),
            "not_found": p.get("not_found", 0),
            "errors":    p.get("errors", 0),
            "remaining": p.get("remaining"),
        },
    }


def _project_hygiene() -> dict:
    """Project _hygiene_progress into the unified scan-status shape.

    The Hygiene chain runs 6 sub-jobs; we expose its overall "job X
    of 6" framing as `current` / `total` for the banner's main
    progress bar, and stash per-job rolling stats in `extra.jobs`
    so the dashboard can render a one-line "this run merged 12
    books, +83 IDs" digest while running and after completion.
    """
    p = state._hygiene_progress
    return {
        "kind": "hygiene",
        "type": p.get("type", "none"),
        "label": _label_for("hygiene", p.get("type", "none")),
        "running": bool(p.get("running")),
        "current": int(p.get("current_job_idx", 0))
                   + (1 if p.get("running") else 0),
        "total": int(p.get("total_jobs", 0)),
        "current_label": p.get("current_job_name", "") or None,
        "current_book": p.get("current_library", "") or None,
        "status": p.get("status", "idle"),
        "completed_at": _stamp_completed(p),
        "extra": {
            "jobs": p.get("jobs", []),
            "phase_current": int(p.get("current", 0)),
            "phase_total": int(p.get("total", 0)),
        },
    }


def _project_libraries() -> list[dict]:
    """Project per-library sync progress into the unified shape.

    Emits one entry per discovered library so the Command Center can
    render a dedicated row for Calibre and Audiobookshelf simultaneously,
    each with its own "(Last Sync: …)" stamp and in-flight progress
    bar. Syncs are still serialized through `_library_sync_in_progress`;
    the per-slug keying exists so a just-completed Calibre sync doesn't
    get erased when ABS starts, and vice versa.

    Libraries that have never been synced still get an idle entry (via
    `get_lib_progress` lazily seeding) so every row renders at startup
    instead of appearing only after the first sync completes.
    """
    out: list[dict] = []
    for lib in state._discovered_libraries:
        slug = lib["slug"]
        p = state.get_lib_progress(slug)
        display_name = lib.get("display_name") or lib.get("name") or "Library"
        out.append({
            "kind": "library",
            "slug": slug,
            "app_type": lib.get("app_type", ""),
            "content_type": lib.get("content_type", "ebook"),
            "type": p.get("type", "none"),
            "label": _label_for("library", display_name),
            "running": bool(p.get("running")),
            "current": p.get("current", 0),
            "total": p.get("total", 0),
            "current_label": None,
            "current_book": p.get("current_book", "") or None,
            "status": p.get("status", "idle"),
            "completed_at": _stamp_completed(p),
            # Distinct from completed_at: bumps on every scheduler
            # tick whether or not real work happened. Lets the
            # Command Center show "Scheduler alive — last checked X
            # min ago" without claiming a sync just finished.
            "last_check_at": p.get("last_check_at"),
            "extra": {
                "books_new": p.get("books_new", 0),
                "books_updated": p.get("books_updated", 0),
                "books_pruned": p.get("books_pruned", 0),
            },
        })
    return out


@router.get("/scan-status")
async def scan_status():
    """Unified scan progress for the Dashboard widget.

    Returns every tracked scan in a uniform shape regardless of whether
    it's an author lookup, full re-scan, MAM scan, scheduled job, or a
    single-author trigger from the Author page. The frontend renders
    one row per scan with running > complete > idle ordering. A scan
    in 'idle' state with type='none' is filtered out so the widget
    auto-hides when nothing has run yet.
    """
    out = []
    for proj in (_project_lookup(), _project_mam(), _project_hygiene(), *_project_libraries()):
        # Hide entries that are pristine idle (never ran). Keep complete
        # ones so the user sees the result of the last scan even after
        # it finishes. Library rows are always kept so the Command
        # Center shows every library (Calibre, ABS) as a dedicated row
        # even before the first scheduled sync tick fires.
        if proj.get("kind") != "library" and proj["status"] == "idle" and proj["type"] == "none":
            continue
        out.append(proj)
    # `startup_complete` gates the frontend's progress banner. Flips
    # True once the supervised startup-sync task finishes its first
    # pass through every library (regardless of per-library outcome).
    # Stays False on libraries-less setups too — the lifespan never
    # spawns the task, so a freshly-installed Seshat shows the
    # SetupWizard rather than this splash anyway.
    #
    # `first_boot` is the splash-mode gate: True only when NO library
    # has a non-zero `last_full_sync_ts` (i.e., we genuinely have no
    # prior sync data). Persisted in `library_sync_state` so it
    # survives container restarts — without this gate, the very first
    # restart on a Seshat upgrade would erroneously trigger the
    # full-screen splash because `_library_sync_progress` is in-memory
    # state that resets at boot.
    sync_state_dict = (load_settings().get("library_sync_state") or {})
    first_boot = not any(
        (entry or {}).get("last_full_sync_ts") or 0
        for entry in sync_state_dict.values()
    )
    return {
        "scans": out,
        "startup_complete": bool(state._startup_sync_complete),
        "first_boot": first_boot,
    }


@router.post("/sync/full-rescan")
async def trigger_full_rescan():
    s = load_settings()
    if not s.get("author_scanning_enabled", True):
        return {"error": "Author scanning is disabled — enable it in Settings"}
    if state._lookup_task and not state._lookup_task.done():
        return {"error": "An author scan is already running"}
    state._lookup_progress = {"running": True, "checked": 0, "total": 0, "current_author": "",
                        "current_book": "",
                        "new_books": 0, "status": "scanning", "type": "full_rescan"}
    def _progress(data):
        state._lookup_progress.update({"checked": data["checked"], "total": data["total"],
                                 "current_author": data["current_author"], "new_books": data["new_books"]})
    async def _do():
        try:
            result = await run_full_rescan(on_progress=_progress)
            state._lookup_progress.update({
                "running": False, "status": "complete",
                "source_timeouts": result.get("source_timeouts") or {},
            })
            try:
                from app.discovery.notify import notify_scan_complete
                await notify_scan_complete(
                    label="Full Re-Scan",
                    new_books=int(state._lookup_progress.get("new_books", 0)),
                    authors_total=int(state._lookup_progress.get("total", 0) or 1),
                )
            except Exception:
                logger.debug("full-rescan notify failed", exc_info=True)
        except Exception as e:
            logger.error(f"Full re-scan error: {e}")
            state._lookup_progress.update({"running": False, "status": f"error: {e}"})
    state._lookup_task = asyncio.create_task(_do())
    return {"status": "started"}


# ─── Scanning Toggles ────────────────────────────────────────
@router.post("/scanning/author/toggle")
async def toggle_author_scanning():
    """Toggle author scanning on/off. Cancels running scan when disabled."""
    s = load_settings()
    new_val = not s.get("author_scanning_enabled", True)
    s["author_scanning_enabled"] = new_val
    save_settings(s)
    if not new_val and state._lookup_task and not state._lookup_task.done():
        state._lookup_task.cancel()
        state._lookup_progress.update({"running": False, "status": "cancelled"})
        logger.info("Author scanning disabled — cancelled running scan")
    return {"enabled": new_val}


@router.post("/scanning/mam/toggle")
async def toggle_mam_scanning():
    """Toggle MAM scanning on/off without affecting MAM feature visibility."""
    s = load_settings()
    new_val = not s.get("mam_scanning_enabled", True)
    s["mam_scanning_enabled"] = new_val
    save_settings(s)
    if not new_val:
        if state._mam_scan_task and not state._mam_scan_task.done():
            state._mam_scan_task.cancel()
            state._mam_scan_progress.update({"running": False, "status": "cancelled"})
        if state._mam_full_scan_task and not state._mam_full_scan_task.done():
            state._mam_full_scan_task.cancel()
            state._mam_scan_progress.update({"running": False, "status": "cancelled"})
        logger.info("MAM scanning disabled — cancelled running scans")
    return {"enabled": new_val}
