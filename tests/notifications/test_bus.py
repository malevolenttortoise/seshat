"""Unit tests for the notification event bus.

The bus delegates to ``app.notify.ntfy.send()`` for the actual HTTP
call; tests stub the ntfy client with ``httpx.MockTransport`` so no
real ntfy server is contacted.
"""
from __future__ import annotations

import httpx
import pytest

from app import config
from app.notifications import bus, events
from app.notify import ntfy


@pytest.fixture
def seed_settings(tmp_path, monkeypatch):
    """Return a helper that writes a settings.json payload."""
    def _seed(payload: str) -> None:
        p = tmp_path / "settings.json"
        p.write_text(payload)
        monkeypatch.setattr(config, "SETTINGS_PATH", p)
        config._settings_cache["data"] = None
        config._settings_cache["mtime"] = object()
    return _seed


@pytest.fixture
async def mock_ntfy_client():
    captured = {"requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["requests"].append(request)
        return httpx.Response(200, text='{"id":"test"}')

    original = ntfy._client
    ntfy._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        yield captured
    finally:
        await ntfy._client.aclose()
        ntfy._client = original


# ─── is_enabled — legacy fallback ────────────────────────────


class TestIsEnabledLegacyOrchestrator:
    """Orchestrator events (legacy_requires_master=True) honour the
    pre-v2.28.0 master + per-event toggle pair."""

    def test_master_off_returns_false(self, seed_settings):
        seed_settings(
            '{"per_event_notifications": false, "notify_on_grab": true}'
        )
        assert bus.is_enabled(events.GRAB_SUCCESS) is False

    def test_master_on_subtoggle_off(self, seed_settings):
        seed_settings(
            '{"per_event_notifications": true, "notify_on_grab": false}'
        )
        assert bus.is_enabled(events.GRAB_SUCCESS) is False

    def test_master_on_subtoggle_on(self, seed_settings):
        seed_settings(
            '{"per_event_notifications": true, "notify_on_grab": true}'
        )
        assert bus.is_enabled(events.GRAB_SUCCESS) is True

    def test_master_on_subtoggle_missing_defaults_true(self, seed_settings):
        seed_settings('{"per_event_notifications": true}')
        assert bus.is_enabled(events.GRAB_SUCCESS) is True
        assert bus.is_enabled(events.PIPELINE_DOWNLOAD_COMPLETE) is True
        assert bus.is_enabled(events.PIPELINE_ERROR) is True

    def test_per_event_keys_independent(self, seed_settings):
        seed_settings(
            '{"per_event_notifications": true,'
            ' "notify_on_grab": false,'
            ' "notify_on_download_complete": true,'
            ' "notify_on_pipeline_error": true}'
        )
        assert bus.is_enabled(events.GRAB_SUCCESS) is False
        assert bus.is_enabled(events.PIPELINE_DOWNLOAD_COMPLETE) is True
        assert bus.is_enabled(events.PIPELINE_ERROR) is True


class TestIsEnabledLegacyDiscovery:
    """Discovery + sync + digest events never required the legacy
    master toggle — only their own per-event key."""

    def test_no_master_required(self, seed_settings):
        # per_event_notifications absent — irrelevant for discovery.
        seed_settings('{"ntfy_on_scan_complete": true}')
        assert bus.is_enabled(events.DISCOVERY_SCAN_COMPLETE) is True

    def test_per_event_off(self, seed_settings):
        seed_settings('{"ntfy_on_scan_complete": false}')
        assert bus.is_enabled(events.DISCOVERY_SCAN_COMPLETE) is False

    def test_default_enabled_when_missing(self, seed_settings):
        seed_settings("{}")
        # Default-True events.
        assert bus.is_enabled(events.DISCOVERY_SCAN_COMPLETE) is True
        assert bus.is_enabled(events.DISCOVERY_NEW_BOOKS) is True
        assert bus.is_enabled(events.DIGEST_DAILY_ACCEPTED) is True

    def test_default_disabled_when_missing(self, seed_settings):
        seed_settings("{}")
        # Default-False events (legacy_default_enabled=False).
        assert bus.is_enabled(events.SYNC_LIBRARY) is False
        assert bus.is_enabled(events.SYNC_MAM_COOKIE_ROTATED) is False


# ─── is_enabled — new shape ──────────────────────────────────


class TestIsEnabledNewShape:
    """The new ``notifications.events.<name>.enabled`` key wins over
    the legacy fallback."""

    def test_new_shape_explicit_true_overrides_legacy_false(self, seed_settings):
        # Legacy says disabled but new shape explicitly enables.
        seed_settings(
            '{"per_event_notifications": false,'
            ' "notifications": {"events": {"grab.success": {"enabled": true}}}}'
        )
        assert bus.is_enabled(events.GRAB_SUCCESS) is True

    def test_new_shape_explicit_false_overrides_legacy_true(self, seed_settings):
        seed_settings(
            '{"per_event_notifications": true, "notify_on_grab": true,'
            ' "notifications": {"events": {"grab.success": {"enabled": false}}}}'
        )
        assert bus.is_enabled(events.GRAB_SUCCESS) is False

    def test_new_master_off_suppresses_everything(self, seed_settings):
        seed_settings(
            '{"per_event_notifications": true, "notify_on_grab": true,'
            ' "ntfy_on_scan_complete": true,'
            ' "notifications": {"master_enabled": false,'
            '   "events": {"grab.success": {"enabled": true}}}}'
        )
        assert bus.is_enabled(events.GRAB_SUCCESS) is False
        assert bus.is_enabled(events.DISCOVERY_SCAN_COMPLETE) is False

    def test_new_master_absent_defaults_on(self, seed_settings):
        seed_settings('{"ntfy_on_scan_complete": true, "notifications": {}}')
        assert bus.is_enabled(events.DISCOVERY_SCAN_COMPLETE) is True

    def test_new_shape_partial_dict_falls_back(self, seed_settings):
        # Per-event dict present but no `enabled` key → fall back to legacy.
        seed_settings(
            '{"ntfy_on_scan_complete": false,'
            ' "notifications": {"events": {"discovery.scan_complete": {"topic": "x"}}}}'
        )
        assert bus.is_enabled(events.DISCOVERY_SCAN_COMPLETE) is False


class TestIsEnabledUnknown:
    def test_unknown_event_returns_false(self, seed_settings):
        seed_settings("{}")
        assert bus.is_enabled("not.a.real.event") is False


# ─── emit() ──────────────────────────────────────────────────


class TestEmit:
    async def test_unknown_event_short_circuits(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings('{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t"}')
        result = await bus.emit("not.real", title="T", message="M")
        assert result is False
        assert mock_ntfy_client["requests"] == []

    async def test_disabled_short_circuits(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t",'
            ' "per_event_notifications": false}'
        )
        result = await bus.emit(events.GRAB_SUCCESS, title="T", message="M")
        assert result is False
        assert mock_ntfy_client["requests"] == []

    async def test_uses_registry_defaults(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t",'
            ' "per_event_notifications": true}'
        )
        await bus.emit(events.GRAB_SUCCESS, title="New grab", message="Book")
        req = mock_ntfy_client["requests"][0]
        # grab.success default priority is 3.
        assert req.headers["priority"] == "3"
        # grab.success default tags is ("books",).
        assert req.headers["tags"] == "books"

    async def test_priority_override(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t",'
            ' "per_event_notifications": true}'
        )
        await bus.emit(
            events.GRAB_SUCCESS, title="T", message="M", priority=5,
        )
        req = mock_ntfy_client["requests"][0]
        assert req.headers["priority"] == "5"

    async def test_tags_override(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t",'
            ' "per_event_notifications": true}'
        )
        await bus.emit(
            events.GRAB_SUCCESS, title="T", message="M",
            tags=["custom", "tag"],
        )
        req = mock_ntfy_client["requests"][0]
        assert req.headers["tags"] == "custom,tag"

    async def test_uses_default_topic(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "seshat",'
            ' "per_event_notifications": true}'
        )
        await bus.emit(events.GRAB_SUCCESS, title="T", message="M")
        req = mock_ntfy_client["requests"][0]
        assert str(req.url) == "https://ntfy.example.com/seshat"

    async def test_pipeline_error_higher_priority_default(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t",'
            ' "per_event_notifications": true}'
        )
        await bus.emit(events.PIPELINE_ERROR, title="!", message="boom")
        req = mock_ntfy_client["requests"][0]
        # pipeline.error registry default is priority 4.
        assert req.headers["priority"] == "4"

    async def test_no_ntfy_url_no_send(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "", "ntfy_topic": "t",'
            ' "per_event_notifications": true}'
        )
        result = await bus.emit(events.GRAB_SUCCESS, title="T", message="M")
        assert result is False
        assert mock_ntfy_client["requests"] == []

    async def test_discovery_event_does_not_require_master(
        self, seed_settings, mock_ntfy_client,
    ):
        # per_event_notifications absent — discovery should still send.
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t"}'
        )
        result = await bus.emit(
            events.DISCOVERY_SCAN_COMPLETE,
            title="Scan complete", message="3 new",
        )
        assert result is True
        assert len(mock_ntfy_client["requests"]) == 1


# ─── Phase 2 — routing through emit() ────────────────────────


class TestEmitRouting:
    async def test_exact_routing_override(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "seshat",'
            ' "per_event_notifications": true,'
            ' "notifications": {"events": {"grab.success":'
            '   {"topic": "seshat-grabs"}}}}'
        )
        await bus.emit(events.GRAB_SUCCESS, title="T", message="M")
        req = mock_ntfy_client["requests"][0]
        assert str(req.url) == "https://ntfy.example.com/seshat-grabs"

    async def test_wildcard_routing(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "seshat",'
            ' "per_event_notifications": true,'
            ' "notifications": {"events": {"grab.*":'
            '   {"topic": "seshat-grabs"}}}}'
        )
        await bus.emit(events.GRAB_SUCCESS, title="T", message="M")
        await bus.emit(events.GRAB_BUFFER_BLOCKED, title="T", message="M")
        assert str(mock_ntfy_client["requests"][0].url) == "https://ntfy.example.com/seshat-grabs"
        assert str(mock_ntfy_client["requests"][1].url) == "https://ntfy.example.com/seshat-grabs"

    async def test_universal_routing(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "default",'
            ' "ntfy_on_scan_complete": true,'
            ' "notifications": {"events": {"*": {"topic": "everything"}}}}'
        )
        await bus.emit(events.DISCOVERY_SCAN_COMPLETE, title="T", message="M")
        req = mock_ntfy_client["requests"][0]
        assert str(req.url) == "https://ntfy.example.com/everything"

    async def test_routing_override_beats_url_path(
        self, seed_settings, mock_ntfy_client,
    ):
        """Critical Phase 2 fix: a URL with embedded topic must NOT
        swallow a routing override."""
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com/seshat", "ntfy_topic": "",'
            ' "per_event_notifications": true,'
            ' "notifications": {"events": {"grab.*":'
            '   {"topic": "seshat-grabs"}}}}'
        )
        await bus.emit(events.GRAB_SUCCESS, title="T", message="M")
        req = mock_ntfy_client["requests"][0]
        assert str(req.url) == "https://ntfy.example.com/seshat-grabs"

    async def test_no_override_uses_default_topic(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "seshat",'
            ' "per_event_notifications": true,'
            ' "notifications": {"events": {"discovery.*":'
            '   {"topic": "seshat-scans"}}}}'
        )
        # grab.success is NOT covered by discovery.* — falls back to ntfy_topic.
        await bus.emit(events.GRAB_SUCCESS, title="T", message="M")
        req = mock_ntfy_client["requests"][0]
        assert str(req.url) == "https://ntfy.example.com/seshat"


# ─── Phase 2 — routing applies to enabled too ────────────────


class TestEnabledWildcard:
    def test_wildcard_disable_blocks_event(self, seed_settings):
        seed_settings(
            '{"per_event_notifications": true, "notify_on_grab": true,'
            ' "notifications": {"events": {"grab.*": {"enabled": false}}}}'
        )
        assert bus.is_enabled(events.GRAB_SUCCESS) is False
        assert bus.is_enabled(events.GRAB_BUFFER_BLOCKED) is False

    def test_exact_enable_beats_wildcard_disable(self, seed_settings):
        seed_settings(
            '{"per_event_notifications": false,'
            ' "notifications": {"events": {'
            '   "grab.*": {"enabled": false},'
            '   "grab.success": {"enabled": true}}}}'
        )
        assert bus.is_enabled(events.GRAB_SUCCESS) is True
        assert bus.is_enabled(events.GRAB_BUFFER_BLOCKED) is False

    def test_universal_enable_only_applies_when_nothing_more_specific(
        self, seed_settings,
    ):
        seed_settings(
            '{"per_event_notifications": false,'
            ' "notifications": {"events": {"*": {"enabled": true}}}}'
        )
        # Universal-enabled grants is_enabled even when legacy says no.
        assert bus.is_enabled(events.GRAB_SUCCESS) is True
        assert bus.is_enabled(events.DISCOVERY_SCAN_COMPLETE) is True


# ─── Phase 3 — quiet hours ───────────────────────────────────


class TestEmitQuietHours:
    async def test_suppressible_event_dropped_in_quiet_hours(
        self, seed_settings, mock_ntfy_client, monkeypatch,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t",'
            ' "per_event_notifications": true}'
        )
        from app.notifications import quiet_hours
        monkeypatch.setattr(
            quiet_hours, "is_in_quiet_hours", lambda s, now=None: True,
        )
        result = await bus.emit(events.GRAB_SUCCESS, title="T", message="M")
        assert result is False
        assert mock_ntfy_client["requests"] == []

    async def test_non_suppressible_event_still_fires_in_quiet_hours(
        self, seed_settings, mock_ntfy_client, monkeypatch,
    ):
        """Errors must wake the operator even at 03:00."""
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t",'
            ' "per_event_notifications": true}'
        )
        from app.notifications import quiet_hours
        monkeypatch.setattr(
            quiet_hours, "is_in_quiet_hours", lambda s, now=None: True,
        )
        result = await bus.emit(
            events.PIPELINE_ERROR, title="!", message="boom",
        )
        assert result is True
        assert len(mock_ntfy_client["requests"]) == 1

    async def test_quiet_hours_off_does_not_suppress(
        self, seed_settings, mock_ntfy_client, monkeypatch,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t",'
            ' "per_event_notifications": true}'
        )
        from app.notifications import quiet_hours
        monkeypatch.setattr(
            quiet_hours, "is_in_quiet_hours", lambda s, now=None: False,
        )
        result = await bus.emit(events.GRAB_SUCCESS, title="T", message="M")
        assert result is True


# ─── Phase 3 — priority overrides via routing ────────────────


class TestEmitPriorityOverride:
    async def test_routing_priority_override(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t",'
            ' "per_event_notifications": true,'
            ' "notifications": {"events": {'
            '   "grab.success": {"priority": 5}}}}'
        )
        await bus.emit(events.GRAB_SUCCESS, title="T", message="M")
        req = mock_ntfy_client["requests"][0]
        assert req.headers["priority"] == "5"

    async def test_routing_priority_via_wildcard(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t",'
            ' "per_event_notifications": true,'
            ' "notifications": {"events": {'
            '   "grab.*": {"priority": 1}}}}'
        )
        await bus.emit(events.GRAB_SUCCESS, title="T", message="M")
        req = mock_ntfy_client["requests"][0]
        assert req.headers["priority"] == "1"

    async def test_kwarg_priority_beats_routing(
        self, seed_settings, mock_ntfy_client,
    ):
        """``priority=X`` on the call wins over routing config — call
        sites that explicitly want a priority override get it."""
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t",'
            ' "per_event_notifications": true,'
            ' "notifications": {"events": {'
            '   "grab.success": {"priority": 5}}}}'
        )
        await bus.emit(
            events.GRAB_SUCCESS, title="T", message="M", priority=2,
        )
        req = mock_ntfy_client["requests"][0]
        assert req.headers["priority"] == "2"

    async def test_malformed_priority_falls_back_to_default(
        self, seed_settings, mock_ntfy_client,
    ):
        seed_settings(
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "t",'
            ' "per_event_notifications": true,'
            ' "notifications": {"events": {'
            '   "grab.success": {"priority": "not-a-number"}}}}'
        )
        await bus.emit(events.GRAB_SUCCESS, title="T", message="M")
        req = mock_ntfy_client["requests"][0]
        # grab.success registry default is 3.
        assert req.headers["priority"] == "3"
