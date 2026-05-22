"""
v2.21.0 Phase D — background worker tests.

Covers `tick()` end-to-end across every outcome branch with mocked
`_perform_amazon_scan` so the test runs offline. Also covers:

  - `recover_stuck_in_progress` — startup recovery
  - cooldown escalation tier selection
  - queue pop ordering (priority + due_at)
  - worker_state heartbeat / scan counters

`run_loop` itself is covered by a one-tick integration test that
uses `stop_event` to break the loop cleanly.
"""
from __future__ import annotations

import time

import pytest

from app import state
from app.discovery import metadata_cache, metadata_cache_worker
from app.discovery.database import set_active_library
from app.discovery.sources.base import AuthorResult, BookResult, SeriesResult


# ─── Fixtures ───────────────────────────────────────────────────


@pytest.fixture
async def worker_under(tmp_path, monkeypatch):
    """Empty cache DB + two-library discovery state + Amazon settings.

    Mocks `_perform_amazon_scan` to a no-op by default; individual
    tests override the mock to drive specific scan outcomes. Always
    leaves `metadata_cache.amazon.enabled` ON for the test (the
    real-world default is OFF; we flip it on so tick() doesn't
    short-circuit).

    Also stubs the persistence path of `record_amazon_soft_block` so
    a soft-block test doesn't write to the real settings.json.
    """
    from app import config as app_config
    from app.discovery import database as disco_db
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(metadata_cache, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)

    # Settings: enable the worker, default amazon source config.
    fake_settings = {
        "metadata_cache": {"amazon": {"enabled": True}},
        "metadata_sources": {
            "amazon": {
                "format": "kindle",
                "audiobook_format": "audible_audiobook",
                "language": "English",
            },
        },
    }
    monkeypatch.setattr(
        app_config, "load_settings", lambda: dict(fake_settings),
    )
    monkeypatch.setattr(
        app_config, "save_settings", lambda d: fake_settings.update(d),
    )

    # Mute the v2.20.3 cooldown persistence path so tests don't write
    # to real settings.json on soft-block triggers.
    from app.discovery import amazon_author_id_resolver as resolver_module
    monkeypatch.setattr(
        resolver_module, "_persist_block_state", lambda **_: None,
    )
    resolver_module._blocked_until = 0.0
    resolver_module._block_reason = ""
    resolver_module._block_count = 0

    # Pretend curl_cffi exists; tick() only uses the session as an
    # opaque handle that gets passed to `_perform_amazon_scan` (which
    # the tests monkey-patch).
    class _FakeSession:
        async def get(self, *_a, **_kw):
            class _R:
                status_code = 200
            return _R()
        async def close(self):
            return None
    monkeypatch.setattr(
        metadata_cache_worker, "_create_session", lambda: _FakeSession(),
    )

    # Two-library discovery state.
    monkeypatch.setattr(
        state, "_discovered_libraries",
        [
            {"slug": "books-lib", "name": "Books",
             "content_type": "ebook",
             "source_db_path": "/x", "library_path": "/x"},
            {"slug": "audio-lib", "name": "Audio",
             "content_type": "audiobook",
             "source_db_path": "/y", "library_path": "/y"},
        ],
    )

    await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
    prev_lib = disco_db.get_active_library()
    set_active_library("books-lib")

    yield {
        "tmp_path": tmp_path,
        "settings": fake_settings,
        "monkeypatch": monkeypatch,
    }

    set_active_library(prev_lib)
    resolver_module._blocked_until = 0.0
    resolver_module._block_reason = ""
    resolver_module._block_count = 0


async def _seed_queue_row(
    *,
    author_id: str,
    library_slug: str,
    seshat_author_id: int = 1,
    priority: float = 100.0,
    status: str = "pending",
    next_scan_due_at: float = 0.0,
    consecutive_failures: int = 0,
    enqueued_reason: str = "test_seed",
) -> None:
    db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
    try:
        await db.execute(
            f"INSERT OR REPLACE INTO {metadata_cache.queue_table()} "
            f"(author_id, library_slug, seshat_author_id, priority, "
            f" status, next_scan_due_at, consecutive_failures, "
            f" enqueued_reason) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (author_id, library_slug, seshat_author_id, priority,
             status, next_scan_due_at, consecutive_failures,
             enqueued_reason),
        )
        await db.commit()
    finally:
        await db.close()


def _author_result(*titles: str, author_id: str = "B0AAAAAAAA") -> AuthorResult:
    """Build a flat AuthorResult with one BookResult per title."""
    books = [
        BookResult(
            title=t,
            external_id=f"B0{i:08d}",
            source="amazon",
            language="English",
        )
        for i, t in enumerate(titles, start=1)
    ]
    return AuthorResult(
        name=author_id, external_id=author_id, books=books, series=[],
    )


# ─── Settings gate ──────────────────────────────────────────────


class TestDisabledGate:
    async def test_disabled_returns_disabled_outcome(self, worker_under):
        worker_under["settings"]["metadata_cache"]["amazon"]["enabled"] = False
        result = await metadata_cache_worker.tick()
        assert result.outcome == "disabled"
        # Idle sleep — we don't blast tight loops while disabled.
        assert result.next_sleep_s >= 60

    async def test_is_worker_enabled_reads_settings(self, worker_under):
        assert metadata_cache_worker.is_worker_enabled("amazon") is True
        worker_under["settings"]["metadata_cache"]["amazon"]["enabled"] = False
        assert metadata_cache_worker.is_worker_enabled("amazon") is False


# ─── No-libraries gate ─────────────────────────────────────────


class TestNoLibrariesGate:
    async def test_empty_library_list_returns_no_libraries(
        self, worker_under, monkeypatch,
    ):
        monkeypatch.setattr(state, "_discovered_libraries", [])
        result = await metadata_cache_worker.tick()
        assert result.outcome == "no_libraries"


# ─── Cooldown gate ─────────────────────────────────────────────


class TestCooldownGate:
    async def test_active_cooldown_skips_pop(self, worker_under):
        from app.discovery import amazon_author_id_resolver as r
        # Arm a 120s cooldown so the worker should skip the tick.
        r.record_amazon_soft_block("test", retry_after_s=120)
        await _seed_queue_row(
            author_id="B0COOLDOWN", library_slug="books-lib",
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "cooldown"
        # next_sleep_s lines up with the cooldown remaining (~120s).
        assert 100 <= result.next_sleep_s <= 130
        # Queue row was not popped — still pending.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT status FROM {metadata_cache.queue_table()} "
                f"WHERE author_id = ?",
                ("B0COOLDOWN",),
            )
            status = (await cur.fetchone())[0]
        finally:
            await db.close()
        assert status == "pending"


# ─── Empty queue ───────────────────────────────────────────────


class TestQueueEmpty:
    async def test_empty_queue_returns_queue_empty(self, worker_under):
        result = await metadata_cache_worker.tick()
        assert result.outcome == "queue_empty"


# ─── Successful scan ───────────────────────────────────────────


class TestSuccessfulScan:
    async def test_successful_scan_writes_cache_and_advances_queue(
        self, worker_under, monkeypatch,
    ):
        async def _fake_scan(author_id, library_slug, session):
            return _author_result(
                "Book One", "Book Two", "Book Three", author_id=author_id,
            ), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0TESTSCAN", library_slug="books-lib",
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "ok"
        assert result.books_cached == 3
        assert result.author_id == "B0TESTSCAN"
        # State row written.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT last_outcome, book_count FROM "
                f"{metadata_cache.state_table()} "
                f"WHERE author_id = ?",
                ("B0TESTSCAN",),
            )
            srow = await cur.fetchone()
            cur = await db.execute(
                f"SELECT title, format FROM {metadata_cache.books_table()} "
                f"WHERE author_id = ? ORDER BY title",
                ("B0TESTSCAN",),
            )
            book_rows = await cur.fetchall()
            cur = await db.execute(
                f"SELECT status, consecutive_failures, next_scan_due_at "
                f"FROM {metadata_cache.queue_table()} "
                f"WHERE author_id = ?",
                ("B0TESTSCAN",),
            )
            qrow = await cur.fetchone()
            cur = await db.execute(
                f"SELECT today_scan_count, last_scan_completed_at "
                f"FROM {metadata_cache.worker_state_table()} "
                f"WHERE id = 1"
            )
            wrow = await cur.fetchone()
        finally:
            await db.close()
        # State row reflects success.
        assert srow[0] == "ok"
        assert srow[1] == 3
        # Books written with the binding-symbol format from
        # FILTER_TO_BINDING (kindle -> kindle_edition).
        assert len(book_rows) == 3
        assert all(b[1] == "kindle_edition" for b in book_rows)
        # Queue row deferred forward (next_scan_due_at > now), still
        # pending, failure counter reset.
        assert qrow[0] == "pending"
        assert qrow[1] == 0
        assert qrow[2] > time.time() + 60  # well into the future
        # Worker state recorded the scan.
        assert wrow[0] == 1
        assert wrow[1] is not None

    async def test_audiobook_library_uses_audiobook_format(
        self, worker_under, monkeypatch,
    ):
        async def _fake_scan(author_id, library_slug, session):
            return _author_result("Audiobook One", author_id=author_id), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0AUDIO0001", library_slug="audio-lib",
        )
        await metadata_cache_worker.tick()
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT format FROM {metadata_cache.books_table()} "
                f"WHERE author_id = ?",
                ("B0AUDIO0001",),
            )
            fmt = (await cur.fetchone())[0]
        finally:
            await db.close()
        # audible_audiobook (filter) → audio_download (binding).
        assert fmt == "audio_download"


# ─── Empty-result scan ─────────────────────────────────────────


class TestEmptyResultScan:
    async def test_scan_with_zero_books_returns_ok_empty(
        self, worker_under, monkeypatch,
    ):
        async def _fake_scan(author_id, library_slug, session):
            return _author_result(author_id=author_id), None  # 0 books
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0NOBOOKS01", library_slug="books-lib",
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "ok_empty"
        assert result.books_cached == 0
        # State row still recorded so the reader sees a hit + empty.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT last_outcome, book_count FROM "
                f"{metadata_cache.state_table()} "
                f"WHERE author_id = ?",
                ("B0NOBOOKS01",),
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == "ok"
        assert row[1] == 0


# ─── Soft-block path ───────────────────────────────────────────


class TestSoftBlockPath:
    async def test_scan_that_trips_cooldown_records_softblock(
        self, worker_under, monkeypatch,
    ):
        async def _fake_scan(author_id, library_slug, session):
            # Simulate `AmazonSource` recording a soft-block during the
            # scan (HTTP 429 / 202 / thin body / no-ProductGrid path).
            from app.discovery import amazon_author_id_resolver as r
            r.record_amazon_soft_block(
                "fake 429 during test", retry_after_s=600,
            )
            return None, "HTTP 429 (fake)"
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0SOFTBLK1", library_slug="books-lib",
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "soft_block"
        # Queue row deferred past the cooldown — NOT a failure.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT status, consecutive_failures, next_scan_due_at "
                f"FROM {metadata_cache.queue_table()} "
                f"WHERE author_id = ?",
                ("B0SOFTBLK1",),
            )
            qrow = await cur.fetchone()
            cur = await db.execute(
                f"SELECT consecutive_blocks, today_block_count "
                f"FROM {metadata_cache.worker_state_table()} "
                f"WHERE id = 1"
            )
            wrow = await cur.fetchone()
        finally:
            await db.close()
        assert qrow[0] == "pending"
        assert qrow[1] == 0  # NOT a failure
        assert qrow[2] > time.time() + 500  # deferred past cooldown
        assert wrow[0] == 1  # first consecutive block
        assert wrow[1] == 1  # today_block_count

    async def test_second_block_escalates_cooldown_tier(
        self, worker_under, monkeypatch,
    ):
        # Pre-stamp worker_state to look like a 1st block just
        # happened, so this scan becomes the 2nd-in-window.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"UPDATE {metadata_cache.worker_state_table()} "
                f"SET last_block_at = ?, consecutive_blocks = 1 "
                f"WHERE id = 1",
                (time.time(),),
            )
            await db.commit()
        finally:
            await db.close()

        async def _fake_scan(author_id, library_slug, session):
            from app.discovery import amazon_author_id_resolver as r
            # Tier-1 cooldown from the source (600s).
            r.record_amazon_soft_block("fake", retry_after_s=600)
            return None, "HTTP 429 (fake)"
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0SOFTBLK2", library_slug="books-lib",
        )
        await metadata_cache_worker.tick()
        # Worker escalated to tier 2 (1800s).
        from app.discovery import amazon_author_id_resolver as r
        assert r.amazon_block_remaining_s() > 1500


# ─── Hard error path ───────────────────────────────────────────


class TestHardErrorPath:
    async def test_scan_returns_none_increments_failure_counter(
        self, worker_under, monkeypatch,
    ):
        async def _fake_scan(author_id, library_slug, session):
            return None, "transport: socket closed"
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0ERROR0001", library_slug="books-lib",
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "error"
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT status, consecutive_failures FROM "
                f"{metadata_cache.queue_table()} "
                f"WHERE author_id = ?",
                ("B0ERROR0001",),
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        # Still pending, but failure counter advanced.
        assert row[0] == "pending"
        assert row[1] == 1

    async def test_repeated_failures_flip_to_permanent_fail(
        self, worker_under, monkeypatch,
    ):
        async def _fake_scan(author_id, library_slug, session):
            return None, "transport: socket closed"
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        # Seed at consecutive_failures=4 so the next tick crosses the
        # 5-failure cap.
        await _seed_queue_row(
            author_id="B0DOOM00001", library_slug="books-lib",
            consecutive_failures=4,
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "permanent_fail"
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT status, consecutive_failures FROM "
                f"{metadata_cache.queue_table()} "
                f"WHERE author_id = ?",
                ("B0DOOM00001",),
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == "failed_permanent"
        assert row[1] == 5


# ─── Queue ordering ────────────────────────────────────────────


class TestQueuePopOrdering:
    async def test_higher_priority_pops_first(
        self, worker_under, monkeypatch,
    ):
        scan_calls = []
        async def _fake_scan(author_id, library_slug, session):
            scan_calls.append(author_id)
            return _author_result("X", author_id=author_id), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        # Three rows at different priorities. Highest must pop first.
        await _seed_queue_row(
            author_id="B0LOWPRIO1", library_slug="books-lib",
            priority=100.0,
        )
        await _seed_queue_row(
            author_id="B0HIGHPRI1", library_slug="books-lib",
            priority=1000.0,
        )
        await _seed_queue_row(
            author_id="B0MIDPRIO1", library_slug="books-lib",
            priority=500.0,
        )
        await metadata_cache_worker.tick()
        await metadata_cache_worker.tick()
        await metadata_cache_worker.tick()
        assert scan_calls == ["B0HIGHPRI1", "B0MIDPRIO1", "B0LOWPRIO1"]

    async def test_due_at_in_future_is_skipped(
        self, worker_under, monkeypatch,
    ):
        scan_calls = []
        async def _fake_scan(author_id, library_slug, session):
            scan_calls.append(author_id)
            return _author_result("X", author_id=author_id), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        # One row with due_at in the future, one due now. Only the
        # due-now row pops.
        await _seed_queue_row(
            author_id="B0FUTURE01", library_slug="books-lib",
            next_scan_due_at=time.time() + 10_000,
        )
        await _seed_queue_row(
            author_id="B0NOWAVL01", library_slug="books-lib",
            next_scan_due_at=0.0,
        )
        await metadata_cache_worker.tick()
        result = await metadata_cache_worker.tick()
        assert scan_calls == ["B0NOWAVL01"]
        # Second tick hit empty (future row not due yet).
        assert result.outcome == "queue_empty"


# ─── Crash recovery ───────────────────────────────────────────


class TestCrashRecovery:
    async def test_recover_resets_in_progress_rows(self, worker_under):
        # Manually seed a stuck in_progress row.
        await _seed_queue_row(
            author_id="B0STUCK0001", library_slug="books-lib",
            status="in_progress",
        )
        n = await metadata_cache_worker.recover_stuck_in_progress("amazon")
        assert n == 1
        # Row is back to pending and the worker can now pop it.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT status FROM {metadata_cache.queue_table()} "
                f"WHERE author_id = ?",
                ("B0STUCK0001",),
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == "pending"

    async def test_recover_leaves_failed_permanent_untouched(
        self, worker_under,
    ):
        # Permanent-fail rows must NOT come back to pending — those
        # need operator triage. Recovery only addresses crash-stuck
        # in_progress rows.
        await _seed_queue_row(
            author_id="B0PERMFAIL", library_slug="books-lib",
            status="failed_permanent",
        )
        await metadata_cache_worker.recover_stuck_in_progress("amazon")
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT status FROM {metadata_cache.queue_table()} "
                f"WHERE author_id = ?",
                ("B0PERMFAIL",),
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == "failed_permanent"


# ─── Escalation tier helper ────────────────────────────────────


class TestEscalationTierHelper:
    def test_tier_1_for_first_block(self):
        assert metadata_cache_worker._pick_escalation_cooldown(1) == 600.0

    def test_tier_2_for_second_block(self):
        assert metadata_cache_worker._pick_escalation_cooldown(2) == 1800.0

    def test_tier_3_for_third_block(self):
        assert metadata_cache_worker._pick_escalation_cooldown(3) == 3600.0

    def test_past_third_sticks_at_top_tier(self):
        assert metadata_cache_worker._pick_escalation_cooldown(10) == 3600.0

    def test_zero_falls_back_to_tier_1(self):
        # Defensive — `consecutive_blocks` shouldn't ever be 0 when we
        # call this, but a fallback to tier 1 is sane.
        assert metadata_cache_worker._pick_escalation_cooldown(0) == 600.0


# ─── Heartbeat ────────────────────────────────────────────────


class TestHeartbeat:
    async def test_tick_stamps_heartbeat(self, worker_under):
        # Even an outcome=queue_empty tick should stamp the heartbeat
        # — the operator's "is the worker alive" check is that field.
        before = time.time()
        await metadata_cache_worker.tick()
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT last_heartbeat_at FROM "
                f"{metadata_cache.worker_state_table()} WHERE id = 1"
            )
            hb = (await cur.fetchone())[0]
        finally:
            await db.close()
        assert hb is not None
        assert hb >= before


# ─── run_loop integration (one tick + stop_event) ─────────────


class TestRunLoopStopEvent:
    async def test_run_loop_exits_cleanly_on_stop_event(
        self, worker_under, monkeypatch,
    ):
        # Replace sleep with an immediate yield so the loop runs a
        # single iteration and then checks stop_event.
        import asyncio
        async def _fast_sleep(_s):
            return None
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

        stop_event = asyncio.Event()

        async def _tick_then_stop():
            # Let the loop run one tick.
            await asyncio.sleep(0)
            stop_event.set()

        # Empty queue → tick returns quickly + run_loop honors stop_event
        # via wait_for on the sleep step.
        async def _run():
            await metadata_cache_worker.run_loop(
                source_name="amazon", stop_event=stop_event,
            )

        stopper = asyncio.create_task(_tick_then_stop())
        runner = asyncio.create_task(_run())
        await asyncio.wait_for(runner, timeout=5.0)
        await stopper
        # No assertion needed beyond clean exit; the timeout would
        # fire if run_loop got stuck.
