"""
v2.3.4 — sidebar edits populate `books.user_edited_fields`.

When the user changes a field via the book sidebar (PUT
/api/discovery/books/{bid}), the field name is added to
`books.user_edited_fields` (a JSON array). Subsequent Calibre / ABS
syncs read this set and skip auto-flow on those fields, routing the
diff to `metadata_review_queue` instead of overwriting.

Subtleties tested here:
  - The sidebar form re-sends every field on every save, so
    "incoming != current" is the only reliable signal of an actual
    user edit. Tracking by-presence-in-payload would falsely flag
    every field on every save.
  - Tracking is set-union, idempotent on repeat saves.
  - source_url has its own editor + canonical-form rules and is
    explicitly excluded from this tracking.
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


async def _seed_book(**fields) -> int:
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (101, 'A', 'A', ?)",
            (normalize_author_name("A"),),
        )
        cols = ["title", "author_id", "source"]
        vals_ = ["Original Title", 101, "goodreads"]
        for k, v in fields.items():
            cols.append(k)
            vals_.append(v)
        ph = ",".join("?" * len(cols))
        cur = await db.execute(
            f"INSERT INTO books ({','.join(cols)}) VALUES ({ph})",
            vals_,
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _user_edited(book_id: int) -> list[str]:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT user_edited_fields FROM books WHERE id = ?",
            (book_id,),
        )).fetchone()
        raw = row["user_edited_fields"] if row else None
        return json.loads(raw) if raw else []
    finally:
        await db.close()


class TestUserEditedFields:
    async def test_adds_field_when_value_actually_changed(self, client):
        bid = await _seed_book(description="Original")
        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={"description": "User-edited description"},
        )
        assert r.status_code == 200
        assert await _user_edited(bid) == ["description"]

    async def test_skips_field_when_value_unchanged(self, client):
        # The sidebar sends every field on every save — incoming ==
        # current means the user didn't touch it, so don't track.
        bid = await _seed_book(description="Same")
        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={"description": "Same"},
        )
        assert r.status_code == 200
        assert await _user_edited(bid) == []

    async def test_tracks_multiple_fields_in_one_save(self, client):
        bid = await _seed_book(
            description="Original", pub_date="2020-01-01", isbn=None,
        )
        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={
                "description": "New description",
                "pub_date": "1965-04-15",
                "isbn": "9780000000000",
            },
        )
        assert r.status_code == 200
        assert sorted(await _user_edited(bid)) == [
            "description", "isbn", "pub_date",
        ]

    async def test_set_union_across_repeat_saves(self, client):
        bid = await _seed_book(description="d1", isbn=None)
        # First save edits description.
        await client.put(
            f"/api/discovery/books/{bid}",
            json={"description": "d2"},
        )
        # Second save edits isbn.
        await client.put(
            f"/api/discovery/books/{bid}",
            json={"isbn": "9780000000000"},
        )
        assert sorted(await _user_edited(bid)) == ["description", "isbn"]

    async def test_idempotent_on_repeat_save_of_same_field(self, client):
        bid = await _seed_book(description="d1")
        # Edit description.
        await client.put(
            f"/api/discovery/books/{bid}",
            json={"description": "d2"},
        )
        # Save again with the now-current value — nothing should change.
        await client.put(
            f"/api/discovery/books/{bid}",
            json={"description": "d2"},
        )
        assert await _user_edited(bid) == ["description"]

    async def test_source_url_not_tracked(self, client):
        # source_url has its own dedicated editor with canonical-form
        # rules; we don't track it via the v2.3.4 user_edited_fields
        # mechanism because the auto-flow path doesn't read it
        # either. Sidebar saves of source_url should NOT flag it.
        bid = await _seed_book()
        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={"source_url": json.dumps({"goodreads": "https://x/y"})},
        )
        assert r.status_code == 200
        assert await _user_edited(bid) == []

    async def test_404_on_unknown_book(self, client):
        r = await client.put(
            "/api/discovery/books/99999",
            json={"description": "x"},
        )
        assert r.status_code == 404
