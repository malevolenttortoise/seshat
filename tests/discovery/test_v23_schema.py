"""
v2.3.0 schema migration tests.

Covers:
- `series.author_id` becomes nullable on upgraded DBs (pre-v2.3 had
  NOT NULL); fresh DBs already get nullable from SCHEMA.
- New v2.3 tables exist after init_db: books_calibre_snapshot,
  books_abs_snapshot, metadata_review_queue.
- New books columns exist: metadata_source_pref, field_source_map,
  user_edited_fields.
- Cold-start snapshot backfill copies current books rows into the
  snapshot tables and is idempotent on re-run.
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


async def _column_info(table: str) -> list[dict]:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def _table_exists(table: str) -> bool:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )).fetchone()
        return row is not None
    finally:
        await db.close()


class TestFreshSchema:
    """A fresh DB created by SCHEMA already has the v2.3 shape."""

    async def test_series_author_id_is_nullable(self, discovery_db):
        cols = await _column_info("series")
        author_id = next(c for c in cols if c["name"] == "author_id")
        assert author_id["notnull"] == 0, \
            "series.author_id must be nullable in v2.3 SCHEMA"

    async def test_snapshot_tables_exist(self, discovery_db):
        assert await _table_exists("books_calibre_snapshot")
        assert await _table_exists("books_abs_snapshot")

    async def test_review_queue_table_exists(self, discovery_db):
        assert await _table_exists("metadata_review_queue")

    async def test_new_books_columns_exist(self, discovery_db):
        cols = await _column_info("books")
        names = {c["name"] for c in cols}
        assert "metadata_source_pref" in names
        assert "field_source_map" in names
        assert "user_edited_fields" in names

    async def test_metadata_source_pref_defaults_to_seshat(self, discovery_db):
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute("INSERT INTO authors (name, sort_name) VALUES (?, ?)", ("A", "A"))
            cur = await db.execute("INSERT INTO books (title) VALUES (?)", ("B",))
            book_id = cur.lastrowid
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, ?, 0)",
                (book_id, 1),
            )
            await db.commit()
            row = await (await db.execute(
                "SELECT metadata_source_pref, user_edited_fields FROM books"
            )).fetchone()
            assert row["metadata_source_pref"] == "seshat"
            assert row["user_edited_fields"] == "[]"
        finally:
            await db.close()


class TestSeriesAuthorNullableMigration:
    """The recreate-table dance for upgraded DBs that still have
    NOT NULL on series.author_id."""

    async def test_idempotent_on_already_nullable(self, discovery_db):
        from app.discovery.database import _migrate_series_author_nullable, get_db
        db = await get_db()
        try:
            ran = await _migrate_series_author_nullable(db)
        finally:
            await db.close()
        # Fresh DB starts nullable, so the migration is a no-op.
        assert ran is False

    async def test_migrates_legacy_not_null_table(self, tmp_path, monkeypatch):
        """Simulate a pre-v2.3 DB where series.author_id is NOT NULL.
        Running the migration must rebuild the table with nullable
        author_id while preserving all existing rows + ids."""
        from app import config as app_config
        from app.discovery import database as disco_db

        monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
        disco_db.set_active_library("legacy")

        # Hand-craft a legacy schema by opening the DB raw and creating
        # the old-shape series table BEFORE init_db runs.
        import aiosqlite
        path = disco_db.get_db_path("legacy")
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(path)) as db:
            await db.execute("""
                CREATE TABLE authors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE series (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    author_id INTEGER NOT NULL,
                    hardcover_id TEXT,
                    goodreads_id TEXT,
                    kobo_id TEXT,
                    fictiondb_id TEXT,
                    total_books INTEGER,
                    description TEXT,
                    last_lookup_at REAL,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    amazon_id TEXT,
                    audiobookshelf_id TEXT,
                    FOREIGN KEY (author_id) REFERENCES authors(id),
                    UNIQUE(name, author_id)
                )
            """)
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES (1, 'A', 'A')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) VALUES (42, 'S', 1)"
            )
            await db.commit()

        from app.discovery.database import _migrate_series_author_nullable, get_db
        db = await get_db("legacy")
        try:
            ran = await _migrate_series_author_nullable(db)
            assert ran is True

            cols = await (await db.execute(
                "PRAGMA table_info(series)"
            )).fetchall()
            author_id = next(c for c in cols if c["name"] == "author_id")
            assert author_id["notnull"] == 0

            # Row preserved with original id.
            row = await (await db.execute(
                "SELECT id, name, author_id FROM series"
            )).fetchone()
            assert row["id"] == 42
            assert row["name"] == "S"
            assert row["author_id"] == 1

            # Idempotent re-run.
            ran_again = await _migrate_series_author_nullable(db)
            assert ran_again is False
        finally:
            await db.close()
            disco_db.set_active_library(None)


class TestSnapshotBackfill:
    """Cold-start backfill of snapshot tables from existing books rows."""

    async def test_calibre_owned_books_get_snapshot(self, discovery_db):
        from app.discovery.database import (
            _backfill_metadata_snapshots, get_db,
        )
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES (1, 'AuthorA', 'AuthorA')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) VALUES (10, 'Halo', 1)"
            )
            await db.execute("""
                INSERT INTO books
                (id, title, series_id, series_index, isbn,
                 cover_path, description, tags, rating, language,
                 publisher, formats, pub_date, source, owned, calibre_id)
                VALUES (1, 'BookA', 10, 1.0, '978-X', '/c/a.jpg',
                        'desc', 'tag1,tag2', 8, 'eng', 'Pub', 'epub',
                        '2024-01-01', 'calibre', 1, 100)
            """)
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 1, 0)"
            )
            await db.commit()

            cal, abs_count = await _backfill_metadata_snapshots(db)
            assert cal == 1
            assert abs_count == 0

            row = await (await db.execute(
                "SELECT * FROM books_calibre_snapshot WHERE book_id=1"
            )).fetchone()
            assert row["title"] == "BookA"
            assert row["series_name"] == "Halo"
            assert row["series_index"] == 1.0
            assert row["isbn"] == "978-X"
            assert row["rating"] == 8
            assert row["synced_at"] > 0
            assert json.loads(row["authors_json"]) == [
                {"id": None, "name": "AuthorA"}
            ]
        finally:
            await db.close()

    async def test_abs_books_get_snapshot(self, discovery_db):
        from app.discovery.database import (
            _backfill_metadata_snapshots, get_db,
        )
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES (1, 'A', 'A')"
            )
            await db.execute("""
                INSERT INTO books
                (id, title, audiobookshelf_id, narrator,
                 duration_sec, abridged, asin, audio_formats, source, owned)
                VALUES (1, 'AudioBook', 'abs-uuid-1', 'Reader X',
                        43200.5, 0, 'B0XYZ', 'm4b', 'audiobookshelf', 1)
            """)
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 1, 0)"
            )
            await db.commit()

            cal, abs_count = await _backfill_metadata_snapshots(db)
            assert cal == 0
            assert abs_count == 1

            row = await (await db.execute(
                "SELECT * FROM books_abs_snapshot WHERE book_id=1"
            )).fetchone()
            assert row["title"] == "AudioBook"
            assert row["narrator"] == "Reader X"
            assert row["duration_sec"] == 43200.5
            assert row["asin"] == "B0XYZ"
        finally:
            await db.close()

    async def test_idempotent_re_run(self, discovery_db):
        from app.discovery.database import (
            _backfill_metadata_snapshots, get_db,
        )
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES (1, 'A', 'A')"
            )
            await db.execute(
                "INSERT INTO books (id, title, source, owned, calibre_id) "
                "VALUES (1, 'B', 'calibre', 1, 100)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 1, 0)"
            )
            await db.commit()

            cal1, _ = await _backfill_metadata_snapshots(db)
            cal2, _ = await _backfill_metadata_snapshots(db)
            assert cal1 == 1
            assert cal2 == 0  # second run sees existing snapshot, skips

            # Only one row in snapshot table.
            row = await (await db.execute(
                "SELECT COUNT(*) FROM books_calibre_snapshot"
            )).fetchone()
            assert row[0] == 1
        finally:
            await db.close()

    async def test_skips_unowned_calibre_books(self, discovery_db):
        """Source-discovered (unowned) books with source='calibre' don't
        actually exist on the user's Calibre side — skip them."""
        from app.discovery.database import (
            _backfill_metadata_snapshots, get_db,
        )
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES (1, 'A', 'A')"
            )
            await db.execute(
                "INSERT INTO books (id, title, source, owned) "
                "VALUES (1, 'B', 'calibre', 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 1, 0)"
            )
            await db.commit()

            cal, _ = await _backfill_metadata_snapshots(db)
            assert cal == 0
        finally:
            await db.close()


class TestSharedSeriesRow:
    """Verify a shared series row (author_id NULL) is allowed and
    coexists with per-author rows of the same name."""

    async def test_can_insert_shared_series_row(self, discovery_db):
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES (1, 'A', 'A')"
            )
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES (2, 'B', 'B')"
            )
            # Per-author rows.
            await db.execute(
                "INSERT INTO series (name, author_id) VALUES ('Halo', 1)"
            )
            await db.execute(
                "INSERT INTO series (name, author_id) VALUES ('Halo', 2)"
            )
            # Shared row.
            await db.execute(
                "INSERT INTO series (name, author_id) VALUES ('Halo', NULL)"
            )
            await db.commit()

            rows = await (await db.execute(
                "SELECT name, author_id FROM series WHERE name='Halo' ORDER BY author_id"
            )).fetchall()
            assert len(rows) == 3
            author_ids = [r["author_id"] for r in rows]
            assert author_ids == [None, 1, 2]
        finally:
            await db.close()
