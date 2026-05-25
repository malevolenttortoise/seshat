"""HTTP-level tests for the notifications router (Bundle B.2)."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.notifications import events
from app.routers.notifications import router as notifications_router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(notifications_router)
    return app


@pytest.fixture
async def client():
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as c:
        yield c


class TestEventCatalogue:
    async def test_returns_all_registered_events(self, client):
        resp = await client.get("/api/v1/notifications/events")
        assert resp.status_code == 200
        body = resp.json()
        names = {e["name"] for e in body["events"]}
        assert names == set(events.all_event_names())

    async def test_shape_matches_meta(self, client):
        resp = await client.get("/api/v1/notifications/events")
        by_name = {e["name"]: e for e in resp.json()["events"]}
        grab = by_name[events.GRAB_SUCCESS]
        assert grab["default_priority"] == 3
        assert grab["default_tags"] == ["books"]
        assert grab["suppressible_during_quiet_hours"] is True
        assert grab["legacy_setting_key"] == "notify_on_grab"
        assert grab["legacy_requires_master"] is True
        assert grab["legacy_default_enabled"] is True

    async def test_error_events_marked_non_suppressible(self, client):
        resp = await client.get("/api/v1/notifications/events")
        by_name = {e["name"]: e for e in resp.json()["events"]}
        for name in (
            events.PIPELINE_ERROR,
            events.GRAB_BUFFER_BLOCKED,
            events.SOURCE_GOODREADS_CANARY_FAILED,
            events.SOURCE_METADATA_CACHE_ERROR,
        ):
            assert by_name[name]["suppressible_during_quiet_hours"] is False

    async def test_declaration_order_preserved(self, client):
        """The UI uses the response order verbatim — verify it matches
        the registry's declaration order so events stay grouped by
        family (grab.*, pipeline.*, discovery.*, …)."""
        resp = await client.get("/api/v1/notifications/events")
        returned = [e["name"] for e in resp.json()["events"]]
        assert returned == events.all_event_names()
