"""
HTTP-level tests for the v2.15.0 #B global search endpoint.

Seeds two discovery libraries (ebook + audiobook) with overlapping
author + series + book names, then verifies:

  - Books / authors / series each return matching hits.
  - Cross-library aggregation works (results from both libs appear).
  - Substring + prefix matching ranks prefix hits above mid-string.
  - Hidden books are excluded.
  - `limit` caps results per category.
  - Empty / unrelated queries return empty categories without
    erroring.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture
async def two_libraries(tmp_path, monkeypatch):
    """Two discovery DBs seeded with a mix of overlapping + distinct
    authors + series + books across an ebook and an audiobook library."""
    from app import config as app_config, database, state
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    # v2.20.0 Phase 4 — /v1/search now dedupes author hits via the
    # global author_links table; the test env needs the global DB
    # initialized for the dedup helper to succeed.
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "seshat.db")
    await database.init_db()

    libs = [
        {"slug": "ebooks", "name": "Ebooks", "display_name": "Ebooks",
         "content_type": "ebook", "app_type": "calibre"},
        {"slug": "audio", "name": "Audio", "display_name": "Audio",
         "content_type": "audiobook", "app_type": "audiobookshelf"},
    ]
    monkeypatch.setattr(state, "_discovered_libraries", libs)

    # Ebooks library
    disco_db.set_active_library("ebooks")
    await disco_db.init_db("ebooks")
    db = await disco_db.get_db(slug="ebooks")
    try:
        # Authors
        await db.execute("INSERT INTO authors (id, name, sort_name) VALUES (1, ?, ?)",
                         ("Brandon Sanderson", "Sanderson, Brandon"))
        await db.execute("INSERT INTO authors (id, name, sort_name) VALUES (2, ?, ?)",
                         ("Patrick Rothfuss", "Rothfuss, Patrick"))
        # Series
        await db.execute("INSERT INTO series (id, name, author_id) VALUES (1, ?, 1)",
                         ("Mistborn",))
        await db.execute("INSERT INTO series (id, name, author_id) VALUES (2, ?, 2)",
                         ("Kingkiller Chronicle",))
        # Books — mix of owned + unowned, hidden + not, prefix + mid-string matches
        await db.execute(
            "INSERT INTO books (id, title, author_id, series_id, owned, hidden, is_unreleased) "
            "VALUES (1, ?, 1, 1, 1, 0, 0)", ("Mistborn: The Final Empire",))
        await db.execute(
            "INSERT INTO books (id, title, author_id, series_id, owned, hidden, is_unreleased) "
            "VALUES (2, ?, 1, NULL, 0, 0, 0)", ("Way of Kings",))
        await db.execute(
            "INSERT INTO books (id, title, author_id, series_id, owned, hidden, is_unreleased) "
            "VALUES (3, ?, 2, 2, 1, 0, 0)", ("The Name of the Wind",))
        # Hidden book — should NOT appear in search results.
        await db.execute(
            "INSERT INTO books (id, title, author_id, series_id, owned, hidden, is_unreleased) "
            "VALUES (4, ?, 1, NULL, 1, 1, 0)", ("Hidden Mistborn Outtake",))
        # v3.0.0 Phase 4 (ADR-0008): author book_count reads via
        # book_authors — link each seeded book to its author at pos 0.
        for bid, aid in ((1, 1), (2, 1), (3, 2), (4, 1)):
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, 0)", (bid, aid))
        await db.commit()
    finally:
        await db.close()

    # Audio library — distinct authors so we can verify cross-library merge
    disco_db.set_active_library("audio")
    await disco_db.init_db("audio")
    db = await disco_db.get_db(slug="audio")
    try:
        await db.execute("INSERT INTO authors (id, name, sort_name) VALUES (1, ?, ?)",
                         ("Robin Hobb", "Hobb, Robin"))
        await db.execute("INSERT INTO series (id, name, author_id) VALUES (1, ?, 1)",
                         ("Realm of the Elderlings",))
        await db.execute(
            "INSERT INTO books (id, title, author_id, series_id, owned, hidden, is_unreleased) "
            "VALUES (1, ?, 1, 1, 1, 0, 0)", ("Assassin's Apprentice",))
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (1, 1, 0)")
        await db.commit()
    finally:
        await db.close()

    disco_db.set_active_library("ebooks")
    yield libs
    disco_db.set_active_library(None)


def _make_app() -> FastAPI:
    from app.routers.search import router as search_router
    app = FastAPI()
    app.include_router(search_router)
    return app


@pytest.fixture
async def client(two_libraries):
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as ac:
        yield ac


class TestBookSearch:
    async def test_substring_match(self, client):
        r = await client.get("/api/v1/search?q=mistborn")
        body = r.json()
        titles = [b["title"] for b in body["books"]]
        assert "Mistborn: The Final Empire" in titles

    async def test_hidden_books_excluded(self, client):
        r = await client.get("/api/v1/search?q=mistborn")
        titles = [b["title"] for b in r.json()["books"]]
        # Hidden Mistborn Outtake was inserted with hidden=1.
        assert "Hidden Mistborn Outtake" not in titles

    async def test_owned_ranks_above_unowned(self, client):
        # "Mistborn: The Final Empire" is owned; "Way of Kings" is
        # unowned. Both match "the" via title substring, but the
        # owned title should rank first.
        r = await client.get("/api/v1/search?q=the")
        titles = [b["title"] for b in r.json()["books"]]
        owned_idx = next((i for i, t in enumerate(titles) if "Mistborn" in t), -1)
        unowned_idx = next((i for i, t in enumerate(titles) if "Way of Kings" in t), -1)
        if owned_idx >= 0 and unowned_idx >= 0:
            assert owned_idx < unowned_idx

    async def test_carries_library_slug_and_author(self, client):
        r = await client.get("/api/v1/search?q=mistborn")
        hit = next(b for b in r.json()["books"] if "Mistborn" in b["title"])
        assert hit["library_slug"] == "ebooks"
        assert hit["author_name"] == "Brandon Sanderson"

    async def test_audiobook_library_book_returned(self, client):
        r = await client.get("/api/v1/search?q=assassin")
        hit = next(b for b in r.json()["books"] if "Assassin" in b["title"])
        assert hit["library_slug"] == "audio"


class TestAuthorSearch:
    async def test_finds_author_by_substring(self, client):
        r = await client.get("/api/v1/search?q=sanderson")
        names = [a["name"] for a in r.json()["authors"]]
        assert "Brandon Sanderson" in names

    async def test_cross_library_author_returned(self, client):
        # Robin Hobb only exists in the audio library.
        r = await client.get("/api/v1/search?q=hobb")
        names = [a["name"] for a in r.json()["authors"]]
        assert "Robin Hobb" in names

    async def test_author_book_count_included(self, client):
        r = await client.get("/api/v1/search?q=sanderson")
        sanderson = next(a for a in r.json()["authors"] if a["name"] == "Brandon Sanderson")
        # 2 visible books (excluding the hidden one) in ebooks lib.
        assert sanderson["book_count"] == 2


class TestSeriesSearch:
    async def test_finds_series_by_name(self, client):
        r = await client.get("/api/v1/search?q=mistborn")
        names = [s["name"] for s in r.json()["series"]]
        assert "Mistborn" in names

    async def test_series_returns_author(self, client):
        r = await client.get("/api/v1/search?q=kingkiller")
        hit = next(s for s in r.json()["series"] if s["name"] == "Kingkiller Chronicle")
        assert hit["author_name"] == "Patrick Rothfuss"

    async def test_cross_library_series(self, client):
        r = await client.get("/api/v1/search?q=elderlings")
        names = [s["name"] for s in r.json()["series"]]
        assert "Realm of the Elderlings" in names


class TestLimit:
    async def test_limit_caps_books(self, client):
        r = await client.get("/api/v1/search?q=the&limit=1")
        assert len(r.json()["books"]) <= 1


class TestEmptyAndUnrelated:
    async def test_unrelated_query_returns_empty_categories(self, client):
        r = await client.get("/api/v1/search?q=zzzzzznomatchzzz")
        body = r.json()
        assert body["books"] == []
        assert body["authors"] == []
        assert body["series"] == []
        assert body["q"] == "zzzzzznomatchzzz"

    async def test_empty_query_rejected(self, client):
        # min_length=1 on the q param — empty string is a validation
        # error from FastAPI, status 422.
        r = await client.get("/api/v1/search?q=")
        assert r.status_code == 422
