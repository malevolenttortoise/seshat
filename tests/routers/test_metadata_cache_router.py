"""
HTTP-level tests for `app.routers.metadata_cache` (v2.21.0 Phase E).

    GET   /api/v1/metadata-cache/{source}/status
    PATCH /api/v1/metadata-cache/{source}/settings
    POST  /api/v1/metadata-cache/{source}/reset-cooldown

Covers:
  - GET returns the worker + queue + cache + cooldown shape with
    real row counts derived from a seeded DB
  - PATCH enables / disables the worker and the change persists
    (read-back via subsequent GET)
  - POST reset-cooldown clears the IP penalty box even when the
    cooldown timestamp is in the future, and persists the clear
    so a re-import of the resolver module sees no cooldown
  - Unknown source 404s; non-amazon source 400s on reset-cooldown
"""
from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi import FastAPI

from app import config
from app.discovery import metadata_cache
from app.routers.metadata_cache import router as mc_router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(mc_router)
    return app


@pytest.fixture
async def cache_router_client(tmp_path, monkeypatch):
    """Per-test cache DB + isolated settings.json so toggle writes
    don't leak to the dev data dir."""
    from app import config as app_config
    from app.discovery import database as disco_db
    from app.discovery import amazon_author_id_resolver as resolver_module

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(metadata_cache, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({**config.DEFAULT_SETTINGS}))
    monkeypatch.setattr(app_config, "SETTINGS_PATH", settings_path)
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = object()

    # Mute the v2.20.3 cooldown-persistence path so resolver tests
    # don't write to a separately-tracked settings.json under the dev
    # data dir. Tests that need the persistence path enable it
    # explicitly.
    monkeypatch.setattr(
        resolver_module, "_persist_block_state", lambda **_: None,
    )
    resolver_module._blocked_until = 0.0
    resolver_module._block_reason = ""
    resolver_module._block_count = 0

    await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)

    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as ac:
        yield ac

    resolver_module._blocked_until = 0.0
    resolver_module._block_reason = ""
    resolver_module._block_count = 0
    app_config._settings_cache["data"] = None


# ─── GET /status ────────────────────────────────────────────────


async def _seed_some_rows() -> None:
    """Seed enough rows that the GET /status counts have something
    interesting to assert on. Mix of queue + state + books rows.

    Schema-v2: queue PK is `author_id` only — no `library_slug` on
    queue rows. State + books still partition per library."""
    db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
    try:
        # 3 queue rows: 2 pending, 1 failed_permanent.
        await db.execute(
            f"INSERT INTO {metadata_cache.queue_table()} "
            f"(author_id, priority, status, next_scan_due_at) "
            f"VALUES (?, ?, ?, ?), (?, ?, ?, ?), (?, ?, ?, ?)",
            (
                "B0AAAAAAAA", 100.0, "pending", 0.0,
                "B0BBBBBBBB", 200.0, "pending", 0.0,
                "B0CCCCCCCC", 100.0, "failed_permanent", 0.0,
            ),
        )
        # 2 state rows: 1 ok, 1 error.
        now = time.time()
        await db.execute(
            f"INSERT INTO {metadata_cache.state_table()} "
            f"(author_id, library_slug, last_scanned_at, last_outcome, "
            f" book_count) VALUES (?, ?, ?, ?, ?), (?, ?, ?, ?, ?)",
            (
                "B0DDDDDDDD", "books-lib", now, "ok", 3,
                "B0EEEEEEEE", "books-lib", now, "error", 0,
            ),
        )
        # 3 book rows under the ok author.
        for asin in ("B0BK000001", "B0BK000002", "B0BK000003"):
            await db.execute(
                f"INSERT INTO {metadata_cache.books_table()} "
                f"(author_id, library_slug, book_asin, title, cached_at) "
                f"VALUES (?, ?, ?, ?, ?)",
                ("B0DDDDDDDD", "books-lib", asin, "T", now),
            )
        await db.commit()
    finally:
        await db.close()


class TestGetStatus:
    async def test_get_status_returns_full_shape(self, cache_router_client):
        await _seed_some_rows()
        r = await cache_router_client.get("/api/v1/metadata-cache/amazon/status")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "amazon"
        # Enabled defaults to False (the safe ship default).
        assert body["enabled"] is False
        # Cooldown clear by default in this fixture.
        assert body["cooldown"]["blocked"] is False
        assert body["cooldown"]["remaining_s"] == 0
        # Queue counts came from the seeded rows.
        assert body["queue"]["pending"] == 2
        assert body["queue"]["failed_permanent"] == 1
        assert body["queue"]["total"] == 3
        # Cache counts.
        assert body["cache"]["state_rows"] == 2
        assert body["cache"]["ok_authors"] == 1
        assert body["cache"]["error_authors"] == 1
        assert body["cache"]["books_rows"] == 3
        # Worker singleton seeded by Phase B init; defaults are 0 / null.
        worker = body["worker"]
        assert worker["consecutive_blocks"] == 0
        assert worker["today_scan_count"] == 0
        assert worker["today_block_count"] == 0
        assert worker["last_heartbeat_at"] is None
        assert worker["seconds_since_heartbeat"] is None

    async def test_get_status_surfaces_active_cooldown(
        self, cache_router_client,
    ):
        from app.discovery import amazon_author_id_resolver as r
        r.record_amazon_soft_block("test 429", retry_after_s=120)
        resp = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/status"
        )
        body = resp.json()
        assert body["cooldown"]["blocked"] is True
        assert 100 <= body["cooldown"]["remaining_s"] <= 130
        assert body["cooldown"]["reason"] == "test 429"

    async def test_get_status_rejects_unknown_source(self, cache_router_client):
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/hardcover/status"
        )
        assert r.status_code == 404


# ─── PATCH /settings ────────────────────────────────────────────


class TestPatchSettings:
    async def test_patch_enables_worker(self, cache_router_client):
        # Default is disabled.
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/status"
        )
        assert r.json()["enabled"] is False

        # Flip on.
        r = await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={"enabled": True},
        )
        assert r.status_code == 200
        assert r.json()["enabled"] is True

        # Read back via status.
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/status"
        )
        assert r.json()["enabled"] is True

    async def test_patch_disables_worker(self, cache_router_client):
        # Enable then disable round-trip.
        await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={"enabled": True},
        )
        r = await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={"enabled": False},
        )
        assert r.json()["enabled"] is False
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/status"
        )
        assert r.json()["enabled"] is False

    async def test_patch_empty_body_is_a_noop(self, cache_router_client):
        # Future settings fields are deliberately deferred; the
        # endpoint must accept an empty body without crashing so a
        # frontend can poll the canonical shape without sending a
        # mutation every time.
        r = await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={},
        )
        assert r.status_code == 200
        assert r.json()["enabled"] is False  # default unchanged

    async def test_patch_persists_through_settings_json(
        self, cache_router_client,
    ):
        # The setting must round-trip through settings.json, not just
        # in-process memory — that's what the worker reads on every
        # tick.
        from app.config import load_settings
        await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={"enabled": True},
        )
        s = load_settings()
        assert s.get("metadata_cache", {}).get("amazon", {}).get("enabled") is True


# ─── v2.21.0 Phase I — mode + schedule round-trip ──────────────


class TestPhaseIPatchMode:
    async def test_status_exposes_mode_and_schedule_defaults(
        self, cache_router_client,
    ):
        # Fresh-deploy default: enabled=False → mode=disabled.
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/status"
        )
        body = r.json()
        assert body["mode"] == "disabled"
        # Schedule defaults populated even when never set by user.
        assert body["schedule"]["active_hours"] == "10:00-22:00"
        assert body["schedule"]["timezone"] == ""
        # Disabled mode is always inside-window (no scheduling applied).
        assert body["inside_schedule_window"] is True
        assert body["seconds_until_window_open"] == 0.0

    async def test_patch_mode_continuous(self, cache_router_client):
        r = await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={"mode": "continuous"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "continuous"
        assert body["enabled"] is True  # synced for back-compat
        # Read-back via status.
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/status"
        )
        assert r.json()["mode"] == "continuous"

    async def test_patch_mode_scheduled_with_window(self, cache_router_client):
        r = await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={
                "mode": "scheduled",
                "schedule": {
                    "active_hours": "08:00-20:00", "timezone": "America/Detroit",
                },
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "scheduled"
        assert body["enabled"] is True
        assert body["schedule"]["active_hours"] == "08:00-20:00"
        assert body["schedule"]["timezone"] == "America/Detroit"

    async def test_patch_mode_disabled_syncs_enabled_false(
        self, cache_router_client,
    ):
        # Enable first, then flip to mode=disabled. `enabled` must
        # follow so any legacy reader sees the right value.
        await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={"enabled": True},
        )
        r = await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={"mode": "disabled"},
        )
        body = r.json()
        assert body["mode"] == "disabled"
        assert body["enabled"] is False

    async def test_patch_unknown_mode_400s(self, cache_router_client):
        r = await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={"mode": "go-fast-mode"},
        )
        assert r.status_code == 400

    async def test_patch_invalid_active_hours_400s(self, cache_router_client):
        r = await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={
                "mode": "scheduled",
                "schedule": {"active_hours": "garbage", "timezone": ""},
            },
        )
        assert r.status_code == 400

    async def test_legacy_enabled_patch_derives_mode(
        self, cache_router_client,
    ):
        # Frontend pre-dating Phase I can still PATCH enabled=true and
        # see the mode field get derived to continuous.
        r = await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={"enabled": True},
        )
        body = r.json()
        assert body["enabled"] is True
        assert body["mode"] == "continuous"
        r2 = await cache_router_client.patch(
            "/api/v1/metadata-cache/amazon/settings",
            json={"enabled": False},
        )
        assert r2.json()["mode"] == "disabled"


# ─── POST /reset-cooldown ──────────────────────────────────────


class TestResetCooldown:
    async def test_reset_cooldown_clears_active_block(
        self, cache_router_client,
    ):
        from app.discovery import amazon_author_id_resolver as r
        r.record_amazon_soft_block("test", retry_after_s=600)
        assert r.is_amazon_blocked()

        resp = await cache_router_client.post(
            "/api/v1/metadata-cache/amazon/reset-cooldown"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["previously_blocked"] is True
        assert 500 < body["previous_remaining_s"] <= 600
        # Cooldown now clear.
        assert not r.is_amazon_blocked()

    async def test_reset_cooldown_idempotent_when_already_clear(
        self, cache_router_client,
    ):
        resp = await cache_router_client.post(
            "/api/v1/metadata-cache/amazon/reset-cooldown"
        )
        body = resp.json()
        assert body["ok"] is True
        assert body["previously_blocked"] is False
        assert body["previous_remaining_s"] == 0

    async def test_reset_cooldown_404_on_unknown_source(
        self, cache_router_client,
    ):
        r = await cache_router_client.post(
            "/api/v1/metadata-cache/unknown/reset-cooldown"
        )
        assert r.status_code == 404


# ─── GET /author/{id} ──────────────────────────────────────────


class TestGetAuthorCacheState:
    """v2.21.0 Phase F — per-author endpoint that the author detail
    page's cache badge consumes. Returns one row per library the
    author has been seen in, with state + queue info."""

    async def test_returns_empty_libraries_for_unknown_author(
        self, cache_router_client,
    ):
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/author/B0NEVERSEEN"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["amazon_author_id"] == "B0NEVERSEEN"
        assert body["libraries"] == []

    async def test_returns_state_and_queue_for_cached_author(
        self, cache_router_client,
    ):
        now = time.time()
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"INSERT INTO {metadata_cache.state_table()} "
                f"(author_id, library_slug, last_scanned_at, "
                f" last_outcome, book_count) VALUES (?, ?, ?, ?, ?)",
                ("B0TESTAUTH", "calibre-library", now, "ok", 5),
            )
            # Schema-v2: queue PK is author_id only.
            await db.execute(
                f"INSERT INTO {metadata_cache.queue_table()} "
                f"(author_id, priority, status, next_scan_due_at) "
                f"VALUES (?, ?, ?, ?)",
                ("B0TESTAUTH", 100.0, "pending", 0.0),
            )
            await db.commit()
        finally:
            await db.close()

        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/author/B0TESTAUTH"
        )
        body = r.json()
        assert len(body["libraries"]) == 1
        row = body["libraries"][0]
        assert row["library_slug"] == "calibre-library"
        assert row["state"]["last_outcome"] == "ok"
        assert row["state"]["book_count"] == 5
        # Singleton queue row attached to each library entry.
        assert row["queue"]["status"] == "pending"
        assert row["queue"]["priority"] == 100.0

    async def test_returns_queue_only_when_not_yet_scanned(
        self, cache_router_client, monkeypatch, tmp_path,
    ):
        """An author backfilled into the queue but never scanned has
        a queue row but no state row. The endpoint synthesizes per-
        library entries by reading the discovery DBs so the frontend
        can still render 'in queue' lines per library."""
        # v2: state rows are empty, so the router falls back to
        # `_libraries_for_author` (which reads the discovery DB
        # `authors` table). Set up a single library with the author
        # row so synthesis returns one entry.
        from app import state
        from app.discovery import database as disco_db
        monkeypatch.setattr(
            state, "_discovered_libraries",
            [{"slug": "calibre-library", "name": "Calibre",
              "content_type": "ebook",
              "source_db_path": "/x", "library_path": "/x"}],
        )
        await disco_db.init_db("calibre-library")
        disc = await disco_db.get_db(slug="calibre-library")
        try:
            await disc.execute(
                "INSERT INTO authors "
                "(name, sort_name, normalized_name, amazon_id) "
                "VALUES (?, ?, ?, ?)",
                ("Test", "Test", "test", "B0NOTSCANNED"),
            )
            await disc.commit()
        finally:
            await disc.close()

        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"INSERT INTO {metadata_cache.queue_table()} "
                f"(author_id, priority, status, next_scan_due_at) "
                f"VALUES (?, ?, ?, ?)",
                ("B0NOTSCANNED", 100.0, "pending", 0.0),
            )
            await db.commit()
        finally:
            await db.close()
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/author/B0NOTSCANNED"
        )
        body = r.json()
        assert len(body["libraries"]) == 1
        assert body["libraries"][0]["library_slug"] == "calibre-library"
        assert body["libraries"][0]["state"] is None
        assert body["libraries"][0]["queue"]["status"] == "pending"

    async def test_returns_multiple_libraries(
        self, cache_router_client,
    ):
        now = time.time()
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            for slug in ("calibre-library", "abs-audio-library"):
                await db.execute(
                    f"INSERT INTO {metadata_cache.state_table()} "
                    f"(author_id, library_slug, last_scanned_at, "
                    f" last_outcome, book_count) VALUES (?, ?, ?, ?, ?)",
                    ("B0BOTHLIBS", slug, now, "ok", 3),
                )
            await db.commit()
        finally:
            await db.close()
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/author/B0BOTHLIBS"
        )
        body = r.json()
        slugs = sorted(row["library_slug"] for row in body["libraries"])
        assert slugs == ["abs-audio-library", "calibre-library"]

    async def test_404_on_unknown_source(self, cache_router_client):
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/hardcover/author/B0WHATEVER"
        )
        assert r.status_code == 404


# ─── GET /recent-discoveries ───────────────────────────────────


async def _seed_book_at(
    *,
    author_id: str,
    library_slug: str,
    book_asin: str,
    title: str,
    cached_at: float,
    series_name: str | None = None,
    series_pos: float | None = None,
) -> None:
    db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
    try:
        # state row required by the FK before we can insert the book
        await db.execute(
            f"INSERT OR IGNORE INTO {metadata_cache.state_table()} "
            f"(author_id, library_slug, last_scanned_at, last_outcome, "
            f" book_count) VALUES (?, ?, ?, ?, ?)",
            (author_id, library_slug, cached_at, "ok", 1),
        )
        await db.execute(
            f"INSERT INTO {metadata_cache.books_table()} "
            f"(author_id, library_slug, book_asin, title, "
            f" series_name, series_pos, cached_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                author_id, library_slug, book_asin, title,
                series_name, series_pos, cached_at,
            ),
        )
        await db.commit()
    finally:
        await db.close()


class TestRecentDiscoveries:
    async def test_returns_newest_first_within_window(
        self, cache_router_client,
    ):
        now = time.time()
        await _seed_book_at(
            author_id="B0A0", library_slug="books-lib",
            book_asin="B0BK0001", title="Old Book",
            cached_at=now - 3600,
        )
        await _seed_book_at(
            author_id="B0A0", library_slug="books-lib",
            book_asin="B0BK0002", title="Newer Book",
            cached_at=now - 60,
        )
        await _seed_book_at(
            author_id="B0A1", library_slug="books-lib",
            book_asin="B0BK0003", title="Newest Book",
            cached_at=now - 10,
        )
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/recent-discoveries"
        )
        body = r.json()
        assert body["source"] == "amazon"
        assert body["window_hours"] == 24
        titles = [d["title"] for d in body["discoveries"]]
        assert titles == ["Newest Book", "Newer Book", "Old Book"]
        # seconds_ago is precomputed server-side; check rough ranges.
        secs = [d["seconds_ago"] for d in body["discoveries"]]
        assert 0 <= secs[0] < 20
        assert 50 < secs[1] < 100
        assert 3500 < secs[2] < 3700

    async def test_window_hours_filter(self, cache_router_client):
        now = time.time()
        await _seed_book_at(
            author_id="B0A0", library_slug="books-lib",
            book_asin="B0BK0001", title="Within Window",
            cached_at=now - 1800,  # 30min ago — within 1h window
        )
        await _seed_book_at(
            author_id="B0A0", library_slug="books-lib",
            book_asin="B0BK0002", title="Outside Window",
            cached_at=now - 7200,  # 2h ago — outside 1h window
        )
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/recent-discoveries?hours=1"
        )
        titles = [d["title"] for d in r.json()["discoveries"]]
        assert titles == ["Within Window"]

    async def test_limit_caps_results(self, cache_router_client):
        now = time.time()
        for i in range(15):
            await _seed_book_at(
                author_id="B0A0", library_slug="books-lib",
                book_asin=f"B0BK{i:04d}", title=f"Book {i}",
                cached_at=now - i * 10,
            )
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/recent-discoveries?limit=5"
        )
        body = r.json()
        assert len(body["discoveries"]) == 5

    async def test_empty_returns_empty_list(self, cache_router_client):
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/recent-discoveries"
        )
        body = r.json()
        assert body["discoveries"] == []
        assert body["source"] == "amazon"

    async def test_series_fields_round_trip(self, cache_router_client):
        now = time.time()
        await _seed_book_at(
            author_id="B0A0", library_slug="books-lib",
            book_asin="B0BK0001", title="Mistborn 1",
            series_name="Mistborn", series_pos=1.0,
            cached_at=now,
        )
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/recent-discoveries"
        )
        row = r.json()["discoveries"][0]
        assert row["series_name"] == "Mistborn"
        assert row["series_pos"] == 1.0

    async def test_404_on_unknown_source(self, cache_router_client):
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/hardcover/recent-discoveries"
        )
        assert r.status_code == 404


# ─── GET /goodreads/author/{id} — v3.6.0 frontend parity ───────


@pytest.fixture
async def gr_cache_router_client(tmp_path, monkeypatch):
    """Same shape as `cache_router_client` but additionally inits the
    GR cache DB so the v3.6.0 /goodreads/author endpoint tests have
    its tables available."""
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(metadata_cache, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({**config.DEFAULT_SETTINGS}))
    monkeypatch.setattr(app_config, "SETTINGS_PATH", settings_path)
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = object()

    await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)

    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as ac:
        yield ac

    app_config._settings_cache["data"] = None


class TestGetGoodreadsAuthorCacheState:
    """v3.6.0 ADR-0018-aware projection: GR caches list pages, not
    per-book detail, so the `/goodreads/author/{aid}` response
    populates `list_pages` per library instead of (well, alongside)
    the shared `state` + `queue` shape."""

    async def test_returns_empty_libraries_for_unknown_author(
        self, gr_cache_router_client,
    ):
        r = await gr_cache_router_client.get(
            "/api/v1/metadata-cache/goodreads/author/9999.NeverSeen"
        )
        assert r.status_code == 200
        body = r.json()
        # GR responses use `author_id`; `amazon_author_id` is empty.
        assert body["author_id"] == "9999.NeverSeen"
        assert body["amazon_author_id"] == ""
        assert body["libraries"] == []
        # GR has no IP-level cooldown surface.
        assert body["cooldown"]["blocked"] is False

    async def test_returns_state_and_list_pages_for_cached_author(
        self, gr_cache_router_client,
    ):
        now = time.time()
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            state_t = metadata_cache.state_table(
                metadata_cache.SOURCE_GOODREADS,
            )
            await db.execute(
                f"INSERT INTO {state_t} "
                f"(author_id, library_slug, last_scanned_at, "
                f" last_outcome, book_count) VALUES (?, ?, ?, ?, ?)",
                ("38550.Sanderson", "cwa-library", now, "ok", 399),
            )
            lp_t = metadata_cache.list_pages_table(
                metadata_cache.SOURCE_GOODREADS,
            )
            # Three pages, varying book counts. fetched_at slightly
            # earlier than now so the seconds_ago math is sensible.
            await db.execute(
                f"INSERT INTO {lp_t} "
                f"(author_id, library_slug, page_num, fetched_at, "
                f" book_ids_json) VALUES (?, ?, ?, ?, ?)",
                ("38550.Sanderson", "cwa-library", 1, now - 60.0,
                 json.dumps(["b1", "b2", "b3"])),
            )
            await db.execute(
                f"INSERT INTO {lp_t} "
                f"(author_id, library_slug, page_num, fetched_at, "
                f" book_ids_json) VALUES (?, ?, ?, ?, ?)",
                ("38550.Sanderson", "cwa-library", 2, now - 60.0,
                 json.dumps(["b4", "b5"])),
            )
            await db.commit()
        finally:
            await db.close()

        r = await gr_cache_router_client.get(
            "/api/v1/metadata-cache/goodreads/author/38550.Sanderson"
        )
        body = r.json()
        assert body["author_id"] == "38550.Sanderson"
        assert len(body["libraries"]) == 1
        row = body["libraries"][0]
        assert row["library_slug"] == "cwa-library"
        assert row["state"]["last_outcome"] == "ok"
        assert row["state"]["book_count"] == 399
        # List-page projection: 2 pages, book counts derived from the
        # cached `book_ids_json` array lengths.
        assert row["list_pages"] is not None
        assert len(row["list_pages"]) == 2
        assert row["list_pages"][0]["page_num"] == 1
        assert row["list_pages"][0]["book_count"] == 3
        assert row["list_pages"][1]["page_num"] == 2
        assert row["list_pages"][1]["book_count"] == 2

    async def test_amazon_response_unchanged_carries_no_list_pages(
        self, cache_router_client,
    ):
        """Amazon-side regression — adding `list_pages` to the model
        as Optional must default to None for Amazon responses (Path
        B caching is GR-only). Confirms the v2.21.0 frontend badge
        stays unaffected by the v3.6.0 model extension."""
        now = time.time()
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"INSERT INTO {metadata_cache.state_table()} "
                f"(author_id, library_slug, last_scanned_at, "
                f" last_outcome, book_count) VALUES (?, ?, ?, ?, ?)",
                ("B0TESTAUTH2", "calibre-library", now, "ok", 5),
            )
            await db.commit()
        finally:
            await db.close()
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/author/B0TESTAUTH2"
        )
        body = r.json()
        row = body["libraries"][0]
        assert row["list_pages"] is None
        # Backwards-compat: Amazon still populates amazon_author_id.
        assert body["amazon_author_id"] == "B0TESTAUTH2"
        # And the new generic field too.
        assert body["author_id"] == "B0TESTAUTH2"

    async def test_returns_multiple_libraries_with_per_library_pages(
        self, gr_cache_router_client,
    ):
        """List pages partition by library_slug — Sanderson fanned
        out across cwa + abs should return both libraries with
        independent per-library list_pages projections."""
        now = time.time()
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            state_t = metadata_cache.state_table(
                metadata_cache.SOURCE_GOODREADS,
            )
            lp_t = metadata_cache.list_pages_table(
                metadata_cache.SOURCE_GOODREADS,
            )
            for slug in ("cwa-library", "abs-audiobooks"):
                await db.execute(
                    f"INSERT INTO {state_t} "
                    f"(author_id, library_slug, last_scanned_at, "
                    f" last_outcome, book_count) VALUES (?, ?, ?, ?, ?)",
                    ("38550.Sanderson", slug, now, "ok", 399),
                )
                await db.execute(
                    f"INSERT INTO {lp_t} "
                    f"(author_id, library_slug, page_num, fetched_at, "
                    f" book_ids_json) VALUES (?, ?, ?, ?, ?)",
                    ("38550.Sanderson", slug, 1, now - 30.0,
                     json.dumps(["a", "b"])),
                )
            await db.commit()
        finally:
            await db.close()
        r = await gr_cache_router_client.get(
            "/api/v1/metadata-cache/goodreads/author/38550.Sanderson"
        )
        body = r.json()
        slugs = sorted(row["library_slug"] for row in body["libraries"])
        assert slugs == ["abs-audiobooks", "cwa-library"]
        for row in body["libraries"]:
            assert row["list_pages"] is not None
            assert len(row["list_pages"]) == 1
            assert row["list_pages"][0]["book_count"] == 2
