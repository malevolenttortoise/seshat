"""
HTTP-level tests for `/api/discovery/authors/{id}` detail response.

Covers the empty-series filter: a series with every author-linked book
hidden shouldn't appear in the response, otherwise the frontend renders
a "(0/0)" tile with no books.
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
    # v2.20.0 — `/authors/{aid}` now reads `author_links` to surface
    # `person_id`; the global DB must exist or the response 500s with
    # `no such table: author_links`.
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "seshat.db")
    await database.init_db()
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    # Register the library so `_author_detail_for_slug` resolves
    # the content_type via `state._discovered_libraries`.
    from app import state
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "test", "content_type": "ebook", "name": "Test"},
    ])
    yield tmp_path
    disco_db.set_active_library(None)


async def _seed(author_name: str, series_name: str, titles_hidden: list[tuple[str, int]]) -> int:
    """Insert an author + series + books. Returns the author id.

    `titles_hidden` is a list of `(title, hidden_flag)` tuples.
    """
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES (?, ?)",
            (author_name, author_name),
        )
        aid = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO series (name, author_id) VALUES (?, ?)",
            (series_name, aid),
        )
        sid = cur.lastrowid
        for title, hidden in titles_hidden:
            cur = await db.execute(
                "INSERT INTO books (title, series_id, hidden, owned) "
                "VALUES (?, ?, ?, ?)",
                (title, sid, hidden, 0),
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, 0)",
                (cur.lastrowid, aid),
            )
        await db.commit()
        return aid
    finally:
        await db.close()


@pytest.fixture
async def client(discovery_db):
    from app.discovery.routers.authors import router

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_series_with_all_books_hidden_is_omitted(client):
    aid = await _seed("Alice Author", "Mercy Temple",
                      [("Book 1", 1), ("Book 2", 1)])
    r = await client.get(f"/api/discovery/authors/{aid}")
    assert r.status_code == 200
    assert r.json()["series"] == []


async def test_series_with_one_visible_book_still_appears(client):
    aid = await _seed("Alice Author", "Mercy Temple",
                      [("Book 1", 0), ("Book 2", 1)])
    r = await client.get(f"/api/discovery/authors/{aid}")
    assert r.status_code == 200
    series = r.json()["series"]
    assert len(series) == 1
    assert series[0]["author_book_count"] == 1


async def test_coauthored_book_surfaces_on_coauthor_detail_and_counts(client):
    """v3.0.0 Phase 4 (ADR-0008) — a book OWNED under primary author A
    with co-author B (book_authors=[A,B]) surfaces on B's author-detail
    page AND counts toward B's owned total, even though
    `books.author_id` is A. This is the core value of the multi-author
    rework: co-authored books are owned for every contributor."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        a = await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES ('J.N. Chaney', 'Chaney')")
        chaney = a.lastrowid
        b = await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES ('Jason Anspach', 'Anspach')")
        anspach = b.lastrowid
        bk = await db.execute(
            "INSERT INTO books (title, owned, hidden) "
            "VALUES ('Able Bodied Soldier', 1, 0)")
        bid = bk.lastrowid
        for pos, aid in enumerate((chaney, anspach)):
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, ?)", (bid, aid, pos))
        await db.commit()
    finally:
        await db.close()

    # B's detail page lists the co-authored book.
    r = await client.get(f"/api/discovery/authors/{anspach}")
    assert r.status_code == 200
    titles = [bk["title"] for bk in r.json()["standalone_books"]]
    assert "Able Bodied Soldier" in titles, (
        f"co-authored book should surface on co-author B's detail — got {titles!r}"
    )

    # ...and counts toward B's owned total on the authors list.
    rl = await client.get("/api/discovery/authors?content_type=all&search=Anspach")
    rows = [x for x in rl.json()["authors"] if x["name"] == "Jason Anspach"]
    assert len(rows) == 1
    assert rows[0]["owned_count"] == 1, (
        f"co-authored owned book should count for B — got {rows[0]}"
    )


async def test_authors_list_counts_are_global_on_every_tab(
    tmp_path, monkeypatch,
):
    """v2.17.1 — author tile counts on the Authors page should be
    GLOBAL (summed across libraries) regardless of which format tab
    the user clicked. The tab filters which authors appear; the
    counts always show the full picture.

    Repro from UAT 2026-05-18: Emrys Ambrosius has 5 ebooks (Calibre)
    + 1 audiobook (ABS). v2.17.0 showed "1 owned / 0 missing" on the
    Audiobooks tab (per-library) and the correct "1 owned / 5
    missing" on the All tab. After fix, every tab shows the global
    "1 owned / 5 missing"; the Audiobooks tab simply also includes
    Emrys in the list (because she has audiobooks).
    """
    from app import config as app_config
    from app import database, state
    from app.discovery import database as disco_db
    from app.discovery.database import get_db
    from app.discovery.routers.authors import router

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    # v2.20.0 — /authors list dedup uses author_links from the global DB.
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "seshat.db")
    await database.init_db()
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "calibre", "content_type": "ebook", "name": "Calibre"},
        {"slug": "abs", "content_type": "audiobook", "name": "ABS"},
    ])

    for slug in ("calibre", "abs"):
        disco_db.set_active_library(slug)
        await disco_db.init_db(slug)

    # Calibre side — 5 unowned ebooks.
    disco_db.set_active_library("calibre")
    db = await get_db("calibre")
    try:
        await db.execute("INSERT INTO authors (name, sort_name) VALUES ('Emrys Ambrosius', 'Ambrosius')")
        for i in range(5):
            cur = await db.execute(
                "INSERT INTO books (title, owned, hidden) VALUES (?, 0, 0)",
                (f"Ebook {i+1}",),
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, 1, 0)",
                (cur.lastrowid,),
            )
        await db.commit()
    finally:
        await db.close()
    # ABS side — 1 owned audiobook.
    disco_db.set_active_library("abs")
    db = await get_db("abs")
    try:
        await db.execute("INSERT INTO authors (name, sort_name) VALUES ('Emrys Ambrosius', 'Ambrosius')")
        cur = await db.execute(
            "INSERT INTO books (title, owned, hidden) VALUES ('Audio 1', 1, 0)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, 1, 0)",
            (cur.lastrowid,),
        )
        await db.commit()
    finally:
        await db.close()

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        # All tab — author present with global counts.
        r_all = (await c.get(
            "/api/discovery/authors?content_type=all&search=Emrys"
        )).json()["authors"]
        # Audiobooks tab — author present (because they have audio),
        # but counts must be the same global 1/5/6 (not 1/0/1).
        r_audio = (await c.get(
            "/api/discovery/authors?content_type=audiobook&search=Emrys"
        )).json()["authors"]
        # Ebooks tab — same shape.
        r_ebook = (await c.get(
            "/api/discovery/authors?content_type=ebook&search=Emrys"
        )).json()["authors"]

    for label, rows in (("all", r_all), ("audiobook", r_audio), ("ebook", r_ebook)):
        assert len(rows) == 1, f"{label}: expected 1 author row, got {len(rows)}"
        a = rows[0]
        assert a["owned_count"] == 1, f"{label}: expected owned=1, got {a['owned_count']}"
        assert a["missing_count"] == 5, f"{label}: expected missing=5, got {a['missing_count']}"
        assert a["total_books"] == 6, f"{label}: expected total=6, got {a['total_books']}"
        # The 📖🎧 badge data is the same across tabs — both formats
        # populated on every tab so the frontend can render the dual
        # icon consistently.
        assert set(a.get("content_types") or []) == {"ebook", "audiobook"}

    disco_db.set_active_library(None)


async def test_authors_list_sorts_by_last_name(
    tmp_path, monkeypatch,
):
    """v2.17.2 — cross-library Authors list sort keys on LAST NAME
    (matching the frontend's letter-sidebar `getLetterKey` logic),
    not on `sort_name`. Calibre's `sort_name` is inconsistent
    ("Last, First" for most rows, but raw "First Last" for
    pseudonyms / some imports), so SQL ORDER BY sort_name placed
    "Emrys Ambrosius" under E even though the alphabet sidebar
    correctly grouped them under A. Both layers now agree.
    """
    from app import config as app_config
    from app import database, state
    from app.discovery import database as disco_db
    from app.discovery.database import get_db
    from app.discovery.routers.authors import router

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    # v2.20.0 — /authors list dedup uses author_links from the global DB.
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "seshat.db")
    await database.init_db()
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "calibre", "content_type": "ebook", "name": "Calibre"},
    ])

    disco_db.set_active_library("calibre")
    await disco_db.init_db("calibre")
    db = await get_db("calibre")
    try:
        # Mix of authors with different sort_name conventions
        # (the bug's repro condition). All start with "A"-letters
        # by LAST name but sort_name varies wildly.
        rows = [
            ("Alfir2", "Alfir2"),                # single-word, no comma
            ("Ameria", "Ameria"),                # single-word
            ("Emrys Ambrosius", "Emrys Ambrosius"),  # buggy "First Last"
            ("Ann Aguirre", "Aguirre, Ann"),     # canonical "Last, First"
            ("Ryan Anderson", "Anderson, Ryan"), # canonical
        ]
        for name, sort_name in rows:
            cur = await db.execute(
                "INSERT INTO authors (name, sort_name) VALUES (?, ?)",
                (name, sort_name),
            )
            aid = cur.lastrowid
            bk = await db.execute(
                "INSERT INTO books (title, owned, hidden) VALUES ('B', 0, 0)",
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, ?, 0)",
                (bk.lastrowid, aid),
            )
        await db.commit()
    finally:
        await db.close()

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.get("/api/discovery/authors?content_type=all&sort=name&sort_dir=asc")
    assert r.status_code == 200
    names = [a["name"] for a in r.json()["authors"]]
    # Expected order by LAST NAME:
    # Aguirre < Alfir2 < Ambrosius < Ameria < Anderson
    assert names == [
        "Ann Aguirre",
        "Alfir2",
        "Emrys Ambrosius",
        "Ameria",
        "Ryan Anderson",
    ], f"unexpected order: {names}"

    disco_db.set_active_library(None)


async def test_authors_list_audiobook_only_excluded_from_ebook_tab(
    tmp_path, monkeypatch,
):
    """v2.17.1 — the post-merge content_type filter correctly
    excludes authors that don't have books in the requested format.
    An audiobook-only author appears on All + Audiobooks but NOT on
    Ebooks.
    """
    from app import config as app_config
    from app import database, state
    from app.discovery import database as disco_db
    from app.discovery.database import get_db
    from app.discovery.routers.authors import router

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    # v2.20.0 — /authors list dedup uses author_links from the global DB.
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "seshat.db")
    await database.init_db()
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "calibre", "content_type": "ebook", "name": "Calibre"},
        {"slug": "abs", "content_type": "audiobook", "name": "ABS"},
    ])

    for slug in ("calibre", "abs"):
        disco_db.set_active_library(slug)
        await disco_db.init_db(slug)

    # Audiobook-only author with no Calibre presence.
    disco_db.set_active_library("abs")
    db = await get_db("abs")
    try:
        await db.execute("INSERT INTO authors (name, sort_name) VALUES ('Eugene Astakhov', 'Astakhov')")
        cur = await db.execute(
            "INSERT INTO books (title, owned, hidden) VALUES ('Audio', 1, 0)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, 1, 0)",
            (cur.lastrowid,),
        )
        await db.commit()
    finally:
        await db.close()

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        r_all = (await c.get(
            "/api/discovery/authors?content_type=all&search=Astakhov"
        )).json()["authors"]
        r_audio = (await c.get(
            "/api/discovery/authors?content_type=audiobook&search=Astakhov"
        )).json()["authors"]
        r_ebook = (await c.get(
            "/api/discovery/authors?content_type=ebook&search=Astakhov"
        )).json()["authors"]

    assert len(r_all) == 1
    assert len(r_audio) == 1
    assert len(r_ebook) == 0, (
        "audiobook-only author must not appear on Ebooks tab "
        "(post-merge content_type filter)"
    )

    disco_db.set_active_library(None)


async def test_bulk_hide_authors_books_cascades_across_libraries(
    tmp_path, monkeypatch,
):
    """v2.17.0 Feat C — `POST /authors/bulk-hide-books` accepts a
    list of author names and hides every book they wrote across
    every configured library. Books rows stay (hidden=1); the
    author rows themselves are untouched so the v2.12.1 dual-row
    mirror pattern isn't disturbed.
    """
    from app import config as app_config
    from app import database, state
    from app.discovery import database as disco_db
    from app.discovery.database import get_db
    from app.discovery.routers.authors import router

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    # v2.20.0 — /authors list dedup uses author_links from the global DB.
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "seshat.db")
    await database.init_db()
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "calibre", "content_type": "ebook", "name": "Calibre"},
        {"slug": "abs", "content_type": "audiobook", "name": "ABS"},
    ])

    for slug in ("calibre", "abs"):
        disco_db.set_active_library(slug)
        await disco_db.init_db(slug)

    # Seed Hatton in both libraries — 3 ebooks in Calibre, 1 audiobook in ABS.
    for slug, n_books in (("calibre", 3), ("abs", 1)):
        disco_db.set_active_library(slug)
        db = await get_db(slug)
        try:
            await db.execute(
                "INSERT INTO authors (name, sort_name) VALUES ('R. A. Hatton', 'Hatton')"
            )
            for i in range(n_books):
                cur = await db.execute(
                    "INSERT INTO books (title, owned, hidden) VALUES (?, 0, 0)",
                    (f"Book {i} ({slug})",),
                )
                await db.execute(
                    "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                    "VALUES (?, 1, 0)",
                    (cur.lastrowid,),
                )
            await db.commit()
        finally:
            await db.close()

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post(
            "/api/discovery/authors/bulk-hide-books",
            json={"author_names": ["R. A. Hatton"]},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # 3 ebooks + 1 audiobook = 4 books hidden across 2 libs.
    assert body["books_hidden"] == 4
    assert body["libraries_touched"] == 2

    # Confirm books are hidden and author rows still exist.
    for slug in ("calibre", "abs"):
        db = await get_db(slug)
        try:
            cur = await db.execute(
                "SELECT COUNT(*) AS n FROM books WHERE hidden = 1"
            )
            row = await cur.fetchone()
            assert (row["n"] or 0) >= 1
            cur = await db.execute(
                "SELECT COUNT(*) AS n FROM authors WHERE name = 'R. A. Hatton'"
            )
            row = await cur.fetchone()
            assert row["n"] == 1, (
                f"author row in {slug} must survive bulk-hide-books"
            )
        finally:
            await db.close()

    disco_db.set_active_library(None)


async def test_bulk_delete_authors_books_skips_library_synced(
    tmp_path, monkeypatch,
):
    """v2.17.0 Feat C — `POST /authors/bulk-delete-books` removes
    unowned discovery rows but skips Calibre / Audiobookshelf-synced
    books (those rows are upstream-managed). Returns per-library
    counts so the toast can summarize the partial outcome.
    """
    from app import config as app_config
    from app import database, state
    from app.discovery import database as disco_db
    from app.discovery.database import get_db
    from app.discovery.routers.authors import router

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    # v2.20.0 — /authors list dedup uses author_links from the global DB.
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "seshat.db")
    await database.init_db()
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "calibre", "content_type": "ebook", "name": "Calibre"},
    ])

    disco_db.set_active_library("calibre")
    await disco_db.init_db("calibre")
    db = await get_db("calibre")
    try:
        await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES ('Test Author', 'Test')"
        )
        # 2 unowned discovery rows (deletable).
        cur = await db.execute(
            "INSERT INTO books (title, owned, hidden) VALUES ('Unowned A', 0, 0)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, 1, 0)",
            (cur.lastrowid,),
        )
        cur = await db.execute(
            "INSERT INTO books (title, owned, hidden) VALUES ('Unowned B', 0, 0)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, 1, 0)",
            (cur.lastrowid,),
        )
        # 1 Calibre-synced row (must be skipped).
        cur = await db.execute(
            "INSERT INTO books (title, owned, hidden, calibre_id) VALUES ('Calibre-synced', 1, 0, 42)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, 1, 0)",
            (cur.lastrowid,),
        )
        await db.commit()
    finally:
        await db.close()

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.post(
            "/api/discovery/authors/bulk-delete-books",
            json={"author_names": ["Test Author"]},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["books_deleted"] == 2
    assert body["books_skipped"] == 1

    # Confirm the Calibre-synced row survived.
    db = await get_db("calibre")
    try:
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM books WHERE calibre_id IS NOT NULL"
        )
        row = await cur.fetchone()
        assert row["n"] == 1
    finally:
        await db.close()

    disco_db.set_active_library(None)


async def test_global_stats_sums_primary_plus_cross_library(
    tmp_path, monkeypatch,
):
    """v2.17.0 Bug B — when `include_cross_library=1` is passed, the
    `global_stats` field on the response sums owned / total / series
    across the primary library + every cross-library entry. Repro:
    an audiobook-only author has 1 owned audiobook in ABS but 4
    unowned ebooks in Calibre (post cross-format-scan). Per-library
    counts would show "1 owned, 0 missing" on the ABS side; global
    should show "1 owned, 4 missing".
    """
    from app import config as app_config
    from app import database, state
    from app.discovery import database as disco_db
    from app.discovery.database import get_db
    from app.discovery.routers.authors import router

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    # v2.20.0 — `/authors/{aid}` reads `author_links`; ensure the
    # global identity DB exists in the test env.
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "seshat.db")
    await database.init_db()
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "calibre", "content_type": "ebook", "name": "Calibre"},
        {"slug": "abs", "content_type": "audiobook", "name": "Audiobookshelf"},
    ])

    # Seed both libraries' DBs with the same author (dual-row pattern).
    for slug in ("calibre", "abs"):
        disco_db.set_active_library(slug)
        await disco_db.init_db(slug)

    # Calibre side: author + 4 unowned ebooks in 1 series.
    disco_db.set_active_library("calibre")
    db = await get_db("calibre")
    try:
        await db.execute("INSERT INTO authors (id, name, sort_name) VALUES (10, 'V. E. Schwab', 'Schwab')")
        await db.execute("INSERT INTO series (id, name, author_id) VALUES (1, 'Shades of Magic', 10)")
        for i in range(4):
            cur = await db.execute(
                "INSERT INTO books (title, series_id, owned, hidden) VALUES (?, 1, 0, 0)",
                (f"Ebook {i+1}",),
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, 10, 0)",
                (cur.lastrowid,),
            )
        await db.commit()
    finally:
        await db.close()

    # ABS side: same author (different per-library id) + 1 owned audiobook in a different series.
    disco_db.set_active_library("abs")
    db = await get_db("abs")
    try:
        await db.execute("INSERT INTO authors (id, name, sort_name) VALUES (266, 'V. E. Schwab', 'Schwab')")
        await db.execute("INSERT INTO series (id, name, author_id) VALUES (1, 'Audio Stories', 266)")
        cur = await db.execute(
            "INSERT INTO books (title, series_id, owned, hidden) VALUES ('Audio 1', 1, 1, 0)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, 266, 0)",
            (cur.lastrowid,),
        )
        await db.commit()
    finally:
        await db.close()

    # Hit the endpoint with include_cross_library=1, primary=abs side.
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        r = await c.get(
            "/api/discovery/authors/266?include_cross_library=1&slug=abs"
        )
    assert r.status_code == 200
    body = r.json()
    assert "global_stats" in body
    gs = body["global_stats"]
    # 1 owned audiobook + 0 owned ebooks = 1
    assert gs["owned"] == 1
    # 4 unowned ebooks + 0 unowned audiobooks = 4 missing
    assert gs["missing"] == 4
    # 5 total books across both libraries
    assert gs["total"] == 5
    # 2 distinct series (Shades of Magic + Audio Stories)
    assert gs["series_count"] == 2

    disco_db.set_active_library(None)
