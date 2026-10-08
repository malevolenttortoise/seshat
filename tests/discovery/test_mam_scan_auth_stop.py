"""Bulk MAM scans stop on the first auth error (audit issue 04, L1-09).

`check_book` returns `auth_error` on MAM's 401/403 before its pacing
sleep, so a loop that carries on fires every remaining search back to
back with a dead cookie. The three user-started bulk scans (books
multi-select, multi-author, single author) now stop and show the error
in the scan widget, as the batch scanners already did.

Proven by asserting the remaining books are never searched.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from app import state
from app.discovery.sources import mam as mam_source

_AUTH_ERROR = {
    "status": "auth_error", "error": "HTTP 403", "mam_url": None,
    "mam_formats": None, "mam_torrent_id": None, "mam_has_multiple": False,
}


@pytest.fixture
async def library(tmp_path, monkeypatch):
    """One ebook discovery library: one author, three books MAM hasn't scanned."""
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    lib = {"slug": "ebooks", "name": "Ebooks", "display_name": "Ebooks",
           "content_type": "ebook", "app_type": "calibre"}
    monkeypatch.setattr(state, "_discovered_libraries", [lib])
    disco_db.set_active_library("ebooks")
    await disco_db.init_db("ebooks")
    db = await disco_db.get_db(slug="ebooks")
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES ('Some Author', 'Author, Some')"
        )
        aid = cur.lastrowid
        book_ids = []
        for i in range(3):
            cur = await db.execute(
                "INSERT INTO books (title, owned, hidden, is_unreleased) "
                "VALUES (?, 0, 0, 0)", (f"Book {i}",),
            )
            book_ids.append(cur.lastrowid)
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) VALUES (?, ?, 0)",
                (cur.lastrowid, aid),
            )
        await db.commit()
    finally:
        await db.close()
    yield {"author_id": aid, "book_ids": book_ids}
    disco_db.set_active_library(None)


@pytest.fixture
def mam_refuses(tmp_path, monkeypatch):
    """MAM scanning on, a cookie set, and every search answered 403."""
    from app import config as app_config
    from app.discovery import cover_phash
    from app.discovery.routers import mam as mam_router

    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        **app_config.DEFAULT_SETTINGS,
        "mam_enabled": True, "mam_scanning_enabled": True, "rate_mam": 0,
    }))
    monkeypatch.setattr(app_config, "SETTINGS_PATH", p)
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = object()

    async def token():
        return "tok"

    async def no_phash(*_a, **_kw):
        return None

    async def noop():
        return None

    monkeypatch.setattr(mam_router, "_get_mam_token", token)
    monkeypatch.setattr(mam_router, "_notify_mam_done", noop)
    monkeypatch.setattr(mam_source, "_resolve_mam_languages", lambda _: [1])
    monkeypatch.setattr(cover_phash, "ensure_cover_phash", no_phash)

    searched: list[str] = []

    async def check_book(token, title, *_a, **_kw):
        searched.append(title)
        return dict(_AUTH_ERROR)

    monkeypatch.setattr(mam_source, "check_book", check_book)
    monkeypatch.setattr(mam_router, "mam_check_book", check_book)  # bound at import
    state._mam_scan_progress = {"running": False}
    state._mam_scan_task = None
    yield searched
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = object()
    state._mam_scan_progress = {"running": False}
    state._mam_scan_task = None


async def _run(router, path: str, body: dict | None = None) -> dict:
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post(path, json=body) if body is not None else await c.post(path)
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "started", r.json()
    await asyncio.wait_for(state._mam_scan_task, timeout=5.0)
    return dict(state._mam_scan_progress)


async def test_books_scan_stops_after_the_first_auth_error(temp_db, library, mam_refuses):
    from app.discovery.routers import books

    progress = await _run(
        books.router, "/api/discovery/books/scan-mam?slug=ebooks",
        {"book_ids": library["book_ids"]},
    )

    assert len(mam_refuses) == 1
    assert progress["running"] is False
    assert progress["status"] == "error: HTTP 403"


async def test_multi_author_scan_stops_after_the_first_auth_error(
    temp_db, library, mam_refuses,
):
    from app.discovery.routers import authors

    progress = await _run(
        authors.router, "/api/discovery/authors/scan-mam",
        {"author_names": ["Some Author"], "content_type": "ebook"},
    )

    assert len(mam_refuses) == 1
    assert progress["status"] == "error: HTTP 403"


async def test_single_author_scan_stops_after_the_first_auth_error(
    temp_db, library, mam_refuses,
):
    from app.discovery.routers import mam as mam_router

    progress = await _run(
        mam_router.router, f"/api/discovery/mam/scan-author/{library['author_id']}",
    )

    assert len(mam_refuses) == 1
    assert progress["status"] == "error: HTTP 403"
