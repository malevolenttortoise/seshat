"""Refresh spread for both cache workers (2026-10 audit wave 4 S2: A-B, G70).

Authors scanned together came due together, so each worker re-swept its
whole queue in one burst every 7 days (Amazon: 689 authors on 10-06/07,
ending in Akamai blocks). Now each reschedule lands 7 days ± up to a day,
and routine refreshes stop for the day at rows ÷ 7 × 1.25; authors the
worker has never attempted skip the cap."""
from __future__ import annotations

import time

import pytest

from app.discovery import metadata_cache, metadata_cache_worker
from tests.discovery.test_metadata_cache_worker import (  # noqa: F401 (fixtures)
    _author_result, _seed_gr_queue_row, _seed_queue_row,
    gr_worker_under, worker_under,
)

DAY = 24 * 3600.0


async def _set_attempted(source, author_id, at):
    db = await metadata_cache.get_db(source)
    try:
        await db.execute(
            f"UPDATE {metadata_cache.queue_table(source)} "
            f"SET last_attempt_at = ? WHERE author_id = ?",
            (at, author_id),
        )
        await db.commit()
    finally:
        await db.close()


async def _due_at(source, author_id):
    db = await metadata_cache.get_db(source)
    try:
        cur = await db.execute(
            f"SELECT next_scan_due_at FROM {metadata_cache.queue_table(source)} "
            f"WHERE author_id = ?", (author_id,),
        )
        return (await cur.fetchone())[0]
    finally:
        await db.close()


def test_a_reschedule_lands_seven_days_out_plus_or_minus_a_day(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(metadata_cache_worker.random, "uniform", lambda lo, hi: lo)
    assert metadata_cache_worker._next_refresh_due(now) == now + 6 * DAY
    monkeypatch.setattr(metadata_cache_worker.random, "uniform", lambda lo, hi: hi)
    assert metadata_cache_worker._next_refresh_due(now) == now + 8 * DAY


async def test_the_cap_is_rows_over_seven_with_headroom(worker_under):
    for i in range(30):
        await _seed_queue_row(author_id=f"B0CAP{i:05d}", seed_in_libraries=())
    db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
    try:
        attempted, cap = await metadata_cache_worker.refresh_cap_state(
            db, metadata_cache.SOURCE_AMAZON, time.time(),
        )
    finally:
        await db.close()
    assert (attempted, cap) == (0, 6)          # ceil(30 / 7 × 1.25)


async def _seed_capped_amazon_queue():
    """14 rows → cap 3. Three attempted today (cap used), the rest last
    attempted a week ago and due now."""
    now = time.time()
    for i in range(14):
        await _seed_queue_row(author_id=f"B0ROUT{i:04d}")
        await _set_attempted(
            metadata_cache.SOURCE_AMAZON, f"B0ROUT{i:04d}",
            now - 5.0 if i < 3 else now - 7 * DAY,
        )
    return now


async def test_routine_refreshes_stop_at_the_cap(worker_under, monkeypatch):
    """The dangerous call: no Amazon scan once today's cap is used."""
    await _seed_capped_amazon_queue()
    scans: list[str] = []

    async def _scan(author_id, session):
        scans.append(author_id)
        return _author_result("X", author_id=author_id), None
    monkeypatch.setattr(metadata_cache_worker, "_perform_amazon_scan", _scan)

    result = await metadata_cache_worker.tick()
    assert result.outcome == "daily_cap"
    assert scans == []


async def test_a_new_author_skips_the_cap(worker_under, monkeypatch):
    await _seed_capped_amazon_queue()
    await _seed_queue_row(author_id="B0NEWAUTH1", priority=1000.0,
                          enqueued_reason="lookup_miss")
    scans: list[str] = []

    async def _scan(author_id, session):
        scans.append(author_id)
        return _author_result("X", author_id=author_id), None
    monkeypatch.setattr(metadata_cache_worker, "_perform_amazon_scan", _scan)

    result = await metadata_cache_worker.tick()
    assert scans == ["B0NEWAUTH1"]
    assert result.outcome in ("ok", "ok_empty")


async def test_yesterdays_attempts_dont_count(worker_under, monkeypatch):
    now = await _seed_capped_amazon_queue()
    day_start = metadata_cache_worker._local_day_start(
        metadata_cache.SOURCE_AMAZON, now,
    )
    for i in range(3):
        await _set_attempted(metadata_cache.SOURCE_AMAZON, f"B0ROUT{i:04d}",
                             day_start - 60.0)
    scans: list[str] = []

    async def _scan(author_id, session):
        scans.append(author_id)
        return _author_result("X", author_id=author_id), None
    monkeypatch.setattr(metadata_cache_worker, "_perform_amazon_scan", _scan)

    await metadata_cache_worker.tick()
    assert len(scans) == 1


async def test_nothing_due_is_still_queue_empty(worker_under):
    now = time.time()
    await _seed_queue_row(author_id="B0LATER001", next_scan_due_at=now + DAY)
    await _set_attempted(metadata_cache.SOURCE_AMAZON, "B0LATER001", now - 5.0)
    result = await metadata_cache_worker.tick()
    assert result.outcome == "queue_empty"


async def test_a_scanned_author_is_due_six_to_eight_days_later(worker_under, monkeypatch):
    await _seed_queue_row(author_id="B0SCANNED1")

    async def _scan(author_id, session):
        return _author_result("X", author_id=author_id), None
    monkeypatch.setattr(metadata_cache_worker, "_perform_amazon_scan", _scan)

    before = time.time()
    await metadata_cache_worker.tick()
    due = await _due_at(metadata_cache.SOURCE_AMAZON, "B0SCANNED1")
    assert before + 6 * DAY - 5 <= due <= time.time() + 8 * DAY


async def test_the_goodreads_worker_has_the_same_cap(gr_worker_under, monkeypatch):
    now = time.time()
    for i in range(14):
        await _seed_gr_queue_row(author_id=f"GR-{i}")
        await _set_attempted(
            metadata_cache.SOURCE_GOODREADS, f"GR-{i}",
            now - 5.0 if i < 3 else now - 7 * DAY,
        )
    scans: list[str] = []

    async def _scan(author_id):
        scans.append(author_id)
        return ({1: [{"book_id": "b1", "title": "B1"}]}, None, False, False)
    monkeypatch.setattr(metadata_cache_worker, "_perform_goodreads_scan", _scan)

    result = await metadata_cache_worker.tick_goodreads()
    assert result.outcome == "daily_cap"
    assert scans == []


async def test_the_status_reports_the_cap(worker_under):
    from app.routers.metadata_cache import get_status

    await _seed_capped_amazon_queue()
    status = await get_status("amazon")
    assert (status.queue.refreshed_today, status.queue.daily_cap) == (3, 3)


async def test_the_goodreads_worker_waits_out_a_list_page_backoff(gr_worker_under, monkeypatch):
    """No list-page request while list pages back off after a block (the
    worker never checked Goodreads' block state before the 2026-10 audit)."""
    from app.metadata import goodreads_session

    await _seed_gr_queue_row(author_id="GR-1")
    goodreads_session._record_block("list_page", 202)
    scans: list[str] = []

    async def _scan(author_id):
        scans.append(author_id)
        return ({1: []}, None, False, False)
    monkeypatch.setattr(metadata_cache_worker, "_perform_goodreads_scan", _scan)

    result = await metadata_cache_worker.tick_goodreads()
    assert result.outcome == "cooldown"
    assert 0 < result.cooldown_remaining_s <= 120
    assert scans == []


async def test_a_refused_list_page_is_a_deferral_not_a_failure(gr_worker_under, monkeypatch):
    """`GoodreadsBackingOff` from the scan must not climb the failure
    ladder toward failed_permanent."""
    from app.discovery.sources import goodreads as gr_source
    from app.metadata.goodreads_session import GoodreadsBackingOff

    async def _refused(self, author_id, **_):
        raise GoodreadsBackingOff("list_page", time.time() + 60)
    monkeypatch.setattr(gr_source.GoodreadsSource, "list_page_inventory", _refused)

    pages, err, soft, hard_404 = await metadata_cache_worker._perform_goodreads_scan("GR-2")
    assert (pages, soft, hard_404) == (None, True, False)

