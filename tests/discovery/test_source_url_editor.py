"""
v2.3.2 source URL editor endpoint tests.

POST /api/discovery/books/{bid}/source-urls       — parse + merge
DELETE /api/discovery/books/{bid}/source-urls/{source_name} — drop one
"""
from __future__ import annotations

import json

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


async def _seed_book(initial_source_url=None) -> int:
    """Insert one book + author. Returns the book id."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name) VALUES (1, 'A', 'A')"
        )
        await db.execute(
            "INSERT INTO books (id, title, source_url) "
            "VALUES (1, 'Quarks and Qi', ?)",
            (initial_source_url,),
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (1, 1, 0)"
        )
        await db.commit()
        return 1
    finally:
        await db.close()


async def _stored_urls(bid: int) -> dict:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT source_url FROM books WHERE id = ?", (bid,)
        )).fetchone()
        if not row or not row["source_url"]:
            return {}
        return json.loads(row["source_url"])
    finally:
        await db.close()


class TestAddSourceUrl:
    async def test_parses_and_canonicalizes(self, client):
        await _seed_book()
        r = await client.post(
            "/api/discovery/books/1/source-urls",
            json={"url": "https://www.goodreads.com/book/show/246416427-quarks-and-qi"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["added"] == "goodreads"
        assert body["canonical_url"] == \
            "https://www.goodreads.com/book/show/246416427"
        assert body["source_url"] == {
            "goodreads": "https://www.goodreads.com/book/show/246416427",
        }
        assert await _stored_urls(1) == {
            "goodreads": "https://www.goodreads.com/book/show/246416427",
        }

    async def test_merges_with_existing_urls(self, client):
        existing = json.dumps({
            "kobo": "https://www.kobo.com/us/en/ebook/the-name-of-the-wind",
        })
        await _seed_book(initial_source_url=existing)
        r = await client.post(
            "/api/discovery/books/1/source-urls",
            json={"url": "https://hardcover.app/books/name-of-the-wind"},
        )
        assert r.status_code == 200
        urls = r.json()["source_url"]
        # Both keys present, both canonical.
        assert set(urls) == {"kobo", "hardcover"}
        assert urls["hardcover"] == "https://hardcover.app/books/name-of-the-wind"

    async def test_replaces_existing_same_source(self, client):
        """Pasting a second Goodreads URL replaces the first one
        rather than appending — there can only be one URL per source."""
        existing = json.dumps({
            "goodreads": "https://www.goodreads.com/book/show/111",
        })
        await _seed_book(initial_source_url=existing)
        r = await client.post(
            "/api/discovery/books/1/source-urls",
            json={"url": "https://www.goodreads.com/book/show/222-different"},
        )
        urls = r.json()["source_url"]
        assert urls == {"goodreads": "https://www.goodreads.com/book/show/222"}

    async def test_400_on_unrecognized_url(self, client):
        await _seed_book()
        r = await client.post(
            "/api/discovery/books/1/source-urls",
            json={"url": "https://example.com/some-page"},
        )
        assert r.status_code == 400
        assert "Could not identify" in r.text

    async def test_400_on_missing_url(self, client):
        await _seed_book()
        r = await client.post(
            "/api/discovery/books/1/source-urls", json={},
        )
        assert r.status_code == 400

    async def test_404_on_missing_book(self, client):
        r = await client.post(
            "/api/discovery/books/999/source-urls",
            json={"url": "https://www.goodreads.com/book/show/123"},
        )
        assert r.status_code == 404


class TestRemoveSourceUrl:
    async def test_removes_named_source(self, client):
        existing = json.dumps({
            "goodreads": "https://www.goodreads.com/book/show/123",
            "kobo": "https://www.kobo.com/us/en/ebook/test",
        })
        await _seed_book(initial_source_url=existing)
        r = await client.delete("/api/discovery/books/1/source-urls/goodreads")
        assert r.status_code == 200
        urls = r.json()["source_url"]
        assert urls == {"kobo": "https://www.kobo.com/us/en/ebook/test"}
        assert await _stored_urls(1) == urls

    async def test_idempotent_when_source_absent(self, client):
        existing = json.dumps({
            "goodreads": "https://www.goodreads.com/book/show/123",
        })
        await _seed_book(initial_source_url=existing)
        r = await client.delete("/api/discovery/books/1/source-urls/kobo")
        assert r.status_code == 200
        # Unchanged.
        assert r.json()["source_url"] == {
            "goodreads": "https://www.goodreads.com/book/show/123",
        }

    async def test_clearing_last_source_writes_null(self, client):
        existing = json.dumps({
            "goodreads": "https://www.goodreads.com/book/show/123",
        })
        await _seed_book(initial_source_url=existing)
        await client.delete("/api/discovery/books/1/source-urls/goodreads")
        # Empty dict → store NULL instead of "{}" so the URL-backfill
        # logic that gates on `if raw_urls` continues to work.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT source_url FROM books WHERE id = 1"
            )).fetchone()
            assert row["source_url"] is None
        finally:
            await db.close()

    async def test_404_on_missing_book(self, client):
        r = await client.delete(
            "/api/discovery/books/999/source-urls/goodreads"
        )
        assert r.status_code == 404


class TestMalformedExistingJson:
    """Pre-v1.x writes used a plain string format. A legacy row with a
    bare URL string in source_url should be silently overwritten on
    the next add — not 500."""

    async def test_legacy_string_overwritten(self, client):
        await _seed_book(initial_source_url="https://legacy-format/url")
        r = await client.post(
            "/api/discovery/books/1/source-urls",
            json={"url": "https://www.goodreads.com/book/show/123"},
        )
        assert r.status_code == 200
        # Legacy plain string was dropped; new dict has only the
        # added entry.
        assert r.json()["source_url"] == {
            "goodreads": "https://www.goodreads.com/book/show/123",
        }
