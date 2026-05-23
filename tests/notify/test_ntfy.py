"""
Unit tests for the ntfy notification module.

Uses httpx.MockTransport to intercept HTTP calls — no real ntfy
server is contacted.
"""
import httpx
import pytest

from app.notify import ntfy


@pytest.fixture(autouse=True)
async def _mock_ntfy_client():
    """Replace the ntfy HTTP client with a mock transport for each test."""
    captured = {"requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["requests"].append(request)
        return httpx.Response(200, text='{"id":"test"}')

    original = ntfy._client
    ntfy._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    captured["client"] = ntfy._client
    try:
        yield captured
    finally:
        await ntfy._client.aclose()
        ntfy._client = original


class TestSend:
    async def test_sends_to_correct_endpoint(self, _mock_ntfy_client):
        await ntfy.send(
            url="https://ntfy.example.com",
            topic="seshat",
            title="Test",
            message="Hello",
        )
        req = _mock_ntfy_client["requests"][0]
        assert str(req.url) == "https://ntfy.example.com/seshat"

    async def test_sets_title_header(self, _mock_ntfy_client):
        await ntfy.send(
            url="https://ntfy.example.com",
            topic="test",
            title="My Title",
            message="body",
        )
        req = _mock_ntfy_client["requests"][0]
        assert req.headers["title"] == "My Title"

    async def test_sets_priority_header(self, _mock_ntfy_client):
        await ntfy.send(
            url="https://ntfy.example.com",
            topic="test",
            title="T",
            message="M",
            priority=5,
        )
        req = _mock_ntfy_client["requests"][0]
        assert req.headers["priority"] == "5"

    async def test_sets_tags_header(self, _mock_ntfy_client):
        await ntfy.send(
            url="https://ntfy.example.com",
            topic="test",
            title="T",
            message="M",
            tags=["books", "fire"],
        )
        req = _mock_ntfy_client["requests"][0]
        assert req.headers["tags"] == "books,fire"

    async def test_message_in_body(self, _mock_ntfy_client):
        await ntfy.send(
            url="https://ntfy.example.com",
            topic="test",
            title="T",
            message="Hello world",
        )
        req = _mock_ntfy_client["requests"][0]
        assert req.content == b"Hello world"

    async def test_returns_true_on_200(self, _mock_ntfy_client):
        result = await ntfy.send(
            url="https://ntfy.example.com",
            topic="test",
            title="T",
            message="M",
        )
        assert result is True

    async def test_noop_when_url_empty(self, _mock_ntfy_client):
        result = await ntfy.send(url="", topic="test", title="T", message="M")
        assert result is False
        assert len(_mock_ntfy_client["requests"]) == 0

    async def test_noop_when_topic_empty(self, _mock_ntfy_client):
        result = await ntfy.send(
            url="https://ntfy.example.com", topic="", title="T", message="M"
        )
        assert result is False
        assert len(_mock_ntfy_client["requests"]) == 0

    async def test_strips_trailing_slash_from_url(self, _mock_ntfy_client):
        await ntfy.send(
            url="https://ntfy.example.com/",
            topic="test",
            title="T",
            message="M",
        )
        req = _mock_ntfy_client["requests"][0]
        assert str(req.url) == "https://ntfy.example.com/test"

    async def test_url_with_topic_combined(self, _mock_ntfy_client):
        """User can pass full URL with topic and empty topic param."""
        await ntfy.send(
            url="https://ntfy.example.com/my-topic",
            topic="",
            title="T",
            message="M",
        )
        req = _mock_ntfy_client["requests"][0]
        assert str(req.url) == "https://ntfy.example.com/my-topic"

    async def test_url_without_scheme_gets_https(self, _mock_ntfy_client):
        """User can omit https:// prefix."""
        await ntfy.send(
            url="ntfy.sh",
            topic="my-topic",
            title="T",
            message="M",
        )
        req = _mock_ntfy_client["requests"][0]
        assert str(req.url) == "https://ntfy.sh/my-topic"

    async def test_url_no_scheme_with_combined_topic(self, _mock_ntfy_client):
        """The exact form the user has in their compose."""
        await ntfy.send(
            url="ntfy.sh/turtles81-autobrr-books",
            topic="",
            title="T",
            message="M",
        )
        req = _mock_ntfy_client["requests"][0]
        assert str(req.url) == "https://ntfy.sh/turtles81-autobrr-books"

    async def test_em_dash_in_title_does_not_crash(self, _mock_ntfy_client):
        """Daily digest titles contain em-dashes ("Daily digest — N
        new books"). httpx rejects non-ASCII in headers; the v2.3.1
        fix folds them to ASCII before sending. Pre-fix this raised
        UnicodeEncodeError and dropped the entire notification."""
        result = await ntfy.send(
            url="https://ntfy.example.com",
            topic="t",
            title="Daily digest — 5 new books",
            message="body",
        )
        assert result is True
        req = _mock_ntfy_client["requests"][0]
        assert req.headers["title"] == "Daily digest - 5 new books"

    async def test_smart_quotes_and_ellipsis_folded(self, _mock_ntfy_client):
        result = await ntfy.send(
            url="https://ntfy.example.com",
            topic="t",
            title="“The Way” of Kings…",
            message="m",
        )
        assert result is True
        req = _mock_ntfy_client["requests"][0]
        assert req.headers["title"] == '"The Way" of Kings...'

    async def test_unmapped_unicode_dropped_not_crashed(self, _mock_ntfy_client):
        """Characters outside the fold table (e.g. Japanese kana)
        get stripped rather than crashing the send. Title legibility
        degrades but the notification still goes through."""
        result = await ntfy.send(
            url="https://ntfy.example.com",
            topic="t",
            title="Book あい finished",
            message="m",
        )
        assert result is True
        req = _mock_ntfy_client["requests"][0]
        # Japanese stripped, ASCII preserved.
        assert req.headers["title"] == "Book  finished"


class TestConvenienceSenders:
    async def test_notify_grab(self, _mock_ntfy_client):
        result = await ntfy.notify_grab(
            "https://ntfy.example.com", "seshat",
            "The Way of Kings", "Brandon Sanderson", "Ebooks - Fantasy",
        )
        assert result is True
        req = _mock_ntfy_client["requests"][0]
        assert req.headers["title"] == "New book grabbed"
        assert b"The Way of Kings" in req.content
        assert b"Brandon Sanderson" in req.content

    async def test_notify_download_complete(self, _mock_ntfy_client):
        result = await ntfy.notify_download_complete(
            "https://ntfy.example.com", "seshat",
            "The Way of Kings", "Brandon Sanderson",
        )
        assert result is True
        req = _mock_ntfy_client["requests"][0]
        assert req.headers["title"] == "Download complete"

    async def test_notify_pipeline_complete(self, _mock_ntfy_client):
        result = await ntfy.notify_pipeline_complete(
            "https://ntfy.example.com", "seshat",
            "The Way of Kings", "Calibre",
        )
        assert result is True
        req = _mock_ntfy_client["requests"][0]
        assert "Calibre" in req.headers["title"]

    async def test_notify_error(self, _mock_ntfy_client):
        result = await ntfy.notify_error(
            "https://ntfy.example.com", "seshat",
            "The Way of Kings", "Calibre rejected the file",
        )
        assert result is True
        req = _mock_ntfy_client["requests"][0]
        assert req.headers["priority"] == "4"


class TestIsEventEnabled:
    """v2.11.1 N1 — the per-event ntfy gate. Pre-v2.11.1 the master
    `per_event_notifications` toggle was checked at each call site
    but the per-event sub-toggles (`notify_on_grab`,
    `notify_on_download_complete`, `notify_on_pipeline_error`) were
    not. UAT-confirmed bug: users who disabled specific events in
    Settings still received them. This helper centralizes the gate
    so every call site stays in sync."""

    def _seed_settings(self, tmp_path, monkeypatch, payload: str) -> None:
        from app import config
        p = tmp_path / "settings.json"
        p.write_text(payload)
        monkeypatch.setattr(config, "SETTINGS_PATH", p)
        config._settings_cache["data"] = None
        config._settings_cache["mtime"] = object()

    def test_master_off_returns_false(self, tmp_path, monkeypatch):
        self._seed_settings(tmp_path, monkeypatch,
            '{"per_event_notifications": false, "notify_on_grab": true}')
        assert ntfy.is_event_enabled("grab") is False

    def test_master_on_subtoggle_off_returns_false(
        self, tmp_path, monkeypatch,
    ):
        self._seed_settings(tmp_path, monkeypatch,
            '{"per_event_notifications": true, "notify_on_grab": false}')
        assert ntfy.is_event_enabled("grab") is False

    def test_master_on_subtoggle_on_returns_true(
        self, tmp_path, monkeypatch,
    ):
        self._seed_settings(tmp_path, monkeypatch,
            '{"per_event_notifications": true, "notify_on_grab": true}')
        assert ntfy.is_event_enabled("grab") is True

    def test_master_on_subtoggle_missing_defaults_true(
        self, tmp_path, monkeypatch,
    ):
        """Upgraded settings.json may pre-date the per-event toggle
        keys. Missing → True (ship-default — matches the
        DEFAULT_SETTINGS shape in app/config.py)."""
        self._seed_settings(tmp_path, monkeypatch,
            '{"per_event_notifications": true}')
        assert ntfy.is_event_enabled("grab") is True
        assert ntfy.is_event_enabled("download_complete") is True
        assert ntfy.is_event_enabled("pipeline_error") is True

    def test_per_event_keys_independent(self, tmp_path, monkeypatch):
        """Disabling one sub-toggle doesn't affect the others."""
        self._seed_settings(tmp_path, monkeypatch,
            '{"per_event_notifications": true,'
            '"notify_on_grab": false,'
            '"notify_on_download_complete": true,'
            '"notify_on_pipeline_error": true}')
        assert ntfy.is_event_enabled("grab") is False
        assert ntfy.is_event_enabled("download_complete") is True
        assert ntfy.is_event_enabled("pipeline_error") is True


# ─── v2.24.0 — BasicAuth credential split ────────────────────


class TestAuth:
    """v2.24.0 — `ntfy_username` setting + encrypted `ntfy_password`
    secret resolve into BasicAuth on every `send()`. Inline
    `https://user:pass@host/topic` URLs still work as a backwards-
    compat fallback but the dedicated fields take precedence.
    """

    def _seed_settings(self, tmp_path, monkeypatch, payload: str) -> None:
        from app import config
        p = tmp_path / "settings.json"
        p.write_text(payload)
        monkeypatch.setattr(config, "SETTINGS_PATH", p)
        config._settings_cache["data"] = None
        config._settings_cache["mtime"] = object()

    def _stub_secret(self, monkeypatch, value):
        """Replace `app.secrets.get_secret` with a fixed return value.

        Patches the symbol imported INSIDE `_resolve_auth` (which does
        a local import per call), not the module attribute directly —
        that's the safer seam for runtime-imported names.
        """
        async def _fake(_key):
            return value
        monkeypatch.setattr("app.secrets.get_secret", _fake)

    async def test_no_auth_when_username_empty(
        self, _mock_ntfy_client, tmp_path, monkeypatch,
    ):
        """No username + no inline creds → no Authorization header."""
        self._seed_settings(tmp_path, monkeypatch,
            '{"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "seshat"}')
        self._stub_secret(monkeypatch, None)
        await ntfy.send(
            url="https://ntfy.example.com", topic="seshat",
            title="T", message="M",
        )
        req = _mock_ntfy_client["requests"][0]
        assert "authorization" not in {k.lower() for k in req.headers.keys()}

    async def test_dedicated_username_password_sets_basicauth(
        self, _mock_ntfy_client, tmp_path, monkeypatch,
    ):
        """Username + password from secret → Authorization: Basic ..."""
        import base64
        self._seed_settings(tmp_path, monkeypatch,
            '{"ntfy_url": "https://ntfy.example.com",'
            '"ntfy_topic": "seshat",'
            '"ntfy_username": "alice"}')
        self._stub_secret(monkeypatch, "s3cret")
        await ntfy.send(
            url="https://ntfy.example.com", topic="seshat",
            title="T", message="M",
        )
        req = _mock_ntfy_client["requests"][0]
        expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
        assert req.headers.get("authorization") == expected

    async def test_inline_url_creds_strip_from_endpoint(
        self, _mock_ntfy_client, tmp_path, monkeypatch,
    ):
        """Legacy URL form: creds extracted into auth header, URL
        rewritten so the path the server sees is bare.

        This matters because every reverse-proxy / WAF on the way
        logs the full URL — inline creds would leak into logs.
        """
        import base64
        self._seed_settings(tmp_path, monkeypatch,
            '{"ntfy_url": "https://bob:hunter2@ntfy.example.com",'
            '"ntfy_topic": "seshat"}')
        self._stub_secret(monkeypatch, None)
        await ntfy.send(
            url="https://bob:hunter2@ntfy.example.com", topic="seshat",
            title="T", message="M",
        )
        req = _mock_ntfy_client["requests"][0]
        # URL must NOT contain the password.
        assert "hunter2" not in str(req.url)
        assert str(req.url) == "https://ntfy.example.com/seshat"
        # Auth header populated from the stripped creds.
        expected = "Basic " + base64.b64encode(b"bob:hunter2").decode()
        assert req.headers.get("authorization") == expected

    async def test_dedicated_creds_win_over_inline_url_creds(
        self, _mock_ntfy_client, tmp_path, monkeypatch,
    ):
        """If both are present, the dedicated fields take precedence.

        Inline-URL creds are a backwards-compat seam; once the user
        moves to the dedicated fields, those should be authoritative
        even if the inline form is still in the URL (e.g., they
        haven't pruned the URL yet).
        """
        import base64
        self._seed_settings(tmp_path, monkeypatch,
            '{"ntfy_url": "https://bob:hunter2@ntfy.example.com",'
            '"ntfy_topic": "seshat",'
            '"ntfy_username": "alice"}')
        self._stub_secret(monkeypatch, "newpw")
        await ntfy.send(
            url="https://bob:hunter2@ntfy.example.com", topic="seshat",
            title="T", message="M",
        )
        req = _mock_ntfy_client["requests"][0]
        expected = "Basic " + base64.b64encode(b"alice:newpw").decode()
        assert req.headers.get("authorization") == expected

    async def test_username_set_but_password_missing_falls_to_inline(
        self, _mock_ntfy_client, tmp_path, monkeypatch,
    ):
        """Username set but secret store empty → don't send half an
        auth header. Fall through to inline-URL creds if present;
        otherwise no auth.

        Mid-setup state — the user typed a username but hasn't saved
        the password yet. Sending the username alone would just
        produce 401s; falling through to whatever else is configured
        keeps the previous behavior working.
        """
        import base64
        self._seed_settings(tmp_path, monkeypatch,
            '{"ntfy_url": "https://bob:hunter2@ntfy.example.com",'
            '"ntfy_topic": "seshat",'
            '"ntfy_username": "alice"}')
        self._stub_secret(monkeypatch, None)  # no ntfy_password
        await ntfy.send(
            url="https://bob:hunter2@ntfy.example.com", topic="seshat",
            title="T", message="M",
        )
        req = _mock_ntfy_client["requests"][0]
        # Fell back to inline creds.
        expected = "Basic " + base64.b64encode(b"bob:hunter2").decode()
        assert req.headers.get("authorization") == expected

    async def test_endpoint_resolution_strips_inline_creds_in_url(
        self,
    ):
        """`_resolve_endpoint()` must NOT carry inline creds into the
        endpoint URL it returns — they get extracted separately by
        `_resolve_auth_from_url()`.
        """
        endpoint = ntfy._resolve_endpoint(
            "https://bob:hunter2@ntfy.example.com", "seshat",
        )
        assert endpoint == "https://ntfy.example.com/seshat"
        assert "hunter2" not in endpoint
        assert "bob" not in endpoint

    async def test_resolve_auth_from_url_extracts_credentials(self):
        """Helper isolation: `_resolve_auth_from_url()` returns the
        creds tuple when the URL has them, None otherwise.
        """
        assert ntfy._resolve_auth_from_url(
            "https://user:pass@host.example.com"
        ) == ("user", "pass")
        assert ntfy._resolve_auth_from_url(
            "https://host.example.com"
        ) is None
        # Edge case: URL with username but no password — treat as
        # "no inline creds" rather than half-broken.
        assert ntfy._resolve_auth_from_url(
            "https://user@host.example.com"
        ) is None
