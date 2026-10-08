"""
One gate per metadata source: the pacer and the counter every request to
Goodreads, Amazon, Hardcover, Kobo, Google Books, OpenLibrary, IBDB or
Audible goes through.

**Pacing (G71).** A source's Metadata Sources rate is the minimum gap
between the starts of any two requests to that source, whoever sends
them: a discovery scan, a cache worker, enrichment, the Goodreads ID
resolver, the author-ID backfill, the Settings probe, URL import. The
rate is read from settings before every request, so a change applies at
once. Until the 2026-10 audit each caller kept its own pacing or none:
the enricher built its sources with constructor defaults (Amazon's 100s
and Hardcover's rate were read by nothing), Hardcover's discovery POSTs
skipped the sleep, and the resolver's autocomplete calls had their own
unpaced client. A request after a quiet spell goes at once; Goodreads
keeps the 0-1s jitter its session always added.

**Queue.** Requests wait for their turn one at a time per source. A grab's
enrichment, the Settings probe and URL import go ahead of scans and the
cache workers (G78), the way IRC goes first in the MAM pacer
(`app.mam.pacer`). The turn is held only while waiting, never during the
request, so a request made inside another one never deadlocks.

**Who is asking.** `caller()` names the work a request belongs to. The
outermost name wins (G80): the backfill's resolver requests count as
`backfill`, a scan's as `scan`; `resolver` only when nothing above it
named a caller.

**Counting (G62, G79, G81).** Every turn counts one request and one
outcome (ok / blocked / error / timeout) per local day × source × caller
× kind; `kind` splits Goodreads into book pages, list pages and
autocomplete. Scans add the books they created and updated and the times
a source hit its scan time cap. Counts collect in memory and
`flush_loop()` writes them to `source_counters` in the app DB every
minute (and at shutdown, and before the panel reads them); rows older
than 90 days are pruned. A hard crash loses at most a minute.

**Turn waits don't eat enrichment's timeouts (G78).** Enrichment gives
each source 15s and each book 60s; a Goodreads turn can be 30s away and an
Amazon one 100s. `wait_for_excluding_turns()` is `asyncio.wait_for` with
the time spent waiting for turns added back, and `measure_turn_waits()`
does the same for a longer budget.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import heapq
import itertools
import logging
import random
import time
import weakref
from typing import (
    Any, AsyncIterator, Awaitable, Callable, Iterator, Optional, TypeVar,
)

import httpx

from app.config import load_settings

_log = logging.getLogger("seshat.metadata.source_gate")

T = TypeVar("T")

# ─── Callers ─────────────────────────────────────────────────

CALLER_SCAN = "scan"
CALLER_WORKER = "worker"
CALLER_ENRICHMENT = "enrichment"
CALLER_RESOLVER = "resolver"
CALLER_BACKFILL = "backfill"
CALLER_PROBE = "probe"
CALLER_URL_IMPORT = "url_import"
CALLER_OTHER = "other"

# Someone is waiting on these: they queue ahead of scans and workers.
_FIRST_IN_LINE = frozenset({CALLER_ENRICHMENT, CALLER_PROBE, CALLER_URL_IMPORT})
_PRIORITY_FIRST = 0
_PRIORITY_NORMAL = 1

# ─── Goodreads request kinds (G79; S3's backoff uses the same) ──

KIND_BOOK_PAGE = "book_page"
KIND_LIST_PAGE = "list_page"
KIND_AUTOCOMPLETE = "autocomplete"
KIND_OTHER = "other"


def goodreads_kind(url: str) -> str:
    """The kind of a goodreads.com request, from its URL."""
    u = url or ""
    if "/book/auto_complete" in u:
        return KIND_AUTOCOMPLETE
    if "/author/list/" in u:
        return KIND_LIST_PAGE
    if "/book/show/" in u:
        return KIND_BOOK_PAGE
    return KIND_OTHER


_caller: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "source_gate_caller", default=None,
)


@contextlib.contextmanager
def caller(name: str) -> Iterator[None]:
    """Requests made inside this block (and tasks it spawns) count under
    `name`, unless an enclosing block already named a caller."""
    if _caller.get() is not None:
        yield
        return
    token = _caller.set(name)
    try:
        yield
    finally:
        _caller.reset(token)


def current_caller() -> str:
    return _caller.get() or CALLER_OTHER


def as_caller(name: str) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator: the coroutine function's requests count under `name`
    (outermost wins, as with `caller()`)."""
    def deco(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            with caller(name):
                return await fn(*args, **kwargs)
        return wrapper
    return deco


# ─── Turn waits (G78) ────────────────────────────────────────


class TurnWaits:
    """How long a piece of work has spent waiting for source turns.

    Overlapping waits (Kobo's concurrent book fetches) count once."""

    def __init__(self) -> None:
        self._waiting = 0
        self._since = 0.0
        self._done = 0.0

    def _start(self, now: float) -> None:
        if self._waiting == 0:
            self._since = now
        self._waiting += 1

    def _stop(self, now: float) -> None:
        self._waiting -= 1
        if self._waiting == 0:
            self._done += now - self._since

    def seconds(self) -> float:
        if self._waiting:
            return self._done + (_clock() - self._since)
        return self._done


_sinks: contextvars.ContextVar[tuple[TurnWaits, ...]] = contextvars.ContextVar(
    "source_gate_turn_waits", default=(),
)


@contextlib.contextmanager
def measure_turn_waits() -> Iterator[TurnWaits]:
    """Time spent waiting for turns inside this block (and tasks it
    spawns) adds up in the yielded `TurnWaits`."""
    sink = TurnWaits()
    token = _sinks.set(_sinks.get() + (sink,))
    try:
        yield sink
    finally:
        _sinks.reset(token)


async def wait_for_excluding_turns(aw: Awaitable[T], timeout: float) -> T:
    """`asyncio.wait_for`, except that time spent waiting for a source's
    turn doesn't count towards `timeout`. Raises `asyncio.TimeoutError`."""
    with measure_turn_waits() as waits:
        task = asyncio.ensure_future(aw)   # the task keeps this context
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            remaining = deadline + waits.seconds() - loop.time()
            if remaining <= 0:
                break
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if done:
                return task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise
    task.cancel()
    await asyncio.wait({task})
    if not task.cancelled() and task.exception() is None:
        return task.result()   # finished as it was being cut off
    raise asyncio.TimeoutError()


# ─── The queue ───────────────────────────────────────────────


class _Queue:
    """Who is waiting for a source's turn (best priority first, then first
    come). `busy` while one of them is sleeping out the gap."""

    def __init__(self) -> None:
        self.busy = False
        self.waiters: list[tuple[int, int, asyncio.Future]] = []
        self.seq = itertools.count()


# Per event loop, like the MAM pacer: futures bind to the loop that made
# them, and the test suite runs a fresh loop per test.
_queues: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _Queue]]" = (
    weakref.WeakKeyDictionary()
)
# When the last request to each source started (monotonic clock).
_last_start: dict[str, float] = {}

# Goodreads' session always added up to 1s on top of its rate.
_JITTER_S: dict[str, tuple[float, float]] = {"goodreads": (0.0, 1.0)}

# Seams for tests.
_clock: Callable[[], float] = time.monotonic
_wall: Callable[[], float] = time.time
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
_jitter: Callable[[float, float], float] = random.uniform


def _queue(source: str) -> _Queue:
    loop = asyncio.get_running_loop()
    per_loop = _queues.get(loop)
    if per_loop is None:
        per_loop = _queues[loop] = {}
    q = per_loop.get(source)
    if q is None:
        q = per_loop[source] = _Queue()
    return q


# Gated hosts with no Metadata Sources entry: Audnexus hydrates Audible's
# hits (deliberately not listed, `source_config.KNOWN_SOURCES`) and keeps
# its own 0.2s floor.
_UNLISTED_GAP_S: dict[str, float] = {"audnexus": 0.2}


def gap_seconds(source: str) -> float:
    """The source's Metadata Sources rate (seconds), read now."""
    from app.metadata.source_config import KNOWN_SOURCES, get_source_rate_limit
    if source not in KNOWN_SOURCES:
        return _UNLISTED_GAP_S.get(source, 0.0)
    try:
        rate = get_source_rate_limit(load_settings(), source)
    except Exception:
        return 0.0
    return max(0.0, rate)


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


async def _wait_turn(source: str, level: int) -> None:
    sinks = _sinks.get()
    now = _clock()
    for s in sinks:
        s._start(now)
    try:
        q = _queue(source)
        await _take_turn(q, level)
        try:
            last = _last_start.get(source)
            if last is not None:
                gap = gap_seconds(source)
                lo, hi = _JITTER_S.get(source, (0.0, 0.0))
                if gap > 0 and hi > 0:
                    gap += _jitter(lo, hi)
                wait = last + gap - _clock()
                if wait > 0:
                    _log.debug(
                        "source gate: %s waits %.1fs (%s)",
                        source, wait, current_caller(),
                    )
                    await _sleep(wait)
            _last_start[source] = _clock()
        finally:
            _pass_turn(q)
    finally:
        now = _clock()
        for s in sinks:
            s._stop(now)


# ─── One request ─────────────────────────────────────────────

OK = "ok"
BLOCKS = "blocks"
ERRORS = "errors"
TIMEOUTS = "timeouts"


def outcome_for_status(status: Optional[int]) -> str:
    """2xx = ok; 403 / 429 = blocked (a bot gate or a quota); anything else
    is an error. Sources that see blocks in a 2xx (Goodreads' 202, Amazon's
    captcha pages) say so with `Turn.block()`."""
    if status is None:
        return ERRORS
    if 200 <= status < 300:
        return OK
    if status in (403, 429):
        return BLOCKS
    return ERRORS


def _outcome_for_exception(e: BaseException) -> str:
    if isinstance(e, (asyncio.CancelledError, TimeoutError, httpx.TimeoutException)):
        return TIMEOUTS
    if "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower():
        return TIMEOUTS   # curl_cffi / requests timeouts
    if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
        return outcome_for_status(e.response.status_code)
    return ERRORS


class Turn:
    """The request a `turn()` block sends. Say what came back with
    `status()`, `block()`, `ok()` or `error()`; a block that raises counts
    as a timeout or an error; one that says nothing counts as ok."""

    def __init__(self, source: str, who: str, kind: str) -> None:
        self.source = source
        self.caller = who
        self.kind = kind
        self.outcome: Optional[str] = None
        self._counted: Optional[str] = None

    def status(self, code: Optional[int]) -> None:
        self.outcome = outcome_for_status(code)

    def ok(self) -> None:
        self.outcome = OK

    def block(self) -> None:
        self.outcome = BLOCKS

    def error(self) -> None:
        self.outcome = ERRORS


# The last request this task made, so a block noticed after the response
# was read (Amazon's captcha pages) can still be counted as one.
_last_turn: contextvars.ContextVar[Optional[Turn]] = contextvars.ContextVar(
    "source_gate_last_turn", default=None,
)


@contextlib.asynccontextmanager
async def turn(source: str, *, kind: str = "") -> AsyncIterator[Turn]:
    """Wait for `source`'s turn, then run the block (one request) and count
    it. `kind` splits Goodreads' counts (`goodreads_kind`)."""
    who = current_caller()
    level = _PRIORITY_FIRST if who in _FIRST_IN_LINE else _PRIORITY_NORMAL
    await _wait_turn(source, level)
    t = Turn(source, who, kind)
    _last_turn.set(t)
    _bump(source, who, kind, "requests")
    try:
        yield t
    except BaseException as e:
        if t.outcome is None:
            t.outcome = _outcome_for_exception(e)
        raise
    finally:
        t._counted = t.outcome or OK
        _bump(source, who, kind, t._counted)


def mark_last_blocked(source: str) -> None:
    """Count this task's last request to `source` as blocked: for blocks a
    source recognises after reading the response (`record_amazon_soft_block`)."""
    t = _last_turn.get()
    if t is None or t.source != source:
        return
    if t._counted is None:
        t.outcome = BLOCKS          # still in its block
    elif t._counted != BLOCKS:
        _bump(t.source, t.caller, t.kind, t._counted, -1)
        _bump(t.source, t.caller, t.kind, BLOCKS)
        t._counted = BLOCKS


async def request(
    source: str, send: Callable[[], Awaitable[Any]], *, kind: str = "",
) -> Any:
    """`send()` one request in `source`'s turn; its HTTP status decides the
    count. For call sites that need nothing more than that."""
    async with turn(source, kind=kind) as t:
        resp = await send()
        t.status(getattr(resp, "status_code", None))
        return resp


# ─── Counters ────────────────────────────────────────────────

COLUMNS = (
    "requests", OK, BLOCKS, ERRORS, TIMEOUTS, "created", "updated", "capped",
)
RETENTION_DAYS = 90
FLUSH_INTERVAL_S = 60.0

# (day, source, caller, kind) -> column -> count, not yet written.
_pending: dict[tuple[str, str, str, str], dict[str, int]] = {}
_last_prune_day: Optional[str] = None


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime(_wall()))


def _bump(source: str, who: str, kind: str, column: str, n: int = 1) -> None:
    if n == 0:
        return
    key = (_today(), source, who, kind)
    row = _pending.get(key)
    if row is None:
        row = _pending[key] = dict.fromkeys(COLUMNS, 0)
    row[column] += n


def count_merged(source: str, *, created: int = 0, updated: int = 0) -> None:
    """Books a scan created and updated from `source`'s result."""
    who = current_caller()
    _bump(source, who, "", "created", created)
    _bump(source, who, "", "updated", updated)


def count_capped(source: str) -> None:
    """`source` hit its scan time cap before it finished (G81)."""
    _bump(source, current_caller(), "", "capped")


async def flush() -> None:
    """Write the counts collected so far to `source_counters`. On a write
    failure they go back in the pile for the next flush."""
    global _pending, _last_prune_day
    if not _pending:
        return
    batch, _pending = _pending, {}
    from app.database import get_db
    cols = ", ".join(COLUMNS)
    marks = ", ".join("?" for _ in COLUMNS)
    adds = ", ".join(f"{c} = {c} + excluded.{c}" for c in COLUMNS)
    rows = [(*key, *(counts[c] for c in COLUMNS)) for key, counts in batch.items()]
    today = _today()
    try:
        db = await get_db()
        try:
            await db.executemany(
                f"INSERT INTO source_counters (day, source, caller, kind, {cols}) "
                f"VALUES (?, ?, ?, ?, {marks}) "
                f"ON CONFLICT (day, source, caller, kind) DO UPDATE SET {adds}",
                rows,
            )
            if _last_prune_day != today:
                cutoff = time.strftime(
                    "%Y-%m-%d",
                    time.localtime(_wall() - RETENTION_DAYS * 86400),
                )
                await db.execute(
                    "DELETE FROM source_counters WHERE day < ?", (cutoff,),
                )
            await db.commit()
        finally:
            await db.close()
        _last_prune_day = today
    except Exception:
        for key, counts in batch.items():
            row = _pending.get(key)
            if row is None:
                _pending[key] = counts
            else:
                for c in COLUMNS:
                    row[c] += counts[c]
        _log.warning("source gate: couldn't write the source counters", exc_info=True)


async def flush_loop() -> None:
    """Write the counts every minute (a supervised lifespan task)."""
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_S)
        await flush()


async def read_counters(days: int = 8) -> dict[str, Any]:
    """Every count from the last `days` local days (today included), after
    writing what's pending: `{"today": day, "rows": [{day, source, caller,
    kind, <COLUMNS>...}]}`."""
    from app.database import get_db
    await flush()
    since = time.strftime(
        "%Y-%m-%d", time.localtime(_wall() - (max(1, days) - 1) * 86400),
    )
    db = await get_db()
    try:
        cur = await db.execute(
            f"SELECT day, source, caller, kind, {', '.join(COLUMNS)} "
            f"FROM source_counters WHERE day >= ? "
            f"ORDER BY day DESC, source, caller, kind",
            (since,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    return {"today": _today(), "rows": rows}


async def traffic_summary(days: int = 8) -> dict[str, Any]:
    """The Metadata Sources panel's view: per source, today's counts by
    caller (and Goodreads request kind) and the daily totals for the last
    `days` days, today first. Days with no requests are listed as zeros."""
    data = await read_counters(days)
    day_list = [
        time.strftime("%Y-%m-%d", time.localtime(_wall() - i * 86400))
        for i in range(max(1, days))
    ]
    today = data["today"]
    sources: dict[str, Any] = {}
    for r in data["rows"]:
        src = sources.setdefault(r["source"], {
            "today": dict.fromkeys(COLUMNS, 0),
            "today_rows": [],
            "daily": {d: dict.fromkeys(COLUMNS, 0) for d in day_list},
        })
        counts = {c: int(r[c] or 0) for c in COLUMNS}
        daily = src["daily"].get(r["day"])
        if daily is not None:
            for c in COLUMNS:
                daily[c] += counts[c]
        if r["day"] == today:
            for c in COLUMNS:
                src["today"][c] += counts[c]
            src["today_rows"].append(
                {"caller": r["caller"], "kind": r["kind"], **counts},
            )
    for src in sources.values():
        src["daily"] = [{"day": d, **src["daily"][d]} for d in day_list]
    return {"today": today, "days": day_list, "sources": sources}


def reset() -> None:
    """Forget every turn and uncounted request (tests)."""
    global _last_prune_day
    _last_start.clear()
    _pending.clear()
    _last_prune_day = None
