"""
v2.3.4 — sidebar edits populate `books.user_edited_fields`.

When the user changes a field via the book sidebar (PUT
/api/discovery/books/{bid}), the field name is added to
`books.user_edited_fields` (a JSON array). Subsequent Calibre / ABS
syncs read this set and skip auto-flow on those fields, routing the
diff to `metadata_review_queue` instead of overwriting.

Subtleties tested here:
  - The sidebar form re-sends every field on every save, so
    "incoming != current" is the only reliable signal of an actual
    user edit. Tracking by-presence-in-payload would falsely flag
    every field on every save.
  - Tracking is set-union, idempotent on repeat saves.
  - source_url has its own editor + canonical-form rules and is
    explicitly excluded from this tracking.
"""
from __future__ import annotations

import json

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
    from app.discovery.routers import books as books_router

    app = FastAPI()
    app.include_router(books_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _seed_book(**fields) -> int:
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (101, 'A', 'A', ?)",
            (normalize_author_name("A"),),
        )
        cols = ["title", "source"]
        vals_ = ["Original Title", "goodreads"]
        for k, v in fields.items():
            cols.append(k)
            vals_.append(v)
        ph = ",".join("?" * len(cols))
        cur = await db.execute(
            f"INSERT INTO books ({','.join(cols)}) VALUES ({ph})",
            vals_,
        )
        book_id = cur.lastrowid
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (?, 101, 0)", (book_id,),
        )
        await db.commit()
        return book_id
    finally:
        await db.close()


async def _user_edited(book_id: int) -> list[str]:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT user_edited_fields FROM books WHERE id = ?",
            (book_id,),
        )).fetchone()
        raw = row["user_edited_fields"] if row else None
        return json.loads(raw) if raw else []
    finally:
        await db.close()


class TestUserEditedFields:
    async def test_adds_field_when_value_actually_changed(self, client):
        bid = await _seed_book(description="Original")
        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={"description": "User-edited description"},
        )
        assert r.status_code == 200
        assert await _user_edited(bid) == ["description"]

    async def test_skips_field_when_value_unchanged(self, client):
        # The sidebar sends every field on every save — incoming ==
        # current means the user didn't touch it, so don't track.
        bid = await _seed_book(description="Same")
        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={"description": "Same"},
        )
        assert r.status_code == 200
        assert await _user_edited(bid) == []

    async def test_tracks_multiple_fields_in_one_save(self, client):
        bid = await _seed_book(
            description="Original", pub_date="2020-01-01", isbn=None,
        )
        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={
                "description": "New description",
                "pub_date": "1965-04-15",
                "isbn": "9780000000000",
            },
        )
        assert r.status_code == 200
        assert sorted(await _user_edited(bid)) == [
            "description", "isbn", "pub_date",
        ]

    async def test_set_union_across_repeat_saves(self, client):
        bid = await _seed_book(description="d1", isbn=None)
        # First save edits description.
        await client.put(
            f"/api/discovery/books/{bid}",
            json={"description": "d2"},
        )
        # Second save edits isbn.
        await client.put(
            f"/api/discovery/books/{bid}",
            json={"isbn": "9780000000000"},
        )
        assert sorted(await _user_edited(bid)) == ["description", "isbn"]

    async def test_idempotent_on_repeat_save_of_same_field(self, client):
        bid = await _seed_book(description="d1")
        # Edit description.
        await client.put(
            f"/api/discovery/books/{bid}",
            json={"description": "d2"},
        )
        # Save again with the now-current value — nothing should change.
        await client.put(
            f"/api/discovery/books/{bid}",
            json={"description": "d2"},
        )
        assert await _user_edited(bid) == ["description"]

    async def test_source_url_not_tracked(self, client):
        # source_url has its own dedicated editor with canonical-form
        # rules; we don't track it via the v2.3.4 user_edited_fields
        # mechanism because the auto-flow path doesn't read it
        # either. Sidebar saves of source_url should NOT flag it.
        bid = await _seed_book()
        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={"source_url": json.dumps({"goodreads": "https://x/y"})},
        )
        assert r.status_code == 200
        assert await _user_edited(bid) == []

    async def test_form_resending_unchanged_strings_does_not_flag(self, client):
        # v2.3.4.4 type-aware diff regression. The BookSidebar form
        # re-sends every field on every save; for series_index the
        # form sends "1.0" (string) while the DB stores 1.0 (REAL).
        # Pre-v2.3.4.4 these compared as different (str != float) and
        # series_index got falsely flagged as user-edited (Mark's
        # book 68 incident — 5 false-positive flags from the audiobook
        # save). The fix normalizes both sides before comparing.
        from app.discovery.database import get_db
        bid = await _seed_book(description="Original")
        # Set series_index = 1.0 on the seeded book.
        db = await get_db()
        try:
            await db.execute(
                "UPDATE books SET series_index=?, expected_date=? WHERE id=?",
                (1.0, None, bid),
            )
            await db.commit()
        finally:
            await db.close()

        # Form re-sends series_index as the string "1.0" and an empty
        # expected_date — neither is an actual user change.
        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={
                "description": "Original",  # unchanged
                "series_index": "1.0",       # string roundtrip
                "expected_date": "",          # empty string vs stored NULL
            },
        )
        assert r.status_code == 200, r.text
        # No fields should have been flagged.
        assert await _user_edited(bid) == []

    async def test_save_with_mam_url_in_payload_does_not_500(self, client):
        # v2.3.4.2 regression: the inner mam_url branch reassigned
        # `current_row` to a 1-col row (mam_url only), shadowing the
        # outer current_row that the user_edited_fields merge reads
        # from. The BookSidebar form re-sends every field on every
        # save, so any save that included mam_url tripped IndexError
        # on `current_row["user_edited_fields"]`.
        bid = await _seed_book(description="d1")
        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={
                "title": "Updated Title",
                "description": "d1",  # unchanged
                "mam_url": "",         # form always sends this
            },
        )
        assert r.status_code == 200, r.text
        # title flagged as user-edited; description unchanged stays out.
        assert await _user_edited(bid) == ["title"]

    async def test_404_on_unknown_book(self, client):
        r = await client.put(
            "/api/discovery/books/99999",
            json={"description": "x"},
        )
        assert r.status_code == 404


class TestApproveMamFlow:
    """The BookSidebar's "Approve MAM" button on a `possible` match
    re-sends the existing mam_url unchanged and expects the row to
    flip to `mam_status='found'`. v2.2.6's diff-aware mam_url check
    silently broke this path (no diff → no write); restored by an
    explicit "same URL + currently 'possible' = approval" branch."""

    async def _seed_possible(self, mam_url: str = "https://www.myanonamouse.net/t/123") -> int:
        from app.discovery.database import get_db
        from app.metadata.author_names import normalize_author_name
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name, normalized_name) "
                "VALUES (501, 'Ambiguous Author', 'Ambiguous Author', ?)",
                (normalize_author_name("Ambiguous Author"),),
            )
            cur = await db.execute(
                "INSERT INTO books (title, source, mam_url, "
                "mam_status, mam_torrent_id) VALUES "
                "('Possibly Theirs', 'goodreads', ?, 'possible', 123)",
                (mam_url,),
            )
            book_id = cur.lastrowid
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, 501, 0)", (book_id,),
            )
            await db.commit()
            return book_id
        finally:
            await db.close()

    async def _read_status(self, bid: int) -> tuple[str | None, str | None]:
        from app.discovery.database import get_db
        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT mam_url, mam_status FROM books WHERE id=?", (bid,),
            )).fetchone()
            return (row["mam_url"], row["mam_status"])
        finally:
            await db.close()

    async def test_same_url_on_possible_flips_to_found(self, client):
        bid = await self._seed_possible()
        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={"mam_url": "https://www.myanonamouse.net/t/123"},
        )
        assert r.status_code == 200, r.text
        url, status = await self._read_status(bid)
        assert url == "https://www.myanonamouse.net/t/123"
        assert status == "found"

    async def test_same_url_on_found_is_noop(self, client):
        # The approval branch only fires for currently-'possible' rows.
        # Re-saving the URL on an already-'found' row must not write.
        from app.discovery.database import get_db
        bid = await self._seed_possible()
        db = await get_db()
        try:
            await db.execute(
                "UPDATE books SET mam_status='found' WHERE id=?", (bid,),
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={"mam_url": "https://www.myanonamouse.net/t/123"},
        )
        assert r.status_code == 200, r.text
        # Status unchanged. (Body is "no changes" or "ok" depending on
        # whether other fields would have driven a write — here, none.)
        _, status = await self._read_status(bid)
        assert status == "found"

    async def test_same_url_on_not_found_search_url_is_noop(self, client):
        # MAM scan stores a search URL on not_found rows so the user can
        # click through. Re-saving a not_found row's URL (e.g. from the
        # BookSidebar form's full-payload save on an unrelated edit)
        # must NOT flip status to 'found'. The approval branch is gated
        # on currently='possible'.
        bid = await self._seed_possible(
            mam_url="https://www.myanonamouse.net/tor/browse.php?tor%5Btext%5D=foo",
        )
        from app.discovery.database import get_db
        db = await get_db()
        try:
            await db.execute(
                "UPDATE books SET mam_status='not_found' WHERE id=?", (bid,),
            )
            await db.commit()
        finally:
            await db.close()

        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={"mam_url": "https://www.myanonamouse.net/tor/browse.php?tor%5Btext%5D=foo"},
        )
        assert r.status_code == 200, r.text
        _, status = await self._read_status(bid)
        assert status == "not_found"

    async def test_clearing_url_on_possible_marks_not_found(self, client):
        # The "Remove MAM" button — payload is mam_url="" — should
        # still flip status to 'not_found' regardless of current status.
        bid = await self._seed_possible()
        r = await client.put(
            f"/api/discovery/books/{bid}",
            json={"mam_url": ""},
        )
        assert r.status_code == 200, r.text
        url, status = await self._read_status(bid)
        assert url is None
        assert status == "not_found"
