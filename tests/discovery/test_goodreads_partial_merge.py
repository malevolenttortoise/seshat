"""A Goodreads scan cut off by the per-source cap keeps what it finished
(2026-10 audit issue 12; Mark, G35).

The retry loop used to give up (budget, retry cap, no progress) with the
finished books still sitting in the source's resume point, unmerged:
"partial writes are durable" was never true for Goodreads.
"""
from __future__ import annotations

import asyncio

import pytest

from app.discovery.sources.base import AuthorResult, BookResult


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    from app import config as app_config
    from app import database
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "seshat.db")
    await database.init_db()
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    from app import state
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "test", "content_type": "ebook", "name": "Test"},
    ])
    yield tmp_path
    disco_db.set_active_library(None)


async def _seed_author_with_owned_book() -> int:
    from app.discovery.database import get_db
    db = await get_db("test")
    try:
        aid = (await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES ('Toni Placeholder', 'Placeholder, Toni')",
        )).lastrowid
        bid = (await db.execute(
            "INSERT INTO books (title, owned, source) VALUES ('Owned Book', 1, 'calibre')",
        )).lastrowid
        await db.execute(
            "INSERT INTO book_authors (book_id, author_id, position) VALUES (?, ?, 0)",
            (bid, aid))
        await db.commit()
        return aid
    finally:
        await db.close()


async def _titles(aid: int) -> set[str]:
    from app.discovery.database import get_db
    db = await get_db("test")
    try:
        rows = await (await db.execute(
            "SELECT b.title FROM books b JOIN book_authors ba ON ba.book_id = b.id "
            "WHERE ba.author_id = ?", (aid,))).fetchall()
        return {r["title"] for r in rows}
    finally:
        await db.close()


def _gr_book(title: str, gid: str) -> BookResult:
    return BookResult(
        title=title, external_id=gid, source="goodreads",
        source_url=f"https://www.goodreads.com/book/show/{gid}",
    )


class _StuckGoodreads:
    """Finishes one new book, then never gets further before the cap."""

    name = "goodreads"

    def __init__(self):
        self._partial_state = None
        self._known_titles: set = set()
        self.calls = 0

    async def search_author(self, name, **kwargs):
        return AuthorResult(name=name, external_id="GR-1")

    async def get_author_books(self, author_id, existing_titles=None,
                               owned_titles=None, owned_only=False, start_at=0):
        self.calls += 1
        self._partial_state = {
            "author_id": author_id,
            "books": [_gr_book("Brand New Book", "901")],
            "series": [], "index": 1, "total": 5,
            "catalogue": ["Brand New Book", "Owned Book", "Three", "Four", "Five"],
        }
        await asyncio.sleep(30)


async def test_a_stalled_scan_merges_the_books_it_finished(discovery_db, monkeypatch):
    from app.discovery import lookup

    aid = await _seed_author_with_owned_book()
    stuck = _StuckGoodreads()
    monkeypatch.setattr(lookup, "_sources_for_content_type", lambda *a, **k: [
        lookup.SourceSpec("goodreads", "primary", 0.05, lambda: stuck, True),
    ])

    await lookup.lookup_author(aid, "Toni Placeholder")

    assert stuck.calls >= 2                      # timed out, then retried
    assert "Brand New Book" in await _titles(aid)
    assert stuck._partial_state is None
    # Goodreads was told which titles discovery already has (G47).
    assert "owned book" in lookup.goodreads._known_titles


async def test_a_partial_result_is_checked_against_the_whole_catalogue(discovery_db):
    """A partial holds the new books first, so the owned-title check looks
    at the catalogue; and a failed check never retracts earlier books."""
    from app.discovery import lookup
    from app.discovery.database import get_db

    aid = await _seed_author_with_owned_book()
    db = await get_db("test")
    try:
        earlier = (await db.execute(
            "INSERT INTO books (title, owned, source, source_url) VALUES "
            "('Earlier Find', 0, 'goodreads', '{\"goodreads\": \"https://www.goodreads.com/book/show/7\"}')",
        )).lastrowid
        await db.execute(
            "INSERT INTO book_authors (book_id, author_id, position) VALUES (?, ?, 0)",
            (earlier, aid))
        await db.commit()
    finally:
        await db.close()

    common = dict(
        author_name="Toni Placeholder", author_id=aid, our_titles=["Owned Book"],
        languages=["English"], full_scan=False, owned_only=False,
        series_collector={}, exclude_audiobooks=True, linked_author_ids=[aid],
        link_type_by_id={},
    )
    stranger = {"author_id": "GR-X", "books": [_gr_book("Stranger Book", "902")],
                "series": [], "index": 1, "total": 2,
                "catalogue": ["Stranger Book", "Another Stranger"]}
    assert await lookup._merge_unfinished_source("goodreads", stranger, **common) == 0
    assert "Stranger Book" not in await _titles(aid)
    assert "Earlier Find" in await _titles(aid)          # not retracted

    ours = {"author_id": "GR-1", "books": [_gr_book("Brand New Book", "901")],
            "series": [], "index": 1, "total": 2,
            "catalogue": ["Brand New Book", "Owned Book"]}
    assert await lookup._merge_unfinished_source("goodreads", ours, **common) == 1
    assert "Brand New Book" in await _titles(aid)
