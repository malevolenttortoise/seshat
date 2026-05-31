"""
APScheduler helpers for digest jobs.

Exposes `register_digest_jobs(scheduler, ...)` which adds three
cron-style jobs onto a caller-owned AsyncIOScheduler:

  - `daily_digest` — fires at `daily_digest_hour` local time every day
  - `weekly_digest` — fires Sundays at 23:30 local time
  - `weekly_calibre_audit` — fires Sundays at 22:30 local time (one
    hour before the weekly digest so any discrepancies surface in the
    same window the user reviews). Skipped when ctx.calibre_library_path
    is empty — the job coroutine no-ops early.

The scheduler itself is owned by `main.py`'s lifespan, which also
registers the discovery-domain interval jobs (library sync + author
lookup) onto the same instance. All jobs wrap their coroutines in
broad exception handling so a crash in one job doesn't kill the
scheduler thread.

Why APScheduler vs a hand-rolled asyncio loop for these jobs: digest
cadence is measured in hours and days and cron-style "fire at 9am every
day" is awkward to express without APScheduler's trigger classes. The
cookie keep-alive and review-timeout loops, by contrast, are pure
intervals and live as plain supervised_task coroutines.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.notify.digests import (
    DigestContext, run_daily, run_weekly, run_calibre_audit,
)

_log = logging.getLogger("seshat.orchestrator.scheduler")


def register_digest_jobs(
    scheduler: AsyncIOScheduler,
    *,
    daily_digest_hour: int,
    ctx: DigestContext,
) -> None:
    """Register the daily / weekly digest + audit jobs onto an existing scheduler.

    Split out of `build_scheduler` so callers that already own an
    AsyncIOScheduler (for example, main.py also registering discovery
    jobs on the same scheduler) can add digest jobs without constructing
    a second scheduler instance.
    """
    async def _daily_job():
        _log.info("daily digest tick")
        try:
            await run_daily(ctx)
        except Exception:
            _log.exception("daily digest crashed")

    async def _weekly_job():
        _log.info("weekly digest tick")
        try:
            await run_weekly(ctx)
        except Exception:
            _log.exception("weekly digest crashed")

    async def _calibre_audit_job():
        _log.info("weekly Calibre audit tick")
        try:
            await run_calibre_audit(ctx)
        except Exception:
            _log.exception("weekly Calibre audit crashed")

    scheduler.add_job(
        _daily_job,
        trigger=CronTrigger(hour=int(daily_digest_hour), minute=0),
        id="daily_digest",
        name="Daily digest",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _weekly_job,
        trigger=CronTrigger(day_of_week="sun", hour=23, minute=30),
        id="weekly_digest",
        name="Weekly digest",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    # Fire the audit an hour before the weekly digest so any
    # discrepancies show up in the same review window. Job itself
    # no-ops when ctx.calibre_library_path is empty.
    scheduler.add_job(
        _calibre_audit_job,
        trigger=CronTrigger(day_of_week="sun", hour=22, minute=30),
        id="weekly_calibre_audit",
        name="Weekly Calibre audit",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    if not ctx.calibre_library_path:
        _log.info(
            "weekly_calibre_audit: disabled (no calibre_library_path configured); "
            "job will be a no-op"
        )


# ─── v2.13.0 Stage 6: Goodreads session canary ───────────────────────


def register_goodreads_canary(scheduler: AsyncIOScheduler) -> None:
    """Register the weekly Goodreads-session canary onto the scheduler.

    Fires Mondays at 03:00 local. Does one probe-style fetch through
    the production `goodreads_session` module against a known-stable
    book (The Hobbit, id=5907). Two outcomes:

      - 200 with body → silently mark the session "active" (in case
        a prior soft-block was sitting in state and the user hasn't
        noticed). Also prunes expired id_cache rows.
      - 202 / soft-block → flip state to "soft_blocked" (the session
        module does this automatically inside `get()`) AND emit a
        ntfy notification (gated on `notify_on_goodreads_canary_failed`).

    The canary is a passive observer — it doesn't try to recover or
    rotate credentials. Recovery is user-driven: see the
    GoodreadsStatusCard's "Run probe" / "Mark as active" buttons.

    Cadence: weekly is the design's "we don't expect Cloudflare to
    flip mid-week if cookies aren't part of the bypass" cadence.
    Phase B may tighten to daily once cookies enter the mix
    (cf_clearance typically lasts hours-to-days).
    """
    async def _canary():
        from app.metadata import goodreads_session, id_cache
        from app.notifications import bus, events

        _log.info("goodreads canary tick")
        try:
            session = await goodreads_session.get_session()
            resp = await session.get("https://www.goodreads.com/book/show/5907")
            soft_blocked = goodreads_session.is_cloudflare_soft_block(resp)
        except Exception:
            _log.exception("goodreads canary fetch crashed")
            return

        # Side effect: prune expired id_cache rows. Cheap, weekly is fine.
        try:
            id_cache.prune_expired()
        except Exception:
            _log.exception("goodreads canary: id_cache prune failed (non-fatal)")

        if not soft_blocked:
            _log.info(
                "goodreads canary: 200 OK (state=active)",
            )
            return

        # Soft-block detected — bus.emit handles the enable gate,
        # ntfy config check, topic routing, quiet hours, and exception
        # swallowing. Any "not sent" outcome is silent by design.
        try:
            await bus.emit(
                events.SOURCE_GOODREADS_CANARY_FAILED,
                title="Goodreads soft-blocked",
                message=(
                    "Weekly canary detected a Cloudflare soft-block. "
                    "Open Settings > Sources > Goodreads and run a probe "
                    "to confirm + investigate. Discovery scans will skip "
                    "Goodreads until the session state is marked active."
                ),
            )
        except Exception:
            _log.exception("goodreads canary ntfy send failed (non-fatal)")

    scheduler.add_job(
        _canary,
        trigger=CronTrigger(day_of_week="mon", hour=3, minute=0),
        id="goodreads_canary",
        name="Goodreads session canary",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )


# ─── v2.21.0 Phase G: metadata-cache health watchdog + daily summary ──


def register_metadata_cache_health(
    scheduler: AsyncIOScheduler,
    *,
    daily_summary_hour: int,
) -> None:
    """Register the metadata-cache worker watchdog + daily-summary jobs.

    Two jobs land on the caller's scheduler:

      - `metadata_cache_amazon_stall_watch` — every 2 min, calls
        `check_stall(source_name="amazon")`. Fires the Tier-1 error
        ntfy if the worker is enabled but its heartbeat hasn't been
        updated within `metadata_cache_stall_threshold_s` (default
        300s). Self-debounces via a settings runtime-state key.

      - `metadata_cache_amazon_daily_summary` — fires once per day at
        the user's configured hour. Aggregates today's scan + block
        counts, sends the Tier-3 info ntfy (if opted-in), and zeroes
        the `today_*` columns so the UI numbers are for-today rather
        than monotonic-since-deploy.

    Source-name-agnostic by intent — a future Goodreads cache adds
    its own pair of jobs (different id) without touching this
    helper's signature.
    """
    from apscheduler.triggers.interval import IntervalTrigger

    async def _stall_watch():
        from app.discovery import metadata_cache_worker
        try:
            await metadata_cache_worker.check_stall("amazon")
        except Exception:
            _log.exception("metadata_cache_amazon_stall_watch crashed")

    async def _daily_summary():
        from app.discovery import metadata_cache_worker
        _log.info("metadata_cache amazon daily summary tick")
        try:
            await metadata_cache_worker.send_daily_summary("amazon")
        except Exception:
            _log.exception(
                "metadata_cache_amazon_daily_summary crashed"
            )

    scheduler.add_job(
        _stall_watch,
        trigger=IntervalTrigger(minutes=2),
        id="metadata_cache_amazon_stall_watch",
        name="Amazon metadata-cache stall watchdog",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _daily_summary,
        trigger=CronTrigger(hour=int(daily_summary_hour), minute=5),
        id="metadata_cache_amazon_daily_summary",
        name="Amazon metadata-cache daily summary",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # v3.4.0 slice 05 — sibling pair for the Goodreads worker. Same
    # cadence (2-min stall watch + daily summary at user-configured
    # hour) reusing the source-agnostic `check_stall` / `send_daily_summary`
    # primitives. Separate job IDs so APScheduler can hold both
    # without collision. The daily summary cron is intentionally
    # offset by 1 minute so the two source summaries don't fire
    # simultaneously (small ntfy-quality nicety).
    async def _gr_stall_watch():
        from app.discovery import metadata_cache_worker
        try:
            await metadata_cache_worker.check_stall("goodreads")
        except Exception:
            _log.exception("metadata_cache_goodreads_stall_watch crashed")

    async def _gr_daily_summary():
        from app.discovery import metadata_cache_worker
        _log.info("metadata_cache goodreads daily summary tick")
        try:
            await metadata_cache_worker.send_daily_summary("goodreads")
        except Exception:
            _log.exception(
                "metadata_cache_goodreads_daily_summary crashed"
            )

    scheduler.add_job(
        _gr_stall_watch,
        trigger=IntervalTrigger(minutes=2),
        id="metadata_cache_goodreads_stall_watch",
        name="Goodreads metadata-cache stall watchdog",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _gr_daily_summary,
        trigger=CronTrigger(hour=int(daily_summary_hour), minute=6),
        id="metadata_cache_goodreads_daily_summary",
        name="Goodreads metadata-cache daily summary",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
