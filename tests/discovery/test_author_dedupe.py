"""
Tests for `_dedupe_author_rows` — the one-time pass that merges
pre-existing duplicate author rows detected by matching
`normalized_name`.
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


async def _seed_two_authors(name_a: str, name_b: str,
                            books_a: int = 1, books_b: int = 1):
    """Seed two author rows that normalize to the same form, with
    the given number of books each. Returns the two author ids."""
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name) "
            "VALUES (?, ?, ?)",
            (name_a, name_a, normalize_author_name(name_a)),
        )
        aid_a = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name) "
            "VALUES (?, ?, ?)",
            (name_b, name_b, normalize_author_name(name_b)),
        )
        aid_b = cur.lastrowid
        for i in range(books_a):
            cur = await db.execute(
                "INSERT INTO books (title) VALUES (?)",
                (f"A{i}",),
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, ?, 0)",
                (cur.lastrowid, aid_a),
            )
        for i in range(books_b):
            cur = await db.execute(
                "INSERT INTO books (title) VALUES (?)",
                (f"B{i}",),
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, ?, 0)",
                (cur.lastrowid, aid_b),
            )
        await db.commit()
        return aid_a, aid_b
    finally:
        await db.close()


async def _author_rows():
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT id, name, normalized_name FROM authors ORDER BY id"
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def _count_books_by_author(author_id: int) -> int:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT COUNT(*) c FROM book_authors WHERE author_id = ?",
            (author_id,),
        )).fetchone()
        return row["c"]
    finally:
        await db.close()


async def test_more_punctuation_wins_and_loser_books_reparent(discovery_db):
    # "A. K. DuBoff" (more periods) should win over "A K DuBoff",
    # regardless of which was inserted first or has more books.
    from app.discovery.database import _dedupe_author_rows, get_db

    aid_a, aid_b = await _seed_two_authors(
        "A. K. DuBoff", "A K DuBoff", books_a=3, books_b=5,
    )
    db = await get_db()
    try:
        merged = await _dedupe_author_rows(db)
    finally:
        await db.close()

    assert merged == 1
    rows = await _author_rows()
    assert len(rows) == 1
    assert rows[0]["id"] == aid_a
    assert rows[0]["name"] == "A. K. DuBoff"
    assert await _count_books_by_author(aid_a) == 8


async def test_tiebreak_on_book_count(discovery_db):
    # Equal period count on both — book count decides.
    from app.discovery.database import _dedupe_author_rows, get_db

    aid_less_books, aid_more_books = await _seed_two_authors(
        "A K DuBoff", "a k duboff", books_a=1, books_b=4,
    )
    db = await get_db()
    try:
        await _dedupe_author_rows(db)
    finally:
        await db.close()

    rows = await _author_rows()
    assert len(rows) == 1
    assert rows[0]["id"] == aid_more_books
    assert await _count_books_by_author(aid_more_books) == 5


async def test_series_reparented(discovery_db):
    from app.discovery.database import _dedupe_author_rows, get_db

    aid_a, aid_b = await _seed_two_authors(
        "A. K. DuBoff", "A K DuBoff", books_a=1, books_b=1,
    )
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO series (name, author_id) VALUES (?, ?)",
            ("Cadicle", aid_b),  # Series under the losing row.
        )
        await db.commit()
        await _dedupe_author_rows(db)
        rows = await (await db.execute(
            "SELECT author_id FROM series"
        )).fetchall()
    finally:
        await db.close()

    assert len(rows) == 1
    assert rows[0]["author_id"] == aid_a


async def test_pen_name_links_reparented(discovery_db):
    # A pen_name_link pointing at a loser row should follow the merge.
    from app.discovery.database import _dedupe_author_rows, get_db

    aid_a, aid_b = await _seed_two_authors(
        "A. K. DuBoff", "A K DuBoff",
    )
    db = await get_db()
    try:
        # Also seed an unrelated author to be the other end of the link.
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name) "
            "VALUES ('Amy DuBoff', 'DuBoff, Amy', 'amy duboff')"
        )
        other = cur.lastrowid
        await db.execute(
            "INSERT INTO pen_name_links "
            "(canonical_author_id, alias_author_id, link_type) "
            "VALUES (?, ?, 'pen_name')",
            (aid_b, other),  # Link lives on the losing row.
        )
        await db.commit()
        await _dedupe_author_rows(db)
        rows = await (await db.execute(
            "SELECT canonical_author_id, alias_author_id FROM pen_name_links"
        )).fetchall()
    finally:
        await db.close()

    assert len(rows) == 1
    assert rows[0]["canonical_author_id"] == aid_a
    assert rows[0]["alias_author_id"] != aid_b


async def test_self_referencing_pen_name_link_dropped(discovery_db):
    # If two authors were linked and we then merge them, the surviving
    # link would point canonical → canonical. Drop it.
    from app.discovery.database import _dedupe_author_rows, get_db

    aid_a, aid_b = await _seed_two_authors(
        "A. K. DuBoff", "A K DuBoff",
    )
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO pen_name_links "
            "(canonical_author_id, alias_author_id, link_type) "
            "VALUES (?, ?, 'pen_name')",
            (aid_a, aid_b),
        )
        await db.commit()
        await _dedupe_author_rows(db)
        rows = await (await db.execute(
            "SELECT COUNT(*) c FROM pen_name_links"
        )).fetchone()
    finally:
        await db.close()

    assert rows["c"] == 0


async def test_distinct_authors_untouched(discovery_db):
    # Sanity check — different normalized forms should survive.
    from app.discovery.database import _dedupe_author_rows, get_db

    await _seed_two_authors(
        "Brandon Sanderson", "Pierce Brown",
    )
    db = await get_db()
    try:
        merged = await _dedupe_author_rows(db)
    finally:
        await db.close()

    assert merged == 0
    rows = await _author_rows()
    assert len(rows) == 2


async def test_idempotent_second_run(discovery_db):
    from app.discovery.database import _dedupe_author_rows, get_db

    await _seed_two_authors("A. K. DuBoff", "A K DuBoff")
    db = await get_db()
    try:
        first = await _dedupe_author_rows(db)
        second = await _dedupe_author_rows(db)
    finally:
        await db.close()

    assert first == 1
    assert second == 0  # nothing left to merge
