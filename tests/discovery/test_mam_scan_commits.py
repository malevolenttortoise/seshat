"""Bulk MAM scans commit per book (audit issue 27, L4-05).

The three user-started bulk scans (selected books, several authors, one
author) updated each book's row but committed only after the loop. Every
book's MAM searches wait their turn in the pacer, so the write
transaction stayed open across all those waits, and any other write to
the same library (a sidebar edit, Hide, Approve) waited behind the whole
scan, up to the 30s busy timeout. `scan_books_batch` already committed
per book for exactly this reason.

Proven by a second connection writing to the library during the scan's
second search, with a short busy timeout: it must not find it locked.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app import state
from app.discovery.database import get_db as get_discovery_db
from app.discovery.sources import mam as mam_source
from tests.discovery.test_mam_scan_auth_stop import library  # noqa: F401 (fixture)
from tests.discovery.test_mam_scan_lock import _post, mam_scans  # noqa: F401 (fixture)

_NOT_FOUND = {
    "status": "not_found", "mam_url": None, "mam_formats": None,
    "mam_torrent_id": None, "mam_has_multiple": False,
}


@pytest.fixture
def writer_during_scan(monkeypatch, mam_scans, library):
    """On each search after the first, another connection tries a quick
    write to the library; records "ok" or the lock error."""
    from app.discovery.routers import mam as mam_router

    seen: list[str] = []

    async def check_book(token, title, *_a, **_kw):
        if seen or title != "Book 0":
            db = await get_discovery_db("ebooks")
            try:
                await db.execute("PRAGMA busy_timeout = 200")
                await db.execute(
                    "UPDATE books SET description = 'edited mid-scan' WHERE id = ?",
                    (library["book_ids"][2],),
                )
                await db.commit()
                seen.append("ok")
            except sqlite3.OperationalError as e:
                seen.append(str(e))
            finally:
                await db.close()
        else:
            seen.append("first")
        return dict(_NOT_FOUND)

    monkeypatch.setattr(mam_source, "check_book", check_book)
    monkeypatch.setattr(mam_router, "mam_check_book", check_book)  # bound at import
    return seen


@pytest.mark.parametrize("path,body", [
    ("/api/discovery/books/scan-mam?slug=ebooks", "books"),
    ("/api/discovery/mam/scan-author/{author}?slug=ebooks", None),
    ("/api/discovery/authors/scan-mam",
     {"author_names": ["Some Author"], "content_type": "ebook"}),
])
async def test_a_write_during_the_scan_is_not_locked_out(
    temp_db, library, writer_during_scan, path, body,
):
    path = path.replace("{author}", str(library["author_id"]))
    if body == "books":
        body = {"book_ids": library["book_ids"]}

    r = await _post(path, body)
    assert r.json().get("status") == "started", r.json()
    await asyncio.wait_for(state._mam_scan_task, timeout=10)

    assert writer_during_scan[0] == "first"
    assert writer_during_scan[1:] and all(s == "ok" for s in writer_during_scan[1:]), (
        writer_during_scan
    )
