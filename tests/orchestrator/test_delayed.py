"""
Delayed-torrents folder rotation tests.

When the queue is full and a new grab arrives, the oldest queued
grab should be evicted to disk and the new grab should take its slot.
The evicted grab's .torrent comes from its saved copy in the torrent
store — rotation never fetches from MAM (snatch safety, ADR-0022).
"""
from app.database import get_db
from app.orchestrator import torrent_store
from app.orchestrator.delayed import rotate_oldest_to_delayed
from app.rate_limit import queue as queue_mod
from app.storage import grabs as grabs_storage

TORRENT = b"d4:name4:fakee"


async def _make_queued_grab(
    db, *, mam_id: str, name: str = "Old Book", saved: bool = True,
) -> int:
    gid = await grabs_storage.create_grab(
        db, announce_id=None, mam_torrent_id=mam_id,
        torrent_name=name, category="ebooks fantasy",
        author_blob="Author", state=grabs_storage.STATE_PENDING_QUEUE,
    )
    if saved:
        path = torrent_store.save(gid, TORRENT)
        await grabs_storage.set_state(
            db, gid, grabs_storage.STATE_PENDING_QUEUE,
            torrent_file_path=str(path),
        )
    await queue_mod.enqueue(db, gid)
    return gid


class TestDelayedRotation:
    async def test_rotates_oldest_to_disk(self, temp_db, tmp_path):
        db = await get_db()
        try:
            old_id = await _make_queued_grab(db, mam_id="111")
            saved = (await grabs_storage.get_grab(db, old_id)).torrent_file_path

            delayed = tmp_path / "delayed"
            evicted = await rotate_oldest_to_delayed(
                db, delayed_path=str(delayed),
            )
            assert evicted == old_id

            # The saved bytes moved to the delayed dir.
            files = list(delayed.glob("*.torrent"))
            assert len(files) == 1
            assert files[0].name == f"{old_id}_111.torrent"
            assert files[0].read_bytes() == TORRENT
            assert torrent_store.load(saved) is None

            # Queue now empty.
            assert await queue_mod.size(db) == 0

            # Grab row marked as failed with the reason.
            grab = await grabs_storage.get_grab(db, old_id)
            assert grab.state == grabs_storage.STATE_FAILED_UNKNOWN
            assert "delayed folder" in (grab.failed_reason or "")
        finally:
            await db.close()

    async def test_empty_queue_returns_none(self, temp_db, tmp_path):
        db = await get_db()
        try:
            evicted = await rotate_oldest_to_delayed(
                db, delayed_path=str(tmp_path / "delayed"),
            )
            assert evicted is None
        finally:
            await db.close()

    async def test_no_path_disables(self, temp_db):
        db = await get_db()
        try:
            await _make_queued_grab(db, mam_id="222")

            evicted = await rotate_oldest_to_delayed(db, delayed_path="")
            assert evicted is None
            assert await queue_mod.size(db) == 1
        finally:
            await db.close()

    async def test_missing_saved_bytes_fails_loudly_and_frees_the_slot(
        self, temp_db, tmp_path, monkeypatch,
    ):
        notified: list[str] = []

        async def capture(grab_id, detail):
            notified.append(detail)

        monkeypatch.setattr(torrent_store, "notify_grab_failed", capture)
        db = await get_db()
        try:
            gid = await _make_queued_grab(db, mam_id="333", saved=False)

            delayed = tmp_path / "delayed"
            evicted = await rotate_oldest_to_delayed(
                db, delayed_path=str(delayed),
            )
            assert evicted == gid
            assert not delayed.exists() or list(delayed.iterdir()) == []
            assert await queue_mod.size(db) == 0
            grab = await grabs_storage.get_grab(db, gid)
            assert grab.state == grabs_storage.STATE_FAILED_UNKNOWN
            assert "missing from disk" in (grab.failed_reason or "")
            assert len(notified) == 1
        finally:
            await db.close()

    async def test_write_failure_preserves_queue(self, temp_db, tmp_path):
        db = await get_db()
        try:
            gid = await _make_queued_grab(db, mam_id="444")
            blocker = tmp_path / "not-a-dir"
            blocker.write_text("x")  # mkdir under a file fails

            evicted = await rotate_oldest_to_delayed(
                db, delayed_path=str(blocker / "delayed"),
            )
            assert evicted is None
            # Grab still in queue, untouched, bytes still saved.
            assert await queue_mod.size(db) == 1
            grab = await grabs_storage.get_grab(db, gid)
            assert grab.state == grabs_storage.STATE_PENDING_QUEUE
            assert torrent_store.load(grab.torrent_file_path) == TORRENT
        finally:
            await db.close()
