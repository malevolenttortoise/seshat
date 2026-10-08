"""The source gate: one pacer and one counter for every request to a
metadata source (2026-10 audit wave 4, issue 15: G62, G71, G78–G81)."""
import asyncio
import time

import httpx
import pytest

from app.metadata import source_gate


class FakeClock:
    """Monotonic + wall clock that only moves when someone sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.wall = time.mktime((2026, 10, 9, 12, 0, 0, 0, 0, -1))
        self.slept: list[float] = []

    def mono(self) -> float:
        return self.now

    def walltime(self) -> float:
        return self.wall

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        self.wall += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(source_gate, "_clock", c.mono)
    monkeypatch.setattr(source_gate, "_wall", c.walltime)
    monkeypatch.setattr(source_gate, "_sleep", c.sleep)
    monkeypatch.setattr(source_gate, "_jitter", lambda lo, hi: 0.0)
    return c


@pytest.fixture
def rates(monkeypatch):
    """Metadata Sources rates as settings would hold them; edit the dict to
    change one mid-test."""
    r = {"goodreads": 30.0, "amazon": 100.0, "hardcover": 3.0, "kobo": 3.0}
    monkeypatch.setattr(
        source_gate, "load_settings",
        lambda: {"metadata_sources": {k: {"rate_limit": v} for k, v in r.items()}},
    )
    return r


async def _one(source, caller=None, *, kind="", status=200):
    async def send():
        return httpx.Response(status)
    if caller is None:
        return await source_gate.request(source, send, kind=kind)
    with source_gate.caller(caller):
        return await source_gate.request(source, send, kind=kind)


def _pending(source, caller, kind=""):
    return source_gate._pending.get((source_gate._today(), source, caller, kind)) or {}


# ─── Pacing (G71) ────────────────────────────────────────────


async def test_the_first_request_after_a_quiet_spell_goes_at_once(clock, rates):
    await _one("goodreads", "worker")
    assert clock.slept == []


async def test_any_two_requests_to_a_source_are_the_rate_apart_whoever_sends_them(clock, rates):
    await _one("goodreads", "worker")
    await _one("goodreads", "scan")
    await _one("goodreads", "enrichment")
    assert clock.slept == [30.0, 30.0]


async def test_the_rate_is_read_before_every_request(clock, rates):
    await _one("hardcover", "scan")
    rates["hardcover"] = 12.0
    await _one("hardcover", "scan")
    assert clock.slept == [12.0]


async def test_time_already_passed_counts_towards_the_gap(clock, rates):
    await _one("amazon", "worker")
    clock.now += 70.0
    await _one("amazon", "enrichment")
    assert clock.slept == [30.0]


async def test_sources_dont_wait_on_each_other(clock, rates):
    await _one("goodreads", "worker")
    await _one("hardcover", "scan")
    await _one("kobo", "scan")
    assert clock.slept == []


async def test_goodreads_keeps_its_jitter(clock, rates, monkeypatch):
    monkeypatch.setattr(source_gate, "_jitter", lambda lo, hi: hi)
    await _one("goodreads", "worker")
    await _one("goodreads", "worker")
    await _one("kobo", "scan")
    await _one("kobo", "scan")
    assert clock.slept == [31.0, 3.0]


async def test_a_zero_rate_means_no_wait(clock, rates):
    rates["kobo"] = 0.0
    await _one("kobo", "scan")
    await _one("kobo", "scan")
    assert clock.slept == []


async def test_audnexus_keeps_its_own_floor(clock, rates):
    await _one("audnexus", "enrichment")
    await _one("audnexus", "enrichment")
    assert clock.slept == [pytest.approx(0.2)]


async def test_concurrent_requests_go_one_gap_apart(clock, rates):
    """Kobo's four concurrent book fetches no longer multiply its rate
    (G72)."""
    starts: list[float] = []

    async def send():
        starts.append(clock.now)
        return httpx.Response(200)

    with source_gate.caller("scan"):
        await asyncio.gather(*(source_gate.request("kobo", send) for _ in range(4)))
    assert starts == [1000.0, 1003.0, 1006.0, 1009.0]


async def test_enrichment_goes_ahead_of_waiting_scans_and_workers(clock, rates, monkeypatch):
    """G78: a grab's enrichment waits behind at most the request in its
    gap, never behind a queue of scan or worker requests."""
    order: list[str] = []
    gap_over = asyncio.Event()

    async def held_sleep(seconds):
        await asyncio.wait_for(gap_over.wait(), 5)
        clock.now += seconds

    monkeypatch.setattr(source_gate, "_sleep", held_sleep)

    async def req(who):
        async def send():
            order.append(who)
            return httpx.Response(200)
        with source_gate.caller("enrichment" if who == "enrichment" else "scan"):
            await source_gate.request("kobo", send)

    await req("scan-1")                       # sets the last start
    tasks = [asyncio.create_task(req(f"scan-{i}")) for i in (2, 3, 4)]
    for _ in range(5):
        await asyncio.sleep(0)                # scan-2 sleeps the gap; 3, 4 queue
    tasks.append(asyncio.create_task(req("enrichment")))
    for _ in range(5):
        await asyncio.sleep(0)
    gap_over.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert order == ["scan-1", "scan-2", "enrichment", "scan-3", "scan-4"]


# ─── Who is asking (G80) ─────────────────────────────────────


async def test_the_outermost_caller_names_the_request(clock, rates):
    with source_gate.caller("backfill"):
        with source_gate.caller("resolver"):
            await _one("goodreads", kind="autocomplete")
    assert _pending("goodreads", "backfill", "autocomplete")["requests"] == 1
    assert _pending("goodreads", "resolver", "autocomplete") == {}


async def test_an_unnamed_request_counts_as_other(clock, rates):
    await _one("openlibrary")
    assert _pending("openlibrary", "other")["requests"] == 1


async def test_a_decorated_coroutine_names_its_requests(clock, rates):
    @source_gate.as_caller("worker")
    async def tick():
        await _one("amazon")

    await tick()
    assert _pending("amazon", "worker")["requests"] == 1
    assert source_gate.current_caller() == "other"


# ─── Counting (G62, G79, G81) ────────────────────────────────


@pytest.mark.parametrize("status,column", [
    (200, "ok"), (202, "ok"), (403, "blocks"), (429, "blocks"),
    (404, "errors"), (503, "errors"),
])
async def test_the_status_decides_the_outcome(clock, rates, status, column):
    await _one("google_books", "scan", status=status)
    row = _pending("google_books", "scan")
    assert row["requests"] == 1 and row[column] == 1
    assert sum(row[c] for c in ("ok", "blocks", "errors", "timeouts")) == 1


@pytest.mark.parametrize("exc,column", [
    (httpx.ReadTimeout("slow"), "timeouts"),
    (asyncio.TimeoutError(), "timeouts"),
    (RuntimeError("curl: (28) Operation timed out after 30001 ms"), "timeouts"),
    (httpx.ConnectError("refused"), "errors"),
])
async def test_a_request_that_raises_is_counted(clock, rates, exc, column):
    async def send():
        raise exc
    with source_gate.caller("scan"), pytest.raises(type(exc)):
        await source_gate.request("ibdb", send)
    assert _pending("ibdb", "scan")[column] == 1


async def test_a_request_cut_off_by_its_caller_counts_as_a_timeout(clock, rates):
    started = asyncio.Event()

    async def send():
        started.set()
        await asyncio.Event().wait()

    with source_gate.caller("scan"):
        task = asyncio.create_task(source_gate.request("kobo", send))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert _pending("kobo", "scan")["timeouts"] == 1


async def test_a_block_seen_after_the_response_moves_the_count(clock, rates, monkeypatch):
    """Amazon's captcha pages come back 200; `record_amazon_soft_block`
    marks the request that showed them."""
    from app.discovery import amazon_author_id_resolver as resolver

    monkeypatch.setattr(resolver, "_blocked_until", 0.0)
    monkeypatch.setattr(resolver, "_block_count", 0)
    monkeypatch.setattr(resolver, "_persist_block_state", lambda **_: None)
    with source_gate.caller("worker"):
        await _one("amazon")
        resolver.record_amazon_soft_block("thin body")
        resolver.record_amazon_soft_block("worker escalation")   # same request
    row = _pending("amazon", "worker")
    assert (row["requests"], row["ok"], row["blocks"]) == (1, 0, 1)


async def test_scans_count_books_and_cap_hits(clock, rates):
    with source_gate.caller("scan"):
        source_gate.count_merged("kobo", created=3, updated=2)
        source_gate.count_merged("kobo", created=1)
        source_gate.count_capped("goodreads")
    assert (_pending("kobo", "scan")["created"], _pending("kobo", "scan")["updated"]) == (4, 2)
    assert _pending("goodreads", "scan")["capped"] == 1


def test_goodreads_kinds():
    k = source_gate.goodreads_kind
    assert k("https://www.goodreads.com/book/show/241980410") == "book_page"
    assert k("https://www.goodreads.com/author/list/123?page=2") == "list_page"
    assert k("https://www.goodreads.com/book/auto_complete?format=json&q=x") == "autocomplete"
    assert k("https://www.goodreads.com/author/show/123") == "other"


# ─── Writing the counts ──────────────────────────────────────


async def _rows():
    from app.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT day, source, caller, kind, requests, ok, blocks, created "
            "FROM source_counters ORDER BY day, source, caller, kind"
        )
        return [tuple(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _clear():
    from app.database import get_db
    db = await get_db()
    try:
        await db.execute("DELETE FROM source_counters")
        await db.commit()
    finally:
        await db.close()


async def test_flush_writes_and_adds_up(clock, rates):
    await _clear()
    await _one("goodreads", "worker", kind="list_page")
    await source_gate.flush()
    await _one("goodreads", "worker", kind="list_page", status=202)
    await _one("goodreads", "worker", kind="list_page", status=429)
    with source_gate.caller("scan"):
        source_gate.count_merged("goodreads", created=2)
    await source_gate.flush()
    assert source_gate._pending == {}
    day = source_gate._today()
    assert await _rows() == [
        (day, "goodreads", "scan", "", 0, 0, 0, 2),
        (day, "goodreads", "worker", "list_page", 3, 2, 1, 0),
    ]


async def test_rows_older_than_90_days_are_pruned(clock, rates):
    await _clear()
    await _one("kobo", "scan")
    await source_gate.flush()
    clock.wall += 91 * 86400
    await _one("kobo", "scan")
    await source_gate.flush()
    rows = await _rows()
    assert len(rows) == 1 and rows[0][0] == source_gate._today()


async def test_a_failed_write_keeps_the_counts(clock, rates, monkeypatch):
    from app import database

    async def broken():
        raise RuntimeError("disk full")

    await _one("ibdb", "scan")
    monkeypatch.setattr(database, "get_db", broken)
    await source_gate.flush()
    assert _pending("ibdb", "scan")["requests"] == 1
    await _one("ibdb", "scan")
    assert _pending("ibdb", "scan")["requests"] == 2


async def test_the_panel_summary(clock, rates):
    await _clear()
    with source_gate.caller("worker"):
        await _one("goodreads", kind="list_page")
    await source_gate.flush()
    clock.wall += 86400
    await _one("goodreads", "enrichment", kind="book_page", status=202)
    with source_gate.caller("enrichment"):
        source_gate.mark_last_blocked("goodreads")
    with source_gate.caller("scan"):
        source_gate.count_merged("goodreads", created=1)
    out = await source_gate.traffic_summary(days=8)
    gr = out["sources"]["goodreads"]
    assert out["days"][0] == out["today"] and len(out["days"]) == 8
    assert (gr["today"]["requests"], gr["today"]["blocks"], gr["today"]["created"]) == (1, 1, 1)
    assert {(r["caller"], r["kind"]) for r in gr["today_rows"]} == {
        ("enrichment", "book_page"), ("scan", ""),
    }
    assert [d["requests"] for d in gr["daily"][:3]] == [1, 1, 0]


# ─── Turn waits don't eat enrichment's timeouts (G78) ────────


async def test_waiting_for_a_turn_doesnt_count_against_the_timeout(rates, monkeypatch):
    monkeypatch.setattr(source_gate, "_sleep", asyncio.sleep)
    rates["kobo"] = 0.3
    await _one("kobo", "worker")

    async def search():
        return await _one("kobo")

    resp = await source_gate.wait_for_excluding_turns(search(), timeout=0.1)
    assert resp.status_code == 200


async def test_a_slow_request_still_times_out(rates):
    async def send():
        await asyncio.sleep(5)
        return httpx.Response(200)

    with pytest.raises(asyncio.TimeoutError):
        await source_gate.wait_for_excluding_turns(
            source_gate.request("kobo", send), timeout=0.05,
        )


async def test_measured_waits_add_up(clock, rates):
    with source_gate.measure_turn_waits() as waits:
        await _one("goodreads", "enrichment")
        await _one("goodreads", "enrichment")
        await _one("goodreads", "enrichment")
    assert waits.seconds() == 60.0
