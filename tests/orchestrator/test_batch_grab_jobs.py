"""Batch grabs run as jobs: send to pipeline, tentative bulk approve
(audit issue 06, L1-03; G7, G14; ADR-0024).

Every MAM request is paced (issue 05), so a batch grab takes minutes,
past what a reverse proxy waits for one request. Both endpoints now start
an in-memory job and return at once; a GET polls it, and the finished
job's `result` is exactly what the endpoint used to return.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from app import state
from app.discovery.database import get_db as get_discovery_db
from app.discovery.routers import pipeline_send
from app.orchestrator import jobs
from app.orchestrator.dispatch import DispatchResult
from app.routers import tentative
from tests.discovery.test_mam_scan_auth_stop import library  # noqa: F401 (fixture)
from tests.fake_mam import MINIMAL_BENCODED_TORRENT
from tests.orchestrator.test_dispatch import _make_deps
from tests.orchestrator.test_manual_grab import _item, fake_clock, mam_search  # noqa: F401


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(pipeline_send.router)
    app.include_router(tentative.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _poll(c: httpx.AsyncClient, path: str) -> dict:
    for _ in range(200):
        body = (await c.get(path)).json()
        if body["done"]:
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} never finished")


async def _found(library, *tids: str) -> list[int]:
    """Mark the library's first books Found on MAM with these torrent IDs."""
    db = await get_discovery_db("ebooks")
    try:
        for bid, tid in zip(library["book_ids"], tids):
            await db.execute(
                "UPDATE books SET mam_status = 'found', mam_torrent_id = ?, "
                "mam_formats = 'epub' WHERE id = ?", (tid, bid),
            )
        await db.commit()
    finally:
        await db.close()
    return library["book_ids"][: len(tids)]


@pytest.fixture
def no_jobs_left():
    yield
    jobs.batch_jobs.jobs.clear()


@pytest.fixture
def fake_inject(monkeypatch):
    """`inject_grab` as send-to-pipeline calls it: recorded, held on a gate."""
    box = {"calls": [], "gate": asyncio.Event()}
    box["gate"].set()

    async def inject(deps, *, torrent_id, **kw):
        box["calls"].append(torrent_id)
        await box["gate"].wait()
        return DispatchResult(action="submit", reason="manual_inject", announce_id=1)

    monkeypatch.setattr(pipeline_send, "inject_grab", inject)
    return box


class TestSendToPipelineJob:
    async def test_returns_at_once_and_the_result_keeps_the_old_shape(
        self, temp_db, library, fake_inject, no_jobs_left,
    ):
        book_ids = await _found(library, "501", "502")
        state.dispatcher = _make_deps()
        fake_inject["gate"].clear()
        async with _client() as c:
            started = (await c.post(
                "/api/discovery/send-to-pipeline", json={"book_ids": book_ids},
            )).json()
            assert started["done"] is False          # the request didn't wait
            assert started["total"] == 2
            fake_inject["gate"].set()
            done = await _poll(c, f"/api/discovery/send-to-pipeline/{started['job_id']}")

        assert fake_inject["calls"] == ["501", "502"]
        assert done["completed"] == 2
        assert [(r["torrent_id"], r["status"], r["ok"]) for r in done["rows"]] == [
            ("501", "done", True), ("502", "done", True),
        ]
        result = done["result"]
        assert set(result) == {"sent", "skipped", "failed", "message", "results"}
        assert (result["sent"], result["failed"], result["skipped"]) == (2, 0, 0)
        assert result["message"] == "Sent 2 to pipeline"

    async def test_nothing_found_answers_with_a_finished_job(
        self, temp_db, library, fake_inject, no_jobs_left,
    ):
        state.dispatcher = _make_deps()
        async with _client() as c:
            body = (await c.post(
                "/api/discovery/send-to-pipeline",
                json={"book_ids": library["book_ids"]},
            )).json()
        assert body["done"] is True
        assert body["result"] == {
            "sent": 0, "skipped": 3,
            "message": "No books with 'Found' MAM status to send",
        }
        assert fake_inject["calls"] == []

    async def test_a_bad_row_fails_alone(
        self, temp_db, library, fake_inject, no_jobs_left,
    ):
        book_ids = await _found(library, "abc", "503")
        state.dispatcher = _make_deps()
        async with _client() as c:
            started = (await c.post(
                "/api/discovery/send-to-pipeline", json={"book_ids": book_ids},
            )).json()
            done = await _poll(c, f"/api/discovery/send-to-pipeline/{started['job_id']}")
        assert fake_inject["calls"] == ["503"]
        assert [(r["ok"], r["error"]) for r in done["rows"]] == [
            (False, "bad torrent ID"), (True, None),
        ]
        assert (done["result"]["sent"], done["result"]["failed"]) == (1, 1)

    async def test_paced_end_to_end(
        self, temp_db, library, mam_search, fake_clock, monkeypatch, no_jobs_left,
    ):
        """Three sends through the real inject_grab and fetch_torrent: every
        MAM request (lookup, download) at least the gap apart."""
        from app.mam import cookie, grab, pacer

        monkeypatch.setattr(pacer, "gap_seconds", lambda: 2.0)
        for tid in ("601", "602", "603"):
            mam_search["items"][tid] = _item(tid, title=f"Book {tid}")
        downloads: list[float] = []

        def mam(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/tor/download.php":
                downloads.append(fake_clock["now"])
                return httpx.Response(200, content=MINIMAL_BENCODED_TORRENT)
            return httpx.Response(404)

        monkeypatch.setattr(
            cookie, "_client", httpx.AsyncClient(transport=httpx.MockTransport(mam)),
        )
        deps = _make_deps(budget_cap=1000)
        deps.fetch_torrent = grab.fetch_torrent
        state.dispatcher = deps
        book_ids = await _found(library, "601", "602", "603")

        async with _client() as c:
            started = (await c.post(
                "/api/discovery/send-to-pipeline", json={"book_ids": book_ids},
            )).json()
            done = await _poll(c, f"/api/discovery/send-to-pipeline/{started['job_id']}")

        assert done["result"]["sent"] == 3, done
        assert mam_search["calls"] == ["601", "602", "603"]
        assert len(downloads) == 3
        # Every MAM request the pacer let through (the lookups, the
        # downloads, anything else the grab asked for), on the fake clock.
        sent = sorted(pacer._recent)
        assert len(sent) >= 6
        assert all(b - a >= 2.0 for a, b in zip(sent, sent[1:]))


class TestTentativeBulkApproveJob:
    @pytest.fixture
    def approvals(self, monkeypatch):
        box = {"calls": [], "gate": asyncio.Event(), "fail": set()}
        box["gate"].set()

        async def pending(db, subset):
            return [11, 12, 13] if subset is None else list(subset)

        async def approve(tid, override_mam_snatched=False):
            box["calls"].append(tid)
            await box["gate"].wait()
            if tid in box["fail"]:
                return tentative.TentativeActionResponse(
                    ok=False, id=tid, status="approved", error="already grabbed",
                )
            return tentative.TentativeActionResponse(ok=True, id=tid, status="approved")

        monkeypatch.setattr(tentative, "_pending_ids", pending)
        monkeypatch.setattr(tentative, "approve", approve)
        state.dispatcher = _make_deps()
        return box

    async def test_returns_at_once_and_the_result_keeps_the_old_shape(
        self, temp_db, approvals, no_jobs_left,
    ):
        approvals["fail"].add(12)
        approvals["gate"].clear()
        async with _client() as c:
            started = (await c.post("/api/v1/tentative/bulk/approve", json={})).json()
            assert started["done"] is False and started["total"] == 3
            approvals["gate"].set()
            done = await _poll(c, f"/api/v1/tentative/bulk/approve/{started['job_id']}")

        assert approvals["calls"] == [11, 12, 13]
        assert done["result"] == {
            "processed": 2, "failed": 1, "errors": ["tid=12: already grabbed"],
        }
        assert [(r["id"], r["status"], r["ok"]) for r in done["rows"]] == [
            (11, "done", True), (12, "done", False), (13, "done", True),
        ]
        assert done["completed"] == 3

    async def test_no_pending_rows_is_a_finished_job(
        self, temp_db, approvals, no_jobs_left,
    ):
        async with _client() as c:
            body = (await c.post("/api/v1/tentative/bulk/approve", json={"ids": []})).json()
        assert body["done"] is True
        assert body["result"] == {"processed": 0, "failed": 0, "errors": []}
        assert approvals["calls"] == []

    async def test_reject_and_dismiss_stay_synchronous(self, temp_db, approvals):
        async with _client() as c:
            r = await c.post("/api/v1/tentative/bulk/dismiss", json={"ids": []})
        assert set(r.json()) == {"processed", "failed", "errors"}


class TestPolling:
    @pytest.mark.parametrize("path", [
        "/api/discovery/send-to-pipeline/nope",
        "/api/v1/tentative/bulk/approve/nope",
    ])
    async def test_unknown_job_is_404_with_the_restart_hint(self, path):
        async with _client() as c:
            r = await c.get(path)
        assert r.status_code == 404
        assert r.json()["detail"] == jobs.GONE_MESSAGE

    async def test_a_job_is_only_found_under_its_own_endpoint(self, no_jobs_left):
        job = jobs.finished_batch_job(pipeline_send.SEND_JOB, {"sent": 0})
        async with _client() as c:
            r = await c.get(f"/api/v1/tentative/bulk/approve/{job.id}")
        assert r.status_code == 404

    async def test_a_crashed_job_reports_the_error(self, no_jobs_left):
        async def boom(job):
            raise RuntimeError("disk full")

        job = jobs.start_batch_job("x", [{"status": "pending"}], boom)
        await asyncio.wait_for(asyncio.gather(*jobs.batch_jobs.tasks), timeout=5)
        assert job.done is True
        assert job.to_dict()["error"] == "Unexpected error: disk full"
