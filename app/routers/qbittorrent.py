"""
qBittorrent client diagnostic endpoints.

Currently exposes one endpoint:

  - POST /api/qbittorrent/test
      Connect to the configured qBit instance and return a
      structured connection report (mirrors the ABS pattern at
      `/api/discovery/audiobookshelf/test`). On success surfaces
      version + default save path + visible categories so the
      operator can verify they're talking to the right qBit.
      On failure classifies the error into one of a small set
      of named buckets (DNS, connect_refused, timeout, tls, auth,
      not_qbit, unknown) so the UI can render an actionable
      message instead of a raw exception string.

The endpoint deliberately does its own HTTP login rather than going
through `QbitClient.login()` — the client swallows httpx errors and
returns False, which loses the exception type we need to classify
the failure mode. The diagnostic path is the only place we care
about that distinction; production dispatch is happy with the
boolean.

Why this file is separate from `mam.py` (where a basic
`/v1/mam/test-qbit` lived through v2.22.x): the test is logically
about the qBit download client, not about MAM. Splitting it out
also gives a natural home for any future qBit-specific diagnostics
(category audit, save-path sanity check, etc.) without growing the
MAM router further.
"""
from __future__ import annotations

import logging
import socket
from typing import Any, Optional

import httpx
from fastapi import APIRouter

from app.config import load_settings
from app.secrets import get_secret

_log = logging.getLogger("seshat.qbittorrent")

router = APIRouter(
    prefix="/api/qbittorrent",
    tags=["qbittorrent"],
)


def _classify_httpx_error(exc: BaseException) -> tuple[str, str]:
    """Map an httpx / network exception to a (error_class, error_msg) pair.

    Buckets:
      - `dns`             — DNS resolution failed (gaierror in the cause)
      - `connect_refused` — TCP connect failed (server not listening / wrong port)
      - `timeout`         — request timed out
      - `tls`             — TLS handshake failed (cert / protocol / proxy mismatch)
      - `unknown`         — anything else from httpx land

    The message is a short human-readable string suitable for the
    UI; the original exception type goes into the log line for
    deeper debugging.
    """
    name = type(exc).__name__

    if isinstance(exc, httpx.ConnectTimeout) or isinstance(exc, httpx.ReadTimeout) or isinstance(exc, httpx.TimeoutException):
        return "timeout", f"Request timed out reaching qBit ({name})"

    # DNS errors typically come through as ConnectError wrapping a
    # gaierror. Check the cause chain for socket.gaierror.
    walked = exc
    seen = set()
    while walked is not None and id(walked) not in seen:
        seen.add(id(walked))
        if isinstance(walked, socket.gaierror):
            return "dns", f"DNS lookup failed for the configured URL ({walked})"
        walked = walked.__cause__ or walked.__context__

    if isinstance(exc, httpx.ConnectError):
        # ConnectionRefusedError is the classic "qBit isn't running
        # on the port you told us." A "Network is unreachable" or
        # "Host is unreachable" lives in the same bucket from the
        # operator's perspective — "I can't reach the URL you set."
        return "connect_refused", f"Could not connect to qBit ({exc})"

    # httpx raises this for SSL handshake / protocol mismatch (e.g.
    # plain HTTP on an HTTPS URL or vice versa).
    if "ssl" in name.lower() or "tls" in name.lower() or isinstance(exc, httpx.RemoteProtocolError):
        return "tls", f"TLS/SSL error reaching qBit ({name}: {exc})"

    return "unknown", f"Network error reaching qBit ({name}: {exc})"


async def _qbit_app_info(
    client: httpx.AsyncClient,
    base_url: str,
) -> tuple[Optional[str], Optional[str], Optional[list[str]]]:
    """Best-effort fetch of (version, default_save_path, categories).

    All three calls are tolerant — any single one failing returns
    None for that field but does NOT fail the whole test. The test
    endpoint already established connectivity + auth at this point;
    if a follow-up call 404s or returns junk, the test is still
    "ok" but the UI will show "—" for the missing field.
    """
    version: Optional[str] = None
    save_path: Optional[str] = None
    categories: Optional[list[str]] = None
    headers = {"Referer": base_url}

    try:
        resp = await client.get("/api/v2/app/version", headers=headers)
        if resp.status_code == 200:
            version = resp.text.strip() or None
    except httpx.HTTPError as e:
        _log.info("qBit /app/version probe failed: %s", e)

    try:
        resp = await client.get("/api/v2/app/preferences", headers=headers)
        if resp.status_code == 200:
            prefs = resp.json()
            if isinstance(prefs, dict):
                save_path = (
                    prefs.get("save_path")
                    or prefs.get("default_save_path")
                    or None
                )
    except (httpx.HTTPError, ValueError) as e:
        _log.info("qBit /app/preferences probe failed: %s", e)

    try:
        resp = await client.get("/api/v2/torrents/categories", headers=headers)
        if resp.status_code == 200:
            raw = resp.json()
            if isinstance(raw, dict):
                categories = sorted(raw.keys())
    except (httpx.HTTPError, ValueError) as e:
        _log.info("qBit /torrents/categories probe failed: %s", e)

    return version, save_path, categories


@router.post("/test")
async def test_connection() -> dict[str, Any]:
    """Connect to the configured qBit and report what we see.

    Returns a 200 with one of two shapes:

    Success:
      {
        "ok": true,
        "version": "v5.0.0",
        "default_save_path": "/downloads",
        "categories": ["[mam-reseed]", "..."],
        "watch_category_present": true
      }

    Failure:
      {
        "ok": false,
        "error_class": "dns" | "connect_refused" | "timeout" | "tls"
                       | "auth" | "not_qbit" | "not_configured" | "unknown",
        "error": "short human-readable string"
      }

    Never raises for configuration / connectivity problems — those
    are well-formed test results with `ok=false`. Only programming
    bugs surface as 5xx.

    WARNING on the `auth` class: qBittorrent's WebUI bans the
    caller's IP for ~30 minutes (configurable) after 5 failed logins
    in a row. This endpoint counts. If the user keeps clicking Test
    with bad creds, they will eventually lock themselves out — the
    UI should keep the warning prominent.
    """
    settings = load_settings()
    url = (settings.get("qbit_url") or "").strip()
    username = (settings.get("qbit_username") or "").strip()
    if not url:
        return {
            "ok": False,
            "error_class": "not_configured",
            "error": "qbit_url is not configured",
        }
    if not username:
        return {
            "ok": False,
            "error_class": "not_configured",
            "error": "qbit_username is not configured",
        }

    # Password lives in the encrypted secrets store; the plaintext
    # `qbit_password` setting is a legacy fallback for very old
    # installs that haven't migrated. Try both in that order.
    password = await get_secret("qbit_password")
    if not password:
        password = (settings.get("qbit_password") or "").strip() or None
    if not password:
        return {
            "ok": False,
            "error_class": "not_configured",
            "error": "qbit_password is not configured",
        }

    base_url = url.rstrip("/")
    client_kwargs: dict[str, Any] = {
        "base_url": base_url,
        # Shorter than the dispatch client default; the user is
        # waiting on this response and we'd rather report "timeout"
        # quickly than block the page for 30s.
        "timeout": httpx.Timeout(10.0, connect=5.0),
        "follow_redirects": True,
    }
    transport = _TRANSPORT_OVERRIDE
    if transport is not None:
        client_kwargs["transport"] = transport

    async with httpx.AsyncClient(**client_kwargs) as http:
        try:
            resp = await http.post(
                "/api/v2/auth/login",
                data={"username": username, "password": password},
                headers={"Referer": base_url},
            )
        except httpx.HTTPError as e:
            err_class, err_msg = _classify_httpx_error(e)
            _log.warning(
                "qBit test connection network error: class=%s detail=%s",
                err_class, e,
            )
            return {"ok": False, "error_class": err_class, "error": err_msg}

        # qBit login contract:
        #   200 + "Ok."        → success, session cookie set
        #   200 + "Fails."     → bad creds (handled identically to 403)
        #   204                → IP whitelist bypass (success, no cookie)
        #   403                → bad creds OR IP-ban after 5 failed attempts
        # See `QbitClient.login()` for the production parser this mirrors.
        if resp.status_code == 204:
            login_ok = True
        elif resp.status_code == 200 and resp.text.strip() == "Ok.":
            login_ok = True
        else:
            login_ok = False

        if not login_ok:
            # The only way to land here is a 4xx/5xx or 200 "Fails." —
            # all observable as "qBit answered but rejected our login."
            # qBit returns the SAME 403 for bad creds and IP-ban, so we
            # can't distinguish from one response without burning more
            # attempts. Message names both possibilities so the user
            # knows clicking again could lock them out.
            return {
                "ok": False,
                "error_class": "auth",
                "error": (
                    f"qBit rejected login (HTTP {resp.status_code}). "
                    "Either credentials are wrong OR the WebUI temporarily "
                    "banned this IP after repeated failures (default 30 "
                    "min ban after 5 failures)."
                ),
            }

        version, save_path, categories = await _qbit_app_info(http, base_url)

        watch_category = (settings.get("qbit_watch_category") or "").strip()
        watch_present: Optional[bool] = None
        if categories is not None and watch_category:
            watch_present = watch_category in categories

        return {
            "ok": True,
            "version": version,
            "default_save_path": save_path,
            "categories": categories,
            "watch_category": watch_category or None,
            "watch_category_present": watch_present,
        }


# Tests inject a `httpx.MockTransport` here. Production leaves this
# None; the endpoint then creates an `AsyncClient` with no transport
# override and uses real httpx networking. Module-level rather than
# a router-state attribute because FastAPI doesn't pass router state
# to handlers and the seam only matters for tests anyway.
_TRANSPORT_OVERRIDE: Optional[httpx.BaseTransport] = None
