"""
Scheduled jobs for the discovery domain.

Ported from AthenaScout v1.2.0's main.py lifespan:

  - `sync_all_libraries` — APScheduler interval job. For each discovered
    library, skip the sync if metadata.db mtime is unchanged, otherwise
    call the backend's sync() method. On skipped ticks, bumps the
    library-sync progress dict's completed_at stamp so the Command
    Center "(Last Sync: …)" display advances instead of freezing on
    the last actual sync.
  - `scheduled_lookup` — APScheduler interval job. Runs the full
    source-lookup scan on the `lookup_interval_days` cadence.
  - `mam_scheduler_loop` — Long-running supervised task. Ticks every
    60 seconds and fires a bounded MAM scan batch when the configured
    interval elapses. Defers while a library sync is running.

`add_discovery_jobs(scheduler, settings)` registers the two interval
jobs onto a caller-supplied AsyncIOScheduler, mirroring the pattern in
`orchestrator.scheduler.register_digest_jobs`.

The MAM scheduler is a supervised_task rather than an APScheduler job
because it needs to poll its own interval setting on every tick (so the
user can change `mam_scan_interval_minutes` in Settings without a
restart) and because its "defer while library sync running" check is
easier to express as an in-loop condition than as trigger logic.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import state
from app.config import SYNC_INTERVAL_MINUTES, load_settings, save_settings
from app.discovery.calibre_sync import sync_calibre
from app.discovery.database import (
    get_active_library,
    get_db,
    set_active_library,
)
from app.discovery.lookup import run_full_lookup
from app.discovery.notify import (
    notify_library_sync,
    notify_mam_scan_complete,
    notify_scan_complete,
)
from app.discovery.sources.mam import (
    _resolve_mam_languages,
    scan_books_batch as mam_scan_batch,
    validate_connection as mam_validate,
)
from app.library_apps import get_app

logger = logging.getLogger("seshat.discovery.scheduled")


async def sync_all_libraries() -> None:
    """Interval job: sync each discovered library with mtime skip."""
    current_active = get_active_library()
    st = load_settings()
    mtimes = st.get("library_mtimes", {})
    any_synced = False
    # Per-library interval config. The APScheduler job fires every
    # library_sync_interval_minutes (the minimum cadence), but each
    # library can opt into a less-frequent cadence via its own
    # setting. Right now only ABS gets this override; new app_types
    # can be added here when they want their own cadence knob.
    default_interval = int(st.get("library_sync_interval_minutes", 60) or 60)
    abs_interval_override = int(st.get("abs_sync_interval_minutes", 0) or 0)
    now = time.time()
    # Signal background writers (MAM scanner, etc.) that a bulk sync
    # is in flight so they yield. try/finally ensures the flag clears
    # even if a sync crashes mid-run.
    state._library_sync_in_progress = True
    try:
        for lib in state._discovered_libraries:
            try:
                slug = lib["slug"]
                app_type = lib.get("app_type", "calibre")
                # Resolve effective interval for this library.
                if app_type == "audiobookshelf" and abs_interval_override > 0:
                    effective_interval = abs_interval_override
                else:
                    effective_interval = default_interval
                last_at = state._library_last_sync_at.get(slug, 0.0)
                elapsed_min = (now - last_at) / 60 if last_at else float("inf")
                if last_at and elapsed_min < effective_interval:
                    logger.debug(
                        "Scheduled sync: '%s' interval not elapsed "
                        "(%.1fm < %dm), skipping tick",
                        lib["name"], elapsed_min, effective_interval,
                    )
                    continue
                set_active_library(slug)
                lib_app = get_app(app_type)
                # Pull current mtime via the app so API-based sources
                # (ABS `lastUpdate`) route through the same change-detection
                # path that Calibre's file mtime uses.
                current_mtime = (
                    lib_app.get_mtime(lib)
                    if lib_app
                    else os.path.getmtime(lib["source_db_path"])
                )
                last_mtime = mtimes.get(lib["slug"])
                if last_mtime is not None and current_mtime == last_mtime:
                    logger.debug(
                        f"Scheduled sync: '{lib['name']}' source unchanged, skipping"
                    )
                    # Count the mtime-unchanged skip as a "checked"
                    # tick so the per-library interval gate doesn't
                    # re-check on every scheduler fire. Without this,
                    # a library whose mtime never changes would be
                    # re-checked every default_interval minutes
                    # regardless of its own override.
                    state._library_last_sync_at[slug] = time.time()
                    continue
                logger.info(
                    f"Scheduled sync: '{lib['name']}' "
                    f"{lib_app.display_name if lib_app else 'database'} changed, syncing..."
                )
                if lib_app:
                    sync_result = await lib_app.sync(lib)
                else:
                    sync_result = await sync_calibre(
                        lib["source_db_path"], lib["library_path"]
                    )
                mtimes[lib["slug"]] = current_mtime
                st["library_mtimes"] = mtimes
                save_settings(st)
                state._library_last_sync_at[slug] = time.time()
                any_synced = True
                try:
                    await notify_library_sync(
                        lib.get("display_name") or lib.get("name") or "Library",
                        int((sync_result or {}).get("books_new", 0)),
                        int((sync_result or {}).get("books_updated", 0)),
                    )
                except Exception:
                    logger.debug("library-sync notify failed", exc_info=True)
            except Exception as e:
                logger.warning(f"Scheduled sync failed for '{lib['name']}': {e}")
        set_active_library(current_active)
        state._last_library_sync_check["at"] = time.time()
        state._last_library_sync_check["synced"] = any_synced
        # Bump the Command Center "(Last Sync: …)" timestamp on every
        # tick, including no-op skips. Without this, the displayed "ago"
        # value freezes at the last real sync and users correctly suspect
        # the scheduler stopped. sync_calibre already updates the progress
        # dict on actual syncs — we only need to handle the all-skipped
        # case here. Stamp every discovered library's per-slug dict so
        # both Calibre and ABS rows advance together on a no-op tick.
        if not any_synced:
            for lib in state._discovered_libraries:
                state.get_lib_progress(lib["slug"]).update({
                    "running": False,
                    "status": "complete",
                    "type": "scheduled_skip",
                    "current": 0,
                    "total": 0,
                    "current_book": "",
                    "completed_at": time.time(),
                })

        # Post-sync: refresh cross-library work links once the ebook
        # + audiobook libraries are both current. Only run when at
        # least one library actually synced — saves cross-library
        # reads on no-op ticks.
        if any_synced:
            try:
                from app.works.matcher import rebuild_matches
                result = await rebuild_matches()
                if result.links_added or result.orphans_pruned:
                    logger.info(
                        "works matcher post-sync: +%d links, "
                        "%d orphans pruned",
                        result.links_added, result.orphans_pruned,
                    )
            except Exception as e:
                logger.warning(f"works matcher post-sync failed: {e}")
    finally:
        state._library_sync_in_progress = False


async def scheduled_lookup() -> None:
    """Interval job: run a full source-lookup scan."""
    s = load_settings()
    if not s.get("author_scanning_enabled", True):
        return
    if state._lookup_progress.get("running"):
        return
    state._lookup_progress = {
        "running": True, "checked": 0, "total": 0,
        "current_author": "", "current_book": "",
        "new_books": 0, "status": "scanning", "type": "scheduled_lookup",
    }

    def _progress(data):
        state._lookup_progress.update({
            "checked": data["checked"], "total": data["total"],
            "current_author": data["current_author"],
            "new_books": data["new_books"],
        })

    try:
        result = await run_full_lookup(on_progress=_progress)
        state._lookup_progress.update({
            "running": False, "status": "complete",
            "source_timeouts": result.get("source_timeouts") or {},
        })
        try:
            await notify_scan_complete(
                label="Scheduled Source Scan",
                new_books=int(state._lookup_progress.get("new_books", 0)),
                authors_total=int(state._lookup_progress.get("total", 0) or 1),
            )
        except Exception:
            logger.debug("scheduled-lookup notify failed", exc_info=True)
    except Exception as e:
        logger.error(f"Scheduled lookup error: {e}")
        state._lookup_progress.update(
            {"running": False, "status": f"error: {e}"}
        )


async def mam_scheduler_loop() -> None:
    """Supervised task: fires a bounded MAM scan batch on a settings-driven cadence."""
    last_scan_at = 0.0
    while True:
        await asyncio.sleep(60)
        s = load_settings()
        interval = s.get("mam_scan_interval_minutes", 360)
        # Token resolution goes through the discovery router helper so
        # it reads from the encrypted store first, then settings fallback.
        from app.discovery.routers.mam import _get_mam_token
        mam_token = await _get_mam_token()
        if (
            interval <= 0
            or not s.get("mam_enabled")
            or not mam_token
            or not s.get("mam_scanning_enabled", True)
        ):
            continue
        elapsed_min = (time.time() - last_scan_at) / 60
        if elapsed_min < interval:
            continue
        if state._mam_scan_progress.get("running"):
            continue
        # Defer ONLY on a library sync — concurrent author scans are
        # tolerated because WAL + busy_timeout absorb the small per-row
        # contention. Library sync holds the write lock for tens of
        # seconds during bulk inserts, longer than busy_timeout will wait.
        if state._library_sync_in_progress:
            logger.debug("MAM scheduled scan deferred — library sync in progress")
            continue

        last_val = s.get("last_mam_validated_at") or 0
        if time.time() - last_val > 86400:
            logger.info("MAM daily validation check...")
            vr = await mam_validate(mam_token, True)
            if vr["success"]:
                s["last_mam_validated_at"] = time.time()
                s["mam_validation_ok"] = True
            else:
                s["mam_validation_ok"] = False
            save_settings(s)
            if not vr["success"]:
                logger.error(
                    f"MAM validation failed — skipping scan: {vr['message']}"
                )
                last_scan_at = time.time()
                continue

        db = await get_db()
        try:
            rem_row = await (await db.execute(
                "SELECT COUNT(*) FROM books WHERE mam_status IS NULL "
                "AND is_unreleased=0 AND hidden=0"
            )).fetchone()
            total_remaining = rem_row[0] if rem_row else 0
        finally:
            await db.close()
        if total_remaining == 0:
            logger.info("MAM scheduled scan: no books need scanning")
            last_scan_at = time.time()
            continue

        scan_limit = min(150, total_remaining)
        logger.info(
            f"MAM scheduled scan starting ({scan_limit} books, "
            f"{total_remaining} total remaining)"
        )
        # Reset cancel flag so a stale cancel from a prior tick doesn't
        # preempt this one. Cancel flow: /mam/scan/cancel flips this to
        # True; the closure below surfaces it to mam_scan_batch as its
        # cancel_check and aborts at the next per-book boundary.
        state._scheduled_mam_cancel_requested = False
        state._mam_scan_progress = {
            "running": True, "scanned": 0, "total": scan_limit,
            "found": 0, "possible": 0, "not_found": 0,
            "errors": 0, "current_book": "",
            "status": "scanning", "type": "scheduled",
            "remaining": total_remaining,
        }

        def _sched_progress(stats):
            state._mam_scan_progress.update({
                "scanned": stats["scanned"],
                "found": stats["found"],
                "possible": stats["possible"],
                "not_found": stats["not_found"],
                "errors": stats["errors"],
                "current_book": stats.get("current_book", ""),
            })

        def _sched_cancel_check():
            return state._scheduled_mam_cancel_requested

        db = await get_db()
        try:
            # Active-library content_type decides whether this tick
            # scans the ebook or audiobook side of MAM. Scheduled MAM
            # scans honor whatever library the user has selected as
            # active; multi-library users can get audiobook coverage
            # by switching active between ticks (or per manual scan).
            from app.discovery.routers.mam import _active_content_type
            _ct = _active_content_type()
            result = await mam_scan_batch(
                db, session_id=mam_token, limit=150,
                delay=s.get("rate_mam", 2), skip_ip_update=True,
                format_priority=s.get("audiobook_format_priority" if _ct == "audiobook" else "mam_format_priority"),
                on_progress=_sched_progress,
                cancel_check=_sched_cancel_check,
                lang_ids=_resolve_mam_languages(
                    s.get("languages", ["English"])
                ),
                content_type=_ct,
            )
            was_cancelled = state._scheduled_mam_cancel_requested
            state._mam_scan_progress.update({
                "running": False,
                "status": (
                    "cancelled" if was_cancelled
                    else "complete" if not result.get("error")
                    else f"error: {result.get('error')}"
                ),
            })
            if was_cancelled:
                logger.info("MAM scheduled scan cancelled by user")
            await db.execute(
                "INSERT INTO sync_log "
                "(sync_type, started_at, finished_at, status, "
                "books_found, books_new) VALUES (?,?,?,?,?,?)",
                (
                    "mam", time.time(), time.time(),
                    "cancelled" if was_cancelled
                    else "complete" if not result.get("error")
                    else "error",
                    result.get("scanned", 0), result.get("found", 0),
                ),
            )
            await db.commit()
            logger.info(
                f"MAM scheduled scan done: {result.get('scanned', 0)} "
                f"scanned, {result.get('found', 0)} found"
            )
            # Skip the ntfy "scan complete" when the user cancelled —
            # they already know, and a false "done!" push would be noise.
            if not result.get("error") and not was_cancelled:
                try:
                    await notify_mam_scan_complete(
                        scanned=int(state._mam_scan_progress.get("scanned", 0)),
                        found=int(state._mam_scan_progress.get("found", 0)),
                        possible=int(state._mam_scan_progress.get("possible", 0)),
                        not_found=int(state._mam_scan_progress.get("not_found", 0)),
                    )
                except Exception:
                    logger.debug(
                        "MAM scheduled scan notify failed", exc_info=True
                    )
        except Exception as e:
            logger.error(f"MAM scheduled scan error: {e}")
            state._mam_scan_progress.update(
                {"running": False, "status": f"error: {e}"}
            )
        finally:
            await db.close()
        last_scan_at = time.time()


def add_discovery_jobs(
    scheduler: AsyncIOScheduler, settings: dict
) -> None:
    """Register library-sync and scheduled-lookup interval jobs onto the scheduler.

    Both jobs gate internally on settings (enabled flags, running-state
    guards, author-scanning toggle) so the registrations themselves can
    be unconditional — we only skip when the interval is 0 or no
    libraries are configured.
    """
    sync_min = settings.get(
        "library_sync_interval_minutes", SYNC_INTERVAL_MINUTES
    )
    lookup_days = settings.get("lookup_interval_days", 3)

    if sync_min and sync_min > 0:
        if state._discovered_libraries:
            scheduler.add_job(
                sync_all_libraries, "interval", minutes=sync_min,
                id="library_sync", replace_existing=True,
                coalesce=True, max_instances=1,
            )
            logger.info(f"Library sync scheduled every {sync_min} minutes")
        else:
            logger.info("Library auto-sync skipped — no libraries configured")
    else:
        logger.info("Library auto-sync disabled (interval = 0)")

    if lookup_days and lookup_days > 0:
        scheduler.add_job(
            scheduled_lookup, "interval", minutes=lookup_days * 1440,
            id="author_lookup", replace_existing=True,
            coalesce=True, max_instances=1,
        )
        logger.info(f"Author lookup scheduled every {lookup_days} days")
    else:
        logger.info("Auto-lookup disabled (interval = 0)")
