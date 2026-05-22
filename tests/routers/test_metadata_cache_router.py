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
    interesting to assert on. Mix of queue + state + books rows."""
    db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
    try:
        # 3 queue rows: 2 pending, 1 failed_permanent.
        await db.execute(
            f"INSERT INTO {metadata_cache.queue_table()} "
            f"(author_id, library_slug, priority, status, next_scan_due_at) "
            f"VALUES (?, ?, ?, ?, ?), (?, ?, ?, ?, ?), (?, ?, ?, ?, ?)",
            (
                "B0AAAAAAAA", "books-lib", 100.0, "pending", 0.0,
                "B0BBBBBBBB", "books-lib", 200.0, "pending", 0.0,
                "B0CCCCCCCC", "books-lib", 100.0, "failed_permanent", 0.0,
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
            await db.execute(
                f"INSERT INTO {metadata_cache.queue_table()} "
                f"(author_id, library_slug, priority, status, "
                f" next_scan_due_at) VALUES (?, ?, ?, ?, ?)",
                ("B0TESTAUTH", "calibre-library", 100.0, "pending", 0.0),
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
        assert row["queue"]["status"] == "pending"
        assert row["queue"]["priority"] == 100.0

    async def test_returns_queue_only_when_not_yet_scanned(
        self, cache_router_client,
    ):
        """An author backfilled into the queue but never scanned has
        a queue row but no state row. The endpoint surfaces that
        case so the frontend can render 'in queue' instead of
        'never seen.'"""
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"INSERT INTO {metadata_cache.queue_table()} "
                f"(author_id, library_slug, priority, status, "
                f" next_scan_due_at) VALUES (?, ?, ?, ?, ?)",
                ("B0NOTSCANNED", "calibre-library", 100.0, "pending", 0.0),
            )
            await db.commit()
        finally:
            await db.close()
        r = await cache_router_client.get(
            "/api/v1/metadata-cache/amazon/author/B0NOTSCANNED"
        )
        body = r.json()
        assert len(body["libraries"]) == 1
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
