"""
Metadata cache router (v2.21.0 Phase E).

Surfaces the worker + cache state to the frontend so an operator can
flip the worker on, watch it work, and intervene when something
goes wrong.

    GET   /api/v1/metadata-cache/{source}/status
    PATCH /api/v1/metadata-cache/{source}/settings    body: {enabled: bool}
    POST  /api/v1/metadata-cache/{source}/reset-cooldown

`{source}` is validated against `metadata_cache.SUPPORTED_SOURCES`
(today: just `amazon`; v2.22.0 candidate: `goodreads`). Unknown
sources 404 instead of silently falling back so a typo doesn't
write to the wrong tree.

The PATCH endpoint deliberately doesn't expose the full settings
surface yet — Phase E ships the enable toggle; cooldown curves,
schedules, daily caps, and notification preferences land in later
phases as the operator needs them.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import load_settings, save_settings
from app.discovery import metadata_cache
from app.discovery.amazon_author_id_resolver import (
    amazon_block_remaining_s,
    is_amazon_blocked,
)

_log = logging.getLogger("seshat.routers.metadata_cache")

router = APIRouter(
    prefix="/api/v1/metadata-cache", tags=["metadata-cache"],
)


# ─── Response models ────────────────────────────────────────────


class WorkerStatusModel(BaseModel):
    """Singleton worker_state row + derived live stats."""
    last_block_at: float
    block_cooldown_s: float
    consecutive_blocks: int
    last_heartbeat_at: Optional[float]
    last_scan_completed_at: Optional[float]
    today_scan_count: int
    today_block_count: int
    # Derived (not stored): "seconds since last heartbeat", so the
    # frontend doesn't have to recompute against client-side clock
    # drift.
    seconds_since_heartbeat: Optional[float]
    seconds_since_scan_completed: Optional[float]


class QueueStatsModel(BaseModel):
    """Aggregate counts across the queue. The four explicit-status
    counters cover every value the worker writes; `total` is the
    sum so the frontend doesn't have to re-add.

    v2.22.0 — `pending` is split into `due_now` (next_scan_due_at
    has elapsed and the worker may pop the row this tick) and
    `scheduled_later` (the row is enrolled but the worker is
    cooling it off). The unified `pending` field is kept for
    backwards compat — it equals `due_now + scheduled_later`. The
    UI should display `due_now` for the active "Queue" tile so a
    backfill visibly counts down toward zero, instead of staying
    pinned at the enrolled-author total like it did pre-v2.22.0.
    """
    total: int
    pending: int
    in_progress: int
    failed_permanent: int
    other: int  # any unknown status — defensive, should be 0 in practice
    due_now: int = 0          # pending AND next_scan_due_at <= now()
    scheduled_later: int = 0  # pending AND next_scan_due_at > now()


class CacheStatsModel(BaseModel):
    """Aggregate counts across the cache state + books tables.

    v2.22.0 — `unique_ok_authors` and `unique_total_authors` are the
    author-level (de-duplicated across libraries) counters the UI
    should display for "Authors cached" so a 645-author backfill
    shows N/645 instead of the 2×N / 2×645 per-library row counts.
    The `state_rows` / `ok_authors` fields are retained for
    backwards compat (e.g. debugging panels that want raw row
    counts) but should not be the primary user-facing progress
    gauge.
    """
    state_rows: int
    books_rows: int
    ok_authors: int       # state rows with last_outcome='ok' (per-lib)
    error_authors: int    # state rows with last_outcome='error' (per-lib)
    unique_ok_authors: int = 0      # DISTINCT author_id w/ any 'ok' state row
    unique_total_authors: int = 0   # DISTINCT author_id in the state table
    # v3.4.0 slice 06 — GR-shape counters. `list_pages_rows` is the
    # per-page snapshot count; always 0 for Amazon (no list_pages
    # table). `today_budget_exhaust_count` mirrors slice 05's
    # worker_state column for GR; always 0 for Amazon.
    list_pages_rows: int = 0
    today_budget_exhaust_count: int = 0


class CooldownModel(BaseModel):
    """Current IP-level penalty box state."""
    blocked: bool
    remaining_s: float
    reason: Optional[str]


class ScheduleModel(BaseModel):
    """Phase I — operating-hours window for `mode=scheduled`.

    `active_hours` is `"HH:MM-HH:MM"`; start can be greater than end
    for overnight windows (`22:00-06:00`). `timezone` accepts any
    IANA tz name (e.g. `America/Detroit`); empty string means
    "system local".
    """
    active_hours: str = "10:00-22:00"
    timezone: str = ""


class StatusResponse(BaseModel):
    source: str
    enabled: bool
    # Phase I — mode-aware status. `mode` is one of
    # "continuous" / "scheduled" / "disabled"; `schedule` carries the
    # window spec when relevant. `inside_schedule_window` exposes the
    # current gate state so the frontend can show "active now" vs
    # "sleeping until 10:00" without re-implementing the parser.
    mode: str
    schedule: ScheduleModel
    inside_schedule_window: bool
    seconds_until_window_open: float
    cooldown: CooldownModel
    worker: WorkerStatusModel
    queue: QueueStatsModel
    cache: CacheStatsModel


class SettingsPatchRequest(BaseModel):
    """Operator-facing knobs for the metadata cache worker.

    Phase I adds `mode` + `schedule`. `enabled` is preserved for
    backwards compat — clients that PATCH `enabled` still work, and
    `mode` is derived from it when `mode` isn't sent. Sending both
    is allowed; `mode` wins when present.
    """
    enabled: Optional[bool] = Field(default=None)
    mode: Optional[str] = Field(default=None)  # continuous|scheduled|disabled
    schedule: Optional[ScheduleModel] = Field(default=None)


class SettingsPatchResponse(BaseModel):
    ok: bool
    source: str
    enabled: bool
    mode: str
    schedule: ScheduleModel


class ResetCooldownResponse(BaseModel):
    ok: bool
    source: str
    previously_blocked: bool
    previous_remaining_s: float


class RecentDiscoveryRow(BaseModel):
    """One newly-cached book for the dashboard "recent finds"
    widget. Light projection — only the fields the widget renders;
    the full book row stays in the per-author detail flow."""
    author_id: str
    library_slug: str
    book_asin: str
    title: str
    series_name: Optional[str]
    series_pos: Optional[float]
    cached_at: float
    seconds_ago: float


class RecentDiscoveriesResponse(BaseModel):
    source: str
    window_hours: int
    discoveries: list[RecentDiscoveryRow]


class ListPageEntry(BaseModel):
    """One Goodreads list-page snapshot per `(author, library, page_num)`.
    GR caches list pages instead of per-book detail (ADR-0018 §1, Path
    B), so the per-author projection is a count of cached pages + the
    total book IDs they cover. v3.6.0 frontend-parity slice."""
    page_num: int
    fetched_at: float
    book_count: int


class AuthorCacheStateRow(BaseModel):
    """One per-(author, library) cache state + queue position. The
    frontend's per-author badge composes a single human-readable line
    from one or more of these (an author may appear in multiple
    libraries — e.g. ebook + audiobook — and have different cache
    state in each).

    `list_pages` is populated for the GR projection only (ADR-0018 §2
    — GR caches list pages, not per-book detail). Amazon rows always
    carry `list_pages=None`."""
    library_slug: str
    state: Optional[dict]  # last_scanned_at / last_outcome / book_count
    queue: Optional[dict]  # status / priority / next_scan_due_at / consecutive_failures
    list_pages: Optional[list[ListPageEntry]] = None


class AuthorCacheResponse(BaseModel):
    source: str
    # `amazon_author_id` predates the v3.6.0 GR projection and stays
    # populated for the Amazon source for backwards compatibility with
    # `AuthorCacheStatusBadge.tsx`. GR responses leave it as the empty
    # string and rely on `author_id` instead.
    amazon_author_id: str = ""
    author_id: str = ""
    libraries: list[AuthorCacheStateRow]
    cooldown: CooldownModel


# ─── Helpers ────────────────────────────────────────────────────


def _validate_source(source: str) -> str:
    if source not in metadata_cache.SUPPORTED_SOURCES:
        raise HTTPException(
            404, f"unknown metadata cache source: {source!r}",
        )
    return source


def _require_amazon_shape(source: str) -> None:
    """Reject non-Amazon sources for endpoints that read Amazon-shape
    per-book detail rows (`books` table). v3.4.0 slice 06 un-gated
    `/status` (per-source-aware now); `/recent-discoveries` +
    `/author/{aid}` stay Amazon-only because they project per-book
    detail rows that GR doesn't cache (Path B = list-page only,
    ADR-0018 §1)."""
    if source != metadata_cache.SOURCE_AMAZON:
        raise HTTPException(
            501,
            f"endpoint not yet wired for source={source!r} "
            f"(only Amazon caches per-book detail rows; GR is list-"
            f"page only per ADR-0018 §1)",
        )


def _cache_settings_get(source: str) -> dict:
    s = load_settings()
    mc = s.get("metadata_cache") or {}
    return mc.get(source) or {}


def _cache_settings_set(source: str, *, key: str, value: Any) -> None:
    """Idempotent in-place update of `metadata_cache.<source>.<key>`."""
    s = dict(load_settings())
    mc = dict(s.get("metadata_cache") or {})
    src = dict(mc.get(source) or {})
    src[key] = value
    mc[source] = src
    s["metadata_cache"] = mc
    save_settings(s)


def _cooldown_state(source: str) -> CooldownModel:
    """Read the IP-level cooldown shared with the live AmazonSource."""
    if source != metadata_cache.SOURCE_AMAZON:
        # Only Amazon has the cooldown plumbing today; other sources
        # report a permanently-clear cooldown until their equivalent
        # ships (Goodreads has its own session_state surface).
        return CooldownModel(blocked=False, remaining_s=0.0, reason=None)
    from app.discovery import amazon_author_id_resolver as r
    return CooldownModel(
        blocked=is_amazon_blocked(),
        remaining_s=amazon_block_remaining_s(),
        reason=r._block_reason or None,  # type: ignore[attr-defined]
    )


# ─── Endpoints ──────────────────────────────────────────────────


@router.get("/{source}/status", response_model=StatusResponse)
async def get_status(source: str) -> StatusResponse:
    """Live worker + queue + cache stats.

    Polled by the frontend status card. Read-only — never mutates
    state. Counts are exact (COUNT(*) per status / outcome) so a
    1289-row queue gives 1289 here, not a rate-limited estimate.
    """
    _validate_source(source)
    enabled = bool(_cache_settings_get(source).get("enabled", False))
    cooldown = _cooldown_state(source)
    is_gr = source == metadata_cache.SOURCE_GOODREADS

    db = await metadata_cache.get_db(source)
    try:
        # Worker singleton row.
        wt = metadata_cache.worker_state_table(source)
        cur = await db.execute(f"SELECT * FROM {wt} WHERE id = 1")
        wrow = await cur.fetchone()

        # Queue aggregates — one query, GROUP BY status.
        qt = metadata_cache.queue_table(source)
        cur = await db.execute(
            f"SELECT status, COUNT(*) FROM {qt} GROUP BY status"
        )
        qcounts: dict[str, int] = {}
        for row in await cur.fetchall():
            qcounts[str(row[0])] = int(row[1])

        # v2.22.0 — split `pending` into `due_now` vs
        # `scheduled_later`. The worker pops rows that satisfy
        # status='pending' AND next_scan_due_at <= now, so `due_now`
        # is the "work remaining in this ramp" gauge users actually
        # want to watch. `scheduled_later` is the steady-state pool
        # waiting for its next cadence tick.
        now_for_queue = time.time()
        cur = await db.execute(
            f"SELECT COUNT(*) FROM {qt} "
            f"WHERE status='pending' AND next_scan_due_at <= ?",
            (now_for_queue,),
        )
        due_now = int((await cur.fetchone())[0])

        # Cache state + per-source-shape detail counts.
        st_table = metadata_cache.state_table(source)
        cur = await db.execute(f"SELECT COUNT(*) FROM {st_table}")
        state_rows = int((await cur.fetchone())[0])
        # v3.4.0 slice 06 — per-source shape (ADR-0018 §2).
        # Amazon has `books`; Goodreads has `list_pages`. Surface
        # the right count under the matching response field; the
        # other field stays 0.
        books_rows = 0
        list_pages_rows = 0
        if is_gr:
            lp_table = metadata_cache.list_pages_table(source)
            cur = await db.execute(f"SELECT COUNT(*) FROM {lp_table}")
            list_pages_rows = int((await cur.fetchone())[0])
        else:
            b_table = metadata_cache.books_table(source)
            cur = await db.execute(f"SELECT COUNT(*) FROM {b_table}")
            books_rows = int((await cur.fetchone())[0])
        cur = await db.execute(
            f"SELECT last_outcome, COUNT(*) FROM {st_table} "
            f"GROUP BY last_outcome"
        )
        state_outcomes: dict[str, int] = {}
        for row in await cur.fetchall():
            state_outcomes[str(row[0])] = int(row[1])
        # v2.22.0 — author-level dedup so "X / Y authors cached" is
        # author-level not row-level. Pre-fix, a 2-library setup
        # produced 1289 rows for 645 authors and the tile read
        # 1289/1289 — useless as a progress gauge.
        cur = await db.execute(
            f"SELECT COUNT(DISTINCT author_id) FROM {st_table}"
        )
        unique_total_authors = int((await cur.fetchone())[0])
        cur = await db.execute(
            f"SELECT COUNT(DISTINCT author_id) FROM {st_table} "
            f"WHERE last_outcome='ok'"
        )
        unique_ok_authors = int((await cur.fetchone())[0])
    finally:
        await db.close()

    now = time.time()
    hb = wrow["last_heartbeat_at"] if wrow else None
    scan_completed = wrow["last_scan_completed_at"] if wrow else None
    worker_model = WorkerStatusModel(
        last_block_at=float(wrow["last_block_at"]) if wrow else 0.0,
        block_cooldown_s=float(wrow["block_cooldown_s"]) if wrow else 600.0,
        consecutive_blocks=int(wrow["consecutive_blocks"]) if wrow else 0,
        last_heartbeat_at=hb,
        last_scan_completed_at=scan_completed,
        today_scan_count=int(wrow["today_scan_count"]) if wrow else 0,
        today_block_count=int(wrow["today_block_count"]) if wrow else 0,
        seconds_since_heartbeat=(now - hb) if hb is not None else None,
        seconds_since_scan_completed=(
            (now - scan_completed) if scan_completed is not None else None
        ),
    )

    # v3.4.0 slice 05 — budget-exhaust counter is GR-only (Amazon
    # worker has no equivalent failure mode). Pull from the row if
    # the column exists; fall back to 0 otherwise so a pre-v3.4.0
    # Amazon-only install (no migration v7 ever ran) still serves
    # /amazon/status without exploding.
    today_budget_exhausts = 0
    if is_gr and wrow is not None:
        try:
            today_budget_exhausts = int(
                wrow["today_budget_exhaust_count"] or 0
            )
        except (IndexError, KeyError, TypeError):
            today_budget_exhausts = 0

    pending = qcounts.pop("pending", 0)
    in_progress = qcounts.pop("in_progress", 0)
    failed_permanent = qcounts.pop("failed_permanent", 0)
    other = sum(qcounts.values())  # any unknown status — defensive
    total = pending + in_progress + failed_permanent + other
    scheduled_later = max(pending - due_now, 0)
    queue_model = QueueStatsModel(
        total=total,
        pending=pending,
        in_progress=in_progress,
        failed_permanent=failed_permanent,
        other=other,
        due_now=due_now,
        scheduled_later=scheduled_later,
    )

    cache_model = CacheStatsModel(
        state_rows=state_rows,
        books_rows=books_rows,
        ok_authors=state_outcomes.get("ok", 0),
        error_authors=state_outcomes.get("error", 0),
        unique_ok_authors=unique_ok_authors,
        unique_total_authors=unique_total_authors,
        list_pages_rows=list_pages_rows,
        today_budget_exhaust_count=today_budget_exhausts,
    )

    # Phase I — mode + schedule. Reads through the same accessors the
    # worker uses so the API and the worker tick agree on the gate
    # state at every tick.
    from app.discovery import metadata_cache_worker
    mode = metadata_cache_worker.get_worker_mode(source)
    sched = _cache_settings_get(source).get("schedule") or {}
    schedule_model = ScheduleModel(
        active_hours=str(sched.get("active_hours") or "10:00-22:00"),
        timezone=str(sched.get("timezone") or ""),
    )
    inside = metadata_cache_worker.is_inside_schedule_window(source)
    wait_s = metadata_cache_worker.seconds_until_window_open(source)

    return StatusResponse(
        source=source,
        enabled=enabled,
        mode=mode,
        schedule=schedule_model,
        inside_schedule_window=inside,
        seconds_until_window_open=wait_s,
        cooldown=cooldown,
        worker=worker_model,
        queue=queue_model,
        cache=cache_model,
    )


@router.patch("/{source}/settings", response_model=SettingsPatchResponse)
async def patch_settings(
    source: str, body: SettingsPatchRequest,
) -> SettingsPatchResponse:
    """Update operator-facing knobs.

    Phase I (Settings → Sources Amazon panel) accepts:
      - `enabled` (legacy boolean — still honored for clients that
        pre-date the mode toggle)
      - `mode` ("continuous" / "scheduled" / "disabled")
      - `schedule.active_hours` + `schedule.timezone`

    Writes through `app.config.save_settings` so the change persists
    across restart and the worker (which re-reads settings on every
    tick) picks it up on the next iteration — no container restart
    needed.

    When `mode` is sent, `enabled` is also synced to match (False
    when mode=disabled, True otherwise) so any downstream consumer
    still reading the legacy field gets the right value.
    """
    _validate_source(source)
    if body.mode is not None:
        if body.mode not in ("continuous", "scheduled", "disabled"):
            raise HTTPException(
                400, f"unknown mode: {body.mode!r} "
                "(expected continuous / scheduled / disabled)",
            )
        _cache_settings_set(source, key="mode", value=body.mode)
        # Keep `enabled` in sync so legacy reads (frontend code that
        # hasn't migrated to `mode` yet, future Goodreads cache code)
        # still see the correct boolean.
        _cache_settings_set(
            source, key="enabled", value=body.mode != "disabled",
        )
        _log.info(
            "metadata_cache settings: %s mode → %s", source, body.mode,
        )
    elif body.enabled is not None:
        _cache_settings_set(source, key="enabled", value=bool(body.enabled))
        # Mirror into mode so the two fields don't drift if a later
        # PATCH sends mode without enabled.
        _cache_settings_set(
            source, key="mode",
            value="continuous" if body.enabled else "disabled",
        )
        _log.info(
            "metadata_cache settings: %s enabled → %s "
            "(mode derived to %s)",
            source, body.enabled,
            "continuous" if body.enabled else "disabled",
        )
    if body.schedule is not None:
        # Validate the spec — reject obviously bad input rather than
        # silently letting the parser fall back to "always on".
        from app.discovery import metadata_cache_worker
        parsed = metadata_cache_worker._parse_active_hours(
            body.schedule.active_hours
        )
        if parsed is None:
            raise HTTPException(
                400, f"invalid schedule.active_hours: "
                f"{body.schedule.active_hours!r} (expected HH:MM-HH:MM)",
            )
        _cache_settings_set(
            source, key="schedule",
            value={
                "active_hours": body.schedule.active_hours,
                "timezone": body.schedule.timezone,
            },
        )
        _log.info(
            "metadata_cache settings: %s schedule → %s (tz=%r)",
            source, body.schedule.active_hours, body.schedule.timezone,
        )

    from app.discovery import metadata_cache_worker
    new_enabled = bool(_cache_settings_get(source).get("enabled", False))
    new_mode = metadata_cache_worker.get_worker_mode(source)
    sched_now = _cache_settings_get(source).get("schedule") or {}

    # v3.4.0 slice 04 — GR mode change must rebuild the `goodreads`
    # module-level source in lookup.py so the live↔cached swap
    # takes effect without a container restart. Amazon's cached
    # source has been the only path since v2.21.0 so its panel
    # never needed this hook; GR is the first source the mode
    # toggle re-wires.
    if source == metadata_cache.SOURCE_GOODREADS:
        try:
            from app.discovery.lookup import reload_sources
            reload_sources()
        except Exception:
            _log.exception(
                "metadata_cache settings: goodreads source reload "
                "failed (non-fatal; next container restart will "
                "honor the new mode)"
            )

    return SettingsPatchResponse(
        ok=True, source=source,
        enabled=new_enabled,
        mode=new_mode,
        schedule=ScheduleModel(
            active_hours=str(sched_now.get("active_hours") or "10:00-22:00"),
            timezone=str(sched_now.get("timezone") or ""),
        ),
    )


@router.get(
    "/{source}/recent-discoveries",
    response_model=RecentDiscoveriesResponse,
)
async def get_recent_discoveries(
    source: str,
    limit: int = Query(10, ge=1, le=100),
    hours: int = Query(24, ge=1, le=720),
) -> RecentDiscoveriesResponse:
    """Latest cached books for the dashboard "Recent Amazon finds"
    section. Returns rows whose `cached_at` is within the last
    `hours` window, newest first, capped at `limit`.

    Used by `UnifiedDashboard` to celebrate worker wins. Kept
    intentionally minimal: just enough fields to render a compact
    line, with `seconds_ago` precomputed server-side so the widget
    isn't tied to client clock accuracy.
    """
    _validate_source(source)
    _require_amazon_shape(source)
    now = time.time()
    cutoff = now - (hours * 3600)
    bt = metadata_cache.books_table(source)
    db = await metadata_cache.get_db(source)
    try:
        cur = await db.execute(
            f"SELECT author_id, library_slug, book_asin, title, "
            f"series_name, series_pos, cached_at "
            f"FROM {bt} "
            f"WHERE cached_at >= ? "
            f"ORDER BY cached_at DESC LIMIT ?",
            (cutoff, limit),
        )
        rows = await cur.fetchall()
    finally:
        await db.close()
    discoveries = [
        RecentDiscoveryRow(
            author_id=row["author_id"],
            library_slug=row["library_slug"],
            book_asin=row["book_asin"],
            title=row["title"] or "",
            series_name=row["series_name"],
            series_pos=row["series_pos"],
            cached_at=float(row["cached_at"]),
            seconds_ago=now - float(row["cached_at"]),
        )
        for row in rows
    ]
    return RecentDiscoveriesResponse(
        source=source, window_hours=hours, discoveries=discoveries,
    )


@router.get(
    "/{source}/author/{author_id}",
    response_model=AuthorCacheResponse,
)
async def get_author_cache_state(
    source: str, author_id: str,
) -> AuthorCacheResponse:
    """Per-author cache state across every library that's seen this
    author ID.

    Used by the author detail page's per-author cache badges to
    render a single human-readable line summarizing whether this
    specific author has been scanned, when, and how many books /
    list pages are cached.

    Source-branched: Amazon projects per-book detail (`books` table);
    Goodreads projects list-page snapshots (`list_pages` table) per
    ADR-0018 §1 (Path B = list-page only). Both share `state` and
    `queue` columns from the source-agnostic state/queue tables, so
    the same per-library scaffold serves both shapes.
    """
    _validate_source(source)
    cooldown = _cooldown_state(source)
    is_gr = source == metadata_cache.SOURCE_GOODREADS

    db = await metadata_cache.get_db(source)
    try:
        st_table = metadata_cache.state_table(source)
        q_table = metadata_cache.queue_table(source)
        cur = await db.execute(
            f"SELECT library_slug, last_scanned_at, last_outcome, "
            f"book_count FROM {st_table} WHERE author_id = ?",
            (author_id,),
        )
        state_rows = await cur.fetchall()
        cur = await db.execute(
            f"SELECT status, priority, next_scan_due_at, "
            f"consecutive_failures FROM {q_table} WHERE author_id = ?",
            (author_id,),
        )
        queue_row = await cur.fetchone()

        list_pages_by_slug: dict[str, list[ListPageEntry]] = {}
        if is_gr:
            lp_table = metadata_cache.list_pages_table(source)
            cur = await db.execute(
                f"SELECT library_slug, page_num, fetched_at, "
                f"book_ids_json FROM {lp_table} "
                f"WHERE author_id = ? ORDER BY library_slug, page_num",
                (author_id,),
            )
            import json as _json
            for row in await cur.fetchall():
                try:
                    ids = _json.loads(row["book_ids_json"]) or []
                except Exception:
                    ids = []
                list_pages_by_slug.setdefault(row["library_slug"], []).append(
                    ListPageEntry(
                        page_num=row["page_num"],
                        fetched_at=row["fetched_at"],
                        book_count=len(ids),
                    )
                )
    finally:
        await db.close()

    queue_info: Optional[dict] = None
    if queue_row is not None:
        queue_info = {
            "status": queue_row["status"],
            "priority": queue_row["priority"],
            "next_scan_due_at": queue_row["next_scan_due_at"],
            "consecutive_failures": queue_row["consecutive_failures"],
        }

    by_slug: dict[str, dict[str, Any]] = {}
    for row in state_rows:
        by_slug[row["library_slug"]] = {
            "state": {
                "last_scanned_at": row["last_scanned_at"],
                "last_outcome": row["last_outcome"],
                "book_count": row["book_count"],
            },
            "queue": queue_info,
            "list_pages": list_pages_by_slug.get(row["library_slug"]),
        }

    if not by_slug and queue_info is not None:
        # No state rows yet — synthesize one "in queue" line per
        # library this author belongs to via `_libraries_for_author`.
        from app.discovery.metadata_cache_worker import _libraries_for_author
        try:
            libs = await _libraries_for_author(author_id, source)
        except Exception:
            libs = []
        for lib in libs:
            by_slug[lib["slug"]] = {
                "state": None, "queue": queue_info, "list_pages": None,
            }

    libraries = [
        AuthorCacheStateRow(
            library_slug=slug,
            state=entry["state"],
            queue=entry["queue"],
            list_pages=entry.get("list_pages"),
        )
        for slug, entry in sorted(by_slug.items())
    ]
    return AuthorCacheResponse(
        source=source,
        amazon_author_id="" if is_gr else author_id,
        author_id=author_id,
        libraries=libraries,
        cooldown=cooldown,
    )


@router.post("/{source}/reset-cooldown", response_model=ResetCooldownResponse)
async def reset_cooldown(source: str) -> ResetCooldownResponse:
    """Emergency override — clear the IP-level penalty box.

    Use case: the operator has manually verified Amazon is reachable
    (different IP, VPN switched off, Akamai's bot-score decayed) and
    wants to retry now rather than wait for the timer to clear. Logs
    loudly because a misuse could trigger a fresh 429 cascade — but
    sometimes the cooldown holds longer than it needs to (a stale
    block from a previous IP, for example) and immediate intervention
    is the right call.

    Today this only resets the in-process module-state cooldown +
    the settings.json mirror added in v2.20.3. The worker_state row's
    `consecutive_blocks` is left alone so escalation tier history
    survives across the reset — a second block within the 1h window
    will still escalate to tier 2.
    """
    _validate_source(source)
    if source != metadata_cache.SOURCE_AMAZON:
        raise HTTPException(
            400, "reset-cooldown only applies to the amazon source today",
        )
    from app.discovery import amazon_author_id_resolver as r
    previously_blocked = is_amazon_blocked()
    previous_remaining = amazon_block_remaining_s()
    r._blocked_until = 0.0          # type: ignore[attr-defined]
    r._block_reason = ""            # type: ignore[attr-defined]
    # Persist the clear so a container restart doesn't restore the
    # cooldown via v2.20.3's `_load_persisted_block_state`.
    try:
        s = dict(load_settings())
        s["amazon_blocked_until"] = 0.0
        s["amazon_block_reason"] = ""
        s["amazon_blocked_since"] = None
        save_settings(s)
    except Exception:
        _log.exception(
            "metadata_cache reset-cooldown: failed to persist clear "
            "to settings (in-memory cooldown cleared regardless)"
        )
    _log.warning(
        "metadata_cache reset-cooldown: operator cleared amazon cooldown "
        "(was blocked=%s, %.0fs remaining); next scan will hit Amazon live",
        previously_blocked, previous_remaining,
    )
    return ResetCooldownResponse(
        ok=True, source=source,
        previously_blocked=previously_blocked,
        previous_remaining_s=previous_remaining,
    )
