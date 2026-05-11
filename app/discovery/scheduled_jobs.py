"""
Scheduled jobs for the discovery domain.

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
    _NEEDS_SCAN_BASIC_BARE,
    _resolve_mam_languages,
    scan_books_batch as mam_scan_batch,
    validate_connection as mam_validate,
)
from app.library_apps import get_app

logger = logging.getLogger("seshat.discovery.scheduled")


async def sync_all_libraries() -> None:
    """Interval job: sync each discovered library with mtime skip."""
    from app.discovery import sync_state as _sync_state
    current_active = get_active_library()
    st = load_settings()
    if _sync_state.migrate_settings(st):
        save_settings(st)
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
                # Drift slack: each tick stamps `last_at = time.time()`
                # AFTER processing (which can take seconds to minutes
                # when a real sync runs), so the next tick at the same
                # APScheduler cadence sees slightly less than
                # `effective_interval` elapsed and skips. Without the
                # slack, ABS / Calibre with a per-library interval
                # matching the APScheduler interval ends up checking
                # every OTHER tick — Mark's 130-vs-131 case
                # (2026-05-11): 118.7m elapsed vs 120m configured
                # caused a 4-hour gap between ABS change-detection
                # checks. 5 minutes absorbs typical processing drift
                # without weakening the gate for genuinely longer
                # per-library overrides.
                if last_at and elapsed_min < effective_interval - 5:
                    logger.debug(
                        "Scheduled sync: '%s' interval not elapsed "
                        "(%.1fm < %dm - 5m slack), skipping tick",
                        lib["name"], elapsed_min, effective_interval,
                    )
                    continue
                set_active_library(slug)
                lib_app = get_app(app_type)
                # Pull current mtime via the app so API-based sources
                # (ABS `lastUpdate`) route through the same change-detection
                # path that Calibre's file mtime uses.
                current_mtime = (
                    await lib_app.get_mtime(lib)
                    if lib_app
                    else os.path.getmtime(lib["source_db_path"])
                )
                last_mtime = _sync_state.get_state(st, slug)["last_mtime"]
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
                _sync_state.record_completion(
                    st, slug,
                    mtime=current_mtime,
                    mode=(sync_result or {}).get("mode", "full"),
                )
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
                try:
                    _sync_state.record_failure(st, lib["slug"])
                    save_settings(st)
                except Exception:
                    logger.debug("record_failure crashed", exc_info=True)
        set_active_library(current_active)
        state._last_library_sync_check["at"] = time.time()
        state._last_library_sync_check["synced"] = any_synced
        # Stamp `last_check_at` on every tick (including skips) so the
        # Command Center can show "Scheduler is alive — last checked X
        # min ago". Crucially, do NOT bump `completed_at` here — that
        # field is reserved for actual sync completions so the "Last
        # Sync: …" indicator stays accurate. Previously this code
        # overwrote `completed_at = time.time()` on every skip, which
        # produced a "Last Sync 3 min ago" display when the last real
        # sync was actually 40 hours ago — exactly the confusion Mark
        # hit 2026-05-11 chasing the missing 131st audiobook.
        if not any_synced:
            now_ts = time.time()
            for lib in state._discovered_libraries:
                p = state.get_lib_progress(lib["slug"])
                p.update({
                    "running": False,
                    "status": "complete",
                    "type": "scheduled_skip",
                    "current": 0,
                    "total": 0,
                    "current_book": "",
                    "last_check_at": now_ts,
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

        # Multi-library: tally remaining books per library, scan each.
        # Without this, the tick only operated on the active library
        # and the user had to flip active to ABS between ticks to get
        # audiobook coverage (Mark added 66 audiobooks, never saw any
        # MAM coverage on them until manually scanning the ABS lib).
        per_lib_remaining: dict[str, int] = {}
        libs_to_scan: list[dict] = []
        for lib in state._discovered_libraries:
            slug = lib["slug"]
            ldb = await get_db(slug=slug)
            try:
                # Auto-release: a book stamped is_unreleased=1 with an
                # expected_date that has now arrived stops looking like
                # "Upcoming" and starts looking like a normal Missing book
                # eligible for MAM scanning. Without this flip, books would
                # sit in the Upcoming bucket forever (or until a source scan
                # rewrote the row), invisible to the MAM scanner. Runs in
                # the same per-library loop as the count below so freshly
                # released books are picked up the same tick they age in.
                # `localtime` so a book whose expected_date is "today" in
                # Mark's timezone flips at local-midnight rather than
                # waiting up to 24h for UTC to catch up.
                await ldb.execute(
                    "UPDATE books SET is_unreleased=0 "
                    "WHERE is_unreleased=1 "
                    "AND expected_date IS NOT NULL "
                    "AND substr(expected_date, 1, 10) <= date('now', 'localtime')"
                )
                await ldb.commit()
                rem_row = await (await ldb.execute(
                    f"SELECT COUNT(*) FROM books WHERE {_NEEDS_SCAN_BASIC_BARE}"
                )).fetchone()
                count = rem_row[0] if rem_row else 0
            finally:
                await ldb.close()
            per_lib_remaining[slug] = count
            if count > 0:
                libs_to_scan.append(lib)

        total_remaining = sum(per_lib_remaining.values())
        if total_remaining == 0:
            logger.info(
                "MAM scheduled scan: no books need scanning across any library"
            )
            last_scan_at = time.time()
            continue

        # 150 books/library/tick — fixed budget per library so a busy
        # library doesn't starve a quiet one.
        per_lib_limit = 150
        scan_total = sum(
            min(per_lib_limit, per_lib_remaining[lib["slug"]])
            for lib in libs_to_scan
        )
        logger.info(
            f"MAM scheduled scan starting: {scan_total} books across "
            f"{len(libs_to_scan)} libraries "
            f"({total_remaining} total remaining)"
        )

        # Reset cancel flag so a stale cancel from a prior tick doesn't
        # preempt this one. Cancel flow: /mam/scan/cancel flips this to
        # True; the closure below surfaces it to mam_scan_batch as its
        # cancel_check and aborts at the next per-book boundary.
        state._scheduled_mam_cancel_requested = False
        state._mam_scan_progress = {
            "running": True, "scanned": 0, "total": scan_total,
            "found": 0, "possible": 0, "not_found": 0,
            "errors": 0, "current_book": "",
            "current_library": "",
            "status": "scanning", "type": "scheduled",
            "remaining": total_remaining,
        }

        def _sched_cancel_check() -> bool:
            return state._scheduled_mam_cancel_requested

        agg = {"scanned": 0, "found": 0, "possible": 0,
               "not_found": 0, "errors": 0}
        last_error: str | None = None
        from app.discovery.routers.mam import _active_content_type
        for lib in libs_to_scan:
            if state._scheduled_mam_cancel_requested:
                break
            slug = lib["slug"]
            lib_name = lib.get("display_name") or lib.get("name") or slug
            state._mam_scan_progress["current_library"] = lib_name
            ct = lib.get("content_type", "ebook")
            lib_limit = min(per_lib_limit, per_lib_remaining[slug])

            # Closure captures the per-library baseline so progress
            # accumulates across libraries instead of resetting.
            base_scanned = agg["scanned"]
            base_found = agg["found"]
            base_possible = agg["possible"]
            base_not_found = agg["not_found"]
            base_errors = agg["errors"]

            def _sched_progress(stats: dict) -> None:
                state._mam_scan_progress.update({
                    "scanned": base_scanned + stats["scanned"],
                    "found": base_found + stats["found"],
                    "possible": base_possible + stats["possible"],
                    "not_found": base_not_found + stats["not_found"],
                    "errors": base_errors + stats["errors"],
                    "current_book": stats.get("current_book", ""),
                })

            ldb = await get_db(slug=slug)
            try:
                logger.info(
                    f"MAM scheduled scan: '{lib_name}' "
                    f"({ct}, {lib_limit} books)"
                )
                result = await mam_scan_batch(
                    ldb, session_id=mam_token, limit=lib_limit,
                    delay=s.get("rate_mam", 2), skip_ip_update=True,
                    format_priority=s.get(
                        "audiobook_format_priority"
                        if ct == "audiobook"
                        else "mam_format_priority"
                    ),
                    on_progress=_sched_progress,
                    cancel_check=_sched_cancel_check,
                    lang_ids=_resolve_mam_languages(
                        s.get("languages", ["English"])
                    ),
                    content_type=ct,
                )
                agg["scanned"] += result.get("scanned", 0)
                agg["found"] += result.get("found", 0)
                agg["possible"] += result.get("possible", 0)
                agg["not_found"] += result.get("not_found", 0)
                agg["errors"] += result.get("errors", 0)
                err = result.get("error")
                await ldb.execute(
                    "INSERT INTO sync_log "
                    "(sync_type, started_at, finished_at, status, "
                    "books_found, books_new) VALUES (?,?,?,?,?,?)",
                    (
                        "mam", time.time(), time.time(),
                        "cancelled" if state._scheduled_mam_cancel_requested
                        else "complete" if not err
                        else "error",
                        result.get("scanned", 0), result.get("found", 0),
                    ),
                )
                await ldb.commit()
                if err:
                    last_error = f"{lib_name}: {err}"
                    logger.error(f"MAM scheduled scan '{lib_name}' error: {err}")
                    # Continue to other libraries — one failure
                    # shouldn't stall the rest. (IP-registration
                    # failures DO bail out at the source layer below
                    # because every library would hit the same wall.)
                    if "IP registration failed" in str(err):
                        break
            except Exception as e:
                last_error = f"{lib_name}: {e}"
                logger.error(
                    f"MAM scheduled scan '{lib_name}' exception: {e}",
                    exc_info=True,
                )
                agg["errors"] += 1
            finally:
                await ldb.close()

        was_cancelled = state._scheduled_mam_cancel_requested
        state._mam_scan_progress.update({
            "running": False,
            "current_library": "",
            "status": (
                "cancelled" if was_cancelled
                else "complete" if last_error is None
                else f"error: {last_error}"
            ),
        })
        if was_cancelled:
            logger.info("MAM scheduled scan cancelled by user")
        logger.info(
            f"MAM scheduled scan done: {agg['scanned']} scanned, "
            f"{agg['found']} found across {len(libs_to_scan)} libraries"
        )
        # Skip the ntfy "scan complete" when the user cancelled —
        # they already know, and a false "done!" push would be noise.
        if last_error is None and not was_cancelled:
            try:
                await notify_mam_scan_complete(
                    scanned=agg["scanned"], found=agg["found"],
                    possible=agg["possible"], not_found=agg["not_found"],
                )
            except Exception:
                logger.debug(
                    "MAM scheduled scan notify failed", exc_info=True
                )
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
