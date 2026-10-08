"""Grabs waiting for MAM's index survive a restart (audit issue 26, L2-10; G26).

An allowed IRC announce MAM's search doesn't list yet waits up to 10
minutes for it (D37, `23f7c04`). The wait was an in-memory task only, so
a restart dropped the grab. It's now also a `pending_holds` row (kind
'index_wait', the announce as JSON); after a restart the hold-release
tick resumes it on the rest of the schedule.

A restart is simulated by leaving the first task blocked mid-wait
(as if its process had died) and moving `_process_started_at` past the
row's creation. Safety: `fetch_torrent` is called exactly once.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app.database import get_db
from app.filter.gate import Announce
from app.orchestrator import dispatch, hold_release
from app.orchestrator.dispatch import handle_announce
from app.storage import holds as holds_storage
from tests.orchestrator.test_dispatch import _make_announce
from tests.orchestrator.test_manual_grab import TID, _item, mam_search  # noqa: F401
from tests.orchestrator.test_wedge_guard import (  # noqa: F401 (fixtures)
    _irc_deps, _recording, mam_index, wedges_in_hand,
)


@pytest.fixture(autouse=True)
def _fresh_resume_state():
    dispatch._resumed_hold_ids.clear()
    yield
    dispatch._resumed_hold_ids.clear()


async def _holds() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, kind, state, resolution_reason, payload, announce_id "
            "FROM pending_holds ORDER BY id"
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _age_holds(seconds: int) -> None:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE pending_holds SET created_at = datetime('now', ?)",
            (f"-{seconds} seconds",),
        )
        await db.commit()
    finally:
        await db.close()


@pytest.fixture
async def died_mid_wait(monkeypatch, mam_index):
    """Hold an announce, then 'kill' its process: the task is left blocked
    in its first poll and forgotten, and the process start moves past the
    hold row, so it reads as left over from before a restart."""
    stuck = asyncio.Event()

    async def blocked_sleep(_seconds):
        await stuck.wait()

    monkeypatch.setattr(dispatch, "_index_sleep", blocked_sleep)
    box: dict = {}

    async def hold(deps, announce: Announce) -> None:
        result = await handle_announce(deps, announce)
        assert (result.action, result.reason) == ("hold", "waiting_for_mam_index")
        box["old_tasks"] = set(dispatch._held_grabs)
        dispatch._held_grabs.clear()
        await _age_holds(30)
        monkeypatch.setattr(
            dispatch, "_process_started_at", holds_storage._utc_now_iso(),
        )

        async def instant(seconds):
            mam_index["waits"].append(seconds)
            await asyncio.sleep(0)

        monkeypatch.setattr(dispatch, "_index_sleep", instant)

    box["hold"] = hold
    yield box
    for task in box.get("old_tasks", ()):
        task.cancel()
    await asyncio.gather(*box.get("old_tasks", ()), return_exceptions=True)


async def _resumed_done() -> None:
    await asyncio.gather(*list(dispatch._held_grabs))


class TestRestartMidWait:
    async def test_resumed_and_grabbed_once_when_indexed(
        self, temp_db, mam_search, mam_index, died_mid_wait,
    ):
        deps = _irc_deps()
        calls = _recording(deps)
        await died_mid_wait["hold"](deps, _make_announce(torrent_id=TID))
        assert calls == []
        mam_search["items"][TID] = _item()   # MAM lists it by now

        await hold_release.tick(deps)
        await _resumed_done()

        assert [c["tid"] for c in calls] == [TID]
        [hold] = await _holds()
        assert hold["kind"] == "index_wait"
        assert hold["state"] == "released"
        assert hold["resolution_reason"].startswith("indexed:submit:grab_")

    async def test_resumes_on_the_rest_of_the_schedule(
        self, temp_db, mam_search, mam_index, died_mid_wait,
    ):
        deps = _irc_deps()
        _recording(deps)
        await died_mid_wait["hold"](deps, _make_announce(torrent_id=TID))
        mam_index["waits"].clear()

        await hold_release.tick(deps)
        await _resumed_done()

        # 30s in: the next poll was due at 60s, then 60, 60, 120, 120, 180.
        assert mam_index["waits"][0] == pytest.approx(30, abs=2)
        assert mam_index["waits"][1:] == [60, 60, 120, 120, 180]

    async def test_never_listed_goes_ahead_on_the_announce_vip_word(
        self, temp_db, mam_search, mam_index, died_mid_wait,
    ):
        """The announce (and its VIP flag) came back from the row, so a
        torrent MAM never lists is grabbed as D37 says: VIP → no wedge."""
        deps = _irc_deps()
        calls = _recording(deps)
        mam_index["appears_after"] = None
        await died_mid_wait["hold"](
            deps, replace(_make_announce(torrent_id=TID), vip=True),
        )
        await _age_holds(15 * 60)   # down past the whole 10-minute wait

        await hold_release.tick(deps)
        await _resumed_done()

        assert [(c["tid"], c["use_fl_wedge"]) for c in calls] == [(TID, False)]
        [hold] = await _holds()
        assert hold["resolution_reason"].startswith("trust_announce:submit:grab_")

    async def test_resumed_once_however_many_ticks(
        self, temp_db, mam_search, mam_index, died_mid_wait,
    ):
        deps = _irc_deps()
        calls = _recording(deps)
        await died_mid_wait["hold"](deps, _make_announce(torrent_id=TID))
        mam_search["items"][TID] = _item()

        assert await dispatch.resume_index_waits(deps) == 1
        assert await dispatch.resume_index_waits(deps) == 0
        await _resumed_done()
        assert len(calls) == 1

    async def test_uses_the_live_dispatcher(
        self, temp_db, mam_search, mam_index, died_mid_wait,
    ):
        from app import state
        from app.policy.engine import PolicyConfig

        startup = _irc_deps()
        startup_calls = _recording(startup)
        await died_mid_wait["hold"](startup, _make_announce(torrent_id=TID))
        mam_search["items"][TID] = _item()
        live = _irc_deps()
        live.policy_config = PolicyConfig(vip_only=True)   # a save during the outage
        live_calls = _recording(live)
        state.dispatcher = live

        await hold_release.tick(live)
        await _resumed_done()

        assert startup_calls == [] and live_calls == []   # VIP-only refused it
        [hold] = await _holds()
        assert hold["resolution_reason"].startswith("indexed:skip:")   # it did run


class TestNoRestart:
    async def test_a_live_wait_is_not_resumed_twice(self, temp_db, mam_search, mam_index):
        """A row this process created has its task: the tick leaves it alone."""
        stuck = asyncio.Event()

        async def blocked_sleep(_seconds):
            await stuck.wait()

        dispatch._index_sleep, saved = blocked_sleep, dispatch._index_sleep
        try:
            deps = _irc_deps()
            calls = _recording(deps)
            await handle_announce(deps, _make_announce(torrent_id=TID))
            resumed = await dispatch.resume_index_waits(deps)
            mam_search["items"][TID] = _item()
            stuck.set()
            await asyncio.gather(*list(dispatch._held_grabs))
            assert resumed == 0
            assert len(calls) == 1
        finally:
            dispatch._index_sleep = saved
            stuck.set()
            for task in list(dispatch._held_grabs):
                task.cancel()
            await asyncio.gather(*list(dispatch._held_grabs), return_exceptions=True)

    async def test_a_finished_wait_is_marked_released(self, temp_db, mam_search, mam_index):
        deps = _irc_deps()
        _recording(deps)
        await handle_announce(deps, _make_announce(torrent_id=TID))
        await asyncio.gather(*list(dispatch._held_grabs))
        [hold] = await _holds()
        assert (hold["kind"], hold["state"]) == ("index_wait", "released")

    async def test_a_wait_stopped_by_the_kill_switch_is_dropped(
        self, temp_db, mam_search, mam_index, monkeypatch,
    ):
        deps = _irc_deps()
        calls = _recording(deps)
        await handle_announce(deps, _make_announce(torrent_id=TID))
        # Switched off while it waits (the held task hasn't run yet).
        monkeypatch.setattr(
            dispatch, "_live_kill_switch_state",
            lambda: {"irc_enabled": False, "dry_run": False},
        )
        await asyncio.gather(*list(dispatch._held_grabs))
        assert calls == []
        [hold] = await _holds()
        assert (hold["state"], hold["resolution_reason"]) == ("dropped", "index_wait_dropped")


class TestFormatDedupUntouched:
    async def test_index_waits_are_not_format_dedup_holds(self, temp_db):
        db = await get_db()
        try:
            await holds_storage.create_index_wait(
                db, announce_id=None, torrent_id=TID, torrent_name="X",
                category="Ebooks - Fantasy", author_blob="A", book_format="epub",
                payload="{}", wait_seconds=0,
            )
            assert await holds_storage.list_due(db, now_iso="9999-01-01 00:00:00") == []
        finally:
            await db.close()


class TestSchedule:
    def test_remaining_delays(self):
        assert dispatch._remaining_index_delays(0) == dispatch._INDEX_WAIT_DELAYS_S
        assert dispatch._remaining_index_delays(47) == (13, 60, 60, 120, 120, 180)
        assert dispatch._remaining_index_delays(600) == (0.0,)
        assert dispatch._remaining_index_delays(5000) == (0.0,)
