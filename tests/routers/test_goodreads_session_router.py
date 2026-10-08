"""
HTTP-level tests for `app.routers.goodreads_session`.

    GET  /api/v1/metadata/goodreads/state
    POST /api/v1/metadata/goodreads/test          (mode=single|burst)
    POST /api/v1/metadata/goodreads/mark-active

Covers:
  - GET reports the current runtime-state shape
  - Single probe makes ONE request and reports the result
  - Burst probe runs N requests, aggregates correctly
  - Burst probe with custom book_ids overrides the default pool
  - mark-active flips the runtime state and returns the new state
  - Soft-block during probe is correctly surfaced in the response

All tests stub the GoodreadsSession via monkeypatch so no real HTTP
fires (and so the probe doesn't try to wait 10*5s for a real burst).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app import config
from app.routers.goodreads_session import router as gr_router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(gr_router)
    return app


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point config at a tmp_path settings.json so probe state-flag
    writes don't leak to the dev DATA_DIR."""
    p = tmp_path / "settings.json"
    seed = {**config.DEFAULT_SETTINGS}
    p.write_text(json.dumps(seed))
    monkeypatch.setattr(config, "SETTINGS_PATH", p)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = object()
    yield p
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = object()


@pytest.fixture
def stub_session(monkeypatch):
    """Build a real GoodreadsSession (zero rate-limit) whose httpx
    fallback transport is stubbed. Using the real session class keeps
    the state-flag side-effects in `get()` honest — only the actual
    HTTP call is faked."""
    from app.metadata import goodreads_session as gr

    def factory(responses: list[tuple[int, bytes]]):
        session = gr.GoodreadsSession()
        monkeypatch.setattr(session, "_get_curl", lambda: None)

        class FakeClient:
            def __init__(self):
                self._responses = list(responses)
                self.calls: list[str] = []

            async def get(self, url, **kwargs):
                self.calls.append(url)
                if not self._responses:
                    return SimpleNamespace(status_code=200, content=b"<html>default</html>")
                status, body = self._responses.pop(0)
                return SimpleNamespace(status_code=status, content=body)

        fake_client = FakeClient()
        monkeypatch.setattr(session, "_get_httpx", lambda: fake_client)

        # Bridge: the router calls gr.get_session() — return our stubbed instance.
        async def _get_session(rate_limit=None):
            return session

        monkeypatch.setattr(gr, "get_session", _get_session)
        # Expose the call log on the session for assertion convenience.
        session.calls = fake_client.calls  # type: ignore[attr-defined]
        return session

    return factory


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


class TestGetState:
    async def test_default_state_is_unknown(self, isolated_settings):
        app = _make_app()
        async with await _client(app) as c:
            resp = await c.get("/api/v1/metadata/goodreads/state")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "unknown"
        assert body["since"] is None
        assert body["last_status"] is None

    async def test_state_reflects_mark_soft_blocked(self, isolated_settings):
        from app.metadata import goodreads_session as gr
        gr.mark_soft_blocked(last_status=202)
        app = _make_app()
        async with await _client(app) as c:
            resp = await c.get("/api/v1/metadata/goodreads/state")
        body = resp.json()
        assert body["state"] == "soft_blocked"
        assert body["last_status"] == 202


class TestSingleProbe:
    async def test_single_probe_returns_one_result(
        self, isolated_settings, stub_session,
    ):
        stub_session([(200, b"<html>" + b"x" * 4096 + b"</html>")])
        app = _make_app()
        async with await _client(app) as c:
            resp = await c.post(
                "/api/v1/metadata/goodreads/test",
                json={"mode": "single"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "single"
        assert body["single"] is not None
        assert body["single"]["status"] == 200
        assert body["single"]["body_size_kb"] > 0
        assert body["single"]["soft_blocked"] is False
        assert body["state_after"]["state"] == "active"

    async def test_single_probe_soft_block_reported(
        self, isolated_settings, stub_session,
    ):
        stub_session([(202, b"")])
        app = _make_app()
        async with await _client(app) as c:
            resp = await c.post(
                "/api/v1/metadata/goodreads/test",
                json={"mode": "single"},
            )
        body = resp.json()
        assert body["single"]["status"] == 202
        assert body["single"]["soft_blocked"] is True
        assert body["state_after"]["state"] == "soft_blocked"
        assert body["state_after"]["last_status"] == 202

    async def test_single_probe_custom_book_id(
        self, isolated_settings, stub_session,
    ):
        stub = stub_session([(200, b"<html>ok</html>")])
        app = _make_app()
        async with await _client(app) as c:
            await c.post(
                "/api/v1/metadata/goodreads/test",
                json={"mode": "single", "book_ids": ["12345"]},
            )
        # Custom book_id was the URL fetched.
        assert any("12345" in u for u in stub.calls)


class TestBurstProbe:
    async def test_burst_runs_all_default_pool_books(
        self, isolated_settings, stub_session,
    ):
        # 10 responses to match the default pool size.
        stub = stub_session([(200, b"<html>" + b"x" * 2048 + b"</html>")] * 10)
        app = _make_app()
        async with await _client(app) as c:
            resp = await c.post(
                "/api/v1/metadata/goodreads/test",
                json={"mode": "burst"},
            )
        body = resp.json()
        assert body["mode"] == "burst"
        assert body["burst"] is not None
        assert body["burst"]["requests"] == 10
        assert body["burst"]["soft_blocks"] == 0
        assert body["burst"]["status_distribution"] == {"200": 10}
        assert len(stub.calls) == 10
        # State ended active (last response was 200).
        assert body["state_after"]["state"] == "active"

    async def test_burst_partial_soft_blocks_counted(
        self, isolated_settings, stub_session,
    ):
        # 7 good + 3 soft-blocks intermixed.
        responses = [
            (200, b"<html>ok</html>"),
            (202, b""),
            (200, b"<html>ok</html>"),
            (200, b"<html>ok</html>"),
            (202, b""),
            (200, b"<html>ok</html>"),
            (200, b"<html>ok</html>"),
            (202, b""),
            (200, b"<html>ok</html>"),
            (200, b"<html>ok</html>"),
        ]
        stub_session(responses)
        app = _make_app()
        async with await _client(app) as c:
            resp = await c.post(
                "/api/v1/metadata/goodreads/test",
                json={"mode": "burst"},
            )
        body = resp.json()
        assert body["burst"]["requests"] == 10
        assert body["burst"]["soft_blocks"] == 3
        # status_distribution keys arrive as strings via JSON.
        assert body["burst"]["status_distribution"]["200"] == 7
        assert body["burst"]["status_distribution"]["202"] == 3

    async def test_burst_custom_book_ids(
        self, isolated_settings, stub_session,
    ):
        stub = stub_session([(200, b"<html>ok</html>")] * 3)
        app = _make_app()
        async with await _client(app) as c:
            resp = await c.post(
                "/api/v1/metadata/goodreads/test",
                json={"mode": "burst", "book_ids": ["1", "2", "3"]},
            )
        body = resp.json()
        assert body["burst"]["requests"] == 3
        # All three custom IDs got hit.
        assert any("/1" in u for u in stub.calls)
        assert any("/2" in u for u in stub.calls)
        assert any("/3" in u for u in stub.calls)


class TestMarkActive:
    async def test_mark_active_clears_soft_blocked(
        self, isolated_settings,
    ):
        from app.metadata import goodreads_session as gr
        gr.mark_soft_blocked(last_status=202)
        assert gr.is_soft_blocked()
        app = _make_app()
        async with await _client(app) as c:
            resp = await c.post(
                "/api/v1/metadata/goodreads/mark-active",
            )
        body = resp.json()
        assert body["ok"] is True
        assert body["state_after"]["state"] == "active"
        assert not gr.is_soft_blocked()
