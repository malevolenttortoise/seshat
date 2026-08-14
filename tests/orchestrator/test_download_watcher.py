"""
Unit tests for the post-download completion detector.

Tests target `check_for_completions()` directly with a temp database
and synthetic qBit snapshots.
"""
from dataclasses import dataclass

from app.database import get_db
from app.orchestrator.download_watcher import (
    TorrentSnap,
    adopt_orphan_torrents,
    check_for_completions,
)
from app.storage import grabs as grabs_storage
from app.storage import pipeline as pipeline_storage


@dataclass
class _FakeQbitTorrent:
    """Stand-in for the real QbitTorrent snapshot fields used by the adopter."""
    hash: str
    name: str
    added_on: int = 0


async def _insert_submitted_grab(db, torrent_id: str, qbit_hash: str) -> int:
    """Insert a grab in submitted state with a qbit_hash."""
    grab_id = await grabs_storage.create_grab(
        db,
        announce_id=None,
        mam_torrent_id=torrent_id,
        torrent_name=f"Book {torrent_id}",
        category="ebooks fantasy",
        author_blob="Test Author",
        state=grabs_storage.STATE_SUBMITTED,
    )
    await grabs_storage.set_state(
        db, grab_id, grabs_storage.STATE_SUBMITTED, qbit_hash=qbit_hash
    )
    return grab_id


class TestCheckForCompletions:
    async def test_detects_uploading_as_complete(self, temp_db):
        db = await get_db()
        try:
            grab_id = await _insert_submitted_grab(db, "111", "hash_aaa")
            snapshot = {"hash_aaa": TorrentSnap(state="uploading", save_path="/dl/book1")}

            events = await check_for_completions(db, snapshot)

            assert len(events) == 1
            assert events[0].grab_id == grab_id
            assert events[0].qbit_hash == "hash_aaa"
            assert events[0].save_path == "/dl/book1"

            # Grab should be in "downloaded" state now.
            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_DOWNLOADED

            # Pipeline run should exist.
            run = await pipeline_storage.find_by_grab_id(db, grab_id)
            assert run is not None
            assert run.state == pipeline_storage.PIPE_STAGED
            assert run.source_path == "/dl/book1"
        finally:
            await db.close()

    async def test_ignores_still_downloading(self, temp_db):
        db = await get_db()
        try:
            await _insert_submitted_grab(db, "222", "hash_bbb")
            snapshot = {"hash_bbb": TorrentSnap(state="downloading", save_path="/dl/book2")}

            events = await check_for_completions(db, snapshot)
            assert len(events) == 0

            # Grab should still be in submitted state.
            grab = await grabs_storage.get_grab(db, 1)
            assert grab.state == grabs_storage.STATE_SUBMITTED
        finally:
            await db.close()

    async def test_ignores_missing_from_qbit(self, temp_db):
        db = await get_db()
        try:
            grab_id = await _insert_submitted_grab(db, "333", "hash_ccc")
            snapshot = {}  # torrent not in qBit

            events = await check_for_completions(db, snapshot)
            assert len(events) == 0
            # An empty snapshot is never evidence of removal — the grab
            # stays submitted rather than being failed wholesale.
            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_SUBMITTED
        finally:
            await db.close()

    async def test_skips_already_processed(self, temp_db):
        db = await get_db()
        try:
            grab_id = await _insert_submitted_grab(db, "444", "hash_ddd")
            # Simulate already having a pipeline run.
            await pipeline_storage.create_run(
                db, grab_id=grab_id, qbit_hash="hash_ddd"
            )
            snapshot = {"hash_ddd": TorrentSnap(state="uploading", save_path="/dl/book4")}

            events = await check_for_completions(db, snapshot)
            assert len(events) == 0
        finally:
            await db.close()

    async def test_detects_multiple_completions(self, temp_db):
        db = await get_db()
        try:
            await _insert_submitted_grab(db, "555", "hash_eee")
            await _insert_submitted_grab(db, "666", "hash_fff")
            snapshot = {
                "hash_eee": TorrentSnap(state="uploading", save_path="/dl/book5"),
                "hash_fff": TorrentSnap(state="pausedUP", save_path="/dl/book6"),
            }

            events = await check_for_completions(db, snapshot)
            assert len(events) == 2
        finally:
            await db.close()

    async def test_various_post_download_states(self, temp_db):
        """All non-downloading qBit states should count as complete."""
        post_dl_states = ["uploading", "forcedUP", "pausedUP", "stalledUP",
                          "checkingUP", "queuedUP", "stoppedUP"]
        db = await get_db()
        try:
            for i, state in enumerate(post_dl_states):
                await _insert_submitted_grab(db, str(700 + i), f"hash_{i}")
            snapshot = {
                f"hash_{i}": TorrentSnap(state=state, save_path=f"/dl/book{i}")
                for i, state in enumerate(post_dl_states)
            }

            events = await check_for_completions(db, snapshot)
            assert len(events) == len(post_dl_states)
        finally:
            await db.close()

    async def test_various_downloading_states_ignored(self, temp_db):
        """All downloading qBit states should NOT trigger completion."""
        dl_states = ["downloading", "forcedDL", "metaDL", "stalledDL",
                     "checkingDL", "queuedDL", "allocating", "moving"]
        db = await get_db()
        try:
            for i, state in enumerate(dl_states):
                await _insert_submitted_grab(db, str(800 + i), f"hash_dl_{i}")
            snapshot = {
                f"hash_dl_{i}": TorrentSnap(state=state, save_path=f"/dl/book{i}")
                for i, state in enumerate(dl_states)
            }

            events = await check_for_completions(db, snapshot)
            assert len(events) == 0
        finally:
            await db.close()


class TestAdoptOrphanTorrents:
    """Fix: manually-added qBit torrents (no grabs row) get adopted so
    the pipeline can process them when they finish."""

    async def test_adopts_unknown_hashes(self, temp_db):
        db = await get_db()
        try:
            torrents = [
                _FakeQbitTorrent(hash="orphan_aaa", name="Manual Book A"),
                _FakeQbitTorrent(hash="orphan_bbb", name="Manual Book B"),
            ]
            adopted = await adopt_orphan_torrents(db, torrents, adoption_cutoff=0)
            assert adopted == 2

            # Both grabs should exist in submitted state with qbit_hash set.
            cursor = await db.execute(
                "SELECT qbit_hash, state, torrent_name, category, mam_torrent_id "
                "FROM grabs ORDER BY id"
            )
            rows = await cursor.fetchall()
            assert len(rows) == 2
            assert rows[0]["qbit_hash"] == "orphan_aaa"
            assert rows[0]["state"] == grabs_storage.STATE_SUBMITTED
            assert rows[0]["torrent_name"] == "Manual Book A"
            assert rows[0]["category"] == "manual_add"
            # mam_torrent_id blank — we didn't pull this from MAM.
            assert rows[0]["mam_torrent_id"] == ""
        finally:
            await db.close()

    async def test_skips_known_hashes(self, temp_db):
        db = await get_db()
        try:
            # Pre-existing grab for this hash.
            await _insert_submitted_grab(db, "mam_111", "known_hash")
            torrents = [
                _FakeQbitTorrent(hash="known_hash", name="Already Tracked"),
                _FakeQbitTorrent(hash="orphan_new", name="New Manual Add"),
            ]
            adopted = await adopt_orphan_torrents(db, torrents, adoption_cutoff=0)
            assert adopted == 1  # only orphan_new

            cursor = await db.execute("SELECT COUNT(*) as cnt FROM grabs")
            row = await cursor.fetchone()
            assert row["cnt"] == 2  # one pre-existing + one adopted
        finally:
            await db.close()

    async def test_empty_list_noop(self, temp_db):
        db = await get_db()
        try:
            adopted = await adopt_orphan_torrents(db, [], adoption_cutoff=0)
            assert adopted == 0
        finally:
            await db.close()

    async def test_cutoff_skips_pre_existing_torrents(self, temp_db):
        """Grandfather line: torrents added before `adoption_cutoff` are
        silently skipped. This is the fix for the cascade bug — without
        it, the first tick after deploying the adopter would re-adopt
        every pre-existing torrent in the watch category (thousands on
        a long-running qBit instance), flooding the review queue.
        """
        db = await get_db()
        try:
            cutoff = 1_700_000_000  # a fixed Unix timestamp
            torrents = [
                _FakeQbitTorrent(
                    hash="old_aaa", name="Pre-existing",
                    added_on=cutoff - 86400,
                ),
                _FakeQbitTorrent(
                    hash="new_bbb", name="Fresh Manual Add",
                    added_on=cutoff + 3600,
                ),
                _FakeQbitTorrent(
                    hash="exact_ccc", name="Added exactly at cutoff",
                    added_on=cutoff,
                ),
            ]
            adopted = await adopt_orphan_torrents(
                db, torrents, adoption_cutoff=cutoff,
            )
            assert adopted == 2  # fresh + exact-boundary, not pre-existing

            cursor = await db.execute(
                "SELECT qbit_hash FROM grabs ORDER BY id"
            )
            rows = await cursor.fetchall()
            hashes = {r["qbit_hash"] for r in rows}
            assert hashes == {"new_bbb", "exact_ccc"}
        finally:
            await db.close()

    async def test_cutoff_zero_disables_filter(self, temp_db):
        """`adoption_cutoff=0` disables the time filter (tests + backward
        compat). All unknown torrents get adopted regardless of added_on.
        """
        db = await get_db()
        try:
            torrents = [
                _FakeQbitTorrent(
                    hash="ancient", name="Very Old",
                    added_on=1,  # effectively "forever ago"
                ),
            ]
            adopted = await adopt_orphan_torrents(
                db, torrents, adoption_cutoff=0,
            )
            assert adopted == 1
        finally:
            await db.close()

    async def test_skips_torrents_without_hash(self, temp_db):
        db = await get_db()
        try:
            torrents = [
                _FakeQbitTorrent(hash="", name="No Hash"),
                _FakeQbitTorrent(hash="orphan_ccc", name="Valid"),
            ]
            adopted = await adopt_orphan_torrents(db, torrents, adoption_cutoff=0)
            assert adopted == 1
        finally:
            await db.close()

    async def test_adopted_row_picked_up_by_completion_check(self, temp_db):
        """Integration: adopt a torrent that's already finished, then the
        completion check on the same tick should fire the pipeline."""
        db = await get_db()
        try:
            torrents = [
                _FakeQbitTorrent(hash="orphan_done", name="Finished Manual Add"),
            ]
            await adopt_orphan_torrents(db, torrents, adoption_cutoff=0)

            snapshot = {
                "orphan_done": TorrentSnap(
                    state="stalledUP", save_path="/dl/finished",
                ),
            }
            events = await check_for_completions(db, snapshot)
            assert len(events) == 1
            assert events[0].qbit_hash == "orphan_done"
            assert events[0].torrent_name == "Finished Manual Add"
        finally:
            await db.close()


class TestFileRaceRetry:
    """The qBit file-move race: pipeline ran before files landed in
    save_path, `_prepare_book` emitted "no file matching", run was
    marked PIPE_FAILED. Auto-retry picks these up on the next tick
    after a cooldown so recovery doesn't require manual DB surgery.
    Reproduces the grab 2779 (Super Sales on Super Heroes) incident
    from Tier 1 UAT.
    """

    async def _setup_failed_run(
        self, db, torrent_id: str, qbit_hash: str,
        error: str = (
            "torrent files unavailable from client; "
            "no file matching 'Test Book' in /downloads/"
        ),
    ) -> tuple[int, int]:
        """Insert a grab in DOWNLOADED state + a failed pipeline_run."""
        grab_id = await _insert_submitted_grab(db, torrent_id, qbit_hash)
        await grabs_storage.set_state(
            db, grab_id, grabs_storage.STATE_DOWNLOADED, qbit_hash=qbit_hash,
        )
        run_id = await pipeline_storage.create_run(
            db, grab_id=grab_id, qbit_hash=qbit_hash, source_path="/dl/x",
        )
        await pipeline_storage.set_state(
            db, run_id, pipeline_storage.PIPE_FAILED, error=error,
        )
        return grab_id, run_id

    async def _backdate_run(self, db, run_id: int, seconds_ago: int) -> None:
        """Rewrite `state_updated_at` to an earlier UTC timestamp so
        the cooldown gate treats the failure as old enough to retry."""
        import datetime as _dt
        past = (
            _dt.datetime.now(tz=_dt.timezone.utc)
            - _dt.timedelta(seconds=seconds_ago)
        ).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "UPDATE pipeline_runs SET state_updated_at = ? WHERE id = ?",
            (past, run_id),
        )
        await db.commit()

    async def _clear_retry_counts(self):
        """Reset the module-level retry-count dict between tests."""
        from app.orchestrator import download_watcher
        download_watcher._file_race_retry_counts.clear()

    async def test_retries_after_cooldown(self, temp_db):
        await self._clear_retry_counts()
        db = await get_db()
        try:
            grab_id, old_run_id = await self._setup_failed_run(
                db, "555", "hash_race"
            )
            await self._backdate_run(db, old_run_id, seconds_ago=60)
            snapshot = {
                "hash_race": TorrentSnap(
                    state="uploading", save_path="/dl/book"
                )
            }

            events = await check_for_completions(db, snapshot)

            assert len(events) == 1
            assert events[0].grab_id == grab_id
            # Old failed run deleted, fresh one created.
            run = await pipeline_storage.find_by_grab_id(db, grab_id)
            assert run is not None
            assert run.id != old_run_id
            assert run.state == pipeline_storage.PIPE_STAGED
        finally:
            await db.close()

    async def test_skipped_within_cooldown(self, temp_db):
        """Failure timestamp < cooldown → no retry this tick."""
        await self._clear_retry_counts()
        db = await get_db()
        try:
            _, old_run_id = await self._setup_failed_run(
                db, "556", "hash_fresh"
            )
            # default state_updated_at is "just now" — within cooldown
            snapshot = {
                "hash_fresh": TorrentSnap(
                    state="uploading", save_path="/dl/book"
                )
            }

            events = await check_for_completions(db, snapshot)

            assert events == []
            # Old run still present, untouched.
            run = await pipeline_storage.get_run(db, old_run_id)
            assert run is not None
            assert run.state == pipeline_storage.PIPE_FAILED
        finally:
            await db.close()

    async def test_respects_max_retry_count(self, temp_db):
        """After MAX_RETRIES, stop retrying even if cooldown has elapsed."""
        from app.orchestrator import download_watcher
        await self._clear_retry_counts()
        db = await get_db()
        try:
            grab_id, _ = await self._setup_failed_run(
                db, "557", "hash_maxed"
            )
            # Simulate already-at-limit counter.
            download_watcher._file_race_retry_counts[grab_id] = (
                download_watcher._FILE_RACE_MAX_RETRIES
            )
            snapshot = {
                "hash_maxed": TorrentSnap(
                    state="uploading", save_path="/dl/book"
                )
            }

            # Cooldown eligible but retry-count exhausted.
            events = await check_for_completions(db, snapshot)
            assert events == []
        finally:
            await db.close()

    async def test_skipped_when_torrent_missing_from_qbit(self, temp_db):
        await self._clear_retry_counts()
        db = await get_db()
        try:
            _, old_run_id = await self._setup_failed_run(
                db, "558", "hash_gone"
            )
            await self._backdate_run(db, old_run_id, seconds_ago=60)

            snapshot = {}  # torrent absent from qBit

            events = await check_for_completions(db, snapshot)
            assert events == []
        finally:
            await db.close()

    async def test_non_file_race_failures_not_retried(self, temp_db):
        """Other pipeline-fail errors (e.g. "no book files found in")
        are NOT file-race; they stay failed for operator review."""
        await self._clear_retry_counts()
        db = await get_db()
        try:
            _, old_run_id = await self._setup_failed_run(
                db, "559", "hash_other",
                error="no book files found in /dl/book",
            )
            await self._backdate_run(db, old_run_id, seconds_ago=60)
            snapshot = {
                "hash_other": TorrentSnap(
                    state="uploading", save_path="/dl/book"
                )
            }

            events = await check_for_completions(db, snapshot)
            assert events == []
        finally:
            await db.close()


class TestMissingTorrentGiveUp:
    """Grabs whose torrent vanished from qBit must not poll forever.

    The snatch ledger already releases the budget slot when a torrent
    disappears; before this sweep existed the `grabs` row stayed
    `submitted` indefinitely and was re-checked on every tick (the
    production log carried ~8,900 repeats of a single grab).
    """

    async def _backdate_submitted(self, db, grab_id: int, hours_ago: float) -> None:
        await db.execute(
            "UPDATE grabs SET submitted_at = datetime('now', ?) WHERE id = ?",
            (f"-{hours_ago} hours", grab_id),
        )
        await db.commit()

    async def test_gives_up_after_grace_period(self, temp_db, monkeypatch):
        import app.orchestrator.download_watcher as dw

        monkeypatch.setattr(
            dw, "load_settings", lambda: {"qbit_missing_grab_grace_hours": 12}
        )
        db = await get_db()
        try:
            grab_id = await _insert_submitted_grab(db, "901", "hash_gone")
            await self._backdate_submitted(db, grab_id, hours_ago=24)
            # Snapshot is non-empty but doesn't contain our hash — qBit
            # is clearly reporting, and this torrent isn't there.
            snapshot = {
                "other_hash": TorrentSnap(state="uploading", save_path="/dl/x")
            }

            events = await check_for_completions(db, snapshot)
            assert events == []

            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_FAILED_TORRENT_GONE
            assert "absent from qBit" in (grab.failed_reason or "")
        finally:
            await db.close()

    async def test_within_grace_period_left_alone(self, temp_db, monkeypatch):
        import app.orchestrator.download_watcher as dw

        monkeypatch.setattr(
            dw, "load_settings", lambda: {"qbit_missing_grab_grace_hours": 12}
        )
        db = await get_db()
        try:
            grab_id = await _insert_submitted_grab(db, "902", "hash_recent")
            await self._backdate_submitted(db, grab_id, hours_ago=1)
            snapshot = {
                "other_hash": TorrentSnap(state="uploading", save_path="/dl/x")
            }

            await check_for_completions(db, snapshot)

            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_SUBMITTED
        finally:
            await db.close()

    async def test_empty_snapshot_never_gives_up(self, temp_db, monkeypatch):
        """A qBit that reports zero torrents must not condemn everything.

        `list_torrents` raising is handled upstream, but a *successful*
        empty response (wrong category, qBit mid-restore) would
        otherwise fail every submitted grab at once.
        """
        import app.orchestrator.download_watcher as dw

        monkeypatch.setattr(
            dw, "load_settings", lambda: {"qbit_missing_grab_grace_hours": 12}
        )
        db = await get_db()
        try:
            grab_id = await _insert_submitted_grab(db, "903", "hash_orphan")
            await self._backdate_submitted(db, grab_id, hours_ago=999)

            await check_for_completions(db, {})

            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_SUBMITTED
        finally:
            await db.close()

    async def test_zero_grace_disables_sweep(self, temp_db, monkeypatch):
        import app.orchestrator.download_watcher as dw

        monkeypatch.setattr(
            dw, "load_settings", lambda: {"qbit_missing_grab_grace_hours": 0}
        )
        db = await get_db()
        try:
            grab_id = await _insert_submitted_grab(db, "904", "hash_disabled")
            await self._backdate_submitted(db, grab_id, hours_ago=999)
            snapshot = {
                "other_hash": TorrentSnap(state="uploading", save_path="/dl/x")
            }

            await check_for_completions(db, snapshot)

            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_SUBMITTED
        finally:
            await db.close()

    async def test_unparseable_submitted_at_left_alone(self, temp_db, monkeypatch):
        """A grab we can't age is a grab we have no business failing."""
        import app.orchestrator.download_watcher as dw

        monkeypatch.setattr(
            dw, "load_settings", lambda: {"qbit_missing_grab_grace_hours": 12}
        )
        db = await get_db()
        try:
            grab_id = await _insert_submitted_grab(db, "905", "hash_nostamp")
            await db.execute(
                "UPDATE grabs SET submitted_at = NULL WHERE id = ?", (grab_id,)
            )
            await db.commit()
            snapshot = {
                "other_hash": TorrentSnap(state="uploading", save_path="/dl/x")
            }

            await check_for_completions(db, snapshot)

            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_SUBMITTED
        finally:
            await db.close()
