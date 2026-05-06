"""
v2.3 ABS sync snapshot + diff-routing tests.

Mirrors `test_calibre_sync_snapshot_diff.py` but with audiobook-specific
fields (narrator, duration_sec, abridged, asin, audio_formats).
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


def _fake_library() -> dict:
    return {
        "abs_base_url": "http://abs:13378",
        "abs_library_id": "lib-x",
        "slug": "test",
        "name": "Test",
    }


def _abs_book(abs_id, title, author="Author", **overrides):
    """Mimics what `_flatten_item` produces."""
    base = {
        "abs_id": abs_id,
        "title": title,
        "authors": [author],
        "isbn": None,
        "asin": None,
        "narrator": None,
        "duration_sec": None,
        "abridged": False,
        "audio_formats": "audiobook",
        "description": None,
        "language": None,
        "publisher": None,
        "pub_date": None,
        "series_name": None,
        "series_index": None,
    }
    base.update(overrides)
    return base


async def _stub_sync(monkeypatch, books):
    """Patch out the network-dependent layers so tests can drive the
    sync purely from a books fixture."""
    from app.library_apps import audiobookshelf as abs_mod
    from app.discovery import audiobookshelf_sync as abs_sync

    async def _fake_iter(self, library_id, page_size=500):
        for b in books:
            # `iter_all_items` yields raw items; the flatten happens
            # downstream. We bypass the flatten by patching it instead.
            yield {"id": b["abs_id"]}

    monkeypatch.setattr(
        abs_mod.AudiobookshelfClient, "iter_all_items", _fake_iter
    )

    async def _fake_get_key():
        return "tok"
    monkeypatch.setattr(abs_mod, "_get_abs_api_key", _fake_get_key)

    book_iter = iter(books)
    def _fake_flatten(item):
        try:
            return next(book_iter)
        except StopIteration:
            return None
    monkeypatch.setattr(abs_sync, "_flatten_item", _fake_flatten)


async def _book_row(abs_id):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT * FROM books WHERE audiobookshelf_id = ?", (abs_id,)
        )).fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def _snapshot_row(abs_id):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT s.* FROM books_abs_snapshot s "
            "JOIN books b ON b.id = s.book_id "
            "WHERE b.audiobookshelf_id = ?", (abs_id,)
        )).fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def _queue_rows():
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT * FROM metadata_review_queue ORDER BY id"
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


class TestAbsSnapshotWrite:
    async def test_new_book_creates_snapshot(self, discovery_db, monkeypatch):
        from app.discovery.audiobookshelf_sync import sync_audiobookshelf

        await _stub_sync(monkeypatch, [
            _abs_book("abs-1", "AudioBookA", "AuthorA",
                      narrator="Reader X", duration_sec=43200.5,
                      asin="B0XYZ", description="Desc",
                      language="eng", publisher="Pub",
                      audio_formats="m4b"),
        ])
        await sync_audiobookshelf(_fake_library())

        snap = await _snapshot_row("abs-1")
        assert snap is not None
        assert snap["title"] == "AudioBookA"
        assert snap["narrator"] == "Reader X"
        assert snap["duration_sec"] == 43200.5
        assert snap["asin"] == "B0XYZ"
        assert snap["description"] == "Desc"
        assert snap["audio_formats"] == "m4b"
        assert snap["abridged"] == 0
        assert snap["synced_at"] > 0
        authors = json.loads(snap["authors_json"])
        assert authors == [{"id": None, "name": "AuthorA"}]


class TestAbsAutoFlow:
    async def test_unedited_audiobook_field_auto_flows(
        self, discovery_db, monkeypatch,
    ):
        from app.discovery.audiobookshelf_sync import sync_audiobookshelf

        await _stub_sync(monkeypatch, [
            _abs_book("abs-1", "T", narrator="Reader A",
                      duration_sec=1000),
        ])
        await sync_audiobookshelf(_fake_library())

        await _stub_sync(monkeypatch, [
            _abs_book("abs-1", "T", narrator="Reader B",
                      duration_sec=2000),
        ])
        await sync_audiobookshelf(_fake_library())

        row = await _book_row("abs-1")
        assert row["narrator"] == "Reader B"
        assert row["duration_sec"] == 2000
        assert await _queue_rows() == []


class TestAbsQueueRouting:
    async def test_user_edited_narrator_routes_to_queue(
        self, discovery_db, monkeypatch,
    ):
        from app.discovery.audiobookshelf_sync import sync_audiobookshelf
        from app.discovery.database import get_db

        await _stub_sync(monkeypatch, [
            _abs_book("abs-1", "T", narrator="Reader Original"),
        ])
        await sync_audiobookshelf(_fake_library())

        # Mark `narrator` as user-edited.
        db = await get_db()
        try:
            await db.execute(
                "UPDATE books SET narrator = 'My Narrator', "
                "user_edited_fields = ? WHERE audiobookshelf_id = 'abs-1'",
                (json.dumps(["narrator"]),),
            )
            await db.commit()
        finally:
            await db.close()

        # ABS changed narrator again.
        await _stub_sync(monkeypatch, [
            _abs_book("abs-1", "T", narrator="Reader Updated"),
        ])
        await sync_audiobookshelf(_fake_library())

        row = await _book_row("abs-1")
        assert row["narrator"] == "My Narrator"

        queue = await _queue_rows()
        narrator_q = [q for q in queue if q["field"] == "narrator"]
        assert len(narrator_q) == 1
        assert narrator_q[0]["source"] == "abs"
        assert narrator_q[0]["old_value"] == "My Narrator"
        assert narrator_q[0]["new_value"] == "Reader Updated"


class TestAbsAbridgedNormalization:
    """ABS emits `abridged` as bool/None; books column stores INT.
    The diff helper must coerce both sides for the comparison."""

    async def test_unchanged_abridged_no_diff(
        self, discovery_db, monkeypatch,
    ):
        from app.discovery.audiobookshelf_sync import sync_audiobookshelf

        await _stub_sync(monkeypatch, [
            _abs_book("abs-1", "T", abridged=False),
        ])
        await sync_audiobookshelf(_fake_library())

        # Re-sync with the same (False/None-ish) value.
        await _stub_sync(monkeypatch, [
            _abs_book("abs-1", "T", abridged=None),
        ])
        await sync_audiobookshelf(_fake_library())

        # No queue row — both flatten to 0.
        assert await _queue_rows() == []
