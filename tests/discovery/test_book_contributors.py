"""HTTP-level tests for contributor removal/replacement on a book.

Covers `DELETE /api/discovery/books/{bid}/contributors/{author_id}`:
remove a co-author, promote on primary removal, role-preserving
renumber, the last-author 409 guard + replacement swap, the
`removed_author_orphaned` signal, and slug isolation. Plus the
slug-scoped `GET /api/discovery/authors/search` typeahead.

The contributor model is `book_authors(book_id, author_id, position,
role)` — position 0 = primary, 1..N = co-authors (ADR-0008/0012). Two
position-0 rows are the corruption that double-renders a book on author
detail pages; these tests pin that the endpoint never produces one.
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
    await disco_db.init_db("other")
    from app import state
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "test", "content_type": "ebook", "name": "Test"},
        {"slug": "other", "content_type": "audiobook", "name": "Other"},
    ])
    yield tmp_path
    disco_db.set_active_library(None)


@pytest.fixture
async def client(discovery_db):
    from app.discovery.routers.books import router as books_router
    from app.discovery.routers.authors import router as authors_router

    app = FastAPI()
    app.include_router(books_router)
    app.include_router(authors_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _author(slug: str, name: str) -> int:
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES (?, ?)", (name, name))
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _book(slug: str, title: str, authors: list[tuple[int, str | None]]) -> int:
    """Insert a book with an ordered contributor list of (author_id, role)."""
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        cur = await db.execute(
            "INSERT INTO books (title, owned) VALUES (?, 1)", (title,))
        bid = cur.lastrowid
        for pos, (aid, role) in enumerate(authors):
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position, role) "
                "VALUES (?, ?, ?, ?)",
                (bid, aid, pos, role),
            )
        await db.commit()
        return bid
    finally:
        await db.close()


async def _rows(slug: str, bid: int) -> list[tuple]:
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        rows = await (await db.execute(
            "SELECT author_id, position, role FROM book_authors "
            "WHERE book_id=? ORDER BY position", (bid,))).fetchall()
        return [(r["author_id"], r["position"], r["role"]) for r in rows]
    finally:
        await db.close()


# ─── remove a co-author ──────────────────────────────────────

async def test_remove_coauthor_leaves_primary(client):
    a = await _author("test", "Plum Parrot")
    b = await _author("test", "Sanderson")
    bid = await _book("test", "Andy in the Apocalypse", [(a, None), (b, None)])

    r = await client.delete(
        f"/api/discovery/books/{bid}/contributors/{b}?slug=test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["author_id"] for c in body["contributors"]] == [a]
    assert body["contributors"][0]["position"] == 0
    assert await _rows("test", bid) == [(a, 0, None)]


async def test_remove_primary_promotes_next(client):
    a = await _author("test", "Primary")
    b = await _author("test", "CoAuthor")
    bid = await _book("test", "Book", [(a, None), (b, None)])

    r = await client.delete(
        f"/api/discovery/books/{bid}/contributors/{a}?slug=test")
    assert r.status_code == 200, r.text
    # b is promoted to position 0 (the new primary).
    assert await _rows("test", bid) == [(b, 0, None)]


async def test_renumber_preserves_order_and_roles(client):
    a = await _author("test", "A")
    b = await _author("test", "B")
    c = await _author("test", "C")
    bid = await _book("test", "Triple", [(a, None), (b, None), (c, "translator")])

    r = await client.delete(
        f"/api/discovery/books/{bid}/contributors/{b}?slug=test")
    assert r.status_code == 200, r.text
    # Survivors keep relative order, positions densify, C keeps its role.
    assert await _rows("test", bid) == [(a, 0, None), (c, 1, "translator")]


# ─── last-author guard + replacement ─────────────────────────

async def test_remove_last_author_without_replacement_409(client):
    a = await _author("test", "Solo")
    bid = await _book("test", "OnlyBook", [(a, None)])

    r = await client.delete(
        f"/api/discovery/books/{bid}/contributors/{a}?slug=test")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "last_author"
    # Untouched.
    assert await _rows("test", bid) == [(a, 0, None)]


async def test_remove_last_author_with_replacement_swaps(client):
    a = await _author("test", "Wrong Author")
    d = await _author("test", "Right Author")
    bid = await _book("test", "Book", [(a, None)])

    r = await client.delete(
        f"/api/discovery/books/{bid}/contributors/{a}"
        f"?slug=test&replacement_author_id={d}")
    assert r.status_code == 200, r.text
    assert [c["author_id"] for c in r.json()["contributors"]] == [d]
    assert await _rows("test", bid) == [(d, 0, None)]


async def test_replacement_must_differ(client):
    a = await _author("test", "Solo")
    bid = await _book("test", "Book", [(a, None)])
    r = await client.delete(
        f"/api/discovery/books/{bid}/contributors/{a}"
        f"?slug=test&replacement_author_id={a}")
    assert r.status_code == 400


async def test_replacement_must_exist(client):
    a = await _author("test", "Solo")
    bid = await _book("test", "Book", [(a, None)])
    r = await client.delete(
        f"/api/discovery/books/{bid}/contributors/{a}"
        f"?slug=test&replacement_author_id=99999")
    assert r.status_code == 404


# ─── orphan signal ───────────────────────────────────────────

async def test_removed_author_orphaned_flag(client):
    a = await _author("test", "Keeper")
    b = await _author("test", "Phantom")
    # `b` only appears on this one book → removing it orphans it.
    bid = await _book("test", "Book", [(a, None), (b, None)])
    r = await client.delete(
        f"/api/discovery/books/{bid}/contributors/{b}?slug=test")
    assert r.json()["removed_author_orphaned"] is True


async def test_removed_author_not_orphaned_when_other_books(client):
    a = await _author("test", "Keeper")
    b = await _author("test", "Busy CoAuthor")
    bid1 = await _book("test", "Book One", [(a, None), (b, None)])
    await _book("test", "Book Two", [(b, None)])  # b has another book
    r = await client.delete(
        f"/api/discovery/books/{bid1}/contributors/{b}?slug=test")
    assert r.json()["removed_author_orphaned"] is False


# ─── error paths + isolation ─────────────────────────────────

async def test_not_a_contributor_404(client):
    a = await _author("test", "A")
    other = await _author("test", "Unrelated")
    bid = await _book("test", "Book", [(a, None)])
    r = await client.delete(
        f"/api/discovery/books/{bid}/contributors/{other}?slug=test")
    assert r.status_code == 404


async def test_book_not_found_404(client):
    r = await client.delete(
        "/api/discovery/books/424242/contributors/1?slug=test")
    assert r.status_code == 404


async def test_slug_isolation(client):
    """Same numeric book id in two libraries — removing in `test` must
    not touch `other` (ADR-0002)."""
    a1 = await _author("test", "A1")
    b1 = await _author("test", "B1")
    a2 = await _author("other", "A2")
    b2 = await _author("other", "B2")
    bid_t = await _book("test", "Shared Id", [(a1, None), (b1, None)])
    # Force the same numeric id in `other`.
    from app.discovery.database import get_db
    odb = await get_db("other")
    try:
        await odb.execute(
            "INSERT INTO books (id, title, owned) VALUES (?, 'Other Book', 1)",
            (bid_t,))
        await odb.execute(
            "INSERT INTO book_authors (book_id, author_id, position) VALUES (?,?,0)",
            (bid_t, a2))
        await odb.execute(
            "INSERT INTO book_authors (book_id, author_id, position) VALUES (?,?,1)",
            (bid_t, b2))
        await odb.commit()
    finally:
        await odb.close()

    r = await client.delete(
        f"/api/discovery/books/{bid_t}/contributors/{b1}?slug=test")
    assert r.status_code == 200
    assert await _rows("test", bid_t) == [(a1, 0, None)]
    # `other` library's same-id book is untouched.
    assert await _rows("other", bid_t) == [(a2, 0, None), (b2, 1, None)]


# ─── author typeahead ────────────────────────────────────────

async def test_author_search_scoped_to_slug(client):
    await _author("test", "Brandon Sanderson")
    await _author("test", "Plum Parrot")
    await _author("other", "Sanderson Smith")  # different library

    r = await client.get("/api/discovery/authors/search?q=sander&slug=test")
    assert r.status_code == 200
    names = [a["name"] for a in r.json()["authors"]]
    assert "Brandon Sanderson" in names
    assert "Sanderson Smith" not in names  # other library not searched


async def test_author_search_empty_query(client):
    r = await client.get("/api/discovery/authors/search?q=%20%20&slug=test")
    assert r.status_code == 200
    assert r.json()["authors"] == []
