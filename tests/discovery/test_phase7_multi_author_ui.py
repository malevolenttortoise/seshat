"""
v3.0.0 Phase 7 — multi-author UI backend contract.

Three new read behaviors land in Phase 7:
  1. `/discovery/books?author_id=X` is contributor-aware (book_authors),
     so a co-author who is never a primary still surfaces their books.
  2. Every book-list response carries an ordered `contributors` array
     (the multi-author byline data) via `attach_contributors`.
  3. Author-detail series carry `is_owner` — computed on read by
     count-equality (ADR-0011): the author contributes to every visible
     linked book of the series → owner (full series); else incidental
     guest (own entries + an "N of M" pill).
  4. `/discovery/series/{sid}/authors` returns the stored `author_mode`
     so the modal header shows the 3-way label.

Fixture shape: author A (Alice) is primary on books; co-author B (Bob) is
a position-1 contributor on some. Series S has book1 {A,B} + book2 {A} —
so I (the owner set) = {A}: Alice owns S, Bob is a guest (1 of 2). A
co-authored standalone book3 {A,B} exercises the contributor-aware filter
+ byline outside a series.
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
    """Seed Alice (primary), Bob (co-author), a 2-book series + a
    co-authored standalone. Returns the ids dict."""
    from app.discovery.database import get_db, write_book_authors
    from app.discovery.routers.series import _recompute_series_author
    db = await get_db()
    try:
        a = (await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES ('Alice','Alice')"
        )).lastrowid
        b = (await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES ('Bob','Bob')"
        )).lastrowid
        sid = (await db.execute(
            "INSERT INTO series (name, author_id) VALUES ('S', ?)", (a,)
        )).lastrowid

        async def book(title, sid_, idx, primary):
            return (await db.execute(
                "INSERT INTO books (title, series_id, series_index, "
                "owned, hidden) VALUES (?,?,?,1,0)",
                (title, sid_, idx),
            )).lastrowid

        b1 = await book("S Book 1", sid, 1, a)   # {Alice, Bob}
        b2 = await book("S Book 2", sid, 2, a)   # {Alice}
        b3 = await book("Standalone", None, None, a)  # {Alice, Bob}
        await write_book_authors(db, b1, [a, b])
        await write_book_authors(db, b2, [a])
        await write_book_authors(db, b3, [a, b])
        await db.commit()
        await _recompute_series_author(db, [sid])
        await db.commit()
        return {"alice": a, "bob": b, "sid": sid, "b1": b1, "b2": b2, "b3": b3}
    finally:
        await db.close()


@pytest.fixture
async def client(discovery_db):
    from app.discovery.routers.authors import router as authors_router
    from app.discovery.routers.books import router as books_router
    from app.discovery.routers.series import router as series_router

    app = FastAPI()
    app.include_router(authors_router)
    app.include_router(books_router)
    app.include_router(series_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_books_author_id_is_contributor_aware(client, seeded):
    """Bob is never a PRIMARY author, yet his co-authored books surface
    (pre-Phase-7 this filter was `b.author_id=?` → 0 rows for Bob)."""
    r = await client.get(f"/api/discovery/books?author_id={seeded['bob']}&per_page=50")
    titles = {b["title"] for b in r.json()["books"]}
    assert titles == {"S Book 1", "Standalone"}, titles
    # Count is exact (IN-subquery doesn't fan out).
    assert r.json()["total"] == 2


async def test_book_response_carries_ordered_contributors(client, seeded):
    r = await client.get("/api/discovery/books?per_page=50")
    by_title = {b["title"]: b for b in r.json()["books"]}
    b1 = by_title["S Book 1"]
    names = [c["name"] for c in b1["contributors"]]
    positions = [c["position"] for c in b1["contributors"]]
    assert names == ["Alice", "Bob"], names      # primary first
    assert positions == [0, 1]
    # Single-author book still carries a 1-element contributors list.
    assert [c["name"] for c in by_title["S Book 2"]["contributors"]] == ["Alice"]


async def test_books_author_id_series_scoped_returns_only_own_entries(client, seeded):
    """The guest own-entries fetch the UI uses: Bob's entries in series S =
    only Book 1 (he's not on Book 2)."""
    r = await client.get(
        f"/api/discovery/books?author_id={seeded['bob']}&series_id={seeded['sid']}"
    )
    assert {b["title"] for b in r.json()["books"]} == {"S Book 1"}


async def test_author_detail_is_owner_owner_vs_guest(client, seeded):
    # Alice is in every book of S → owner.
    ra = await client.get(f"/api/discovery/authors/{seeded['alice']}")
    sa = next(s for s in ra.json()["series"] if s["id"] == seeded["sid"])
    assert sa["is_owner"] is True

    # Bob is in 1 of 2 → incidental guest; pill counts = author_book_count
    # (1) of book_count (2).
    rb = await client.get(f"/api/discovery/authors/{seeded['bob']}")
    sb = next(s for s in rb.json()["series"] if s["id"] == seeded["sid"])
    assert sb["is_owner"] is False
    assert sb["author_book_count"] == 1
    assert sb["book_count"] == 2


async def test_series_authors_returns_author_mode(client, seeded):
    r = await client.get(f"/api/discovery/series/{seeded['sid']}/authors")
    body = r.json()
    # I = {Alice} (Bob only on book 1) → per_author.
    assert body["author_mode"] == "per_author"
    assert "authors" in body
