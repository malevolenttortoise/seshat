"""Wave 5a S1 — the stranded-grab self-heal (G116–G120).

A restart mid-pipeline and a failed qBit submit whose torrent qBit has
anyway both used to strand a grab for good. The budget watcher's sweep
re-runs the first (≤ 7 days, ≤ 2 re-runs) and puts the second back to
`submitted`, from what qBit already holds. Every test that drives a tick
also proves the dangerous calls never happen: no `fetch_torrent`, no qBit
`add_torrent` (ADR-0022).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Optional

import pytest

from app.clients.base import TorrentInfo
from app.database import get_db
from app.orchestrator import budget_watcher, pipeline, self_heal
from app.orchestrator.download_watcher import TorrentSnap
from app.rate_limit import ledger as ledger_mod
from app.storage import grabs as grabs_storage
from app.storage import pipeline as pipe_storage
from app.storage import review_queue as review_storage
from tests.orchestrator.test_budget_watcher import _FakeQbit, _make_deps

HASH = "a" * 40
SAVE = "/downloads/[mam-complete]"


class _Qbit(_FakeQbit):
    """The watcher's fake qBit plus a torrent-file listing."""

    def __init__(self, *, torrents=None, files: Optional[dict] = None):
        super().__init__(torrents=torrents)
        self.files = files or {}

    async def list_torrent_files(self, torrent_hash: str) -> list[str]:
        return list(self.files.get(torrent_hash, []))


def _torrent(hash_: str = HASH, state: str = "uploading") -> TorrentInfo:
    return TorrentInfo(
        hash=hash_, name="The Book", category="mam-complete", state=state,
        seeding_seconds=60, save_path=SAVE, added_on=1,
    )


@pytest.fixture
def ran(monkeypatch):
    """Stand-in for the pipeline: records the events the tick hands it."""
    events: list = []

    async def fake_process_completion(db, event, **kwargs):
        events.append(event)
        return True

    monkeypatch.setattr(budget_watcher, "process_completion", fake_process_completion)
    return events


@pytest.fixture
def notified(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_emit(event, *, title="", message="", **kwargs):
        sent.append((event, message))

    from app.notifications import bus
    monkeypatch.setattr(bus, "emit", fake_emit)
    return sent


async def _grab(state: str, *, qbit_hash: str = HASH, name: str = "The Book",
                grabbed_ago: str = "-1 hours") -> int:
    db = await get_db()
    try:
        gid = await grabs_storage.create_grab(
            db, announce_id=None, mam_torrent_id="777", torrent_name=name,
            category="Ebooks - Fantasy", author_blob="Some Author",
            state=state, qbit_hash=qbit_hash,
        )
        await db.execute(
            "UPDATE grabs SET grabbed_at = datetime('now', ?) WHERE id = ?",
            (grabbed_ago, gid),
        )
        await db.commit()
        return gid
    finally:
        await db.close()


async def _run(grab_id: int, state: str, *, idle: str = "-30 minutes",
               staged_path: Optional[str] = None, error: Optional[str] = None,
               qbit_hash: str = HASH) -> int:
    db = await get_db()
    try:
        run_id = await pipe_storage.create_run(
            db, grab_id=grab_id, qbit_hash=qbit_hash, source_path=SAVE,
            state=state,
        )
        await db.execute(
            "UPDATE pipeline_runs SET state_updated_at = datetime('now', ?), "
            "staged_path = ?, error = ? WHERE id = ?",
            (idle, staged_path, error, run_id),
        )
        await db.commit()
        return run_id
    finally:
        await db.close()


async def _runs(grab_id: int) -> list[tuple[int, str, Optional[str]]]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, state, error FROM pipeline_runs WHERE grab_id = ? ORDER BY id",
            (grab_id,),
        )
        return [(r["id"], r["state"], r["error"]) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _grab_state(grab_id: int) -> str:
    db = await get_db()
    try:
        return (await grabs_storage.get_grab(db, grab_id)).state
    finally:
        await db.close()


def _deps(qbit, staging: Path):
    return dataclasses.replace(_make_deps(qbit=qbit), staging_path=str(staging))


def _assert_nothing_fetched_or_added(deps, qbit) -> None:
    assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
    assert qbit.add_calls == []


# ─── A restart mid-pipeline ──────────────────────────────────


class TestStrandedRun:
    async def test_rerun_from_qbits_files_without_fetching(
        self, temp_db, tmp_path, ran,
    ):
        staging = tmp_path / "staging"
        folder = staging / "The Book"
        folder.mkdir(parents=True)
        (folder / "The Book.epub").write_bytes(b"half-staged copy")
        (folder / "Other Grab.m4b").write_bytes(b"another grab's file")
        (folder / "cover.jpg").write_bytes(b"not a staged book file")
        review_dir = tmp_path / "review" / "grab-1"
        review_dir.mkdir(parents=True)
        (review_dir / "The Book.epub").write_bytes(b"x")

        gid = await _grab(grabs_storage.STATE_DOWNLOADED)
        old = await _run(gid, pipe_storage.PIPE_METADATA_DONE, staged_path=str(folder))
        db = await get_db()
        try:
            await review_storage.create_entry(
                db, grab_id=gid, pipeline_run_id=old, staged_path=str(review_dir),
                book_filename="The Book.epub", book_format="epub", metadata={},
                bundle_total=2, bundle_index=0,
            )
        finally:
            await db.close()

        qbit = _Qbit(torrents=[_torrent()], files={HASH: ["The Book.epub", "cover.jpg"]})
        deps = _deps(qbit, staging)
        result = await budget_watcher.tick(deps)

        assert result.error is None
        runs = await _runs(gid)
        assert runs[0][0] == old and runs[0][1] == pipe_storage.PIPE_FAILED
        assert runs[0][2].startswith(self_heal.INTERRUPTED)
        assert runs[1][1] == pipe_storage.PIPE_STAGED
        assert [(e.grab_id, e.pipeline_run_id, e.save_path) for e in ran] == [
            (gid, runs[1][0], SAVE)
        ]
        assert not (folder / "The Book.epub").exists()
        assert (folder / "Other Grab.m4b").read_bytes() == b"another grab's file"
        assert (folder / "cover.jpg").exists()  # the copier never stages non-book files
        assert not review_dir.exists()
        db = await get_db()
        try:
            assert await review_storage.list_for_run(db, old) == []
        finally:
            await db.close()
        _assert_nothing_fetched_or_added(deps, qbit)

    async def test_a_run_this_process_is_driving_is_left_alone(
        self, temp_db, tmp_path, ran,
    ):
        gid = await _grab(grabs_storage.STATE_DOWNLOADED)
        run = await _run(gid, pipe_storage.PIPE_EXTRACTED)
        pipeline._RUNS_IN_FLIGHT.add(run)
        try:
            await budget_watcher.tick(_deps(_Qbit(torrents=[_torrent()]), tmp_path))
        finally:
            pipeline._RUNS_IN_FLIGHT.discard(run)

        assert await _runs(gid) == [(run, pipe_storage.PIPE_EXTRACTED, None)]
        assert ran == []

    async def test_a_run_that_moved_in_the_last_ten_minutes_waits(
        self, temp_db, tmp_path, ran,
    ):
        gid = await _grab(grabs_storage.STATE_DOWNLOADED)
        run = await _run(gid, pipe_storage.PIPE_STAGED, idle="-5 minutes")

        await budget_watcher.tick(_deps(_Qbit(torrents=[_torrent()]), tmp_path))

        assert await _runs(gid) == [(run, pipe_storage.PIPE_STAGED, None)]
        assert ran == []

    async def test_older_than_seven_days_is_closed_not_rerun(
        self, temp_db, tmp_path, ran,
    ):
        gid = await _grab(grabs_storage.STATE_DOWNLOADED, grabbed_ago="-26 days")
        run = await _run(gid, pipe_storage.PIPE_EXTRACTED, idle="-26 days")

        await budget_watcher.tick(_deps(_Qbit(torrents=[_torrent()]), tmp_path))

        [(rid, state, error)] = await _runs(gid)
        assert (rid, state) == (run, pipe_storage.PIPE_FAILED)
        assert "too old to re-run" in error
        assert ran == []
        assert await _grab_state(gid) == grabs_storage.STATE_DOWNLOADED

    async def test_gives_up_after_two_reruns_and_notifies(
        self, temp_db, tmp_path, ran, notified,
    ):
        gid = await _grab(grabs_storage.STATE_DOWNLOADED)
        for _ in range(2):
            await _run(gid, pipe_storage.PIPE_FAILED,
                       error=f"{self_heal.INTERRUPTED} at metadata_done; re-run")
        run = await _run(gid, pipe_storage.PIPE_METADATA_DONE)

        await budget_watcher.tick(_deps(_Qbit(torrents=[_torrent()]), tmp_path))

        runs = await _runs(gid)
        assert len(runs) == 3
        assert runs[-1][0] == run and "gave up after 2 re-runs" in runs[-1][2]
        assert ran == []
        assert [e for e, _ in notified] == ["pipeline.error"]

    async def test_second_strand_is_rerun_again(self, temp_db, tmp_path, ran):
        gid = await _grab(grabs_storage.STATE_DOWNLOADED)
        await _run(gid, pipe_storage.PIPE_FAILED,
                   error=f"{self_heal.INTERRUPTED} at staged; re-run 1 of 2")
        await _run(gid, pipe_storage.PIPE_STAGED)

        await budget_watcher.tick(_deps(_Qbit(torrents=[_torrent()]), tmp_path))

        runs = await _runs(gid)
        assert "re-run 2 of 2" in runs[1][2]
        assert runs[2][1] == pipe_storage.PIPE_STAGED
        assert len(ran) == 1

    async def test_torrent_not_complete_in_qbit_is_closed(
        self, temp_db, tmp_path, ran,
    ):
        gid = await _grab(grabs_storage.STATE_DOWNLOADED)
        run = await _run(gid, pipe_storage.PIPE_STAGED)

        await budget_watcher.tick(
            _deps(_Qbit(torrents=[_torrent(state="downloading")]), tmp_path)
        )

        [(rid, state, error)] = await _runs(gid)
        assert state == pipe_storage.PIPE_FAILED and "isn't complete in qBit" in error
        assert ran == []

    async def test_an_empty_qbit_list_closes_nothing(self, temp_db, tmp_path, ran):
        gid = await _grab(grabs_storage.STATE_DOWNLOADED)
        run = await _run(gid, pipe_storage.PIPE_STAGED)

        await budget_watcher.tick(_deps(_Qbit(torrents=[]), tmp_path))

        assert await _runs(gid) == [(run, pipe_storage.PIPE_STAGED, None)]

    async def test_partly_reviewed_bundle_is_closed_for_a_hand(
        self, temp_db, tmp_path, ran,
    ):
        gid = await _grab(grabs_storage.STATE_DOWNLOADED)
        run = await _run(gid, pipe_storage.PIPE_METADATA_DONE)
        db = await get_db()
        try:
            rid = await review_storage.create_entry(
                db, grab_id=gid, pipeline_run_id=run, staged_path=str(tmp_path / "r"),
                book_filename="a.epub", book_format="epub", metadata={},
                bundle_total=3, bundle_index=0,
            )
            await review_storage.set_status(db, rid, review_storage.STATUS_APPROVED)
        finally:
            await db.close()

        await budget_watcher.tick(_deps(_Qbit(torrents=[_torrent()]), tmp_path))

        [(_, state, error)] = await _runs(gid)
        assert state == pipe_storage.PIPE_FAILED and "part of it was reviewed" in error
        assert ran == []

    async def test_sunk_and_awaiting_review_runs_are_never_touched(
        self, temp_db, tmp_path, ran,
    ):
        sunk = await _grab(grabs_storage.STATE_DOWNLOADED, qbit_hash="b" * 40)
        s_run = await _run(sunk, pipe_storage.PIPE_SUNK, qbit_hash="b" * 40)
        waiting = await _grab(grabs_storage.STATE_PROCESSING)
        w_run = await _run(waiting, pipe_storage.PIPE_AWAITING_REVIEW)

        await budget_watcher.tick(_deps(
            _Qbit(torrents=[_torrent(), _torrent("b" * 40)]), tmp_path,
        ))

        assert await _runs(sunk) == [(s_run, pipe_storage.PIPE_SUNK, None)]
        assert await _runs(waiting) == [(w_run, pipe_storage.PIPE_AWAITING_REVIEW, None)]
        assert ran == []


class TestInFlight:
    async def test_process_completion_marks_its_run_while_it_runs(
        self, temp_db, monkeypatch,
    ):
        from app.orchestrator.download_watcher import CompletionEvent
        seen: list[bool] = []

        async def fake_prepare(db, event, **kwargs):
            seen.append(event.pipeline_run_id in pipeline.runs_in_flight())
            return []

        monkeypatch.setattr(pipeline, "_prepare_book", fake_prepare)
        event = CompletionEvent(
            grab_id=1, qbit_hash=HASH, torrent_name="x", save_path=SAVE,
            pipeline_run_id=4242,
        )
        db = await get_db()
        try:
            await pipeline.process_completion(
                db, event, staging_path="", default_sink="folder",
                calibre_library_path="", folder_sink_path="", ntfy_url="",
                ntfy_topic="",
            )
        finally:
            await db.close()

        assert seen == [True]
        assert 4242 not in pipeline.runs_in_flight()


class TestStagedCopiesStayInsideStaging:
    async def test_staging_off_never_deletes_qbits_own_files(self, tmp_path):
        """With staging off, `staged_path` is qBit's download folder:
        the seeding file itself. The sweep must not touch it."""
        seeding = tmp_path / "downloads" / "The Book"
        seeding.mkdir(parents=True)
        (seeding / "The Book.epub").write_bytes(b"seeding")
        run = pipe_storage.PipelineRow(
            id=1, grab_id=1, qbit_hash=HASH, source_path=str(seeding),
            staged_path=str(seeding), book_filename="The Book.epub",
            book_format="epub", metadata_title=None, metadata_author=None,
            metadata_series=None, metadata_language=None, sink_name=None,
            sink_result=None, state=pipe_storage.PIPE_EXTRACTED, started_at="",
            completed_at=None, error=None,
        )

        async def files(_h):
            return ["The Book.epub"]

        for staging in ("", str(tmp_path / "staging")):
            await self_heal._remove_staged_copies(
                run, "The Book", HASH, list_files=files, staging_path=staging,
            )

        assert (seeding / "The Book.epub").read_bytes() == b"seeding"


# ─── A failed submit qBit has anyway ─────────────────────────


class TestFailedSubmitHeal:
    @pytest.mark.parametrize("state", [
        grabs_storage.STATE_FAILED_UNKNOWN, grabs_storage.STATE_DUPLICATE_IN_QBIT,
    ])
    async def test_back_to_submitted_and_into_the_pipeline(
        self, temp_db, tmp_path, ran, state,
    ):
        gid = await _grab(state)
        qbit = _Qbit(torrents=[_torrent()])
        deps = _deps(qbit, tmp_path)

        await budget_watcher.tick(deps)

        assert await _grab_state(gid) == grabs_storage.STATE_DOWNLOADED
        assert [e.grab_id for e in ran] == [gid]
        db = await get_db()
        try:
            assert await ledger_mod.get_row(db, gid) is not None
        finally:
            await db.close()
        _assert_nothing_fetched_or_added(deps, qbit)

    async def test_waits_while_qbit_is_still_downloading(self, temp_db, tmp_path, ran):
        gid = await _grab(grabs_storage.STATE_FAILED_UNKNOWN)

        await budget_watcher.tick(
            _deps(_Qbit(torrents=[_torrent(state="downloading")]), tmp_path)
        )

        assert await _grab_state(gid) == grabs_storage.STATE_FAILED_UNKNOWN

    async def test_left_alone(self, temp_db, tmp_path, ran):
        """Too old, another grab on the hash, already has a run, not in
        qBit, or a state qBit said no to: none of these is healed."""
        too_old = await _grab(grabs_storage.STATE_FAILED_UNKNOWN,
                              qbit_hash="1" * 40, grabbed_ago="-8 days")
        shared = await _grab(grabs_storage.STATE_DUPLICATE_IN_QBIT, qbit_hash="2" * 40)
        await _grab(grabs_storage.STATE_COMPLETE, qbit_hash="2" * 40)
        has_run = await _grab(grabs_storage.STATE_FAILED_UNKNOWN, qbit_hash="3" * 40)
        await _run(has_run, pipe_storage.PIPE_FAILED, qbit_hash="3" * 40, error="x")
        absent = await _grab(grabs_storage.STATE_FAILED_UNKNOWN, qbit_hash="4" * 40)
        rejected = await _grab(grabs_storage.STATE_FAILED_QBIT_REJECTED, qbit_hash="5" * 40)
        torrents = [_torrent(h * 40) for h in "1235"]

        await budget_watcher.tick(_deps(_Qbit(torrents=torrents), tmp_path))

        assert await _grab_state(too_old) == grabs_storage.STATE_FAILED_UNKNOWN
        assert await _grab_state(shared) == grabs_storage.STATE_DUPLICATE_IN_QBIT
        assert await _grab_state(has_run) == grabs_storage.STATE_FAILED_UNKNOWN
        assert await _grab_state(absent) == grabs_storage.STATE_FAILED_UNKNOWN
        assert await _grab_state(rejected) == grabs_storage.STATE_FAILED_QBIT_REJECTED
        assert ran == []


class TestSweepDirect:
    async def test_snapshot_states_come_from_the_watcher(self, temp_db, tmp_path):
        """`sweep` reads only the snapshot it's handed (no qBit call of its own)."""
        gid = await _grab(grabs_storage.STATE_DOWNLOADED)
        await _run(gid, pipe_storage.PIPE_STAGED)

        async def files(_h):
            return []

        db = await get_db()
        try:
            events = await self_heal.sweep(
                db, {HASH: TorrentSnap(state="stalledUP", save_path=SAVE)},
                list_files=files, staging_path=str(tmp_path),
            )
        finally:
            await db.close()
        assert [e.grab_id for e in events] == [gid]
