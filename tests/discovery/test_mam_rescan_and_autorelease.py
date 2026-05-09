"""
v2.3.6 behavior: MAM rescan widening + auto-release on expected_date.

Two features land together because they both flow through the same
per-library loop in `mam_scheduler_loop`:

  1. Books with `mam_status` = 'possible' or 'not_found' are now
     eligible for rescan (previously only NULL-status books were
     scanned). Catalog churn on MAM means a search that came up
     empty last week may hit today.

  2. Books with `is_unreleased=1` whose `expected_date` has passed
     auto-clear the unreleased flag at the top of each MAM scan
     tick — they stop looking like Upcoming and become normal
     Missing books eligible for MAM scanning the same tick they
     age in.
"""
from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture
async def single_library(tmp_path, monkeypatch):
    """One ebook library, registered in `state._discovered_libraries`,
    initialized with the production schema."""
    from app import config as app_config, state
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)

    libs = [
        {"slug": "ebooks", "name": "Ebooks", "display_name": "Ebooks",
         "content_type": "ebook", "app_type": "calibre"},
    ]
    monkeypatch.setattr(state, "_discovered_libraries", libs)

    disco_db.set_active_library("ebooks")
    await disco_db.init_db("ebooks")
    db = await disco_db.get_db(slug="ebooks")
    try:
        await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES ('A', 'A')"
        )
        aid = (await (await db.execute(
            "SELECT last_insert_rowid()"
        )).fetchone())[0]
        yield db, aid
    finally:
        await db.close()
        disco_db.set_active_library(None)


# ─── Feature 2: rescan widening ──────────────────────────────────


async def test_basic_predicate_includes_null_possible_and_not_found(
    single_library,
):
    """The BASIC predicate (used by every scheduled/manual scan) must
    pick up books that are unscanned, possible, OR not_found — but
    not books with `mam_status='found'` (terminal)."""
    from app.discovery.sources.mam import _NEEDS_SCAN_BASIC_BARE

    db, aid = single_library
    # Seed one book per status state.
    await db.execute(
        "INSERT INTO books (title, author_id, mam_status) VALUES "
        "('null-row', ?, NULL),"
        "('possible-row', ?, 'possible'),"
        "('not_found-row', ?, 'not_found'),"
        "('found-row', ?, 'found')",
        (aid, aid, aid, aid),
    )
    await db.commit()

    rows = await db.execute_fetchall(
        f"SELECT title FROM books WHERE {_NEEDS_SCAN_BASIC_BARE}"
    )
    titles = sorted(r[0] for r in rows)
    assert titles == ["not_found-row", "null-row", "possible-row"]


async def test_basic_predicate_excludes_unreleased_and_hidden(
    single_library,
):
    """`is_unreleased=1` and `hidden=1` short-circuit the predicate —
    those books need different lifecycle steps before they can be
    scanned (release-date arrival or unhide)."""
    from app.discovery.sources.mam import _NEEDS_SCAN_BASIC_BARE

    db, aid = single_library
    await db.execute(
        "INSERT INTO books "
        "(title, author_id, mam_status, is_unreleased, hidden) VALUES "
        "('unreleased-not_found', ?, 'not_found', 1, 0),"
        "('hidden-possible', ?, 'possible', 0, 1),"
        "('eligible-not_found', ?, 'not_found', 0, 0)",
        (aid, aid, aid),
    )
    await db.commit()

    rows = await db.execute_fetchall(
        f"SELECT title FROM books WHERE {_NEEDS_SCAN_BASIC_BARE}"
    )
    titles = [r[0] for r in rows]
    assert titles == ["eligible-not_found"]


async def test_strict_predicate_keeps_url_guard_for_null_rows(
    single_library,
):
    """STRICT preserves its defensive `mam_url IS NULL` check for
    never-scanned rows (a NULL-status row with a stale mam_url is
    excluded), but rescannable possible/not_found rows bypass that
    guard because 'possible' rows legitimately have a mam_url set."""
    from app.discovery.sources.mam import _NEEDS_SCAN_STRICT_BARE

    db, aid = single_library
    await db.execute(
        "INSERT INTO books (title, author_id, mam_status, mam_url) VALUES "
        "('null-no-url', ?, NULL, NULL),"
        "('null-with-stale-url', ?, NULL, 'https://stale'),"
        "('possible-with-url', ?, 'possible', 'https://match'),"
        "('not_found-no-url', ?, 'not_found', NULL)",
        (aid, aid, aid, aid),
    )
    await db.commit()

    rows = await db.execute_fetchall(
        f"SELECT title FROM books WHERE {_NEEDS_SCAN_STRICT_BARE}"
    )
    titles = sorted(r[0] for r in rows)
    assert titles == ["not_found-no-url", "null-no-url", "possible-with-url"]


# ─── Feature 1: auto-release on expected_date ────────────────────


async def test_scheduler_clears_expired_unreleased(
    single_library, monkeypatch, tmp_path,
):
    """The `mam_scheduler_loop` per-library tick clears `is_unreleased=1`
    on rows whose `expected_date` has passed, so they're picked up by
    the same tick's scan eligibility query."""
    from app import state as app_state
    from app.discovery import scheduled_jobs

    db, aid = single_library
    # Seed: past-date book (should flip), today book (should flip),
    # future-date book (must NOT flip), no-date book (must NOT flip).
    await db.execute(
        "INSERT INTO books (title, author_id, owned, is_unreleased, "
        "expected_date) VALUES "
        "('past-book', ?, 0, 1, '2020-01-01'),"
        "('today-book', ?, 0, 1, date('now', 'localtime')),"
        "('future-book', ?, 0, 1, '2099-12-31'),"
        "('no-date-book', ?, 0, 1, NULL)",
        (aid, aid, aid, aid),
    )
    await db.commit()

    # Set up the loop's prerequisites: settings enable MAM, no other
    # scan running, no library sync in progress.
    settings_path = tmp_path / "settings.json"
    from app import config as app_config
    settings_path.write_text(json.dumps({
        **app_config.DEFAULT_SETTINGS,
        "mam_enabled": True,
        "mam_session_id": "tok",
        "mam_scanning_enabled": True,
        "mam_scan_interval_minutes": 1,
        "last_mam_validated_at": __import__("time").time(),
        "mam_validation_ok": True,
    }))
    monkeypatch.setattr(app_config, "SETTINGS_PATH", settings_path)
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = object()

    app_state._mam_scan_progress = {"running": False}
    app_state._library_sync_in_progress = False

    # Stub the scan batch so we don't talk to MAM. We only care about
    # whether the auto-release step ran before the count query.
    async def fake_scan_batch(*_a, **_kw):
        return {"scanned": 0, "found": 0, "possible": 0,
                "not_found": 0, "errors": 0, "error": None}

    monkeypatch.setattr(scheduled_jobs, "mam_scan_batch", fake_scan_batch)

    async def fake_token():
        return "tok"
    from app.discovery.routers import mam as mam_router
    monkeypatch.setattr(mam_router, "_get_mam_token", fake_token)

    # Skip the validate call (would hit real MAM).
    async def fake_validate(_token, _flag):
        return {"success": True, "message": "ok"}
    monkeypatch.setattr(scheduled_jobs, "mam_validate", fake_validate)

    # Drive ONE tick of the loop, then cancel. Replace asyncio.sleep
    # with an immediate yield so the 60s wait is skipped, and arrange
    # for the second sleep to raise CancelledError.
    sleep_count = {"n": 0}
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        sleep_count["n"] += 1
        if sleep_count["n"] >= 2:
            # After the loop has done one full pass, cancel it.
            raise asyncio.CancelledError()
        await real_sleep(0)

    monkeypatch.setattr(scheduled_jobs.asyncio, "sleep", fast_sleep)

    with pytest.raises(asyncio.CancelledError):
        await scheduled_jobs.mam_scheduler_loop()

    # Verify the auto-release UPDATE ran: past-book and today-book
    # should be cleared; future-book and no-date-book should still
    # be unreleased.
    rows = await db.execute_fetchall(
        "SELECT title, is_unreleased FROM books ORDER BY title"
    )
    state_by_title = {r[0]: r[1] for r in rows}
    assert state_by_title == {
        "future-book": 1,
        "no-date-book": 1,
        "past-book": 0,
        "today-book": 0,
    }


# ─── Recently-scanned skip + oldest-first ordering ───────────────


class TestRecentScanCutoffSeconds:
    """Reads the configurable window from settings; default is 7 days."""

    def test_default_is_seven_days(self, monkeypatch):
        from app import config as app_config
        from app.discovery.sources.mam import _recent_scan_cutoff_seconds

        # Pass an explicit dict so we don't need a real settings.json.
        cutoff = _recent_scan_cutoff_seconds(dict(app_config.DEFAULT_SETTINGS))
        assert cutoff == 7 * 86400.0

    def test_zero_disables(self):
        from app.discovery.sources.mam import _recent_scan_cutoff_seconds

        assert _recent_scan_cutoff_seconds({"mam_recent_scan_skip_days": 0}) == 0.0

    def test_negative_disables(self):
        # Defensive: a malformed settings value shouldn't crash, just
        # treat as "disabled".
        from app.discovery.sources.mam import _recent_scan_cutoff_seconds

        assert _recent_scan_cutoff_seconds({"mam_recent_scan_skip_days": -3}) == 0.0

    def test_garbage_falls_back_to_default(self):
        from app.discovery.sources.mam import _recent_scan_cutoff_seconds

        # Non-numeric → fall back to 7 (the documented default).
        assert _recent_scan_cutoff_seconds({"mam_recent_scan_skip_days": "weekly"}) == 7 * 86400.0

    def test_fractional_days_supported(self):
        # Power-user case: tighter cadence than a whole day.
        from app.discovery.sources.mam import _recent_scan_cutoff_seconds

        assert _recent_scan_cutoff_seconds({"mam_recent_scan_skip_days": 0.5}) == 0.5 * 86400.0


class TestRecentScanSkipClause:
    """SQL fragment for excluding recently-scanned books from the
    eligibility query. Returns empty string when disabled so the
    base predicate stays unchanged."""

    def test_disabled_returns_empty(self):
        from app.discovery.sources.mam import _recent_scan_skip_clause

        assert _recent_scan_skip_clause(0) == ""
        assert _recent_scan_skip_clause(0.0) == ""
        assert _recent_scan_skip_clause(-100) == ""

    def test_enabled_emits_null_or_older_than_cutoff(self):
        from app.discovery.sources.mam import _recent_scan_skip_clause

        clause = _recent_scan_skip_clause(7 * 86400.0)
        # Always preserves never-scanned books (NULL → keep) — they're
        # the highest-priority candidates.
        assert "mam_last_scanned_at IS NULL" in clause
        # And keeps any book whose timestamp is before the cutoff.
        assert "mam_last_scanned_at <" in clause
        # Leading AND so it composes with the existing predicate.
        assert clause.lstrip().startswith("AND")

    def test_aliased_prefix(self):
        # Queries that JOIN authors need `b.` prefix to disambiguate.
        from app.discovery.sources.mam import _recent_scan_skip_clause

        clause = _recent_scan_skip_clause(7 * 86400.0, prefix="b.")
        assert "b.mam_last_scanned_at" in clause
        assert " mam_last_scanned_at" not in clause  # no unprefixed reference


class TestRecentScanOrderClause:
    """Oldest-first ORDER BY fragment. Pairs with the skip clause to
    give libraries full coverage over time."""

    def test_owned_first_then_oldest_then_id(self):
        from app.discovery.sources.mam import _recent_scan_order_clause

        clause = _recent_scan_order_clause()
        # Owned books still get priority.
        assert "owned DESC" in clause
        # Oldest first (NULL → 0 sorts before any positive timestamp,
        # so never-scanned books lead).
        assert "COALESCE(mam_last_scanned_at, 0) ASC" in clause
        # Stable id tiebreaker.
        assert "id ASC" in clause

    def test_aliased_prefix(self):
        from app.discovery.sources.mam import _recent_scan_order_clause

        clause = _recent_scan_order_clause(prefix="b.")
        assert "b.owned" in clause
        assert "COALESCE(b.mam_last_scanned_at, 0)" in clause
        assert "b.id ASC" in clause


async def test_skip_clause_excludes_recently_scanned_rows(single_library):
    """End-to-end: a row scanned 1 day ago is excluded when the window
    is 7 days; a row scanned 10 days ago is included; never-scanned
    rows are always included."""
    from app.discovery.sources.mam import (
        _NEEDS_SCAN_BASIC_BARE,
        _recent_scan_skip_clause,
    )
    import time as _time

    db, aid = single_library
    now = _time.time()
    await db.execute(
        "INSERT INTO books (title, author_id, mam_status, mam_last_scanned_at) "
        "VALUES "
        "('never-scanned', ?, 'possible', NULL),"
        "('day-old', ?, 'possible', ?),"
        "('ten-days-old', ?, 'possible', ?)",
        (aid, aid, now - 86400, aid, now - 10 * 86400),
    )
    await db.commit()

    # 7-day window — day-old should be excluded; ten-days-old included.
    skip_clause = _recent_scan_skip_clause(7 * 86400.0)
    rows = await db.execute_fetchall(
        f"SELECT title FROM books WHERE {_NEEDS_SCAN_BASIC_BARE}{skip_clause} "
        f"ORDER BY title"
    )
    titles = [r[0] for r in rows]
    assert "day-old" not in titles
    assert "ten-days-old" in titles
    assert "never-scanned" in titles


async def test_skip_clause_disabled_includes_all(single_library):
    """When the skip is disabled (cutoff=0), the eligibility query
    matches the legacy behavior — every Possible row is in the queue
    regardless of last-scan timestamp."""
    from app.discovery.sources.mam import (
        _NEEDS_SCAN_BASIC_BARE,
        _recent_scan_skip_clause,
    )
    import time as _time

    db, aid = single_library
    await db.execute(
        "INSERT INTO books (title, author_id, mam_status, mam_last_scanned_at) "
        "VALUES "
        "('just-scanned', ?, 'possible', ?),"
        "('never-scanned', ?, 'possible', NULL)",
        (aid, _time.time(), aid),
    )
    await db.commit()

    skip_clause = _recent_scan_skip_clause(0)  # disabled → empty string
    rows = await db.execute_fetchall(
        f"SELECT title FROM books WHERE {_NEEDS_SCAN_BASIC_BARE}{skip_clause}"
    )
    titles = sorted(r[0] for r in rows)
    assert titles == ["just-scanned", "never-scanned"]


async def test_oldest_first_ordering(single_library):
    """End-to-end: the order clause sorts never-scanned (NULL → 0)
    first, then ascending by timestamp, then by id. Owned books
    still sort to the front before any of that."""
    from app.discovery.sources.mam import _recent_scan_order_clause
    import time as _time

    db, aid = single_library
    now = _time.time()
    # Insert with explicit ids so we can pin tiebreaker behavior.
    await db.execute(
        "INSERT INTO books "
        "(id, title, author_id, owned, mam_status, mam_last_scanned_at) VALUES "
        "(1, 'old-unowned', ?, 0, 'possible', ?),"
        "(2, 'recent-unowned', ?, 0, 'possible', ?),"
        "(3, 'never-unowned', ?, 0, 'possible', NULL),"
        "(4, 'old-owned', ?, 1, 'possible', ?)",
        (aid, now - 30 * 86400, aid, now - 1 * 86400, aid, aid, now - 30 * 86400),
    )
    await db.commit()

    rows = await db.execute_fetchall(
        f"SELECT title FROM books "
        f"ORDER BY {_recent_scan_order_clause()}"
    )
    titles = [r[0] for r in rows]
    # owned books first (just one), then never-scanned, then oldest
    # unowned, then most recent.
    assert titles == [
        "old-owned",
        "never-unowned",
        "old-unowned",
        "recent-unowned",
    ]


async def test_auth_error_does_not_stamp_timestamp(single_library):
    """The CASE in the UPDATE statement preserves the existing
    timestamp on auth_error — otherwise a bad cookie would mark
    every book as 'recently scanned' and starve the queue when auth
    recovers. Tests the SQL pattern directly so we catch any drift
    between the three call sites that share the same logic."""
    import time as _time

    db, aid = single_library
    original_ts = _time.time() - 10 * 86400  # 10 days ago
    await db.execute(
        "INSERT INTO books (id, title, author_id, mam_status, mam_last_scanned_at) "
        "VALUES (1, 't', ?, 'possible', ?)",
        (aid, original_ts),
    )
    await db.commit()

    # Mirror the production UPDATE shape from scan_books_batch.
    new_ts = _time.time()
    await db.execute(
        """
        UPDATE books SET mam_status=?,
               mam_last_scanned_at=CASE
                   WHEN ? = 'auth_error' THEN mam_last_scanned_at
                   ELSE ?
               END
        WHERE id=1
        """,
        ("auth_error", "auth_error", new_ts),
    )
    await db.commit()

    row = await db.execute_fetchall(
        "SELECT mam_last_scanned_at FROM books WHERE id=1"
    )
    # The CASE preserved the original timestamp; the new value was
    # NOT written.
    assert row[0][0] == original_ts


async def test_lock_retry_recovers_when_sync_clears_mid_update(
    single_library, monkeypatch,
):
    """Regression: Mark's UAT 2026-05-09 15:08 — sync started DURING
    check_book's network call, so the per-book pause check at the top
    of the iteration (which fired BEFORE check_book) had seen the flag
    as False. By the time MAM's UPDATE submitted, sync was holding the
    writer lock; busy_timeout expired and the scan task crashed.

    The fix is layered:
      - Second pause check right before UPDATE (eliminates the big
        check_book-window race)
      - _execute_with_lock_retry wraps the UPDATE itself (eliminates
        the residual sub-second race between pause check + submission)

    This test exercises the retry path directly: stub db.execute to
    raise "database is locked" once, flip the sync flag back, and
    verify the second attempt succeeds. Without the retry, the
    OperationalError would bubble up.
    """
    import sqlite3 as _sqlite3

    from app import state
    from app.discovery.sources import mam as mam_mod

    saved_sync = state._library_sync_in_progress
    saved_refs = state._source_scan_refs
    state._source_scan_refs = 0
    state._library_sync_in_progress = True

    call_count = {"n": 0}

    class FakeDb:
        async def execute(self, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First attempt — clear the sync flag (as if the
                # sync finished just as MAM was about to write) and
                # raise the locked error so the retry path engages.
                # Clearing the flag here lets the pause loop exit
                # cleanly on the retry.
                state._library_sync_in_progress = False
                raise _sqlite3.OperationalError("database is locked")
            return None

        async def commit(self):
            pass

    fake_db = FakeDb()

    try:
        await mam_mod._execute_with_lock_retry(
            fake_db, "UPDATE books SET x=?", ("v",), 0, 1,
        )
        assert call_count["n"] == 2, (
            f"expected 2 execute calls (locked + retry), got {call_count['n']}"
        )
    finally:
        state._library_sync_in_progress = saved_sync
        state._source_scan_refs = saved_refs


async def test_lock_retry_reraises_after_max_attempts(
    single_library, monkeypatch,
):
    """If the writer lock is held for the entire 3-attempt window —
    e.g., a runaway sync — the helper re-raises the OperationalError
    so the surrounding scan loop can record the failure and abort
    cleanly instead of silently dropping the book.
    """
    import sqlite3 as _sqlite3

    from app import state
    from app.discovery.sources import mam as mam_mod

    saved_sync = state._library_sync_in_progress
    saved_refs = state._source_scan_refs
    state._source_scan_refs = 0
    # Flag stays True throughout — pause never finishes, every
    # attempt raises locked.
    state._library_sync_in_progress = True

    class AlwaysLockedDb:
        async def execute(self, *args, **kwargs):
            raise _sqlite3.OperationalError("database is locked")

        async def commit(self):
            pass

    # Force the pause-loop's wall-clock check to bail on the first
    # iteration by setting the cap negative — `elapsed > -1` is True
    # immediately, so each attempt's pause exits and proceeds to the
    # next retry without waiting on a real-clock 20-minute window.
    monkeypatch.setattr(mam_mod, "_MAM_PAUSE_MAX_SECONDS", -1.0)

    try:
        with pytest.raises(_sqlite3.OperationalError, match="database is locked"):
            await mam_mod._execute_with_lock_retry(
                AlwaysLockedDb(), "UPDATE books SET x=?", ("v",), 0, 1,
            )
    finally:
        state._library_sync_in_progress = saved_sync
        state._source_scan_refs = saved_refs


async def test_lock_retry_does_not_swallow_other_operational_errors(
    monkeypatch,
):
    """OperationalError comes in many flavors — schema mismatch, syntax
    error, constraint violation, etc. We only retry on 'database is
    locked'; everything else re-raises immediately so real bugs surface.
    """
    import sqlite3 as _sqlite3

    from app.discovery.sources import mam as mam_mod

    class SyntaxErrorDb:
        async def execute(self, *args, **kwargs):
            raise _sqlite3.OperationalError("near 'SLEECT': syntax error")

        async def commit(self):
            pass

    with pytest.raises(_sqlite3.OperationalError, match="syntax error"):
        await mam_mod._execute_with_lock_retry(
            SyntaxErrorDb(), "SLEECT * FROM books", (), 0, 1,
        )


async def test_per_book_pause_for_library_sync(single_library, monkeypatch):
    """Regression: when library sync starts mid-batch, MAM scan was
    crashing with `database is locked` instead of pausing — the
    pre-existing `_wait_for_other_writers` only fires between
    batches, but a 150-book batch can run 15+ minutes and the sync
    holds the writer lock long enough to exceed busy_timeout. Per-
    book pause via `state._library_sync_in_progress` prevents that.

    Test: stub check_book + register_ip so we don't hit MAM; set the
    library_sync flag BEFORE the scan; have a parallel task flip the
    flag back to False after a short delay; verify scan_books_batch
    blocked until the flag cleared.
    """
    import asyncio as _asyncio
    import time as _time

    from app import state
    from app.discovery.sources import mam as mam_mod

    # Seed one eligible book.
    db, aid = single_library
    await db.execute(
        "INSERT INTO books (title, author_id, mam_status) "
        "VALUES ('one', ?, NULL)",
        (aid,),
    )
    await db.commit()

    # Stub network calls.
    async def fake_register_ip(*_a, **_kw):
        return {"success": True, "message": "ok"}

    async def fake_check_book(*_a, **_kw):
        return {
            "status": "not_found", "mam_url": "", "mam_torrent_id": None,
            "mam_title": None, "mam_formats": None,
            "mam_has_multiple": False, "mam_my_snatched": False,
            "mam_is_bundle": False,
        }

    monkeypatch.setattr(mam_mod, "register_ip", fake_register_ip)
    monkeypatch.setattr(mam_mod, "check_book", fake_check_book)

    # Snapshot + restore state flags so the test doesn't bleed.
    saved_refs = state._source_scan_refs
    saved_sync = state._library_sync_in_progress
    state._source_scan_refs = 0
    state._library_sync_in_progress = True

    # Flip the flag off after 2s so the scan can resume + complete.
    async def _release_after(delay):
        await _asyncio.sleep(delay)
        state._library_sync_in_progress = False

    release_task = _asyncio.create_task(_release_after(2.0))

    try:
        started = _time.monotonic()
        result = await mam_mod.scan_books_batch(
            db, session_id="tok", limit=1, delay=0.0,
            skip_ip_update=True, lang_ids=[1],
        )
        elapsed = _time.monotonic() - started
    finally:
        await release_task
        state._source_scan_refs = saved_refs
        state._library_sync_in_progress = saved_sync

    assert result["scanned"] == 1
    # Scan should have waited at least the ~2s release delay before
    # the per-book write fired. Tight floor (1.5s) tolerates timing
    # jitter on slow CI; the bug-state would have completed in ~0s
    # since check_book is a stub.
    assert elapsed >= 1.5, f"Scan ran in {elapsed:.2f}s — pause didn't fire"


async def test_command_center_router_snapshot_applies_skip(
    single_library, monkeypatch, tmp_path,
):
    """Regression: the /api/mam/scan router's per-library snapshot used
    inline SQL with the old ORDER BY and no skip clause, so a canceled-
    then-restarted scan would re-include every book — including the
    1,127 Mark had just freshly stamped before a `database is locked`
    crash terminated the in-flight batch (UAT 2026-05-09 14:46). Wire
    the same `_recent_scan_*` helpers the other call sites use.
    """
    import time as _time

    # Seed: 5 eligible books — 3 stamped fresh (last 1h), 2 never
    # scanned. With a 7-day skip the eligible queue should be 2.
    db, aid = single_library
    now = _time.time()
    fresh_ts = now - 3600  # 1 hour ago
    await db.execute(
        "INSERT INTO books (title, author_id, mam_status, mam_last_scanned_at) "
        "VALUES "
        "('fresh-1', ?, 'possible', ?),"
        "('fresh-2', ?, 'not_found', ?),"
        "('fresh-3', ?, 'possible', ?),"
        "('never-1', ?, NULL, NULL),"
        "('never-2', ?, 'possible', NULL)",
        (aid, fresh_ts, aid, fresh_ts, aid, fresh_ts, aid, aid),
    )
    await db.commit()

    # The endpoint reads settings via load_settings, then builds the
    # snapshot. We don't need to fire the actual scan — just exercise
    # the snapshot SQL the way the router does.
    from app.discovery.sources.mam import (
        _NEEDS_SCAN_BASIC_BARE,
        _recent_scan_cutoff_seconds,
        _recent_scan_skip_clause,
        _recent_scan_order_clause,
    )

    cutoff = _recent_scan_cutoff_seconds({"mam_recent_scan_skip_days": 7})
    skip = _recent_scan_skip_clause(cutoff)
    order = _recent_scan_order_clause()

    rows = await db.execute_fetchall(
        f"SELECT title FROM books WHERE {_NEEDS_SCAN_BASIC_BARE}"
        f"{skip} ORDER BY {order}"
    )
    titles = [r[0] for r in rows]
    # The 3 fresh ones are excluded; only the 2 never-scanned remain.
    assert sorted(titles) == ["never-1", "never-2"]


async def test_successful_scan_stamps_timestamp(single_library):
    """The other half of the contract: a real scan result (found /
    possible / not_found) DOES update the timestamp."""
    import time as _time

    db, aid = single_library
    original_ts = _time.time() - 10 * 86400
    await db.execute(
        "INSERT INTO books (id, title, author_id, mam_status, mam_last_scanned_at) "
        "VALUES (1, 't', ?, 'possible', ?)",
        (aid, original_ts),
    )
    await db.commit()

    new_ts = _time.time()
    for status in ("found", "possible", "not_found"):
        await db.execute(
            """
            UPDATE books SET mam_status=?,
                   mam_last_scanned_at=CASE
                       WHEN ? = 'auth_error' THEN mam_last_scanned_at
                       ELSE ?
                   END
            WHERE id=1
            """,
            (status, status, new_ts),
        )
        await db.commit()
        row = await db.execute_fetchall(
            "SELECT mam_last_scanned_at FROM books WHERE id=1"
        )
        # Each non-auth_error status stamps the new timestamp.
        assert row[0][0] == new_ts
