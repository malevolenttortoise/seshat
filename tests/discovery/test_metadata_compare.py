"""
v2.3.4 Compare panel + Metadata Manager queue endpoints.

Covers:
  - GET    /books/{bid}/compare            — three-way side-by-side
  - POST   /books/{bid}/pull               — pull snapshot fields
  - GET    /queue                          — list pending review-queue rows
  - POST   /queue/{qid}/apply              — accept a queue row
  - POST   /queue/{qid}/dismiss            — reject a queue row
  - POST   /queue/bulk                     — bulk apply/dismiss

Snapshot rows are seeded directly via SQL — the real seed paths
(calibre_sync, audiobookshelf_sync) are tested elsewhere; this
file isolates the routing + mutation behavior of the new endpoints.
"""
from __future__ import annotations

import json
import time

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
    from app.discovery.routers import metadata as metadata_router

    app = FastAPI()
    app.include_router(metadata_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ── seed helpers ─────────────────────────────────────────────────────


async def _seed_book(
    book_id: int = 1,
    title: str = "My Book",
    description: str | None = None,
    pub_date: str | None = None,
    isbn: str | None = None,
    user_edited: list[str] | None = None,
):
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (101, 'A', 'A', ?)",
            (normalize_author_name("A"),),
        )
        # user_edited_fields is NOT NULL DEFAULT '[]' — pass an empty
        # array literal when the test doesn't provide one.
        await db.execute(
            "INSERT INTO books (id, title, description, pub_date, "
            "isbn, source, owned, user_edited_fields) "
            "VALUES (?, ?, ?, ?, ?, 'goodreads', 0, ?)",
            (book_id, title, description, pub_date, isbn,
             json.dumps(user_edited or [])),
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (?, 101, 0)", (book_id,),
        )
        await db.commit()
    finally:
        await db.close()


async def _seed_calibre_snapshot(
    book_id: int = 1,
    title: str | None = None,
    description: str | None = None,
    pubdate: str | None = None,
    isbn: str | None = None,
):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO books_calibre_snapshot "
            "(book_id, title, description, pubdate, isbn, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (book_id, title, description, pubdate, isbn, time.time()),
        )
        await db.commit()
    finally:
        await db.close()


async def _seed_abs_snapshot(
    book_id: int = 1,
    title: str | None = None,
    description: str | None = None,
    narrator: str | None = None,
):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO books_abs_snapshot "
            "(book_id, title, description, narrator, synced_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (book_id, title, description, narrator, time.time()),
        )
        await db.commit()
    finally:
        await db.close()


async def _enqueue(book_id: int, field: str, old: str, new: str, source: str = "goodreads") -> int:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO metadata_review_queue "
            "(book_id, field, old_value, new_value, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (book_id, field, old, new, source),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _book_row(book_id: int) -> dict:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT * FROM books WHERE id = ?", (book_id,),
        )).fetchone()
        return dict(row) if row else {}
    finally:
        await db.close()


# ── Compare endpoint ────────────────────────────────────────────────


class TestCompare:
    async def test_returns_seshat_only_when_no_snapshots(self, client):
        await _seed_book(description="Seshat desc")
        r = await client.get("/api/discovery/books/1/compare")
        body = r.json()
        assert body["calibre_synced_at"] is None
        assert body["abs_synced_at"] is None
        # All field rows should have calibre/abs as None.
        for f in body["fields"]:
            assert f["calibre"] is None
            assert f["abs"] is None
            assert f["calibre_diff"] is False
            assert f["abs_diff"] is False

    async def test_diff_flags_when_calibre_differs(self, client):
        await _seed_book(description="Seshat desc")
        await _seed_calibre_snapshot(description="Calibre desc")
        r = await client.get("/api/discovery/books/1/compare")
        body = r.json()
        desc_field = next(f for f in body["fields"] if f["field"] == "description")
        assert desc_field["seshat"] == "Seshat desc"
        assert desc_field["calibre"] == "Calibre desc"
        assert desc_field["calibre_diff"] is True
        assert desc_field["abs_diff"] is False

    async def test_three_way_diff(self, client):
        await _seed_book(title="Seshat", description="Seshat desc")
        await _seed_calibre_snapshot(title="Calibre", description="Same desc")
        await _seed_abs_snapshot(title="ABS", description="Same desc")
        r = await client.get("/api/discovery/books/1/compare")
        body = r.json()
        title_f = next(f for f in body["fields"] if f["field"] == "title")
        desc_f = next(f for f in body["fields"] if f["field"] == "description")
        # All three differ on title.
        assert title_f["calibre_diff"] is True
        assert title_f["abs_diff"] is True
        # Description: Seshat differs from both snapshots (both are "Same desc",
        # Seshat is "Seshat desc"). Both diffs true.
        assert desc_f["calibre_diff"] is True
        assert desc_f["abs_diff"] is True

    async def test_user_edited_flag_surfaces(self, client):
        await _seed_book(description="x", user_edited=["description"])
        r = await client.get("/api/discovery/books/1/compare")
        body = r.json()
        assert body["user_edited_fields"] == ["description"]
        desc_f = next(f for f in body["fields"] if f["field"] == "description")
        assert desc_f["user_edited"] is True

    async def test_empty_rows_are_skipped(self, client):
        # Book has only a title. Description, pub_date, etc. are NULL
        # everywhere — those rows shouldn't render in Compare.
        await _seed_book(title="Just a title")
        r = await client.get("/api/discovery/books/1/compare")
        body = r.json()
        fields_present = {f["field"] for f in body["fields"]}
        assert "title" in fields_present
        assert "description" not in fields_present  # all-empty → skipped

    async def test_404_on_unknown_book(self, client):
        r = await client.get("/api/discovery/books/99/compare")
        assert r.status_code == 404


# ── Pull endpoint ───────────────────────────────────────────────────


class TestPull:
    async def test_pulls_calibre_field_into_books(self, client):
        await _seed_book(description="Old")
        await _seed_calibre_snapshot(description="From Calibre")
        r = await client.post(
            "/api/discovery/books/1/pull",
            json={"source": "calibre", "fields": ["description"]},
        )
        assert r.status_code == 200
        assert (await _book_row(1))["description"] == "From Calibre"

    async def test_pull_clears_field_from_user_edited(self, client):
        # v2.3.5: pull is symmetric with push — both clear UEF on
        # success because both DBs now agree on the value, so there's
        # no edit divergence left to flag. Future Calibre changes to
        # this field will auto-flow on next sync.
        await _seed_book(description="Old", user_edited=["description", "title"])
        await _seed_calibre_snapshot(description="From Calibre")
        await client.post(
            "/api/discovery/books/1/pull",
            json={"source": "calibre", "fields": ["description"]},
        )
        row = await _book_row(1)
        uef = json.loads(row["user_edited_fields"])
        assert "description" not in uef       # cleared by pull
        assert "title" in uef                  # untouched

    async def test_pull_no_op_when_field_not_in_user_edited(self, client):
        # Pulling a field that wasn't user-edited still works (the user
        # is just choosing to align with upstream) — it just doesn't
        # change UEF.
        await _seed_book(description="Old", user_edited=[])
        await _seed_calibre_snapshot(description="From Calibre")
        await client.post(
            "/api/discovery/books/1/pull",
            json={"source": "calibre", "fields": ["description"]},
        )
        row = await _book_row(1)
        assert row["description"] == "From Calibre"
        assert json.loads(row["user_edited_fields"]) == []

    async def test_pending_edits_lists_books_with_uef(self, client, monkeypatch):
        from app import state
        await _seed_book(book_id=1, title="Edited Book",
                         user_edited=["description", "title"])
        await _seed_book(book_id=2, title="Untouched", user_edited=[])
        await _seed_calibre_snapshot(book_id=1, description="C")
        # Cross-library helper iterates state._discovered_libraries —
        # stub a single-library setup that points to the test DB.
        monkeypatch.setattr(state, "_discovered_libraries", [
            {"slug": "test", "name": "Test Library", "content_type": "ebook"},
        ])
        r = await client.get("/api/discovery/pending-edits")
        body = r.json()
        assert body["total"] == 1
        row = body["rows"][0]
        assert row["book_id"] == 1
        assert row["title"] == "Edited Book"
        assert sorted(row["fields"]) == ["description", "title"]
        assert row["has_calibre_snapshot"] is True
        assert row["has_abs_snapshot"] is False
        assert row["library_slug"] == "test"

    async def test_pending_edits_empty_when_no_uef(self, client, monkeypatch):
        from app import state
        await _seed_book(book_id=1, title="x", user_edited=[])
        monkeypatch.setattr(state, "_discovered_libraries", [
            {"slug": "test", "name": "Test", "content_type": "ebook"},
        ])
        r = await client.get("/api/discovery/pending-edits")
        body = r.json()
        assert body["total"] == 0
        assert body["rows"] == []

    async def test_pending_edits_filters_seshat_only_fields(self, client, monkeypatch):
        # `expected_date` and `cover_url` are tracked by PUT /books/{bid}
        # but have no Calibre/ABS counterpart in COMPARE_FIELDS. They
        # legitimately stay in user_edited_fields after a bulk pull (no
        # action can clear them) but they shouldn't surface in the
        # Pending Manual Edits view since the per-row push/pull buttons
        # can't act on them.
        from app import state
        await _seed_book(
            book_id=1, title="Has stranded UEF",
            user_edited=["expected_date", "cover_url"],
        )
        await _seed_book(
            book_id=2, title="Has actionable + stranded",
            user_edited=["description", "expected_date"],
        )
        monkeypatch.setattr(state, "_discovered_libraries", [
            {"slug": "test", "name": "Test", "content_type": "ebook"},
        ])
        r = await client.get("/api/discovery/pending-edits")
        body = r.json()
        # Book 1 (only Seshat-only fields) drops out entirely.
        assert body["total"] == 1
        # Book 2 surfaces, but only `description` shown — `expected_date`
        # is filtered out so the chip list reflects what bulk
        # push/pull can actually act on.
        row = body["rows"][0]
        assert row["book_id"] == 2
        assert row["fields"] == ["description"]

    async def test_pull_all_user_edited_iterates(self, client):
        # Bulk variant: only the intersection of UEF and the source's
        # writable fields should be pulled.
        await _seed_book(
            title="Old t", description="Old d",
            user_edited=["title", "description", "narrator"],
        )
        await _seed_calibre_snapshot(title="C t", description="C d")
        r = await client.post(
            "/api/discovery/books/1/pull",
            json={"source": "calibre", "all_user_edited": True},
        )
        assert r.status_code == 200
        row = await _book_row(1)
        assert row["title"] == "C t"
        assert row["description"] == "C d"
        # narrator stayed in UEF (Calibre snapshot has no narrator
        # column, so the bulk filter didn't pick it).
        uef = json.loads(row["user_edited_fields"])
        assert "narrator" in uef
        assert "title" not in uef
        assert "description" not in uef

    async def test_pulls_multiple_fields(self, client):
        await _seed_book(description="Old", isbn=None)
        await _seed_calibre_snapshot(
            description="C desc", isbn="9780000000001",
        )
        r = await client.post(
            "/api/discovery/books/1/pull",
            json={"source": "calibre",
                  "fields": ["description", "isbn"]},
        )
        assert r.status_code == 200
        row = await _book_row(1)
        assert row["description"] == "C desc"
        assert row["isbn"] == "9780000000001"

    async def test_400_on_invalid_source(self, client):
        await _seed_book()
        r = await client.post(
            "/api/discovery/books/1/pull",
            json={"source": "wrong", "fields": ["description"]},
        )
        assert r.status_code == 400

    async def test_400_on_field_not_pullable(self, client):
        # narrator only exists in ABS snapshot, not Calibre.
        await _seed_book()
        await _seed_calibre_snapshot()
        r = await client.post(
            "/api/discovery/books/1/pull",
            json={"source": "calibre", "fields": ["narrator"]},
        )
        assert r.status_code == 400

    async def test_404_when_snapshot_missing(self, client):
        await _seed_book()
        r = await client.post(
            "/api/discovery/books/1/pull",
            json={"source": "calibre", "fields": ["description"]},
        )
        assert r.status_code == 404


# ── Queue endpoints ─────────────────────────────────────────────────


class TestQueueList:
    async def test_lists_with_book_and_author_joined(self, client):
        await _seed_book(title="Book")
        await _enqueue(1, "description", "old", "new", source="goodreads")
        r = await client.get("/api/discovery/queue")
        body = r.json()
        assert body["total"] == 1
        row = body["rows"][0]
        assert row["book_title"] == "Book"
        assert row["author_name"] == "A"
        assert row["field"] == "description"

    async def test_filters_by_source(self, client):
        await _seed_book()
        await _enqueue(1, "description", "x", "y", source="goodreads")
        await _enqueue(1, "isbn", "x", "9780000000000", source="hardcover")
        r = await client.get("/api/discovery/queue?source=hardcover")
        body = r.json()
        assert body["total"] == 1
        assert body["rows"][0]["source"] == "hardcover"

    async def test_paginates(self, client):
        await _seed_book()
        for i in range(15):
            await _enqueue(1, f"f{i}", "x", str(i), source="goodreads")
        r = await client.get("/api/discovery/queue?limit=5&offset=5")
        body = r.json()
        assert body["total"] == 15
        assert len(body["rows"]) == 5


class TestQueueApply:
    async def test_applies_writes_and_deletes_row(self, client):
        await _seed_book(description="Old")
        qid = await _enqueue(1, "description", "Old", "Proposed")
        r = await client.post(f"/api/discovery/queue/{qid}/apply")
        assert r.status_code == 200
        # Row written, queue empty.
        assert (await _book_row(1))["description"] == "Proposed"
        body = (await client.get("/api/discovery/queue")).json()
        assert body["total"] == 0

    async def test_apply_marks_field_user_edited(self, client):
        await _seed_book(description="Old")
        qid = await _enqueue(1, "description", "Old", "Proposed")
        await client.post(f"/api/discovery/queue/{qid}/apply")
        uef = json.loads((await _book_row(1))["user_edited_fields"])
        assert "description" in uef

    async def test_apply_coerces_numeric_field(self, client):
        await _seed_book()
        qid = await _enqueue(1, "series_index", "1.0", "3.0")
        r = await client.post(f"/api/discovery/queue/{qid}/apply")
        assert r.status_code == 200
        assert (await _book_row(1))["series_index"] == 3.0

    async def test_apply_404_on_unknown_id(self, client):
        r = await client.post("/api/discovery/queue/9999/apply")
        assert r.status_code == 404


class TestQueueDismiss:
    async def test_deletes_without_writing(self, client):
        await _seed_book(description="Original")
        qid = await _enqueue(1, "description", "Original", "Proposed")
        r = await client.post(f"/api/discovery/queue/{qid}/dismiss")
        assert r.status_code == 200
        # Books table untouched.
        assert (await _book_row(1))["description"] == "Original"
        # Queue empty.
        body = (await client.get("/api/discovery/queue")).json()
        assert body["total"] == 0

    async def test_404_on_unknown_id(self, client):
        r = await client.post("/api/discovery/queue/9999/dismiss")
        assert r.status_code == 404


class TestQueueBulk:
    async def test_bulk_apply(self, client):
        await _seed_book(description=None, isbn=None)
        q1 = await _enqueue(1, "description", "x", "Desc")
        q2 = await _enqueue(1, "isbn", None, "9780000000000")
        r = await client.post(
            "/api/discovery/queue/bulk",
            json={"action": "apply", "ids": [q1, q2]},
        )
        body = r.json()
        assert body["succeeded"] == 2
        row = await _book_row(1)
        assert row["description"] == "Desc"
        assert row["isbn"] == "9780000000000"

    async def test_bulk_dismiss(self, client):
        await _seed_book()
        q1 = await _enqueue(1, "description", "x", "y")
        q2 = await _enqueue(1, "isbn", "x", "y")
        r = await client.post(
            "/api/discovery/queue/bulk",
            json={"action": "dismiss", "ids": [q1, q2]},
        )
        body = r.json()
        assert body["succeeded"] == 2
        list_body = (await client.get("/api/discovery/queue")).json()
        assert list_body["total"] == 0

    async def test_bulk_partial_failure_reports_per_id(self, client):
        await _seed_book()
        valid = await _enqueue(1, "description", "x", "y")
        r = await client.post(
            "/api/discovery/queue/bulk",
            json={"action": "dismiss", "ids": [valid, 9999]},
        )
        body = r.json()
        assert body["succeeded"] == 1
        # Per-id results so the caller can resolve.
        statuses = {row["id"]: row["ok"] for row in body["results"]}
        assert statuses == {valid: True, 9999: False}


# ── v2.3.4.4: series_name on Compare + pull resolves series_id ───────


class TestCompareSeries:
    async def test_compare_surfaces_series_name(self, client):
        from app.discovery.database import get_db
        await _seed_book()
        await _seed_calibre_snapshot()
        # Add Calibre series_name to the snapshot directly.
        db = await get_db()
        try:
            await db.execute(
                "UPDATE books_calibre_snapshot SET series_name=?, series_index=? "
                "WHERE book_id=1", ("Horizon", 1.0),
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.get("/api/discovery/books/1/compare")
        body = r.json()
        # synthetic series_name row should appear with calibre value.
        sf = next(f for f in body["fields"] if f["field"] == "series_name")
        assert sf["seshat"] is None  # book has no series_id yet
        assert sf["calibre"] == "Horizon"
        assert sf["calibre_diff"] is True
        assert sf["label"] == "Series"

    async def test_pull_series_name_creates_series_and_links_book(self, client):
        from app.discovery.database import get_db
        await _seed_book()
        await _seed_calibre_snapshot()
        db = await get_db()
        try:
            await db.execute(
                "UPDATE books_calibre_snapshot SET series_name=? WHERE book_id=1",
                ("Horizon",),
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.post(
            "/api/discovery/books/1/pull",
            json={"source": "calibre", "fields": ["series_name"]},
        )
        assert r.status_code == 200, r.text

        # Series row was created and book is linked.
        row = await _book_row(1)
        assert row["series_id"] is not None
        db = await get_db()
        try:
            srow = await (await db.execute(
                "SELECT name, author_id FROM series WHERE id=?",
                (row["series_id"],),
            )).fetchone()
        finally:
            await db.close()
        assert srow["name"] == "Horizon"
        assert srow["author_id"] == 101  # the seeded author

    async def test_pull_series_name_reuses_existing_series_row(self, client):
        from app.discovery.database import get_db
        await _seed_book()
        await _seed_calibre_snapshot()
        db = await get_db()
        try:
            await db.execute(
                "UPDATE books_calibre_snapshot SET series_name=? WHERE book_id=1",
                ("Horizon",),
            )
            # Pre-existing series row for the same author + name.
            cur = await db.execute(
                "INSERT INTO series (name, author_id) VALUES (?, ?)",
                ("Horizon", 101),
            )
            existing_sid = cur.lastrowid
            await db.commit()
        finally:
            await db.close()

        await client.post(
            "/api/discovery/books/1/pull",
            json={"source": "calibre", "fields": ["series_name"]},
        )
        row = await _book_row(1)
        assert row["series_id"] == existing_sid

    async def test_pull_empty_series_clears_link(self, client):
        # Snapshot has no series → pulling sets books.series_id = NULL.
        from app.discovery.database import get_db
        await _seed_book()
        await _seed_calibre_snapshot()
        db = await get_db()
        try:
            cur = await db.execute(
                "INSERT INTO series (name, author_id) VALUES (?, ?)",
                ("Old Series", 101),
            )
            old_sid = cur.lastrowid
            await db.execute(
                "UPDATE books SET series_id=? WHERE id=1", (old_sid,),
            )
            # snapshot has no series_name (default NULL).
            await db.commit()
        finally:
            await db.close()

        await client.post(
            "/api/discovery/books/1/pull",
            json={"source": "calibre", "fields": ["series_name"]},
        )
        row = await _book_row(1)
        assert row["series_id"] is None
