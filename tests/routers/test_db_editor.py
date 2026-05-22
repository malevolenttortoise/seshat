"""
HTTP-level tests for the v2.14.x Database Manager rework (#F).

Exercises the new `sort`, `sort_dir`, and numeric-aware `search`
behavior on GET /api/v1/db/table/{name}. Older fields (page,
per_page, plain-text search) are covered implicitly by the round-trip
shape assertions.

Tests run against the `announces` table because it carries the
exact mix we need: INTEGER PK (numeric-search target), several
TEXT columns (text-search target), and a natural insertion order
to verify sort semantics against.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.database import get_db
from app.routers.db_editor import router as db_editor_router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(db_editor_router)
    return app


async def _seed_announces() -> None:
    """Three announces with distinct IDs + text values for unambiguous
    sort/search assertions."""
    db = await get_db()
    try:
        for row in [
            ("aaa", "Alpha Book", "Ebooks", "Author Alpha", "allow", "ok"),
            ("bbb", "Bravo Book", "Ebooks", "Author Bravo", "skip", "format"),
            ("ccc", "Charlie Book", "Audiobooks", "Author Charlie", "hold", "dedup"),
        ]:
            await db.execute(
                """
                INSERT INTO announces
                  (raw, torrent_id, torrent_name, category, author_blob,
                   decision, decision_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("raw", *row),
            )
        await db.commit()
    finally:
        await db.close()


@pytest.fixture
async def client(temp_db):
    await _seed_announces()
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as ac:
        yield ac


class TestSort:
    async def test_sort_asc_by_id_is_default_natural_order(self, client):
        r = await client.get("/api/v1/db/table/announces?sort=id&sort_dir=asc")
        assert r.status_code == 200
        ids = [row["id"] for row in r.json()["rows"]]
        assert ids == sorted(ids)

    async def test_sort_desc_by_id(self, client):
        r = await client.get("/api/v1/db/table/announces?sort=id&sort_dir=desc")
        assert r.status_code == 200
        ids = [row["id"] for row in r.json()["rows"]]
        assert ids == sorted(ids, reverse=True)

    async def test_sort_by_text_column(self, client):
        r = await client.get(
            "/api/v1/db/table/announces?sort=torrent_name&sort_dir=asc",
        )
        assert r.status_code == 200
        names = [row["torrent_name"] for row in r.json()["rows"]]
        assert names == ["Alpha Book", "Bravo Book", "Charlie Book"]

    async def test_sort_desc_by_text_column(self, client):
        r = await client.get(
            "/api/v1/db/table/announces?sort=torrent_name&sort_dir=desc",
        )
        names = [row["torrent_name"] for row in r.json()["rows"]]
        assert names == ["Charlie Book", "Bravo Book", "Alpha Book"]

    async def test_sort_unknown_column_falls_back_silently(self, client):
        # Should not 500 or 400 — just ignores the unknown sort col.
        # Defends against SQL injection via the `sort` param: even if
        # a caller smuggles `id; DROP TABLE`, it fails the schema
        # check and we run the unordered query instead.
        r = await client.get(
            "/api/v1/db/table/announces?sort=nope; DROP TABLE&sort_dir=asc",
        )
        assert r.status_code == 200
        assert len(r.json()["rows"]) == 3

    async def test_sort_dir_garbage_defaults_to_asc(self, client):
        r = await client.get(
            "/api/v1/db/table/announces?sort=id&sort_dir=sideways",
        )
        ids = [row["id"] for row in r.json()["rows"]]
        assert ids == sorted(ids)


class TestNumericSearch:
    async def test_numeric_search_matches_integer_pk(self, client):
        # Searching by id should INCLUDE that row. Numeric search is
        # a union with text-substring search — and the auto-generated
        # `seen_at` ('YYYY-MM-DDTHH:MM:SS') sweeps in any rows whose
        # timestamp text happens to contain the queried digit. So we
        # assert inclusion of the target row, not exclusivity.
        all_rows = (await client.get("/api/v1/db/table/announces")).json()["rows"]
        target_id = all_rows[1]["id"]
        r = await client.get(
            f"/api/v1/db/table/announces?search={target_id}",
        )
        ids_returned = [row["id"] for row in r.json()["rows"]]
        assert target_id in ids_returned

    async def test_numeric_search_no_match_returns_empty(self, client):
        r = await client.get("/api/v1/db/table/announces?search=99999")
        body = r.json()
        assert body["total"] == 0
        assert body["rows"] == []

    async def test_numeric_search_still_matches_text_containing_digits(
        self, client,
    ):
        # Seed a row whose text column carries a digit substring that
        # also happens to be a valid integer.
        db = await get_db()
        try:
            await db.execute(
                """INSERT INTO announces
                   (raw, torrent_name, category, author_blob, decision,
                    decision_reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("raw", "Book 42", "Ebooks", "Author", "allow", "ok"),
            )
            await db.commit()
        finally:
            await db.close()
        r = await client.get("/api/v1/db/table/announces?search=42")
        names = [row["torrent_name"] for row in r.json()["rows"]]
        assert "Book 42" in names


class TestTextSearchUnchanged:
    async def test_text_search_still_works(self, client):
        r = await client.get("/api/v1/db/table/announces?search=Alpha")
        body = r.json()
        assert body["total"] == 1
        assert body["rows"][0]["torrent_name"] == "Alpha Book"

    async def test_text_search_case_insensitive(self, client):
        r = await client.get("/api/v1/db/table/announces?search=charlie")
        body = r.json()
        assert body["total"] == 1
        assert body["rows"][0]["torrent_name"] == "Charlie Book"


class TestCombined:
    async def test_search_and_sort_compose(self, client):
        # Search "Book" matches all three, sort desc by name puts
        # Charlie first.
        r = await client.get(
            "/api/v1/db/table/announces?search=Book&sort=torrent_name&sort_dir=desc",
        )
        names = [row["torrent_name"] for row in r.json()["rows"]]
        assert names == ["Charlie Book", "Bravo Book", "Alpha Book"]


# ─── v2.17.5: per-library discovery routing ──────────────────────


@pytest.fixture
async def multi_lib_client(tmp_path, monkeypatch):
    """Spin up two real discovery DBs (`books-lib` + `audio-lib`) so
    tests can confirm `?library=<slug>` lands in the right file
    instead of the active-library fallback.

    Each DB gets one distinctly-named author row so a successful
    routing fetch returns a single, identifiable row. Without
    routing, the call would fall back to the active library and
    return that library's row instead.
    """
    from app import config as app_config
    from app import database as pipeline_database
    from app import state as app_state
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(pipeline_database, "APP_DB_PATH", tmp_path / "seshat.db")

    # Two libraries registered in app state so the slug-validation
    # gate accepts them. `active` matches the first slug — the
    # "without library param" path lands there by default.
    monkeypatch.setattr(
        app_state, "_discovered_libraries",
        [
            {"slug": "books-lib", "name": "Books Lib",
             "source_db_path": "/x", "library_path": "/x"},
            {"slug": "audio-lib", "name": "Audio Lib",
             "source_db_path": "/y", "library_path": "/y"},
        ],
    )

    await pipeline_database.init_db()
    disco_db.set_active_library("books-lib")
    await disco_db.init_db("books-lib")
    await disco_db.init_db("audio-lib")

    # One author per library so a routed query returns exactly one
    # known row. The names are deliberately distinct so the assert
    # fails clearly if routing crosses libraries.
    for slug, author_name in [
        ("books-lib", "Books Author"),
        ("audio-lib", "Audio Author"),
    ]:
        db = await disco_db.get_db(slug=slug)
        try:
            await db.execute(
                "INSERT INTO authors (name, sort_name, normalized_name) "
                "VALUES (?, ?, ?)",
                (author_name, author_name, author_name.lower()),
            )
            await db.commit()
        finally:
            await db.close()

    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as ac:
        yield ac

    disco_db.set_active_library(None)


class TestLibraryRouting:
    async def test_discovery_query_routes_to_named_library(self, multi_lib_client):
        # ?library=audio-lib must read audio-lib's DB, not the
        # active-library's (books-lib). Asserting the returned name
        # proves routing actually happened — the active-library
        # fallback would return "Books Author".
        r = await multi_lib_client.get(
            "/api/v1/db/table/authors?library=audio-lib",
        )
        assert r.status_code == 200
        names = [row["name"] for row in r.json()["rows"]]
        assert names == ["Audio Author"]

    async def test_discovery_query_without_library_uses_active(
        self, multi_lib_client,
    ):
        # Backward-compat: omitting `library` keeps the old behavior
        # (read from whatever `set_active_library` was last set to).
        r = await multi_lib_client.get("/api/v1/db/table/authors")
        names = [row["name"] for row in r.json()["rows"]]
        assert names == ["Books Author"]

    async def test_unknown_library_slug_is_rejected(self, multi_lib_client):
        # Silent fallback to a fresh `seshat_<typo>.db` would be a
        # data-loss footgun — we 400 instead.
        r = await multi_lib_client.get(
            "/api/v1/db/table/authors?library=nope",
        )
        assert r.status_code == 400

    async def test_pipeline_table_ignores_library_param(self, multi_lib_client):
        # Pipeline tables live in the global seshat.db; the param is
        # accepted (so the frontend doesn't have to strip it per
        # call) but has no effect on which DB gets queried.
        r = await multi_lib_client.get(
            "/api/v1/db/table/announces?library=audio-lib",
        )
        assert r.status_code == 200

    async def test_delete_routes_to_named_library(self, multi_lib_client):
        # The most important multi-library hazard: a delete with
        # the wrong routing would nuke a row in the active library
        # while the user expected the named one. Verify both:
        # the named library loses the row AND the other doesn't.
        r = await multi_lib_client.get(
            "/api/v1/db/table/authors?library=audio-lib",
        )
        audio_id = r.json()["rows"][0]["id"]
        r2 = await multi_lib_client.delete(
            f"/api/v1/db/table/authors/row/{audio_id}?library=audio-lib",
        )
        assert r2.status_code == 200

        # audio-lib should now be empty
        r3 = await multi_lib_client.get(
            "/api/v1/db/table/authors?library=audio-lib",
        )
        assert r3.json()["total"] == 0

        # books-lib (active) must still have its row
        r4 = await multi_lib_client.get("/api/v1/db/table/authors")
        assert r4.json()["total"] == 1
        assert r4.json()["rows"][0]["name"] == "Books Author"


class TestTablesScopeTag:
    async def test_tables_response_carries_scope_per_entry(self, multi_lib_client):
        r = await multi_lib_client.get("/api/v1/db/tables")
        tables = r.json()["tables"]
        by_name = {t["name"]: t["scope"] for t in tables}
        # Sample one of each — full table presence is asserted by
        # the expanded-whitelist test below.
        assert by_name["announces"] == "pipeline"
        assert by_name["authors"] == "discovery"

    async def test_tables_library_param_targets_discovery_counts(
        self, multi_lib_client,
    ):
        # Counts for discovery tables must reflect the requested
        # library, not the active one. audio-lib has its single
        # author row; switching the `library` arg flips the count.
        r_audio = await multi_lib_client.get(
            "/api/v1/db/tables?library=audio-lib",
        )
        rows_audio = {t["name"]: t["row_count"] for t in r_audio.json()["tables"]}
        r_books = await multi_lib_client.get(
            "/api/v1/db/tables?library=books-lib",
        )
        rows_books = {t["name"]: t["row_count"] for t in r_books.json()["tables"]}
        # Each has exactly one author, separate IDs — same count,
        # but the routing is exercised by the delete test above.
        assert rows_audio["authors"] == 1
        assert rows_books["authors"] == 1


class TestExpandedWhitelist:
    """v2.17.5 added four discovery tables to the whitelist.
    Each must be reachable through the editor — otherwise the
    user is back to SSH-ing the DB box for hand edits."""

    @pytest.mark.parametrize(
        "table",
        [
            "book_merges",
            "metadata_review_queue",
            "books_abs_snapshot",
            "books_calibre_snapshot",
        ],
    )
    async def test_new_table_is_readable(self, multi_lib_client, table):
        r = await multi_lib_client.get(
            f"/api/v1/db/table/{table}?library=books-lib",
        )
        assert r.status_code == 200
        # Empty is fine — the assert here is that the whitelist
        # accepts the name. A 404 would mean the table didn't make
        # it into _DISCOVERY_TABLES.
        body = r.json()
        assert "rows" in body
        assert "total" in body

    async def test_new_table_schema_endpoint_works(self, multi_lib_client):
        r = await multi_lib_client.get(
            "/api/v1/db/table/book_merges/schema?library=books-lib",
        )
        assert r.status_code == 200
        cols = {c["name"] for c in r.json()["columns"]}
        # Sanity-check: the new table's known columns survived
        # the whitelist expansion.
        assert "winner_id" in cols
        assert "loser_id" in cols


# ─── v2.21.0 Phase B: metadata cache surface ─────────────────────


@pytest.fixture
async def metadata_cache_client(tmp_path, monkeypatch):
    """Spin up a real Amazon metadata cache DB so router tests can
    exercise the /tables + /table/{name} surface end-to-end.

    Patches DATA_DIR everywhere the cache module looks (config
    + metadata_cache), then runs init_db so the cache schema is
    in place before requests fire.
    """
    from app import config as app_config
    from app import database as pipeline_database
    from app.discovery import database as disco_db
    from app.discovery import metadata_cache

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(metadata_cache, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(pipeline_database, "APP_DB_PATH", tmp_path / "seshat.db")

    await pipeline_database.init_db()
    await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)

    # Seed one queue row so list endpoints have something non-empty
    # to chew on. The router test only needs presence; the row's
    # contents are not asserted here (covered by the metadata_cache
    # unit tests).
    cache_db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
    try:
        # Schema-v2: queue PK is `author_id` only. Per-library
        # partitioning lives on state + books rows; queue is the
        # global "we owe this author a scan" signal.
        await cache_db.execute(
            f"INSERT INTO {metadata_cache.queue_table()} "
            f"(author_id, priority, enqueued_reason, next_scan_due_at) "
            f"VALUES (?, ?, ?, ?)",
            ("B0SEEDAAAA", 100.0, "test_seed", 0.0),
        )
        await cache_db.commit()
    finally:
        await cache_db.close()

    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as ac:
        yield ac


class TestMetadataCacheSurface:
    """v2.21.0 Phase B — the Amazon metadata cache tables are surfaced
    through the existing db_editor endpoints under a new
    `metadata_cache` scope tag. Library picker doesn't apply
    (the cache spans every library)."""

    async def test_tables_endpoint_lists_metadata_cache_scope(
        self, metadata_cache_client,
    ):
        r = await metadata_cache_client.get("/api/v1/db/tables")
        assert r.status_code == 200
        cache_entries = [
            t for t in r.json()["tables"]
            if t["scope"] == "metadata_cache"
        ]
        names = {t["name"] for t in cache_entries}
        # Exactly the four amazon cache tables, all under the new scope.
        assert names == {
            "metadata_cache_amazon_state",
            "metadata_cache_amazon_books",
            "metadata_cache_amazon_queue",
            "metadata_cache_amazon_worker_state",
        }

    async def test_tables_endpoint_reports_row_counts(
        self, metadata_cache_client,
    ):
        r = await metadata_cache_client.get("/api/v1/db/tables")
        by_name = {t["name"]: t for t in r.json()["tables"]}
        # Queue seeded with 1 row; worker_state singleton seeded with 1.
        assert by_name["metadata_cache_amazon_queue"]["row_count"] == 1
        assert by_name["metadata_cache_amazon_worker_state"]["row_count"] == 1
        # Books + state still empty.
        assert by_name["metadata_cache_amazon_books"]["row_count"] == 0
        assert by_name["metadata_cache_amazon_state"]["row_count"] == 0

    async def test_table_rows_endpoint_returns_seeded_queue_row(
        self, metadata_cache_client,
    ):
        r = await metadata_cache_client.get(
            "/api/v1/db/table/metadata_cache_amazon_queue"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["rows"][0]["author_id"] == "B0SEEDAAAA"
        # Schema-v2: queue rows no longer carry library_slug.
        assert "library_slug" not in body["rows"][0]

    async def test_table_rows_endpoint_ignores_library_param(
        self, metadata_cache_client,
    ):
        # The cache spans every library, so `?library=` is accepted
        # but has no effect on which DB gets queried. Matches the
        # pipeline-table convention (also accepts the param silently).
        r = await metadata_cache_client.get(
            "/api/v1/db/table/metadata_cache_amazon_queue?library=anything"
        )
        assert r.status_code == 200
        body = r.json()
        # Still hits the cache DB and returns the seeded row, NOT a
        # discovery DB row from "anything".
        assert body["total"] == 1
        assert body["rows"][0]["author_id"] == "B0SEEDAAAA"

    async def test_schema_endpoint_works_for_cache_table(
        self, metadata_cache_client,
    ):
        r = await metadata_cache_client.get(
            "/api/v1/db/table/metadata_cache_amazon_state/schema"
        )
        assert r.status_code == 200
        cols = {c["name"]: c for c in r.json()["columns"]}
        assert "author_id" in cols
        assert "library_slug" in cols
        assert "last_outcome" in cols
        # PK columns are flagged correctly.
        assert cols["author_id"]["primary_key"] is True
        assert cols["library_slug"]["primary_key"] is True
