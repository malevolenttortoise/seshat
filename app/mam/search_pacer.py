"""
One MAM call at a time, spaced out: the pacer behind Manual Grab.

A Manual Grab batch holds up to 30 torrents, and MAM's search API looks
them up one ID per call (`tor.id` is single-ID; ADR-0023). Without
pacing, a pasted batch would fire 30 search calls in a burst, then 30
cover fetches, then up to 30 personal-FL buys at Grab all. Every MAM
request Manual Grab makes goes through `paced()`, which serializes
them and keeps at least `gap_seconds()` between the end of one and
the start of the next.

The gap is the live `rate_mam` setting (the delay MAM scans already
use between searches), read per call, floored at 1s. Only Manual Grab
uses the pacer (D15); IRC dispatch and discovery scans keep their own
timing.
"""
from __future__ import annotations

import asyncio
import logging
import time
import weakref
from typing import Awaitable, Callable, Optional, TypeVar

from app.config import load_settings
from app.mam.torrent_info import (
    TorrentInfo,
    cached_torrent_info,
    get_torrent_info,
)
from app.mam.user_status import UserStatus, cached_user_status, get_user_status

_log = logging.getLogger("seshat.mam.search_pacer")

T = TypeVar("T")

_DEFAULT_GAP_S = 2.0
_MIN_GAP_S = 1.0

# Per event loop, like `grab_claim_lock`: an asyncio.Lock binds to the
# first loop that contends for it, and the test suite runs a fresh
# loop per test. Production has one loop, so one lock.
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)
_last_call_end: Optional[float] = None

# Seams for tests (a fake clock instead of real sleeps).
_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock


def gap_seconds() -> float:
    try:
        gap = float(load_settings().get("rate_mam", _DEFAULT_GAP_S))
    except (TypeError, ValueError):
        gap = _DEFAULT_GAP_S
    return max(_MIN_GAP_S, gap)


def reset() -> None:
    """Forget the last call (tests)."""
    global _last_call_end
    _last_call_end = None


async def paced(fn: Callable[[], Awaitable[T]], *, label: str = "") -> T:
    """Run `fn` (one MAM request) once the gap since the last one has passed."""
    global _last_call_end
    async with _lock():
        if _last_call_end is not None:
            wait = _last_call_end + gap_seconds() - _clock()
        else:
            wait = 0.0
        if wait > 0:
            _log.debug("pacer: waiting %.2fs before %s", wait, label or "MAM call")
            await _sleep(wait)
        try:
            return await fn()
        finally:
            _last_call_end = _clock()


async def paced_torrent_info(
    torrent_id: str, token: Optional[str],
) -> TorrentInfo:
    """`get_torrent_info` through the pacer; a cache hit skips it.

    Raises what `get_torrent_info` raises.
    """
    hit = cached_torrent_info(torrent_id)
    if hit is not None:
        return hit
    return await paced(
        lambda: get_torrent_info(torrent_id, token=token),
        label=f"torrent info tid={torrent_id}",
    )


async def paced_user_status(token: Optional[str]) -> UserStatus:
    """`get_user_status` through the pacer; a cache hit skips it.

    Raises what `get_user_status` raises.
    """
    hit = cached_user_status(token)
    if hit is not None:
        return hit
    return await paced(lambda: get_user_status(token=token), label="user status")
