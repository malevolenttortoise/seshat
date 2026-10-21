"""Wave B S7's list filters: "hide short" on the Missing / Library lists
(F11 / C8: a list filter, not a hide) and the Hidden page's reason chips
(F19: one chip may cover several reasons)."""
from __future__ import annotations

import pytest

from app import config as app_config


@pytest.fixture
async def lib(tmp_path, monkeypatch):
    from app.discovery import database as disco_db
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    await disco_db.init_db("test")
    prev = disco_db.get_active_library()
    disco_db.set_active_library("test")
    db = await disco_db.get_db()
    try:
        await db.execute("INSERT INTO authors (id, name, sort_name) VALUES (1, 'Ann Author', 'Author, Ann')")
        for title, pages, hidden, reason in (
            ("Long Novel", 320, 0, None), ("Short Story", 40, 0, None), ("No Count", None, 0, None),
            ("Zero Count", 0, 0, None), ("Ninety Nine", 99, 0, None), ("One Hundred", 100, 0, None),
            ("A Comic", 30, 1, "comic"), ("A Split", 80, 1, "split"), ("A Sampler", 20, 1, "sampler"),
            ("Mine", 300, 1, None),
        ):
            cur = await db.execute(
                "INSERT INTO books (title, owned, page_count, hidden, hidden_reason, source) "
                "VALUES (?, 0, ?, ?, ?, 'hardcover')", (title, pages, hidden, reason))
            await db.execute("INSERT INTO book_authors (book_id, author_id, position) VALUES (?, 1, 0)",
                             (cur.lastrowid,))
        await db.commit()
    finally:
        await db.close()
    yield
    disco_db.set_active_library(prev)


def _titles(resp):
    return sorted(b["title"] for b in resp["books"])


async def _missing(**kw):
    from app.discovery.routers.books import get_missing
    args = dict(search=None, author_id=None, series_id=None, book_type=None, mam_status=None,
                sort="title", sort_dir="asc", page=1, per_page=60, include_hidden=False,
                content_type=None, hide_short=False)
    args.update(kw)
    return await get_missing(**args)


async def test_hide_short_leaves_out_books_under_100_pages(lib):
    assert _titles(await _missing()) == sorted(
        ["Long Novel", "Short Story", "No Count", "Zero Count", "Ninety Nine", "One Hundred"])
    # A book with no page count can't be badged, so it stays.
    assert _titles(await _missing(hide_short=True)) == sorted(
        ["Long Novel", "No Count", "Zero Count", "One Hundred"])


async def test_a_chip_can_cover_several_reasons(lib):
    from app.discovery.routers.books import get_hidden
    args = dict(search=None, sort="title", sort_dir="asc", page=1, per_page=60,
                content_type=None, owned=None, filtered=True)
    assert _titles(await get_hidden(**args, reason="split,sampler,listing")) == ["A Sampler", "A Split"]
    assert _titles(await get_hidden(**args, reason="comic")) == ["A Comic"]
    assert _titles(await get_hidden(**args, reason=None)) == ["A Comic", "A Sampler", "A Split"]
