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
from datetime import datetime
from typing import Any, Optional

import aiosqlite

from app import config as app_config
from app import state
from app.discovery import metadata_cache
from app.discovery.amazon_author_id_resolver import (
    amazon_block_remaining_s,
    is_amazon_blocked,
)
logger = logging.getLogger("seshat.discovery.metadata_cache_worker")

# Tracks whether the optional rotated file handler has already been
# attached to `logger`, so a hot-reload of settings doesn't stack
# multiple handlers. Lifespan calls `install_log_file_handler` at
# startup; the function is idempotent.
_log_file_handler: Optional[logging.Handler] = None


def install_log_file_handler() -> Optional[str]:
    """Attach a `RotatingFileHandler` to the worker logger when the
    `metadata_cache_log_file_enabled` setting is truthy.

    Returns the resolved log-file path on success, None when disabled
    or when the path is not writeable. Safe to call multiple times —
    the second call detaches the prior handler before attaching the
    new one, so a settings change during runtime is honored on the
    next call (typically: next container start).
    """
    global _log_file_handler
    s = app_config.load_settings()
    enabled = bool(s.get("metadata_cache_log_file_enabled", False))

    # Detach any existing handler if either the toggle just went off
    # or we're about to attach a fresh one with new parameters.
    if _log_file_handler is not None:
        logger.removeHandler(_log_file_handler)
        try:
            _log_file_handler.close()
        except Exception:
            pass
        _log_file_handler = None

    if not enabled:
        return None

    try:
        from logging.handlers import RotatingFileHandler
        log_dir = app_config.DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "metadata_cache_worker.log"
        max_bytes = int(s.get("metadata_cache_log_file_max_bytes", 1_000_000))
        backup_count = int(s.get("metadata_cache_log_file_backup_count", 3))
        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        logger.addHandler(handler)
        _log_file_handler = handler
        return str(log_path)
    except Exception:
        logger.exception(
            "metadata_cache_worker: failed to install rotated log file "
            "(non-fatal — INFO/WARN/ERROR still emit to the container log)"
        )
        return None


# ─── Structured logging helpers (v2.21.0 Phase G) ──────────────


def _scan_logger(source_name: str) -> logging.Logger:
    """Per-source child logger (`...metadata_cache_worker.<source>`)
    so a tail/grep can scope to one source without picking up every
    metadata-cache message in the container log."""
    return logger.getChild(source_name)


def _format_fields(**fields: Any) -> str:
    """Render ``key=value`` pairs into a single grep-friendly string.

    ``None`` values are omitted so optional fields don't add visual
    noise. Strings with spaces are single-quoted; everything else
    renders bare so numbers and short identifiers stay scannable.
    """
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, str) and (" " in value or "=" in value):
            parts.append(f"{key}='{value}'")
        elif isinstance(value, float):
            parts.append(f"{key}={value:.0f}" if value.is_integer()
                         else f"{key}={value:.1f}")
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)


# ─── ntfy emit helper (v2.21.0 Phase G — migrated to bus in v2.28.0) ────


_EVENT_KEY_TO_BUS_EVENT: dict[str, str] = {}


def _load_event_key_mapping() -> dict[str, str]:
    """Lazy-import the bus event constants. The notifications package
    pulls in app.config; importing it at module load on a fresh
    worker process can race the settings file path setup, so defer."""
    global _EVENT_KEY_TO_BUS_EVENT
    if _EVENT_KEY_TO_BUS_EVENT:
        return _EVENT_KEY_TO_BUS_EVENT
    from app.notifications import events
    _EVENT_KEY_TO_BUS_EVENT = {
        "metadata_cache_daily_summary": events.SOURCE_METADATA_CACHE_DAILY_SUMMARY,
        "metadata_cache_error": events.SOURCE_METADATA_CACHE_ERROR,
        "metadata_cache_warning": events.SOURCE_METADATA_CACHE_WARNING,
        "metadata_cache_new_book": events.SOURCE_METADATA_CACHE_NEW_BOOK,
    }
    return _EVENT_KEY_TO_BUS_EVENT


async def _send_ntfy(
    *,
    event_key: str,
    title: str,
    message: str,
    priority: int = 3,
    tags: Optional[list[str]] = None,
) -> bool:
    """Fire a metadata-cache ntfy through the notification bus.

    The bus handles the enabled gate (legacy + new shape), topic
    routing, quiet-hours suppression, and never raises. ``priority``
    and ``tags`` overrides at each call site flow through verbatim
    so the metadata-cache worker can keep its established defaults
    per event type."""
    bus_event = _load_event_key_mapping().get(event_key)
    if bus_event is None:
        logger.warning(
            "metadata_cache_worker: unknown event_key=%r — skipping ntfy",
            event_key,
        )
        return False
    try:
        from app.notifications import bus
        return await bus.emit(
            bus_event,
            title=title, message=message,
            priority=priority, tags=tags,
        )
    except Exception:
        logger.exception(
            "metadata_cache_worker: bus.emit failed for event=%s "
            "(non-fatal)", event_key,
        )
        return False


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
      - "ok"               — scan succeeded, cache updated
      - "ok_empty"         — scan succeeded but returned no books
      - "cooldown"         — skipped, cooldown engaged
      - "queue_empty"      — no work to do this tick
      - "disabled"         — operator set mode=disabled
      - "outside_schedule" — mode=scheduled but the current time is
                             outside the configured active-hours
                             window. `next_sleep_s` is set to
                             seconds-until-window-open so the loop
                             quietly waits through the off-hours.
      - "no_libraries"     — pre-setup state
      - "soft_block"       — scan tripped the cooldown (worker bumped
                             its own queue row)
      - "permanent_fail"   — author hit the consecutive-failure cap
      - "error"            — unexpected exception caught + logged
    """
    author_id: Optional[str] = None
    library_slug: Optional[str] = None
    books_cached: int = 0
    queue_size: int = 0
    cooldown_remaining_s: float = 0.0
    next_sleep_s: float = _IDLE_SLEEP_S
    error: Optional[str] = None
    new_books: int = 0
    """Count of book_asins this tick saw that weren't in the cache
    before. Always 0 for first-scan authors (no prior baseline) so a
    backfill doesn't get treated as N new discoveries."""
    elapsed_ms: float = 0.0
    """Wall-clock time spent in scan + cache write for this tick.
    Used both in the `[scan]` log line and in the daily-summary
    aggregator. Zero for short-circuit outcomes (disabled / cooldown
    / queue_empty)."""


# ─── Settings + state accessors ────────────────────────────────


def _source_settings(source_name: str) -> dict[str, Any]:
    """Return the nested settings sub-dict for `metadata_cache.<source>.*`.
    Always a dict — empty when the user has never opened the panel."""
    s = app_config.load_settings()
    mc = (s.get("metadata_cache") or {}).get(source_name) or {}
    return mc


def get_worker_mode(source_name: str) -> str:
    """Return one of `"continuous"` / `"scheduled"` / `"disabled"`.

    Backwards compat: when `mode` is unset, derive from the legacy
    `enabled` boolean — True → `"continuous"`, False → `"disabled"`.
    A user who never touches the Phase I UI keeps the v2.21.0
    Phases A-G semantics.
    """
    mc = _source_settings(source_name)
    mode = mc.get("mode")
    if mode in ("continuous", "scheduled", "disabled"):
        return mode
    return "continuous" if mc.get("enabled", False) else "disabled"


def is_worker_enabled(source_name: str) -> bool:
    """True iff the worker should attempt scans at all.

    Now mode-aware (Phase I): `disabled` returns False. Both
    `continuous` and `scheduled` return True — the scheduled-window
    check is a separate gate (`is_inside_schedule_window`) so the
    tick can return a distinct `outside_schedule` outcome instead of
    masquerading as `disabled`.

    Reads settings on every call so the operator can flip the mode
    from the UI without a container restart.
    """
    return get_worker_mode(source_name) != "disabled"


def _parse_active_hours(spec: str) -> Optional[tuple[int, int, int, int]]:
    """Parse `"HH:MM-HH:MM"` into `(start_h, start_m, end_h, end_m)`.
    Returns None on any malformed input — caller treats that as
    "always on" rather than locking the worker out due to a typo.
    Spec is inclusive on the start, exclusive on the end (so
    `10:00-22:00` runs 10:00:00 through 21:59:59).
    """
    if not spec or not isinstance(spec, str):
        return None
    try:
        start_s, end_s = spec.split("-", 1)
        sh, sm = start_s.strip().split(":", 1)
        eh, em = end_s.strip().split(":", 1)
        start_h, start_m = int(sh), int(sm)
        end_h, end_m = int(eh), int(em)
    except (ValueError, AttributeError):
        return None
    if not (0 <= start_h <= 23 and 0 <= start_m <= 59
            and 0 <= end_h <= 23 and 0 <= end_m <= 59):
        return None
    return start_h, start_m, end_h, end_m


def _resolve_local_now(tz_name: str = "") -> datetime:
    """Current wallclock in the schedule's timezone. Empty / invalid
    `tz_name` falls back to system local time so a typo in the
    settings doesn't crash the worker."""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            logger.debug(
                "metadata_cache_worker: timezone %r not resolvable, "
                "falling back to system local",
                tz_name, exc_info=True,
            )
    return datetime.now()


def _minute_of_day(h: int, m: int) -> int:
    return h * 60 + m


def is_inside_schedule_window(source_name: str) -> bool:
    """True when worker mode is anything OTHER than `"scheduled"`,
    OR when mode is `"scheduled"` and the current local wallclock
    falls inside the configured active-hours window.

    Window spec is `"HH:MM-HH:MM"` with two interpretations:
      - **Daytime window** (start < end): `10:00-22:00` runs from 10
        AM through 9:59 PM, sleeps the rest of the day.
      - **Overnight window** (start > end): `22:00-06:00` runs from
        10 PM through 5:59 AM, spanning midnight.

    An invalid spec is treated as "always inside" so an operator
    typo can never strand the worker indefinitely.
    """
    mc = _source_settings(source_name)
    mode = get_worker_mode(source_name)
    if mode != "scheduled":
        return True
    schedule = mc.get("schedule") or {}
    spec = schedule.get("active_hours") or "10:00-22:00"
    parsed = _parse_active_hours(spec)
    if parsed is None:
        return True
    start_h, start_m, end_h, end_m = parsed
    now = _resolve_local_now(schedule.get("timezone") or "")
    now_min = _minute_of_day(now.hour, now.minute)
    start_min = _minute_of_day(start_h, start_m)
    end_min = _minute_of_day(end_h, end_m)
    if start_min == end_min:
        # `00:00-00:00` is a no-op spec; treat as always-on so the
        # user can't accidentally lock themselves out.
        return True
    if start_min < end_min:
        # Daytime window.
        return start_min <= now_min < end_min
    # Overnight window — inside iff we're past start OR before end.
    return now_min >= start_min or now_min < end_min


def seconds_until_window_open(source_name: str) -> float:
    """Seconds from now until the next active-window start. Returns 0
    when already inside the window or when mode is not `scheduled`.

    Used as the worker's `next_sleep_s` for the new
    `outside_schedule` outcome so the loop quietly sleeps until the
    window opens instead of polling every 60s through the off-hours.
    """
    mc = _source_settings(source_name)
    if get_worker_mode(source_name) != "scheduled":
        return 0.0
    schedule = mc.get("schedule") or {}
    spec = schedule.get("active_hours") or "10:00-22:00"
    parsed = _parse_active_hours(spec)
    if parsed is None:
        return 0.0
    start_h, start_m, _, _ = parsed
    now = _resolve_local_now(schedule.get("timezone") or "")
    # Build today's start timestamp in the same tz as `now`.
    today_start = now.replace(
        hour=start_h, minute=start_m, second=0, microsecond=0,
    )
    if is_inside_schedule_window(source_name):
        return 0.0
    delta = (today_start - now).total_seconds()
    if delta <= 0:
        # Today's window already started/ended — next start is
        # tomorrow's same time.
        delta += 24 * 3600
    return float(delta)


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


def _is_same_local_day(prior_ts: float, now: float) -> bool:
    """True iff both timestamps fall on the same calendar day in the
    local timezone. Used to reset `today_*` counters at the user's
    wallclock midnight rather than UTC midnight (which would land
    mid-evening for America/Detroit users)."""
    if not prior_ts:
        return False
    try:
        return (
            datetime.fromtimestamp(prior_ts).date()
            == datetime.fromtimestamp(now).date()
        )
    except (OverflowError, OSError, ValueError):
        return False


async def _record_block_in_worker_state(
    db: aiosqlite.Connection, source_name: str, now: float,
    *, cooldown_s: float,
) -> int:
    """Increment `consecutive_blocks` / `today_block_count`, stamp
    `last_block_at` + `block_cooldown_s`. Returns the new
    consecutive_blocks value so the caller can pick the right
    escalation tier.

    `today_block_count` resets when the local-tz day rolls over (the
    Phase G cosmetic fix) — before, the counter incremented monoto-
    nically since deploy and never returned to zero.
    """
    table = metadata_cache.worker_state_table(source_name)
    cur = await db.execute(
        f"SELECT last_block_at, consecutive_blocks, today_block_count "
        f"FROM {table} WHERE id = 1"
    )
    row = await cur.fetchone()
    prior_last = float(row[0] or 0.0) if row else 0.0
    prior_consecutive = int(row[1] or 0) if row else 0
    prior_today = int(row[2] or 0) if row else 0
    if (now - prior_last) > _ESCALATION_RESET_WINDOW_S:
        # 1h blockless — consecutive-blocks counter reset.
        new_consecutive = 1
    else:
        new_consecutive = prior_consecutive + 1
    if _is_same_local_day(prior_last, now):
        new_today = prior_today + 1
    else:
        new_today = 1
    await db.execute(
        f"UPDATE {table} "
        f"SET last_block_at = ?, consecutive_blocks = ?, "
        f"    block_cooldown_s = ?, "
        f"    today_block_count = ? "
        f"WHERE id = 1",
        (now, new_consecutive, cooldown_s, new_today),
    )
    await db.commit()
    return new_consecutive


async def _record_scan_completed(
    db: aiosqlite.Connection, source_name: str, now: float,
) -> None:
    """Bump `today_scan_count` + stamp `last_scan_completed_at`.

    `today_scan_count` resets when the local-tz day rolls over (the
    Phase G cosmetic fix) — before, the counter incremented monoto-
    nically since deploy and never returned to zero.
    """
    table = metadata_cache.worker_state_table(source_name)
    cur = await db.execute(
        f"SELECT last_scan_completed_at, today_scan_count "
        f"FROM {table} WHERE id = 1"
    )
    row = await cur.fetchone()
    prior_last = float(row[0] or 0.0) if row else 0.0
    prior_today = int(row[1] or 0) if row else 0
    if _is_same_local_day(prior_last, now):
        new_today = prior_today + 1
    else:
        new_today = 1
    await db.execute(
        f"UPDATE {table} "
        f"SET last_scan_completed_at = ?, today_scan_count = ? "
        f"WHERE id = 1",
        (now, new_today),
    )
    await db.commit()


async def _read_state_for_summary(
    source_name: str,
) -> dict[str, Any]:
    """Snapshot the counters the daily summary cares about. Read in
    its own connection so the summary path doesn't deadlock against
    the worker tick's transaction."""
    table = metadata_cache.worker_state_table(source_name)
    is_gr = source_name == metadata_cache.SOURCE_GOODREADS
    db = await metadata_cache.get_db(source_name)
    try:
        if is_gr:
            cur = await db.execute(
                f"SELECT today_scan_count, today_block_count, "
                f"       last_heartbeat_at, last_scan_completed_at, "
                f"       last_block_at, consecutive_blocks, "
                f"       today_budget_exhaust_count "
                f"FROM {table} WHERE id = 1"
            )
        else:
            cur = await db.execute(
                f"SELECT today_scan_count, today_block_count, "
                f"       last_heartbeat_at, last_scan_completed_at, "
                f"       last_block_at, consecutive_blocks "
                f"FROM {table} WHERE id = 1"
            )
        row = await cur.fetchone()
        if row is None:
            return {
                "today_scan_count": 0, "today_block_count": 0,
                "last_heartbeat_at": None,
                "last_scan_completed_at": None,
                "last_block_at": 0.0, "consecutive_blocks": 0,
                "today_budget_exhaust_count": 0,
            }
        snapshot = {
            "today_scan_count": int(row[0] or 0),
            "today_block_count": int(row[1] or 0),
            "last_heartbeat_at": row[2],
            "last_scan_completed_at": row[3],
            "last_block_at": float(row[4] or 0.0),
            "consecutive_blocks": int(row[5] or 0),
            "today_budget_exhaust_count": 0,
        }
        if is_gr and len(row) > 6:
            snapshot["today_budget_exhaust_count"] = int(row[6] or 0)
        return snapshot
    finally:
        await db.close()


async def record_goodreads_budget_exhaust() -> None:
    """v3.4.0 slice 05 — bump the GR budget-exhaust counter.

    Called from `lookup.py` at the `[goodreads] giving up on …`
    warning point (the Path A wall-clock budget exhaustion). Persists
    in `metadata_cache_goodreads_worker_state.today_budget_exhaust_count`;
    daily-summary rolls it over alongside the other today_* counters.

    Best-effort — a DB hiccup never raises into the caller. The
    daily summary message body surfaces the count, giving Mark a
    data signal for the v3.5.0 Path C decision (PRD §6.2 "perceptible
    wastage on filter-rejected new books that Path B can't eliminate").
    """
    try:
        source_name = metadata_cache.SOURCE_GOODREADS
        table = metadata_cache.worker_state_table(source_name)
        db = await metadata_cache.get_db(source_name)
        try:
            now = time.time()
            cur = await db.execute(
                f"SELECT today_budget_exhaust_count, last_block_at "
                f"FROM {table} WHERE id = 1"
            )
            row = await cur.fetchone()
            prior_count = int(row[0] or 0) if row else 0
            prior_last = float(row[1] or 0.0) if row else 0.0
            # Same day-rollover heuristic as the other today_*
            # counters: if the singleton hasn't been touched today,
            # this exhaust is the first of the new day.
            new_count = (
                prior_count + 1
                if _is_same_local_day(prior_last, now)
                else 1
            )
            await db.execute(
                f"UPDATE {table} "
                f"SET today_budget_exhaust_count = ? WHERE id = 1",
                (new_count,),
            )
            await db.commit()
        finally:
            await db.close()
    except Exception:
        logger.debug(
            "metadata_cache_worker: goodreads budget-exhaust "
            "counter bump failed (non-fatal)",
            exc_info=True,
        )


async def send_daily_summary(
    source_name: str = metadata_cache.SOURCE_AMAZON,
) -> None:
    """Fire the daily-summary ntfy (if enabled + configured) and
    zero out the `today_*` counters. Scheduler calls this once per
    day at `metadata_cache_daily_summary_hour` local time.

    Resets the counters even when ntfy is disabled — that's the
    primary purpose: clear yesterday's running totals so today's
    UI numbers reflect the past 24h, not all-time.
    """
    snapshot = await _read_state_for_summary(source_name)
    prior_scans, prior_blocks, prior_exhausts = (
        await reset_today_counters(source_name)
    )
    # The day-rollover logic in `_record_scan_completed` may have
    # already partially reset things on the first tick of the new
    # day. Either way, the snapshot above reflects the moment-before
    # state and is what we summarize.
    today_scans = max(snapshot["today_scan_count"], prior_scans)
    today_blocks = max(snapshot["today_block_count"], prior_blocks)
    today_exhausts = max(
        snapshot.get("today_budget_exhaust_count", 0), prior_exhausts,
    )
    logger.info(
        "metadata_cache_worker: daily summary for source=%s — "
        "scans=%d, blocks=%d, budget_exhausts=%d",
        source_name, today_scans, today_blocks, today_exhausts,
    )
    title = (
        f"{source_name.title()} worker — daily summary "
        f"({today_scans} scans)"
    )
    message_lines = [
        f"Scans: {today_scans}",
        f"Soft-blocks: {today_blocks}",
    ]
    if source_name == metadata_cache.SOURCE_GOODREADS:
        # v3.4.0 slice 05 — Path C decision data signal. Operator
        # sees the budget-exhaust count for the day; non-zero on
        # prolific authors is the trigger condition for shipping
        # detail-cache as v3.5.0 (PRD §6.2).
        message_lines.append(f"Budget exhausts: {today_exhausts}")
    if snapshot["consecutive_blocks"]:
        message_lines.append(
            f"Consecutive blocks in last hour: "
            f"{snapshot['consecutive_blocks']}"
        )
    await _send_ntfy(
        event_key="metadata_cache_daily_summary",
        title=title,
        message="\n".join(message_lines),
        priority=2,
        tags=["bar_chart"],
    )


async def check_stall(
    source_name: str = metadata_cache.SOURCE_AMAZON,
    *,
    threshold_s: Optional[float] = None,
) -> bool:
    """Return True iff the worker is enabled AND its
    `last_heartbeat_at` is older than `threshold_s` (default from
    settings). Fires the Tier-1 stall ntfy at most once per stall
    window — debounced via a runtime-state key in settings.json so
    a watchdog firing every minute doesn't spam.
    """
    if not is_worker_enabled(source_name):
        return False
    # Phase I — when the worker is intentionally outside its
    # schedule window, the long sleep is healthy by design. Don't
    # let the watchdog page on a worker that's doing exactly what
    # it was told to.
    if not is_inside_schedule_window(source_name):
        return False
    s = app_config.load_settings()
    if threshold_s is None:
        threshold_s = float(
            s.get("metadata_cache_stall_threshold_s", 300)
        )
    snapshot = await _read_state_for_summary(source_name)
    heartbeat = snapshot["last_heartbeat_at"]
    if heartbeat is None:
        # Worker enabled but has never ticked. That's a stall.
        age_s = float("inf")
    else:
        age_s = time.time() - float(heartbeat)
    if age_s < threshold_s:
        # Healthy — clear any prior debounce so the next stall fires
        # a notification cleanly.
        if s.get(f"metadata_cache.{source_name}.stall_notified_at"):
            s[f"metadata_cache.{source_name}.stall_notified_at"] = 0.0
            try:
                app_config.save_settings(s)
            except Exception:
                logger.exception(
                    "metadata_cache_worker: failed to clear stall "
                    "debounce key (non-fatal)"
                )
        return False
    # Stall confirmed. Debounce — only fire once per stall (cleared
    # when the heartbeat recovers above).
    debounce_key = f"metadata_cache.{source_name}.stall_notified_at"
    prior_notified = float(s.get(debounce_key) or 0.0)
    if prior_notified and heartbeat is not None and prior_notified > float(heartbeat):
        # Already notified for THIS stall window — stay quiet.
        return True
    s[debounce_key] = time.time()
    try:
        app_config.save_settings(s)
    except Exception:
        logger.exception(
            "metadata_cache_worker: failed to persist stall debounce "
            "key (notification will re-fire next watchdog tick)"
        )
    age_repr = f"{age_s:.0f}s" if age_s != float("inf") else "never"
    logger.warning(
        "metadata_cache_worker: stall detected — last heartbeat %s ago "
        "(threshold %.0fs)",
        age_repr, threshold_s,
    )
    await _send_ntfy(
        event_key="metadata_cache_error",
        title="Amazon worker — stalled",
        message=(
            f"No heartbeat for {age_repr}. Worker may have crashed "
            "or be stuck in a long-running call. Check container "
            "logs and the cache panel."
        ),
        priority=5,
        tags=["rotating_light"],
    )
    return True


async def reset_today_counters(
    source_name: str = metadata_cache.SOURCE_AMAZON,
) -> tuple[int, int, int]:
    """Zero the today_* counters and return the pre-reset values so
    the daily-summary scheduler job can include them in the ntfy
    message.

    Returns `(today_scan_count, today_block_count,
    today_budget_exhaust_count)` — the third element is always 0 for
    sources that don't track budget exhausts (Amazon today; only
    Goodreads since v3.4.0 slice 05).

    Idempotent — calling twice in the same day returns (0, 0, 0) the
    second time. Doesn't touch `last_scan_completed_at` /
    `last_block_at`, so the day-rollover heuristic in
    `_record_scan_completed` / `_record_block_in_worker_state`
    continues to work.
    """
    table = metadata_cache.worker_state_table(source_name)
    is_gr = source_name == metadata_cache.SOURCE_GOODREADS
    db = await metadata_cache.get_db(source_name)
    try:
        if is_gr:
            cur = await db.execute(
                f"SELECT today_scan_count, today_block_count, "
                f"       today_budget_exhaust_count "
                f"FROM {table} WHERE id = 1"
            )
            row = await cur.fetchone()
            prior_scans = int(row[0] or 0) if row else 0
            prior_blocks = int(row[1] or 0) if row else 0
            prior_exhausts = int(row[2] or 0) if row else 0
            await db.execute(
                f"UPDATE {table} "
                f"SET today_scan_count = 0, today_block_count = 0, "
                f"    today_budget_exhaust_count = 0 "
                f"WHERE id = 1"
            )
        else:
            cur = await db.execute(
                f"SELECT today_scan_count, today_block_count "
                f"FROM {table} WHERE id = 1"
            )
            row = await cur.fetchone()
            prior_scans = int(row[0] or 0) if row else 0
            prior_blocks = int(row[1] or 0) if row else 0
            prior_exhausts = 0
            await db.execute(
                f"UPDATE {table} "
                f"SET today_scan_count = 0, today_block_count = 0 "
                f"WHERE id = 1"
            )
        await db.commit()
        return prior_scans, prior_blocks, prior_exhausts
    finally:
        await db.close()


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

    Schema-v2: queue PK is `author_id` only. Worker reads per-library
    rows from the discovery DBs at scan time to partition results.
    """
    queue = metadata_cache.queue_table(source_name)
    await db.execute("BEGIN IMMEDIATE")
    try:
        cur = await db.execute(
            f"SELECT author_id, priority, status, next_scan_due_at, "
            f"last_attempt_at, consecutive_failures, enqueued_reason "
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
            f"WHERE author_id = ?",
            (now, row[0]),
        )
        await db.execute("COMMIT")
        return {
            "author_id": row[0],
            "priority": row[1],
            "status": "in_progress",
            "next_scan_due_at": row[3],
            "last_attempt_at": now,
            "consecutive_failures": row[5],
            "enqueued_reason": row[6],
        }
    except Exception:
        await db.execute("ROLLBACK")
        raise


async def _mark_queue_row_pending(
    db: aiosqlite.Connection, source_name: str, *,
    author_id: str,
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
            f"WHERE author_id = ?",
            (next_scan_due_at, author_id),
        )
    else:
        await db.execute(
            f"UPDATE {queue} SET status = 'pending', "
            f"    next_scan_due_at = ? "
            f"WHERE author_id = ?",
            (next_scan_due_at, author_id),
        )
    await db.commit()


async def _mark_queue_row_failure(
    db: aiosqlite.Connection, source_name: str, *,
    author_id: str,
    next_scan_due_at: float,
) -> tuple[int, bool]:
    """Increment `consecutive_failures`; flip to `failed_permanent`
    once the cap is hit. Returns (new_failure_count, became_permanent).
    """
    queue = metadata_cache.queue_table(source_name)
    cur = await db.execute(
        f"SELECT consecutive_failures FROM {queue} "
        f"WHERE author_id = ?",
        (author_id,),
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
            f"WHERE author_id = ?",
            (new_count, next_scan_due_at, author_id),
        )
    else:
        await db.execute(
            f"UPDATE {queue} "
            f"SET status = 'pending', "
            f"    consecutive_failures = ?, next_scan_due_at = ? "
            f"WHERE author_id = ?",
            (new_count, next_scan_due_at, author_id),
        )
    await db.commit()
    return new_count, became_permanent


# ─── Per-library partitioning (schema-v2) ────────────────────


# Map content_type → set of acceptable Amazon binding_symbols.
# Books returned by an `allFormats` scan are partitioned into each
# library's cache rows based on this mapping. Keeps audiobook
# bindings out of ebook libraries and vice versa even though we now
# scan once for all formats.
_EBOOK_BINDINGS: frozenset[str] = frozenset({
    "kindle_edition", "paperback", "hardcover", "mass_market",
})
_AUDIOBOOK_BINDINGS: frozenset[str] = frozenset({
    "audio_download", "audioCD", "mp3_cd",
    "preloaded_digital_audio_player",
})


def _bindings_for_content_type(content_type: str) -> frozenset[str]:
    """Map a library's content_type to the set of Amazon binding
    symbols its book rows should carry. Conservative — unknown
    bindings won't make it into either library's cache (the worker
    drops them rather than guess)."""
    if content_type == "audiobook":
        return _AUDIOBOOK_BINDINGS
    return _EBOOK_BINDINGS


async def _libraries_for_author(
    author_id: str,
    source_name: str = metadata_cache.SOURCE_AMAZON,
) -> list[dict[str, Any]]:
    """Return the list of libraries that have a row for ``author_id``.

    Each entry carries `slug`, `content_type`, and `seshat_author_id`
    (the per-library authors.id). Used by the worker to fan out a
    single source scan into per-library state + cache rows.

    Reads from the discovery DBs every time — no caching — so a
    just-resolved source ID is observed without needing a worker
    restart.

    `source_name` picks the discovery-authors column to look up by
    (amazon → `amazon_id`, goodreads → `goodreads_id`). v3.4.0 slice
    03 extension; pre-slice the function was Amazon-only.
    """
    _COL_BY_SOURCE = {
        metadata_cache.SOURCE_AMAZON: "amazon_id",
        metadata_cache.SOURCE_GOODREADS: "goodreads_id",
    }
    column = _COL_BY_SOURCE.get(source_name)
    if column is None:
        raise ValueError(
            f"_libraries_for_author: unknown source {source_name!r}"
        )
    from app.discovery.database import get_db as get_discovery_db
    result: list[dict[str, Any]] = []
    for lib in state._discovered_libraries:
        slug = lib.get("slug")
        if not slug:
            continue
        try:
            disc = await get_discovery_db(slug=slug)
        except Exception as exc:
            logger.debug(
                "metadata_cache_worker: cannot open discovery DB %r (%s) "
                "— skipping for fan-out",
                slug, exc,
            )
            continue
        try:
            cur = await disc.execute(
                f"SELECT id FROM authors WHERE {column} = ?",
                (author_id,),
            )
            row = await cur.fetchone()
        except Exception as exc:
            logger.debug(
                "metadata_cache_worker: authors lookup failed for %r/%s "
                "(%s) — skipping",
                slug, author_id, exc,
            )
            row = None
        finally:
            try:
                await disc.close()
            except Exception:
                pass
        if row is None:
            continue
        result.append({
            "slug": slug,
            "content_type": lib.get("content_type") or "ebook",
            "seshat_author_id": int(row[0]),
        })
    return result


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


def _flatten_for_library(
    author_result: Any, *,
    author_id: str, library_slug: str, now: float,
    allowed_bindings: frozenset[str], language: str,
) -> list[tuple]:
    """Walk an AmazonSource AuthorResult into cache-row tuples for
    ONE library, filtering by `allowed_bindings`.

    Schema-v2: each row stamps its own `format` from
    `BookResult.format` (Amazon's binding_symbol per product). A
    single `allFormats` scan returns books with mixed bindings —
    Kindle, Paperback, Hardcover, Audible, etc. — and this helper
    partitions them into the calling library's content_type-matched
    subset (kindle/paperback/hardcover for ebook libraries;
    audio_download and friends for audiobook libraries).

    Dedupes by `book_asin` so duplicate-ASIN payloads from mediaMatrix
    overlap or pagination repeats don't trip the
    `(author_id, library_slug, book_asin)` UNIQUE constraint
    (the Phase D hotfix on 2026-05-22 first introduced this dedup;
    schema-v2 keeps it).
    """
    rows: list[tuple] = []
    seen_asins: set[str] = set()
    duplicates_dropped = 0
    binding_filtered = 0
    missing_binding = 0
    all_books: list[Any] = list(author_result.books or [])
    for series in author_result.series or []:
        all_books.extend(series.books or [])
    for book in all_books:
        if not book.external_id:
            continue
        if book.external_id in seen_asins:
            duplicates_dropped += 1
            continue
        binding = getattr(book, "format", None)
        if binding is None:
            # No binding info — conservatively skip rather than
            # cross-contaminate libraries. Real Amazon products
            # always have a binding via `Product.binding_symbol`.
            missing_binding += 1
            continue
        if binding not in allowed_bindings:
            binding_filtered += 1
            continue
        seen_asins.add(book.external_id)
        rows.append((
            author_id, library_slug, book.external_id,
            book.title or "",
            book.series_name, book.series_index,
            book.pub_date,
            binding,
            book.language or language,
            book.isbn, book.cover_url,
            None,  # raw_json — reserved for richer future shapes
            now,
        ))
    if duplicates_dropped or binding_filtered or missing_binding:
        logger.debug(
            "metadata_cache_worker: %s/%s partition — kept %d, dropped "
            "%d dupes / %d off-content-type / %d missing-binding",
            author_id, library_slug, len(rows),
            duplicates_dropped, binding_filtered, missing_binding,
        )
    return rows


async def _fetch_prior_book_asins(
    db: aiosqlite.Connection, source_name: str, *, author_id: str,
) -> set[str]:
    """Return the set of `book_asin` values currently cached for
    ``author_id`` across every library. Used to compute the "new
    books this scan" set so the Phase G info-tier ntfy doesn't
    spam on first-fill scans.
    """
    books_table = metadata_cache.books_table(source_name)
    cur = await db.execute(
        f"SELECT DISTINCT book_asin FROM {books_table} WHERE author_id = ?",
        (author_id,),
    )
    return {row[0] for row in await cur.fetchall()}


async def _replace_list_page_rows(
    db: aiosqlite.Connection, source_name: str, *,
    author_id: str, library_slug: str,
    pages: dict[int, list[dict]],
) -> None:
    """v3.4.0 slice 03/04 — Goodreads-shape cache write. Replaces
    all `_list_pages` rows for one (author, library) atomically:
    DELETE the previous snapshots, INSERT the fresh scan's per-page
    raw_book records as JSON.

    `pages` is `{page_num: [{book_id, title, list_series,
    list_series_idx, list_cover, is_audio_list}, ...]}` — the
    slice 04 hybrid path needs these records to drive the detail-
    only loop in `GoodreadsSource.get_author_books` without
    re-fetching the list page. The column name `book_ids_json`
    predates slice 04 (it stored bare IDs in slice 03) but is
    kept as-is to avoid a schema rename + back-compat shim.

    Mirrors `_replace_book_rows` for Amazon — same DELETE-then-
    INSERT discipline so deletions on Goodreads' side (a book
    removed from an author's list page) propagate correctly. Worth
    it because list_page rows are tiny and a stale page outliving
    its GR shape would mask removed books from the reader
    downstream.
    """
    import json
    lp_table = metadata_cache.list_pages_table(source_name)
    now = time.time()
    await db.execute(
        f"DELETE FROM {lp_table} "
        f"WHERE author_id = ? AND library_slug = ?",
        (author_id, library_slug),
    )
    rows = [
        (author_id, library_slug, page_num, now, json.dumps(records))
        for page_num, records in sorted(pages.items())
    ]
    if rows:
        await db.executemany(
            f"INSERT INTO {lp_table} "
            f"(author_id, library_slug, page_num, fetched_at, "
            f" book_ids_json) "
            f"VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    await db.commit()


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
    author_id: str, session: Any,
) -> tuple[Any, Optional[str]]:
    """Schema-v2: scan ONCE with `allFormats` so a single Amazon
    round-trip covers every library this author lives in. The
    per-library partition happens at cache-write time downstream
    (see `_flatten_for_library`).

    Returns (None, error_msg) on:
      - curl_cffi missing (caller already handled, but defensive)
      - source raised
      - source returned None (transport or soft-block)
    """
    from app.discovery.sources.amazon import AmazonSource
    s = app_config.load_settings()
    amz = (s.get("metadata_sources") or {}).get("amazon") or {}
    language = amz.get("language") or "English"
    # Both format kwargs set to `allFormats` so AmazonSource's
    # `_active_format_filter()` returns the same value regardless of
    # the (now-irrelevant) `_content_type` attribute below.
    source = AmazonSource(
        rate_limit=0.0,           # worker controls cadence via jitter
        format_filter="allFormats",
        audiobook_format_filter="allFormats",
        language=language,
        burst_delay_s=0.0,        # no extra in-scan delays
    )
    source._session = session
    source._session_init_attempted = True
    # `_content_type` is now unused for binding selection (always
    # `allFormats`) but the AmazonSource init reads it for logging /
    # signature parity. Set to a stable value.
    source._content_type = "ebook"
    try:
        result = await source.get_author_books(author_id)
    except Exception as exc:
        logger.exception(
            "metadata_cache_worker: scan raised for %s: %s",
            author_id, exc,
        )
        return None, f"{type(exc).__name__}: {exc}"
    if result is None:
        return None, "source returned None (transport / soft-block)"
    return result, None


async def _perform_goodreads_scan(
    author_id: str,
) -> tuple[Optional[dict[int, list[str]]], Optional[str], bool]:
    """v3.4.0 slice 03 — Goodreads list-only scan for the cache
    worker (ADR-0018 §1).

    Builds a one-shot `GoodreadsSource` (NO curl_cffi, NO warmup —
    GR has no Akamai layer; the existing httpx-based GR session is
    sufficient) and calls `list_page_inventory(author_id)`.

    Returns `(pages_dict, error_msg, is_soft_block)`:
      - `(pages, None, False)` on success — pages is `{page_num:
        [book_id, ...]}`.
      - `(None, error_msg, False)` on hard error (parser raised,
        transport-level failure).
      - `(None, error_msg, True)` on soft-block (HTTP 202 / 503
        observed by the source's existing retry path; surfaces as
        the source returning None after exhausting retries).

    The soft-block vs hard-error distinction matters in `tick`: a
    soft-block defers the queue row past a cooldown without
    incrementing `consecutive_failures`; a hard error increments
    failures and eventually flips the row to `failed_permanent`.
    The current `GoodreadsSource` doesn't surface the distinction
    explicitly — it just returns None — so we treat None as a
    cooldown-class signal (soft-block) for safety. A future
    `list_page_inventory` extension can return a richer error.
    """
    from app.discovery.sources.goodreads import GoodreadsSource
    s = app_config.load_settings()
    gr_cfg = (s.get("metadata_sources") or {}).get("goodreads") or {}
    rate_limit = float(gr_cfg.get("rate_limit", 5.0))
    source = GoodreadsSource(rate_limit=rate_limit)
    try:
        pages = await source.list_page_inventory(author_id)
    except Exception as exc:
        logger.exception(
            "metadata_cache_worker: goodreads list-page scan raised "
            "for %s: %s", author_id, exc,
        )
        return None, f"{type(exc).__name__}: {exc}", False
    finally:
        # GoodreadsSource may hold an httpx session in
        # `_goodreads_session`; close it cleanly if exposed.
        try:
            sess = getattr(source, "_session", None)
            if sess is not None and hasattr(sess, "aclose"):
                await sess.aclose()
        except Exception:
            pass
    if pages is None:
        # GR source returned None — treat as soft-block (cooldown
        # class). A genuine hard parser failure would have raised.
        return None, "goodreads returned None (likely 202/503 soft-block)", True
    return pages, None, False


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
    started_at = now  # for elapsed_ms in the `[scan]` summary line
    scan_log = _scan_logger(source_name)

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

    # Phase I schedule gate. When mode=scheduled and we're outside the
    # active-hours window, return a distinct outcome and sleep until
    # the window opens. Capped at _COOLDOWN_MAX_SLEEP_S so a misparsed
    # window can't deadlock the worker for an entire day; capped at
    # _IDLE_SLEEP_S min so an off-by-a-minute return keeps us
    # cooperative with stop_event-driven shutdowns.
    if not is_inside_schedule_window(source_name):
        wait_s = seconds_until_window_open(source_name)
        sleep_s = min(max(wait_s, _IDLE_SLEEP_S), _COOLDOWN_MAX_SLEEP_S)
        return TickResult(
            source_name=source_name, outcome="outside_schedule",
            next_sleep_s=sleep_s,
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

    # Schema-v2: figure out which libraries this author belongs to so
    # one scan response can be partitioned into per-library state +
    # books rows downstream. If the author has no per-library rows
    # (amazon_id was removed since the queue row was created, or the
    # author was deleted), drop the queue row gracefully — there's
    # nothing left for the worker to do.
    libraries = await _libraries_for_author(author_id)
    if not libraries:
        db = await metadata_cache.get_db(source_name)
        try:
            await _mark_queue_row_pending(
                db, source_name,
                author_id=author_id,
                next_scan_due_at=now + _NORMAL_RESCAN_CADENCE_S,
                reset_failures=True,
            )
        finally:
            await db.close()
        logger.info(
            "metadata_cache_worker: %s has no per-library rows; "
            "deferring queue row by %.0fs",
            author_id, _NORMAL_RESCAN_CADENCE_S,
        )
        return TickResult(
            source_name=source_name, outcome="ok_empty",
            author_id=author_id, queue_size=queue_size,
            next_sleep_s=random.uniform(_JITTER_MIN_S, _JITTER_MAX_S),
        )

    session = _create_session()
    if session is None:
        # curl_cffi missing — defer the queue row + report error.
        db = await metadata_cache.get_db(source_name)
        try:
            await _mark_queue_row_pending(
                db, source_name,
                author_id=author_id,
                next_scan_due_at=now + _COOLDOWN_MAX_SLEEP_S,
                reset_failures=False,
            )
        finally:
            await db.close()
        return TickResult(
            source_name=source_name, outcome="error",
            author_id=author_id,
            queue_size=queue_size,
            error="curl_cffi not installed",
            next_sleep_s=_IDLE_SLEEP_S,
        )

    try:
        await _run_warmup(session)
        result, scan_error = await _perform_amazon_scan(
            author_id, session,
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
            escalated_s = _pick_escalation_cooldown(consecutive)
            escalated = escalated_s > cooldown_s
            if escalated:
                from app.discovery.amazon_author_id_resolver import (
                    record_amazon_soft_block,
                )
                record_amazon_soft_block(
                    f"worker escalation (block #{consecutive} within "
                    f"{_ESCALATION_RESET_WINDOW_S:.0f}s)",
                    retry_after_s=escalated_s,
                )
                cooldown_s = escalated_s
            # Defer queue row past the cooldown; not a failure.
            await _mark_queue_row_pending(
                db, source_name,
                author_id=author_id,
                next_scan_due_at=now + cooldown_s + 60.0,
                reset_failures=True,
            )
        finally:
            await db.close()
        elapsed_ms = (time.time() - started_at) * 1000.0
        scan_log.warning(
            "[scan] %s",
            _format_fields(
                author=author_id, outcome="soft_block",
                consecutive=consecutive, cooldown_s=cooldown_s,
                escalated=escalated, elapsed_ms=elapsed_ms,
            ),
        )
        # ntfy Tier 2 (warning) only fires when we just escalated TO
        # the top tier (3600s). Routine 1st/2nd-tier blocks are normal
        # operation — the worker spins them out via `cooldown_s + 60s`
        # deferral without paging the operator.
        if escalated and cooldown_s >= _ESCALATION_TIERS_S[-1]:
            await _send_ntfy(
                event_key="metadata_cache_warning",
                title="Amazon worker — cooldown escalated to top tier",
                message=(
                    f"Author {author_id} tripped consecutive soft-block "
                    f"#{consecutive} within {_ESCALATION_RESET_WINDOW_S:.0f}s. "
                    f"Cooldown is now {cooldown_s:.0f}s "
                    f"(top of the {len(_ESCALATION_TIERS_S)}-tier curve). "
                    "Worker will resume scanning automatically once it clears."
                ),
                priority=4,
                tags=["warning", "snail"],
            )
        return TickResult(
            source_name=source_name, outcome="soft_block",
            author_id=author_id,
            queue_size=queue_size, cooldown_remaining_s=cooldown_s,
            next_sleep_s=min(cooldown_s + 1.0, _COOLDOWN_MAX_SLEEP_S),
            elapsed_ms=elapsed_ms,
        )

    if result is None:
        # Non-soft-block failure (transport error, parse error, …).
        db = await metadata_cache.get_db(source_name)
        try:
            new_count, became_permanent = await _mark_queue_row_failure(
                db, source_name,
                author_id=author_id,
                next_scan_due_at=now + _NORMAL_RESCAN_CADENCE_S,
            )
            # Record a per-library state row for each library so the
            # reader knows we tried. Cache reader keeps returning
            # None until a later successful scan replaces this.
            for lib in libraries:
                await _upsert_state_row(
                    db, source_name,
                    author_id=author_id, library_slug=lib["slug"],
                    seshat_author_id=lib["seshat_author_id"],
                    now=now, outcome="error",
                    book_count=0, last_error=(scan_error or "unknown"),
                )
        finally:
            await db.close()
        outcome = "permanent_fail" if became_permanent else "error"
        elapsed_ms = (time.time() - started_at) * 1000.0
        scan_log.warning(
            "[scan] %s",
            _format_fields(
                author=author_id, outcome=outcome,
                consecutive_failures=new_count, permanent=became_permanent,
                elapsed_ms=elapsed_ms, error=(scan_error or "unknown"),
            ),
        )
        # ntfy Tier 2 (warning): one queue row has exhausted its
        # retries and is now `failed_permanent`. The operator needs to
        # decide whether to manually re-queue or accept the gap. Non-
        # permanent transient failures don't notify — the worker
        # keeps retrying them silently.
        if became_permanent:
            await _send_ntfy(
                event_key="metadata_cache_warning",
                title="Amazon worker — author flipped to failed_permanent",
                message=(
                    f"{author_id} hit {_MAX_CONSECUTIVE_FAILURES} consecutive "
                    f"failures and was retired from the active queue. "
                    f"Last error: {scan_error or 'unknown'}. "
                    "Visit the cache panel to re-queue if this was transient."
                ),
                priority=4,
                tags=["warning", "skull"],
            )
        return TickResult(
            source_name=source_name, outcome=outcome,
            author_id=author_id,
            queue_size=queue_size, error=scan_error,
            next_sleep_s=random.uniform(_JITTER_MIN_S, _JITTER_MAX_S),
            elapsed_ms=elapsed_ms,
        )

    # Successful scan: fan out per-library writes from the single
    # `allFormats` response. `_flatten_for_library` partitions by
    # the per-library content_type's allowed bindings.
    s = app_config.load_settings()
    language = (
        (s.get("metadata_sources") or {}).get("amazon", {}).get("language")
        or "English"
    )
    per_library_rows: list[tuple[dict[str, Any], list[tuple]]] = []
    for lib in libraries:
        bindings = _bindings_for_content_type(lib["content_type"])
        rows = _flatten_for_library(
            result,
            author_id=author_id, library_slug=lib["slug"],
            now=now, allowed_bindings=bindings, language=language,
        )
        per_library_rows.append((lib, rows))
    total_cache_rows = sum(len(rows) for _, rows in per_library_rows)

    # New-book detection (Phase G): capture prior cached ASINs BEFORE
    # the cache write so we can diff the new set against them. A
    # first-fill scan (no prior rows) yields `prior_asins = set()`, in
    # which case we treat the entire result as baseline rather than
    # firing an N-book ntfy.
    new_titles: list[tuple[str, str]] = []
    try:
        db = await metadata_cache.get_db(source_name)
        try:
            prior_asins = await _fetch_prior_book_asins(
                db, source_name, author_id=author_id,
            )
        finally:
            await db.close()
    except Exception:
        logger.debug(
            "metadata_cache_worker: prior-ASIN lookup failed for %s "
            "(non-fatal — new-book detection skipped this tick)",
            author_id, exc_info=True,
        )
        prior_asins = set()

    # Single try/except for the whole multi-library cache-write so any
    # failure mid-fan-out cleanly unwinds via the failure path.
    try:
        db = await metadata_cache.get_db(source_name)
        try:
            for lib, rows in per_library_rows:
                await _upsert_state_row(
                    db, source_name,
                    author_id=author_id, library_slug=lib["slug"],
                    seshat_author_id=lib["seshat_author_id"],
                    now=now, outcome="ok", book_count=len(rows),
                )
                await _replace_book_rows(
                    db, source_name,
                    author_id=author_id, library_slug=lib["slug"],
                    rows=rows,
                )
            await _mark_queue_row_pending(
                db, source_name,
                author_id=author_id,
                next_scan_due_at=now + _NORMAL_RESCAN_CADENCE_S,
                reset_failures=True,
            )
            await _record_scan_completed(db, source_name, now)
        finally:
            await db.close()
    except Exception as exc:
        logger.exception(
            "metadata_cache_worker: cache-write failed for %s (%s) — "
            "resetting queue row so the next iteration can retry",
            author_id, exc,
        )
        try:
            db = await metadata_cache.get_db(source_name)
            try:
                await _mark_queue_row_failure(
                    db, source_name,
                    author_id=author_id,
                    next_scan_due_at=now + _NORMAL_RESCAN_CADENCE_S,
                )
            finally:
                await db.close()
        except Exception:
            logger.exception(
                "metadata_cache_worker: queue-row recovery also "
                "failed — row will be cleaned up by startup "
                "recover_stuck_in_progress on the next restart"
            )
        elapsed_ms = (time.time() - started_at) * 1000.0
        scan_log.error(
            "[scan] %s",
            _format_fields(
                author=author_id, outcome="cache_write_fail",
                elapsed_ms=elapsed_ms,
                error=f"{type(exc).__name__}: {exc}",
            ),
        )
        # ntfy Tier 1 (error): DB-write failure is one of the
        # explicitly-error-tier events from the plan. Worker stays
        # alive but the operator should know — a stuck cache-write
        # path can silently halt cache freshness for every author.
        await _send_ntfy(
            event_key="metadata_cache_error",
            title="Amazon worker — cache write failed",
            message=(
                f"Author {author_id}: {type(exc).__name__}: {exc}. "
                "Queue row reset; worker will retry on the next "
                "iteration. Check container logs if this repeats."
            ),
            priority=5,
            tags=["rotating_light"],
        )
        return TickResult(
            source_name=source_name, outcome="error",
            author_id=author_id,
            queue_size=queue_size,
            error=f"cache write failed: {type(exc).__name__}: {exc}",
            next_sleep_s=random.uniform(_JITTER_MIN_S, _JITTER_MAX_S),
            elapsed_ms=elapsed_ms,
        )

    # Diff the new ASIN set against the captured prior set. Each row
    # tuple shape is `(author_id, library_slug, book_asin, title, …)`
    # — see `_flatten_for_library`. We only count ASINs as "new" if
    # the author had prior cached books AT ALL (otherwise every book
    # would be new on first scan, which would spam the operator).
    new_count = 0
    if prior_asins:
        new_titles_map: dict[str, str] = {}
        for _lib, rows in per_library_rows:
            for row in rows:
                asin = row[2]
                title = row[3] or ""
                if asin not in prior_asins:
                    new_titles_map.setdefault(asin, title)
        new_titles = list(new_titles_map.items())
        new_count = len(new_titles)

    per_lib_summary = ", ".join(
        f"{lib['slug']}={len(rows)}" for lib, rows in per_library_rows
    )
    elapsed_ms = (time.time() - started_at) * 1000.0
    outcome = "ok" if total_cache_rows else "ok_empty"
    scan_log.info(
        "[scan] %s [%s]",
        _format_fields(
            author=author_id, outcome=outcome,
            books=total_cache_rows, new=new_count,
            libraries=len(libraries), elapsed_ms=elapsed_ms,
        ),
        per_lib_summary,
    )
    # ntfy Tier 3 (info, opt-in): operator wants to celebrate every
    # new ASIN the worker uncovers. Capped at 3 titles per message
    # so a multi-book release doesn't produce a wall of text.
    if new_titles:
        sample = new_titles[:3]
        message_lines = [f"{title or asin} ({asin})" for asin, title in sample]
        if new_count > len(sample):
            message_lines.append(f"… and {new_count - len(sample)} more")
        await _send_ntfy(
            event_key="metadata_cache_new_book",
            title=(
                f"Amazon worker — {new_count} new book"
                f"{'s' if new_count != 1 else ''} for {author_id}"
            ),
            message="\n".join(message_lines),
            priority=3,
            tags=["books", "sparkles"],
        )
    return TickResult(
        source_name=source_name, outcome=outcome,
        author_id=author_id,
        books_cached=total_cache_rows, queue_size=queue_size,
        new_books=new_count,
        next_sleep_s=random.uniform(_JITTER_MIN_S, _JITTER_MAX_S),
        elapsed_ms=elapsed_ms,
    )


# ─── Goodreads tick (v3.4.0 slice 03) ──────────────────────────


# GR has no global IP-level cooldown (no Akamai-class hard wall) —
# soft-block defers the queue row by a single 300s cooldown without
# escalating (per ADR-0018 §1 — "lighter cooldown curve" diverges
# intentionally from Amazon's 600s/1800s/3600s tier).
_GR_SOFT_BLOCK_COOLDOWN_S = 300.0


async def tick_goodreads() -> TickResult:
    """v3.4.0 slice 03 — Goodreads cache worker tick (ADR-0018).

    Mirrors `tick()`'s skeleton (heartbeat / mode gate / schedule
    gate / queue pop / per-library fan-out / cache write) but with
    GR-specific differences: NO curl_cffi session, NO warmup, NO
    `is_amazon_blocked()` global gate (GR has no IP-level cooldown),
    NO escalation tier, and writes to the GR-shape `list_pages`
    table instead of Amazon's `books` table. New-book detection
    deferred to slice 05 telemetry.

    Never raises; every error path returns a `TickResult` so
    `run_loop` can decide the next sleep.
    """
    source_name = metadata_cache.SOURCE_GOODREADS
    now = time.time()
    started_at = now
    scan_log = _scan_logger(source_name)

    # Heartbeat fires every tick before any gate (same rationale as
    # the Amazon tick — distinguishes "disabled / cooled / empty"
    # from "task died").
    try:
        hb_db = await metadata_cache.get_db(source_name)
        try:
            await _stamp_heartbeat(hb_db, source_name, now)
        finally:
            await hb_db.close()
    except Exception:
        logger.exception(
            "metadata_cache_worker[goodreads]: heartbeat stamp "
            "failed (non-fatal)"
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

    if not is_inside_schedule_window(source_name):
        wait_s = seconds_until_window_open(source_name)
        sleep_s = min(max(wait_s, _IDLE_SLEEP_S), _COOLDOWN_MAX_SLEEP_S)
        return TickResult(
            source_name=source_name, outcome="outside_schedule",
            next_sleep_s=sleep_s,
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
    libraries = await _libraries_for_author(author_id, source_name)
    if not libraries:
        db = await metadata_cache.get_db(source_name)
        try:
            await _mark_queue_row_pending(
                db, source_name,
                author_id=author_id,
                next_scan_due_at=now + _NORMAL_RESCAN_CADENCE_S,
                reset_failures=True,
            )
        finally:
            await db.close()
        return TickResult(
            source_name=source_name, outcome="ok_empty",
            author_id=author_id, queue_size=queue_size,
            next_sleep_s=random.uniform(_JITTER_MIN_S, _JITTER_MAX_S),
        )

    pages, scan_error, is_soft_block = await _perform_goodreads_scan(
        author_id,
    )

    if is_soft_block:
        cooldown_s = _GR_SOFT_BLOCK_COOLDOWN_S
        db = await metadata_cache.get_db(source_name)
        try:
            await _record_block_in_worker_state(
                db, source_name, now, cooldown_s=cooldown_s,
            )
            # Defer the queue row past the cooldown; not a failure.
            await _mark_queue_row_pending(
                db, source_name,
                author_id=author_id,
                next_scan_due_at=now + cooldown_s + 60.0,
                reset_failures=True,
            )
        finally:
            await db.close()
        elapsed_ms = (time.time() - started_at) * 1000.0
        scan_log.warning(
            "[scan] %s",
            _format_fields(
                author=author_id, outcome="soft_block",
                cooldown_s=cooldown_s, elapsed_ms=elapsed_ms,
            ),
        )
        return TickResult(
            source_name=source_name, outcome="soft_block",
            author_id=author_id,
            queue_size=queue_size, cooldown_remaining_s=cooldown_s,
            next_sleep_s=min(cooldown_s + 1.0, _COOLDOWN_MAX_SLEEP_S),
            elapsed_ms=elapsed_ms,
        )

    if pages is None:
        # Hard error path (scan raised). Mark the row failed; after
        # N consecutive failures it flips to `failed_permanent`.
        db = await metadata_cache.get_db(source_name)
        try:
            new_count, became_permanent = await _mark_queue_row_failure(
                db, source_name,
                author_id=author_id,
                next_scan_due_at=now + _NORMAL_RESCAN_CADENCE_S,
            )
            for lib in libraries:
                await _upsert_state_row(
                    db, source_name,
                    author_id=author_id, library_slug=lib["slug"],
                    seshat_author_id=lib["seshat_author_id"],
                    now=now, outcome="error",
                    book_count=0, last_error=(scan_error or "unknown"),
                )
        finally:
            await db.close()
        outcome = "permanent_fail" if became_permanent else "error"
        elapsed_ms = (time.time() - started_at) * 1000.0
        scan_log.warning(
            "[scan] %s",
            _format_fields(
                author=author_id, outcome=outcome,
                consecutive_failures=new_count, permanent=became_permanent,
                elapsed_ms=elapsed_ms, error=(scan_error or "unknown"),
            ),
        )
        if became_permanent:
            await _send_ntfy(
                event_key="metadata_cache_warning",
                title="Goodreads worker — author flipped to failed_permanent",
                message=(
                    f"{author_id} hit {_MAX_CONSECUTIVE_FAILURES} consecutive "
                    f"failures and was retired from the active queue. "
                    f"Last error: {scan_error or 'unknown'}."
                ),
                priority=4,
                tags=["warning", "skull"],
            )
        return TickResult(
            source_name=source_name, outcome=outcome,
            author_id=author_id,
            queue_size=queue_size, error=scan_error,
            next_sleep_s=random.uniform(_JITTER_MIN_S, _JITTER_MAX_S),
            elapsed_ms=elapsed_ms,
        )

    # Successful scan. Goodreads list pages are global per GR
    # author (no per-library divergence — the list page doesn't
    # vary by content_type the way Amazon's binding_symbol does).
    # We still fan out per-library state + list_pages rows so the
    # downstream reader can scope by library_slug (matches v2.21.0
    # Amazon shape; required for ADR-0002 compliance).
    # `pages` is `{page_num: [{book_id, title, ...}, ...]}` post-slice 04.
    book_count_total = sum(len(records) for records in pages.values())
    try:
        db = await metadata_cache.get_db(source_name)
        try:
            for lib in libraries:
                await _upsert_state_row(
                    db, source_name,
                    author_id=author_id, library_slug=lib["slug"],
                    seshat_author_id=lib["seshat_author_id"],
                    now=now, outcome="ok", book_count=book_count_total,
                )
                await _replace_list_page_rows(
                    db, source_name,
                    author_id=author_id, library_slug=lib["slug"],
                    pages=pages,
                )
            await _mark_queue_row_pending(
                db, source_name,
                author_id=author_id,
                next_scan_due_at=now + _NORMAL_RESCAN_CADENCE_S,
                reset_failures=True,
            )
            await _record_scan_completed(db, source_name, now)
        finally:
            await db.close()
    except Exception as exc:
        logger.exception(
            "metadata_cache_worker[goodreads]: cache-write failed "
            "for %s (%s)", author_id, exc,
        )
        try:
            db = await metadata_cache.get_db(source_name)
            try:
                await _mark_queue_row_failure(
                    db, source_name,
                    author_id=author_id,
                    next_scan_due_at=now + _NORMAL_RESCAN_CADENCE_S,
                )
            finally:
                await db.close()
        except Exception:
            logger.exception(
                "metadata_cache_worker[goodreads]: queue-row recovery "
                "also failed — startup recovery will sweep on next "
                "restart"
            )
        elapsed_ms = (time.time() - started_at) * 1000.0
        scan_log.error(
            "[scan] %s",
            _format_fields(
                author=author_id, outcome="cache_write_fail",
                elapsed_ms=elapsed_ms,
                error=f"{type(exc).__name__}: {exc}",
            ),
        )
        await _send_ntfy(
            event_key="metadata_cache_error",
            title="Goodreads worker — cache write failed",
            message=(
                f"Author {author_id}: {type(exc).__name__}: {exc}. "
                "Queue row reset; worker will retry next tick."
            ),
            priority=5,
            tags=["rotating_light"],
        )
        return TickResult(
            source_name=source_name, outcome="error",
            author_id=author_id,
            queue_size=queue_size,
            error=f"cache write failed: {type(exc).__name__}: {exc}",
            next_sleep_s=random.uniform(_JITTER_MIN_S, _JITTER_MAX_S),
            elapsed_ms=elapsed_ms,
        )

    elapsed_ms = (time.time() - started_at) * 1000.0
    outcome = "ok" if book_count_total else "ok_empty"
    per_lib_summary = ", ".join(
        f"{lib['slug']}={book_count_total}" for lib in libraries
    )
    scan_log.info(
        "[scan] %s [%s]",
        _format_fields(
            author=author_id, outcome=outcome,
            books=book_count_total, pages=len(pages),
            libraries=len(libraries), elapsed_ms=elapsed_ms,
        ),
        per_lib_summary,
    )
    return TickResult(
        source_name=source_name, outcome=outcome,
        author_id=author_id,
        books_cached=book_count_total, queue_size=queue_size,
        next_sleep_s=random.uniform(_JITTER_MIN_S, _JITTER_MAX_S),
        elapsed_ms=elapsed_ms,
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

    # v3.4.0 slice 03 — per-source dispatch. Amazon uses the
    # original `tick()` (curl_cffi + Akamai-tuned soft-block); GR
    # uses the slim `tick_goodreads()` (no session prep, no
    # warmup, no escalation). Source-name picks the correct tick.
    tick_fn = (
        tick_goodreads
        if source_name == metadata_cache.SOURCE_GOODREADS
        else tick
    )

    while True:
        try:
            result = (
                await tick_fn()
                if source_name == metadata_cache.SOURCE_GOODREADS
                else await tick_fn(source_name)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "metadata_cache_worker: tick crashed unexpectedly"
            )
            # Phase G: surface tick crashes to the operator. We rely
            # on the supervised-task restart to bring the loop back —
            # this ntfy is purely so the user knows it happened.
            try:
                await _send_ntfy(
                    event_key="metadata_cache_error",
                    title=f"{source_name.title()} worker — tick crashed",
                    message=(
                        f"{type(exc).__name__}: {exc}. "
                        "Worker loop is recovering automatically; "
                        "check container logs for traceback."
                    ),
                    priority=5,
                    tags=["rotating_light"],
                )
            except Exception:
                logger.exception(
                    "metadata_cache_worker: post-crash ntfy emit failed"
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
