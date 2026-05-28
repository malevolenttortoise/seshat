"""
HTTP-level tests for the `POST /api/discovery/sync/lookup` endpoint.

Focuses on the `content_type` query param that fans the scan across
every discovered library of a given content type — the "Scan Audiobooks"
Dashboard button sets it so audiobook libraries get visited even when
the active library is an ebook one.
"""
from __future__ import annotations

import asyncio
import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("cal")
    # Both library dbs get schema-initialized so the due-count
    # pre-flight can hit them.
    await disco_db.init_db("cal")
    await disco_db.init_db("abs")
    yield tmp_path
    disco_db.set_active_library(None)


@pytest.fixture(autouse=True)
async def _clear_task_state():
    from app import state
    state._lookup_task = None
    state._lookup_progress = {}
    yield
    state._lookup_task = None
    state._lookup_progress = {}


@pytest.fixture
async def client(discovery_db, monkeypatch):
    from app import state
    from app.discovery.routers.scan import router

    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "cal", "content_type": "ebook", "name": "Calibre"},
        {"slug": "abs", "content_type": "audiobook", "name": "ABS"},
    ])
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _seed_due_author(slug: str, name: str = "Author"):
    """Insert an author + one book into `slug`'s db so it counts as 'due'."""
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, last_lookup_at) VALUES (?, ?, 0)",
            (name, name),
        )
        aid = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO books (title) VALUES (?)", ("T",),
        )
        bid = cur.lastrowid
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (?, ?, 0)", (bid, aid),
        )
        await db.commit()
    finally:
        await db.close()


async def test_content_type_no_matching_libraries_returns_friendly_message(client, monkeypatch):
    # Override discovered libraries to omit audiobook libs entirely.
    from app import state
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "cal", "content_type": "ebook", "name": "Calibre"},
    ])
    r = await client.post("/api/discovery/sync/lookup?content_type=audiobook")
    assert r.status_code == 200
    body = r.json()
    assert body["due"] == 0
    assert "audiobook" in body["message"].lower()


async def test_content_type_audiobook_scans_only_matching_libraries(client, monkeypatch):
    # Seed due authors in BOTH libraries; the audiobook-typed scan should
    # only run lookups against `abs`.
    await _seed_due_author("cal", "Ebook Author")
    await _seed_due_author("abs", "Audiobook Author")

    visited_slugs: list[str] = []

    async def fake_run_full_lookup(on_progress=None):
        from app.discovery.database import get_active_library
        visited_slugs.append(get_active_library())
        if on_progress:
            on_progress({"checked": 1, "total": 1, "current_author": "x", "new_books": 0})
        return {"authors_checked": 1, "new_books": 0, "source_timeouts": {}}

    monkeypatch.setattr(
        "app.discovery.routers.scan.run_full_lookup", fake_run_full_lookup,
    )

    r = await client.post("/api/discovery/sync/lookup?content_type=audiobook")
    assert r.status_code == 200
    assert r.json()["status"] == "started"

    # Wait for the scheduled task to finish.
    from app import state
    assert state._lookup_task is not None
    await asyncio.wait_for(state._lookup_task, timeout=5)

    assert visited_slugs == ["abs"]
    # Active library must be restored after the scan.
    from app.discovery.database import get_active_library
    assert get_active_library() == "cal"


async def test_no_content_type_scans_active_library_only(client, monkeypatch):
    await _seed_due_author("cal", "Ebook Author")
    await _seed_due_author("abs", "Audiobook Author")

    visited_slugs: list[str] = []

    async def fake_run_full_lookup(on_progress=None):
        from app.discovery.database import get_active_library
        visited_slugs.append(get_active_library())
        return {"authors_checked": 1, "new_books": 0, "source_timeouts": {}}

    monkeypatch.setattr(
        "app.discovery.routers.scan.run_full_lookup", fake_run_full_lookup,
    )

    r = await client.post("/api/discovery/sync/lookup")
    assert r.status_code == 200

    from app import state
    assert state._lookup_task is not None
    await asyncio.wait_for(state._lookup_task, timeout=5)

    assert visited_slugs == ["cal"]
