"""
Unit tests for the MAM user-status API (jsonLoad.php).

Covers the happy path, error handling, caching, and cookie rotation
integration. All requests go through the FakeMAM fixture — no real
MAM traffic.
"""
import json

import pytest

from app.mam.user_status import (
    UserStatus,
    UserStatusError,
    get_user_status,
    invalidate_cache,
)
from tests.fake_mam import DEFAULT_USER_STATUS_BODY


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure each test starts with a clean user-status cache."""
    invalidate_cache()
    yield
    invalidate_cache()


# ─── Happy path ─────────────────────────────────────────────


class TestGetUserStatusSuccess:
    async def test_parses_all_fields(self, fake_mam):
        status = await get_user_status(token="good_token")
        assert isinstance(status, UserStatus)
        assert status.ratio == 91184.8
        assert status.wedges == 462
        assert status.seedbonus == 71088
        assert status.classname == "Elite VIP"
        assert status.username == "Turtles81"
        assert status.uid == 224285
        assert status.uploaded_bytes == 8768386723586
        assert status.downloaded_bytes == 96160650

    async def test_request_hits_jsonload_endpoint(self, fake_mam):
        await get_user_status(token="good_token")
        assert any("jsonLoad.php" in str(req.url) for req in fake_mam.requests)

    async def test_request_attaches_cookie(self, fake_mam):
        await get_user_status(token="my_session_cookie")
        assert "my_session_cookie" in fake_mam.cookies_seen()


# ─── Caching ────────────────────────────────────────────────


class TestUserStatusCaching:
    async def test_second_call_uses_cache(self, fake_mam):
        await get_user_status(token="tok")
        await get_user_status(token="tok")
        # Only one request should have been made.
        jsonload_requests = [
            r for r in fake_mam.requests if "jsonLoad.php" in str(r.url)
        ]
        assert len(jsonload_requests) == 1

    async def test_rotated_cookie_still_hits_the_cache(self, fake_mam):
        """MAM rotates mam_id on every jsonLoad response, so the next read
        always carries a new token. Keyed per token, the cache never hit
        and every status read was a MAM call (fixed 2026-10-06)."""
        from app.mam.cookie import get_current_token, set_current_token

        set_current_token("first_cookie")
        fake_mam.rotate_cookie_to = "rotated_cookie"
        await get_user_status(token="first_cookie")
        assert get_current_token() == "rotated_cookie"
        await get_user_status(token=get_current_token())
        jsonload_requests = [
            r for r in fake_mam.requests if "jsonLoad.php" in str(r.url)
        ]
        assert len(jsonload_requests) == 1

    async def test_saving_a_new_cookie_clears_the_cache(self, fake_mam):
        from app.mam.cookie import set_current_token

        set_current_token("first_cookie")
        await get_user_status(token="first_cookie")
        set_current_token("pasted_cookie")   # maybe another account
        await get_user_status(token="pasted_cookie")
        jsonload_requests = [
            r for r in fake_mam.requests if "jsonLoad.php" in str(r.url)
        ]
        assert len(jsonload_requests) == 2

    async def test_ttl_zero_bypasses_cache(self, fake_mam):
        await get_user_status(token="tok")
        await get_user_status(token="tok", ttl=0)
        jsonload_requests = [
            r for r in fake_mam.requests if "jsonLoad.php" in str(r.url)
        ]
        assert len(jsonload_requests) == 2

    async def test_invalidate_cache_forces_refetch(self, fake_mam):
        await get_user_status(token="tok")
        invalidate_cache()
        await get_user_status(token="tok")
        jsonload_requests = [
            r for r in fake_mam.requests if "jsonLoad.php" in str(r.url)
        ]
        assert len(jsonload_requests) == 2


# ─── Error handling ─────────────────────────────────────────


class TestUserStatusErrors:
    async def test_http_403_raises(self, fake_mam):
        fake_mam.user_status.status = 403
        fake_mam.user_status.body = b"forbidden"
        with pytest.raises(UserStatusError, match="HTTP 403"):
            await get_user_status(token="bad_token")

    async def test_empty_body_raises(self, fake_mam):
        fake_mam.user_status.body = b""
        with pytest.raises(UserStatusError, match="invalid JSON"):
            await get_user_status(token="tok")

    async def test_html_body_raises(self, fake_mam):
        fake_mam.user_status.status = 200
        fake_mam.user_status.body = b"<html>login</html>"
        with pytest.raises(UserStatusError, match="invalid JSON"):
            await get_user_status(token="tok")

    async def test_non_dict_response_raises(self, fake_mam):
        fake_mam.user_status.body = b'"just a string"'
        with pytest.raises(UserStatusError, match="unexpected response shape"):
            await get_user_status(token="tok")

    async def test_missing_fields_default_to_zero(self, fake_mam):
        # Minimal valid JSON object — all fields should fall back to defaults.
        fake_mam.user_status.body = b'{"username":"test"}'
        status = await get_user_status(token="tok")
        assert status.ratio == 0.0
        assert status.wedges == 0
        assert status.seedbonus == 0
        assert status.classname == ""


# ─── Cookie rotation ───────────────────────────────────────


class TestUserStatusCookieRotation:
    async def test_rotation_fires_on_response(self, fake_mam):
        from app.mam.cookie import get_current_token, set_current_token

        set_current_token("old_cookie")
        fake_mam.rotate_cookie_to = "new_rotated_cookie"

        await get_user_status(token="old_cookie")
        assert get_current_token() == "new_rotated_cookie"
