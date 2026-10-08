"""The one MAM pacer (audit issue 05: L1-05, L1-06, L1-12).

Every MAM HTTP request goes through `app.mam.pacer.paced` from inside
`app.mam.cookie._do_get` / `_do_post`: one at a time, `rate_mam` apart
(floored at 1s), IRC announces ahead of everything else. A fake clock
stands in for real sleeps.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.mam import cookie, pacer


@pytest.fixture
def clock(monkeypatch):
    """Sleeping advances the clock instead of waiting."""
    box = {"now": 1000.0, "sleeps": [], "starts": []}

    async def fake_sleep(seconds):
        box["sleeps"].append(seconds)
        box["now"] += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(pacer, "_clock", lambda: box["now"])
    monkeypatch.setattr(pacer, "_sleep", fake_sleep)
    monkeypatch.setattr(pacer, "gap_seconds", lambda: 2.0)
    return box


def _gaps(times: list[float]) -> list[float]:
    return [b - a for a, b in zip(times, times[1:])]


class TestSpacing:
    async def test_spaces_calls_by_the_gap(self, clock):
        async def call():
            clock["starts"].append(clock["now"])

        for _ in range(4):
            await pacer.paced(call)
        assert _gaps(clock["starts"]) == [2.0, 2.0, 2.0]

    async def test_concurrent_callers_are_serialized(self, clock):
        running = {"now": 0, "max": 0}

        async def call():
            clock["starts"].append(clock["now"])
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            await asyncio.sleep(0)
            running["now"] -= 1

        await asyncio.gather(*(pacer.paced(call) for _ in range(5)))
        assert running["max"] == 1
        assert all(g >= 2.0 for g in _gaps(sorted(clock["starts"])))

    def test_gap_is_rate_mam_floored_at_one_second(self, monkeypatch):
        monkeypatch.setattr(pacer, "load_settings", lambda: {"rate_mam": 0})
        assert pacer.gap_seconds() == 1.0
        monkeypatch.setattr(pacer, "load_settings", lambda: {"rate_mam": 3})
        assert pacer.gap_seconds() == 3.0
        monkeypatch.setattr(pacer, "load_settings", lambda: {})
        assert pacer.gap_seconds() == 2.0
        monkeypatch.setattr(pacer, "load_settings", lambda: {"rate_mam": "junk"})
        assert pacer.gap_seconds() == 2.0


class TestPriority:
    async def test_irc_jumps_the_queue(self, clock):
        """Three scan requests queued, then an IRC one: it goes next."""
        order: list[str] = []
        first_running = asyncio.Event()
        release_first = asyncio.Event()

        async def first():
            order.append("scan-0")
            first_running.set()
            await release_first.wait()

        def call(name):
            async def _():
                order.append(name)
            return _

        holder = asyncio.create_task(pacer.paced(first))
        await first_running.wait()
        scans = [asyncio.create_task(pacer.paced(call(f"scan-{i}"))) for i in (1, 2, 3)]
        await asyncio.sleep(0)

        async def irc():
            with pacer.priority(pacer.PRIORITY_IRC):
                await pacer.paced(call("irc"))

        irc_task = asyncio.create_task(irc())
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(holder, *scans, irc_task)
        assert order == ["scan-0", "irc", "scan-1", "scan-2", "scan-3"]

    async def test_spawned_task_inherits_irc_priority(self):
        seen: list[int] = []

        async def held_grab():
            seen.append(pacer._priority.get())

        with pacer.priority(pacer.PRIORITY_IRC):
            task = asyncio.create_task(held_grab())
        await task
        assert seen == [pacer.PRIORITY_IRC]
        assert pacer._priority.get() == pacer.PRIORITY_NORMAL


class TestRobustness:
    async def test_nested_call_runs_without_deadlock(self, clock):
        async def inner():
            return "inner"

        async def outer():
            return await pacer.paced(inner)

        assert await asyncio.wait_for(pacer.paced(outer), timeout=1) == "inner"

    async def test_cancelled_waiter_does_not_stall_the_queue(self, clock):
        hold = asyncio.Event()
        done: list[str] = []

        async def first():
            await hold.wait()

        async def call():
            done.append("ran")

        holder = asyncio.create_task(pacer.paced(first))
        await asyncio.sleep(0)
        doomed = asyncio.create_task(pacer.paced(call))
        survivor = asyncio.create_task(pacer.paced(call))
        await asyncio.sleep(0)
        doomed.cancel()
        hold.set()
        await asyncio.wait_for(asyncio.gather(holder, survivor), timeout=1)
        assert doomed.cancelled()
        assert done == ["ran"]

    async def test_task_spawned_inside_a_request_still_waits_its_turn(self, clock):
        """Re-entrancy is per task: a background task started from inside a
        paced request (it inherits the context) must not skip the queue."""
        spawned: list[asyncio.Task] = []

        async def child_request():
            clock["starts"].append(("child", clock["now"]))

        async def parent_request():
            clock["starts"].append(("parent", clock["now"]))
            spawned.append(asyncio.create_task(pacer.paced(child_request)))
            await asyncio.sleep(0)

        await pacer.paced(parent_request)
        await spawned[0]
        (p_name, p_at), (c_name, c_at) = clock["starts"]
        assert (p_name, c_name) == ("parent", "child")
        assert c_at - p_at >= 2.0

    async def test_waiter_cancelled_just_after_being_handed_the_turn(self):
        """The turn moved to a waiter that was cancelled before it ran:
        it must pass the turn on, or the queue stays busy for good."""
        q = pacer._Queue()
        q.busy = True
        waiter = asyncio.create_task(pacer._take_turn(q, pacer.PRIORITY_NORMAL))
        await asyncio.sleep(0)            # queued
        pacer._pass_turn(q)               # handed the turn...
        waiter.cancel()                   # ...and cancelled before it resumed
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert q.busy is False

    async def test_a_failing_request_passes_the_turn_on(self, clock):
        async def boom():
            raise RuntimeError("MAM down")

        async def ok():
            return "ok"

        with pytest.raises(RuntimeError):
            await pacer.paced(boom)
        assert await asyncio.wait_for(pacer.paced(ok), timeout=1) == "ok"


class TestCounter:
    async def test_counts_requests_in_the_last_minute(self, clock):
        async def call():
            pass

        for _ in range(3):
            await pacer.paced(call)
        assert pacer.requests_last_minute() == 3
        clock["now"] += 61
        assert pacer.requests_last_minute() == 0

    async def test_status_endpoint_reports_it(self, clock, monkeypatch):
        from app.routers import mam as mam_router

        async def call():
            pass

        await pacer.paced(call)
        await pacer.paced(call)

        async def no_token():
            return ""

        monkeypatch.setattr(mam_router.mam_cookie, "get_active_token", no_token)
        resp = await mam_router._build_status()
        assert resp.requests_last_minute == 2


class TestHttpLayerIsPaced:
    """The real client wrappers wait their turn: every MAM request, any
    kind, any caller."""

    @pytest.fixture
    def mam(self, monkeypatch, clock):
        def handler(request: httpx.Request) -> httpx.Response:
            clock["starts"].append(clock["now"])
            return httpx.Response(200, text="{}")

        monkeypatch.setattr(
            cookie, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return clock["starts"]

    async def test_gets_and_posts_share_one_queue(self, mam):
        await cookie._do_post(cookie.MAM_SEARCH_URL, token="t", payload="{}")
        await cookie._do_get("https://www.myanonamouse.net/jsonLoad.php", token="t")
        await cookie._do_get(
            "https://cdn.myanonamouse.net/t/p/1700000000/large/1.jpeg", token="t",
        )
        assert len(mam) == 3
        assert _gaps(mam) == [2.0, 2.0]

    async def test_concurrent_callers_never_overlap(self, mam):
        await asyncio.gather(*(
            cookie._do_post(cookie.MAM_SEARCH_URL, token="t", payload="{}")
            for _ in range(4)
        ))
        assert len(mam) == 4
        assert all(g >= 2.0 for g in _gaps(sorted(mam)))
