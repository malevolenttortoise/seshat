"""
Tests for the qBittorrent diagnostic router.

Exercises `POST /api/qbittorrent/test` across the failure-class taxonomy
the UI relies on:

  - not_configured     — missing url / username / password
  - dns                — DNS lookup failure
  - connect_refused    — TCP RST / nothing listening
  - timeout            — request timed out
  - auth               — qBit returned HTTP 403 (bad creds OR IP-ban)
  - ok                 — full happy path with version, save_path, categories

The QbitClient is constructed inside the endpoint, so we patch the
QbitClient symbol in `app.routers.qbittorrent` to inject an
`httpx.MockTransport` per test. That way real test-routes the
endpoint's own assembly logic without touching the network.
"""
from __future__ import annotations

import json
import socket
from typing import Optional

import httpx
import pytest
from fastapi import FastAPI

from app import config
from app.routers import qbittorrent as qbit_router_mod
from app.routers.qbittorrent import router as qbit_router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(qbit_router)
    return app


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Isolate settings.json + secrets to tmp_path.

    Seeds qbit_url/username so the not_configured path doesn't fire
    unintentionally. Individual tests override pieces as needed.
    Secrets defaults to an empty dict so we can drive `get_secret`
    deterministically.
    """
    p = tmp_path / "settings.json"
    seed = {
        **config.DEFAULT_SETTINGS,
        "qbit_url": "http://qbit.example.test:8080",
        "qbit_username": "admin",
        "qbit_password": "fallback-plaintext",  # used if secret store empty
        "qbit_watch_category": "[mam-reseed]",
    }
    p.write_text(json.dumps(seed))
    monkeypatch.setattr(config, "SETTINGS_PATH", p)
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = object()
    # Stub the secrets store so the endpoint doesn't reach into
    # a real encrypted file under the data dir.
    async def _fake_get_secret(_key: str) -> Optional[str]:
        return None  # forces fallback to plaintext setting
    monkeypatch.setattr("app.routers.qbittorrent.get_secret", _fake_get_secret)
    # Ensure transport override is clean between tests.
    monkeypatch.setattr(qbit_router_mod, "_TRANSPORT_OVERRIDE", None)
    yield p
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = object()


def _install_transport(monkeypatch, handler):
    """Install a MockTransport on the endpoint's seam.

    The endpoint builds its own `httpx.AsyncClient` with the configured
    URL + timeout; the `_TRANSPORT_OVERRIDE` module attribute lets us
    swap the transport in without touching the rest of the construction
    (timeout, redirects, etc.).
    """
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(qbit_router_mod, "_TRANSPORT_OVERRIDE", transport)
    return transport


# ─── Configuration gates ─────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_not_configured_when_url_missing(isolated_settings, monkeypatch):
    isolated_settings.write_text(json.dumps({**config.DEFAULT_SETTINGS, "qbit_url": ""}))
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = object()

    transport = httpx.ASGITransport(_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/qbittorrent/test")
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": False, "error_class": "not_configured", "error": "qbit_url is not configured"}


@pytest.mark.asyncio
async def test_returns_not_configured_when_password_missing(isolated_settings):
    isolated_settings.write_text(json.dumps({
        **config.DEFAULT_SETTINGS,
        "qbit_url": "http://qbit.example.test:8080",
        "qbit_username": "admin",
        "qbit_password": "",  # plaintext empty; secret store stubbed empty by fixture
    }))
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = object()

    transport = httpx.ASGITransport(_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/qbittorrent/test")
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == "not_configured"
    assert "qbit_password" in body["error"]


# ─── Network failure classes ─────────────────────────────────


@pytest.mark.asyncio
async def test_classifies_dns_failure(isolated_settings, monkeypatch):
    def _raise_gaierror(request):
        raise httpx.ConnectError(
            "could not resolve",
            request=request,
        ) from socket.gaierror("Name or service not known")

    _install_transport(monkeypatch, _raise_gaierror)

    transport = httpx.ASGITransport(_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/qbittorrent/test")
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == "dns"


@pytest.mark.asyncio
async def test_classifies_connect_refused(isolated_settings, monkeypatch):
    def _raise_refused(request):
        raise httpx.ConnectError("Connection refused", request=request)

    _install_transport(monkeypatch, _raise_refused)

    transport = httpx.ASGITransport(_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/qbittorrent/test")
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == "connect_refused"


@pytest.mark.asyncio
async def test_classifies_timeout(isolated_settings, monkeypatch):
    def _raise_timeout(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    _install_transport(monkeypatch, _raise_timeout)

    transport = httpx.ASGITransport(_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/qbittorrent/test")
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == "timeout"


# ─── Auth failure ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_failure_returns_403_class(isolated_settings, monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(403, content=b"")
        return httpx.Response(404)

    _install_transport(monkeypatch, _handler)

    transport = httpx.ASGITransport(_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/qbittorrent/test")
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == "auth"
    # Message must mention both possibilities (creds + IP ban) so the
    # user knows clicking again could lock them out.
    assert "credentials" in body["error"].lower()
    assert "ban" in body["error"].lower()


# ─── Happy path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_success_returns_version_save_path_and_categories(isolated_settings, monkeypatch):
    """Full happy path with all three follow-up calls succeeding.

    Also verifies `watch_category_present=True` when the configured
    watch category is in the returned category list.
    """
    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/auth/login":
            return httpx.Response(200, content=b"Ok.")
        if path == "/api/v2/app/version":
            return httpx.Response(200, content=b"v5.0.0")
        if path == "/api/v2/app/preferences":
            return httpx.Response(200, json={"save_path": "/downloads", "other": "ignored"})
        if path == "/api/v2/torrents/categories":
            return httpx.Response(200, json={
                "[mam-reseed]": {"name": "[mam-reseed]", "savePath": ""},
                "books": {"name": "books", "savePath": ""},
            })
        return httpx.Response(404)

    _install_transport(monkeypatch, _handler)

    transport = httpx.ASGITransport(_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/qbittorrent/test")
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == "v5.0.0"
    assert body["default_save_path"] == "/downloads"
    assert set(body["categories"]) == {"[mam-reseed]", "books"}
    assert body["watch_category"] == "[mam-reseed]"
    assert body["watch_category_present"] is True


@pytest.mark.asyncio
async def test_success_with_missing_watch_category_flags_absent(isolated_settings, monkeypatch):
    """Watch category configured but not returned in qBit's category list."""
    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/auth/login":
            return httpx.Response(200, content=b"Ok.")
        if path == "/api/v2/app/version":
            return httpx.Response(200, content=b"v5.0.0")
        if path == "/api/v2/app/preferences":
            return httpx.Response(200, json={"save_path": "/downloads"})
        if path == "/api/v2/torrents/categories":
            # Note: missing `[mam-reseed]`.
            return httpx.Response(200, json={"books": {"name": "books", "savePath": ""}})
        return httpx.Response(404)

    _install_transport(monkeypatch, _handler)

    transport = httpx.ASGITransport(_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/qbittorrent/test")
    body = r.json()
    assert body["ok"] is True
    assert body["watch_category_present"] is False


@pytest.mark.asyncio
async def test_success_tolerates_partial_info_failures(isolated_settings, monkeypatch):
    """Login OK but /app/version 500s — endpoint still returns ok=true.

    The follow-up calls are best-effort. A version probe failure should
    NOT downgrade an otherwise successful test, because the operator
    has already learned what they wanted (qBit is reachable + creds OK).
    """
    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/auth/login":
            return httpx.Response(200, content=b"Ok.")
        if path == "/api/v2/app/version":
            return httpx.Response(500, content=b"oops")
        if path == "/api/v2/app/preferences":
            return httpx.Response(200, json={"save_path": "/downloads"})
        if path == "/api/v2/torrents/categories":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    _install_transport(monkeypatch, _handler)

    transport = httpx.ASGITransport(_make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/qbittorrent/test")
    body = r.json()
    assert body["ok"] is True
    assert body["version"] is None  # probe failed
    assert body["default_save_path"] == "/downloads"
    assert body["categories"] == []  # empty dict → empty list, not None
