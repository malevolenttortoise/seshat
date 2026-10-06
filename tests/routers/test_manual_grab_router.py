"""HTTP surface of Manual Grab: the batch cap and in-batch duplicates are
enforced by the server, not just the page (ADR-0023)."""
from __future__ import annotations

import httpx
from fastapi import FastAPI

from app import state
from app.orchestrator import manual_grab
from app.routers.manual_grab import router
from tests.orchestrator.test_dispatch import _make_deps


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _links(n: int) -> list[dict]:
    return [{"kind": "link", "value": str(1000 + i)} for i in range(n)]


async def test_more_than_30_is_refused_and_nothing_starts(temp_db, monkeypatch):
    started: list = []
    monkeypatch.setattr(manual_grab, "start_job", lambda deps, items: started.append(items))
    state.dispatcher = _make_deps()
    try:
        async with _client() as c:
            resp = await c.post("/api/v1/manual-grab/grab", json={"items": _links(31)})
    finally:
        state.dispatcher = None
    assert resp.status_code == 422
    assert started == []


async def test_duplicate_torrent_in_batch_is_refused(temp_db, monkeypatch):
    started: list = []
    monkeypatch.setattr(manual_grab, "start_job", lambda deps, items: started.append(items))
    state.dispatcher = _make_deps()
    try:
        async with _client() as c:
            resp = await c.post("/api/v1/manual-grab/grab", json={"items": [
                {"kind": "link", "value": "1274788"},
                {"kind": "link", "value": "https://www.myanonamouse.net/t/1274788"},
            ]})
    finally:
        state.dispatcher = None
    assert resp.status_code == 422
    assert "rows 2" in resp.json()["detail"]
    assert started == []


async def test_30_starts_a_job(temp_db, monkeypatch):
    class _Job:
        def to_dict(self):
            return {"job_id": "x", "done": False, "rows": []}

    started: list = []

    def fake_start(deps, items):
        started.append(items)
        return _Job()

    monkeypatch.setattr(manual_grab, "start_job", fake_start)
    state.dispatcher = _make_deps()
    try:
        async with _client() as c:
            resp = await c.post("/api/v1/manual-grab/grab", json={"items": _links(30)})
    finally:
        state.dispatcher = None
    assert resp.status_code == 200
    assert len(started[0]) == 30


async def test_unknown_job_is_404_with_a_restart_hint(temp_db):
    async with _client() as c:
        resp = await c.get("/api/v1/manual-grab/grab/nope")
    assert resp.status_code == 404
    assert "restarted" in resp.json()["detail"]


async def test_cover_rejects_non_numeric_ids(temp_db):
    async with _client() as c:
        assert (await c.get("/api/v1/manual-grab/cover/..%2Fetc")).status_code in (400, 404)
        assert (await c.get("/api/v1/manual-grab/cover/abc")).status_code == 400


async def test_preview_without_dispatcher_is_503(temp_db):
    state.dispatcher = None
    async with _client() as c:
        resp = await c.post("/api/v1/manual-grab/preview", json={"kind": "link", "value": "1"})
    assert resp.status_code == 503
