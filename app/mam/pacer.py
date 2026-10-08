"""
One MAM request at a time, spaced out: the pacer every MAM HTTP request
goes through.

`app.mam.cookie._do_get` / `_do_post` (the one MAM client, audit L1-07)
run each request through `paced()`, so a discovery scan's searches,
description lookups and cover fetches, a grab's `download.php`, the
account lookups behind auto-buy and the wedge checks, IRC dispatch's
torrent-info lookups and Manual Grab all share one queue: one request at
a time, and at least `gap_seconds()` between the end of one and the start
of the next. Until the 2026-10 audit (L1-05, L1-06, L1-12) only Manual
Grab was paced and everything else kept its own timing or none, so a scan
burst its in-between calls and two scans could run at double the rate.
Proactive Search reuses this; nothing bounds Seshat's MAM request rate
except this module, so don't add a MAM request that bypasses it.

The gap is the live `rate_mam` setting (the delay MAM scans already use
between searches), read per request and floored at 1s.

**Priority.** IRC announces wait in front of everything else
(`PRIORITY_IRC`), so a long scan never holds up an autograb by more than
the request in flight and one gap. The priority rides on a context
variable: `with priority(PRIORITY_IRC):` around the IRC dispatch, and
tasks spawned inside it (a grab held for MAM's index) inherit it.

**Re-entrant.** A `paced()` call made inside another one, in the same
task, runs straight away instead of deadlocking on the turn it already
holds. A task spawned inside a paced request queues like anyone else.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import heapq
import itertools
import logging
import time
import weakref
from typing import Awaitable, Callable, Iterator, Optional, TypeVar

from app.config import load_settings

_log = logging.getLogger("seshat.mam.pacer")

T = TypeVar("T")

_DEFAULT_GAP_S = 2.0
_MIN_GAP_S = 1.0

PRIORITY_IRC = 0
PRIORITY_NORMAL = 1

_priority: contextvars.ContextVar[int] = contextvars.ContextVar(
    "mam_pacer_priority", default=PRIORITY_NORMAL,
)
# The task holding the turn, as seen from inside its own paced request.
# A task spawned in there copies this context but is a different task,
# so it doesn't count as "inside".
_holder: contextvars.ContextVar[Optional[asyncio.Task]] = contextvars.ContextVar(
    "mam_pacer_holder", default=None,
)


class _Queue:
    """Who holds the turn, and who is waiting for it (best priority first,
    then first come)."""

    def __init__(self) -> None:
        self.busy = False
        self.waiters: list[tuple[int, int, asyncio.Future]] = []
        self.seq = itertools.count()


# Per event loop, like `grab_claim_lock`: futures bind to the loop that
# made them, and the test suite runs a fresh loop per test. Production
# has one loop, so one queue.
_queues: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Queue]" = (
    weakref.WeakKeyDictionary()
)
_last_call_end: Optional[float] = None
# Start times of recent requests, for the MAM page's per-minute count.
_recent: "collections.deque[float]" = collections.deque()

# Seams for tests (a fake clock instead of real sleeps).
_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


def _queue() -> _Queue:
    loop = asyncio.get_running_loop()
    q = _queues.get(loop)
    if q is None:
        q = _queues[loop] = _Queue()
    return q


def gap_seconds() -> float:
    try:
        gap = float(load_settings().get("rate_mam", _DEFAULT_GAP_S))
    except (TypeError, ValueError):
        gap = _DEFAULT_GAP_S
    return max(_MIN_GAP_S, gap)


def reset() -> None:
    """Forget the last request and the per-minute count (tests)."""
    global _last_call_end
    _last_call_end = None
    _recent.clear()


def requests_last_minute() -> int:
    """MAM requests started in the last 60 seconds."""
    cutoff = _clock() - 60.0
    while _recent and _recent[0] < cutoff:
        _recent.popleft()
    return len(_recent)


@contextlib.contextmanager
def priority(level: int) -> Iterator[None]:
    """Requests made inside this block (and tasks it spawns) queue at `level`."""
    token = _priority.set(level)
    try:
        yield
    finally:
        _priority.reset(token)


async def _take_turn(q: _Queue, level: int) -> None:
    if not q.busy and not q.waiters:
        q.busy = True
        return
    fut = asyncio.get_running_loop().create_future()
    heapq.heappush(q.waiters, (level, next(q.seq), fut))
    try:
        await fut
    except asyncio.CancelledError:
        # Handed the turn just as we were cancelled: pass it on, or the
        # queue stalls for good.
        if fut.done() and not fut.cancelled():
            _pass_turn(q)
        raise


def _pass_turn(q: _Queue) -> None:
    while q.waiters:
        _, _, fut = heapq.heappop(q.waiters)
        if not fut.done():
            fut.set_result(None)   # the turn moves to it; still busy
            return
    q.busy = False


async def paced(
    fn: Callable[[], Awaitable[T]],
    *,
    label: str = "",
    priority: Optional[int] = None,
) -> T:
    """Run `fn` (one MAM request) once it's this caller's turn and the gap
    since the last request has passed."""
    global _last_call_end
    me = asyncio.current_task()
    if me is not None and _holder.get() is me:
        return await fn()
    level = _priority.get() if priority is None else priority
    q = _queue()
    await _take_turn(q, level)
    holding = _holder.set(me)
    try:
        if _last_call_end is not None:
            wait = _last_call_end + gap_seconds() - _clock()
            if wait > 0:
                _log.debug("pacer: waiting %.2fs before %s", wait, label or "MAM request")
                await _sleep(wait)
        _recent.append(_clock())
        requests_last_minute()   # prunes, so the deque stays a minute long
        return await fn()
    finally:
        _last_call_end = _clock()
        _holder.reset(holding)
        _pass_turn(q)
