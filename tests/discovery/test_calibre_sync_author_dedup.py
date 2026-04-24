"""
Tests for calibre_sync's normalized-name author upsert.

Calibre's metadata.db can contain two author records for the same
person when books were imported at different times with different
punctuation ("A. K. DuBoff" as calibre_id=254, "A K DuBoff" as
calibre_id=1179). Before this change, Seshat mirrored both as two
separate rows. Now they collapse into one Seshat author row via
`normalized_name` lookup.
"""
from __future__ import annotations

import pytest


def _book(book_id, title, author_name="Test Author", author_id=100,
          author_sort=None):
    """Minimal shape matching `_read_calibre_db`'s output."""
    return {
        "book_id": book_id,
        "title": title,
        "pubdate": "2024-01-01",
        "series_index": 1.0,
        "book_path": f"Author/{title}",
        "cover_path": None,
        "isbn": None,
        "authors": [{
            "id": author_id,
            "name": author_name,
            "sort": author_sort or author_name,
        }],
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
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


async def _author_rows():
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT id, name, sort_name, calibre_id, normalized_name "
            "FROM authors ORDER BY id"
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def test_normalized_name_populated_on_insert(discovery_db, monkeypatch):
    from app.discovery import calibre_sync

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [
            _book(1, "Book One", "A. K. DuBoff", author_id=254),
        ]},
    )
    await calibre_sync.sync_calibre("x", "y")

    rows = await _author_rows()
    assert len(rows) == 1
    assert rows[0]["name"] == "A. K. DuBoff"
    assert rows[0]["normalized_name"] == "ak duboff"


async def test_two_punctuation_variants_collapse_to_one_row(discovery_db, monkeypatch):
    # The real-world bug: Calibre has author_id 254 "A. K. DuBoff" AND
    # author_id 1179 "A K DuBoff", both referring to the same person.
    # After sync we should see ONE Seshat row, not two.
    from app.discovery import calibre_sync

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [
            _book(1, "Stranded", "A. K. DuBoff", author_id=254),
            _book(2, "Empire Reborn", "A K DuBoff", author_id=1179),
        ]},
    )
    await calibre_sync.sync_calibre("x", "y")

    rows = await _author_rows()
    assert len(rows) == 1
    # Display name picks the more-punctuated variant (option 4a).
    assert rows[0]["name"] == "A. K. DuBoff"
    assert rows[0]["normalized_name"] == "ak duboff"


async def test_unpunctuated_first_then_punctuated_upgrades_display(discovery_db, monkeypatch):
    # Insertion order matters for the initial state but shouldn't
    # matter for the final state — the punctuated variant always wins.
    from app.discovery import calibre_sync

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [
            _book(1, "Book One", "A K DuBoff", author_id=1179),
            _book(2, "Book Two", "A. K. DuBoff", author_id=254),
        ]},
    )
    await calibre_sync.sync_calibre("x", "y")

    rows = await _author_rows()
    assert len(rows) == 1
    assert rows[0]["name"] == "A. K. DuBoff"


async def test_distinct_authors_remain_separate(discovery_db, monkeypatch):
    # Sanity: normalization must not collapse genuinely different
    # authors with unrelated names.
    from app.discovery import calibre_sync

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [
            _book(1, "Book One", "Brandon Sanderson", author_id=1),
            _book(2, "Book Two", "Pierce Brown", author_id=2),
        ]},
    )
    await calibre_sync.sync_calibre("x", "y")

    rows = await _author_rows()
    names = sorted(r["name"] for r in rows)
    assert names == ["Brandon Sanderson", "Pierce Brown"]


async def test_books_by_both_variants_link_to_merged_author(discovery_db, monkeypatch):
    # After two Calibre authors collapse into one Seshat row, both
    # books must point at that row via books.author_id.
    from app.discovery import calibre_sync
    from app.discovery.database import get_db

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [
            _book(1, "Stranded", "A. K. DuBoff", author_id=254),
            _book(2, "Empire Reborn", "A K DuBoff", author_id=1179),
        ]},
    )
    await calibre_sync.sync_calibre("x", "y")

    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT title, author_id FROM books ORDER BY title"
        )).fetchall()
        author_ids = {r["author_id"] for r in rows}
        assert len(author_ids) == 1
        assert len(rows) == 2
    finally:
        await db.close()
