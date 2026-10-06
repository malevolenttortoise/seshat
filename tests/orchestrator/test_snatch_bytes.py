"""Phase 0 S2 — keep the .torrent bytes; never re-fetch.

A grab that can't reach qBit right away keeps the bytes MAM already
served (`torrent_store`), and every later step — queue pop, delayed
rotation, delayed reinject — works from those bytes. MAM sees one
download per torrent (ADR-0022). Each test proves it by counting
`fetch_torrent` calls, never by fetching twice.
"""
from __future__ import annotations

import os

import pytest

from app import state
from app.clients.base import AddResult
from app.database import get_db
from app.orchestrator import budget_watcher, torrent_store
from app.orchestrator.dispatch import inject_grab, submit_torrent_bytes
from app.rate_limit import ledger as ledger_mod
from app.rate_limit import queue as queue_mod
from app.routers import delayed as delayed_router
from app.storage import grabs as grabs_storage
from tests.fake_mam import MINIMAL_BENCODED_TORRENT
from tests.orchestrator.test_dispatch import _FakeQbit, _make_deps

TID = "5150"


@pytest.fixture(autouse=True)
def mam_liveness(monkeypatch):
    box = {"answer": "ok", "asked": []}

    async def fake_check(torrent_id, token):
        box["asked"].append(torrent_id)
        return box["answer"]

    monkeypatch.setattr(torrent_store, "check_still_on_mam", fake_check)
    return box


async def _grab(grab_id: int) -> grabs_storage.GrabRow:
    db = await get_db()
    try:
        return await grabs_storage.get_grab(db, grab_id)
    finally:
        await db.close()


async def _fill_budget(n: int) -> None:
    db = await get_db()
    try:
        for i in range(n):
            gid = await grabs_storage.create_grab(
                db, announce_id=None, mam_torrent_id=f"busy{i}",
                torrent_name="busy", category="", author_blob="",
                state=grabs_storage.STATE_SUBMITTED, qbit_hash=f"{i:040d}",
            )
            await ledger_mod.record_grab(db, gid, f"{i:040d}")
    finally:
        await db.close()


# ─── Dispatcher: queueing saves the bytes ────────────────────


class TestQueueSavesBytes:
    async def test_budget_full_queue_saves_the_fetched_bytes(self, temp_db):
        await _fill_budget(1)
        deps = _make_deps(budget_cap=1)

        result = await inject_grab(deps, torrent_id=TID)

        assert result.action == "queue"
        assert deps.fetch_torrent.calls == [(TID, "good_token")]  # type: ignore[attr-defined]
        grab = await _grab(result.grab_id)
        assert grab.state == grabs_storage.STATE_PENDING_QUEUE
        assert grab.qbit_hash == result.qbit_hash
        assert torrent_store.load(grab.torrent_file_path) == MINIMAL_BENCODED_TORRENT
        assert oct(os.stat(grab.torrent_file_path).st_mode & 0o777) == "0o600"

    async def test_client_unreachable_queue_saves_the_bytes(self, temp_db):
        qbit = _FakeQbit(add_result=AddResult(
            success=False, failure_kind="network_error", failure_detail="refused",
        ))
        deps = _make_deps(qbit=qbit)

        result = await inject_grab(deps, torrent_id=TID)

        assert result.action == "queue"
        assert result.reason == "client_unreachable:network_error"
        grab = await _grab(result.grab_id)
        assert grab.state == grabs_storage.STATE_PENDING_QUEUE
        assert torrent_store.load(grab.torrent_file_path) == MINIMAL_BENCODED_TORRENT

    async def test_save_failure_fails_loudly_instead_of_queueing(
        self, temp_db, monkeypatch,
    ):
        notified: list[str] = []

        async def capture(grab_id, detail):
            notified.append(detail)

        def broken_save(grab_id, data):
            raise OSError("disk full")

        monkeypatch.setattr(torrent_store, "save", broken_save)
        monkeypatch.setattr(torrent_store, "notify_grab_failed", capture)
        await _fill_budget(1)
        deps = _make_deps(budget_cap=1)

        result = await inject_grab(deps, torrent_id=TID)

        assert result.reason == "queue_save_failed"
        assert "disk full" in (result.error or "")
        grab = await _grab(result.grab_id)
        assert grab.state == grabs_storage.STATE_FAILED_UNKNOWN
        db = await get_db()
        try:
            assert await queue_mod.size(db) == 0
        finally:
            await db.close()
        assert len(notified) == 1

    async def test_queued_grab_reaches_qbit_with_one_mam_download(self, temp_db):
        # End to end: dispatch fetches once and queues; the budget frees;
        # the watcher submits the saved bytes. One fetch in total.
        await _fill_budget(1)
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit, budget_cap=1)
        queued = await inject_grab(deps, torrent_id=TID)
        assert queued.action == "queue"

        deps.budget_cap = 2
        result = await budget_watcher.tick(deps)

        assert result.queue_pops_submitted == 1
        assert deps.fetch_torrent.calls == [(TID, "good_token")]  # type: ignore[attr-defined]
        assert len(qbit.add_calls) == 1
        grab = await _grab(queued.grab_id)
        assert grab.state == grabs_storage.STATE_SUBMITTED
        assert torrent_store.load(grab.torrent_file_path) is None


# ─── Bytes-in submit path ────────────────────────────────────


class TestSubmitTorrentBytes:
    async def _parked_grab(self) -> int:
        db = await get_db()
        try:
            return await grabs_storage.create_grab(
                db, announce_id=None, mam_torrent_id=TID,
                torrent_name="Delayed Book", category="Ebooks - Fantasy",
                author_blob="Author", state=grabs_storage.STATE_FETCHED,
            )
        finally:
            await db.close()

    async def test_submits_without_fetching(self, temp_db):
        gid = await self._parked_grab()
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)

        result = await submit_torrent_bytes(
            deps, grab_id=gid, torrent_bytes=MINIMAL_BENCODED_TORRENT,
        )

        assert result.action == "submit" and result.reason == "ok"
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert len(qbit.add_calls) == 1
        assert (await _grab(gid)).state == grabs_storage.STATE_SUBMITTED

    async def test_budget_full_queues_with_bytes_saved(self, temp_db):
        gid = await self._parked_grab()
        await _fill_budget(1)
        deps = _make_deps(budget_cap=1)

        result = await submit_torrent_bytes(
            deps, grab_id=gid, torrent_bytes=MINIMAL_BENCODED_TORRENT,
        )

        assert result.action == "queue"
        grab = await _grab(gid)
        assert grab.state == grabs_storage.STATE_PENDING_QUEUE
        assert torrent_store.load(grab.torrent_file_path) == MINIMAL_BENCODED_TORRENT

    async def test_bad_bytes_and_dry_run_place_nothing(self, temp_db, monkeypatch):
        gid = await self._parked_grab()
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)

        bad = await submit_torrent_bytes(deps, grab_id=gid, torrent_bytes=b"nope")
        assert bad.action == "skip" and bad.reason == "bad_torrent_file"

        from app.orchestrator import dispatch
        monkeypatch.setattr(
            dispatch, "_live_kill_switch_state",
            lambda: {"irc_enabled": True, "dry_run": True},
        )
        dry = await submit_torrent_bytes(
            deps, grab_id=gid, torrent_bytes=MINIMAL_BENCODED_TORRENT,
        )
        assert dry.action == "skip" and dry.reason == "dry_run_live"
        assert qbit.add_calls == []


# ─── Delayed reinject ────────────────────────────────────────


@pytest.fixture
def delayed_dir(tmp_path, monkeypatch):
    folder = tmp_path / "delayed"
    folder.mkdir()
    monkeypatch.setattr(
        delayed_router, "load_settings",
        lambda: {"delayed_torrents_path": str(folder)},
    )
    return folder


async def _rotated_grab(folder, data=MINIMAL_BENCODED_TORRENT) -> tuple[int, str]:
    """A grab the way rotation leaves it: failed_unknown + hash, with
    its bytes in the delayed folder."""
    db = await get_db()
    try:
        gid = await grabs_storage.create_grab(
            db, announce_id=None, mam_torrent_id=TID,
            torrent_name="Delayed Book", category="Ebooks - Fantasy",
            author_blob="Author", state=grabs_storage.STATE_FAILED_UNKNOWN,
            qbit_hash="b" * 40,
        )
        await grabs_storage.set_state(
            db, gid, grabs_storage.STATE_FAILED_UNKNOWN,
            failed_reason="rotated to delayed folder",
        )
    finally:
        await db.close()
    name = f"{gid}_{TID}.torrent"
    (folder / name).write_bytes(data)
    return gid, name


class TestDelayedReinject:
    @pytest.fixture(autouse=True)
    def _dispatcher(self, monkeypatch):
        self.qbit = _FakeQbit()
        self.deps = _make_deps(qbit=self.qbit)
        monkeypatch.setattr(state, "dispatcher", self.deps)

    async def test_submits_the_files_bytes_without_fetching(
        self, temp_db, delayed_dir, mam_liveness,
    ):
        gid, name = await _rotated_grab(delayed_dir)

        resp = await delayed_router.reinject(name)

        assert resp.ok is True and resp.grab_id == gid
        assert mam_liveness["asked"] == [TID]
        assert self.deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert len(self.qbit.add_calls) == 1
        assert not (delayed_dir / name).exists()
        assert (await _grab(gid)).state == grabs_storage.STATE_SUBMITTED

    async def test_removed_from_mam_deletes_file_and_fails_grab(
        self, temp_db, delayed_dir, mam_liveness,
    ):
        mam_liveness["answer"] = "removed"
        gid, name = await _rotated_grab(delayed_dir)

        resp = await delayed_router.reinject(name)

        assert resp.ok is False and "removed from MAM" in (resp.error or "")
        assert self.qbit.add_calls == []
        assert not (delayed_dir / name).exists()
        assert (await _grab(gid)).state == grabs_storage.STATE_FAILED_TORRENT_GONE

    async def test_unreachable_check_changes_nothing(
        self, temp_db, delayed_dir, mam_liveness,
    ):
        mam_liveness["answer"] = "unreachable"
        gid, name = await _rotated_grab(delayed_dir)

        resp = await delayed_router.reinject(name)

        assert resp.ok is False and "try again" in (resp.error or "")
        assert self.qbit.add_calls == []
        assert (delayed_dir / name).exists()
        assert (await _grab(gid)).state == grabs_storage.STATE_FAILED_UNKNOWN

    async def test_second_reinject_is_refused(self, temp_db, delayed_dir):
        gid, name = await _rotated_grab(delayed_dir)
        assert (await delayed_router.reinject(name)).ok is True
        (delayed_dir / name).write_bytes(MINIMAL_BENCODED_TORRENT)  # file reappears

        resp = await delayed_router.reinject(name)

        assert resp.ok is False and "already submitted" in (resp.error or "")
        assert len(self.qbit.add_calls) == 1

    async def test_refused_when_torrent_was_grabbed_again(
        self, temp_db, delayed_dir,
    ):
        gid, name = await _rotated_grab(delayed_dir)
        db = await get_db()
        try:
            newer = await grabs_storage.create_grab(
                db, announce_id=None, mam_torrent_id=TID, torrent_name="x",
                category="", author_blob="",
                state=grabs_storage.STATE_SUBMITTED, qbit_hash="c" * 40,
            )
        finally:
            await db.close()

        resp = await delayed_router.reinject(name)

        assert resp.ok is False and f"grab #{newer}" in (resp.error or "")
        assert self.qbit.add_calls == []
        assert (await _grab(gid)).state == grabs_storage.STATE_FAILED_UNKNOWN

    async def test_dry_run_releases_the_claim(
        self, temp_db, delayed_dir, monkeypatch,
    ):
        from app.orchestrator import dispatch
        monkeypatch.setattr(
            dispatch, "_live_kill_switch_state",
            lambda: {"irc_enabled": True, "dry_run": True},
        )
        gid, name = await _rotated_grab(delayed_dir)

        resp = await delayed_router.reinject(name)

        assert resp.ok is False
        grab = await _grab(gid)
        assert grab.state == grabs_storage.STATE_FAILED_UNKNOWN
        assert grab.failed_reason == "rotated to delayed folder"
        assert (delayed_dir / name).exists()


# ─── Startup sweep ───────────────────────────────────────────


class TestSweep:
    async def test_keeps_queued_files_and_deletes_the_rest(
        self, temp_db, tmp_path, monkeypatch,
    ):
        # Own store dir: the default lives in the session-wide data dir,
        # where other tests' queued files would be counted too.
        monkeypatch.setattr(
            torrent_store, "store_dir", lambda: tmp_path / "queued-torrents",
        )
        db = await get_db()
        try:
            queued = await grabs_storage.create_grab(
                db, announce_id=None, mam_torrent_id="1", torrent_name="q",
                category="", author_blob="",
                state=grabs_storage.STATE_PENDING_QUEUE,
            )
            await queue_mod.enqueue(db, queued)
            done = await grabs_storage.create_grab(
                db, announce_id=None, mam_torrent_id="2", torrent_name="d",
                category="", author_blob="", state=grabs_storage.STATE_SUBMITTED,
            )
            keep = torrent_store.save(queued, b"d1:ae")
            stale = torrent_store.save(done, b"d1:be")
            junk = torrent_store.store_dir() / ".99.torrent.tmp"
            junk.write_bytes(b"partial")

            removed = await torrent_store.sweep(db)
        finally:
            await db.close()

        assert removed == 2
        assert keep.exists()
        assert not stale.exists() and not junk.exists()
