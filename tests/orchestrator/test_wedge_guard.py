"""Wedges: never on a torrent that's already free, never blind (D28-D30).

MAM's `download.php` doc: `fl` "will even spend on VIP torrents ...
no refunds available". So `&fl=1` goes only on a torrent the search API
confirmed is not free, whether the grab policy or a user's tick asked
for it, and every wedge used leaves an economy-audit row.

A fresh announce beats MAM's search index, so an allowed announce waits
for MAM to list it before grabbing (D37, D38).
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from app.database import get_db
from app.mam import user_status
from app.mam.grab import GrabResult
from app.mam.user_status import UserStatus
from app.orchestrator import dispatch
from app.orchestrator.dispatch import handle_announce, inject_grab
from app.policy.engine import PolicyConfig
from app.storage import economy_audit
from tests.fake_mam import MINIMAL_BENCODED_TORRENT
from tests.orchestrator.test_dispatch import _make_announce, _make_deps, _make_filter_config
from tests.orchestrator.test_manual_grab import TID, _item, mam_search  # noqa: F401


@pytest.fixture(autouse=True)
def wedges_in_hand():
    """An account with wedges to spend (cached status: no MAM call)."""
    import time
    user_status._cache[user_status._cache_key("good_token")] = (
        time.monotonic(),
        UserStatus(ratio=5.0, wedges=50, seedbonus=0, classname="", username="",
                   uid=1, uploaded_bytes=0, downloaded_bytes=0, upload_buffer_bytes=10**13),
    )
    yield
    user_status.invalidate_cache()


def _recording(deps) -> list[dict]:
    calls: list[dict] = []

    async def fetch(torrent_id, token, **kwargs):
        calls.append({"tid": torrent_id, **kwargs})
        return GrabResult(success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT)

    deps.fetch_torrent = fetch
    return calls


async def _wedge_rows() -> list:
    db = await get_db()
    try:
        return await economy_audit.list_recent(db, action=economy_audit.ACTION_WEDGE)
    finally:
        await db.close()


def _irc_deps():
    deps = _make_deps(filter_config=_make_filter_config(allowed=["Brandon Sanderson"]))
    deps.policy_config = PolicyConfig(use_wedge=True)
    return deps


class TestPolicyWedge:
    async def test_paid_torrent_gets_a_wedge_and_an_audit_row(self, temp_db, mam_search):
        deps = _irc_deps()
        calls = _recording(deps)
        await handle_announce(deps, _make_announce(torrent_id=TID))
        assert [c["use_fl_wedge"] for c in calls] == [True]
        [row] = await _wedge_rows()
        assert row.torrent_id == TID
        assert row.trigger == economy_audit.TRIGGER_IRC_AUTOGRAB
        assert "grab policy" in row.message

    @pytest.mark.parametrize("flags", [{"vip": 1}, {"free": 1}, {"fl_vip": 1}, {"personal_freeleech": 1}],
                             ids=["vip", "freeleech", "fl_vip", "personal_fl"])
    async def test_free_torrent_never_gets_one(self, temp_db, mam_search, flags):
        mam_search["items"][TID] = _item(**flags)
        deps = _irc_deps()
        calls = _recording(deps)
        await handle_announce(deps, _make_announce(torrent_id=TID))
        assert [c["use_fl_wedge"] for c in calls] == [False]
        assert await _wedge_rows() == []

    async def test_unknown_status_grabs_paid_without_a_wedge(self, temp_db, mam_search):
        """MAM's search didn't answer at all: no waiting, no wedge."""
        mam_search["items"][TID] = httpx.ReadTimeout("")
        deps = _irc_deps()
        calls = _recording(deps)
        result = await handle_announce(deps, _make_announce(torrent_id=TID))
        assert result.action == "submit"
        assert [c["use_fl_wedge"] for c in calls] == [False]
        assert await _wedge_rows() == []


class TestForcedWedge:
    """A user's 'use wedge' tick (inject, send-to-pipeline, BookSidebar)."""

    async def test_tick_on_a_paid_torrent_spends_and_audits(self, temp_db, mam_search):
        deps = _make_deps()
        calls = _recording(deps)
        await inject_grab(deps, torrent_id=TID, torrent_name="The Way of Kings", force_fl_wedge=True)
        assert [c["use_fl_wedge"] for c in calls] == [True]
        [row] = await _wedge_rows()
        assert row.trigger == economy_audit.TRIGGER_USER_GRAB
        assert "manual tick" in row.message

    async def test_tick_on_a_vip_torrent_is_ignored(self, temp_db, mam_search):
        mam_search["items"][TID] = _item(vip=1)
        deps = _make_deps()
        calls = _recording(deps)
        await inject_grab(deps, torrent_id=TID, force_fl_wedge=True)
        assert [c["use_fl_wedge"] for c in calls] == [False]
        assert await _wedge_rows() == []

    async def test_tick_with_unknown_status_is_ignored(self, temp_db, mam_search):
        mam_search["items"][TID] = httpx.ConnectError("")
        deps = _make_deps()
        calls = _recording(deps)
        await inject_grab(deps, torrent_id=TID, force_fl_wedge=True)
        assert [c["use_fl_wedge"] for c in calls] == [False]

    async def test_failed_fetch_records_no_wedge(self, temp_db, mam_search):
        deps = _make_deps()

        async def fetch(torrent_id, token, **kwargs):
            return GrabResult(success=False, failure_kind="unknown", failure_detail="HTTP 500")

        deps.fetch_torrent = fetch
        await inject_grab(deps, torrent_id=TID, force_fl_wedge=True)
        assert await _wedge_rows() == []


@pytest.fixture
def mam_index(monkeypatch, mam_search):
    """A fresh upload MAM's search doesn't list yet. It shows up as `item`
    after `appears_after` waits (None = never); the waits are instant."""
    state = {"item": _item(), "appears_after": 1, "waits": []}
    del mam_search["items"][TID]

    async def fake_sleep(seconds):
        state["waits"].append(seconds)
        if state["appears_after"] is not None and len(state["waits"]) >= state["appears_after"]:
            mam_search["items"][TID] = state["item"]

    monkeypatch.setattr(dispatch, "_index_sleep", fake_sleep)
    return state


async def _held_grabs_done() -> None:
    await asyncio.gather(*list(dispatch._held_grabs))


class TestWaitForMamIndex:
    """An allowed announce waits until MAM's search lists the torrent (D37, D38)."""

    async def test_indexed_announce_grabs_straight_away(self, temp_db, mam_search):
        deps = _irc_deps()
        calls = _recording(deps)
        result = await handle_announce(deps, _make_announce(torrent_id=TID))
        assert result.action == "submit"
        assert [c["use_fl_wedge"] for c in calls] == [True]
        assert not dispatch._held_grabs

    async def test_unindexed_announce_is_held_then_wedged(self, temp_db, mam_index):
        deps = _irc_deps()
        calls = _recording(deps)
        result = await handle_announce(deps, _make_announce(torrent_id=TID))
        assert (result.action, result.reason) == ("hold", "waiting_for_mam_index")
        assert calls == []
        await _held_grabs_done()
        assert [c["use_fl_wedge"] for c in calls] == [True]
        [row] = await _wedge_rows()
        assert row.trigger == economy_audit.TRIGGER_IRC_AUTOGRAB

    async def test_held_grab_links_to_the_one_announce_row(self, temp_db, mam_index):
        deps = _irc_deps()
        _recording(deps)
        result = await handle_announce(deps, _make_announce(torrent_id=TID))
        await _held_grabs_done()
        db = await get_db()
        try:
            async with db.execute("SELECT id FROM announces") as cur:
                announce_ids = [r[0] for r in await cur.fetchall()]
            async with db.execute("SELECT announce_id FROM grabs") as cur:
                grab_links = [r[0] for r in await cur.fetchall()]
        finally:
            await db.close()
        assert announce_ids == [result.announce_id]
        assert grab_links == [result.announce_id]

    async def test_free_once_indexed_gets_no_wedge(self, temp_db, mam_index):
        mam_index["item"] = _item(free=1)
        deps = _irc_deps()
        calls = _recording(deps)
        await handle_announce(deps, _make_announce(torrent_id=TID))
        await _held_grabs_done()
        assert [c["use_fl_wedge"] for c in calls] == [False]
        assert await _wedge_rows() == []

    async def test_excluded_uploader_is_caught_once_indexed(self, temp_db, mam_index):
        mam_index["item"] = _item(ownership='[12345,"MyAccount"]')
        deps = replace(_irc_deps(), excluded_uploaders=frozenset({"myaccount"}))
        calls = _recording(deps)
        await handle_announce(deps, _make_announce(torrent_id=TID))
        await _held_grabs_done()
        assert calls == []

    async def test_never_indexed_trusts_a_normal_announce(self, temp_db, mam_index):
        mam_index["appears_after"] = None
        deps = _irc_deps()
        calls = _recording(deps)
        await handle_announce(deps, _make_announce(torrent_id=TID))
        await _held_grabs_done()
        assert mam_index["waits"] == list(dispatch._INDEX_WAIT_DELAYS_S)
        assert [c["use_fl_wedge"] for c in calls] == [True]
        [row] = await _wedge_rows()
        assert "grab policy" in row.message

    async def test_never_indexed_vip_announce_gets_no_wedge(self, temp_db, mam_index):
        mam_index["appears_after"] = None
        deps = _irc_deps()
        calls = _recording(deps)
        await handle_announce(deps, replace(_make_announce(torrent_id=TID), vip=True))
        await _held_grabs_done()
        assert [c["use_fl_wedge"] for c in calls] == [False]

    async def test_kill_switch_during_the_wait_stops_the_grab(self, temp_db, mam_index, monkeypatch):
        deps = _irc_deps()
        calls = _recording(deps)
        await handle_announce(deps, _make_announce(torrent_id=TID))
        monkeypatch.setattr(
            dispatch, "_live_kill_switch_state",
            lambda: {"irc_enabled": False, "dry_run": False},
        )
        await _held_grabs_done()
        assert calls == []

    async def test_announced_twice_while_held_grabs_once(self, temp_db, mam_index):
        deps = _irc_deps()
        calls = _recording(deps)
        await handle_announce(deps, _make_announce(torrent_id=TID))
        await handle_announce(deps, _make_announce(torrent_id=TID))
        await _held_grabs_done()
        assert len(calls) == 1
