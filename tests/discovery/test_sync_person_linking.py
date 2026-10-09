"""Wave 5a S2 — new authors from a library sync join the person graph.

From v2.20.0 both syncs inserted a new author inside their open
transaction, then `get_or_create_person` read it on another connection,
couldn't see it, and failed at DEBUG ("not found … cannot link"); the
stub the mirror put in the other content type's library was never linked
at all. Every new author stayed out of the graph until a restart (prod
2026-10-09: 21 Calibre authors + their 21 ABS stubs). Now each is linked
once its insert is committed.
"""
from __future__ import annotations

import pytest

from app import state
from app.database import get_db as get_global_db


@pytest.fixture
async def libraries(temp_db, tmp_path, monkeypatch):
    """An ebook library ("books") and an audiobook library ("audio"),
    the global DB for persons / author_links, no network."""
    from app import config as app_config
    from app.discovery import author_identity, goodreads_author_backfill
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(author_identity, "DATA_DIR", tmp_path)

    async def _no_backfill(**_kw):
        return {}

    monkeypatch.setattr(
        goodreads_author_backfill, "backfill_missing_author_ids", _no_backfill,
    )
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "books", "content_type": "ebook"},
        {"slug": "audio", "content_type": "audiobook"},
    ])
    for slug in ("books", "audio"):
        await disco_db.init_db(slug)
    yield
    disco_db.set_active_library(None)


async def _author_id(slug: str, name: str) -> int:
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        row = await (await db.execute(
            "SELECT id FROM authors WHERE name = ?", (name,),
        )).fetchone()
        return row["id"]
    finally:
        await db.close()


async def _person_of(slug: str, author_id: int):
    gdb = await get_global_db()
    try:
        row = await (await gdb.execute(
            "SELECT person_id FROM author_links WHERE library_slug = ? AND author_id = ?",
            (slug, author_id),
        )).fetchone()
        return row["person_id"] if row else None
    finally:
        await gdb.close()


def _calibre_book(book_id, title, authors):
    return {
        "book_id": book_id, "title": title, "pubdate": "2024-01-01",
        "series_index": 1.0, "book_path": f"A/{title}", "cover_path": None,
        "isbn": None, "authors": authors, "series": [], "tags": None,
        "rating": None, "description": None, "language": None,
        "publisher": None, "formats": None,
    }


async def test_calibre_sync_links_the_new_author_and_its_stub(libraries, monkeypatch):
    from app.discovery import calibre_sync
    from app.discovery.database import set_active_library

    set_active_library("books")
    monkeypatch.setattr(
        calibre_sync, "_read_calibre_db",
        lambda *a, **kw: {"books": [_calibre_book(
            7, "Cyberratum Trilogy Box Set",
            [{"id": 1, "name": "Gentry Race", "sort": "Race, Gentry"}],
        )]},
    )

    await calibre_sync.sync_calibre("dummy_path", "dummy_url")

    ebook_person = await _person_of("books", await _author_id("books", "Gentry Race"))
    stub_person = await _person_of("audio", await _author_id("audio", "Gentry Race"))
    assert ebook_person is not None
    assert stub_person == ebook_person


async def test_abs_sync_links_the_new_author_and_its_stub(libraries, monkeypatch):
    from app.discovery.audiobookshelf_sync import sync_audiobookshelf
    from app.discovery.database import set_active_library
    from app.library_apps import audiobookshelf as abs_mod

    async def fake_get_key():
        return "fake-bearer-token"

    async def fake_iter(self, library_id, page_size=500):
        yield {
            "id": "abs-1",
            "media": {"duration": 3600.0, "numAudioFiles": 1, "metadata": {
                "title": "Some Book", "authorName": "Annabelle Oh",
                "narratorName": "", "seriesName": "", "asin": "",
                "abridged": False,
            }},
        }

    monkeypatch.setattr(abs_mod, "_get_abs_api_key", fake_get_key)
    monkeypatch.setattr(abs_mod.AudiobookshelfClient, "iter_all_items", fake_iter)
    set_active_library("audio")

    await sync_audiobookshelf({
        "slug": "audio", "abs_base_url": "http://abs", "abs_library_id": "lib",
    })

    abs_person = await _person_of("audio", await _author_id("audio", "Annabelle Oh"))
    stub_person = await _person_of("books", await _author_id("books", "Annabelle Oh"))
    assert abs_person is not None
    assert stub_person == abs_person


async def test_a_failed_link_is_logged_loudly_and_the_rest_carry_on(
    libraries, caplog,
):
    from app.discovery.author_identity import link_new_authors
    from app.discovery.database import get_db

    db = await get_db("books")
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES ('Real Author', 'Author, Real')"
        )
        await db.commit()
        real = cur.lastrowid
    finally:
        await db.close()

    linked = await link_new_authors(
        "books", [(99999, "Ghost"), (real, "Real Author")], context="test sync",
    )

    assert linked == 1
    assert await _person_of("books", real) is not None
    assert any(
        r.levelname == "WARNING" and "Ghost" in r.getMessage() for r in caplog.records
    )
