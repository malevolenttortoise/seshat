"""MAM's own unsatisfied count as the snatch budget's floor (2026-10 audit,
issue 09; Mark's G41–G43).

The summary shape below mirrors one live `jsonLoad.php?snatch_summary`
read (2026-10-08) with placeholder numbers; no MAM identity in here.
"""
import time

import pytest

from app import state
from app.mam.user_status import UserStatusError
from app.rate_limit import mam_floor
from app.rate_limit.mam_floor import MamSnatchSummary


def _summary_body(count=32, limit=200, inact_unsat=0, inact_hnr=0):
    return {
        "classname": "Placeholder",
        "snatch_summary": {
            "connectable": "yes",
            "created": 1791475283,
            "created_at": "2026-10-08 16:01:23",
            "inactHnr": {"name": "Not Seeding - H&R - Not Yet Satisfied", "count": inact_hnr, "size": None},
            "inactSat": {"name": "Not Seeding - Satisfied", "count": 0, "size": None},
            "inactUnsat": {"name": "Not Seeding - Not Yet Satisfied", "count": inact_unsat, "size": None},
            "leeching": {"name": "Leeching Torrents", "count": 0, "size": None},
            "sSat": {"name": "Seeding - Satisfied", "count": 100, "size": 1},
            "seedUnsat": {"name": "Seeding - Not Yet Satisfied", "count": count, "size": 1},
            "unsat": {"name": "Unsatisfied", "count": count, "limit": limit, "size": 1},
        },
    }


class TestParse:
    def test_live_shape(self):
        s = mam_floor.parse_snatch_summary(_summary_body(32, 200, 2, 1))
        assert s == MamSnatchSummary(
            unsat_count=32, unsat_limit=200, not_seeding=3, as_of=1791475283.0,
        )

    @pytest.mark.parametrize("data", [
        None,
        [],
        {},
        {"snatch_summary": None},
        {"snatch_summary": {"unsat": "32"}},
        {"snatch_summary": {"unsat": {"limit": 200}}},
        {"snatch_summary": {"unsat": {"count": "lots", "limit": 200}}},
        {"snatch_summary": {"unsat": {"count": -1, "limit": 200}}},
    ])
    def test_anything_else_is_none(self, data):
        assert mam_floor.parse_snatch_summary(data) is None

    def test_missing_limit_reads_zero(self):
        s = mam_floor.parse_snatch_summary({"snatch_summary": {"unsat": {"count": 4}}})
        assert s.unsat_count == 4 and s.unsat_limit == 0


class TestFloorAndCap:
    def test_no_summary_leaves_seshat_numbers(self):
        assert mam_floor.floor_count(7) == 7
        assert mam_floor.effective_cap(200) == 200

    def test_floor_is_the_larger_count(self):
        mam_floor.record(MamSnatchSummary(12, 200, 2, None))
        assert mam_floor.floor_count(7) == 12
        assert mam_floor.floor_count(30) == 30

    def test_cap_is_the_lower_limit(self):
        mam_floor.record(MamSnatchSummary(0, 150, 0, None))
        assert mam_floor.effective_cap(200) == 150
        assert mam_floor.effective_cap(100) == 100

    def test_zero_limit_is_ignored(self):
        mam_floor.record(MamSnatchSummary(5, 0, 0, None))
        assert mam_floor.effective_cap(200) == 200

    def test_stale_summary_is_ignored(self):
        old = time.time() - mam_floor.STALE_AFTER_S - 60
        mam_floor.record(MamSnatchSummary(190, 100, 0, None), now=old)
        assert mam_floor.current() is None
        assert mam_floor.floor_count(7) == 7
        assert mam_floor.effective_cap(200) == 200


class TestRefresh:
    @pytest.fixture
    def status_calls(self, monkeypatch):
        """Stand-in for `get_user_status`: records the call and, like the
        real one, records the summary MAM sent (or raises)."""
        box = {"calls": 0, "raise": False}

        async def fake_get_user_status(token=None, ttl=300):
            box["calls"] += 1
            if box["raise"]:
                raise UserStatusError("network error: refused")
            mam_floor.record(MamSnatchSummary(32, 200, 0, None))

        from app.mam import user_status
        monkeypatch.setattr(user_status, "get_user_status", fake_get_user_status)
        return box

    async def test_reads_once_per_interval(self, status_calls):
        assert await mam_floor.refresh_if_due("tok") is True
        assert await mam_floor.refresh_if_due("tok") is False
        assert status_calls["calls"] == 1
        assert mam_floor.current().unsat_count == 32

    async def test_reads_again_after_the_interval(self, status_calls):
        await mam_floor.refresh_if_due("tok")
        state._snatch_budget["mam"]["attempted_at"] -= mam_floor.REFRESH_EVERY_S + 1
        await mam_floor.refresh_if_due("tok")
        assert status_calls["calls"] == 2

    async def test_a_failed_read_waits_the_interval_too(self, status_calls):
        status_calls["raise"] = True
        assert await mam_floor.refresh_if_due("tok") is False
        assert await mam_floor.refresh_if_due("tok") is False
        assert status_calls["calls"] == 1
        assert mam_floor.current() is None

    async def test_no_cookie_no_call(self, status_calls):
        assert await mam_floor.refresh_if_due("") is False
        assert status_calls["calls"] == 0
