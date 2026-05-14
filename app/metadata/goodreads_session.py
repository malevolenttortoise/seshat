"""
Centralized Goodreads HTTP plumbing for Cloudflare bypass + soft-block tracking.

Every caller that fetches goodreads.com (the metadata source, the
discovery source, the goodreads_id_resolver, the paste-URL importer)
routes its requests through this module so:

  - TLS fingerprint impersonation (curl_cffi chrome120) happens
    uniformly. Python's stdlib TLS fingerprint is on every bot-detection
    blocklist; curl_cffi drives libcurl-impersonate to replicate Chrome's
    handshake exactly. Cloudflare's JA3 check passes without ever
    needing to solve a JS challenge.
  - Soft-block detection (HTTP 202 OR 2xx with empty body) is identical
    across all call sites. The detection writes the shared
    `goodreads_session_state` runtime flag, which the enricher dispatcher
    reads to skip Goodreads on subsequent calls until the user clears it
    via the Settings panel.
  - Conservative rate-limit jitter (5s + 0-1s uniform jitter by default)
    is applied uniformly so request density never spikes — Cloudflare
    flags density-based patterns even when the per-request fingerprint
    is clean.

**Phase A (v2.13.0)** — curl_cffi alone, no cookie injection. UAT will
measure whether Chrome120 fingerprint impersonation is enough to clear
the 202s under burst load.

**Phase B (deferred)** — if Phase A 202s come back under sustained scan,
the inject hook at `_build_cookie_header()` flips on with encrypted-store
`goodreads_cf_clearance` / `goodreads_session_id2` / `goodreads_browser_ua`.
Adding cookies later requires no caller changes — they all already go
through `get()`.

Soft-block flag shape (three flat keys in settings.json, behind
`_RUNTIME_STATE_KEYS` so PATCH can't accidentally clear them):

  goodreads_session_state:        "active" | "soft_blocked" | "unknown"
  goodreads_session_state_since:  unix timestamp when state last flipped
  goodreads_session_last_status:  HTTP status of the most recent response

Frontend GoodreadsStatusCard reads these three keys; the "Mark as
active" button calls `mark_active()` to flip back.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional

import httpx

from app.config import load_settings, save_settings

_log = logging.getLogger("seshat.metadata.goodreads_session")

_BASE = "https://www.goodreads.com"

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# Conservative default per Mark's Phase-A design: 5s + 0-1s jitter.
# Tunable via the user-facing source_rate_limits setting (existing).
_DEFAULT_RATE_LIMIT = 5.0
_JITTER_RANGE = (0.0, 1.0)


def is_cloudflare_soft_block(resp: Any) -> bool:
    """Detect Goodreads' Cloudflare 202-with-empty-body interstitial.

    Cloudflare returns HTTP 202 Accepted with an empty body when its
    bot-manager wants the client to solve a JS challenge. Real browsers
    do; httpx/requests don't. A 2xx response with a 0-byte body also
    counts (defensive — some interstitial variants return 200 + empty).
    """
    if resp is None:
        return False
    status = getattr(resp, "status_code", None)
    if status == 202:
        return True
    if status is not None and 200 <= status < 300:
        body = getattr(resp, "content", None) or b""
        if not body:
            return True
    return False


# ─── Runtime-state flag (read/write through settings.json) ────────────


def get_session_state() -> dict:
    """Read the three runtime-state keys as a single dict for the frontend.

    Returns {"state": str, "since": float|None, "last_status": int|None}.
    "state" defaults to "unknown" on a fresh install (no probe yet).
    """
    s = load_settings()
    return {
        "state": s.get("goodreads_session_state", "unknown"),
        "since": s.get("goodreads_session_state_since"),
        "last_status": s.get("goodreads_session_last_status"),
    }


def _write_state(state: str, *, last_status: Optional[int] = None) -> None:
    """Write the three runtime-state keys back to settings.json.

    Idempotent — if the state hasn't flipped, only `last_status` is
    refreshed (avoids polluting the "since" timestamp on every probe).
    """
    s = dict(load_settings())
    flipped = s.get("goodreads_session_state") != state
    s["goodreads_session_state"] = state
    if flipped:
        s["goodreads_session_state_since"] = time.time()
    if last_status is not None:
        s["goodreads_session_last_status"] = last_status
    save_settings(s)
    if flipped:
        _log.info(
            "goodreads: session state → %s (last_status=%s)",
            state, last_status,
        )


def mark_soft_blocked(last_status: Optional[int] = None) -> None:
    """Flip the session state to soft_blocked. Called on 202 / empty 2xx."""
    _write_state("soft_blocked", last_status=last_status)


def mark_active(last_status: Optional[int] = None) -> None:
    """Flip the session state to active. Called when a probe returns 200
    with a real body, OR manually via the Settings "Mark as active"
    button after the user has investigated."""
    _write_state("active", last_status=last_status)


def is_soft_blocked() -> bool:
    """Cheap check for the dispatcher: should we skip Goodreads this pass?"""
    return get_session_state().get("state") == "soft_blocked"


# ─── HTTP session (curl_cffi chrome120 with httpx fallback) ───────────


def _create_curl_cffi_session(timeout: float):
    """Build a curl_cffi AsyncSession with Chrome 120 TLS impersonation.

    Returns None on ImportError — caller falls back to httpx. The
    fallback path exists primarily for the test environment, where
    curl_cffi isn't installed in the venv to keep the test image lean.
    Production containers (Dockerfile installs curl_cffi) always use
    the impersonating path.
    """
    try:
        from curl_cffi.requests import AsyncSession
        return AsyncSession(impersonate="chrome120", timeout=timeout)
    except ImportError:
        _log.warning(
            "goodreads_session: curl_cffi not installed — falling back "
            "to httpx (Cloudflare will likely 202 every request). "
            "Install via `pip install curl_cffi`."
        )
        return None


class GoodreadsSession:
    """Async HTTP session pre-configured for goodreads.com fetches.

    Wraps a curl_cffi AsyncSession when available, httpx.AsyncClient
    otherwise. Exposes a `get()` method that:

      - Applies rate-limit + jitter before the request
      - Detects Cloudflare soft-block on the response
      - Writes the `goodreads_session_state` runtime flag on transition
      - Returns the raw response object on success (caller parses body)

    Designed to be a long-lived singleton per process; create via
    `get_session()` at the module level rather than per-call so the
    underlying TCP+TLS session can be reused.
    """

    def __init__(
        self,
        *,
        rate_limit: float = _DEFAULT_RATE_LIMIT,
        timeout: float = 45.0,
    ):
        self.rate_limit = max(0.0, float(rate_limit))
        self.timeout = float(timeout)
        self._curl: Any = None
        self._httpx: Optional[httpx.AsyncClient] = None
        self._curl_init_attempted = False
        self._lock = asyncio.Lock()

    def _get_curl(self):
        """Lazy curl_cffi session (None if curl_cffi not installed)."""
        if self._curl is not None:
            return self._curl
        if self._curl_init_attempted:
            return None
        self._curl_init_attempted = True
        self._curl = _create_curl_cffi_session(self.timeout)
        return self._curl

    def _get_httpx(self) -> httpx.AsyncClient:
        """Lazy httpx fallback client."""
        if self._httpx is None:
            self._httpx = httpx.AsyncClient(
                timeout=self.timeout,
                headers=_DEFAULT_HEADERS,
                follow_redirects=True,
            )
        return self._httpx

    async def _sleep_with_jitter(self) -> None:
        if self.rate_limit > 0:
            jitter = random.uniform(*_JITTER_RANGE)
            await asyncio.sleep(self.rate_limit + jitter)

    async def get(self, url: str, **kwargs) -> Any:
        """Rate-limited GET with uniform soft-block detection.

        Returns the raw response object. Does NOT raise on non-200; the
        caller (parser) decides what to do with non-200 responses. We
        only side-effect the runtime-state flag on detection.

        Phase-B hook: `_build_cookie_header()` returns None today; flip
        it on later to inject cf_clearance + _session_id2 + UA from the
        encrypted store with no caller changes.
        """
        async with self._lock:
            await self._sleep_with_jitter()

        cookie_header = _build_cookie_header()
        headers = dict(kwargs.pop("headers", {}))
        if cookie_header:
            headers["Cookie"] = cookie_header

        resp = None
        curl = self._get_curl()
        if curl is not None:
            # curl_cffi accepts headers via `headers=` and shares the
            # rest of the httpx-ish kwargs interface (params, etc.).
            merged_headers = {**_DEFAULT_HEADERS, **headers}
            resp = await curl.get(url, headers=merged_headers, **kwargs)
        else:
            client = self._get_httpx()
            resp = await client.get(url, headers=headers, **kwargs)

        # Update runtime state based on the response.
        status = getattr(resp, "status_code", None)
        if is_cloudflare_soft_block(resp):
            mark_soft_blocked(last_status=status)
        elif status is not None and 200 <= status < 300:
            mark_active(last_status=status)
        else:
            # Non-2xx, non-soft-block (404, 5xx, etc.) — record the
            # status but don't flip state. A real 404 is a legitimate
            # answer; a 503 is a transient site issue, neither is a
            # cookie/fingerprint problem.
            s = dict(load_settings())
            s["goodreads_session_last_status"] = status
            save_settings(s)

        return resp

    async def close(self) -> None:
        if self._curl is not None:
            try:
                close = getattr(self._curl, "close", None)
                if close is not None:
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
            except Exception:
                pass
            self._curl = None
        if self._httpx is not None:
            try:
                await self._httpx.aclose()
            except Exception:
                pass
            self._httpx = None


# ─── Phase B inject hook (no-op in Phase A) ───────────────────────────


def _build_cookie_header() -> Optional[str]:
    """Build a Cookie request header from the encrypted store.

    Phase A: returns None unconditionally. curl_cffi's TLS fingerprint
    alone is the bypass we ship and measure.

    Phase B: when `goodreads_cf_clearance` / `goodreads_session_id2` are
    set in the encrypted store, format them as a Cookie header. Flipping
    this on requires no caller changes — every fetch goes through
    `GoodreadsSession.get()`.
    """
    return None


# ─── Module-level singleton ───────────────────────────────────────────


_SESSION: Optional[GoodreadsSession] = None
_SESSION_LOCK = asyncio.Lock()


async def get_session(rate_limit: Optional[float] = None) -> GoodreadsSession:
    """Lazy module-level singleton getter.

    `rate_limit` is the configured value from `source_rate_limits` —
    typically passed by the caller (e.g. `lookup.py` reads it once per
    scan). If omitted, falls back to the conservative 5s default. The
    first caller to set a non-None value pins the session's rate limit
    for the process lifetime; subsequent overrides are ignored.
    """
    global _SESSION
    if _SESSION is None:
        async with _SESSION_LOCK:
            if _SESSION is None:
                rl = rate_limit if rate_limit is not None else _DEFAULT_RATE_LIMIT
                _SESSION = GoodreadsSession(rate_limit=rl)
    return _SESSION


async def close_session() -> None:
    """Close the module-level session. Called on shutdown."""
    global _SESSION
    if _SESSION is not None:
        await _SESSION.close()
        _SESSION = None


def reset_session_for_tests() -> None:
    """Test hook — drop the singleton so each test gets a fresh one."""
    global _SESSION
    _SESSION = None
