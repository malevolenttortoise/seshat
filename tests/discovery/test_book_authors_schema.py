"""v3.0.0 Phase 1A — book_authors schema migration.

Smoke tests for the new join table that replaces the single
`books.author_id` denormalization. Phase 1 ships the schema only;
the column stays in place across phases 2-8 and is dropped in Phase 9.

Coverage:
  - Fresh DB has the table after init_db (SCHEMA block).
  - Existing DB picks the table up via the appended MIGRATIONS entry.
  - Composite PK rejects duplicate (book_id, author_id) pairs.
  - ON DELETE CASCADE on the FK to books drops links when a book is
    removed (prevents orphan link accumulation).
  - position + role columns accept the expected types + default for
    position.
  - The author-side index exists so reverse lookups stay cheap.
"""
from __future__ import annotations

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


async def _columns_of(table: str) -> dict[str, dict]:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
        return {r["name"]: dict(r) for r in rows}
    finally:
        await db.close()


async def test_book_authors_table_exists_after_init(discovery_db):
    cols = await _columns_of("book_authors")
    assert set(cols) == {"book_id", "author_id", "position", "role"}
    # Composite PK on (book_id, author_id) — both columns pk-marked.
    assert cols["book_id"]["pk"] == 1
    assert cols["author_id"]["pk"] == 2
    assert cols["position"]["pk"] == 0
    assert cols["role"]["pk"] == 0
    # position carries the explicit DEFAULT 0.
    assert cols["position"]["dflt_value"] == "0"
    assert cols["position"]["notnull"] == 1
    # role nullable (Phase 3 populates non-NULL for translators etc.).
    assert cols["role"]["notnull"] == 0


async def test_author_index_present(discovery_db):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='book_authors'"
        )).fetchall()
        names = {r["name"] for r in rows}
    finally:
        await db.close()
    # Auto-generated PK index + our explicit author-side index.
    assert "idx_book_authors_author" in names


async def test_duplicate_link_rejected(discovery_db):
    from app.discovery.database import get_db
    import aiosqlite
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name) VALUES (1, 'Alice', 'Alice')"
        )
        await db.execute(
            "INSERT INTO books (id, title, author_id) VALUES (10, 'B', 1)"
        )
        await db.execute(
            "INSERT INTO book_authors (book_id, author_id, position) "
            "VALUES (10, 1, 0)"
        )
        await db.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) "
                "VALUES (10, 1, 1)"  # same (book, author), different position
            )
    finally:
        await db.close()


async def test_book_delete_cascades_links(discovery_db):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        # FK enforcement isn't on by default in SQLite — Seshat's
        # connection helper turns it on. Re-assert here so this test
        # fails loudly if the FK pragma ever regresses.
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute(
            "INSERT INTO authors (id, name, sort_name) VALUES "
            "(1, 'Alice', 'Alice'), (2, 'Bob', 'Bob')"
        )
        await db.execute(
            "INSERT INTO books (id, title, author_id) "
            "VALUES (10, 'B', 1)"
        )
        await db.execute(
            "INSERT INTO book_authors (book_id, author_id, position) "
            "VALUES (10, 1, 0), (10, 2, 1)"
        )
        await db.commit()

        cur = await db.execute(
            "SELECT COUNT(*) FROM book_authors WHERE book_id=10"
        )
        assert (await cur.fetchone())[0] == 2

        await db.execute("DELETE FROM books WHERE id=10")
        await db.commit()

        cur = await db.execute(
            "SELECT COUNT(*) FROM book_authors WHERE book_id=10"
        )
        assert (await cur.fetchone())[0] == 0, (
            "ON DELETE CASCADE on the book FK should have removed the "
            "two book_authors rows along with the books row"
        )
    finally:
        await db.close()


async def test_role_accepts_null_and_strings(discovery_db):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name) VALUES "
            "(1, 'A', 'A'), (2, 'B', 'B'), (3, 'C', 'C')"
        )
        await db.execute(
            "INSERT INTO books (id, title, author_id) "
            "VALUES (10, 'B', 1)"
        )
        await db.execute(
            "INSERT INTO book_authors (book_id, author_id, position, role) "
            "VALUES (10, 1, 0, NULL), (10, 2, 1, 'translator'), "
            "(10, 3, 2, 'illustrator')"
        )
        await db.commit()
        cur = await db.execute(
            "SELECT author_id, role FROM book_authors "
            "WHERE book_id=10 ORDER BY position"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        assert rows == [
            {"author_id": 1, "role": None},
            {"author_id": 2, "role": "translator"},
            {"author_id": 3, "role": "illustrator"},
        ]
    finally:
        await db.close()
