"""Series author mode (ADR-0010) follows every contributor / series /
visibility change, not just the per-book routes that already recomputed
it (2026-10 audit issue 10; Mark, G45).

Each test leaves a series' stored mode wrong for its membership and
checks the path puts it right. `book_authors` is seeded for every book
(CLAUDE.md: ADR-0008 reads go through it).
"""
from __future__ import annotations

import logging

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
async def client(discovery_db):
    from app.discovery.routers.books import router as books_router
    from app.discovery.routers.metadata import router as metadata_router
    from app.discovery.routers.suggestions import router as suggestions_router

    app = FastAPI()
    app.include_router(books_router)
    app.include_router(metadata_router)
    app.include_router(suggestions_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _exec(sql: str, params=()) -> int:
    from app.discovery.database import get_db
    db = await get_db("test")
    try:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _author(name: str) -> int:
    return await _exec(
        "INSERT INTO authors (name, sort_name) VALUES (?, ?)", (name, name))


async def _series(name: str, owner: int | None, mode: str | None) -> int:
    return await _exec(
        "INSERT INTO series (name, author_id, author_mode) VALUES (?, ?, ?)",
        (name, owner, mode))


async def _book(title: str, authors: list[int], series_id: int | None) -> int:
    from app.discovery.database import get_db
    db = await get_db("test")
    try:
        cur = await db.execute(
            "INSERT INTO books (title, series_id, owned, source) VALUES (?, ?, 0, 'hardcover')",
            (title, series_id))
        bid = cur.lastrowid
        for pos, aid in enumerate(authors):
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) VALUES (?, ?, ?)",
                (bid, aid, pos))
        await db.commit()
        return bid
    finally:
        await db.close()


async def _mode(sid: int) -> tuple:
    from app.discovery.database import get_db
    db = await get_db("test")
    try:
        row = await (await db.execute(
            "SELECT author_mode, author_id FROM series WHERE id = ?", (sid,))).fetchone()
        return (row["author_mode"], row["author_id"])
    finally:
        await db.close()


# ─── Per-book routes ─────────────────────────────────────────


async def test_removing_a_contributor_recomputes_the_series(client):
    a, b = await _author("Alice"), await _author("Bob")
    sid = await _series("Duo", a, "multi_author")
    b1 = await _book("One", [a, b], sid)
    await _book("Two", [a], sid)        # intersection {A} → per_author, stored stale
    await _exec("UPDATE series SET author_mode='multi_author' WHERE id=?", (sid,))

    r = await client.delete(f"/api/discovery/books/{b1}/contributors/{b}?slug=test")
    assert r.status_code == 200, r.text
    assert await _mode(sid) == ("per_author", a)


async def test_merging_books_recomputes_the_series(client):
    a, b = await _author("Alice"), await _author("Bob")
    sid = await _series("Pair", None, "shared")
    b1 = await _book("Same Book", [a], sid)
    b2 = await _book("Same Book", [b], sid)

    r = await client.post(f"/api/discovery/books/{b1}/merge?slug=test", json={"other_id": b2})
    assert r.status_code == 200, r.text
    # One book left, contributors unioned: owners {A, B} → multi_author.
    assert (await _mode(sid))[0] == "multi_author"


async def test_moving_a_book_out_of_a_series_recomputes_it(client):
    a, b = await _author("Alice"), await _author("Bob")
    sid = await _series("Mixed", None, "shared")
    await _book("Mine", [a], sid)
    b2 = await _book("Theirs", [b], sid)

    r = await client.put(f"/api/discovery/books/{b2}?slug=test", json={"series_name": ""})
    assert r.status_code == 200, r.text
    assert await _mode(sid) == ("per_author", a)


async def test_applying_a_series_suggestion_recomputes_the_series(client):
    a, b = await _author("Alice"), await _author("Bob")
    sid = await _series("Mixed", None, "shared")
    await _book("Mine", [a], sid)
    b2 = await _book("Theirs", [b], sid)
    sug = await _exec(
        "INSERT INTO book_series_suggestions (book_id, suggested_series_name, "
        "suggested_series_index, sources_agreeing, status) "
        "VALUES (?, NULL, NULL, '[]', 'pending')", (b2,))

    r = await client.post(f"/api/discovery/series-suggestions/{sug}/apply")
    assert r.status_code == 200, r.text
    assert await _mode(sid) == ("per_author", a)


async def test_pulling_a_library_series_recomputes_both_series(client):
    a, b = await _author("Alice"), await _author("Bob")
    sid = await _series("Mixed", None, "shared")
    await _book("Mine", [a], sid)
    b2 = await _book("Theirs", [b], sid)
    await _exec(
        "INSERT INTO books_calibre_snapshot (book_id, series_name, synced_at) "
        "VALUES (?, ?, 0)", (b2, ""))

    r = await client.post(
        f"/api/discovery/books/{b2}/pull?slug=test",
        json={"source": "calibre", "fields": ["series_name"]})
    assert r.status_code == 200, r.text
    assert await _mode(sid) == ("per_author", a)


# ─── End-of-run recompute ────────────────────────────────────


async def test_recompute_all_fixes_stale_rows_and_leaves_the_rest(discovery_db):
    from app.discovery.database import get_db, recompute_all_series_author_mode

    a, b = await _author("Alice"), await _author("Bob")
    stale = await _series("Stale", a, "multi_author")
    await _book("S1", [a], stale)
    right = await _series("Right", a, "per_author")
    await _book("R1", [a], right)
    await _book("B1", [b], await _series("Other", b, "per_author"))

    db = await get_db("test")
    try:
        await recompute_all_series_author_mode(db, context="test")
        first = db.total_changes
        await recompute_all_series_author_mode(db, context="test")
        assert db.total_changes == first        # nothing left to write
    finally:
        await db.close()
    assert await _mode(stale) == ("per_author", a)


async def test_a_unique_clash_warns_once_not_every_run(discovery_db, caplog):
    from app.discovery.database import get_db, recompute_all_series_author_mode

    a, b = await _author("Alice"), await _author("Bob")
    await _series("Saga", b, "per_author")             # holds (Saga, Bob)
    sid = await _series("Saga", a, "multi_author")     # only Bob's books
    await _book("Saga 1", [b], sid)

    db = await get_db("test")
    try:
        with caplog.at_level(logging.WARNING):
            await recompute_all_series_author_mode(db, context="test")
            await recompute_all_series_author_mode(db, context="test")
    finally:
        await db.close()
    assert sum("blocked by UNIQUE" in r.message for r in caplog.records) == 1
    assert await _mode(sid) == ("per_author", a)        # mode recorded, owner kept


async def test_a_source_scan_ends_with_a_recompute(discovery_db, monkeypatch):
    from app.discovery import lookup

    a = await _author("Alice")
    sid = await _series("Stale", a, "multi_author")
    await _book("S1", [a], sid)
    monkeypatch.setattr(lookup, "_sources_for_content_type", lambda *a, **k: [])

    await lookup.lookup_author(a, "Alice")
    assert await _mode(sid) == ("per_author", a)


async def test_hygiene_ends_with_a_recompute(discovery_db, monkeypatch):
    from app.discovery import hygiene

    a = await _author("Alice")
    sid = await _series("Stale", a, "multi_author")
    await _book("S1", [a], sid)

    async def _noop(*args, **kwargs):
        return None

    for name in dir(hygiene):
        if name.startswith("job_"):
            monkeypatch.setattr(hygiene, name, _noop)
    monkeypatch.setattr(hygiene, "purge_expired_soft_deletes", lambda **k: {}, raising=False)

    await hygiene.run_all()
    assert await _mode(sid) == ("per_author", a)


# ─── Library sync ────────────────────────────────────────────

from tests.discovery.test_calibre_sync_incremental import (  # noqa: E402
    _build_calibre_db,
    _iso,
    discovery_db as calibre_db,  # noqa: F401  (fixture)
)


async def test_a_calibre_sync_ends_with_every_series_mode_set(calibre_db):
    """Sync-created series used to keep author_mode NULL (shown as
    per-author) until the next restart's backfill."""
    import time as _time

    from app.discovery import calibre_sync
    from app.discovery.database import get_db

    old = _iso(_time.time() - 86400)
    db_path = calibre_db / "metadata.db"
    _build_calibre_db(db_path, [
        {"id": 1, "title": "Alpha One", "last_modified": old,
         "authors": [(10, "Alice")], "series": [(5, "Alpha")]},
        {"id": 2, "title": "Alpha Two", "last_modified": old,
         "authors": [(10, "Alice")], "series": [(5, "Alpha")]},
        {"id": 3, "title": "Anthology One", "last_modified": old,
         "authors": [(10, "Alice")], "series": [(6, "Anthology")]},
        {"id": 4, "title": "Anthology Two", "last_modified": old,
         "authors": [(11, "Bob")], "series": [(6, "Anthology")]},
    ])
    await calibre_sync.sync_calibre(
        calibre_db_path=str(db_path), calibre_library_path=str(calibre_db),
    )

    db = await get_db("test")
    try:
        rows = await (await db.execute(
            "SELECT name, author_mode FROM series ORDER BY name")).fetchall()
    finally:
        await db.close()
    modes = {r["name"]: r["author_mode"] for r in rows}
    assert modes == {"Alpha": "per_author", "Anthology": "shared"}


def _routers_app():
    from app.discovery.routers.authors import router as authors_router
    from app.discovery.routers.import_export import router as import_router

    app = FastAPI()
    app.include_router(authors_router)
    app.include_router(import_router)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_resetting_source_data_ends_with_a_recompute(discovery_db):
    a, b = await _author("Alice"), await _author("Bob")
    sid = await _series("Mixed", None, "shared")
    owned = await _book("Mine", [a], sid)
    await _exec("UPDATE books SET owned = 1 WHERE id = ?", (owned,))
    await _book("Theirs", [b], sid)          # discovered; the reset deletes it

    async with _routers_app() as c:
        r = await c.post("/api/discovery/sources/reset")
    assert r.status_code == 200, r.text
    assert await _mode(sid) == ("per_author", a)


async def test_an_import_ends_with_a_recompute(discovery_db):
    a, b = await _author("Alice"), await _author("Bob")
    # Alice's series, co-written with Bob so far: owners {A, B}.
    sid = await _series("Duo", a, "multi_author")
    await _book("Duo One", [a, b], sid)

    async with _routers_app() as c:
        r = await c.post("/api/discovery/books/import-add", json={"books": [
            {"title": "Duo Solo", "author_name": "Alice", "series_name": "Duo"},
        ]})
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 1
    # The imported book is Alice's alone: owners {A}.
    assert await _mode(sid) == ("per_author", a)
