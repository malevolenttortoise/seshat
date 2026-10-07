"""Manual Grab, batch — up to 30 rows, links and files mixed.

The batch rules: the cap counts both kinds and is enforced by the
server; the "Use wedges" toggle is refused outright when the batch
needs more wedges than the account may spend (D9: nothing is spent,
nothing starts); and 30 rows never burst MAM — preview and Grab all
both go through the pacer gap.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi import FastAPI

from app import state
from app.mam import search_pacer, torrent_info
from app.mam.grab import GrabResult
from app.orchestrator import manual_grab
from app.routers.manual_grab import router
from tests.fake_mam import MINIMAL_BENCODED_TORRENT
from tests.orchestrator.test_dispatch import _make_deps
from tests.orchestrator.test_manual_grab import (  # noqa: F401 (fixtures)
    TID,
    _Resp,
    _item,
    _no_leftover_jobs,
    _own_cover_cache,
    _run_job,
    covers,
    fake_clock,
    mam_search,
)
from tests.orchestrator.test_manual_grab_upload import _torrent


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _link(tid: str, **kw) -> dict:
    return {"kind": "link", "value": tid, **kw}


def _file(**kw) -> dict:
    return {"kind": "file", "name": "x.torrent",
            "data_b64": base64.b64encode(_torrent(**kw)).decode()}


class _StubJob:
    def to_dict(self):
        return {"job_id": "stub", "done": True, "rows": []}


@pytest.fixture
def no_job(monkeypatch):
    """Record what Grab all would start, without starting it."""
    started: list = []

    def fake_start(deps, items):
        started.append(items)
        return _StubJob()

    monkeypatch.setattr(manual_grab, "start_job", fake_start)
    return started


@pytest.fixture
def wedges(monkeypatch):
    """The account's wedges as Grab all re-reads them."""
    box = {"budget": manual_grab.WedgeBudget(wedges=5, reserved=2), "fresh": []}

    async def fake_budget(deps, *, fresh=False):
        box["fresh"].append(fresh)
        return box["budget"]

    monkeypatch.setattr(manual_grab, "wedge_budget", fake_budget)
    return box


async def _post_grab(items: list[dict]):
    state.dispatcher = _make_deps()
    try:
        async with _client() as c:
            return await c.post("/api/v1/manual-grab/grab", json={"items": items})
    finally:
        state.dispatcher = None


# ─── The cap and the wedge budget, server-side ───────────────


class TestBatchLimits:
    async def test_cap_counts_links_and_files_together(self, temp_db, no_job):
        items = [_link(str(1000 + i)) for i in range(29)] + [_file(mid="1"), _file(mid="2")]
        resp = await _post_grab(items)
        assert resp.status_code == 422
        assert no_job == []

    async def test_too_many_wedges_refuses_the_whole_batch(self, temp_db, no_job, wedges):
        items = [_link(str(1000 + i), use_wedge=True) for i in range(4)]
        resp = await _post_grab(items)
        assert resp.status_code == 409
        assert resp.json()["detail"].startswith("Needs 4 wedges, 3 spendable (5 − 2 reserved)")
        assert no_job == []
        assert wedges["fresh"] == [True]   # checked against a fresh read

    async def test_wedges_that_fit_start_the_job(self, temp_db, no_job, wedges):
        items = [_link(str(1000 + i), use_wedge=True) for i in range(3)] + [_link("2000")]
        resp = await _post_grab(items)
        assert resp.status_code == 200, resp.text
        assert [i.use_wedge for i in no_job[0]] == [True, True, True, False]

    async def test_unreadable_account_refuses_a_wedge_batch(self, temp_db, no_job, wedges):
        wedges["budget"] = None
        resp = await _post_grab([_link("1", use_wedge=True)])
        assert resp.status_code == 409
        assert no_job == []

    async def test_no_wedges_wanted_never_reads_the_account(self, temp_db, no_job, wedges):
        resp = await _post_grab([_link("1"), _link("2")])
        assert resp.status_code == 200
        assert wedges["fresh"] == []

    async def test_an_upload_can_take_a_wedge(self, temp_db, no_job, wedges):
        """D31: via "Buy as FL"; and it counts toward the wedge budget."""
        item = _file()
        item["use_wedge"] = True
        resp = await _post_grab([item, _link("1", use_wedge=True)])
        assert resp.status_code == 200, resp.text
        assert [i.use_wedge for i in no_job[0]] == [True, True]

    async def test_uploads_count_toward_the_wedge_budget(self, temp_db, no_job, wedges):
        files = []
        for mid in ("11", "12", "13", "14"):
            f = _file(mid=mid)
            f["use_wedge"] = True
            files.append(f)
        resp = await _post_grab(files)
        assert resp.status_code == 409
        assert no_job == []


# ─── Wedges in the job ───────────────────────────────────────


def _recording_fetch(deps) -> list[dict]:
    calls: list[dict] = []

    async def fetch(torrent_id, token, **kwargs):
        calls.append({"tid": torrent_id, **kwargs})
        return GrabResult(success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT)

    deps.fetch_torrent = fetch
    return calls


class TestJobWedges:
    async def test_wedge_rides_on_the_download(self, temp_db, mam_search, fake_clock):
        deps = _make_deps()
        calls = _recording_fetch(deps)
        job = await _run_job(deps, manual_grab.GrabRequestItem(kind="link", value=TID, use_wedge=True))
        assert [c["use_fl_wedge"] for c in calls] == [True]
        assert job.rows[0].wedge_used is True

    async def test_no_wedge_on_a_torrent_that_turned_free(self, temp_db, mam_search, fake_clock):
        mam_search["items"][TID] = _item(free=1)
        deps = _make_deps()
        calls = _recording_fetch(deps)
        job = await _run_job(deps, manual_grab.GrabRequestItem(kind="link", value=TID, use_wedge=True))
        assert [c["use_fl_wedge"] for c in calls] == [False]
        assert job.rows[0].wedge_used is False


# ─── 30 rows, never a burst ──────────────────────────────────


class TestBatchPacing:
    @pytest.fixture
    def timed_mam(self, monkeypatch, fake_clock):
        """MAM's search API + cover CDN, logging the clock at each call."""
        from app.metadata import covers as covers_mod

        times: list[float] = []

        async def fake_post(url, token=None, payload=None, timeout=15):
            times.append(fake_clock["now"])
            tid = json.loads(payload)["tor"]["id"]
            return _Resp({"data": [_item(tid, title=f"Book {tid}")]})

        async def fake_cover(torrent_id, *, dest_dir, basename="cover-mam", token=""):
            times.append(fake_clock["now"])
            return None

        monkeypatch.setattr(torrent_info, "_do_post", fake_post)
        monkeypatch.setattr(covers_mod, "fetch_mam_cover", fake_cover)
        monkeypatch.setattr(search_pacer, "gap_seconds", lambda: 2.0)
        return times

    @staticmethod
    def _gaps(times: list[float]) -> list[float]:
        return [b - a for a, b in zip(times, times[1:])]

    async def test_thirty_previews_are_paced(self, temp_db, timed_mam):
        deps = _make_deps()
        for i in range(30):
            await manual_grab.preview_link(deps, str(5000 + i))
        assert len(timed_mam) == 60   # a lookup and a cover per row
        assert min(self._gaps(timed_mam)) >= 2.0

    async def test_thirty_row_grab_all_is_paced(self, temp_db, timed_mam):
        deps = _make_deps(budget_cap=1000)
        job = await _run_job(deps, *[
            manual_grab.GrabRequestItem(kind="link", value=str(6000 + i)) for i in range(30)
        ])
        assert [r.status for r in job.rows] == ["submitted"] * 30
        assert len(timed_mam) == 30   # inject_grab's own lookups were cache hits
        assert min(self._gaps(timed_mam)) >= 2.0
