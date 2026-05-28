"""
v3.0.0 Phase 8 — series detail `contributing_authors` contract.

`GET /discovery/series/{sid}` gains a `contributing_authors[]` for the
new read-only series detail page: every author on any visible book of the
series, with their in-series book_count + an `is_owner` flag (ADR-0011
count-equality — in EVERY visible linked book → owner; some → incidental).

Fixture: series S with book1 {Alice, Bob} + book2 {Alice}. Alice is in
every book → owner; Bob is in one → incidental guest.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI


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


@pytest.fixture
async def seeded(discovery_db):
    from app.discovery.database import get_db, write_book_authors
    from app.discovery.routers.series import _recompute_series_author
    db = await get_db()
    try:
        a = (await db.execute("INSERT INTO authors (name, sort_name) VALUES ('Alice','Alice')")).lastrowid
        b = (await db.execute("INSERT INTO authors (name, sort_name) VALUES ('Bob','Bob')")).lastrowid
        sid = (await db.execute("INSERT INTO series (name, author_id) VALUES ('S', ?)", (a,))).lastrowid

        async def book(title, idx, primary):
            return (await db.execute(
                "INSERT INTO books (title, author_id, series_id, series_index, owned, hidden) "
                "VALUES (?,?,?,?,1,0)", (title, primary, sid, idx),
            )).lastrowid

        b1 = await book("S Book 1", 1, a)
        b2 = await book("S Book 2", 2, a)
        await write_book_authors(db, b1, [a, b])  # Alice + Bob
        await write_book_authors(db, b2, [a])      # Alice only
        await db.commit()
        await _recompute_series_author(db, [sid])
        await db.commit()
        return {"alice": a, "bob": b, "sid": sid}
    finally:
        await db.close()


@pytest.fixture
async def client(discovery_db):
    from app.discovery.routers.series import router
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_series_detail_returns_contributing_authors(client, seeded):
    r = await client.get(f"/api/discovery/series/{seeded['sid']}")
    body = r.json()
    assert "contributing_authors" in body
    by_name = {a["name"]: a for a in body["contributing_authors"]}
    assert set(by_name) == {"Alice", "Bob"}


async def test_owner_vs_incidental_flags(client, seeded):
    body = (await client.get(f"/api/discovery/series/{seeded['sid']}")).json()
    by_name = {a["name"]: a for a in body["contributing_authors"]}
    # Alice in every book → owner, 2 books.
    assert by_name["Alice"]["is_owner"] is True
    assert by_name["Alice"]["book_count"] == 2
    # Bob in one book → incidental guest, 1 book.
    assert by_name["Bob"]["is_owner"] is False
    assert by_name["Bob"]["book_count"] == 1


async def test_owners_sorted_first_by_book_count(client, seeded):
    body = (await client.get(f"/api/discovery/series/{seeded['sid']}")).json()
    # Ordered by book_count DESC → Alice (2) before Bob (1).
    names = [a["name"] for a in body["contributing_authors"]]
    assert names == ["Alice", "Bob"]


async def test_books_carry_phase7_contributors_on_detail(client, seeded):
    """The detail page's book list reuses the Phase 7 byline data."""
    body = (await client.get(f"/api/discovery/series/{seeded['sid']}")).json()
    by_title = {b["title"]: b for b in body["books"]}
    assert [c["name"] for c in by_title["S Book 1"]["contributors"]] == ["Alice", "Bob"]
