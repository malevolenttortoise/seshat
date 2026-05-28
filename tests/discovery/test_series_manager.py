"""
v2.3 Series Manager backend API tests.

Covers the mutation endpoints (`/series/promote`, `/{sid}/demote`,
PATCH, DELETE, `/{sid}/books`, DELETE `/{sid}/books/{book_id}`)
plus the new `shared` filter on the list endpoint.
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
    yield tmp_path
    disco_db.set_active_library(None)


@pytest.fixture
async def client(discovery_db):
    from app.discovery.routers import series as series_router

    app = FastAPI()
    app.include_router(series_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _seed():
    """Seed two authors with one per-author 'Halo' series each, books
    on each. Returns (cressman_id, savarovsky_id, cressman_series_id,
    savarovsky_series_id)."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name) VALUES "
            "(101, 'Eric Nylund', 'Nylund'), "
            "(102, 'Tobias S. Buckell', 'Buckell')"
        )
        await db.execute(
            "INSERT INTO series (id, name, author_id) VALUES "
            "(900, 'Halo', 101), (901, 'Halo', 102)"
        )
        await db.execute(
            "INSERT INTO books (id, title, series_id, series_index) "
            "VALUES (1, 'Reach', 900, 1.0), "
            "(2, 'Cole Protocol', 901, 6.0)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 101, 0)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (2, 102, 0)"
        )
        await db.commit()
        return 101, 102, 900, 901
    finally:
        await db.close()


async def _series_count():
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute("SELECT COUNT(*) FROM series")).fetchone()
        return row[0]
    finally:
        await db.close()


async def _series_row(sid: int):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT id, name, author_id FROM series WHERE id = ?", (sid,)
        )).fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def _book_series(book_id: int):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT series_id, series_index FROM books WHERE id = ?",
            (book_id,),
        )).fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


class TestPromote:
    async def test_promotes_two_per_author_into_shared(self, client):
        await _seed()

        r = await client.post(
            "/api/discovery/series/promote",
            json={"series_ids": [900, 901]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["books_moved"] == 2
        assert sorted(body["promoted_from"]) == [900, 901]
        shared_id = body["shared_id"]

        # Shared row exists with author_id NULL.
        row = await _series_row(shared_id)
        assert row["author_id"] is None
        assert row["name"] == "Halo"

        # Old rows are gone.
        assert await _series_row(900) is None
        assert await _series_row(901) is None

        # Books point at shared row.
        b1 = await _book_series(1)
        b2 = await _book_series(2)
        assert b1["series_id"] == shared_id
        assert b2["series_id"] == shared_id

    async def test_rejects_single_id(self, client):
        await _seed()
        r = await client.post(
            "/api/discovery/series/promote", json={"series_ids": [900]},
        )
        assert r.status_code == 400

    async def test_rejects_already_shared(self, client):
        from app.discovery.database import get_db
        await _seed()
        # Make 900 already shared.
        db = await get_db()
        try:
            await db.execute(
                "UPDATE series SET author_id = NULL WHERE id = 900"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.post(
            "/api/discovery/series/promote",
            json={"series_ids": [900, 901]},
        )
        assert r.status_code == 400
        assert "already-shared" in r.text

    async def test_404_on_missing_id(self, client):
        await _seed()
        r = await client.post(
            "/api/discovery/series/promote",
            json={"series_ids": [900, 999]},
        )
        assert r.status_code == 404


class TestDemote:
    async def test_splits_shared_into_per_author(self, client):
        from app.discovery.database import get_db
        # Seed a shared row with books from two authors.
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'Eric Nylund', 'Nylund'), "
                "(102, 'Tobias S. Buckell', 'Buckell')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) VALUES "
                "(900, 'Halo', NULL)"
            )
            await db.execute(
                "INSERT INTO books (id, title, series_id) "
                "VALUES (1, 'Reach', 900), "
                "(2, 'Cole Protocol', 900)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 101, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (2, 102, 0)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.post("/api/discovery/series/900/demote")
        assert r.status_code == 200
        body = r.json()
        assert body["books_moved"] == 2
        assert len(body["new_series_ids"]) == 2

        # Shared row is gone.
        assert await _series_row(900) is None

        # Each book points at its author's per-author row.
        b1 = await _book_series(1)
        b2 = await _book_series(2)
        new_ids = set(body["new_series_ids"])
        assert b1["series_id"] in new_ids
        assert b2["series_id"] in new_ids
        assert b1["series_id"] != b2["series_id"]

    async def test_rejects_per_author_row(self, client):
        await _seed()
        r = await client.post("/api/discovery/series/900/demote")
        assert r.status_code == 400


class TestRename:
    async def test_renames_series(self, client):
        await _seed()
        r = await client.patch(
            "/api/discovery/series/900", json={"name": "Halo Saga"},
        )
        assert r.status_code == 200
        row = await _series_row(900)
        assert row["name"] == "Halo Saga"

    async def test_rejects_empty_name(self, client):
        await _seed()
        r = await client.patch(
            "/api/discovery/series/900", json={"name": "  "},
        )
        assert r.status_code == 400

    async def test_409_on_conflict(self, client):
        # Seed a per-author row (900) and another for the same author
        # with a different name (902). Renaming 900 to "Other" collides
        # with 902.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'A', 'A')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) VALUES "
                "(900, 'Halo', 101), (902, 'Other', 101)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.patch(
            "/api/discovery/series/900", json={"name": "Other"},
        )
        assert r.status_code == 409
        # FastAPI wraps the dict body in {"detail": ...}.
        assert r.json()["detail"]["conflict_id"] == 902


class TestDelete:
    async def test_deletes_series_and_orphans_books(self, client):
        await _seed()
        r = await client.delete("/api/discovery/series/900")
        assert r.status_code == 200
        body = r.json()
        assert body["books_orphaned"] == 1

        # Series gone, book is standalone.
        assert await _series_row(900) is None
        b = await _book_series(1)
        assert b["series_id"] is None
        assert b["series_index"] is None


class TestMembership:
    async def test_add_books_to_series(self, client):
        from app.discovery.database import get_db
        await _seed()
        # Insert a standalone book to add into a series.
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO books (id, title) "
                "VALUES (3, 'Standalone')"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (3, 101, 0)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.post(
            "/api/discovery/series/900/books",
            json={"book_ids": [3], "indices": {"3": 2.0}},
        )
        assert r.status_code == 200
        b = await _book_series(3)
        assert b["series_id"] == 900
        assert b["series_index"] == 2.0

    async def test_remove_book_from_series(self, client):
        await _seed()
        r = await client.delete("/api/discovery/series/900/books/1")
        assert r.status_code == 200
        b = await _book_series(1)
        assert b["series_id"] is None
        assert b["series_index"] is None

    async def test_remove_404_if_not_member(self, client):
        await _seed()
        # book 2 is on series 901, not 900.
        r = await client.delete("/api/discovery/series/900/books/2")
        assert r.status_code == 404


class TestSharedFilter:
    async def test_shared_true_returns_shared_only(self, client):
        # Seed a shared series WITH a book — post-v2.3.4.2 the
        # default list filters out series with zero visible books.
        from app.discovery.database import get_db
        await _seed()
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (902, 'Halo Universe', NULL)"
            )
            await db.execute(
                "INSERT INTO books (id, title, series_id) "
                "VALUES (3, 'Universe Book', 902)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (3, 101, 0)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.get("/api/discovery/series?shared=true")
        ids = {s["id"] for s in r.json()["series"]}
        assert ids == {902}

    async def test_shared_false_returns_per_author_only(self, client):
        from app.discovery.database import get_db
        await _seed()
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (902, 'Halo Universe', NULL)"
            )
            await db.execute(
                "INSERT INTO books (id, title, series_id) "
                "VALUES (3, 'Universe Book', 902)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (3, 101, 0)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.get("/api/discovery/series?shared=false")
        ids = {s["id"] for s in r.json()["series"]}
        assert ids == {900, 901}


# ── v2.3.3 list endpoint: pagination + book-title search + cover_book_id


class TestListPagination:
    async def test_response_shape_includes_total_limit_offset(self, client):
        await _seed()
        r = await client.get("/api/discovery/series")
        body = r.json()
        assert "series" in body
        assert "total" in body
        assert "limit" in body
        assert "offset" in body
        assert body["total"] == 2
        assert body["offset"] == 0
        assert body["limit"] == 50  # default

    async def test_limit_and_offset_paginate(self, client):
        # Seed 5 standalone series (one author so all are per-author).
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'A Author', 'Author')"
            )
            for i in range(1, 6):
                await db.execute(
                    "INSERT INTO series (id, name, author_id) "
                    "VALUES (?, ?, 101)", (900 + i, f"Series {i}"),
                )
                await db.execute(
                    "INSERT INTO books (id, title, series_id) "
                    "VALUES (?, ?, ?)",
                    (i, f"Book {i}", 900 + i),
                )
                await db.execute(
                    "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, 101, 0)",
                    (i,),
                )
            await db.commit()
        finally:
            await db.close()

        r = await client.get(
            "/api/discovery/series?limit=2&offset=0&sort=name&sort_dir=asc",
        )
        body = r.json()
        assert body["total"] == 5
        assert len(body["series"]) == 2
        names = [s["name"] for s in body["series"]]
        assert names == ["Series 1", "Series 2"]

        r2 = await client.get(
            "/api/discovery/series?limit=2&offset=2&sort=name&sort_dir=asc",
        )
        body2 = r2.json()
        assert body2["total"] == 5
        names2 = [s["name"] for s in body2["series"]]
        assert names2 == ["Series 3", "Series 4"]

    async def test_limit_caps_at_200(self, client):
        await _seed()
        # Over-limit gets rejected by FastAPI Query validation (422).
        r = await client.get("/api/discovery/series?limit=500")
        assert r.status_code == 422


class TestListSearch:
    async def test_search_matches_series_name(self, client):
        await _seed()
        r = await client.get("/api/discovery/series?search=Halo")
        ids = {s["id"] for s in r.json()["series"]}
        assert ids == {900, 901}

    async def test_search_matches_author_name(self, client):
        await _seed()
        r = await client.get("/api/discovery/series?search=Buckell")
        ids = {s["id"] for s in r.json()["series"]}
        # Only 901 belongs to Buckell.
        assert ids == {901}

    async def test_search_matches_book_title(self, client):
        # Series name "Saga", but search for "Reach" should still find it
        # because book 1 (titled 'Reach') is on the series.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'Eric Nylund', 'Nylund')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (900, 'Saga', 101)"
            )
            await db.execute(
                "INSERT INTO books (id, title, series_id) "
                "VALUES (1, 'Reach', 900), "
                "(2, 'Cole', 900)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 101, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (2, 101, 0)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.get("/api/discovery/series?search=Reach")
        body = r.json()
        ids = {s["id"] for s in body["series"]}
        assert ids == {900}
        # Critical: book_count includes BOTH books, not just the one
        # whose title matched. Row-level WHERE on b.title would have
        # broken this — guard against regression.
        assert body["series"][0]["book_count"] == 2

    async def test_search_total_reflects_filter(self, client):
        # Series need at least one visible book to appear in the
        # default list (v2.3.4.2 filter).
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'A', 'A')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) VALUES "
                "(900, 'Halo', 101), "
                "(901, 'Other', 101)"
            )
            await db.execute(
                "INSERT INTO books (id, title, series_id) VALUES "
                "(1, 'Reach', 900), "
                "(2, 'Different', 901)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 101, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (2, 101, 0)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.get("/api/discovery/series?search=Halo")
        body = r.json()
        assert body["total"] == 1


class TestListCoverBookId:
    async def test_returns_first_book_id(self, client):
        # Series 900 has books 1 (index 1.0) and 2 (index 2.0). The
        # earliest by series_index (book 1) wins.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'A', 'A')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (900, 'Halo', 101)"
            )
            await db.execute(
                "INSERT INTO books (id, title, series_id, "
                "series_index, cover_path) VALUES "
                "(1, 'First', 900, 1.0, '/covers/1.jpg'), "
                "(2, 'Second', 900, 2.0, '/covers/2.jpg')"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 101, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (2, 101, 0)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.get("/api/discovery/series")
        s = r.json()["series"][0]
        assert s["cover_book_id"] == 1

    async def test_prefers_book_with_cover(self, client):
        # First book by index has no cover; second does. The "has
        # cover" book wins despite later index.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'A', 'A')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (900, 'Halo', 101)"
            )
            await db.execute(
                "INSERT INTO books (id, title, series_id, "
                "series_index, cover_path, cover_url) VALUES "
                "(1, 'NoCover', 900, 1.0, NULL, NULL), "
                "(2, 'WithCover', 900, 2.0, '/c/2.jpg', NULL)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 101, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (2, 101, 0)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.get("/api/discovery/series")
        s = r.json()["series"][0]
        assert s["cover_book_id"] == 2

    async def test_null_when_series_has_no_books(self, client):
        # Empty series are filtered from the default list (v2.3.4.2).
        # Use include_empty=true to surface them — confirms the cover
        # falls back to NULL.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (900, 'Empty', NULL)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.get("/api/discovery/series?include_empty=true")
        s = next(x for x in r.json()["series"] if x["id"] == 900)
        assert s["cover_book_id"] is None


class TestEmptySeriesFilter:
    """v2.3.4.2: hide series with zero visible books from the default
    list. Both fully-hidden series (Mark's '2B Trilogy' canary —
    3 books, all hidden) and orphaned series (no books at all) drop
    out unless the caller passes include_empty=true."""

    async def test_fully_hidden_series_is_excluded_by_default(self, client):
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'Ann Aguirre', 'Aguirre')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (900, '2B Trilogy', 101)"
            )
            await db.execute(
                "INSERT INTO books (id, title, series_id, hidden) VALUES "
                "(1, 'b1', 900, 1), "
                "(2, 'b2', 900, 1), "
                "(3, 'b3', 900, 1)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 101, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (2, 101, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (3, 101, 0)"
            )
            await db.commit()
        finally:
            await db.close()

        # Default list excludes the all-hidden series.
        r = await client.get("/api/discovery/series")
        ids = {s["id"] for s in r.json()["series"]}
        assert ids == set()

        # include_empty=true surfaces it for cleanup.
        r2 = await client.get("/api/discovery/series?include_empty=true")
        ids2 = {s["id"] for s in r2.json()["series"]}
        assert 900 in ids2

    async def test_orphaned_series_is_excluded_by_default(self, client):
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (900, 'Orphan', NULL)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.get("/api/discovery/series")
        assert r.json()["total"] == 0

        r2 = await client.get("/api/discovery/series?include_empty=true")
        assert r2.json()["total"] == 1

    async def test_visible_series_appears_in_both_modes(self, client):
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'A', 'A')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (900, 'Real', 101)"
            )
            await db.execute(
                "INSERT INTO books (id, title, series_id) "
                "VALUES (1, 'Book', 900)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 101, 0)"
            )
            await db.commit()
        finally:
            await db.close()

        for qs in ["", "?include_empty=true"]:
            r = await client.get(f"/api/discovery/series{qs}")
            assert r.json()["total"] == 1, f"failed for qs={qs!r}"

    async def test_has_missing_implies_visible_books(self, client):
        # has_missing=true was already tighter than the new default
        # filter (missing_count > 0 implies book_count > 0). Confirm
        # the v2.3.4.2 change didn't accidentally collapse both.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'A', 'A')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) VALUES "
                "(900, 'AllOwned', 101), (901, 'HasMissing', 101)"
            )
            await db.execute(
                "INSERT INTO books (id, title, series_id, owned) VALUES "
                "(1, 'a', 900, 1), "
                "(2, 'b', 901, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 101, 0)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (2, 101, 0)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.get("/api/discovery/series?has_missing=true")
        ids = {s["id"] for s in r.json()["series"]}
        assert ids == {901}
