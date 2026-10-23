"""
Centralized Goodreads HTTP plumbing: TLS impersonation, pacing, and
backing off when Goodreads' bot protection blocks us.

Goodreads sits behind AWS WAF Bot Control on CloudFront (not Cloudflare,
as this code said until the 2026-10 audit read the response headers:
`via: … cloudfront.net`, no `cf-ray`). It challenges book pages long before
list pages. Every caller that fetches goodreads.com (the metadata source,
the discovery source, the ID resolver, the author-ID backfill, the
list-page cache worker, the paste-URL importer) routes its requests
through this module so:

  - TLS fingerprint impersonation (curl_cffi chrome120) happens
    uniformly. Python's stdlib TLS fingerprint is on every bot-detection
    blocklist; curl_cffi drives libcurl-impersonate to replicate Chrome's
    handshake exactly. No challenge is ever solved (ADR-0025: access stays
    within tiers 0-1).
  - Every request waits Goodreads' turn in the source gate
    (`app.metadata.source_gate`): the Metadata Sources rate since the
    last Goodreads request from any caller, plus 0-1s of jitter, read
    per request. The gate counts each request by kind (book page, list
    page, autocomplete, other).
  - **A block backs off that kind of request only** (G66). A block (202,
    403, 429, or an empty 2xx) on a book page pauses book pages for 2
    minutes, then 5, 10, 30 and 60 on repeated blocks; the next success
    of that kind clears it. List pages and autocomplete carry on. While a
    kind is backing off, `get()` refuses it without sending
    (`GoodreadsBackingOff`), except for the Settings probe and the weekly
    canary, which always go out. This replaced a single latch that
    switched Goodreads off everywhere until a later success, often for
    days.

Runtime state in settings.json (behind `_RUNTIME_STATE_KEYS` so a PATCH
can't clobber it):

  goodreads_backoff:              {kind: {"level", "until", "last_block_at"}}
  goodreads_session_state:        "active" | "soft_blocked" | "unknown"
  goodreads_session_state_since:  unix timestamp when state last flipped
  goodreads_session_last_status:  HTTP status of the most recent response

The three `goodreads_session_*` keys keep their meaning for the overall
state: `soft_blocked` while any kind is backing off. The Settings
GoodreadsStatusCard shows each kind's backoff; "Mark as active" clears
them all.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from app.config import load_settings, save_settings
from app.metadata import source_gate

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


def is_soft_block(resp: Any) -> bool:
    """True when Goodreads' bot protection turned the request away.

    Catches three patterns, all of which mean "back off":
      - **HTTP 202**: AWS WAF's challenge response (`x-amzn-waf-action:
        challenge`, an empty or script-only body). Real browsers run the
        challenge script; we never do.
      - **HTTP 2xx + empty body**: defensive — some interstitial
        variants return 200 with a zero-length body.
      - **HTTP 403 / 429** (v2.13.2): the WAF's block and rate rules,
        seen on `auto_complete` and the `/author/list/` pages.
    """
    if resp is None:
        return False
    status = getattr(resp, "status_code", None)
    if status in (202, 403, 429):
        return True
    if status is not None and 200 <= status < 300:
        body = getattr(resp, "content", None) or b""
        if not body:
            return True
    return False


# ─── Runtime-state flag (read/write through settings.json) ────────────


def get_session_state() -> dict:
    """The overall state plus each request kind's backoff, for the
    Settings card.

    Returns {"state", "since", "last_status", "backoff"}. "state" is
    "soft_blocked" while any kind is backing off; a backoff that has run
    out reads as "active" (the next request decides). "unknown" on a
    fresh install (no request yet). "backoff" maps each kind to
    {"level", "until", "last_block_at", "backing_off"}.
    """
    s = load_settings()
    backoff = get_backoff()
    stored = s.get("goodreads_session_state", "unknown")
    if any(b["backing_off"] for b in backoff.values()):
        state = "soft_blocked"
    elif stored == "soft_blocked":
        state = "active"
    else:
        state = stored
    return {
        "state": state,
        "since": s.get("goodreads_session_state_since"),
        "last_status": s.get("goodreads_session_last_status"),
        "backoff": backoff,
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
    """Flip the overall state to soft_blocked."""
    _write_state("soft_blocked", last_status=last_status)


def mark_active(last_status: Optional[int] = None) -> None:
    """Flip the overall state to active."""
    _write_state("active", last_status=last_status)


def clear_all() -> None:
    """The Settings "Mark as active" button: end every backoff and flip
    the overall state to active."""
    s = dict(load_settings())
    s[_BACKOFF_KEY] = {}
    save_settings(s)
    _write_state("active")


def is_soft_blocked() -> bool:
    """True while any kind of Goodreads request is backing off."""
    return any(b["backing_off"] for b in get_backoff().values())


# ─── Backoff per request kind (G66) ───────────────────────────────────

_BACKOFF_KEY = "goodreads_backoff"
# 2, 5, 10, 30, 60 minutes on repeated blocks of one kind.
BACKOFF_LADDER_S: tuple[float, ...] = (120.0, 300.0, 600.0, 1800.0, 3600.0)
BACKOFF_KINDS: tuple[str, ...] = (
    source_gate.KIND_BOOK_PAGE, source_gate.KIND_LIST_PAGE,
    source_gate.KIND_AUTOCOMPLETE, source_gate.KIND_OTHER,
)
KIND_LABELS = {
    source_gate.KIND_BOOK_PAGE: "book pages",
    source_gate.KIND_LIST_PAGE: "list pages",
    source_gate.KIND_AUTOCOMPLETE: "autocomplete",
    source_gate.KIND_OTHER: "other pages",
}

# Seam for tests.
_now = time.time


class GoodreadsBackingOff(Exception):
    """A request of a kind that is backing off, refused without sending."""

    def __init__(self, kind: str, until: float) -> None:
        self.kind = kind
        self.until = until
        super().__init__(
            f"Goodreads {KIND_LABELS.get(kind, kind)} backing off until "
            f"{time.strftime('%H:%M', time.localtime(until))}"
        )


def get_backoff() -> dict[str, dict]:
    """Each kind's backoff: level (blocks in a row), until (epoch, 0 =
    none), last_block_at, and whether it is backing off right now."""
    raw = load_settings().get(_BACKOFF_KEY) or {}
    now = _now()
    out: dict[str, dict] = {}
    for kind in BACKOFF_KINDS:
        b = raw.get(kind) or {}
        until = float(b.get("until") or 0.0)
        out[kind] = {
            "level": int(b.get("level") or 0),
            "until": until,
            "last_block_at": b.get("last_block_at"),
            "backing_off": until > now,
        }
    return out


def backoff_until(kind: str) -> float:
    """When `kind`'s backoff ends (epoch), or 0 when it isn't backing off."""
    b = get_backoff().get(kind) or {}
    return b["until"] if b.get("backing_off") else 0.0


def is_backing_off(kind: str) -> bool:
    return backoff_until(kind) > 0.0


def _save_backoff(kind: str, entry: Optional[dict]) -> None:
    s = dict(load_settings())
    raw = dict(s.get(_BACKOFF_KEY) or {})
    if entry is None:
        raw.pop(kind, None)
    else:
        raw[kind] = entry
    s[_BACKOFF_KEY] = raw
    save_settings(s)


def _record_block(kind: str, status: Optional[int]) -> None:
    """A block on `kind`: back it off one step further up the ladder."""
    b = get_backoff()[kind]
    level = min(b["level"], len(BACKOFF_LADDER_S) - 1)
    wait = BACKOFF_LADDER_S[level]
    now = _now()
    _save_backoff(kind, {
        "level": b["level"] + 1, "until": now + wait, "last_block_at": now,
    })
    _log.warning(
        "goodreads: %s blocked by AWS WAF (status=%s) — backing off %d min "
        "(block %d in a row); other request kinds carry on",
        KIND_LABELS.get(kind, kind), status, int(wait // 60), b["level"] + 1,
    )
    mark_soft_blocked(last_status=status)


def _record_success(kind: str, status: Optional[int]) -> None:
    """A success on `kind` ends its backoff; the overall state goes back
    to active once no kind is backing off."""
    b = get_backoff()[kind]
    if b["level"] or b["until"]:
        _save_backoff(kind, None)
        _log.info(
            "goodreads: %s answering again — backoff cleared",
            KIND_LABELS.get(kind, kind),
        )
    if is_soft_blocked():
        s = dict(load_settings())
        s["goodreads_session_last_status"] = status
        save_settings(s)
    else:
        mark_active(last_status=status)


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
            "to httpx (Goodreads' bot protection will likely 202 every request). "
            "Install via `pip install curl_cffi`."
        )
        return None


class GoodreadsSession:
    """Async HTTP session pre-configured for goodreads.com fetches.

    Wraps a curl_cffi AsyncSession when available, httpx.AsyncClient
    otherwise. Exposes a `get()` method that:

      - Refuses a request whose kind is backing off (`GoodreadsBackingOff`)
      - Waits Goodreads' turn in the source gate, which counts the request
      - Detects a bot-protection block on the response and backs that
        kind off; a success clears its backoff
      - Returns the raw response object (caller parses body)

    Designed to be a long-lived singleton per process; create via
    `get_session()` at the module level rather than per-call so the
    underlying TCP+TLS session can be reused.
    """

    def __init__(self, *, timeout: float = 45.0):
        self.timeout = float(timeout)
        self._curl: Any = None
        self._httpx: Optional[httpx.AsyncClient] = None
        self._curl_init_attempted = False

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

    async def get(self, url: str, *, ignore_backoff: bool = False, **kwargs) -> Any:
        """GET in Goodreads' turn, backing its kind off on a block.

        Returns the raw response object. Does NOT raise on non-200; the
        caller (parser) decides what to do with non-200 responses.
        Raises `GoodreadsBackingOff` without sending when this kind of
        request is backing off, unless `ignore_backoff` (the Settings
        probe and the canary, which exist to test whether it's over).
        """
        kind = source_gate.goodreads_kind(url)
        if not ignore_backoff:
            until = backoff_until(kind)
            if until:
                raise GoodreadsBackingOff(kind, until)
        headers = dict(kwargs.pop("headers", {}))

        resp = None
        async with source_gate.turn("goodreads", kind=kind) as turn:
            curl = self._get_curl()
            if curl is not None:
                # curl_cffi accepts headers via `headers=` and shares the
                # rest of the httpx-ish kwargs interface (params, etc.).
                merged_headers = {**_DEFAULT_HEADERS, **headers}
                resp = await curl.get(url, headers=merged_headers, **kwargs)
            else:
                client = self._get_httpx()
                resp = await client.get(url, headers=headers, **kwargs)
            if is_soft_block(resp):
                turn.block()
            else:
                turn.status(getattr(resp, "status_code", None))

        # Update the backoff and runtime state from the response.
        status = getattr(resp, "status_code", None)
        if is_soft_block(resp):
            _record_block(kind, status)
        elif status is not None and 200 <= status < 300:
            _record_success(kind, status)
        else:
            # Non-2xx, non-block (404, 5xx, etc.) — record the status
            # but don't back off. A real 404 is a legitimate answer; a
            # 503 is a transient site issue.
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


# ─── Module-level singleton ───────────────────────────────────────────


_SESSION: Optional[GoodreadsSession] = None
_SESSION_LOCK = asyncio.Lock()


async def get_session() -> GoodreadsSession:
    """Lazy module-level singleton getter.

    Pacing isn't the session's job: every request waits Goodreads' turn
    in the source gate at the Metadata Sources rate, read per request.
    (Until the 2026-10 audit the session slept its own rate, pinned by
    whichever caller came first; once that was the cache reader's 0.0
    and Goodreads went out back to back, issue 12 / G50.)
    """
    global _SESSION
    if _SESSION is None:
        async with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = GoodreadsSession()
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
