"""
v2.3.4.3 — `owned` query param on `/api/discovery/books/hidden`.

Pre-v2.3.4.3 the Hidden page mixed all hidden books regardless of
ownership. Mark's UAT canary: 19 owned books bulk-hidden by
mistake; finding them required scrolling past every discovered
miss. The `owned` filter narrows the page to one subset or the
other.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI


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


@pytest.fixture
async def client(discovery_db):
    from app.discovery.routers import books as books_router

    app = FastAPI()
    app.include_router(books_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _seed_books():
    """Seed 3 owned-hidden + 2 discovered-hidden + 1 owned-visible."""
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (101, 'A', 'A', ?)",
            (normalize_author_name("A"),),
        )
        # Owned + hidden
        for i, t in enumerate(["Owned1", "Owned2", "Owned3"], start=1):
            await db.execute(
                "INSERT INTO books (id, title, owned, hidden, source) "
                "VALUES (?, ?, 1, 1, 'calibre')",
                (i, t),
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, 101, 0)", (i,),
            )
        # Discovered + hidden
        for i, t in enumerate(["Disc1", "Disc2"], start=10):
            await db.execute(
                "INSERT INTO books (id, title, owned, hidden, source) "
                "VALUES (?, ?, 0, 1, 'goodreads')",
                (i, t),
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, 101, 0)", (i,),
            )
        # Owned + visible (should never appear on /books/hidden)
        await db.execute(
            "INSERT INTO books (id, title, owned, hidden, source) "
            "VALUES (99, 'OwnedVisible', 1, 0, 'calibre')"
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (99, 101, 0)"
        )
        await db.commit()
    finally:
        await db.close()


class TestHiddenOwnedFilter:
    async def test_default_returns_all_hidden(self, client):
        await _seed_books()
        r = await client.get("/api/discovery/books/hidden")
        body = r.json()
        ids = {b["id"] for b in body["books"]}
        assert ids == {1, 2, 3, 10, 11}  # 5 hidden, OwnedVisible excluded

    async def test_owned_true_returns_only_owned_hidden(self, client):
        await _seed_books()
        r = await client.get("/api/discovery/books/hidden?owned=true")
        body = r.json()
        ids = {b["id"] for b in body["books"]}
        assert ids == {1, 2, 3}

    async def test_owned_false_returns_only_discovered_hidden(self, client):
        await _seed_books()
        r = await client.get("/api/discovery/books/hidden?owned=false")
        body = r.json()
        ids = {b["id"] for b in body["books"]}
        assert ids == {10, 11}

    async def test_search_combines_with_owned_filter(self, client):
        await _seed_books()
        r = await client.get(
            "/api/discovery/books/hidden?owned=true&search=Owned1",
        )
        body = r.json()
        ids = {b["id"] for b in body["books"]}
        assert ids == {1}
