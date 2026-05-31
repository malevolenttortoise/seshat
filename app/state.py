"""
Shared mutable state for Seshat's background tasks.

Module-level singletons that the lifespan startup, the routers, and the
background workers all read and mutate. Because Python modules are
singletons within a process, every importer sees the same values.

IMPORTANT — module attribute access:
    Always use `state.foo`, not `from app.state import foo`. Direct
    imports create a local binding that won't see updates from other
    modules. For REASSIGNMENT, you MUST use the module attribute form
    (`state.foo = new_value`) — bare assignment inside a function
    rebinds a local variable instead of mutating the shared state.
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

_log = logging.getLogger("seshat")


def supervised_task(
    coro_factory: Callable[[], Awaitable[None]],
    *,
    name: str,
    restart_on_crash: bool = True,
    restart_delay: float = 5.0,
) -> asyncio.Task:
    """Wrap a long-running background coroutine with exception logging.

    The problem it solves: `asyncio.create_task(some_coro())` silently
    loses exceptions unless the task is awaited. For fire-and-forget
    workers (the IRC listener,
    the qBit poller, the snatch-budget watcher) a crash would otherwise
    show up as a one-line "Task exception was never retrieved" at
    interpreter shutdown — no traceback, no restart, no visible failure.

    `coro_factory` is a zero-arg callable that RETURNS a fresh coroutine
    on each call (not a coroutine object), because restarting the task
    requires building a new one — coroutines can only be awaited once.

    Cancellation propagates: if the caller cancels the returned task,
    CancelledError bubbles out without being logged or restarted.
    """
    async def _runner():
        while True:
            try:
                await coro_factory()
                _log.info(f"supervised task {name!r} completed normally")
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception(f"supervised task {name!r} crashed")
                if not restart_on_crash:
                    return
                _log.warning(
                    f"supervised task {name!r} restarting in {restart_delay}s"
                )
                try:
                    await asyncio.sleep(restart_delay)
                except asyncio.CancelledError:
                    raise

    return asyncio.create_task(_runner(), name=name)


# ─── IRC listener state ──────────────────────────────────────
# `_irc_task` is the supervised wrapper around the IrcClient's
# run_forever loop; `irc_client` is the IrcClient instance itself,
# kept reachable so the lifespan shutdown can call stop() on it
# before cancelling the task wrapper.
_irc_task: Optional[asyncio.Task] = None
irc_client: Optional[Any] = None
_irc_status: Dict[str, Any] = {
    "connected": False,
    "last_connect_at": None,
    "last_disconnect_at": None,
    "last_error": "",
    "announces_seen": 0,
    "announces_allowed": 0,
    "announces_skipped": 0,
}


# ─── Budget watcher state ───────────────────────────────────
_budget_watcher_task: Optional[asyncio.Task] = None


# ─── Metadata cache worker state (v2.21.0 Phase D) ───────────
# Background task that drains the Amazon metadata cache queue
# (`metadata_cache_amazon_queue`) by scanning Amazon Author Store
# pages at humanized cadence and writing the results into
# `metadata_cache_amazon_state` + `metadata_cache_amazon_books`. See
# `app/discovery/metadata_cache_worker.py:run_loop`. Disabled by
# default — operator enables via the `metadata_cache.amazon.enabled`
# setting once UAT confirms the worker behaves on their network.
_metadata_cache_amazon_worker_task: Optional[asyncio.Task] = None

# v3.4.0 slice 03 — Goodreads list-page cache worker. Drains
# `metadata_cache_goodreads_queue` (ADR-0018) at humanized cadence
# and writes per-page snapshots into `metadata_cache_goodreads_list_pages`.
# Lighter than the Amazon worker — no curl_cffi session, no warmup,
# no escalation tiers (GR has no Akamai-class IP-level cooldown).
_metadata_cache_goodreads_worker_task: Optional[asyncio.Task] = None


# ─── Cookie keep-alive state ────────────────────────────────
# Long-running background loop that hits MAM's search endpoint on a
# fixed interval to ensure the cookie auto-rotation has SOMETHING to
# fire on, even during long quiet periods. Without this, 15+ days
# of Seshat inactivity would silently expire the session cookie.
_cookie_keepalive_task: Optional[asyncio.Task] = None


# ─── Cookie retry state ────────────────────────────────────
# Periodically re-attempts grabs stuck in failed_cookie_expired.
_cookie_retry_task: Optional[asyncio.Task] = None


# ─── Review-queue auto-add timeout state ────────────────────
# Daily tick that promotes undecided review-queue items past
# their grace period to the sink with bare metadata.
_review_timeout_task: Optional[asyncio.Task] = None


# ─── APScheduler ────────────────────────────────────────────
# AsyncIOScheduler instance running the daily + weekly digest jobs
# plus the discovery-domain library-sync and scheduled-lookup interval
# jobs. Set by main.py's lifespan and torn down during shutdown.
scheduler: Optional[Any] = None


# ─── Discovery scheduler task handles ───────────────────────
# Supervised-task wrappers around the two discovery-side loops that
# aren't APScheduler jobs: the MAM batch-scan scheduler (needs to
# re-read its interval setting on every 60s tick) and the discovery
# digest flush loop (fires at calendar times, cancelled on shutdown
# to trigger a final queue drain).
_mam_scheduler_task: Optional[asyncio.Task] = None
_digest_scheduler_task: Optional[asyncio.Task] = None


# ─── qBit poller state ──────────────────────────────────────
_qbit_poll_task: Optional[asyncio.Task] = None
_qbit_status: Dict[str, Any] = {
    "reachable": False,
    "last_poll_at": None,
    "last_error": "",
    "active_torrents": 0,
}


# ─── Snatch budget state (read-only mirror for the dashboard) ─
# Authoritative numbers live in the snatch_ledger table; this dict is
# the cached/derived snapshot the UI polls.
_snatch_budget: Dict[str, Any] = {
    "used": 0,
    "cap": 0,
    "queued": 0,
    "next_release_at": None,
    "last_updated_at": None,
    # Number of qBit torrents in the watched category that Seshat
    # did NOT submit (manual adds, Autobrr, etc.). Refreshed by the
    # budget watcher each tick. The dispatcher adds this to the
    # ledger count to avoid over-committing the MAM snatch cap.
    "qbit_extras": 0,
}


# ─── Migration job state ────────────────────────────────────
# Background migration runs server-side so the user can navigate
# away from the page. The task processes all selected hashes in
# batches, updating _migration_status as it goes. The frontend
# polls GET /api/v1/migration/status to track progress.
_migration_task: Optional[asyncio.Task] = None
_migration_status: Dict[str, Any] = {
    "running": False,
    "done": 0,
    "total": 0,
    "succeeded": 0,
    "failed": 0,
    "results": [],       # list of {hash, name, ok, error, action}
    "finished": False,
    "dry_run": False,
}


# ─── Discovery domain state ─────────────────────────────────

# Library discovery cache — populated in lifespan startup.
_discovered_libraries: list[dict] = []


def get_active_library_content_type() -> str:
    """Return the content_type of the currently active library.

    Defaults to "ebook" when no library is active (pre-setup) or the
    active slug can't be found in the discovered list. Callers use this
    to pick content-appropriate metadata sources — see
    `app.discovery.lookup._sources_for_content_type`.
    """
    from app.discovery.database import get_active_library
    slug = get_active_library()
    if not slug:
        return "ebook"
    for lib in _discovered_libraries:
        if lib.get("slug") == slug:
            return lib.get("content_type") or "ebook"
    return "ebook"

# Updated after every successful library sync.
_last_library_sync_check: Dict[str, Any] = {"at": None, "synced": False}

# Per-library last-sync timestamps. Used by the scheduler to gate
# individual libraries on their own configured interval — e.g.
# abs_sync_interval_minutes=180 while library_sync_interval_minutes=60
# means the scheduler fires every hour but only actually syncs the
# ABS library on every third tick. Keys are library slugs, values
# are `time.time()` timestamps of the last successful sync (or
# mtime-skip — either counts as "checked").
_library_last_sync_at: Dict[str, float] = {}

# True while any library sync is running. Pipeline tasks check this
# flag before grabbing the DB write lock so they yield cleanly.
_library_sync_in_progress: bool = False

# ─── Startup sync task ──────────────────────────────────────
# The lifespan-time per-library sync loop. Lives in a supervised
# task instead of blocking the lifespan so FastAPI starts accepting
# requests immediately on container boot, even when Calibre/ABS sync
# takes minutes. `_startup_sync_complete` flips True after the first
# pass through every library finishes (success OR all-failed) so the
# frontend can hide its "first-boot splash" once the library is
# usable.
_startup_sync_task: Optional[asyncio.Task] = None
_startup_sync_complete: bool = False

# Per-library sync progress keyed by library slug. Each entry mirrors
# the old single-dict shape (running/current/total/current_book/status
# /type/books_new/books_updated/completed_at). Keyed by slug so the
# Command Center can show Calibre AND Audiobookshelf rows side-by-side
# each with their own in-flight stats and last-sync timestamp. Syncs
# are still serialized through `_library_sync_in_progress`; the keying
# exists to preserve per-library history across alternating syncs.
_library_sync_progress: Dict[str, Dict[str, Any]] = {}

_IDLE_LIB_PROGRESS: Dict[str, Any] = {
    "running": False,
    "current": 0,
    "total": 0,
    "current_book": "",
    "books_new": 0,
    "books_updated": 0,
    "status": "idle",
    "type": "none",
}


def get_lib_progress(slug: str) -> Dict[str, Any]:
    """Return the per-slug progress dict, lazily creating an idle one.

    Readers and writers both use this helper so the keying invariant
    lives in one place. Returns the live dict (not a copy) so callers
    can mutate fields directly without another lookup.
    """
    if slug not in _library_sync_progress:
        _library_sync_progress[slug] = dict(_IDLE_LIB_PROGRESS)
    return _library_sync_progress[slug]

# Author lookup scan state.
_lookup_task: Optional[asyncio.Task] = None
_lookup_progress: Dict[str, Any] = {
    "running": False,
    "checked": 0,
    "total": 0,
    "current_author": "",
    "current_book": "",
    "new_books": 0,
    "status": "idle",
    "type": "none",
}

# Source-scan pressure counter — MAM yields while this is > 0.
_source_scan_refs: int = 0

# Cancel flag for scheduled MAM scans (discovery-side).
_scheduled_mam_cancel_requested: bool = False

# Discovery-side MAM scan state (searching for missing books).
_mam_scan_task: Optional[asyncio.Task] = None
_mam_scan_progress: Dict[str, Any] = {
    "running": False,
    "scanned": 0,
    "total": 0,
    "found": 0,
    "possible": 0,
    "not_found": 0,
    "errors": 0,
    "current_book": "",
    "status": "idle",
    "type": "none",
}
_mam_full_scan_task: Optional[asyncio.Task] = None

# v2.16.0 Data Hygiene action state. One coordinator runs at most one
# Hygiene chain at a time; the same dict is mutated as each of the 6
# sub-jobs progresses, with `extra.jobs` carrying per-job rolling
# stats so the Command Center banner can render both the overall
# 1-of-6 sub-step and the in-flight job's own count.
_hygiene_task: Optional[asyncio.Task] = None
_hygiene_progress: Dict[str, Any] = {
    "running": False,
    "current_job_idx": 0,
    "total_jobs": 0,
    "current_job_name": "",
    "current_library": "",
    "current": 0,   # in-flight job's progress counter
    "total": 0,     # in-flight job's progress total
    "status": "idle",
    "type": "none",
    "jobs": [],      # appended per-completion: {name, library, stats}
}


# ─── Dispatcher singleton ────────────────────────────────────
# Set by main.py's lifespan during startup. The inject router and
# the IRC listener both read this attribute, so swapping in a test
# dispatcher is just `state.dispatcher = test_dispatcher` — no
# monkey-patching, no DI framework.
dispatcher: Optional[Any] = None


async def refresh_filter_authors() -> None:
    """Rebuild `state.dispatcher.filter_config`'s allow/ignore sets
    from the current DB state.

    Called from every site that mutates `authors_allowed` /
    `authors_ignored` (authors router endpoints, tentative approve,
    auto-train, weekly digest promotions). Cheap: two SELECT
    queries + a dataclasses.replace on the filter_config.

    No-op when the dispatcher hasn't been built yet (startup order:
    the authors tables can be mutated by migration code before
    main.py wires up the dispatcher).
    """
    if dispatcher is None:
        return
    import dataclasses
    from app.database import get_db
    from app.storage.authors import load_normalized_sets

    db = await get_db()
    try:
        allowed, ignored = await load_normalized_sets(db)
    finally:
        await db.close()

    dispatcher.filter_config = dataclasses.replace(
        dispatcher.filter_config,
        allowed_authors=allowed,
        ignored_authors=ignored,
    )
    _log.debug(
        f"refresh_filter_authors: allowed={len(allowed)} ignored={len(ignored)}"
    )
