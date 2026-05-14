"""HTTP-level coverage for `POST /api/discovery/books/scan-sources`.

This endpoint had a stale `from app.routers.authors import
_spawn_lookup_task` import that 500'd every call in production for an
unknown stretch of time — the helper lives in `app.discovery.routers.authors`,
not the top-level CRUD module. The bug was caught only because UAT
exercised the MAM Search page's Upload Candidates multi-select on
2026-05-14 (v2.11.2 hotfix).

These tests don't try to run an actual source scan — that would
require the full discovery stack. Instead they:

  1. Confirm the route loads cleanly (import errors surface as 500).
  2. Confirm the happy path returns `{"status": "started", "total": N}`
     when there ARE matching authors for the supplied book_ids.
  3. Confirm the empty-input path returns the documented error
     envelope without raising.
  4. Confirm `author_names` requires `content_type` (400 otherwise).

`_spawn_lookup_task` is monkey-patched to a no-op so the test doesn't
spin up a real asyncio task / touch the source registry.
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
    from app import state
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "test", "content_type": "ebook", "name": "Test"},
    ])
    yield tmp_path
    disco_db.set_active_library(None)


@pytest.fixture
async def client(discovery_db, monkeypatch):
    # No-op the background-task spawner so tests stay hermetic — we
    # only care that the route loads, parses input, and returns the
    # documented response shape. The runner itself is exercised by
    # the broader discovery integration tests.
    from app.discovery.routers import authors as disco_authors

    def _noop_spawn(scan_type, total, runner):
        # Don't actually create_task — just record the inputs so the
        # endpoint can return its "started" envelope.
        return None
    monkeypatch.setattr(disco_authors, "_spawn_lookup_task", _noop_spawn)

    from app.discovery.routers.books import router
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _seed_author_with_book(name: str, title: str) -> tuple[int, int]:
    """Insert (author, book). Returns (author_id, book_id)."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES (?, ?)", (name, name),
        )
        aid = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO books (author_id, title) VALUES (?, ?)",
            (aid, title),
        )
        bid = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    return aid, bid


async def test_happy_path_returns_started_envelope(client):
    """The v2.11.2 bug: stale import → ImportError → 500. This test
    catches that class of bug by exercising the full route — if the
    `_spawn_lookup_task` import in books.py is wrong, the test fails
    with a 500 instead of the expected 200.
    """
    _aid, bid = await _seed_author_with_book("Alice Author", "Book One")
    r = await client.post("/api/discovery/books/scan-sources", json={
        "book_ids": [bid],
    })
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    body = r.json()
    # Active-library path returns {status: started, total: 1} when
    # the book→author resolver finds at least one row.
    assert body.get("status") == "started", body
    assert body.get("total") == 1, body


async def test_empty_input_returns_error_envelope(client):
    """No book_ids AND no author_names → polite error envelope, not 500."""
    r = await client.post("/api/discovery/books/scan-sources", json={})
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("error"), body


async def test_unknown_book_ids_404(client):
    """book_ids that don't match anything in the DB → HTTP 404 with the
    documented 'No matching authors found' message.
    """
    r = await client.post("/api/discovery/books/scan-sources", json={
        "book_ids": [9999],
    })
    assert r.status_code == 404, f"got {r.status_code}: {r.text}"


async def test_author_names_requires_content_type(client):
    """The cross-library names-mode path is gated on content_type
    being present (otherwise we'd try to scan synthetic id=None rows
    in the active library and crash). The endpoint should 400 cleanly.
    """
    r = await client.post("/api/discovery/books/scan-sources", json={
        "author_names": ["Alice Author"],
    })
    assert r.status_code == 400, f"got {r.status_code}: {r.text}"
    assert "content_type" in r.text.lower()
