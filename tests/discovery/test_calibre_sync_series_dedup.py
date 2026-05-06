"""
Tests for calibre_sync's author-scoped series lookup.

Calibre can hold two unrelated authors who happen to share a series
name (Cressman/Savarovsky "The Last Paladin"). The series lookup
during sync used to be a global LOWER(name) match, which collapsed
both authors' books onto a single Seshat series row. It is now
author-scoped, mirroring the v2.2.7 fix in `_ensure_series_for_author`.
"""
from __future__ import annotations

import pytest


def _book(book_id, title, author_name, author_id, series_name=None,
          series_id=None, series_index=1.0):
    return {
        "book_id": book_id,
        "title": title,
        "pubdate": "2024-01-01",
        "series_index": series_index,
        "book_path": f"{author_name}/{title}",
        "cover_path": None,
        "isbn": None,
        "authors": [{
            "id": author_id,
            "name": author_name,
            "sort": author_name,
        }],
        "series": (
            [{"id": series_id, "name": series_name}]
            if series_name else []
        ),
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


async def _series_rows():
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT id, name, author_id FROM series ORDER BY id"
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def _book_rows():
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT title, author_id, series_id FROM books ORDER BY title"
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def test_same_series_name_different_authors_get_separate_rows(
    discovery_db, monkeypatch,
):
    """The Cressman/Savarovsky case: two authors each with a Calibre
    series named "The Last Paladin" must produce two Seshat series
    rows, not one shared row."""
    from app.discovery import calibre_sync

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [
            _book(1, "The Last Paladin",
                  "John Cressman", author_id=580,
                  series_name="The Last Paladin", series_id=100,
                  series_index=1.0),
            _book(2, "The Last Paladin #1",
                  "Roman Savarovsky", author_id=549,
                  series_name="The Last Paladin", series_id=200,
                  series_index=1.0),
        ]},
    )
    await calibre_sync.sync_calibre("x", "y")

    series = await _series_rows()
    paladin_rows = [s for s in series if s["name"] == "The Last Paladin"]
    assert len(paladin_rows) == 2
    assert {r["author_id"] for r in paladin_rows} == \
        {s["author_id"] for s in series if s["name"] == "The Last Paladin"}
    # Each author got their own row.
    author_to_series = {r["author_id"]: r["id"] for r in paladin_rows}
    assert len(author_to_series) == 2

    books = await _book_rows()
    assert len(books) == 2
    # Books point to series rows owned by their own author.
    for b in books:
        assert b["series_id"] == author_to_series[b["author_id"]]


async def test_same_author_same_series_name_dedupes(
    discovery_db, monkeypatch,
):
    """Sanity: a single author with multiple Calibre books in the
    same series still produces ONE Seshat series row."""
    from app.discovery import calibre_sync

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [
            _book(1, "Book One", "Solo Author", author_id=900,
                  series_name="My Series", series_id=50, series_index=1.0),
            _book(2, "Book Two", "Solo Author", author_id=900,
                  series_name="My Series", series_id=50, series_index=2.0),
        ]},
    )
    await calibre_sync.sync_calibre("x", "y")

    series = await _series_rows()
    my_series = [s for s in series if s["name"] == "My Series"]
    assert len(my_series) == 1


async def test_multi_author_calibre_series_becomes_shared(
    discovery_db, monkeypatch,
):
    """The Halo case: a single Calibre series id with books from 2+
    distinct authors is genuinely shared. Seshat creates ONE shared
    row (author_id=NULL) and links every book to it regardless of
    primary author."""
    from app.discovery import calibre_sync

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [
            _book(1, "The Fall of Reach",
                  "Eric Nylund", author_id=101,
                  series_name="Halo", series_id=42,
                  series_index=1.0),
            _book(2, "Halo: The Cole Protocol",
                  "Tobias S. Buckell", author_id=28,
                  series_name="Halo", series_id=42,
                  series_index=6.0),
            _book(3, "Halo: Glasslands",
                  "Karen Traviss", author_id=74,
                  series_name="Halo", series_id=42,
                  series_index=8.0),
        ]},
    )
    await calibre_sync.sync_calibre("x", "y")

    series = await _series_rows()
    halo_rows = [s for s in series if s["name"] == "Halo"]
    assert len(halo_rows) == 1, f"expected 1 shared Halo row, got {halo_rows}"
    assert halo_rows[0]["author_id"] is None, \
        "shared series row must have NULL author_id"

    books = await _book_rows()
    assert len(books) == 3
    # Every book points at the shared row.
    shared_id = halo_rows[0]["id"]
    for b in books:
        assert b["series_id"] == shared_id, \
            f"book '{b['title']}' not on shared row"


async def test_distinct_calibre_series_with_same_name_stay_per_author(
    discovery_db, monkeypatch,
):
    """The Cressman/Savarovsky guard: two DIFFERENT Calibre series
    ids that happen to share a name remain per-author rows. The
    Halo logic only kicks in when one Calibre series id has 2+
    contributors."""
    from app.discovery import calibre_sync

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [
            _book(1, "The Last Paladin",
                  "John Cressman", author_id=580,
                  series_name="The Last Paladin", series_id=100,
                  series_index=1.0),
            _book(2, "The Last Paladin #1",
                  "Roman Savarovsky", author_id=549,
                  series_name="The Last Paladin", series_id=200,
                  series_index=1.0),
        ]},
    )
    await calibre_sync.sync_calibre("x", "y")

    series = await _series_rows()
    paladin = [s for s in series if s["name"] == "The Last Paladin"]
    assert len(paladin) == 2
    assert all(s["author_id"] is not None for s in paladin), \
        "distinct Calibre series ids must NOT collapse to a shared row"


async def test_legacy_per_author_rows_collapsed_into_shared(
    discovery_db, monkeypatch,
):
    """Pre-v2.3 DBs may have per-author Halo rows split across
    every author (the v2.2.7 fragmentation regression). On the
    next Calibre sync — once we detect the multi-author signal —
    Pass 2 cleanup re-points those books to the new shared row
    and deletes the legacy per-author rows."""
    from app.discovery import calibre_sync
    from app.discovery.database import get_db

    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        # Hand-seed pre-existing per-author rows + books matching
        # the legacy state Mark would have on his live DB. Set
        # normalized_name + calibre_id so Pass 1 of calibre_sync
        # finds these rows on its upsert lookup.
        nylund_norm = normalize_author_name("Eric Nylund")
        buckell_norm = normalize_author_name("Tobias S. Buckell")
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, calibre_id, normalized_name) "
            "VALUES (101, 'Eric Nylund', 'Nylund, Eric', 101, ?), "
            "(28, 'Tobias S. Buckell', 'Buckell, Tobias S.', 28, ?)",
            (nylund_norm, buckell_norm),
        )
        await db.execute(
            "INSERT INTO series (id, name, author_id) VALUES "
            "(900, 'Halo', 101), (901, 'Halo', 28)"
        )
        await db.execute(
            "INSERT INTO books (id, title, author_id, series_id, source, owned, calibre_id) "
            "VALUES (1, 'Reach', 101, 900, 'calibre', 1, 1), "
            "(2, 'Cole Protocol', 28, 901, 'calibre', 1, 2)"
        )
        await db.commit()
    finally:
        await db.close()

    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [
            _book(1, "Reach", "Eric Nylund", author_id=101,
                  series_name="Halo", series_id=42, series_index=1.0),
            _book(2, "Cole Protocol", "Tobias S. Buckell", author_id=28,
                  series_name="Halo", series_id=42, series_index=6.0),
        ]},
    )
    await calibre_sync.sync_calibre("x", "y")

    series = await _series_rows()
    halo_rows = [s for s in series if s["name"] == "Halo"]
    # Legacy per-author rows must be gone; one shared row remains.
    assert len(halo_rows) == 1
    assert halo_rows[0]["author_id"] is None

    books = await _book_rows()
    assert len(books) == 2
    shared_id = halo_rows[0]["id"]
    for b in books:
        assert b["series_id"] == shared_id
