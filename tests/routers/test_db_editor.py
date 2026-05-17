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
