"""
Kobo's HTTP layer: one curl_cffi session for both Kobo sources (the
discovery scan and enrichment), and no challenge solving.

kobo.com sits behind Cloudflare. Until the 2026-10 audit both Kobo sources
used `cloudscraper`, a library built to solve Cloudflare's JavaScript
challenges: access tier 3, outside the line Seshat keeps (ADR-0025). Now
(G60) requests go out through curl_cffi with Chrome's TLS profile (tier 0),
like Goodreads and Amazon, in Kobo's turn in the source gate. A challenge
is never solved: it is counted as a block, the session is dropped (tier 1:
discard our own dirty state), and Kobo backs off for 2 minutes, then 5, 10,
30 and 60 on repeated challenges; the next success clears it. While Kobo
is backing off, `fetch()` returns None without sending.

If kobo.com challenges every plain request, Kobo stops returning results:
a drop by default, visible in the Metadata Sources counters.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from app.metadata import source_gate

_log = logging.getLogger("seshat.metadata.kobo_session")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# 2, 5, 10, 30, 60 minutes on repeated challenges (Goodreads' ladder).
BACKOFF_LADDER_S: tuple[float, ...] = (120.0, 300.0, 600.0, 1800.0, 3600.0)

# Markers of a Cloudflare challenge or block page.
_CHALLENGE_MARKERS = (
    "just a moment...", "challenge-platform", "cf-chl",
    "attention required! | cloudflare", "cf-browser-verification",
)

_session: Any = None
_backoff = {"level": 0, "until": 0.0}

# Seams for tests.
_now = time.time


def _create_session() -> Any:
    try:
        from curl_cffi.requests import AsyncSession
        return AsyncSession(impersonate="chrome120", timeout=30.0)
    except ImportError:
        _log.warning("kobo: curl_cffi not installed — Kobo returns nothing")
        return None


def _get_session() -> Any:
    global _session
    if _session is None:
        _session = _create_session()
    return _session


async def _drop_session() -> None:
    global _session
    s, _session = _session, None
    if s is not None:
        try:
            res = s.close()
            if hasattr(res, "__await__"):
                await res
        except Exception:
            pass


def is_challenge(status: Optional[int], headers: Any, body: str) -> bool:
    """A Cloudflare challenge or block: 403 / 429, `cf-mitigated:
    challenge`, or a 503 / 2xx whose page is Cloudflare's interstitial."""
    if status in (403, 429):
        return True
    try:
        if (headers.get("cf-mitigated") or "").lower() == "challenge":
            return True
    except Exception:
        pass
    head = (body or "")[:4096].lower()
    return any(m in head for m in _CHALLENGE_MARKERS)


def backoff_until() -> float:
    """When Kobo's backoff ends (epoch), or 0 when it isn't backing off."""
    return _backoff["until"] if _backoff["until"] > _now() else 0.0


def backoff_state() -> dict:
    return {"level": _backoff["level"], "until": backoff_until()}


async def _record_challenge(status: Optional[int]) -> None:
    level = min(_backoff["level"], len(BACKOFF_LADDER_S) - 1)
    wait = BACKOFF_LADDER_S[level]
    _backoff["level"] += 1
    _backoff["until"] = _now() + wait
    await _drop_session()
    _log.warning(
        "kobo: challenged by Cloudflare (status=%s) — not solving it; "
        "backing off %d min (challenge %d in a row)",
        status, int(wait // 60), _backoff["level"],
    )


def _record_success() -> None:
    if _backoff["level"] or _backoff["until"]:
        _log.info("kobo: answering again — backoff cleared")
    _backoff["level"] = 0
    _backoff["until"] = 0.0


async def fetch(url: str, *, timeout: float = 30.0) -> Optional[str]:
    """GET `url` in Kobo's turn. Returns the page on a 200, None on
    anything else (a challenge, another status, a network error, or Kobo
    backing off, in which case nothing is sent)."""
    if backoff_until():
        _log.debug("kobo: backing off — not requesting %s", url)
        return None
    session = _get_session()
    if session is None:
        return None
    try:
        async with source_gate.turn("kobo") as turn:
            resp = await session.get(url, headers=_HEADERS, timeout=timeout)
            status = getattr(resp, "status_code", None)
            text = getattr(resp, "text", "") or ""
            challenged = is_challenge(status, getattr(resp, "headers", {}), text)
            if challenged:
                turn.block()
            else:
                turn.status(status)
    except Exception as e:
        _log.debug("kobo fetch error for %s: %s", url, e)
        return None
    if challenged:
        await _record_challenge(status)
        return None
    if status == 200:
        _record_success()
        return text
    _log.debug("kobo: HTTP %s for %s", status, url)
    return None


async def close() -> None:
    await _drop_session()


def reset_for_tests() -> None:
    global _session
    _session = None
    _backoff["level"] = 0
    _backoff["until"] = 0.0
