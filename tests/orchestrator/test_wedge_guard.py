"""Wedges: never on a torrent that's already free, never blind (D28-D30).

MAM's `download.php` doc: `fl` "will even spend on VIP torrents ...
no refunds available". So `&fl=1` goes only on a torrent the search API
confirmed is not free, whether the grab policy or a user's tick asked
for it, and every wedge used leaves an economy-audit row.
"""
from __future__ import annotations

import httpx
import pytest

from app.database import get_db
from app.mam import user_status
from app.mam.grab import GrabResult
from app.mam.user_status import UserStatus
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
        """A new torrent can beat MAM's search index; the lookup fails."""
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
