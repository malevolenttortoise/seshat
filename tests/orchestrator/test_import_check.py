"""Wave 5a S3 — a CWA drop is checked against Calibre (G121, G129, G131–G133).

CWA imports on its own and reports nothing. From 2026-09-29 to 10-09 it
deleted every patched ebook unimported while Seshat recorded each as
delivered. Every CWA drop now gets an import check: confirmed when a
matching record appears in Calibre's metadata.db, failed after 15 minutes,
with a notification, an "import failed" review (Re-drop / Mark as imported)
and the review's files kept until then.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import state
from app.database import get_db
from app.orchestrator import import_check
from app.orchestrator.pipeline import deliver_reviewed
from app.routers import review as review_router
from app.storage import grabs as grabs_storage
from app.storage import import_checks as checks_storage
from app.storage import pipeline as pipe_storage
from app.storage import review_queue as review_storage
from tests.orchestrator.test_dispatch import _make_deps
from tests.orchestrator.test_review_queue import _make_epub

TITLE = "No One Dies Today"
AUTHOR = "Maoyi Zhou"


@pytest.fixture
def calibre(tmp_path, monkeypatch):
    """A minimal Calibre metadata.db, registered as the ebook library."""
    path = tmp_path / "calibre" / "metadata.db"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, timestamp TEXT, last_modified TEXT);
        CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "books", "content_type": "ebook", "source_db_path": str(path)},
    ])
    return path


def _calibre_add(path: Path, book_id: int, title: str, author: str, *, at: datetime) -> None:
    stamp = at.strftime("%Y-%m-%d %H:%M:%S+00:00")
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO books VALUES (?, ?, ?, ?)", (book_id, title, stamp, stamp))
    conn.execute("INSERT OR IGNORE INTO authors (name) VALUES (?)", (author,))
    aid = conn.execute("SELECT id FROM authors WHERE name = ?", (author,)).fetchone()[0]
    conn.execute("INSERT INTO books_authors_link (book, author) VALUES (?, ?)", (book_id, aid))
    conn.commit()
    conn.close()


@pytest.fixture
def notified(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_emit(event, *, title="", message="", **kwargs):
        sent.append((event, message))

    from app.notifications import bus
    monkeypatch.setattr(bus, "emit", fake_emit)
    return sent


async def _review(db, tmp_path, *, status=review_storage.STATUS_PENDING, title=TITLE):
    grab_id = await grabs_storage.create_grab(
        db, announce_id=None, mam_torrent_id="", torrent_name=title,
        category="Ebooks - Fantasy", author_blob=AUTHOR,
        state=grabs_storage.STATE_PROCESSING,
    )
    run_id = await pipe_storage.create_run(
        db, grab_id=grab_id, qbit_hash="h", state=pipe_storage.PIPE_AWAITING_REVIEW,
    )
    review_dir = tmp_path / "review" / f"grab-{grab_id}"
    _make_epub(review_dir / f"{title}.epub", title=title, author=AUTHOR)
    review_id = await review_storage.create_entry(
        db, grab_id=grab_id, pipeline_run_id=run_id, staged_path=str(review_dir),
        book_filename=f"{title}.epub", book_format="epub",
        metadata={"title": title, "author": AUTHOR}, library_slug="books",
    )
    if status != review_storage.STATUS_PENDING:
        await review_storage.set_status(db, review_id, status)
    return grab_id, run_id, review_id, review_dir


async def _deliver(db, review_id, ingest: Path, *, sink="cwa", folder: Path = None):
    return await deliver_reviewed(
        db, review_id=review_id, default_sink=sink, calibre_library_path="",
        folder_sink_path=str(folder or ingest), cwa_ingest_path=str(ingest),
        cwa_min_inter_book_seconds=0,
    )


async def _check(db, review_id) -> checks_storage.ImportCheck:
    return await checks_storage.latest_for_review(db, review_id)


def _later(check, minutes: float) -> datetime:
    return import_check._utc(check.dropped_at) + timedelta(minutes=minutes)


# ─── recording the drop ──────────────────────────────────────


class TestDropRecordsACheck:
    async def test_cwa_drop_records_a_check_and_keeps_the_review_files(
        self, temp_db, tmp_path,
    ):
        db = await get_db()
        try:
            grab_id, run_id, review_id, review_dir = await _review(db, tmp_path)
            ingest = tmp_path / "ingest"

            assert await _deliver(db, review_id, ingest) is True

            check = await _check(db, review_id)
            assert check.state == checks_storage.STATE_PENDING
            assert (check.grab_id, check.pipeline_run_id) == (grab_id, run_id)
            assert check.library_slug == "books"
            assert Path(check.drop_path).parent == ingest
            assert (check.title, check.authors) == (TITLE, [AUTHOR])
            assert review_dir.exists()  # kept for a Re-drop until confirmed
        finally:
            await db.close()

    async def test_other_sinks_record_nothing_and_clean_up(self, temp_db, tmp_path):
        db = await get_db()
        try:
            _, _, review_id, review_dir = await _review(db, tmp_path)

            assert await _deliver(db, review_id, tmp_path / "ingest", sink="folder",
                                  folder=tmp_path / "library") is True

            assert await _check(db, review_id) is None
            assert not review_dir.exists()
        finally:
            await db.close()

    async def test_a_sink_pending_review_is_delivered_by_the_retry(
        self, temp_db, tmp_path,
    ):
        """G131: the review-timeout retry hands `sink_pending` rows to
        deliver_reviewed, which used to return early unless `pending`."""
        db = await get_db()
        try:
            _, _, review_id, _ = await _review(
                db, tmp_path, status=review_storage.STATUS_SINK_PENDING,
            )
            assert await _deliver(db, review_id, tmp_path / "ingest") is True
            row = await review_storage.get_entry(db, review_id)
            assert row.status == review_storage.STATUS_DELIVERED
        finally:
            await db.close()


# ─── the check loop ──────────────────────────────────────────


class TestCheckLoop:
    async def test_confirmed_by_title_removes_the_kept_files(
        self, temp_db, tmp_path, calibre,
    ):
        db = await get_db()
        try:
            _, _, review_id, review_dir = await _review(db, tmp_path)
            await _deliver(db, review_id, tmp_path / "ingest")
            check = await _check(db, review_id)
        finally:
            await db.close()
        _calibre_add(calibre, 4837, TITLE, "Someone Else", at=_later(check, 0.2))

        counts = await import_check.tick(now=_later(check, 1))

        assert counts["confirmed"] == 1
        db = await get_db()
        try:
            done = await _check(db, review_id)
        finally:
            await db.close()
        assert (done.state, done.calibre_book_id) == (checks_storage.STATE_CONFIRMED, 4837)
        assert not review_dir.exists()

    async def test_an_author_match_confirms_when_cwa_rewrote_the_title(
        self, temp_db, tmp_path, calibre,
    ):
        db = await get_db()
        try:
            _, _, review_id, _ = await _review(db, tmp_path)
            await _deliver(db, review_id, tmp_path / "ingest")
            check = await _check(db, review_id)
        finally:
            await db.close()
        _calibre_add(calibre, 9, "Frontline Zero 4", AUTHOR, at=_later(check, 0.2))

        assert (await import_check.tick(now=_later(check, 1)))["confirmed"] == 1

    async def test_one_record_confirms_one_drop(self, temp_db, tmp_path, calibre):
        """Three books by one author dropped seconds apart: a record that
        confirmed one can't confirm another by author alone."""
        db = await get_db()
        try:
            ids = []
            for title in ("Lines of Defiance", "A Battlefield of One"):
                _, _, rid, _ = await _review(db, tmp_path, title=title)
                await _deliver(db, rid, tmp_path / "ingest")
                ids.append(rid)
            first = await _check(db, ids[0])
        finally:
            await db.close()
        _calibre_add(calibre, 1, "Lines of Defiance", AUTHOR, at=_later(first, 0.1))

        counts = await import_check.tick(now=_later(first, 1))

        assert (counts["confirmed"], counts["waiting"]) == (1, 1)

    async def test_a_record_from_before_the_drop_doesnt_count(
        self, temp_db, tmp_path, calibre,
    ):
        db = await get_db()
        try:
            _, _, review_id, _ = await _review(db, tmp_path)
            await _deliver(db, review_id, tmp_path / "ingest")
            check = await _check(db, review_id)
        finally:
            await db.close()
        _calibre_add(calibre, 5, TITLE, AUTHOR, at=_later(check, -60))

        assert (await import_check.tick(now=_later(check, 1)))["waiting"] == 1

    @pytest.mark.parametrize("file_left, reason", [
        (True, import_check.STILL_IN_INGEST),
        (False, import_check.TAKEN_NOT_IMPORTED),
    ])
    async def test_failed_at_fifteen_minutes(
        self, temp_db, tmp_path, calibre, notified, file_left, reason,
    ):
        db = await get_db()
        try:
            grab_id, run_id, review_id, review_dir = await _review(db, tmp_path)
            await _deliver(db, review_id, tmp_path / "ingest")
            check = await _check(db, review_id)
        finally:
            await db.close()
        if not file_left:
            Path(check.drop_path).unlink()  # CWA took it and imported nothing

        assert (await import_check.tick(now=_later(check, 14)))["waiting"] == 1
        assert (await import_check.tick(now=_later(check, 15)))["failed"] == 1

        db = await get_db()
        try:
            row = await review_storage.get_entry(db, review_id)
            assert (row.status, row.decision_note) == (
                review_storage.STATUS_IMPORT_FAILED, reason,
            )
            assert (await grabs_storage.get_grab(db, grab_id)).state == \
                grabs_storage.STATE_PROCESSING
            run = await pipe_storage.get_run(db, run_id)
            assert run.state == pipe_storage.PIPE_FAILED and reason in run.error
            assert (await _check(db, review_id)).state == checks_storage.STATE_FAILED
        finally:
            await db.close()
        assert review_dir.exists()
        assert [n for n in notified if n[0] == "pipeline.import_failed"] == [
            ("pipeline.import_failed", f"{TITLE}\n{reason}")
        ]

    async def test_an_unreadable_calibre_library_fails_loudly(
        self, temp_db, tmp_path, monkeypatch, notified,
    ):
        monkeypatch.setattr(state, "_discovered_libraries", [
            {"slug": "books", "content_type": "ebook",
             "source_db_path": str(tmp_path / "nowhere" / "metadata.db")},
        ])
        db = await get_db()
        try:
            _, _, review_id, _ = await _review(db, tmp_path)
            await _deliver(db, review_id, tmp_path / "ingest")
            check = await _check(db, review_id)
        finally:
            await db.close()

        await import_check.tick(now=_later(check, 16))

        db = await get_db()
        try:
            row = await review_storage.get_entry(db, review_id)
        finally:
            await db.close()
        assert row.status == review_storage.STATUS_IMPORT_FAILED
        assert row.decision_note.startswith("couldn't read Calibre's library")


# ─── Re-drop and Mark as imported ────────────────────────────


@pytest.fixture
def dispatcher(tmp_path, monkeypatch):
    deps = dataclasses.replace(
        _make_deps(), default_sink="cwa", cwa_ingest_path=str(tmp_path / "ingest"),
        cwa_min_inter_book_seconds=0,
    )
    monkeypatch.setattr(state, "dispatcher", deps)
    return deps


async def _failed_review(tmp_path, *, drop_left: bool):
    db = await get_db()
    try:
        grab_id, run_id, review_id, review_dir = await _review(db, tmp_path)
        await _deliver(db, review_id, tmp_path / "ingest")
        check = await _check(db, review_id)
    finally:
        await db.close()
    if not drop_left:
        Path(check.drop_path).unlink()
    await import_check.tick(now=_later(check, 16))
    return grab_id, run_id, review_id, review_dir, check


class TestReviewActions:
    async def test_list_shows_import_failures_first(
        self, temp_db, tmp_path, calibre, notified,
    ):
        _, _, failed_id, _, _ = await _failed_review(tmp_path, drop_left=False)
        db = await get_db()
        try:
            _, _, pending_id, _ = await _review(db, tmp_path, title="Another Book")
        finally:
            await db.close()

        listing = await review_router.list_pending()

        assert [i.id for i in listing.items] == [failed_id, pending_id]
        assert listing.items[0].status == review_storage.STATUS_IMPORT_FAILED
        assert listing.pending_count == 1

    async def test_redrop_removes_the_stale_drop_and_delivers_again(
        self, temp_db, tmp_path, calibre, notified, dispatcher,
    ):
        _, _, review_id, review_dir, old = await _failed_review(tmp_path, drop_left=True)
        assert Path(old.drop_path).exists()

        result = await review_router.redrop(review_id)

        assert result.ok and result.status == review_storage.STATUS_DELIVERED
        drops = sorted(p.name for p in (tmp_path / "ingest").iterdir())
        assert drops == [Path(old.drop_path).name]  # one file, not `…_1.epub`
        db = await get_db()
        try:
            new = await _check(db, review_id)
        finally:
            await db.close()
        assert new.id != old.id and new.state == checks_storage.STATE_PENDING
        assert review_dir.exists()

    async def test_redrop_never_deletes_outside_the_ingest_folder(self, tmp_path):
        outside = tmp_path / "library" / "book.epub"
        outside.parent.mkdir()
        outside.write_bytes(b"keep")
        review_router._remove_stale_drop(outside, str(tmp_path / "ingest"))
        assert outside.read_bytes() == b"keep"

    async def test_redrop_refuses_a_review_that_didnt_fail(
        self, temp_db, tmp_path, dispatcher,
    ):
        db = await get_db()
        try:
            _, _, review_id, _ = await _review(db, tmp_path)
        finally:
            await db.close()
        result = await review_router.redrop(review_id)
        assert result.ok is False

    async def test_mark_as_imported_closes_it_as_delivered(
        self, temp_db, tmp_path, calibre, notified,
    ):
        grab_id, run_id, review_id, review_dir, _ = await _failed_review(
            tmp_path, drop_left=False,
        )

        result = await review_router.mark_imported(review_id)

        assert result.ok and result.status == review_storage.STATUS_DELIVERED
        db = await get_db()
        try:
            assert (await grabs_storage.get_grab(db, grab_id)).state == \
                grabs_storage.STATE_COMPLETE
            assert (await pipe_storage.get_run(db, run_id)).state == \
                pipe_storage.PIPE_COMPLETE
        finally:
            await db.close()
        assert not review_dir.exists()
        assert not any((tmp_path / "ingest").iterdir())  # no drop
