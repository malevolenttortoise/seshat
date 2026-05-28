"""v3.0.0 Phase 9 (ADR-0012) — drop the legacy books.author_id column.

Directly exercises `_drop_legacy_books_author_id` against a synthetic
LEGACY-shaped books table (the production fresh schema no longer has the
column, so these tests build the pre-drop shape by hand) plus a fresh-DB
shape check via the real `init_db()`.
"""
import aiosqlite
import pytest

from app.discovery.database import (
    _backfill_book_authors,
    _drop_legacy_books_author_id,
)


# Minimal pre-Phase-9 ("legacy") shape: books WITH author_id + its FK +
# the two author indexes, alongside authors + book_authors.
_LEGACY_DDL = """
CREATE TABLE authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort_name TEXT,
    normalized_name TEXT
);
CREATE TABLE books_calibre_snapshot (book_id INTEGER PRIMARY KEY, authors_json TEXT);
CREATE TABLE books_abs_snapshot (book_id INTEGER PRIMARY KEY, authors_json TEXT);
CREATE TABLE series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    author_id INTEGER
);
CREATE TABLE books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author_id INTEGER NOT NULL,
    series_id INTEGER,
    owned INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (author_id) REFERENCES authors(id),
    FOREIGN KEY (series_id) REFERENCES series(id)
);
CREATE INDEX idx_books_author ON books(author_id);
CREATE INDEX idx_books_author_owned ON books(author_id, owned);
CREATE INDEX idx_books_owned ON books(owned);
CREATE TABLE book_authors (
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors(id),
    position INTEGER NOT NULL DEFAULT 0,
    role TEXT,
    PRIMARY KEY (book_id, author_id)
);
CREATE INDEX idx_book_authors_author ON book_authors(author_id);
"""


async def _legacy_db(tmp_path, *, link_all=True):
    """Build a legacy-shaped DB. Two authors, three books (one co-authored).
    When `link_all`, every book gets its position-0 book_authors row (the
    healthy post-backfill invariant)."""
    db = await aiosqlite.connect(tmp_path / "legacy.db")
    db.row_factory = aiosqlite.Row
    await db.executescript(_LEGACY_DDL)
    await db.execute("INSERT INTO authors (id, name) VALUES (1, 'Chaney')")
    await db.execute("INSERT INTO authors (id, name) VALUES (2, 'Anspach')")
    # book 1: solo Chaney; book 2: co-authored Chaney+Anspach; book 3: solo Anspach
    await db.execute("INSERT INTO books (id, title, author_id, owned) VALUES (1, 'Solo', 1, 1)")
    await db.execute("INSERT INTO books (id, title, author_id, owned) VALUES (2, 'Team', 1, 1)")
    await db.execute("INSERT INTO books (id, title, author_id, owned) VALUES (3, 'Other', 2, 0)")
    if link_all:
        await db.executemany(
            "INSERT INTO book_authors (book_id, author_id, position) VALUES (?, ?, ?)",
            [(1, 1, 0), (2, 1, 0), (2, 2, 1), (3, 2, 0)],
        )
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_drop_removes_column_and_preserves_data(tmp_path):
    db = await _legacy_db(tmp_path)
    try:
        assert await _drop_legacy_books_author_id(db) is True
        cols = [c["name"] for c in await (await db.execute("PRAGMA table_info(books)")).fetchall()]
        assert "author_id" not in cols
        # Every other column + all rows survive.
        assert {"id", "title", "series_id", "owned"}.issubset(set(cols))
        n = (await (await db.execute("SELECT COUNT(*) FROM books")).fetchone())[0]
        assert n == 3
        # book_authors is untouched and still resolves the primary.
        prim = (await (await db.execute(
            "SELECT author_id FROM book_authors WHERE book_id=2 AND position=0"
        )).fetchone())[0]
        assert prim == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_drop_recreates_non_author_indexes_only(tmp_path):
    db = await _legacy_db(tmp_path)
    try:
        await _drop_legacy_books_author_id(db)
        idx = [r["name"] for r in await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='books'"
        )).fetchall()]
        assert "idx_books_owned" in idx          # non-author index recreated
        assert "idx_books_author" not in idx      # author indexes gone
        assert "idx_books_author_owned" not in idx
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_drop_is_idempotent(tmp_path):
    db = await _legacy_db(tmp_path)
    try:
        assert await _drop_legacy_books_author_id(db) is True
        # Second call: column already gone → no-op, no error.
        assert await _drop_legacy_books_author_id(db) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_preflight_aborts_when_a_book_would_lose_its_only_link(tmp_path):
    # A book with a non-NULL author_id but NO position-0 book_authors row
    # would lose its only author reference — the drop must ABORT.
    db = await _legacy_db(tmp_path, link_all=False)
    try:
        # Link only books 1 and 3; book 2 (author_id=1) has no link at all.
        await db.executemany(
            "INSERT INTO book_authors (book_id, author_id, position) VALUES (?, ?, ?)",
            [(1, 1, 0), (3, 2, 0)],
        )
        await db.commit()
        assert await _drop_legacy_books_author_id(db) is False
        # Column is left in place so nothing is lost.
        cols = [c["name"] for c in await (await db.execute("PRAGMA table_info(books)")).fetchall()]
        assert "author_id" in cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_backfill_self_heals_author_id_only_book_before_drop(tmp_path):
    """Upgrade-path self-heal (regression for the prod orphan case):
    a discovered book with a non-NULL author_id but NO book_authors row
    (e.g. scanned in after the last backfill) gets a position-0 row from
    `_backfill_book_authors`'s fallback arm — which runs BEFORE the drop in
    init_db — so the drop's pre-flight then passes and the column drops
    cleanly without the book losing its author.

    Guards the load-bearing upgrade path: fresh-DB fixtures can't exercise
    the author_id fallback arm (no column), so this legacy-shaped test is
    the only coverage of it.
    """
    db = await _legacy_db(tmp_path)
    try:
        # Discovered book (owned=0), author_id set, NO book_authors row,
        # NO snapshot row — exactly the prod orphan shape.
        await db.execute(
            "INSERT INTO books (id, title, author_id, owned) "
            "VALUES (99, 'New Life of a Retired Adventurer', 2, 0)"
        )
        await db.commit()
        pre = await (await db.execute(
            "SELECT COUNT(*) FROM book_authors WHERE book_id=99"
        )).fetchone()
        assert pre[0] == 0  # orphan: would block the drop

        # The backfill (runs before the drop every boot) self-heals it.
        await _backfill_book_authors(db)
        healed = await (await db.execute(
            "SELECT author_id, position FROM book_authors WHERE book_id=99"
        )).fetchone()
        assert healed is not None
        assert (healed["author_id"], healed["position"]) == (2, 0)

        # Pre-flight now passes → drop proceeds; the book keeps its author.
        assert await _drop_legacy_books_author_id(db) is True
        cols = [c["name"] for c in await (await db.execute("PRAGMA table_info(books)")).fetchall()]
        assert "author_id" not in cols
        still = await (await db.execute(
            "SELECT author_id FROM book_authors WHERE book_id=99 AND position=0"
        )).fetchone()
        assert still["author_id"] == 2
    finally:
        await db.close()
