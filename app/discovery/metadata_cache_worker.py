"""
v2.21.0 Phase D — metadata cache background worker.

Drains the per-source scan queue created by Phase B + populated by the
Phase B startup backfill (+ Phase C cache-miss enqueues). Each
iteration:

  1. Heartbeat — stamp `last_heartbeat_at` so a stalled worker shows up
     in the worker_state row.
  2. Settings gate — if `metadata_cache.<source>.enabled` is false,
     skip the tick. Default is OFF; the operator opts in once they've
     UAT'd the worker behavior.
  3. Cooldown gate — if `amazon_blocked_until` (set by
     `record_amazon_soft_block` on 429 / 202 / thin-body) is still in
     the future, skip the tick.
  4. Pop the highest-priority pending queue row whose
     `next_scan_due_at <= now`. Mark `status='in_progress'`.
  5. Build a fresh `curl_cffi` Chrome-120 session — per-author
     rotation is the Arm-3 result that broke Akamai's ceiling on
     2026-05-22.
  6. Behavioral warmup — a single GET to `amazon.com/` before the
     `/stores/author/{id}/allbooks` hit. Research-supported humanizer;
     ~200ms cost.
  7. Hand the session to a one-shot `AmazonSource` and run
     `get_author_books(author_id)` against the right format filter
     for the queue row's library content_type (ebook vs audiobook).
  8. On success: upsert state row + INSERT OR REPLACE books rows.
     Mark queue row `status='pending'` with a forward
     `next_scan_due_at` so the worker doesn't re-pop it tomorrow.
  9. On soft-block: leave the queue row `status='pending'` with
     `next_scan_due_at = cooldown_expiry` and apply escalation tier
     (600s → 1800s → 3600s within a 1h window).
 10. On hard error: increment `consecutive_failures`; after 5,
     mark `status='failed_permanent'` for triage.
 11. Sleep a random 30–90s before the next iteration (humanization).

Crash recovery on startup: any `status='in_progress'` rows from a
previous process get reset to `'pending'` so they re-pop on the next
tick. The worker that crashed mid-scan never wrote a state row, so
no data is lost.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiosqlite

from app import config as app_config
from app import state
from app.discovery import metadata_cache
from app.discovery.amazon_author_id_resolver import (
    amazon_block_remaining_s,
    is_amazon_blocked,
)
from app.discovery.sources.amazon_widget_parser import FILTER_TO_BINDING

logger = logging.getLogger("seshat.discovery.metadata_cache_worker")


# ─── Tuning constants ──────────────────────────────────────────


# Jitter range between worker iterations. 30-90s humanizes the
# request cadence against Akamai's density scoring. Tuned from the
# Arm-1/Arm-3 experiments (2026-05-22) — sustained sub-30s spacing
# trips long-window scoring even with per-session rotation.
_JITTER_MIN_S = 30.0
_JITTER_MAX_S = 90.0

# When the queue is empty or the worker is disabled, sleep longer so
# we don't spin a CPU loop checking for new work.
_IDLE_SLEEP_S = 60.0

# When the cooldown is engaged, sleep until it clears (capped so a
# parse error in cooldown math can't deadlock the worker).
_COOLDOWN_MAX_SLEEP_S = 3600.0

# After a successful scan, push the next re-scan out by this much so
# the worker walks the queue instead of obsessing over the most-
# recent author. ~7 days matches the plan's "normal cadence" tier.
_NORMAL_RESCAN_CADENCE_S = 7 * 24 * 3600.0

# Cooldown escalation tiers (seconds). Applied when the worker
# observes consecutive soft-blocks within a 1h window. Index 0 is
# the first block, index 1 the second, etc. Past the last index the
# top tier sticks.
_ESCALATION_TIERS_S: tuple[float, ...] = (600.0, 1800.0, 3600.0)
_ESCALATION_RESET_WINDOW_S = 3600.0  # 1h blockless → reset counter

# A queue row with this many consecutive failures becomes
# `failed_permanent` and surfaces in the Database Manager triage
# view for manual cleanup. Tunable later via settings.
_MAX_CONSECUTIVE_FAILURES = 5

# Warmup behavior (#2 from the plan). One GET to amazon.com/ on
# every fresh session before the first /stores/author hit.
_WARMUP_URL = "https://www.amazon.com/"


# ─── Tick result ───────────────────────────────────────────────


@dataclass(frozen=True)
class TickResult:
    """Outcome of one worker iteration. Mirrors the
    `app/orchestrator/budget_watcher.py:TickResult` pattern."""

    source_name: str
    outcome: str
    """One of:
      - "ok"             — scan succeeded, cache updated
      - "ok_empty"       — scan succeeded but returned no books
      - "cooldown"       — skipped, cooldown engaged
      - "queue_empty"    — no work to do this tick
      - "disabled"       — operator disabled the worker
      - "no_libraries"   — pre-setup state
      - "soft_block"     — scan tripped the cooldown (worker bumped its own queue row)
      - "permanent_fail" — author hit the consecutive-failure cap
      - "error"          — unexpected exception caught + logged
    """
    author_id: Optional[str] = None
    library_slug: Optional[str] = None
    books_cached: int = 0
    queue_size: int = 0
    cooldown_remaining_s: float = 0.0
    next_sleep_s: float = _IDLE_SLEEP_S
    error: Optional[str] = None


# ─── Settings + state accessors ────────────────────────────────


def is_worker_enabled(source_name: str) -> bool:
    """True iff `metadata_cache.<source>.enabled` is truthy.

    Reads settings on every call so the operator can flip the worker
    on / off from the UI without a container restart. Defaults to
    False — opt-in, so a brand-new deploy doesn't immediately start
    burning Amazon budget the moment v2.21.0 ships.
    """
    s = app_config.load_settings()
    mc = (s.get("metadata_cache") or {}).get(source_name) or {}
    return bool(mc.get("enabled", False))


def _library_content_type(library_slug: str) -> str:
    """Map a library slug to its `content_type` (`"ebook"` /
    `"audiobook"`). Reads from `state._discovered_libraries`; falls
    back to `"ebook"` for unknown slugs so the worker keeps moving
    instead of crashing on a stale queue row."""
    for lib in state._discovered_libraries:
        if lib.get("slug") == library_slug:
            return lib.get("content_type") or "ebook"
    return "ebook"


def _amazon_filters_for_content_type(content_type: str) -> tuple[str, str]:
    """Pick (format_filter, language) for an Amazon scan based on
    the library's content_type. Reads settings on every call so a
    panel change applies to the next worker iteration."""
    s = app_config.load_settings()
    amz = (s.get("metadata_sources") or {}).get("amazon") or {}
    language = amz.get("language") or "English"
    if content_type == "audiobook":
        fmt = amz.get("audiobook_format") or "audible_audiobook"
    else:
        fmt = amz.get("format") or "kindle"
    return fmt, language


# ─── Worker-state row helpers ──────────────────────────────────


async def _read_worker_state(
    db: aiosqlite.Connection, source_name: str,
) -> dict[str, Any]:
    """Read the singleton worker_state row. Always exists (Phase B's
    init seeds it with id=1 + default columns)."""
    table = metadata_cache.worker_state_table(source_name)
    cur = await db.execute(f"SELECT * FROM {table} WHERE id = 1")
    row = await cur.fetchone()
    if row is None:
        # Defensive — Phase B init seeds the row. Re-seed silently.
        await db.execute(f"INSERT OR IGNORE INTO {table} (id) VALUES (1)")
        await db.commit()
        return {
            "id": 1,
            "last_block_at": 0.0,
            "block_cooldown_s": 600.0,
            "consecutive_blocks": 0,
            "last_heartbeat_at": None,
            "last_scan_completed_at": None,
            "today_scan_count": 0,
            "today_block_count": 0,
        }
    return dict(row)


async def _stamp_heartbeat(
    db: aiosqlite.Connection, source_name: str, now: float,
) -> None:
    """Stamp `last_heartbeat_at` so a stalled worker is observable
    via `db_summary` / the worker_state row inspection in the DB
    manager UI."""
    table = metadata_cache.worker_state_table(source_name)
    await db.execute(
        f"UPDATE {table} SET last_heartbeat_at = ? WHERE id = 1",
        (now,),
    )
    await db.commit()


async def _record_block_in_worker_state(
    db: aiosqlite.Connection, source_name: str, now: float,
    *, cooldown_s: float,
) -> int:
    """Increment `consecutive_blocks` / `today_block_count`, stamp
    `last_block_at` + `block_cooldown_s`. Returns the new
    consecutive_blocks value so the caller can pick the right
    escalation tier."""
    table = metadata_cache.worker_state_table(source_name)
    cur = await db.execute(
        f"SELECT last_block_at, consecutive_blocks "
        f"FROM {table} WHERE id = 1"
    )
    row = await cur.fetchone()
    prior_last = float(row[0] or 0.0) if row else 0.0
    prior_count = int(row[1] or 0) if row else 0
    if (now - prior_last) > _ESCALATION_RESET_WINDOW_S:
        # 1h blockless — counter reset.
        new_count = 1
    else:
        new_count = prior_count + 1
    await db.execute(
        f"UPDATE {table} "
        f"SET last_block_at = ?, consecutive_blocks = ?, "
        f"    block_cooldown_s = ?, "
        f"    today_block_count = today_block_count + 1 "
        f"WHERE id = 1",
        (now, new_count, cooldown_s),
    )
    await db.commit()
    return new_count


async def _record_scan_completed(
    db: aiosqlite.Connection, source_name: str, now: float,
) -> None:
    """Bump `today_scan_count` + stamp `last_scan_completed_at`."""
    table = metadata_cache.worker_state_table(source_name)
    await db.execute(
        f"UPDATE {table} "
        f"SET last_scan_completed_at = ?, "
        f"    today_scan_count = today_scan_count + 1 "
        f"WHERE id = 1",
        (now,),
    )
    await db.commit()


def _pick_escalation_cooldown(consecutive_blocks: int) -> float:
    """Map the running consecutive_blocks count to a cooldown second
    count. Past the last tier the top value sticks."""
    if consecutive_blocks <= 0:
        return _ESCALATION_TIERS_S[0]
    idx = min(consecutive_blocks - 1, len(_ESCALATION_TIERS_S) - 1)
    return _ESCALATION_TIERS_S[idx]


# ─── Queue helpers ─────────────────────────────────────────────


async def _pop_next_queue_row(
    db: aiosqlite.Connection, source_name: str, now: float,
) -> Optional[dict[str, Any]]:
    """Atomically pop the highest-priority pending queue row whose
    `next_scan_due_at <= now` and mark it `status='in_progress'` +
    stamp `last_attempt_at`.

    Returns the popped row as a dict, or None if nothing is ready.
    aiosqlite doesn't expose UPDATE...RETURNING cleanly, so we do
    SELECT-then-UPDATE inside a transaction.
    """
    queue = metadata_cache.queue_table(source_name)
    await db.execute("BEGIN IMMEDIATE")
    try:
        cur = await db.execute(
            f"SELECT author_id, library_slug, seshat_author_id, "
            f"priority, status, next_scan_due_at, last_attempt_at, "
            f"consecutive_failures, enqueued_reason "
            f"FROM {queue} "
            f"WHERE status = 'pending' AND next_scan_due_at <= ? "
            f"ORDER BY priority DESC, next_scan_due_at ASC "
            f"LIMIT 1",
            (now,),
        )
        row = await cur.fetchone()
        if row is None:
            await db.execute("COMMIT")
            return None
        await db.execute(
            f"UPDATE {queue} "
            f"SET status = 'in_progress', last_attempt_at = ? "
            f"WHERE author_id = ? AND library_slug = ?",
            (now, row[0], row[1]),
        )
        await db.execute("COMMIT")
        return {
            "author_id": row[0],
            "library_slug": row[1],
            "seshat_author_id": row[2],
            "priority": row[3],
            "status": "in_progress",
            "next_scan_due_at": row[5],
            "last_attempt_at": now,
            "consecutive_failures": row[7],
            "enqueued_reason": row[8],
        }
    except Exception:
        await db.execute("ROLLBACK")
        raise


async def _mark_queue_row_pending(
    db: aiosqlite.Connection, source_name: str, *,
    author_id: str, library_slug: str,
    next_scan_due_at: float,
    reset_failures: bool = True,
) -> None:
    """Return a popped row to the pending state with a forward due-time.
    Used after a successful scan or a soft-block deferral."""
    queue = metadata_cache.queue_table(source_name)
    if reset_failures:
        await db.execute(
            f"UPDATE {queue} SET status = 'pending', "
            f"    next_scan_due_at = ?, consecutive_failures = 0 "
            f"WHERE author_id = ? AND library_slug = ?",
            (next_scan_due_at, author_id, library_slug),
        )
    else:
        await db.execute(
            f"UPDATE {queue} SET status = 'pending', "
            f"    next_scan_due_at = ? "
            f"WHERE author_id = ? AND library_slug = ?",
            (next_scan_due_at, author_id, library_slug),
        )
    await db.commit()


async def _mark_queue_row_failure(
    db: aiosqlite.Connection, source_name: str, *,
    author_id: str, library_slug: str,
    next_scan_due_at: float,
) -> tuple[int, bool]:
    """Increment `consecutive_failures`; flip to `failed_permanent`
    once the cap is hit. Returns (new_failure_count, became_permanent).
    """
    queue = metadata_cache.queue_table(source_name)
    cur = await db.execute(
        f"SELECT consecutive_failures FROM {queue} "
        f"WHERE author_id = ? AND library_slug = ?",
        (author_id, library_slug),
    )
    row = await cur.fetchone()
    prior = int(row[0] or 0) if row else 0
    new_count = prior + 1
    became_permanent = new_count >= _MAX_CONSECUTIVE_FAILURES
    if became_permanent:
        await db.execute(
            f"UPDATE {queue} "
            f"SET status = 'failed_permanent', "
            f"    consecutive_failures = ?, next_scan_due_at = ? "
            f"WHERE author_id = ? AND library_slug = ?",
            (new_count, next_scan_due_at, author_id, library_slug),
        )
    else:
        await db.execute(
            f"UPDATE {queue} "
            f"SET status = 'pending', "
            f"    consecutive_failures = ?, next_scan_due_at = ? "
            f"WHERE author_id = ? AND library_slug = ?",
            (new_count, next_scan_due_at, author_id, library_slug),
        )
    await db.commit()
    return new_count, became_permanent


async def _count_pending_queue_rows(
    db: aiosqlite.Connection, source_name: str,
) -> int:
    queue = metadata_cache.queue_table(source_name)
    cur = await db.execute(
        f"SELECT COUNT(*) FROM {queue} WHERE status = 'pending'"
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


# ─── Cache write ───────────────────────────────────────────────


async def _upsert_state_row(
    db: aiosqlite.Connection, source_name: str, *,
    author_id: str, library_slug: str, seshat_author_id: Optional[int],
    now: float, outcome: str, book_count: int, last_error: Optional[str] = None,
) -> None:
    """INSERT OR REPLACE the (author, library) state row. The schema's
    FK CASCADE from `<source>_books` → `<source>_state` would wipe
    the books table on a plain REPLACE, so we UPDATE-then-INSERT to
    preserve the FK relationship for the new book rows."""
    state_table = metadata_cache.state_table(source_name)
    cur = await db.execute(
        f"SELECT 1 FROM {state_table} "
        f"WHERE author_id = ? AND library_slug = ?",
        (author_id, library_slug),
    )
    exists = (await cur.fetchone()) is not None
    if exists:
        await db.execute(
            f"UPDATE {state_table} SET "
            f"  seshat_author_id = COALESCE(?, seshat_author_id), "
            f"  last_scanned_at = ?, last_outcome = ?, "
            f"  last_error = ?, book_count = ? "
            f"WHERE author_id = ? AND library_slug = ?",
            (
                seshat_author_id, now, outcome, last_error, book_count,
                author_id, library_slug,
            ),
        )
    else:
        await db.execute(
            f"INSERT INTO {state_table} "
            f"(author_id, library_slug, seshat_author_id, "
            f" last_scanned_at, last_outcome, last_error, book_count) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                author_id, library_slug, seshat_author_id, now, outcome,
                last_error, book_count,
            ),
        )
    await db.commit()


def _flatten_author_result_to_book_rows(
    author_result: Any, *,
    author_id: str, library_slug: str, now: float,
    format_filter: str, language: str,
) -> list[tuple]:
    """Walk an AmazonSource AuthorResult into cache-row tuples.

    We use the scan's input filters as the stored `format` /
    `language` because Amazon's `authorFilters` already constrained
    the scan server-side — every returned book is guaranteed to be
    in that format + language. Stamping these on the cache row gives
    the read-time filter (`metadata_cache_reader._format_matches`)
    something concrete to match against, including for the
    FILTER_TO_BINDING translation it does for the user-facing input.

    Dedupes by `book_asin` because AmazonSource occasionally returns
    the same ASIN twice in one scan — mediaMatrix variants reusing a
    canonical ASIN, or pagination handing back a product that was
    already detail-fetched on page 1. Without the dedupe the
    subsequent `INSERT INTO {books}` trips the
    `(author_id, library_slug, book_asin)` UNIQUE constraint and the
    whole tick crashes (UAT 2026-05-22: two scans crashed at 17:49
    and 18:25 from this exact path, B000AP9Y66 and B001H6GPWS, both
    leaving the queue row stuck in `in_progress`). First occurrence
    wins; the duplicate would have the same canonical fields anyway.
    """
    binding_format = FILTER_TO_BINDING.get(format_filter, format_filter)
    rows: list[tuple] = []
    seen_asins: set[str] = set()
    duplicates_dropped = 0
    all_books: list[Any] = list(author_result.books or [])
    for series in author_result.series or []:
        all_books.extend(series.books or [])
    for book in all_books:
        if not book.external_id:
            continue
        if book.external_id in seen_asins:
            duplicates_dropped += 1
            continue
        seen_asins.add(book.external_id)
        rows.append((
            author_id, library_slug, book.external_id,
            book.title or "",
            book.series_name, book.series_index,
            book.pub_date,
            binding_format,
            book.language or language,
            book.isbn, book.cover_url,
            None,  # raw_json — reserved for richer future shapes
            now,
        ))
    if duplicates_dropped:
        logger.debug(
            "metadata_cache_worker: dropped %d duplicate ASIN(s) "
            "from %s/%s scan (mediaMatrix overlap / pagination)",
            duplicates_dropped, author_id, library_slug,
        )
    return rows


async def _replace_book_rows(
    db: aiosqlite.Connection, source_name: str, *,
    author_id: str, library_slug: str,
    rows: list[tuple],
) -> None:
    """Replace the books for one (author, library) atomically — DELETE
    everything we previously had, INSERT the fresh scan's rows. This
    keeps the cache identical to what the latest scan saw (deletions
    on Amazon's side propagate). Worth it because cache rows are
    cheap and a stale row outliving its Amazon page would confuse
    the reader."""
    books_table = metadata_cache.books_table(source_name)
    await db.execute(
        f"DELETE FROM {books_table} "
        f"WHERE author_id = ? AND library_slug = ?",
        (author_id, library_slug),
    )
    if rows:
        await db.executemany(
            f"INSERT INTO {books_table} "
            f"(author_id, library_slug, book_asin, title, series_name, "
            f" series_pos, pub_date, format, language, isbn, cover_url, "
            f" raw_json, cached_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    await db.commit()


# ─── Session + scan ────────────────────────────────────────────


def _create_session() -> Optional[Any]:
    """Build a fresh curl_cffi Chrome-120 session. Returns None when
    curl_cffi isn't installed; caller logs + reports an error tick.
    Single chrome120 profile per Arm-3 — profile rotation was strictly
    worse than session rotation in our 2026-05-22 experiments."""
    try:
        from curl_cffi.requests import AsyncSession
        return AsyncSession(impersonate="chrome120", timeout=30.0)
    except ImportError:
        logger.warning(
            "metadata_cache_worker: curl_cffi not installed — cannot "
            "scan amazon.com. Worker will short-circuit every tick "
            "until the package is added."
        )
        return None


async def _run_warmup(session: Any) -> None:
    """One GET to amazon.com/ on a fresh session before the first
    /stores/author hit. Research-supported humanizer; failures are
    non-fatal — we proceed even if the warmup didn't complete."""
    try:
        resp = await session.get(_WARMUP_URL, timeout=15.0)
        status = getattr(resp, "status_code", None)
        logger.debug(
            "metadata_cache_worker: warmup %s → %s",
            _WARMUP_URL, status,
        )
    except Exception as exc:
        logger.debug(
            "metadata_cache_worker: warmup failed (%s) — continuing",
            exc,
        )


async def _perform_amazon_scan(
    author_id: str, library_slug: str, session: Any,
) -> tuple[Any, Optional[str]]:
    """Build a one-shot AmazonSource bound to `session`, run
    `get_author_books(author_id)`. Returns (author_result, error_message).

    Returns (None, error_msg) on any of:
      - curl_cffi missing (caller already handled, but defensive)
      - source raised
      - source returned None (transport or soft-block)
    """
    from app.discovery.sources.amazon import AmazonSource
    content_type = _library_content_type(library_slug)
    fmt, language = _amazon_filters_for_content_type(content_type)
    source = AmazonSource(
        rate_limit=0.0,           # worker controls cadence via jitter
        format_filter=fmt,
        audiobook_format_filter=fmt if content_type == "audiobook" else (
            (app_config.load_settings().get("metadata_sources") or {})
            .get("amazon", {}).get("audiobook_format") or "audible_audiobook"
        ),
        language=language,
        burst_delay_s=0.0,        # no extra in-scan delays
    )
    source._session = session
    source._session_init_attempted = True
    source._content_type = content_type
    try:
        result = await source.get_author_books(author_id)
    except Exception as exc:
        logger.exception(
            "metadata_cache_worker: scan raised for %s/%s: %s",
            author_id, library_slug, exc,
        )
        return None, f"{type(exc).__name__}: {exc}"
    if result is None:
        return None, "source returned None (transport / soft-block)"
    return result, None


# ─── Crash recovery ────────────────────────────────────────────


async def recover_stuck_in_progress(source_name: str) -> int:
    """Reset any `status='in_progress'` queue rows back to `'pending'`.

    Called once at startup. Covers the case where a previous container
    crashed mid-scan, leaving the row locked. Worker writes the state
    row only at scan-completion, so a crash mid-scan loses no data —
    the row just needs to be made re-poppable.
    """
    queue = metadata_cache.queue_table(source_name)
    db = await metadata_cache.get_db(source_name)
    try:
        cur = await db.execute(
            f"UPDATE {queue} SET status = 'pending' "
            f"WHERE status = 'in_progress'"
        )
        n = cur.rowcount
        await db.commit()
        if n:
            logger.info(
                "metadata_cache_worker: recovered %d stuck in-progress "
                "row(s) for source=%s",
                n, source_name,
            )
        return n
    finally:
        await db.close()


# ─── Tick ──────────────────────────────────────────────────────


async def tick(source_name: str = metadata_cache.SOURCE_AMAZON) -> TickResult:
    """One worker iteration. Never raises — every error path returns
    a TickResult with `outcome=...` so `run_loop` can decide the next
    sleep without a try/except wrap on every call site."""
    now = time.time()

    # Heartbeat fires on EVERY tick, before any short-circuit gate.
    # This lets the operator distinguish "worker disabled" from
    # "worker crashed / never started" by checking
    # `last_heartbeat_at` on the worker_state row — a recent
    # heartbeat with `today_scan_count == 0` means "alive but
    # disabled / cooled down / queue empty", whereas a stale
    # heartbeat means the supervised task died.
    try:
        hb_db = await metadata_cache.get_db(source_name)
        try:
            await _stamp_heartbeat(hb_db, source_name, now)
        finally:
            await hb_db.close()
    except Exception:
        # A heartbeat failure shouldn't block the rest of the tick;
        # log + continue so the worker still tries to do work.
        logger.exception(
            "metadata_cache_worker: heartbeat stamp failed (non-fatal)"
        )

    if not state._discovered_libraries:
        return TickResult(
            source_name=source_name, outcome="no_libraries",
            next_sleep_s=_IDLE_SLEEP_S,
        )

    if not is_worker_enabled(source_name):
        return TickResult(
            source_name=source_name, outcome="disabled",
            next_sleep_s=_IDLE_SLEEP_S,
        )

    # Cooldown gate — shared with the resolver / live AmazonSource. If
    # `record_amazon_soft_block` was triggered by anything in this
    # process (or persisted across restart in v2.20.3), we honor it.
    if is_amazon_blocked():
        remaining = amazon_block_remaining_s()
        sleep_s = min(remaining + 1.0, _COOLDOWN_MAX_SLEEP_S)
        return TickResult(
            source_name=source_name, outcome="cooldown",
            cooldown_remaining_s=remaining, next_sleep_s=sleep_s,
        )

    db = await metadata_cache.get_db(source_name)
    try:
        queue_row = await _pop_next_queue_row(db, source_name, now)
        if queue_row is None:
            return TickResult(
                source_name=source_name, outcome="queue_empty",
                queue_size=0, next_sleep_s=_IDLE_SLEEP_S,
            )
        queue_size = await _count_pending_queue_rows(db, source_name)
    finally:
        await db.close()

    author_id = queue_row["author_id"]
    library_slug = queue_row["library_slug"]
    seshat_author_id = queue_row.get("seshat_author_id")

    session = _create_session()
    if session is None:
        # curl_cffi missing — defer the queue row + report error.
        db = await metadata_cache.get_db(source_name)
        try:
            # Put the row back so a later install of curl_cffi can pop it.
            await _mark_queue_row_pending(
                db, source_name,
                author_id=author_id, library_slug=library_slug,
                next_scan_due_at=now + _COOLDOWN_MAX_SLEEP_S,
                reset_failures=False,
            )
        finally:
            await db.close()
        return TickResult(
            source_name=source_name, outcome="error",
            author_id=author_id, library_slug=library_slug,
            queue_size=queue_size,
            error="curl_cffi not installed",
            next_sleep_s=_IDLE_SLEEP_S,
        )

    try:
        await _run_warmup(session)
        result, scan_error = await _perform_amazon_scan(
            author_id, library_slug, session,
        )
    finally:
        try:
            await session.close()
        except Exception:
            pass

    # The scan may have tripped the IP-level penalty box via
    # `record_amazon_soft_block`; treat that as a soft-block outcome.
    if is_amazon_blocked():
        cooldown_s = amazon_block_remaining_s()
        db = await metadata_cache.get_db(source_name)
        try:
            consecutive = await _record_block_in_worker_state(
                db, source_name, now, cooldown_s=cooldown_s,
            )
            # Escalate the cooldown on the 2nd / 3rd block-in-window.
            # The base cooldown comes from record_amazon_soft_block;
            # we extend it past that floor for subsequent blocks.
            escalated_s = _pick_escalation_cooldown(consecutive)
            if escalated_s > cooldown_s:
                # Re-stamp the penalty box with the longer cooldown.
                from app.discovery.amazon_author_id_resolver import (
                    record_amazon_soft_block,
                )
                record_amazon_soft_block(
                    f"worker escalation (block #{consecutive} within "
                    f"{_ESCALATION_RESET_WINDOW_S:.0f}s)",
                    retry_after_s=escalated_s,
                )
                cooldown_s = escalated_s
            # Defer the queue row to after the cooldown clears, but
            # don't penalize it with a failure (the block isn't this
            # author's fault).
            await _mark_queue_row_pending(
                db, source_name,
                author_id=author_id, library_slug=library_slug,
                next_scan_due_at=now + cooldown_s + 60.0,
                reset_failures=True,
            )
        finally:
            await db.close()
        logger.info(
            "metadata_cache_worker: soft-block on %s/%s — "
            "consecutive=%d, cooldown=%.0fs",
            author_id, library_slug, consecutive, cooldown_s,
        )
        return TickResult(
            source_name=source_name, outcome="soft_block",
            author_id=author_id, library_slug=library_slug,
            queue_size=queue_size, cooldown_remaining_s=cooldown_s,
            next_sleep_s=min(cooldown_s + 1.0, _COOLDOWN_MAX_SLEEP_S),
        )

    if result is None:
        # Non-soft-block failure (transport error, parse error, …).
        db = await metadata_cache.get_db(source_name)
        try:
            new_count, became_permanent = await _mark_queue_row_failure(
                db, source_name,
                author_id=author_id, library_slug=library_slug,
                next_scan_due_at=now + _NORMAL_RESCAN_CADENCE_S,
            )
            # Still record a state row so the reader knows the worker
            # tried; the cache reader can keep returning None until a
            # later successful scan replaces this.
            await _upsert_state_row(
                db, source_name,
                author_id=author_id, library_slug=library_slug,
                seshat_author_id=seshat_author_id,
                now=now, outcome="error",
                book_count=0, last_error=(scan_error or "unknown"),
            )
        finally:
            await db.close()
        outcome = "permanent_fail" if became_permanent else "error"
        logger.warning(
            "metadata_cache_worker: scan FAILED for %s/%s (%s) "
            "consecutive_failures=%d%s",
            author_id, library_slug, scan_error, new_count,
            " — flipped to failed_permanent" if became_permanent else "",
        )
        return TickResult(
            source_name=source_name, outcome=outcome,
            author_id=author_id, library_slug=library_slug,
            queue_size=queue_size, error=scan_error,
            next_sleep_s=random.uniform(_JITTER_MIN_S, _JITTER_MAX_S),
        )

    # Successful scan: flatten to cache rows, write, ack the queue.
    content_type = _library_content_type(library_slug)
    fmt, language = _amazon_filters_for_content_type(content_type)
    cache_rows = _flatten_author_result_to_book_rows(
        result, author_id=author_id, library_slug=library_slug,
        now=now, format_filter=fmt, language=language,
    )

    # v2.21.0 Phase D hotfix — wrap the cache-write block in a
    # try/except so a write-time failure (UNIQUE constraint
    # violations, disk-full, schema drift) doesn't leave the queue
    # row stuck at `status='in_progress'` until the next container
    # restart. Without this guard the run_loop's outer catch-and-
    # continue swallows the exception but the row stays locked,
    # eventually blocking re-scans of that author.
    try:
        db = await metadata_cache.get_db(source_name)
        try:
            await _upsert_state_row(
                db, source_name,
                author_id=author_id, library_slug=library_slug,
                seshat_author_id=seshat_author_id,
                now=now, outcome="ok", book_count=len(cache_rows),
            )
            await _replace_book_rows(
                db, source_name,
                author_id=author_id, library_slug=library_slug,
                rows=cache_rows,
            )
            await _mark_queue_row_pending(
                db, source_name,
                author_id=author_id, library_slug=library_slug,
                next_scan_due_at=now + _NORMAL_RESCAN_CADENCE_S,
                reset_failures=True,
            )
            await _record_scan_completed(db, source_name, now)
        finally:
            await db.close()
    except Exception as exc:
        logger.exception(
            "metadata_cache_worker: cache-write failed for %s/%s "
            "(%s) — resetting queue row to pending so the next "
            "iteration can retry",
            author_id, library_slug, exc,
        )
        # Best-effort recovery: put the queue row back to pending +
        # bump consecutive_failures so a recurring write failure
        # eventually flips to failed_permanent instead of looping
        # forever. Use a fresh connection — the previous one may
        # be in a bad state.
        try:
            db = await metadata_cache.get_db(source_name)
            try:
                await _mark_queue_row_failure(
                    db, source_name,
                    author_id=author_id, library_slug=library_slug,
                    next_scan_due_at=now + _NORMAL_RESCAN_CADENCE_S,
                )
            finally:
                await db.close()
        except Exception:
            logger.exception(
                "metadata_cache_worker: queue-row recovery also "
                "failed — row will be cleaned up by the startup "
                "recover_stuck_in_progress on the next restart"
            )
        return TickResult(
            source_name=source_name, outcome="error",
            author_id=author_id, library_slug=library_slug,
            queue_size=queue_size,
            error=f"cache write failed: {type(exc).__name__}: {exc}",
            next_sleep_s=random.uniform(_JITTER_MIN_S, _JITTER_MAX_S),
        )

    logger.info(
        "metadata_cache_worker: scanned %s/%s — %d books cached",
        author_id, library_slug, len(cache_rows),
    )
    outcome = "ok" if cache_rows else "ok_empty"
    return TickResult(
        source_name=source_name, outcome=outcome,
        author_id=author_id, library_slug=library_slug,
        books_cached=len(cache_rows), queue_size=queue_size,
        next_sleep_s=random.uniform(_JITTER_MIN_S, _JITTER_MAX_S),
    )


# ─── Run loop ──────────────────────────────────────────────────


async def run_loop(
    *,
    source_name: str = metadata_cache.SOURCE_AMAZON,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Long-running loop that drives `tick()`. Wrapped in
    `app.state.supervised_task` by the main lifespan.

    `stop_event` is opt-in early-exit for tests. Production uses the
    surrounding asyncio.Task cancellation instead — `tick()` is short-
    lived (one HTTP burst), so cancellation lands cleanly on the next
    iteration boundary.
    """
    logger.info(
        "metadata_cache_worker: started for source=%s "
        "(jitter %.0f-%.0fs)",
        source_name, _JITTER_MIN_S, _JITTER_MAX_S,
    )

    # Crash recovery: reset any in_progress rows from a previous
    # crashed process so the queue is fully poppable on first iteration.
    try:
        await recover_stuck_in_progress(source_name)
    except Exception:
        logger.exception(
            "metadata_cache_worker: stuck-row recovery failed "
            "(continuing — worker can still operate, "
            "but stuck rows will block their PK)"
        )

    while True:
        try:
            result = await tick(source_name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "metadata_cache_worker: tick crashed unexpectedly"
            )
            # Belt-and-suspenders: if tick() raised before reaching
            # its own internal queue-row-recovery (the post-scan
            # cache-write try/except), any popped row would stay
            # locked at `status='in_progress'` until the next
            # process restart. Re-run the recovery sweep so future
            # iterations can re-pop those rows.
            try:
                await recover_stuck_in_progress(source_name)
            except Exception:
                logger.exception(
                    "metadata_cache_worker: post-crash stuck-row "
                    "recovery also failed — rows will be cleaned "
                    "up on the next container restart"
                )
            result = TickResult(
                source_name=source_name, outcome="error",
                next_sleep_s=_IDLE_SLEEP_S, error="tick exception",
            )

        if stop_event is not None and stop_event.is_set():
            logger.info(
                "metadata_cache_worker: stop_event signaled, exiting"
            )
            return

        try:
            if stop_event is not None:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=result.next_sleep_s,
                )
                logger.info(
                    "metadata_cache_worker: stop_event during sleep, exiting"
                )
                return
            else:
                await asyncio.sleep(result.next_sleep_s)
        except asyncio.TimeoutError:
            continue
