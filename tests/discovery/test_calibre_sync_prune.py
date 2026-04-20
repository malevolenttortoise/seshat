"""Tests for the Pass 4 reconcile step in `sync_calibre`."""
import pytest


def _book(book_id, title, author="Test Author"):
    """Minimal book dict shaped like `_read_calibre_db`'s output."""
    return {
        "book_id": book_id,
        "title": title,
        "pubdate": "2024-01-01",
        "series_index": 1.0,
        "book_path": f"{author}/{title}",
        "cover_path": None,
        "isbn": None,
        "authors": [{"id": 100 + book_id, "name": author, "sort": author}],
        "series": [],
        "tags": None,
        "rating": None,
        "description": None,
        "language": None,
        "publisher": None,
        "formats": None,
    }


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    """Tmp per-library discovery DB, active slug set, schema initialized."""
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


async def _count_calibre_books(slug="test"):
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        row = await (await db.execute(
            "SELECT COUNT(*) c FROM books WHERE source='calibre'"
        )).fetchone()
        return row["c"]
    finally:
        await db.close()


async def _calibre_ids(slug="test"):
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        rows = await (await db.execute(
            "SELECT calibre_id FROM books WHERE source='calibre' "
            "ORDER BY calibre_id"
        )).fetchall()
        return [r["calibre_id"] for r in rows]
    finally:
        await db.close()


async def test_prune_removes_books_no_longer_in_metadata_db(discovery_db, monkeypatch):
    from app.discovery import calibre_sync

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [
            _book(1, "Book One"), _book(2, "Book Two"), _book(3, "Book Three"),
        ]},
    )
    result = await calibre_sync.sync_calibre("x", "y")
    assert result["books_new"] == 3
    assert result["books_pruned"] == 0
    assert await _calibre_ids() == [1, 2, 3]

    # Book 2 removed from Calibre. Second sync should drop it.
    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [_book(1, "Book One"), _book(3, "Book Three")]},
    )
    result = await calibre_sync.sync_calibre("x", "y")
    assert result["books_pruned"] == 1
    assert await _calibre_ids() == [1, 3]


async def test_prune_skipped_when_metadata_db_empty(discovery_db, monkeypatch):
    """Zero-book reads are treated as errors, not deliberate mass-deletes."""
    from app.discovery import calibre_sync

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [_book(1, "Book One")]},
    )
    await calibre_sync.sync_calibre("x", "y")
    assert await _count_calibre_books() == 1

    # Simulate empty read — the single book must survive.
    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": []},
    )
    result = await calibre_sync.sync_calibre("x", "y")
    assert result["books_pruned"] == 0
    assert await _count_calibre_books() == 1


async def test_prune_leaves_non_calibre_rows_alone(discovery_db, monkeypatch):
    """Discovery-only rows (source != 'calibre') are untouched by prune."""
    from app.discovery import calibre_sync
    from app.discovery.database import get_db

    # Seed a Missing row (source='mam') with a calibre_id that's NOT in
    # the upcoming sync — the prune pass must not touch it because the
    # filter is `source='calibre'`.
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES ('Discovery Author', 'Discovery Author')"
        )
        await db.execute(
            "INSERT INTO books (title, author_id, calibre_id, source, owned) "
            "VALUES ('Missing Book', 1, 999, 'mam', 0)"
        )
        await db.commit()
    finally:
        await db.close()

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [_book(1, "Book One")]},
    )
    await calibre_sync.sync_calibre("x", "y")

    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT COUNT(*) c FROM books WHERE source='mam' AND calibre_id=999"
        )).fetchone()
        assert row["c"] == 1
    finally:
        await db.close()
