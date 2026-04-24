"""
HTTP-level tests for `/api/discovery/authors/{id}` detail response.

Covers the empty-series filter: a series with every author-linked book
hidden shouldn't appear in the response, otherwise the frontend renders
a "(0/0)" tile with no books.
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
    # Register the library so `_author_detail_for_slug` resolves
    # the content_type via `state._discovered_libraries`.
    from app import state
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "test", "content_type": "ebook", "name": "Test"},
    ])
    yield tmp_path
    disco_db.set_active_library(None)


async def _seed(author_name: str, series_name: str, titles_hidden: list[tuple[str, int]]) -> int:
    """Insert an author + series + books. Returns the author id.

    `titles_hidden` is a list of `(title, hidden_flag)` tuples.
    """
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES (?, ?)",
            (author_name, author_name),
        )
        aid = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO series (name, author_id) VALUES (?, ?)",
            (series_name, aid),
        )
        sid = cur.lastrowid
        for title, hidden in titles_hidden:
            await db.execute(
                "INSERT INTO books (title, author_id, series_id, hidden, owned) "
                "VALUES (?, ?, ?, ?, ?)",
                (title, aid, sid, hidden, 0),
            )
        await db.commit()
        return aid
    finally:
        await db.close()


@pytest.fixture
async def client(discovery_db):
    from app.discovery.routers.authors import router

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_series_with_all_books_hidden_is_omitted(client):
    aid = await _seed("Alice Author", "Mercy Temple",
                      [("Book 1", 1), ("Book 2", 1)])
    r = await client.get(f"/api/discovery/authors/{aid}")
    assert r.status_code == 200
    assert r.json()["series"] == []


async def test_series_with_one_visible_book_still_appears(client):
    aid = await _seed("Alice Author", "Mercy Temple",
                      [("Book 1", 0), ("Book 2", 1)])
    r = await client.get(f"/api/discovery/authors/{aid}")
    assert r.status_code == 200
    series = r.json()["series"]
    assert len(series) == 1
    assert series[0]["author_book_count"] == 1
