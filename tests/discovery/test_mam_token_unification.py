"""
Regression tests for the v3.10.0 "cookie won't apply until restart" bug.

`app.discovery.sources.mam` used to keep its OWN `_current_token`
module global, separate from `app.mam.cookie`'s, and resolved requests
as `_current_token or token` — the stale global beating the explicit
argument. Nothing in production ever re-seeded it (the local
`set_current_token` was dead code outside tests), so once a rotation
populated it, no freshly-saved cookie could displace it in-process.

The user-visible shape, reported on the MAM forum 2026-09-13:

  - MAM Status showed the cookie healthy (it reads `app.mam.cookie`)
    and buying upload credit worked.
  - Discovery search returned
    "HTTP 403 — session rejected. Check token is valid for this
    server's IP/ASN." on every scan.
  - Re-pasting the cookie could not fix it — each new paste invalidated
    the previous MAM session server-side while this module stayed
    pinned to a token that was already dead.
  - `docker compose up` fixed it, because a fresh process starts with
    the global empty.

These tests pin the corrected behavior: ONE token authority, explicit
argument wins, and rotations observed here land in the shared slot.
"""
import httpx
import pytest

from app.discovery.sources import mam as disc_mam
from app.mam import cookie as cookie_mod


@pytest.fixture
def clean_token():
    """Save/restore the shared token slot around each test."""
    saved = cookie_mod._current_token
    saved_cb = cookie_mod._rotation_callback
    cookie_mod._current_token = None
    cookie_mod._rotation_callback = None
    yield
    cookie_mod._current_token = saved
    cookie_mod._rotation_callback = saved_cb


def _install_recorder(monkeypatch, *, set_cookie: str = "") -> list:
    """Route this module's HTTP layer through a MockTransport.

    Returns a list that receives the outbound `Cookie` header of every
    request, so a test can assert which token actually hit the wire.
    """
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("cookie"))
        headers = {"Set-Cookie": set_cookie} if set_cookie else {}
        return httpx.Response(200, headers=headers, text='{"data":[]}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(disc_mam, "_get_client", lambda: client)
    return sent


class TestExplicitTokenWins:
    """The caller's token beats whatever rotation last stashed."""

    async def test_post_uses_explicit_token_over_stale_global(
        self, clean_token, monkeypatch
    ):
        sent = _install_recorder(monkeypatch)
        # A rotation from some earlier scan left a now-dead token.
        cookie_mod.set_current_token("DEAD_ROTATED_TOKEN")

        # The router resolves the freshly-saved cookie and passes it in.
        await disc_mam._do_post(
            disc_mam.MAM_SEARCH_URL, "FRESHLY_PASTED_TOKEN", "{}"
        )

        assert sent == ["mam_id=FRESHLY_PASTED_TOKEN"]

    async def test_get_uses_explicit_token_over_stale_global(
        self, clean_token, monkeypatch
    ):
        sent = _install_recorder(monkeypatch)
        cookie_mod.set_current_token("DEAD_ROTATED_TOKEN")

        await disc_mam._do_get(disc_mam.MAM_SEARCH_URL, "FRESHLY_PASTED_TOKEN")

        assert sent == ["mam_id=FRESHLY_PASTED_TOKEN"]

    async def test_falls_back_to_shared_token_when_no_explicit(
        self, clean_token, monkeypatch
    ):
        """An empty explicit token still resolves the live one.

        Callers that pass "" (nothing configured yet at their layer)
        must not send an empty cookie when a live token exists.
        """
        sent = _install_recorder(monkeypatch)
        cookie_mod.set_current_token("LIVE_TOKEN")

        await disc_mam._do_post(disc_mam.MAM_SEARCH_URL, "", "{}")

        assert sent == ["mam_id=LIVE_TOKEN"]


class TestRotationSharesOneSlot:
    """A rotation seen here must update the slot everyone else reads."""

    async def test_rotation_lands_in_shared_slot(self, clean_token, monkeypatch):
        _install_recorder(
            monkeypatch,
            set_cookie="mam_id=ROTATED_BY_SEARCH; path=/; domain=.myanonamouse.net",
        )
        cookie_mod.set_current_token("ORIGINAL")

        await disc_mam._do_post(disc_mam.MAM_SEARCH_URL, "ORIGINAL", "{}")

        # Before the fix this landed in a private global and
        # app.mam.cookie never learned about it, so MAM Status and
        # Discovery drifted apart on every scan.
        assert cookie_mod.get_current_token() == "ROTATED_BY_SEARCH"

    async def test_rotation_fires_the_shared_persistence_callback(
        self, clean_token, monkeypatch
    ):
        """Rotations here must persist, not just live in memory.

        This module's own `_rotation_callback` was never wired in
        production, so every rotation observed on a search request was
        silently dropped instead of being written to the secret store.
        """
        persisted: list = []

        async def fake_callback(token: str) -> None:
            persisted.append(token)

        _install_recorder(
            monkeypatch,
            set_cookie="mam_id=ROTATED_BY_SEARCH; path=/; domain=.myanonamouse.net",
        )
        cookie_mod.set_current_token("ORIGINAL")
        cookie_mod.set_rotation_callback(fake_callback)

        await disc_mam._do_post(disc_mam.MAM_SEARCH_URL, "ORIGINAL", "{}")

        assert persisted == ["ROTATED_BY_SEARCH"]


class TestNoParallelTokenState:
    """Guard against reintroducing a second token authority."""

    def test_module_has_no_private_token_globals(self):
        for name in (
            "_current_token",
            "_rotation_callback",
            "_last_rotation_save",
        ):
            assert not hasattr(disc_mam, name), (
                f"{name} is back in app.discovery.sources.mam. This module "
                "must route token state through app.mam.cookie — a second "
                "slot is the v3.10.0 stale-cookie bug."
            )

    async def test_validation_delegates_to_cookie_module(
        self, clean_token, monkeypatch
    ):
        """Discovery's validate must probe via app.mam.cookie.

        Two copies of the same probe is how "Status green, search 403"
        became representable. One implementation, one token.
        """
        called: list = []

        async def fake_validate(token: str, skip_ip_update: bool = True) -> dict:
            called.append((token, skip_ip_update))
            return {
                "success": True,
                "message": "Connection successful",
                "ip_result": None,
                "search_result": {"success": True, "message": "ok"},
            }

        monkeypatch.setattr(cookie_mod, "validate", fake_validate)

        result = await disc_mam.validate_connection("TOKEN_XYZ", True)

        assert called == [("TOKEN_XYZ", True)]
        assert result["success"] is True

    async def test_validation_failure_keeps_search_auth_wording(
        self, clean_token, monkeypatch
    ):
        """The Discovery panel's "Search auth failed:" prefix survives.

        Users quote this string in bug reports; app.mam.cookie's own
        wording is "Session verify failed".
        """
        async def fake_validate(token: str, skip_ip_update: bool = True) -> dict:
            return {
                "success": False,
                "message": "Session verify failed: HTTP 403 — session rejected.",
                "ip_result": None,
                "search_result": {
                    "success": False,
                    "message": "HTTP 403 — session rejected.",
                },
            }

        monkeypatch.setattr(cookie_mod, "validate", fake_validate)

        result = await disc_mam.validate_connection("TOKEN_XYZ", True)

        assert result["success"] is False
        assert result["message"].startswith("Search auth failed:")
