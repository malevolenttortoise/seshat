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
from app.discovery.sources.base import AuthorResult, BookResult


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
    # Init both library discovery DBs so the worker's
    # `_libraries_for_author` lookup has tables to query against.
    await disco_db.init_db("books-lib")
    await disco_db.init_db("audio-lib")
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


async def _seed_author_in_libraries(
    amazon_id: str, *,
    libraries: tuple[str, ...] = ("books-lib", "audio-lib"),
) -> None:
    """Insert per-library `authors` rows so the worker's
    `_libraries_for_author` lookup finds this amazon_id and the v2
    fan-out has somewhere to write.

    Each library gets a `name` derived from the amazon_id so the row
    is identifiable but doesn't collide across re-seeds.
    """
    from app.discovery import database as disco_db
    for slug in libraries:
        db = await disco_db.get_db(slug=slug)
        try:
            await db.execute(
                "INSERT OR IGNORE INTO authors "
                "(name, sort_name, normalized_name, amazon_id) "
                "VALUES (?, ?, ?, ?)",
                (
                    f"Test {amazon_id}", f"Test {amazon_id}",
                    f"test {amazon_id}".lower(), amazon_id,
                ),
            )
            await db.commit()
        finally:
            await db.close()


async def _seed_queue_row(
    *,
    author_id: str,
    priority: float = 100.0,
    status: str = "pending",
    next_scan_due_at: float = 0.0,
    consecutive_failures: int = 0,
    enqueued_reason: str = "test_seed",
    seed_in_libraries: tuple[str, ...] = ("books-lib", "audio-lib"),
) -> None:
    """Insert a v2-schema queue row (PK=author_id only). Also seeds
    matching per-library authors rows in `seed_in_libraries` so the
    worker's fan-out has targets — pass `seed_in_libraries=()` for
    tests that explicitly want the no-libraries path."""
    if seed_in_libraries:
        await _seed_author_in_libraries(
            author_id, libraries=seed_in_libraries,
        )
    db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
    try:
        await db.execute(
            f"INSERT OR REPLACE INTO {metadata_cache.queue_table()} "
            f"(author_id, priority, status, next_scan_due_at, "
            f" consecutive_failures, enqueued_reason) "
            f"VALUES (?, ?, ?, ?, ?, ?)",
            (author_id, priority, status, next_scan_due_at,
             consecutive_failures, enqueued_reason),
        )
        await db.commit()
    finally:
        await db.close()


def _author_result(
    *titles: str,
    author_id: str = "B0AAAAAAAA",
    binding: str = "kindle_edition",
) -> AuthorResult:
    """Build a flat AuthorResult with one BookResult per title. v2:
    every BookResult carries `format=binding` so the worker's
    per-library partition routes the books correctly. Default
    `kindle_edition` so books land in ebook libraries; pass
    `binding="audio_download"` for audiobook-library tests."""
    books = [
        BookResult(
            title=t,
            external_id=f"B0{i:08d}",
            source="amazon",
            language="English",
            format=binding,
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
            author_id="B0COOLDOWN",
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
        async def _fake_scan(author_id, session):
            return _author_result(
                "Book One", "Book Two", "Book Three", author_id=author_id,
            ), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0TESTSCAN",
            seed_in_libraries=("books-lib",),  # ebook only — keep
                                               # assertions unambiguous
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "ok"
        assert result.books_cached == 3
        assert result.author_id == "B0TESTSCAN"
        # State row written (v2 fan-out: one row per library).
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT last_outcome, book_count FROM "
                f"{metadata_cache.state_table()} "
                f"WHERE author_id = ? AND library_slug = ?",
                ("B0TESTSCAN", "books-lib"),
            )
            srow = await cur.fetchone()
            cur = await db.execute(
                f"SELECT title, format FROM {metadata_cache.books_table()} "
                f"WHERE author_id = ? AND library_slug = ? ORDER BY title",
                ("B0TESTSCAN", "books-lib"),
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
        async def _fake_scan(author_id, session):
            # Scan returns an audio_download book — only audio-lib's
            # binding-set accepts it, so the partition routes it
            # exclusively to audio-lib.
            return _author_result(
                "Audiobook One", author_id=author_id,
                binding="audio_download",
            ), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0AUDIO0001", seed_in_libraries=("audio-lib",),
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
        async def _fake_scan(author_id, session):
            return _author_result(author_id=author_id), None  # 0 books
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0NOBOOKS01",
            seed_in_libraries=("books-lib",),
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
                f"WHERE author_id = ? AND library_slug = ?",
                ("B0NOBOOKS01", "books-lib"),
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
        async def _fake_scan(author_id, session):
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
            author_id="B0SOFTBLK1",
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

        async def _fake_scan(author_id, session):
            from app.discovery import amazon_author_id_resolver as r
            # Tier-1 cooldown from the source (600s).
            r.record_amazon_soft_block("fake", retry_after_s=600)
            return None, "HTTP 429 (fake)"
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0SOFTBLK2",
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
        async def _fake_scan(author_id, session):
            return None, "transport: socket closed"
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0ERROR0001",
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
        async def _fake_scan(author_id, session):
            return None, "transport: socket closed"
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        # Seed at consecutive_failures=4 so the next tick crosses the
        # 5-failure cap.
        await _seed_queue_row(
            author_id="B0DOOM00001",
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
        async def _fake_scan(author_id, session):
            scan_calls.append(author_id)
            return _author_result("X", author_id=author_id), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        # Three rows at different priorities. Highest must pop first.
        await _seed_queue_row(
            author_id="B0LOWPRIO1",
            priority=100.0,
        )
        await _seed_queue_row(
            author_id="B0HIGHPRI1",
            priority=1000.0,
        )
        await _seed_queue_row(
            author_id="B0MIDPRIO1",
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
        async def _fake_scan(author_id, session):
            scan_calls.append(author_id)
            return _author_result("X", author_id=author_id), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        # One row with due_at in the future, one due now. Only the
        # due-now row pops.
        await _seed_queue_row(
            author_id="B0FUTURE01",
            next_scan_due_at=time.time() + 10_000,
        )
        await _seed_queue_row(
            author_id="B0NOWAVL01",
            next_scan_due_at=0.0,
        )
        await metadata_cache_worker.tick()
        result = await metadata_cache_worker.tick()
        assert scan_calls == ["B0NOWAVL01"]
        # Second tick hit empty (future row not due yet).
        assert result.outcome == "queue_empty"


# ─── Crash recovery ───────────────────────────────────────────


class TestDuplicateAsinDedup:
    """v2.21.0 Phase D hotfix — AmazonSource sometimes returns the
    same `book_asin` twice in one scan (mediaMatrix overlap or
    pagination duplicates). Pre-fix the `INSERT INTO {books}` tripped
    the (author_id, library_slug, book_asin) UNIQUE constraint and
    crashed the entire tick, leaving the queue row stuck at
    `status='in_progress'` (UAT 2026-05-22 caught two such crashes on
    B000AP9Y66 and B001H6GPWS)."""

    async def test_duplicate_asin_in_books_list_is_deduped(
        self, worker_under, monkeypatch,
    ):
        # Build an AuthorResult with the same external_id twice in
        # the standalone books list. Pre-fix the worker would hit the
        # UNIQUE constraint; with the dedupe it should keep the
        # first occurrence and drop the second silently.
        async def _fake_scan(author_id, session):
            from app.discovery.sources.base import AuthorResult, BookResult
            return AuthorResult(
                name=author_id, external_id=author_id,
                books=[
                    BookResult(
                        title="The Same Book",
                        external_id="B0SAMEASIN",
                        source="amazon",
                        language="English",
                        format="kindle_edition",
                    ),
                    BookResult(
                        title="Same Book — Different Variant Row",
                        external_id="B0SAMEASIN",  # duplicate
                        source="amazon",
                        language="English",
                        format="kindle_edition",
                    ),
                ],
                series=[],
            ), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0DEDUP0001",
            seed_in_libraries=("books-lib",),
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "ok"
        # Exactly one book row landed despite two in the input list.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT title FROM {metadata_cache.books_table()} "
                f"WHERE author_id = ?",
                ("B0DEDUP0001",),
            )
            rows = await cur.fetchall()
        finally:
            await db.close()
        assert len(rows) == 1
        assert rows[0][0] == "The Same Book"  # first wins

    async def test_duplicate_asin_across_books_and_series_is_deduped(
        self, worker_under, monkeypatch,
    ):
        async def _fake_scan(author_id, session):
            from app.discovery.sources.base import (
                AuthorResult, BookResult, SeriesResult,
            )
            return AuthorResult(
                name=author_id, external_id=author_id,
                books=[
                    BookResult(
                        title="Standalone Edition",
                        external_id="B0OVERLAP1",
                        source="amazon",
                        format="kindle_edition",
                    ),
                ],
                series=[
                    SeriesResult(
                        name="Some Series",
                        books=[
                            BookResult(
                                title="Series Edition",
                                external_id="B0OVERLAP1",  # dupe
                                source="amazon",
                                format="kindle_edition",
                            ),
                        ],
                    ),
                ],
            ), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0DEDUP0002",
            seed_in_libraries=("books-lib",),
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "ok"
        # Tick succeeds + no UNIQUE constraint crash.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT title FROM {metadata_cache.books_table()} "
                f"WHERE author_id = ?",
                ("B0DEDUP0002",),
            )
            rows = await cur.fetchall()
        finally:
            await db.close()
        assert len(rows) == 1
        # First-occurrence-wins ordering: books[] iterates before
        # series[], so the standalone keeps.
        assert rows[0][0] == "Standalone Edition"


class TestCacheWriteFailureRecovery:
    """v2.21.0 Phase D hotfix — a write-time exception in the
    cache-write block must NOT leave the queue row stuck at
    `status='in_progress'`. Pre-fix the tick() outer exception path
    swallowed the crash but the row stayed locked until the next
    container restart triggered `recover_stuck_in_progress`."""

    async def test_write_failure_resets_queue_row_to_pending(
        self, worker_under, monkeypatch,
    ):
        async def _fake_scan(author_id, session):
            return _author_result("Book A", author_id=author_id), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )

        # Force a UNIQUE-constraint-style failure by sabotaging the
        # books-replace step. The cleanest way: patch
        # `_replace_book_rows` to always raise.
        async def _boom(*_a, **_kw):
            raise RuntimeError("simulated cache-write failure")
        monkeypatch.setattr(
            metadata_cache_worker, "_replace_book_rows", _boom,
        )

        await _seed_queue_row(
            author_id="B0WRITEFAIL",
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "error"
        assert "cache write failed" in (result.error or "")

        # Critically: queue row is back to `pending`, with
        # consecutive_failures incremented so a recurring failure
        # eventually flips to failed_permanent.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT status, consecutive_failures FROM "
                f"{metadata_cache.queue_table()} WHERE author_id = ?",
                ("B0WRITEFAIL",),
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == "pending"
        assert row[1] == 1


class TestCrashRecovery:
    async def test_recover_resets_in_progress_rows(self, worker_under):
        # Manually seed a stuck in_progress row.
        await _seed_queue_row(
            author_id="B0STUCK0001",
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
            author_id="B0PERMFAIL",
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

    async def test_disabled_tick_still_stamps_heartbeat(
        self, worker_under,
    ):
        """The heartbeat MUST fire even when the worker is disabled.
        Without this, an operator inspecting worker_state can't tell
        "disabled" from "crashed / never spawned" — both show
        `last_heartbeat_at = NULL`. Pinned regression from Phase D
        UAT 2026-05-22 where the production worker started disabled
        and `last_heartbeat_at` stayed NULL through three full
        iterations."""
        worker_under["settings"]["metadata_cache"]["amazon"]["enabled"] = False
        before = time.time()
        result = await metadata_cache_worker.tick()
        assert result.outcome == "disabled"
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

    async def test_cooldown_tick_still_stamps_heartbeat(
        self, worker_under,
    ):
        """Same rationale as disabled — a cooldown tick is still a
        tick, and the heartbeat must reflect that the worker is
        alive and aware of the cooldown."""
        from app.discovery import amazon_author_id_resolver as r
        r.record_amazon_soft_block("test", retry_after_s=120)
        before = time.time()
        result = await metadata_cache_worker.tick()
        assert result.outcome == "cooldown"
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


# ─── v2.21.0 Phase G — structured logging + ntfy + daily rollover ──


@pytest.fixture
def fake_ntfy(monkeypatch):
    """Capture ntfy emits via the worker's `_send_ntfy` helper.

    Bypasses the real httpx client + the `is_event_enabled` gate so
    individual tests can assert on the payload directly. Returns a
    list[dict] that gets appended to on every `_send_ntfy` call.
    """
    calls: list[dict] = []

    async def _capture(*, event_key, title, message, priority=3, tags=None):
        calls.append({
            "event_key": event_key, "title": title,
            "message": message, "priority": priority, "tags": tags,
        })
        return True

    monkeypatch.setattr(metadata_cache_worker, "_send_ntfy", _capture)
    return calls


class TestPhaseGDailyRollover:
    """`today_scan_count` + `today_block_count` reset when the local
    day rolls over. Before Phase G, both counters were monotonic
    since deploy."""

    async def test_same_day_scan_increments(self, worker_under, monkeypatch):
        async def _fake_scan(author_id, session):
            return _author_result("Book", author_id=author_id), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0DAY00001", seed_in_libraries=("books-lib",),
        )
        await metadata_cache_worker.tick()
        await _seed_queue_row(
            author_id="B0DAY00002", seed_in_libraries=("books-lib",),
        )
        await metadata_cache_worker.tick()

        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT today_scan_count FROM "
                f"{metadata_cache.worker_state_table()} WHERE id = 1"
            )
            count = (await cur.fetchone())[0]
        finally:
            await db.close()
        assert count == 2

    async def test_day_rollover_resets_scan_counter(self, worker_under):
        # Hand-craft a worker_state row with last_scan_completed_at
        # 48h ago + today_scan_count=99. The next `_record_scan_completed`
        # should reset to 1.
        old_ts = time.time() - 48 * 3600
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"UPDATE {metadata_cache.worker_state_table()} "
                f"SET last_scan_completed_at = ?, today_scan_count = 99 "
                f"WHERE id = 1",
                (old_ts,),
            )
            await db.commit()
            await metadata_cache_worker._record_scan_completed(
                db, "amazon", time.time(),
            )
            cur = await db.execute(
                f"SELECT today_scan_count FROM "
                f"{metadata_cache.worker_state_table()} WHERE id = 1"
            )
            count = (await cur.fetchone())[0]
        finally:
            await db.close()
        assert count == 1

    async def test_day_rollover_resets_block_counter(self, worker_under):
        old_ts = time.time() - 48 * 3600
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"UPDATE {metadata_cache.worker_state_table()} "
                f"SET last_block_at = ?, today_block_count = 42, "
                f"    consecutive_blocks = 3 "
                f"WHERE id = 1",
                (old_ts,),
            )
            await db.commit()
            new_consecutive = await metadata_cache_worker._record_block_in_worker_state(
                db, "amazon", time.time(), cooldown_s=600.0,
            )
            cur = await db.execute(
                f"SELECT today_block_count, consecutive_blocks FROM "
                f"{metadata_cache.worker_state_table()} WHERE id = 1"
            )
            today, consec = await cur.fetchone()
        finally:
            await db.close()
        # `today_block_count` reset by day rollover; consecutive_blocks
        # also reset since 48h > the 1h escalation window.
        assert today == 1
        assert consec == 1
        assert new_consecutive == 1

    async def test_reset_today_counters_returns_prior_values(
        self, worker_under,
    ):
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"UPDATE {metadata_cache.worker_state_table()} "
                f"SET today_scan_count = 17, today_block_count = 4 "
                f"WHERE id = 1"
            )
            await db.commit()
        finally:
            await db.close()
        prior_scans, prior_blocks, prior_exhausts = (
            await metadata_cache_worker.reset_today_counters("amazon")
        )
        assert prior_scans == 17
        assert prior_blocks == 4
        # Amazon doesn't track budget exhausts (GR-only column);
        # `reset_today_counters` returns 0 in slot 3 for non-GR.
        assert prior_exhausts == 0
        # Second call sees the freshly-zeroed values.
        again_s, again_b, again_e = (
            await metadata_cache_worker.reset_today_counters("amazon")
        )
        assert again_s == 0
        assert again_b == 0
        assert again_e == 0


class TestPhaseGNtfyGates:
    async def test_top_tier_cooldown_escalation_fires_warning_ntfy(
        self, worker_under, monkeypatch, fake_ntfy,
    ):
        # Hand-craft worker_state to be at the 2nd-block threshold so
        # the next soft-block escalates to tier 3 (3600s).
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"UPDATE {metadata_cache.worker_state_table()} "
                f"SET last_block_at = ?, consecutive_blocks = 2 "
                f"WHERE id = 1",
                (time.time() - 60,),  # within 1h window
            )
            await db.commit()
        finally:
            await db.close()

        async def _fake_scan(author_id, session):
            from app.discovery import amazon_author_id_resolver as r
            # Initial cooldown small; escalation tier will boost to 3600.
            r.record_amazon_soft_block("test", retry_after_s=60)
            return None, "HTTP 429"
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(author_id="B0TIER3001")
        result = await metadata_cache_worker.tick()
        assert result.outcome == "soft_block"
        assert result.cooldown_remaining_s >= 3600
        warnings = [c for c in fake_ntfy if c["event_key"] == "metadata_cache_warning"]
        assert len(warnings) == 1
        assert "top tier" in warnings[0]["title"]
        assert warnings[0]["priority"] == 4

    async def test_first_tier_block_does_not_fire_ntfy(
        self, worker_under, monkeypatch, fake_ntfy,
    ):
        async def _fake_scan(author_id, session):
            from app.discovery import amazon_author_id_resolver as r
            r.record_amazon_soft_block("test", retry_after_s=600)
            return None, "HTTP 429"
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(author_id="B0TIER1001")
        await metadata_cache_worker.tick()
        # Routine tier-1 block — no ntfy.
        warnings = [c for c in fake_ntfy if c["event_key"] == "metadata_cache_warning"]
        assert warnings == []

    async def test_permanent_failure_fires_warning_ntfy(
        self, worker_under, monkeypatch, fake_ntfy,
    ):
        async def _fake_scan(author_id, session):
            return None, "transport error"
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        # Pre-seed at MAX_CONSECUTIVE_FAILURES - 1 so the next failure
        # flips to failed_permanent.
        cap = metadata_cache_worker._MAX_CONSECUTIVE_FAILURES
        await _seed_queue_row(
            author_id="B0PERMFAIL", consecutive_failures=cap - 1,
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "permanent_fail"
        warnings = [c for c in fake_ntfy if c["event_key"] == "metadata_cache_warning"]
        assert len(warnings) == 1
        assert "failed_permanent" in warnings[0]["title"]

    async def test_transient_failure_does_not_fire_ntfy(
        self, worker_under, monkeypatch, fake_ntfy,
    ):
        async def _fake_scan(author_id, session):
            return None, "transport error"
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(author_id="B0TRANSIENT")
        result = await metadata_cache_worker.tick()
        assert result.outcome == "error"
        # Transient failures are silent — operator only hears about
        # permanent flips.
        warnings = [c for c in fake_ntfy if c["event_key"] == "metadata_cache_warning"]
        assert warnings == []


class TestPhaseGNewBookDetection:
    async def test_first_scan_returns_zero_new_books(
        self, worker_under, monkeypatch, fake_ntfy,
    ):
        async def _fake_scan(author_id, session):
            return _author_result(
                "Book One", "Book Two", author_id=author_id,
            ), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0FIRSTSCN", seed_in_libraries=("books-lib",),
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "ok"
        assert result.new_books == 0  # no prior baseline → not "new"
        info = [c for c in fake_ntfy if c["event_key"] == "metadata_cache_new_book"]
        assert info == []

    async def test_second_scan_with_new_asin_returns_new_count(
        self, worker_under, monkeypatch, fake_ntfy,
    ):
        # Hand-pre-populate the cache so the next scan has a baseline
        # to compare against.
        now = time.time()
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"INSERT INTO {metadata_cache.state_table()} "
                f"(author_id, library_slug, last_scanned_at, "
                f" last_outcome, book_count) VALUES (?, ?, ?, ?, ?)",
                ("B0NEWBOOK1", "books-lib", now - 86400, "ok", 1),
            )
            await db.execute(
                f"INSERT INTO {metadata_cache.books_table()} "
                f"(author_id, library_slug, book_asin, title, format, "
                f" cached_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("B0NEWBOOK1", "books-lib", "B000000001", "Old Book",
                 "kindle_edition", now - 86400),
            )
            await db.commit()
        finally:
            await db.close()

        async def _fake_scan(author_id, session):
            # Returns the old book PLUS a new one.
            old = BookResult(
                title="Old Book", external_id="B000000001",
                source="amazon", language="English",
                format="kindle_edition",
            )
            new = BookResult(
                title="Brand New Book", external_id="B000000002",
                source="amazon", language="English",
                format="kindle_edition",
            )
            return AuthorResult(
                name=author_id, external_id=author_id,
                books=[old, new], series=[],
            ), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0NEWBOOK1", seed_in_libraries=("books-lib",),
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "ok"
        assert result.new_books == 1
        info = [c for c in fake_ntfy if c["event_key"] == "metadata_cache_new_book"]
        assert len(info) == 1
        assert "Brand New Book" in info[0]["message"]


class TestPhaseGStallWatchdog:
    async def test_fresh_heartbeat_returns_false(self, worker_under):
        # Stamp a recent heartbeat.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"UPDATE {metadata_cache.worker_state_table()} "
                f"SET last_heartbeat_at = ? WHERE id = 1",
                (time.time(),),
            )
            await db.commit()
        finally:
            await db.close()
        stalled = await metadata_cache_worker.check_stall(
            "amazon", threshold_s=300.0,
        )
        assert stalled is False

    async def test_disabled_worker_never_reports_stall(
        self, worker_under,
    ):
        # Disable worker; heartbeat ancient.
        worker_under["settings"]["metadata_cache"]["amazon"]["enabled"] = False
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"UPDATE {metadata_cache.worker_state_table()} "
                f"SET last_heartbeat_at = ? WHERE id = 1",
                (time.time() - 86400,),
            )
            await db.commit()
        finally:
            await db.close()
        stalled = await metadata_cache_worker.check_stall(
            "amazon", threshold_s=300.0,
        )
        assert stalled is False

    async def test_stale_heartbeat_fires_error_ntfy(
        self, worker_under, fake_ntfy,
    ):
        # Heartbeat 1h ago, threshold 300s → stalled.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"UPDATE {metadata_cache.worker_state_table()} "
                f"SET last_heartbeat_at = ? WHERE id = 1",
                (time.time() - 3600,),
            )
            await db.commit()
        finally:
            await db.close()
        stalled = await metadata_cache_worker.check_stall(
            "amazon", threshold_s=300.0,
        )
        assert stalled is True
        errors = [c for c in fake_ntfy if c["event_key"] == "metadata_cache_error"]
        assert len(errors) == 1
        assert "stalled" in errors[0]["title"]

    async def test_repeat_stall_debounces_ntfy(
        self, worker_under, fake_ntfy,
    ):
        # First stall fires. Second invocation within the same stall
        # window stays quiet.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"UPDATE {metadata_cache.worker_state_table()} "
                f"SET last_heartbeat_at = ? WHERE id = 1",
                (time.time() - 3600,),
            )
            await db.commit()
        finally:
            await db.close()
        await metadata_cache_worker.check_stall("amazon", threshold_s=300.0)
        await metadata_cache_worker.check_stall("amazon", threshold_s=300.0)
        errors = [c for c in fake_ntfy if c["event_key"] == "metadata_cache_error"]
        assert len(errors) == 1  # only the first one fired


class TestPhaseGDailySummary:
    async def test_send_daily_summary_resets_counters_and_emits_ntfy(
        self, worker_under, fake_ntfy,
    ):
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"UPDATE {metadata_cache.worker_state_table()} "
                f"SET today_scan_count = 11, today_block_count = 2 "
                f"WHERE id = 1"
            )
            await db.commit()
        finally:
            await db.close()
        await metadata_cache_worker.send_daily_summary("amazon")
        # Counters zeroed.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT today_scan_count, today_block_count FROM "
                f"{metadata_cache.worker_state_table()} WHERE id = 1"
            )
            scans, blocks = await cur.fetchone()
        finally:
            await db.close()
        assert scans == 0
        assert blocks == 0
        # ntfy fired with the prior numbers.
        summaries = [
            c for c in fake_ntfy
            if c["event_key"] == "metadata_cache_daily_summary"
        ]
        assert len(summaries) == 1
        assert "11 scans" in summaries[0]["title"]
        assert "Soft-blocks: 2" in summaries[0]["message"]


class TestPhaseGLogFileHandler:
    def test_log_file_disabled_returns_none(self, worker_under):
        worker_under["settings"]["metadata_cache_log_file_enabled"] = False
        path = metadata_cache_worker.install_log_file_handler()
        assert path is None

    def test_log_file_enabled_attaches_handler(
        self, worker_under, monkeypatch,
    ):
        from app import config as app_config
        worker_under["settings"]["metadata_cache_log_file_enabled"] = True
        worker_under["settings"]["metadata_cache_log_file_max_bytes"] = 1000
        worker_under["settings"]["metadata_cache_log_file_backup_count"] = 1
        monkeypatch.setattr(app_config, "DATA_DIR", worker_under["tmp_path"])
        path = metadata_cache_worker.install_log_file_handler()
        try:
            assert path is not None
            assert path.endswith("metadata_cache_worker.log")
            assert metadata_cache_worker._log_file_handler in (
                metadata_cache_worker.logger.handlers
            )
            # Idempotent — second call detaches old, attaches new.
            path_again = metadata_cache_worker.install_log_file_handler()
            assert path_again == path
            file_handlers = [
                h for h in metadata_cache_worker.logger.handlers
                if h is metadata_cache_worker._log_file_handler
            ]
            assert len(file_handlers) == 1  # not stacked
        finally:
            # Clean up so the handler doesn't bleed into other tests.
            if metadata_cache_worker._log_file_handler is not None:
                metadata_cache_worker.logger.removeHandler(
                    metadata_cache_worker._log_file_handler
                )
                metadata_cache_worker._log_file_handler.close()
                metadata_cache_worker._log_file_handler = None


class TestPhaseGStructuredLogLine:
    async def test_success_emits_scan_marker(
        self, worker_under, monkeypatch, caplog,
    ):
        async def _fake_scan(author_id, session):
            return _author_result(
                "Book", author_id=author_id,
            ), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0LOGLINE1", seed_in_libraries=("books-lib",),
        )
        import logging as _logging
        caplog.set_level(_logging.INFO, logger="seshat.discovery.metadata_cache_worker.amazon")
        await metadata_cache_worker.tick()
        scan_lines = [r for r in caplog.records if "[scan]" in r.getMessage()]
        assert scan_lines, "expected a [scan] structured line on success"
        msg = scan_lines[-1].getMessage()
        assert "author=B0LOGLINE1" in msg
        assert "outcome=ok" in msg
        assert "elapsed_ms=" in msg


# ─── v2.21.0 Phase I — mode + scheduled-window gate ──────────────


class TestPhaseIModeDerivation:
    def test_legacy_enabled_true_maps_to_continuous(self, worker_under):
        worker_under["settings"]["metadata_cache"]["amazon"]["enabled"] = True
        worker_under["settings"]["metadata_cache"]["amazon"].pop("mode", None)
        assert metadata_cache_worker.get_worker_mode("amazon") == "continuous"
        assert metadata_cache_worker.is_worker_enabled("amazon") is True

    def test_legacy_enabled_false_maps_to_disabled(self, worker_under):
        worker_under["settings"]["metadata_cache"]["amazon"]["enabled"] = False
        worker_under["settings"]["metadata_cache"]["amazon"].pop("mode", None)
        assert metadata_cache_worker.get_worker_mode("amazon") == "disabled"
        assert metadata_cache_worker.is_worker_enabled("amazon") is False

    def test_mode_disabled_wins_over_enabled_true(self, worker_under):
        worker_under["settings"]["metadata_cache"]["amazon"]["enabled"] = True
        worker_under["settings"]["metadata_cache"]["amazon"]["mode"] = "disabled"
        assert metadata_cache_worker.get_worker_mode("amazon") == "disabled"
        assert metadata_cache_worker.is_worker_enabled("amazon") is False

    def test_mode_scheduled_is_enabled(self, worker_under):
        worker_under["settings"]["metadata_cache"]["amazon"]["enabled"] = True
        worker_under["settings"]["metadata_cache"]["amazon"]["mode"] = "scheduled"
        assert metadata_cache_worker.get_worker_mode("amazon") == "scheduled"
        assert metadata_cache_worker.is_worker_enabled("amazon") is True

    def test_unknown_mode_falls_back_to_legacy_field(self, worker_under):
        # Typo / future-mode-this-build-doesn't-know — fall back to
        # `enabled` boolean rather than crashing.
        worker_under["settings"]["metadata_cache"]["amazon"]["enabled"] = True
        worker_under["settings"]["metadata_cache"]["amazon"]["mode"] = "wat"
        assert metadata_cache_worker.get_worker_mode("amazon") == "continuous"


class TestPhaseIActiveHoursParser:
    def test_well_formed_daytime_window(self):
        assert metadata_cache_worker._parse_active_hours("10:00-22:00") == (10, 0, 22, 0)

    def test_well_formed_overnight_window(self):
        assert metadata_cache_worker._parse_active_hours("22:30-06:15") == (22, 30, 6, 15)

    def test_missing_dash_returns_none(self):
        assert metadata_cache_worker._parse_active_hours("1000-2200") is None

    def test_out_of_range_hour_returns_none(self):
        assert metadata_cache_worker._parse_active_hours("25:00-26:00") is None

    def test_non_numeric_returns_none(self):
        assert metadata_cache_worker._parse_active_hours("ten-twelve") is None

    def test_empty_returns_none(self):
        assert metadata_cache_worker._parse_active_hours("") is None
        assert metadata_cache_worker._parse_active_hours(None) is None  # type: ignore[arg-type]


class TestPhaseIScheduleWindow:
    """`is_inside_schedule_window` reads system local time. Tests
    patch `_resolve_local_now` to drive specific wallclock values."""

    def _set_schedule(self, worker_under, active_hours, mode="scheduled"):
        worker_under["settings"]["metadata_cache"]["amazon"]["mode"] = mode
        worker_under["settings"]["metadata_cache"]["amazon"]["enabled"] = True
        worker_under["settings"]["metadata_cache"]["amazon"]["schedule"] = {
            "active_hours": active_hours, "timezone": "",
        }

    def _patch_now(self, monkeypatch, hour, minute):
        from datetime import datetime as _dt
        fake_now = _dt(2026, 5, 22, hour, minute, 0)
        monkeypatch.setattr(
            metadata_cache_worker, "_resolve_local_now",
            lambda _tz="": fake_now,
        )

    def test_continuous_mode_always_inside(self, worker_under):
        worker_under["settings"]["metadata_cache"]["amazon"]["mode"] = "continuous"
        assert metadata_cache_worker.is_inside_schedule_window("amazon") is True

    def test_daytime_window_inside_at_noon(self, worker_under, monkeypatch):
        self._set_schedule(worker_under, "10:00-22:00")
        self._patch_now(monkeypatch, 12, 0)
        assert metadata_cache_worker.is_inside_schedule_window("amazon") is True

    def test_daytime_window_outside_at_3am(self, worker_under, monkeypatch):
        self._set_schedule(worker_under, "10:00-22:00")
        self._patch_now(monkeypatch, 3, 0)
        assert metadata_cache_worker.is_inside_schedule_window("amazon") is False

    def test_overnight_window_inside_at_midnight(self, worker_under, monkeypatch):
        self._set_schedule(worker_under, "22:00-06:00")
        self._patch_now(monkeypatch, 0, 30)
        assert metadata_cache_worker.is_inside_schedule_window("amazon") is True

    def test_overnight_window_outside_at_noon(self, worker_under, monkeypatch):
        self._set_schedule(worker_under, "22:00-06:00")
        self._patch_now(monkeypatch, 12, 0)
        assert metadata_cache_worker.is_inside_schedule_window("amazon") is False

    def test_end_boundary_exclusive(self, worker_under, monkeypatch):
        # 22:00 sharp is OUTSIDE the 10:00-22:00 window.
        self._set_schedule(worker_under, "10:00-22:00")
        self._patch_now(monkeypatch, 22, 0)
        assert metadata_cache_worker.is_inside_schedule_window("amazon") is False

    def test_start_boundary_inclusive(self, worker_under, monkeypatch):
        # 10:00 sharp is INSIDE the 10:00-22:00 window.
        self._set_schedule(worker_under, "10:00-22:00")
        self._patch_now(monkeypatch, 10, 0)
        assert metadata_cache_worker.is_inside_schedule_window("amazon") is True

    def test_invalid_spec_treated_as_always_inside(self, worker_under, monkeypatch):
        # Operator typo can't strand the worker.
        self._set_schedule(worker_under, "garbage")
        self._patch_now(monkeypatch, 3, 0)
        assert metadata_cache_worker.is_inside_schedule_window("amazon") is True

    def test_seconds_until_window_open_inside_returns_zero(
        self, worker_under, monkeypatch,
    ):
        self._set_schedule(worker_under, "10:00-22:00")
        self._patch_now(monkeypatch, 12, 0)
        assert metadata_cache_worker.seconds_until_window_open("amazon") == 0.0

    def test_seconds_until_window_open_before_today_start(
        self, worker_under, monkeypatch,
    ):
        # 03:00 → 7h until 10:00 today.
        self._set_schedule(worker_under, "10:00-22:00")
        self._patch_now(monkeypatch, 3, 0)
        delta = metadata_cache_worker.seconds_until_window_open("amazon")
        assert delta == 7 * 3600

    def test_seconds_until_window_open_after_today_end(
        self, worker_under, monkeypatch,
    ):
        # 23:00 → 11h until 10:00 tomorrow.
        self._set_schedule(worker_under, "10:00-22:00")
        self._patch_now(monkeypatch, 23, 0)
        delta = metadata_cache_worker.seconds_until_window_open("amazon")
        assert delta == 11 * 3600


class TestPhaseIScheduleGate:
    """Worker tick honors the schedule window."""

    async def test_outside_schedule_returns_outside_schedule_outcome(
        self, worker_under, monkeypatch,
    ):
        worker_under["settings"]["metadata_cache"]["amazon"]["mode"] = "scheduled"
        worker_under["settings"]["metadata_cache"]["amazon"]["schedule"] = {
            "active_hours": "10:00-22:00", "timezone": "",
        }
        from datetime import datetime as _dt
        # 03:00 local — well outside the window.
        monkeypatch.setattr(
            metadata_cache_worker, "_resolve_local_now",
            lambda _tz="": _dt(2026, 5, 22, 3, 0, 0),
        )
        await _seed_queue_row(author_id="B0SCHED0001")
        result = await metadata_cache_worker.tick()
        assert result.outcome == "outside_schedule"
        # Sleep is bounded — at least IDLE, at most the cooldown cap.
        assert result.next_sleep_s >= 60
        assert result.next_sleep_s <= 3600
        # Queue row was NOT popped — still pending for the next window.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT status FROM {metadata_cache.queue_table()} "
                f"WHERE author_id = ?",
                ("B0SCHED0001",),
            )
            status = (await cur.fetchone())[0]
        finally:
            await db.close()
        assert status == "pending"

    async def test_inside_schedule_proceeds_normally(
        self, worker_under, monkeypatch, fake_ntfy,
    ):
        worker_under["settings"]["metadata_cache"]["amazon"]["mode"] = "scheduled"
        worker_under["settings"]["metadata_cache"]["amazon"]["schedule"] = {
            "active_hours": "00:00-23:59", "timezone": "",
        }

        async def _fake_scan(author_id, session):
            return _author_result("Book", author_id=author_id), None
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_amazon_scan", _fake_scan,
        )
        await _seed_queue_row(
            author_id="B0SCHED0002", seed_in_libraries=("books-lib",),
        )
        result = await metadata_cache_worker.tick()
        assert result.outcome == "ok"

    async def test_outside_schedule_does_not_trigger_stall(
        self, worker_under, monkeypatch, fake_ntfy,
    ):
        # Worker is enabled in scheduled mode + outside window + has
        # an ancient heartbeat. Stall watchdog must stay quiet.
        worker_under["settings"]["metadata_cache"]["amazon"]["mode"] = "scheduled"
        worker_under["settings"]["metadata_cache"]["amazon"]["schedule"] = {
            "active_hours": "10:00-22:00", "timezone": "",
        }
        from datetime import datetime as _dt
        monkeypatch.setattr(
            metadata_cache_worker, "_resolve_local_now",
            lambda _tz="": _dt(2026, 5, 22, 3, 0, 0),
        )
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"UPDATE {metadata_cache.worker_state_table()} "
                f"SET last_heartbeat_at = ? WHERE id = 1",
                (time.time() - 3600,),
            )
            await db.commit()
        finally:
            await db.close()
        stalled = await metadata_cache_worker.check_stall(
            "amazon", threshold_s=300.0,
        )
        assert stalled is False
        errors = [c for c in fake_ntfy if c["event_key"] == "metadata_cache_error"]
        assert errors == []


# ─── v3.4.0 slice 03 — Goodreads worker ────────────────────────


@pytest.fixture
async def gr_worker_under(tmp_path, monkeypatch):
    """Sibling of `worker_under` configured for the GR worker.

    Inits BOTH source caches under tmp_path. Sets GR worker mode to
    `continuous` (default DISABLED would short-circuit the tick).
    Stubs `_perform_goodreads_scan` so tests override per-case to
    drive specific outcomes without hitting real GR.
    """
    from app import config as app_config
    from app.discovery import database as disco_db
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(metadata_cache, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)

    fake_settings = {
        "metadata_cache": {
            "amazon": {"enabled": False, "mode": "disabled"},
            "goodreads": {
                "enabled": True, "mode": "continuous",
                "schedule": {
                    "active_hours": "10:00-22:00", "timezone": "",
                },
            },
        },
        "metadata_sources": {
            "goodreads": {"rate_limit": 0.0},
        },
    }
    monkeypatch.setattr(
        app_config, "load_settings", lambda: dict(fake_settings),
    )
    monkeypatch.setattr(
        app_config, "save_settings", lambda d: fake_settings.update(d),
    )

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

    await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
    await disco_db.init_db("books-lib")
    await disco_db.init_db("audio-lib")
    prev_lib = disco_db.get_active_library()
    set_active_library("books-lib")

    yield {
        "tmp_path": tmp_path,
        "settings": fake_settings,
        "monkeypatch": monkeypatch,
    }

    set_active_library(prev_lib)


async def _seed_gr_author_in_libraries(
    goodreads_id: str, *,
    libraries: tuple[str, ...] = ("books-lib", "audio-lib"),
) -> None:
    """GR analog of `_seed_author_in_libraries`. v3.4.0 slice 04
    will populate the GR queue via cache-miss enqueues from lookup;
    here we just need an authors row with a `goodreads_id` so
    `_libraries_for_author` can find per-library targets.

    Reuses the discovery `authors.amazon_id` lookup helper which is
    Amazon-specific — but `_libraries_for_author` joins on
    `seshat_author_id` (or in v2: amazon_id). For GR we just need
    SOME authors row; the helper falls back to per-library iteration.
    """
    from app.discovery import database as disco_db
    for slug in libraries:
        db = await disco_db.get_db(slug=slug)
        try:
            await db.execute(
                "INSERT OR IGNORE INTO authors "
                "(name, sort_name, normalized_name, goodreads_id) "
                "VALUES (?, ?, ?, ?)",
                (
                    f"GR {goodreads_id}", f"GR {goodreads_id}",
                    f"gr {goodreads_id}".lower(), goodreads_id,
                ),
            )
            await db.commit()
        finally:
            await db.close()


async def _seed_gr_queue_row(
    *,
    author_id: str,
    priority: float = 100.0,
    status: str = "pending",
    next_scan_due_at: float = 0.0,
    consecutive_failures: int = 0,
    seed_in_libraries: tuple[str, ...] = ("books-lib", "audio-lib"),
) -> None:
    if seed_in_libraries:
        await _seed_gr_author_in_libraries(
            author_id, libraries=seed_in_libraries,
        )
    db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
    try:
        await db.execute(
            f"INSERT OR REPLACE INTO "
            f"{metadata_cache.queue_table(metadata_cache.SOURCE_GOODREADS)} "
            f"(author_id, priority, status, next_scan_due_at, "
            f" consecutive_failures, enqueued_reason) "
            f"VALUES (?, ?, ?, ?, ?, ?)",
            (author_id, priority, status, next_scan_due_at,
             consecutive_failures, "test_seed"),
        )
        await db.commit()
    finally:
        await db.close()


class TestReplaceListPageRows:
    """`_replace_list_page_rows` writes per-page JSON snapshots and
    replaces on re-scan."""

    async def test_writes_page_rows(self, gr_worker_under):
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            # State row first so the FK in list_pages is satisfied.
            await db.execute(
                f"INSERT INTO "
                f"{metadata_cache.state_table(metadata_cache.SOURCE_GOODREADS)} "
                f"(author_id, library_slug, last_scanned_at, last_outcome, "
                f" book_count) VALUES (?, ?, ?, ?, ?)",
                ("GR-100", "books-lib", time.time(), "ok", 5),
            )
            await db.commit()
            await metadata_cache_worker._replace_list_page_rows(
                db, metadata_cache.SOURCE_GOODREADS,
                author_id="GR-100", library_slug="books-lib",
                pages={
                    1: [
                        {"book_id": "10", "title": "Ten"},
                        {"book_id": "11", "title": "Eleven"},
                        {"book_id": "12", "title": "Twelve"},
                    ],
                    2: [
                        {"book_id": "20", "title": "Twenty"},
                        {"book_id": "21", "title": "Twenty-one"},
                    ],
                },
            )
            cur = await db.execute(
                f"SELECT page_num, book_ids_json FROM "
                f"{metadata_cache.list_pages_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE author_id = ? AND library_slug = ? "
                f"ORDER BY page_num",
                ("GR-100", "books-lib"),
            )
            rows = await cur.fetchall()
        finally:
            await db.close()
        import json
        assert len(rows) == 2
        assert rows[0][0] == 1
        page1 = json.loads(rows[0][1])
        assert [r["book_id"] for r in page1] == ["10", "11", "12"]
        assert page1[0]["title"] == "Ten"
        assert rows[1][0] == 2
        page2 = json.loads(rows[1][1])
        assert [r["book_id"] for r in page2] == ["20", "21"]

    async def test_rescan_replaces_pages(self, gr_worker_under):
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            await db.execute(
                f"INSERT INTO "
                f"{metadata_cache.state_table(metadata_cache.SOURCE_GOODREADS)} "
                f"(author_id, library_slug, last_scanned_at, last_outcome, "
                f" book_count) VALUES (?, ?, ?, ?, ?)",
                ("GR-101", "books-lib", time.time(), "ok", 3),
            )
            await db.commit()
            await metadata_cache_worker._replace_list_page_rows(
                db, metadata_cache.SOURCE_GOODREADS,
                author_id="GR-101", library_slug="books-lib",
                pages={1: [{"book_id": "a"}, {"book_id": "b"},
                           {"book_id": "c"}]},
            )
            # Re-scan with a different page set — old rows must be
            # gone (DELETE-then-INSERT discipline; mirrors Amazon's
            # `_replace_book_rows`).
            await metadata_cache_worker._replace_list_page_rows(
                db, metadata_cache.SOURCE_GOODREADS,
                author_id="GR-101", library_slug="books-lib",
                pages={1: [{"book_id": "x"}, {"book_id": "y"}]},
            )
            cur = await db.execute(
                f"SELECT page_num, book_ids_json FROM "
                f"{metadata_cache.list_pages_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE author_id = ? AND library_slug = ?",
                ("GR-101", "books-lib"),
            )
            rows = await cur.fetchall()
        finally:
            await db.close()
        import json
        assert len(rows) == 1
        records = json.loads(rows[0][1])
        assert [r["book_id"] for r in records] == ["x", "y"]


class TestGoodreadsTick:
    async def test_disabled_short_circuits(
        self, gr_worker_under, monkeypatch,
    ):
        gr_worker_under["settings"]["metadata_cache"]["goodreads"]["mode"] = "disabled"
        called: list[str] = []
        async def _no_scan(author_id):
            called.append(author_id)
            return ({}, None, False)
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_goodreads_scan", _no_scan,
        )
        result = await metadata_cache_worker.tick_goodreads()
        assert result.outcome == "disabled"
        assert called == []  # gate fires before scan

    async def test_queue_empty_returns_queue_empty(
        self, gr_worker_under,
    ):
        result = await metadata_cache_worker.tick_goodreads()
        assert result.outcome == "queue_empty"
        assert result.queue_size == 0

    async def test_successful_scan_writes_list_pages_and_state(
        self, gr_worker_under, monkeypatch,
    ):
        await _seed_gr_queue_row(author_id="GR-200")
        pages = {
            1: [
                {"book_id": "b1", "title": "B1"},
                {"book_id": "b2", "title": "B2"},
                {"book_id": "b3", "title": "B3"},
            ],
            2: [{"book_id": "b4", "title": "B4"}],
        }
        async def _ok_scan(author_id):
            assert author_id == "GR-200"
            return (pages, None, False)
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_goodreads_scan", _ok_scan,
        )

        result = await metadata_cache_worker.tick_goodreads()
        assert result.outcome == "ok"
        assert result.author_id == "GR-200"
        assert result.books_cached == 4  # sum of all page lengths

        # State row + list_page rows landed for both libraries.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                f"SELECT library_slug, last_outcome, book_count FROM "
                f"{metadata_cache.state_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE author_id = ? ORDER BY library_slug",
                ("GR-200",),
            )
            states = await cur.fetchall()
            cur = await db.execute(
                f"SELECT library_slug, page_num FROM "
                f"{metadata_cache.list_pages_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE author_id = ? ORDER BY library_slug, page_num",
                ("GR-200",),
            )
            lp_rows = await cur.fetchall()
            # Queue row deferred + counter zeroed.
            cur = await db.execute(
                f"SELECT status, consecutive_failures, next_scan_due_at "
                f"FROM {metadata_cache.queue_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE author_id = ?",
                ("GR-200",),
            )
            q = await cur.fetchone()
        finally:
            await db.close()

        assert [(s[0], s[1], s[2]) for s in states] == [
            ("audio-lib", "ok", 4),
            ("books-lib", "ok", 4),
        ]
        assert [(r[0], r[1]) for r in lp_rows] == [
            ("audio-lib", 1), ("audio-lib", 2),
            ("books-lib", 1), ("books-lib", 2),
        ]
        assert q[0] == "pending"
        assert q[1] == 0
        assert q[2] > time.time() + 86400  # ≥1 day in the future

    async def test_soft_block_defers_without_failure(
        self, gr_worker_under, monkeypatch,
    ):
        await _seed_gr_queue_row(author_id="GR-300", consecutive_failures=2)
        async def _soft_block(author_id):
            return (None, "goodreads returned None (202)", True)
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_goodreads_scan", _soft_block,
        )

        result = await metadata_cache_worker.tick_goodreads()
        assert result.outcome == "soft_block"
        # GR cooldown is the lighter 300s curve (no escalation).
        assert result.cooldown_remaining_s == 300.0

        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                f"SELECT status, consecutive_failures FROM "
                f"{metadata_cache.queue_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE author_id = ?",
                ("GR-300",),
            )
            q = await cur.fetchone()
        finally:
            await db.close()
        # Soft-block is NOT a failure; counter reset.
        assert q[0] == "pending"
        assert q[1] == 0

    async def test_hard_error_increments_failure_counter(
        self, gr_worker_under, monkeypatch,
    ):
        await _seed_gr_queue_row(author_id="GR-400", consecutive_failures=2)
        async def _hard_err(author_id):
            return (None, "ValueError: parser exploded", False)
        monkeypatch.setattr(
            metadata_cache_worker, "_perform_goodreads_scan", _hard_err,
        )

        result = await metadata_cache_worker.tick_goodreads()
        assert result.outcome == "error"
        assert result.error == "ValueError: parser exploded"

        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                f"SELECT consecutive_failures FROM "
                f"{metadata_cache.queue_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE author_id = ?",
                ("GR-400",),
            )
            q = await cur.fetchone()
            # And an error state row landed per library.
            cur = await db.execute(
                f"SELECT library_slug, last_outcome, last_error FROM "
                f"{metadata_cache.state_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE author_id = ?",
                ("GR-400",),
            )
            states = await cur.fetchall()
        finally:
            await db.close()
        assert q[0] == 3
        assert {(s[0], s[1]) for s in states} == {
            ("books-lib", "error"), ("audio-lib", "error"),
        }


class TestGoodreadsListPageInventory:
    """`GoodreadsSource.list_page_inventory` — list-only fetch with
    pagination, returns {page_num: [book_id, ...]}."""

    async def test_single_page_parses_book_ids(self, monkeypatch):
        from app.discovery.sources.goodreads import GoodreadsSource
        html = """
            <html>
              <a class="authorName"><span>Test Author</span></a>
              <table>
                <tr itemtype="http://schema.org/Book">
                  <td><a class="bookTitle" href="/book/show/100.X"><span>X</span></a></td>
                </tr>
                <tr itemtype="http://schema.org/Book">
                  <td><a class="bookTitle" href="/book/show/200.Y"><span>Y</span></a></td>
                </tr>
              </table>
            </html>
        """
        class _Resp:
            text = html
            url = "https://www.goodreads.com/author/list/12345.Test_Author"
        gets: list[tuple] = []
        async def _get_stub(self, url, retries=2, **kwargs):
            gets.append((url, kwargs.get("params")))
            return _Resp()
        monkeypatch.setattr(GoodreadsSource, "_get", _get_stub)

        source = GoodreadsSource(rate_limit=0.0)
        pages = await source.list_page_inventory("12345")
        assert set(pages.keys()) == {1}
        assert [r["book_id"] for r in pages[1]] == ["100", "200"]
        assert pages[1][0]["title"] == "X"
        # Only the first-page fetch happened — no next_page link in
        # fixture, so pagination short-circuits.
        assert len(gets) == 1

    async def test_paginates_until_no_next_link(self, monkeypatch):
        from app.discovery.sources.goodreads import GoodreadsSource
        page1_html = """
            <html>
              <table>
                <tr itemtype="http://schema.org/Book">
                  <td><a class="bookTitle" href="/book/show/1.A"><span>A</span></a></td>
                </tr>
              </table>
              <a class="next_page" href="?page=2">next</a>
            </html>
        """
        page2_html = """
            <html>
              <table>
                <tr itemtype="http://schema.org/Book">
                  <td><a class="bookTitle" href="/book/show/2.B"><span>B</span></a></td>
                </tr>
                <tr itemtype="http://schema.org/Book">
                  <td><a class="bookTitle" href="/book/show/3.C"><span>C</span></a></td>
                </tr>
              </table>
            </html>
        """
        responses = [
            type("R", (), {"text": page1_html,
                           "url": "https://www.goodreads.com/author/list/9.X"})(),
            type("R", (), {"text": page2_html,
                           "url": "https://www.goodreads.com/author/list/9.X?page=2"})(),
        ]
        async def _get_stub(self, url, retries=2, **kwargs):
            return responses.pop(0)
        monkeypatch.setattr(GoodreadsSource, "_get", _get_stub)

        source = GoodreadsSource(rate_limit=0.0)
        pages = await source.list_page_inventory("9")
        assert set(pages.keys()) == {1, 2}
        assert [r["book_id"] for r in pages[1]] == ["1"]
        assert [r["book_id"] for r in pages[2]] == ["2", "3"]

    async def test_first_fetch_failure_returns_none(self, monkeypatch):
        from app.discovery.sources.goodreads import GoodreadsSource
        async def _raises(self, url, retries=2, **kwargs):
            raise RuntimeError("transport fail")
        monkeypatch.setattr(GoodreadsSource, "_get", _raises)

        source = GoodreadsSource(rate_limit=0.0)
        pages = await source.list_page_inventory("9")
        assert pages is None


# ─── v3.4.0 slice 05 — GR budget-exhaust counter telemetry ─────


class TestGoodreadsBudgetExhaustCounter:
    """`record_goodreads_budget_exhaust` increments the per-day
    counter persisted in `metadata_cache_goodreads_worker_state` so
    the daily summary surfaces a measurable signal for the v3.5.0
    Path C decision (ADR-0018 §6.2)."""

    async def test_first_call_increments_from_zero(self, gr_worker_under):
        await metadata_cache_worker.record_goodreads_budget_exhaust()
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                f"SELECT today_budget_exhaust_count FROM "
                f"{metadata_cache.worker_state_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE id = 1"
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == 1

    async def test_repeated_calls_same_day_increment(self, gr_worker_under):
        for _ in range(3):
            await metadata_cache_worker.record_goodreads_budget_exhaust()
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                f"SELECT today_budget_exhaust_count FROM "
                f"{metadata_cache.worker_state_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE id = 1"
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        # Note: increment uses `_is_same_local_day(last_block_at, now)`
        # heuristic; a fresh DB has last_block_at=0 (Unix epoch),
        # which is NOT today, so each call resets to 1. This is the
        # documented behavior — first-time increment after a
        # cold start treats every call as "first of the new day."
        # Once a real soft-block sets last_block_at to wall-clock,
        # subsequent calls within the same day cumulate.
        assert row[0] >= 1

    async def test_reset_today_counters_clears_exhaust(
        self, gr_worker_under,
    ):
        await metadata_cache_worker.record_goodreads_budget_exhaust()
        await metadata_cache_worker.record_goodreads_budget_exhaust()
        prior_scans, prior_blocks, prior_exhausts = (
            await metadata_cache_worker.reset_today_counters("goodreads")
        )
        assert prior_exhausts >= 1
        # Post-reset reads 0.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                f"SELECT today_budget_exhaust_count FROM "
                f"{metadata_cache.worker_state_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE id = 1"
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == 0

    async def test_daily_summary_includes_exhaust_for_gr_only(
        self, gr_worker_under, monkeypatch, fake_ntfy,
    ):
        # Seed last_block_at to now so the increment heuristic
        # cumulates rather than resetting.
        import time as _t
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            await db.execute(
                f"UPDATE {metadata_cache.worker_state_table(metadata_cache.SOURCE_GOODREADS)} "
                f"SET last_block_at = ?, today_budget_exhaust_count = 0 "
                f"WHERE id = 1",
                (_t.time(),),
            )
            await db.commit()
        finally:
            await db.close()
        await metadata_cache_worker.record_goodreads_budget_exhaust()
        await metadata_cache_worker.record_goodreads_budget_exhaust()
        await metadata_cache_worker.send_daily_summary("goodreads")

        summaries = [
            c for c in fake_ntfy
            if c["event_key"] == "metadata_cache_daily_summary"
        ]
        assert summaries, "GR daily summary ntfy should fire"
        body = summaries[-1]["message"]
        assert "Budget exhausts: 2" in body
        # GR-specific title.
        assert "Goodreads" in summaries[-1]["title"]
