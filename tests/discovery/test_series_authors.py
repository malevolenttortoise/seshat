"""
v2.3.3 Series Manager author-level membership endpoints.

Covers:
  - GET    /series/{sid}/authors      — distinct author list
  - POST   /series/{sid}/authors      — assign one author's books
  - DELETE /series/{sid}/authors/{aid} — detach all of one author's books
  - Auto-flip side effects of the existing book-level endpoints
    after they were wired to call _recompute_series_author.

The fixtures mirror test_series_manager.py — same in-memory discovery
DB, same FastAPI test client.
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


@pytest.fixture
async def book_client(discovery_db):
    """Test client mounting both `books` and `series` routers — needed
    for the v2.3.4 hide/unhide → series-authority recompute path,
    where a books-router endpoint must trigger series-side updates."""
    from app.discovery.routers import series as series_router
    from app.discovery.routers import books as books_router

    app = FastAPI()
    app.include_router(series_router.router)
    app.include_router(books_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ── shared helpers (kept local; the test_series_manager.py copies are
# not exported and the duplication is small enough to be fine) ───────


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


async def _link_book_authors(db) -> None:
    """v3.0.0 Phase 6 (ADR-0010): _recompute_series_author reads
    book_authors (the contributor-set intersection). Mirror the prod
    backfill — link every seeded book to its author_id at position 0 —
    so the recompute sees contributor data. Idempotent.
    """
    await db.execute(
        "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
        "SELECT id, author_id, 0 FROM books WHERE author_id IS NOT NULL "
        "AND id NOT IN (SELECT book_id FROM book_authors)"
    )
    await db.commit()


async def _seed_two_per_author_series():
    """Seed: two authors, each with their own per-author 'Halo' row,
    one book per series. Mirrors the legacy promote-target setup."""
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
            "INSERT INTO books (id, title, author_id, series_id, series_index) "
            "VALUES (1, 'Reach', 101, 900, 1.0), "
            "(2, 'Cole Protocol', 102, 901, 6.0)"
        )
        await db.commit()
        await _link_book_authors(db)
    finally:
        await db.close()


async def _seed_shared_two_author():
    """Seed: one shared 'Halo' (author_id=NULL) with two books from
    two different authors."""
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
            "(900, 'Halo', NULL)"
        )
        await db.execute(
            "INSERT INTO books (id, title, author_id, series_id) "
            "VALUES (1, 'Reach', 101, 900), "
            "(2, 'Cole Protocol', 102, 900)"
        )
        await db.commit()
        await _link_book_authors(db)
    finally:
        await db.close()


# ── GET /series/{sid}/authors ────────────────────────────────────────


class TestListSeriesAuthors:
    async def test_returns_distinct_authors_with_book_counts(self, client):
        await _seed_shared_two_author()
        r = await client.get("/api/discovery/series/900/authors")
        assert r.status_code == 200
        body = r.json()
        assert body["series_id"] == 900
        names = [a["name"] for a in body["authors"]]
        # Sorted alphabetically.
        assert names == ["Eric Nylund", "Tobias S. Buckell"]
        counts = {a["author_id"]: a["book_count"] for a in body["authors"]}
        assert counts == {101: 1, 102: 1}

    async def test_returns_empty_for_orphaned_series(self, client):
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
        r = await client.get("/api/discovery/series/900/authors")
        assert r.status_code == 200
        assert r.json()["authors"] == []

    async def test_404_on_unknown_series(self, client):
        r = await client.get("/api/discovery/series/999/authors")
        assert r.status_code == 404


# ── POST /series/{sid}/authors ───────────────────────────────────────


class TestAddAuthorToSeries:
    async def test_add_author_flips_destination_to_shared(self, client):
        # 900 is per-author Eric (101). Adding Tobias's (102) book
        # to it should flip 900 to shared.
        await _seed_two_per_author_series()
        r = await client.post(
            "/api/discovery/series/900/authors",
            json={"author_id": 102, "book_ids": [2]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["added"] == 1
        assert body["authority"] == "shared"
        assert body["source_series_recomputed"] == [901]

        # Destination flipped.
        assert (await _series_row(900))["author_id"] is None
        # Book is now on 900 with NULL index.
        b2 = await _book_series(2)
        assert b2["series_id"] == 900
        assert b2["series_index"] is None

    async def test_source_series_flips_back_when_emptied(self, client):
        # Start with a shared 'Halo' (900) holding both authors'
        # books. Add Tobias's book to a NEW destination — source
        # 900 loses its only Tobias book, flips to per-author Eric.
        await _seed_shared_two_author()
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (902, 'Halo Universe', 102)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.post(
            "/api/discovery/series/902/authors",
            json={"author_id": 102, "book_ids": [2]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["source_series_recomputed"] == [900]

        # Source 900 was shared; now it has only Eric's book → flips
        # back to per-author 101.
        src = await _series_row(900)
        assert src["author_id"] == 101

        # Destination still per-author (single Tobias book) — the
        # author count stayed at 1.
        dest = await _series_row(902)
        assert dest["author_id"] == 102

    async def test_rejects_book_by_wrong_author(self, client):
        await _seed_two_per_author_series()
        # Book 1 is by author 101; claim it's by 102.
        r = await client.post(
            "/api/discovery/series/901/authors",
            json={"author_id": 102, "book_ids": [1]},
        )
        assert r.status_code == 400
        assert "not by author" in r.text

    async def test_rejects_unknown_book(self, client):
        await _seed_two_per_author_series()
        r = await client.post(
            "/api/discovery/series/900/authors",
            json={"author_id": 101, "book_ids": [9999]},
        )
        assert r.status_code == 404

    async def test_rejects_empty_book_ids(self, client):
        await _seed_two_per_author_series()
        r = await client.post(
            "/api/discovery/series/900/authors",
            json={"author_id": 101, "book_ids": []},
        )
        assert r.status_code == 400

    async def test_rejects_missing_author_id(self, client):
        await _seed_two_per_author_series()
        r = await client.post(
            "/api/discovery/series/900/authors",
            json={"book_ids": [1]},
        )
        assert r.status_code == 400

    async def test_404_on_unknown_destination_series(self, client):
        await _seed_two_per_author_series()
        r = await client.post(
            "/api/discovery/series/9999/authors",
            json={"author_id": 101, "book_ids": [1]},
        )
        assert r.status_code == 404

    async def test_404_on_unknown_author(self, client):
        await _seed_two_per_author_series()
        r = await client.post(
            "/api/discovery/series/900/authors",
            json={"author_id": 999, "book_ids": [1]},
        )
        assert r.status_code == 404


# ── DELETE /series/{sid}/authors/{author_id} ─────────────────────────


class TestRemoveAuthorFromSeries:
    async def test_detach_flips_shared_to_per_author(self, client):
        # Shared with two authors → remove Tobias → flip to per-author Eric.
        await _seed_shared_two_author()
        r = await client.delete(
            "/api/discovery/series/900/authors/102",
        )
        assert r.status_code == 200
        body = r.json()
        assert body["removed"] == 1
        assert body["authority"] == "per_author"

        # Series flipped back.
        assert (await _series_row(900))["author_id"] == 101
        # Tobias's book detached.
        b2 = await _book_series(2)
        assert b2["series_id"] is None
        assert b2["series_index"] is None
        # Eric's book still on the series.
        b1 = await _book_series(1)
        assert b1["series_id"] == 900

    async def test_detach_orphans_series_when_only_author(self, client):
        # Per-author series with one book → remove that author →
        # series ends up with 0 books, helper no-ops on authority.
        await _seed_two_per_author_series()
        r = await client.delete(
            "/api/discovery/series/900/authors/101",
        )
        assert r.status_code == 200

        # Series row still exists, author_id unchanged (per-author 101)
        # because 0-book branch is a no-op.
        row = await _series_row(900)
        assert row is not None
        assert row["author_id"] == 101
        # Book detached.
        b1 = await _book_series(1)
        assert b1["series_id"] is None

    async def test_404_when_author_has_no_books_on_series(self, client):
        await _seed_two_per_author_series()
        # Series 900 holds Eric (101) only; Tobias (102) has nothing here.
        r = await client.delete(
            "/api/discovery/series/900/authors/102",
        )
        assert r.status_code == 404

    async def test_404_on_unknown_series(self, client):
        await _seed_two_per_author_series()
        r = await client.delete(
            "/api/discovery/series/9999/authors/101",
        )
        assert r.status_code == 404


# ── auto-flip via existing book-level endpoints ──────────────────────


class TestBookLevelAutoFlip:
    async def test_add_books_flips_destination_to_shared(self, client):
        # Add author 102's book (currently on 901) to series 900
        # (which is per-author 101). 900 should flip to shared.
        await _seed_two_per_author_series()
        r = await client.post(
            "/api/discovery/series/900/books",
            json={"book_ids": [2]},
        )
        assert r.status_code == 200

        assert (await _series_row(900))["author_id"] is None
        # 901 lost its only book → 0-book branch leaves authority as-is.
        # Either result is acceptable per the helper contract; we
        # don't assert on 901 here.

    async def test_add_books_flips_source_back_when_emptied(self, client):
        # Shared 900 (Eric + Tobias). Move Tobias's book to a fresh
        # per-author 901 → 900 should flip from shared back to per-author Eric.
        await _seed_shared_two_author()
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (901, 'Other Series', 102)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.post(
            "/api/discovery/series/901/books",
            json={"book_ids": [2]},
        )
        assert r.status_code == 200

        # Source 900: was shared, lost its only Tobias book → per-author 101.
        assert (await _series_row(900))["author_id"] == 101
        # Destination 901: still per-author 102 (single author, same author).
        assert (await _series_row(901))["author_id"] == 102

    async def test_remove_book_flips_shared_to_per_author(self, client):
        # Shared 900 with two authors. Detach Tobias's book → flip
        # back to per-author Eric.
        await _seed_shared_two_author()
        r = await client.delete("/api/discovery/series/900/books/2")
        assert r.status_code == 200

        assert (await _series_row(900))["author_id"] == 101


# ── v2.3.4: hidden books are ignored everywhere ──────────────────────


class TestHiddenBooksRespected:
    async def test_recompute_ignores_hidden_books(self, client):
        # Per-author Alice series (900). Add a hidden Bob book
        # directly to it. Then trigger any membership operation that
        # calls _recompute_series_author — series should stay
        # per-author Alice because Bob's book is hidden.
        from app.discovery.database import get_db
        await _seed_two_per_author_series()
        db = await get_db()
        try:
            # Insert a hidden Bob book on series 900.
            await db.execute(
                "INSERT INTO books (id, title, author_id, series_id, hidden) "
                "VALUES (3, 'Hidden Bob Book', 102, 900, 1)"
            )
            await db.commit()
        finally:
            await db.close()

        # Force a recompute via a no-op-ish DELETE-detach of a book
        # that IS on series 900 — we just need any path that calls
        # the helper. The detach of book 1 leaves only the hidden
        # Bob book; per the helper rule, 0 visible books → no-op,
        # series stays per-author Alice.
        r = await client.delete("/api/discovery/series/900/books/1")
        assert r.status_code == 200
        assert (await _series_row(900))["author_id"] == 101

    async def test_get_authors_excludes_hidden(self, client):
        # Shared 900 (Alice + Bob). Hide Bob's book. GET /authors
        # should return only Alice.
        from app.discovery.database import get_db
        await _seed_shared_two_author()
        db = await get_db()
        try:
            await db.execute(
                "UPDATE books SET hidden = 1 WHERE id = 2"
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.get("/api/discovery/series/900/authors")
        body = r.json()
        names = [a["name"] for a in body["authors"]]
        # Bob (Tobias S. Buckell) should NOT appear.
        assert names == ["Eric Nylund"]
        assert body["authors"][0]["book_count"] == 1


# ── v2.3.4: hide / unhide / delete trigger series recompute ──────────


class TestHideUnhideRecomputeAuthority:
    async def test_hide_flips_shared_to_per_author(self, book_client):
        # Shared 900 (Alice + Bob). Hiding Bob's book leaves only
        # Alice's visible — series should flip to per-author Alice.
        await _seed_shared_two_author()
        r = await book_client.post("/api/discovery/books/2/hide")
        assert r.status_code == 200
        assert (await _series_row(900))["author_id"] == 101

    async def test_unhide_flips_per_author_to_shared(self, book_client):
        # Per-author Alice series (900). Add a hidden Bob book.
        # Series stays per-author until unhide. Unhide → flips shared.
        from app.discovery.database import get_db
        await _seed_two_per_author_series()
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO books (id, title, author_id, series_id, hidden) "
                "VALUES (3, 'Hidden Bob Book', 102, 900, 1)"
            )
            await db.commit()
            await _link_book_authors(db)
        finally:
            await db.close()
        # Pre-condition: still per-author 101.
        assert (await _series_row(900))["author_id"] == 101

        r = await book_client.post("/api/discovery/books/3/unhide")
        assert r.status_code == 200
        # Now 2 visible distinct authors → shared.
        assert (await _series_row(900))["author_id"] is None

    async def test_bulk_hide_flips_authority(self, book_client):
        # Shared 900 with two visible Bob books + one Alice book.
        # Bulk-hide both Bob books → flips back to per-author Alice.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'Alice', 'Alice'), "
                "(102, 'Bob', 'Bob')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (900, 'Halo', NULL)"
            )
            await db.execute(
                "INSERT INTO books (id, title, author_id, series_id) "
                "VALUES (1, 'A1', 101, 900), "
                "(2, 'B1', 102, 900), "
                "(3, 'B2', 102, 900)"
            )
            await db.commit()
            await _link_book_authors(db)
        finally:
            await db.close()

        r = await book_client.post(
            "/api/discovery/books/bulk-hide", json={"book_ids": [2, 3]},
        )
        assert r.status_code == 200
        assert (await _series_row(900))["author_id"] == 101

    async def test_delete_flips_authority(self, book_client):
        # Shared 900 (Alice + Bob, both source='goodreads' so deletable).
        # Delete Bob's book → series flips to per-author Alice.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'Alice', 'Alice'), "
                "(102, 'Bob', 'Bob')"
            )
            await db.execute(
                "INSERT INTO series (id, name, author_id) "
                "VALUES (900, 'Halo', NULL)"
            )
            await db.execute(
                "INSERT INTO books (id, title, author_id, series_id, source) "
                "VALUES (1, 'A1', 101, 900, 'goodreads'), "
                "(2, 'B1', 102, 900, 'goodreads')"
            )
            await db.commit()
            await _link_book_authors(db)
        finally:
            await db.close()

        r = await book_client.delete("/api/discovery/books/2")
        assert r.status_code == 200
        assert (await _series_row(900))["author_id"] == 101

    async def test_hide_book_not_in_series_is_safe(self, book_client):
        # Standalone book → hide should not fail when there's no
        # series to recompute.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'Alice', 'Alice')"
            )
            await db.execute(
                "INSERT INTO books (id, title, author_id) "
                "VALUES (1, 'Standalone', 101)"
            )
            await db.commit()
        finally:
            await db.close()

        r = await book_client.post("/api/discovery/books/1/hide")
        assert r.status_code == 200


# ─── v3.0.0 Phase 6 — author_mode taxonomy (ADR-0010) ────────────


async def _seed_series_with_contributors(series_books):
    """series_books: list of (book_id, primary_author_id, [contributor_ids]).
    Seeds the union of authors, one series (900, author_id NULL), the
    books, and their book_authors rows. Series 900 is left for the
    caller to recompute."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        all_authors = sorted({a for _, _, cs in series_books for a in cs})
        vals = ", ".join(f"({a}, 'Author{a}', 'Author{a}')" for a in all_authors)
        await db.execute(f"INSERT INTO authors (id, name, sort_name) VALUES {vals}")
        await db.execute(
            "INSERT INTO series (id, name, author_id) VALUES (900, 'S', NULL)")
        for bid, primary, cs in series_books:
            await db.execute(
                "INSERT INTO books (id, title, author_id, series_id) "
                "VALUES (?, ?, ?, 900)", (bid, f"B{bid}", primary))
            for pos, a in enumerate(cs):
                await db.execute(
                    "INSERT INTO book_authors (book_id, author_id, position) "
                    "VALUES (?, ?, ?)", (bid, a, pos))
        await db.commit()
    finally:
        await db.close()


async def _recompute_900():
    from app.discovery.database import get_db
    from app.discovery.routers.series import _recompute_series_author
    db = await get_db()
    try:
        await _recompute_series_author(db, [900])
        await db.commit()
        row = await (await db.execute(
            "SELECT author_mode, author_id FROM series WHERE id = 900")).fetchone()
        return row["author_mode"], row["author_id"]
    finally:
        await db.close()


class TestAuthorModeTaxonomy:
    """author_mode is computed from |I| = authors in EVERY book."""

    async def test_per_author_single_author(self, discovery_db):
        await _seed_series_with_contributors([(1, 101, [101]), (2, 101, [101])])
        assert await _recompute_900() == ("per_author", 101)

    async def test_multi_author_consistent_team(self, discovery_db):
        # Every book {101,102}, primary 101 → multi_author, anchor 101.
        await _seed_series_with_contributors([(1, 101, [101, 102]), (2, 101, [101, 102])])
        assert await _recompute_900() == ("multi_author", 101)

    async def test_shared_disjoint_authors(self, discovery_db):
        # Book 1 by 101, Book 2 by 102 — no common author → shared.
        await _seed_series_with_contributors([(1, 101, [101]), (2, 102, [102])])
        assert await _recompute_900() == ("shared", None)

    async def test_guest_coauthor_stays_per_author(self, discovery_db):
        # 101 in every book; 102 a guest on book 2 only → |I|={101} → per_author.
        await _seed_series_with_contributors([(1, 101, [101]), (2, 101, [101, 102])])
        assert await _recompute_900() == ("per_author", 101)

    async def test_multi_author_anchor_is_most_common_primary(self, discovery_db):
        # I={101,102}; 102 is primary in 2 books, 101 in 1 → anchor 102.
        await _seed_series_with_contributors([
            (1, 102, [102, 101]), (2, 102, [102, 101]), (3, 101, [101, 102]),
        ])
        assert await _recompute_900() == ("multi_author", 102)

    async def test_through_line_team_is_multi_author(self, discovery_db):
        # 101 AND 102 in every book, but co-authors rotate (103, 104).
        # I={101,102} → multi_author.
        await _seed_series_with_contributors([
            (1, 101, [101, 102, 103]), (2, 101, [101, 102, 104]),
        ])
        mode, aid = await _recompute_900()
        assert mode == "multi_author" and aid == 101


class TestAddCoAuthorToSeries:
    """v3.0.0 Phase 6: add_author_to_series validates by contributor,
    so a co-author (not the primary) can be associated."""

    async def test_coauthor_can_be_added(self, book_client):
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name) VALUES "
                "(101, 'Chaney', 'Chaney'), (102, 'Anspach', 'Anspach')")
            await db.execute(
                "INSERT INTO series (id, name, author_id, author_mode) "
                "VALUES (900, 'Galaxy Edge', 101, 'per_author')")
            # A co-authored book (primary Chaney, co-author Anspach), not
            # yet in the series.
            await db.execute(
                "INSERT INTO books (id, title, author_id) VALUES (1, 'GE1', 101)")
            for pos, a in enumerate((101, 102)):
                await db.execute(
                    "INSERT INTO book_authors (book_id, author_id, position) "
                    "VALUES (1, ?, ?)", (a, pos))
            await db.commit()
        finally:
            await db.close()

        # Add the book under co-author Anspach (102) — primary is Chaney.
        # Pre-Phase-6 this 400'd ("books not by author 102"); now it's OK.
        r = await book_client.post(
            "/api/discovery/series/900/authors",
            json={"author_id": 102, "book_ids": [1]},
        )
        assert r.status_code == 200, r.text
        assert (await _book_series(1))["series_id"] == 900
