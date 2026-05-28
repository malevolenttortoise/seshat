"""
Multi-library MAM scan iteration tests.

The manual `/scan` endpoint and the `mam_scheduler_loop` both used to
operate on the active library only. Mark added 66 audiobooks overnight
to ABS, ran scheduled scans, and saw zero MAM coverage on them — the
schedule kept hitting the ebook library each tick and never crossed
over to ABS. This file pins the multi-library iteration: snapshots
across all discovered libraries, content_type routed per-library,
format_priority matches each library's content_type.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture
async def two_libraries(tmp_path, monkeypatch):
    """Initialize two discovery DBs (ebook + audiobook), register them
    in `state._discovered_libraries`, and seed each with a couple of
    books that need MAM scanning."""
    from app import config as app_config, state
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)

    libs = [
        {"slug": "ebooks", "name": "Ebooks", "display_name": "Ebooks",
         "content_type": "ebook", "app_type": "calibre"},
        {"slug": "audio", "name": "Audio", "display_name": "Audio",
         "content_type": "audiobook", "app_type": "audiobookshelf"},
    ]
    monkeypatch.setattr(state, "_discovered_libraries", libs)

    for lib in libs:
        disco_db.set_active_library(lib["slug"])
        await disco_db.init_db(lib["slug"])
        db = await disco_db.get_db(slug=lib["slug"])
        try:
            await db.execute(
                "INSERT INTO authors (name, sort_name) VALUES (?, ?)",
                (f"{lib['slug']}-Author", f"{lib['slug']}-Author"),
            )
            aid = (await (await db.execute(
                "SELECT last_insert_rowid()"
            )).fetchone())[0]
            for i in range(3):
                cur = await db.execute(
                    "INSERT INTO books (title, owned, hidden, "
                    "is_unreleased) VALUES (?, 0, 0, 0)",
                    (f"{lib['slug']}-book-{i}",),
                )
                await db.execute(
                    "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                    "VALUES (?, ?, 0)", (cur.lastrowid, aid),
                )
            await db.commit()
        finally:
            await db.close()

    disco_db.set_active_library("ebooks")
    yield libs
    disco_db.set_active_library(None)


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        **__import__("app.config", fromlist=["DEFAULT_SETTINGS"]).DEFAULT_SETTINGS,
        "mam_enabled": True,
        "mam_session_id": "tok",
        "mam_scanning_enabled": True,
        "mam_format_priority": ["epub", "azw3"],
        "audiobook_format_priority": ["m4b", "mp3"],
        "rate_mam": 0,
    }))
    from app import config as app_config
    monkeypatch.setattr(app_config, "SETTINGS_PATH", p)
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = object()
    yield p
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = object()


async def test_scan_endpoint_iterates_all_libraries(
    two_libraries, isolated_settings, monkeypatch,
):
    """POST /scan should snapshot eligible books from EVERY library
    and run a `mam_scan_batch` call against each one with that
    library's content_type. The pre-fix single-library behavior left
    Mark's audiobooks unscanned."""
    from app.discovery.routers import mam as mam_router
    from app import state as app_state

    # Reset progress state so a stale "running" doesn't gate the call.
    app_state._mam_scan_progress = {"running": False}
    app_state._mam_scan_task = None
    app_state._library_sync_in_progress = False

    calls: list[dict] = []

    async def fake_scan_batch(_db, **kwargs):
        calls.append({
            "content_type": kwargs.get("content_type"),
            "format_priority": kwargs.get("format_priority"),
            "book_ids": list(kwargs.get("book_ids") or []),
            "limit": kwargs.get("limit"),
        })
        # Mark progress as if all scanned successfully so the loop
        # advances past this batch.
        on_progress = kwargs.get("on_progress")
        if on_progress:
            on_progress({
                "scanned": len(kwargs["book_ids"] or []),
                "found": 0, "possible": 0,
                "not_found": len(kwargs["book_ids"] or []),
                "errors": 0, "current_book": "",
            })
        return {
            "scanned": len(kwargs["book_ids"] or []),
            "found": 0, "possible": 0,
            "not_found": len(kwargs["book_ids"] or []),
            "errors": 0, "error": None,
        }

    monkeypatch.setattr(mam_router, "mam_scan_batch", fake_scan_batch)

    async def fake_token():
        return "tok"
    monkeypatch.setattr(mam_router, "_get_mam_token", fake_token)
    monkeypatch.setattr(mam_router, "_resolve_mam_languages", lambda _: [1])

    # Skip the inter-batch 60s sleep in tests. Capture the real sleep
    # BEFORE patching so the no-op coroutine doesn't recurse into
    # itself when the same asyncio module is monkeypatched.
    _real_sleep = asyncio.sleep

    async def _noop_sleep(*_a, **_kw):
        await _real_sleep(0)

    monkeypatch.setattr(mam_router.asyncio, "sleep", _noop_sleep)

    # Skip the ntfy "scan complete" call; it would 503 in tests.
    async def noop():
        return None
    monkeypatch.setattr(mam_router, "_notify_mam_done", noop)

    app = FastAPI()
    app.include_router(mam_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post("/api/discovery/mam/scan")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "started"
        assert body["total"] == 6  # 3 books × 2 libraries
        assert sorted(body["libraries"]) == ["audio", "ebooks"]

        # Wait for the background task to finish.
        assert app_state._mam_scan_task is not None
        await asyncio.wait_for(app_state._mam_scan_task, timeout=5.0)

    # Both libraries got a scan call with the right content_type +
    # format_priority routing.
    cts = sorted(c["content_type"] for c in calls)
    assert cts == ["audiobook", "ebook"], calls
    audio = [c for c in calls if c["content_type"] == "audiobook"][0]
    ebook = [c for c in calls if c["content_type"] == "ebook"][0]
    assert audio["format_priority"] == ["m4b", "mp3"]
    assert ebook["format_priority"] == ["epub", "azw3"]
    # Each got 3 books from its own snapshot, no cross-leak.
    assert len(audio["book_ids"]) == 3
    assert len(ebook["book_ids"]) == 3


async def test_scan_endpoint_returns_complete_when_no_books(
    two_libraries, isolated_settings, monkeypatch,
):
    """If every library has zero books needing a scan, the endpoint
    should short-circuit with a `complete` response and not spawn a
    background task. `found` is the only terminal status — `possible`
    and `not_found` are rescannable, so we mark every book `found`."""
    from app.discovery.routers import mam as mam_router
    from app.discovery import database as disco_db
    from app import state as app_state

    app_state._mam_scan_progress = {"running": False}
    app_state._mam_scan_task = None

    # Mark every book as already scanned in both libraries.
    for slug in ("ebooks", "audio"):
        db = await disco_db.get_db(slug=slug)
        try:
            await db.execute("UPDATE books SET mam_status='found'")
            await db.commit()
        finally:
            await db.close()

    async def fake_token():
        return "tok"
    monkeypatch.setattr(mam_router, "_get_mam_token", fake_token)

    app = FastAPI()
    app.include_router(mam_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post("/api/discovery/mam/scan")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "complete"
