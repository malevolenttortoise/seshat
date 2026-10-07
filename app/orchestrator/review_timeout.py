"""
Review-queue auto-add timeout job.

When a book has been sitting in the manual review queue for longer
than `metadata_review_timeout_days`, ship it to the configured sink
with whatever metadata we already have (announce + embedded). The
user's time to object has expired — losing the book entirely is a
worse failure mode than shipping it with imperfect metadata.

This module exposes:
  - `tick()`   — one pass, safe to call on demand (used by tests)
  - `run_loop()` — supervised-task wrapper for the main.py lifespan

The tick runs once per day by default; the configurable interval
is there so tests can drive it in milliseconds.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from app.database import get_db
from app.orchestrator.dispatch import DispatcherDeps
from app.orchestrator.pipeline import deliver_reviewed
from app.storage import review_queue as review_storage

_log = logging.getLogger("seshat.orchestrator.review_timeout")


async def tick(deps: DispatcherDeps) -> int:
    """Process one pass of timed-out review queue items.

    Returns the number of items that were successfully auto-delivered.
    """
    if not deps.review_queue_enabled:
        return 0

    grace_days = max(1, int(deps.metadata_review_timeout_days))
    delivered = 0

    db = await get_db()
    try:
        stale = await review_storage.list_stale_pending(
            db, older_than_days=grace_days
        )
        if not stale:
            return 0

        _log.info(
            "review_timeout: %d item(s) past %d-day grace period",
            len(stale), grace_days,
        )

        for row in stale:
            try:
                ok = await deliver_reviewed(
                    db,
                    review_id=row.id,
                    default_sink=deps.default_sink,
                    calibre_library_path=deps.calibre_library_path,
                    folder_sink_path=deps.folder_sink_path,
                    audiobookshelf_library_path=deps.audiobookshelf_library_path,
                    abs_base_url=deps.abs_base_url,
                    abs_api_key=deps.abs_api_key,
                    abs_library_id=deps.abs_library_id,
                    cwa_ingest_path=deps.cwa_ingest_path,
                    cwa_min_inter_book_seconds=deps.cwa_min_inter_book_seconds,
                    ntfy_url=deps.ntfy_url,
                    ntfy_topic=deps.ntfy_topic,
                    auto_train_enabled=deps.auto_train_enabled,
                    per_event_notifications=deps.per_event_notifications,
                    was_timeout=True,
                )
                if ok:
                    delivered += 1
            except Exception:
                _log.exception(
                    "review_timeout: unexpected error on review_id=%d",
                    row.id,
                )

        # Also retry items where the sink was unreachable on previous
        # delivery attempts. These are books that were approved (or
        # timed out) but CWA/Calibre was down when we tried to deliver.
        sink_pending = await review_storage.list_sink_pending(db)
        if sink_pending:
            _log.info(
                "review_timeout: retrying %d sink-pending item(s)",
                len(sink_pending),
            )
            for row in sink_pending:
                try:
                    ok = await deliver_reviewed(
                        db,
                        review_id=row.id,
                        default_sink=deps.default_sink,
                        calibre_library_path=deps.calibre_library_path,
                        folder_sink_path=deps.folder_sink_path,
                        audiobookshelf_library_path=deps.audiobookshelf_library_path,
                        cwa_ingest_path=deps.cwa_ingest_path,
                        cwa_min_inter_book_seconds=deps.cwa_min_inter_book_seconds,
                        ntfy_url=deps.ntfy_url,
                        ntfy_topic=deps.ntfy_topic,
                        auto_train_enabled=deps.auto_train_enabled,
                        was_timeout=False,
                    )
                    if ok:
                        delivered += 1
                except Exception:
                    _log.exception(
                        "review_timeout: sink retry failed for review_id=%d",
                        row.id,
                    )

        return delivered
    finally:
        await db.close()


async def run_loop(
    get_deps: Callable[[], Optional[DispatcherDeps]],
    *,
    interval_seconds: float,
) -> None:
    """Supervised loop: tick every `interval_seconds`, never raise.

    `get_deps` is called at the start of every tick (main.py passes
    `lambda: state.dispatcher`), so a settings save that rebuilds the
    dispatcher reaches the next tick. None (shutdown) skips the tick.
    """
    _log.info(
        "review_timeout loop starting (interval=%.0fs, grace=%d days)",
        interval_seconds,
        getattr(get_deps(), "metadata_review_timeout_days", 14),
    )
    while True:
        try:
            deps = get_deps()
            if deps is not None:
                await tick(deps)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("review_timeout tick crashed (non-fatal)")
        await asyncio.sleep(interval_seconds)
