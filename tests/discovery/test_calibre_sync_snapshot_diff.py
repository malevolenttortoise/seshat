"""
v2.3 calibre_sync snapshot + diff-routing tests.

Verifies the dual-source-of-truth flow for Calibre sync:
- Every sync writes the books_calibre_snapshot row (full overwrite).
- For existing books, per-field diffs route to either auto-flow
  (UPDATE the books column directly) or the metadata_review_queue
  based on the book's user_edited_fields JSON array.
- New books are INSERTed with Calibre values + an empty
  user_edited_fields, plus a snapshot row.
- Cover-path NULL from Calibre doesn't blow away an existing cover.
"""
from __future__ import annotations

import json

import pytest


def _book(book_id, title, author_name="Author", author_id=100,
          series_name=None, series_id=None, series_index=1.0,
          description=None, tags=None, rating=None, isbn=None,
          cover_path=None, language=None, publisher=None,
          formats=None, pubdate="2024-01-01"):
    return {
        "book_id": book_id,
        "title": title,
        "pubdate": pubdate,
        "series_index": series_index,
        "book_path": f"{author_name}/{title}",
        "cover_path": cover_path,
        "isbn": isbn,
        "authors": [{"id": author_id, "name": author_name,
                     "sort": author_name}],
        "series": ([{"id": series_id, "name": series_name}]
                   if series_name else []),
        "tags": tags,
        "rating": rating,
        "description": description,
        "language": language,
        "publisher": publisher,
        "formats": formats,
    }


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


async def _book_row(book_id):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT * FROM books WHERE id = ?", (book_id,)
        )).fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def _snapshot_row(book_id):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT * FROM books_calibre_snapshot WHERE book_id = ?",
            (book_id,),
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


class TestSnapshotWrite:
    """Snapshot table is populated on every Calibre sync."""

    async def test_new_book_creates_snapshot(self, discovery_db, monkeypatch):
        from app.discovery import calibre_sync

        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(1, "BookA", "AuthorA", author_id=100,
                      description="Desc", tags="t1,t2", rating=8,
                      language="eng", publisher="Pub", isbn="978-X",
                      formats="epub"),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        # Locate the inserted books row.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            br = await (await db.execute(
                "SELECT id FROM books WHERE calibre_id = 1"
            )).fetchone()
            book_id = br["id"]
        finally:
            await db.close()

        snap = await _snapshot_row(book_id)
        assert snap is not None
        assert snap["title"] == "BookA"
        assert snap["description"] == "Desc"
        assert snap["tags"] == "t1,t2"
        assert snap["rating"] == 8
        assert snap["isbn"] == "978-X"
        assert snap["formats"] == "epub"
        assert snap["synced_at"] > 0
        # authors_json is a denormalized array, not an FK.
        authors = json.loads(snap["authors_json"])
        assert authors == [{"id": 100, "name": "AuthorA", "sort": "AuthorA"}]

    async def test_resync_overwrites_snapshot(self, discovery_db, monkeypatch):
        from app.discovery import calibre_sync

        # Initial sync.
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(1, "BookA", description="Old desc"),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        # Calibre's description changed.
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(1, "BookA", description="New desc"),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        from app.discovery.database import get_db
        db = await get_db()
        try:
            br = await (await db.execute(
                "SELECT id FROM books WHERE calibre_id = 1"
            )).fetchone()
        finally:
            await db.close()

        snap = await _snapshot_row(br["id"])
        # Snapshot reflects the latest Calibre value.
        assert snap["description"] == "New desc"


class TestAutoFlow:
    """Calibre changes auto-flow into books when the user hasn't edited
    that field."""

    async def test_unedited_field_auto_flows(self, discovery_db, monkeypatch):
        from app.discovery import calibre_sync

        # First sync seeds the row.
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(1, "BookA", description="Old desc", tags="old"),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        # Second sync brings new Calibre values; user_edited_fields
        # is empty so both should auto-flow.
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(1, "BookA", description="New desc", tags="new"),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        from app.discovery.database import get_db
        db = await get_db()
        try:
            br = await (await db.execute(
                "SELECT description, tags FROM books WHERE calibre_id = 1"
            )).fetchone()
        finally:
            await db.close()

        assert br["description"] == "New desc"
        assert br["tags"] == "new"
        # No queue rows since both auto-flowed.
        assert await _queue_rows() == []


class TestQueueRouting:
    """Calibre changes go to review_queue when the field is in
    `user_edited_fields`."""

    async def test_user_edited_field_routes_to_queue(
        self, discovery_db, monkeypatch,
    ):
        from app.discovery import calibre_sync
        from app.discovery.database import get_db

        # First sync.
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(1, "BookA", description="Original desc",
                      tags="tag1"),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        # Mark `description` as user-edited (simulating a manual edit).
        db = await get_db()
        try:
            br = await (await db.execute(
                "SELECT id FROM books WHERE calibre_id = 1"
            )).fetchone()
            book_id = br["id"]
            await db.execute(
                "UPDATE books SET description = 'My edited desc', "
                "user_edited_fields = ? WHERE id = ?",
                (json.dumps(["description"]), book_id),
            )
            await db.commit()
        finally:
            await db.close()

        # Second sync brings new values for both `description` (edited)
        # and `tags` (not edited).
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(1, "BookA", description="Calibre's new desc",
                      tags="tag2"),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        # Books row: tags auto-flowed, description preserved.
        row = await _book_row(book_id)
        assert row["description"] == "My edited desc"
        assert row["tags"] == "tag2"

        # One queue entry for description.
        queue = await _queue_rows()
        desc_q = [q for q in queue if q["field"] == "description"]
        assert len(desc_q) == 1
        assert desc_q[0]["source"] == "calibre"
        assert desc_q[0]["old_value"] == "My edited desc"
        assert desc_q[0]["new_value"] == "Calibre's new desc"
        # No queue entry for tags since it auto-flowed.
        assert all(q["field"] != "tags" for q in queue)

    async def test_repeat_sync_replaces_queued_proposal(
        self, discovery_db, monkeypatch,
    ):
        """A second Calibre sync with a different value for the same
        edited field should replace the existing queue row, not pile
        up. UNIQUE(book_id, field, source) on the table enforces this."""
        from app.discovery import calibre_sync
        from app.discovery.database import get_db

        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [_book(1, "B", description="V1")]},
        )
        await calibre_sync.sync_calibre("x", "y")

        db = await get_db()
        try:
            br = await (await db.execute(
                "SELECT id FROM books WHERE calibre_id = 1"
            )).fetchone()
            await db.execute(
                "UPDATE books SET description = 'My', user_edited_fields = ? "
                "WHERE id = ?",
                (json.dumps(["description"]), br["id"]),
            )
            await db.commit()
        finally:
            await db.close()

        # First contested sync.
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [_book(1, "B", description="V2")]},
        )
        await calibre_sync.sync_calibre("x", "y")

        # Second contested sync — Calibre changed again.
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [_book(1, "B", description="V3")]},
        )
        await calibre_sync.sync_calibre("x", "y")

        queue = await _queue_rows()
        desc_q = [q for q in queue if q["field"] == "description"]
        assert len(desc_q) == 1, "expected exactly one queue row, got " + repr(desc_q)
        assert desc_q[0]["new_value"] == "V3"


class TestCoverPathNullGuard:
    """Calibre sometimes emits cover_path=None mid-sync. Don't blow
    away an existing cover with NULL."""

    async def test_null_cover_does_not_overwrite_existing(
        self, discovery_db, monkeypatch,
    ):
        from app.discovery import calibre_sync

        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(1, "B", cover_path="/c/a.jpg"),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        from app.discovery.database import get_db
        db = await get_db()
        try:
            br = await (await db.execute(
                "SELECT id FROM books WHERE calibre_id = 1"
            )).fetchone()
            book_id = br["id"]
        finally:
            await db.close()

        # Second sync: Calibre returns None (e.g. cover wasn't computed).
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(1, "B", cover_path=None),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        row = await _book_row(book_id)
        assert row["cover_path"] == "/c/a.jpg"
        # And no spurious queue row for cover_path.
        queue = await _queue_rows()
        assert all(q["field"] != "cover_path" for q in queue)


class TestNoOpOnUnchanged:
    """No diff = no UPDATE, no queue row."""

    async def test_unchanged_resync_is_silent(self, discovery_db, monkeypatch):
        from app.discovery import calibre_sync

        same_book = _book(1, "B", description="Same", tags="tag1",
                          rating=7)
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [same_book]},
        )
        await calibre_sync.sync_calibre("x", "y")
        await calibre_sync.sync_calibre("x", "y")

        # Queue is empty — nothing changed, nothing to review.
        assert await _queue_rows() == []


class TestAutoUnhideOnMerge:
    """When a Calibre sync merges a new owned book with an existing
    hidden source-discovered row, the hidden flag is auto-cleared.

    Mark's UAT 2026-05-09: he hid five Fantasy World Farm books that
    he wasn't interested in. His author allowlist later grabbed them
    via the pipeline and they landed in Calibre. Sync merged owned=1
    onto the existing rows but left hidden=1, so the UI count showed
    5 fewer books than Calibre. Auto-unhide on merge resolves this:
    once a book is OWNED (you have it), it should be visible.

    Scoped narrowly to the merge path — books already owned in
    Calibre that the user explicitly hid (duplicate edition, wrong
    language, etc.) keep their hide.
    """

    async def test_merge_unhides_source_discovered_row(
        self, discovery_db, monkeypatch,
    ):
        from app.discovery import calibre_sync
        from app.discovery.database import get_db

        # Seed: a hidden, unowned source-discovered row.
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (name, sort_name, normalized_name, calibre_id) "
                "VALUES ('Eric Vall', 'Vall, Eric', 'eric vall', 100)"
            )
            cur = await db.execute(
                "INSERT INTO books (title, source, owned, hidden) "
                "VALUES ('Fantasy World Farm 2', 'goodreads', 0, 1)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, 1, 0)",
                (cur.lastrowid,),
            )
            await db.commit()
        finally:
            await db.close()

        # Calibre now reports the same book — should merge + unhide.
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(3877, "Fantasy World Farm 2", "Eric Vall",
                      author_id=100),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        # Existing row converted to Calibre + unhidden + owned.
        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT id, source, owned, hidden, calibre_id FROM books "
                "WHERE title = 'Fantasy World Farm 2'"
            )).fetchone()
        finally:
            await db.close()
        assert row is not None
        assert row["source"] == "calibre"
        assert row["owned"] == 1
        assert row["hidden"] == 0  # ← the new behavior
        assert row["calibre_id"] == 3877

    async def test_existing_calibre_row_keeps_user_hide(
        self, discovery_db, monkeypatch,
    ):
        # Counter-test: a book that's ALREADY a Calibre row in Seshat
        # and the user has hidden it (e.g., duplicate edition the user
        # is keeping for completeness). A re-sync hits the
        # `existing UPDATE` branch, NOT the merge branch — hidden
        # must NOT be cleared.
        from app.discovery import calibre_sync
        from app.discovery.database import get_db

        # First sync: book lands as a fresh Calibre row.
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(1, "Some Book", "Author A", author_id=100),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        # User explicitly hides it.
        db = await get_db()
        try:
            await db.execute(
                "UPDATE books SET hidden=1 WHERE calibre_id = 1"
            )
            await db.commit()
        finally:
            await db.close()

        # Re-sync — same book, no metadata change. Should hit the
        # existing-Calibre-update branch, NOT the merge branch.
        await calibre_sync.sync_calibre("x", "y")

        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT hidden FROM books WHERE calibre_id = 1"
            )).fetchone()
        finally:
            await db.close()
        # User's explicit hide preserved.
        assert row["hidden"] == 1

    async def test_merge_leaves_hidden_alone_when_already_visible(
        self, discovery_db, monkeypatch,
    ):
        # Edge: source row is hidden=0 to begin with. The auto-unhide
        # is idempotent — hidden stays 0, no surprise side effects.
        from app.discovery import calibre_sync
        from app.discovery.database import get_db

        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (name, sort_name, normalized_name, calibre_id) "
                "VALUES ('Eric Vall', 'Vall, Eric', 'eric vall', 100)"
            )
            cur = await db.execute(
                "INSERT INTO books (title, source, owned, hidden) "
                "VALUES ('Visible Source Book', 'goodreads', 0, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, 1, 0)",
                (cur.lastrowid,),
            )
            await db.commit()
        finally:
            await db.close()

        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(99, "Visible Source Book", "Eric Vall",
                      author_id=100),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT hidden, owned, source FROM books "
                "WHERE title = 'Visible Source Book'"
            )).fetchone()
        finally:
            await db.close()
        assert row["hidden"] == 0
        assert row["owned"] == 1
        assert row["source"] == "calibre"
