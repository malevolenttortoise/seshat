"""
Tests for the weekly Goodreads session canary registered by
`app.orchestrator.scheduler.register_goodreads_canary`.

Covers:
  - The canary job is registered with the expected schedule
    (Monday 03:00).
  - A 200 response leaves the session marked active and does NOT
    emit a ntfy notification.
  - A 202 response (soft-block) flips state and DOES emit a ntfy
    notification when notify_on_goodreads_canary_failed is True.
  - The ntfy notification is suppressed when the gate is off.
  - Notification is also skipped when ntfy_url / ntfy_topic are empty.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import config
from app.orchestrator.scheduler import register_goodreads_canary


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Per-test settings.json so canary writes don't leak."""
    p = tmp_path / "settings.json"
    seed = {
        **config.DEFAULT_SETTINGS,
        "ntfy_url": "https://ntfy.test",
        "ntfy_topic": "seshat-test",
        "per_event_notifications": True,
        "notify_on_goodreads_canary_failed": True,
    }
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
    """Build a real GoodreadsSession with the curl path disabled and
    the httpx fallback stubbed. Same pattern as the router tests."""
    from app.metadata import goodreads_session as gr

    def factory(responses: list[tuple[int, bytes]]):
        session = gr.GoodreadsSession(rate_limit=0)
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

        async def _get_session(rate_limit=None):
            return session

        monkeypatch.setattr(gr, "get_session", _get_session)
        session.calls = fake_client.calls  # type: ignore[attr-defined]
        return session

    return factory


@pytest.fixture
def captured_ntfy(monkeypatch):
    """Capture ntfy.send() calls so tests can assert on them without
    making real HTTP."""
    captured: list[dict] = []
    from app.notify import ntfy

    async def fake_send(*, url, topic, title, message, priority=3, tags=None):
        captured.append({
            "url": url, "topic": topic, "title": title,
            "message": message, "priority": priority, "tags": tags or [],
        })
        return True

    monkeypatch.setattr(ntfy, "send", fake_send)
    return captured


class TestCanaryRegistration:
    def test_canary_registered_with_monday_3am_trigger(self):
        scheduler = AsyncIOScheduler()
        register_goodreads_canary(scheduler)
        job = scheduler.get_job("goodreads_canary")
        assert job is not None
        # CronTrigger fields are exposed via .trigger.fields
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields.get("day_of_week") == "mon"
        assert fields.get("hour") == "3"
        assert fields.get("minute") == "0"


class TestCanary200:
    async def test_200_marks_active_no_ntfy(
        self, isolated_settings, stub_session, captured_ntfy,
    ):
        """Healthy 200 with body: session marked active, no ntfy
        notification fires."""
        stub_session([(200, b"<html>" + b"x" * 4096 + b"</html>")])

        scheduler = AsyncIOScheduler()
        register_goodreads_canary(scheduler)
        canary = scheduler.get_job("goodreads_canary").func
        await canary()

        from app.metadata import goodreads_session as gr
        assert gr.get_session_state()["state"] == "active"
        assert captured_ntfy == []


class TestCanary202:
    async def test_202_flips_state_and_sends_ntfy(
        self, isolated_settings, stub_session, captured_ntfy,
    ):
        stub_session([(202, b"")])

        scheduler = AsyncIOScheduler()
        register_goodreads_canary(scheduler)
        canary = scheduler.get_job("goodreads_canary").func
        await canary()

        from app.metadata import goodreads_session as gr
        assert gr.get_session_state()["state"] == "soft_blocked"
        assert len(captured_ntfy) == 1
        assert "Goodreads" in captured_ntfy[0]["title"]
        assert captured_ntfy[0]["topic"] == "seshat-test"

    async def test_ntfy_suppressed_when_per_event_gate_off(
        self, isolated_settings, stub_session, captured_ntfy, monkeypatch,
    ):
        """User toggled `notify_on_goodreads_canary_failed` off — even
        when the canary detects 202, no ntfy fires."""
        stub_session([(202, b"")])
        # Disable the per-event gate.
        s = dict(config.load_settings())
        s["notify_on_goodreads_canary_failed"] = False
        config.save_settings(s)

        scheduler = AsyncIOScheduler()
        register_goodreads_canary(scheduler)
        canary = scheduler.get_job("goodreads_canary").func
        await canary()

        from app.metadata import goodreads_session as gr
        # State still flipped (session module always writes the flag).
        assert gr.get_session_state()["state"] == "soft_blocked"
        # But no notification.
        assert captured_ntfy == []

    async def test_ntfy_suppressed_when_ntfy_unconfigured(
        self, isolated_settings, stub_session, captured_ntfy,
    ):
        """No ntfy_url / ntfy_topic configured: canary detects 202 but
        skips the notification (logs only)."""
        stub_session([(202, b"")])
        s = dict(config.load_settings())
        s["ntfy_url"] = ""
        s["ntfy_topic"] = ""
        config.save_settings(s)

        scheduler = AsyncIOScheduler()
        register_goodreads_canary(scheduler)
        canary = scheduler.get_job("goodreads_canary").func
        await canary()

        assert captured_ntfy == []
