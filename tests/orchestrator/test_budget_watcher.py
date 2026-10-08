"""
Unit tests for the snatch budget watcher loop.

Tests target the `tick()` function rather than `run_loop()` because
the loop is just `tick + sleep + repeat` — the interesting behavior
is all in the tick. The smoke test in `tests/test_lifespan_smoke.py`
exercises `run_loop()` end-to-end.

Coverage targets:
  - tick() reconciles seedtime: rows above threshold get released
  - tick() reconciles removal: rows missing from qBit get released
  - tick() pops from pending_queue when budget has room
  - tick() does NOT pop when budget is still full after reconcile
  - tick() drains the queue until budget fills again
  - Pops submit the SAVED .torrent bytes and never call fetch_torrent
    (snatch safety, ADR-0022): missing file → fail loudly; removed
    from MAM → fail as torrent-gone; liveness unreachable → hold the
    queue; qBit unreachable → put the grab back where it was
  - Pop failure (qBit rejected) marks the grab failed
  - tick() captures unexpected exceptions in TickResult.error
"""
from typing import Optional

import pytest

from app.clients.base import AddResult, TorrentInfo
from app.database import get_db
from app.filter.gate import FilterConfig
from app.orchestrator import torrent_store
from app.orchestrator.budget_watcher import tick
from app.orchestrator.dispatch import DispatcherDeps
from app.rate_limit import ledger as ledger_mod
from app.rate_limit import queue as queue_mod
from app.storage import grabs as grabs_storage
from tests.fake_mam import MINIMAL_BENCODED_TORRENT
from tests.rate_limit._helpers import insert_dummy_grab


# ─── Fakes ───────────────────────────────────────────────────


class _FakeQbit:
    """Records add_torrent calls and returns programmable list_torrents."""

    def __init__(
        self,
        *,
        torrents: list[TorrentInfo] = None,
        add_result: Optional[AddResult] = None,
    ):
        self._torrents = torrents or []
        self.add_result = add_result or AddResult(success=True)
        self.add_calls: list[dict] = []

    async def login(self) -> bool:
        return True

    async def add_torrent(
        self,
        torrent_bytes: bytes,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> AddResult:
        self.add_calls.append(
            {"size": len(torrent_bytes), "category": category, "tags": tags}
        )
        return self.add_result

    async def list_torrents(
        self, category: Optional[str] = None
    ) -> list[TorrentInfo]:
        return list(self._torrents)

    async def get_torrent(self, torrent_hash: str) -> Optional[TorrentInfo]:
        return None

    async def aclose(self) -> None:
        return None


def _never_fetch():
    """fetch_torrent fake for the watcher: a pop must never reach MAM's
    download endpoint, so any call is recorded and fails the test."""
    calls: list[tuple[str, str]] = []

    async def fake_fetch(torrent_id: str, token: str, **kwargs):
        calls.append((torrent_id, token))
        raise AssertionError("budget watcher fetched a .torrent from MAM")

    fake_fetch.calls = calls  # type: ignore[attr-defined]
    return fake_fetch


def _make_deps(
    *,
    qbit: _FakeQbit = None,
    budget_cap: int = 200,
) -> DispatcherDeps:
    return DispatcherDeps(
        filter_config=FilterConfig(
            allowed_categories=frozenset(),
            allowed_authors=frozenset(),
            ignored_authors=frozenset(),
        ),
        mam_token="test",
        qbit_category="mam-complete",
        budget_cap=budget_cap,
        queue_max=100,
        queue_mode_enabled=True,
        seed_seconds_required=72 * 3600,
        db_factory=get_db,
        fetch_torrent=_never_fetch(),
        qbit=qbit or _FakeQbit(),
    )


@pytest.fixture(autouse=True)
def mam_liveness(monkeypatch):
    """What the pre-submit liveness check reports; "ok" unless a test
    says otherwise. Records the torrent IDs it was asked about."""
    box = {"answer": "ok", "asked": []}

    async def fake_check(torrent_id, token):
        box["asked"].append(torrent_id)
        return box["answer"]

    monkeypatch.setattr(torrent_store, "check_still_on_mam", fake_check)
    return box


async def _queue_saved(
    db, *, torrent_id: str = "1", data: bytes = MINIMAL_BENCODED_TORRENT,
) -> int:
    """A queued grab the way the dispatcher leaves one: bytes saved in
    the torrent store, path stamped on the row, row in pending_queue."""
    grab_id = await insert_dummy_grab(
        db, torrent_id=torrent_id, state=grabs_storage.STATE_PENDING_QUEUE,
    )
    path = torrent_store.save(grab_id, data)
    await grabs_storage.set_state(
        db, grab_id, grabs_storage.STATE_PENDING_QUEUE,
        torrent_file_path=str(path),
    )
    await queue_mod.enqueue(db, grab_id)
    return grab_id


def _info(hash_: str, seeding_seconds: int) -> TorrentInfo:
    return TorrentInfo(
        hash=hash_,
        name=f"name-{hash_}",
        category="mam-complete",
        state="uploading",
        seeding_seconds=seeding_seconds,
        save_path="/x",
        added_on=1,
    )


# ─── Reconcile phase ─────────────────────────────────────────


class TestReconcile:
    async def test_seedtime_release(self, temp_db):
        # Pre-populate ledger with one grab past threshold per qBit.
        db = await get_db()
        try:
            grab_id = await insert_dummy_grab(db)
            await ledger_mod.record_grab(db, grab_id, "h1")
        finally:
            await db.close()

        qbit = _FakeQbit(torrents=[_info("h1", 80 * 3600)])
        deps = _make_deps(qbit=qbit)

        result = await tick(deps)

        assert result.qbit_torrents_seen == 1
        assert result.seedtime_released == 1
        assert result.removed_released == 0
        assert result.error is None

        db = await get_db()
        try:
            assert await ledger_mod.count_active(db) == 0
        finally:
            await db.close()

    async def test_removal_release(self, temp_db):
        db = await get_db()
        try:
            grab_id = await insert_dummy_grab(db)
            await ledger_mod.record_grab(db, grab_id, "h_gone")
        finally:
            await db.close()

        # qBit lists it once, then reports nothing — user removed it.
        qbit = _FakeQbit(torrents=[_info("h_gone", 3600)])
        deps = _make_deps(qbit=qbit)
        await tick(deps)
        qbit._torrents = []

        result = await tick(deps)

        assert result.removed_released == 1
        db = await get_db()
        try:
            assert await ledger_mod.count_active(db) == 0
        finally:
            await db.close()


# ─── Pop phase ───────────────────────────────────────────────


class TestPopFromQueue:
    async def test_pops_when_budget_has_room(self, temp_db):
        # Pre-populate the queue with one grab.
        db = await get_db()
        try:
            grab_id = await _queue_saved(db)
            assert await queue_mod.size(db) == 1
            saved = (await grabs_storage.get_grab(db, grab_id)).torrent_file_path
        finally:
            await db.close()

        qbit = _FakeQbit()  # default add_result success
        deps = _make_deps(qbit=qbit)
        result = await tick(deps)

        assert result.queue_pops_attempted == 1
        assert result.queue_pops_submitted == 1
        assert result.queue_pops_failed == 0
        assert len(qbit.add_calls) == 1
        assert qbit.add_calls[0]["size"] == len(MINIMAL_BENCODED_TORRENT)
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]

        db = await get_db()
        try:
            assert await queue_mod.size(db) == 0
            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_SUBMITTED
            assert grab.qbit_hash is not None
            # Ledger should now have the new entry
            assert await ledger_mod.count_active(db) == 1
        finally:
            await db.close()
        # Submitted → the saved copy (it carries the passkey) is gone.
        assert torrent_store.load(saved) is None

    async def test_does_not_pop_when_budget_full(self, temp_db):
        # Fill the budget to exactly cap, then verify nothing pops.
        db = await get_db()
        try:
            for i in range(3):
                grab_id = await insert_dummy_grab(db, torrent_id=str(i))
                await ledger_mod.record_grab(db, grab_id, f"h{i}")
            # And queue one
            await _queue_saved(db, torrent_id="queued")
        finally:
            await db.close()

        # Cap = 3, all already in budget. qBit reports them all
        # well below threshold so reconcile doesn't release any.
        qbit = _FakeQbit(
            torrents=[
                _info("h0", 100),
                _info("h1", 100),
                _info("h2", 100),
            ]
        )
        deps = _make_deps(qbit=qbit, budget_cap=3)
        result = await tick(deps)

        assert result.queue_pops_attempted == 0
        assert qbit.add_calls == []
        db = await get_db()
        try:
            assert await queue_mod.size(db) == 1
            assert await ledger_mod.count_active(db) == 3
        finally:
            await db.close()

    async def test_drains_queue_after_reconcile_releases_room(self, temp_db):
        # Combined: ledger releases 2 rows, queue has 3 entries,
        # budget cap is 3 — so 2 entries should pop, 1 stays queued.
        db = await get_db()
        try:
            for i in range(3):
                grab_id = await insert_dummy_grab(db, torrent_id=f"active{i}")
                await ledger_mod.record_grab(db, grab_id, f"active{i}")
            queued_ids = []
            for i in range(3):
                queued_ids.append(await _queue_saved(db, torrent_id=f"q{i}"))
        finally:
            await db.close()

        # qBit reports two past threshold, one fresh — reconcile
        # will release the two and free 2 budget slots.
        qbit = _FakeQbit(
            torrents=[
                _info("active0", 80 * 3600),
                _info("active1", 80 * 3600),
                _info("active2", 100),
            ]
        )
        deps = _make_deps(qbit=qbit, budget_cap=3)
        result = await tick(deps)

        assert result.seedtime_released == 2
        assert result.queue_pops_submitted == 2
        assert len(qbit.add_calls) == 2

        db = await get_db()
        try:
            # 1 leftover queued + 2 newly submitted + 1 still active = 3
            assert await queue_mod.size(db) == 1
            assert await ledger_mod.count_active(db) == 3
        finally:
            await db.close()


# ─── Pop failure paths ───────────────────────────────────────


class TestPopFailures:
    async def test_missing_saved_file_fails_loudly_without_fetch(
        self, temp_db, monkeypatch,
    ):
        notified: list[str] = []

        async def capture(grab_id, detail):
            notified.append(detail)

        monkeypatch.setattr(torrent_store, "notify_grab_failed", capture)
        db = await get_db()
        try:
            # Queued the pre-Phase-0 way: no bytes saved anywhere.
            grab_id = await insert_dummy_grab(
                db, state=grabs_storage.STATE_PENDING_QUEUE
            )
            await queue_mod.enqueue(db, grab_id)
        finally:
            await db.close()

        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)
        result = await tick(deps)

        assert result.queue_pops_attempted == 1
        assert result.queue_pops_failed == 1
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert qbit.add_calls == []
        assert len(notified) == 1 and "missing from disk" in notified[0]
        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_FAILED_UNKNOWN
            assert "not re-fetched" in (grab.failed_reason or "")
            assert await queue_mod.size(db) == 0
        finally:
            await db.close()

    async def test_torrent_removed_from_mam_is_failed_not_submitted(
        self, temp_db, mam_liveness, monkeypatch,
    ):
        notified: list[str] = []

        async def capture(grab_id, detail):
            notified.append(detail)

        monkeypatch.setattr(torrent_store, "notify_grab_failed", capture)
        mam_liveness["answer"] = "removed"
        db = await get_db()
        try:
            grab_id = await _queue_saved(db, torrent_id="4242")
            saved = (await grabs_storage.get_grab(db, grab_id)).torrent_file_path
        finally:
            await db.close()

        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)
        result = await tick(deps)

        assert mam_liveness["asked"] == ["4242"]
        assert result.queue_pops_failed == 1
        assert qbit.add_calls == []
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert len(notified) == 1 and "removed from MAM" in notified[0]
        assert torrent_store.load(saved) is None
        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_FAILED_TORRENT_GONE
            assert await queue_mod.size(db) == 0
        finally:
            await db.close()

    async def test_liveness_unreachable_holds_the_whole_queue(
        self, temp_db, mam_liveness,
    ):
        mam_liveness["answer"] = "unreachable"
        db = await get_db()
        try:
            queued = [
                await _queue_saved(db, torrent_id="1"),
                await _queue_saved(db, torrent_id="2"),
            ]
        finally:
            await db.close()

        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)
        result = await tick(deps)

        # Checked the head once, then stopped: nothing popped or failed.
        assert len(mam_liveness["asked"]) == 1
        assert result.queue_pops_attempted == 0
        assert qbit.add_calls == []
        db = await get_db()
        try:
            assert await queue_mod.size(db) == 2
            for gid in queued:
                grab = await grabs_storage.get_grab(db, gid)
                assert grab.state == grabs_storage.STATE_PENDING_QUEUE
                assert torrent_store.load(grab.torrent_file_path) is not None
        finally:
            await db.close()

    async def test_qbit_unreachable_puts_grab_back_in_place(self, temp_db):
        db = await get_db()
        try:
            first = await _queue_saved(db, torrent_id="1")
            await _queue_saved(db, torrent_id="2")
            before = await queue_mod.list_all(db)
        finally:
            await db.close()

        qbit = _FakeQbit(
            add_result=AddResult(
                success=False,
                failure_kind="network_error",
                failure_detail="connection refused",
            )
        )
        deps = _make_deps(qbit=qbit)
        result = await tick(deps)

        # One attempt, then the drain stops — the next grab would hit
        # the same dead client.
        assert len(qbit.add_calls) == 1
        assert result.queue_pops_submitted == 0
        assert result.queue_pops_failed == 0
        db = await get_db()
        try:
            assert await queue_mod.list_all(db) == before
            grab = await grabs_storage.get_grab(db, first)
            assert grab.state == grabs_storage.STATE_PENDING_QUEUE
            assert torrent_store.load(grab.torrent_file_path) is not None
        finally:
            await db.close()

    async def test_qbit_failure_marks_grab(self, temp_db):
        db = await get_db()
        try:
            grab_id = await _queue_saved(db)
        finally:
            await db.close()

        qbit = _FakeQbit(
            add_result=AddResult(
                success=False,
                failure_kind="rejected",
                failure_detail="HTTP 415",
            )
        )
        deps = _make_deps(qbit=qbit)
        result = await tick(deps)

        assert result.queue_pops_failed == 1
        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_FAILED_QBIT_REJECTED
            assert torrent_store.load(grab.torrent_file_path) is None
        finally:
            await db.close()


# ─── Error handling ──────────────────────────────────────────


class TestErrorHandling:
    async def test_qbit_list_exception_captured(self, temp_db):
        class _BrokenQbit(_FakeQbit):
            async def list_torrents(self, category=None):
                raise RuntimeError("simulated qBit outage")

        deps = _make_deps(qbit=_BrokenQbit())
        result = await tick(deps)

        assert result.error is not None
        assert "RuntimeError" in result.error
        assert "simulated qBit outage" in result.error


# ─── Issue 09: the ledger stops losing torrents; MAM's count as floor ──


class _FailingListQbit(_FakeQbit):
    """A client whose list call failed (auth / network / bad answer)."""

    async def list_torrents_checked(self, category=None):
        return None


class TestFailedListLeavesTheLedgerAlone:
    async def test_no_release_and_no_pop(self, temp_db, mam_liveness):
        db = await get_db()
        try:
            for i in range(2):
                gid = await insert_dummy_grab(db, torrent_id=str(i))
                await ledger_mod.record_grab(db, gid, f"h{i}")
                await ledger_mod.update_seeding(db, f"h{i}", 3600)
            await _queue_saved(db, torrent_id="queued")
        finally:
            await db.close()

        qbit = _FailingListQbit()
        result = await tick(_make_deps(qbit=qbit))

        assert result.error is not None
        assert result.removed_released == 0
        assert result.qbit_reachable is False
        assert result.queue_pops_attempted == 0
        assert qbit.add_calls == []
        assert mam_liveness["asked"] == []
        db = await get_db()
        try:
            assert await ledger_mod.count_active(db) == 2
            assert await queue_mod.size(db) == 1
        finally:
            await db.close()

    async def test_an_empty_list_that_is_real_still_releases(self, temp_db):
        db = await get_db()
        try:
            gid = await insert_dummy_grab(db)
            await ledger_mod.record_grab(db, gid, "h1")
            await ledger_mod.update_seeding(db, "h1", 3600)
        finally:
            await db.close()

        class _EmptyQbit(_FakeQbit):
            async def list_torrents_checked(self, category=None):
                return []

        result = await tick(_make_deps(qbit=_EmptyQbit()))
        assert result.removed_released == 1


class TestNeverSeenGrace:
    async def test_a_row_qbit_never_listed_keeps_counting(self, temp_db):
        db = await get_db()
        try:
            gid = await insert_dummy_grab(db)
            await ledger_mod.record_grab(db, gid, "h_new")
        finally:
            await db.close()

        result = await tick(_make_deps(qbit=_FakeQbit(torrents=[])))

        assert result.removed_released == 0
        db = await get_db()
        try:
            assert await ledger_mod.count_active(db) == 1
        finally:
            await db.close()

    async def test_released_once_the_grace_has_passed(self, temp_db):
        db = await get_db()
        try:
            gid = await insert_dummy_grab(db)
            await ledger_mod.record_grab(db, gid, "h_new")
            await db.execute(
                "UPDATE grabs SET submitted_at = datetime('now', '-13 hours') WHERE id = ?",
                (gid,),
            )
            await db.commit()
        finally:
            await db.close()

        result = await tick(_make_deps(qbit=_FakeQbit(torrents=[])))
        assert result.removed_released == 1


class TestMamFloorInTheWatcher:
    async def test_mam_count_at_cap_stops_the_drain(self, temp_db, mam_liveness):
        from app.rate_limit import mam_floor
        from app.rate_limit.mam_floor import MamSnatchSummary

        db = await get_db()
        try:
            await _queue_saved(db, torrent_id="queued")
        finally:
            await db.close()
        # Seshat's own ledger is empty; MAM counts 5 against a limit of 5.
        mam_floor.record(MamSnatchSummary(5, 5, 1, None))

        qbit = _FakeQbit()
        result = await tick(_make_deps(qbit=qbit))

        assert result.queue_pops_attempted == 0
        assert qbit.add_calls == []
        assert mam_liveness["asked"] == []

    async def test_reads_mam_at_most_once_an_hour(self, temp_db, monkeypatch):
        from app.mam import user_status

        calls = []

        async def fake_status(token=None, ttl=300):
            calls.append(token)
            raise user_status.UserStatusError("network error: refused")

        monkeypatch.setattr(user_status, "get_user_status", fake_status)
        deps = _make_deps()
        await tick(deps)
        await tick(deps)
        assert calls == ["test"]
