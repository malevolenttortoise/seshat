"""v3.0.0 Phase 2 — sync writers dual-write to book_authors.

The `write_book_authors` helper (in `app/discovery/database.py`) is
the shared INSERT-or-replace path that calibre_sync + audiobookshelf_sync
both call after every books-row INSERT/UPDATE. These tests cover:

  - the helper itself: delete-then-insert semantics, ordered dedup,
    empty no-op (defensive against transient resolution failures).
  - calibre_sync end-to-end: a multi-author Calibre book produces
    the right book_authors rows; a re-sync after a co-author is
    removed from Calibre actually drops the link (the v3.0.0
    trigger pathology — pre-Phase-2, the dropped author would
    have stayed in book_authors from Phase 1B's backfill).
  - audiobookshelf_sync end-to-end: same shape for the ABS side.
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


async def _book_authors(book_id: int) -> list[tuple[int, int, str]]:
    """Return (position, author_id, author_name) tuples in position order."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT ba.position, ba.author_id, a.name "
            "FROM book_authors ba JOIN authors a ON a.id = ba.author_id "
            "WHERE ba.book_id = ? ORDER BY ba.position",
            (book_id,),
        )).fetchall()
        return [(r["position"], r["author_id"], r["name"]) for r in rows]
    finally:
        await db.close()


# ─── write_book_authors helper ───────────────────────────────


async def test_write_replaces_existing_links(discovery_db):
    """Re-running with a different author set DELETEs the old rows
    and INSERTs the new — drops any author no longer in the set."""
    from app.discovery.database import get_db, write_book_authors
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name) VALUES "
            "(1, 'Alice', 'Alice'), (2, 'Bob', 'Bob'), (3, 'Carol', 'Carol')"
        )
        await db.execute(
            "INSERT INTO books (id, title) VALUES (10, 'B')"
        )
        # First pass: Alice + Bob.
        await write_book_authors(db, 10, [1, 2])
        await db.commit()
        # Second pass: Alice + Carol (Bob dropped).
        n = await write_book_authors(db, 10, [1, 3])
        await db.commit()
    finally:
        await db.close()

    assert n == 2
    rows = await _book_authors(10)
    assert [(p, name) for p, _aid, name in rows] == [(0, "Alice"), (1, "Carol")]


async def test_write_empty_list_is_noop(discovery_db):
    """Empty input leaves existing rows alone — defensive against
    transient resolution failure (a sync where author_id_map was
    empty for this book shouldn't nuke prior good data)."""
    from app.discovery.database import get_db, write_book_authors
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name) VALUES (1, 'Alice', 'Alice')"
        )
        await db.execute(
            "INSERT INTO books (id, title) VALUES (10, 'B')"
        )
        await write_book_authors(db, 10, [1])
        await db.commit()
        n = await write_book_authors(db, 10, [])
        await db.commit()
    finally:
        await db.close()

    assert n == 0
    rows = await _book_authors(10)
    assert rows == [(0, 1, "Alice")]


async def test_write_dedupes_preserving_first_position(discovery_db):
    """Duplicate author_ids in the input collapse to one row, kept
    at the earlier position. Defensive against upstream library
    quirks (e.g. Calibre listing the same person twice)."""
    from app.discovery.database import get_db, write_book_authors
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name) VALUES "
            "(1, 'Alice', 'Alice'), (2, 'Bob', 'Bob')"
        )
        await db.execute(
            "INSERT INTO books (id, title) VALUES (10, 'B')"
        )
        n = await write_book_authors(db, 10, [1, 2, 1, 2])
        await db.commit()
    finally:
        await db.close()

    assert n == 2
    rows = await _book_authors(10)
    assert [(p, name) for p, _aid, name in rows] == [(0, "Alice"), (1, "Bob")]


async def test_write_drops_none_entries(discovery_db):
    """None entries in the input (unresolved name) are skipped
    without breaking the position sequence for resolved ones."""
    from app.discovery.database import get_db, write_book_authors
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name) VALUES "
            "(1, 'Alice', 'Alice'), (2, 'Bob', 'Bob')"
        )
        await db.execute(
            "INSERT INTO books (id, title) VALUES (10, 'B')"
        )
        n = await write_book_authors(db, 10, [1, None, 2])  # type: ignore
        await db.commit()
    finally:
        await db.close()

    assert n == 2
    rows = await _book_authors(10)
    assert [(p, name) for p, _aid, name in rows] == [(0, "Alice"), (1, "Bob")]


# ─── calibre_sync end-to-end ─────────────────────────────────


def _calibre_book(book_id, title, authors, **kw):
    """Minimal shape matching `_read_calibre_db`'s output for the
    fields calibre_sync.sync_calibre reads."""
    base = {
        "book_id": book_id,
        "title": title,
        "pubdate": "2024-01-01",
        "series_index": 1.0,
        "book_path": f"Author/{title}",
        "cover_path": None,
        "isbn": None,
        "authors": authors,  # list of {"id": int, "name": str, "sort": str}
        "series": [],
        "tags": None,
        "rating": None,
        "description": None,
        "language": None,
        "publisher": None,
        "formats": None,
    }
    base.update(kw)
    return base


async def test_calibre_sync_writes_multi_author_links(
    discovery_db, monkeypatch,
):
    """A 3-author Calibre book sync produces three book_authors rows
    in Calibre's array order on the INSERT path."""
    from app.discovery import calibre_sync
    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [_calibre_book(
            42, "Affliction",
            [
                {"id": 1, "name": "J.N. Chaney", "sort": "Chaney, J.N."},
                {"id": 2, "name": "Jonathan P. Brazee", "sort": "Brazee, Jonathan P."},
                {"id": 3, "name": "Thomas Webb", "sort": "Webb, Thomas"},
            ],
        )]},
    )
    await calibre_sync.sync_calibre("dummy_path", "dummy_url")

    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT id FROM books WHERE calibre_id = 42"
        )).fetchone()
    finally:
        await db.close()
    assert row is not None
    rows = await _book_authors(row["id"])
    assert [(p, name) for p, _aid, name in rows] == [
        (0, "J.N. Chaney"),
        (1, "Jonathan P. Brazee"),
        (2, "Thomas Webb"),
    ]


async def test_calibre_resync_drops_removed_coauthor(
    discovery_db, monkeypatch,
):
    """The v3.0.0 trigger pathology: a re-sync where the user removed
    a co-author from Calibre must remove that author's link from
    book_authors too. Pre-Phase-2 (just the Phase 1B backfill) the
    dropped author would stay in the join table because backfill
    only INSERTs — write_book_authors's DELETE-then-INSERT is what
    fixes this."""
    from app.discovery import calibre_sync

    # First sync: both authors.
    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [_calibre_book(
            42, "Able Bodied Soldier",
            [
                {"id": 1, "name": "J.N. Chaney", "sort": "Chaney, J.N."},
                {"id": 2, "name": "Jason Anspach", "sort": "Anspach, Jason"},
            ],
        )]},
    )
    await calibre_sync.sync_calibre("dummy_path", "dummy_url")

    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT id FROM books WHERE calibre_id = 42"
        )).fetchone()
    finally:
        await db.close()
    book_id = row["id"]
    assert len(await _book_authors(book_id)) == 2

    # Second sync: user removed Jason Anspach in Calibre.
    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [_calibre_book(
            42, "Able Bodied Soldier",
            [{"id": 1, "name": "J.N. Chaney", "sort": "Chaney, J.N."}],
        )]},
    )
    await calibre_sync.sync_calibre("dummy_path", "dummy_url")

    rows = await _book_authors(book_id)
    assert [(p, name) for p, _aid, name in rows] == [(0, "J.N. Chaney")]


# ─── audiobookshelf_sync end-to-end ──────────────────────────


def _abs_book(abs_id, title, authors, **kw):
    base = {
        "abs_id": abs_id,
        "title": title,
        "authors": authors,  # list of strings
        "series_name": None,
        "series_index": None,
        "isbn": None,
        "asin": None,
        "pub_date": None,
        "description": None,
        "language": None,
        "publisher": None,
        "narrator": None,
        "duration_sec": None,
        "abridged": False,
        "audio_formats": None,
    }
    base.update(kw)
    return base


# ABS sync end-to-end coverage lives in the dev-stack UAT (the API
# surface is heavy to mock — AudiobookshelfClient.iter_all_items +
# _get_abs_api_key + settings load — and the write_book_authors
# wire-up is symmetric to the calibre_sync case covered above).
