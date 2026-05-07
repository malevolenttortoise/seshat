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
