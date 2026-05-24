"""
Tests for app/quality/opportunities.py — replacement-opportunity storage.

Walks the schema path: init DB → record opportunity → list / get /
update / counts. Idempotency (the UNIQUE constraint) is the most
important property to pin because the detector may run twice for the
same grab (linkback hook + pipeline hook).
"""
from __future__ import annotations

import pytest

from app import database
from app.quality.opportunities import (
    get_opportunity,
    list_opportunities,
    opportunity_counts,
    record_opportunity,
    update_status,
)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "test.db")
    await database.init_db()
    conn = await database.get_db()
    try:
        yield conn
    finally:
        await conn.close()


async def _record(db, **overrides):
    """Convenience: insert one opportunity with sensible defaults."""
    defaults = dict(
        candidate_grab_id=101,
        candidate_mam_torrent_id="9001",
        candidate_format="m4b",
        candidate_score=(0, 0, 0),
        owned_library_slug="my-library",
        owned_book_id=42,
        owned_mam_torrent_id="5000",
        owned_format="m4b",
        owned_score=(0, 2, 0),
        media_type="audiobook",
    )
    defaults.update(overrides)
    inserted = await record_opportunity(db, **defaults)
    await db.commit()
    return inserted


# ─── record + idempotency ────────────────────────────────────


async def test_record_inserts_new_row(db):
    inserted = await _record(db)
    assert inserted is True
    rows = await list_opportunities(db)
    assert len(rows) == 1
    assert rows[0]["candidate_grab_id"] == 101
    assert rows[0]["status"] == "detected"
    assert rows[0]["candidate_score"] == [0, 0, 0]
    assert rows[0]["owned_score"] == [0, 2, 0]


async def test_record_is_idempotent_on_same_grab_book(db):
    """The UNIQUE(candidate_grab_id, owned_library_slug, owned_book_id)
    constraint means re-running the detector for the same grab + owned
    book combination silently no-ops. INSERT OR IGNORE returns False."""
    await _record(db)
    second = await _record(db)
    assert second is False
    rows = await list_opportunities(db)
    assert len(rows) == 1


async def test_record_distinct_grabs_against_same_book_both_persist(db):
    """Two different grabs for the same owned book = two opportunities.
    The user may pick which one to enact (or dismiss both)."""
    await _record(db, candidate_grab_id=101, candidate_mam_torrent_id="9001")
    await _record(db, candidate_grab_id=102, candidate_mam_torrent_id="9002")
    rows = await list_opportunities(db)
    assert len(rows) == 2


async def test_record_one_grab_against_multiple_owned_books(db):
    """Bundle torrents can match multiple owned books (one grab, N
    library matches). Each (grab, lib, book) combo is its own row."""
    await _record(db, owned_book_id=42)
    await _record(db, owned_book_id=43)
    await _record(db, owned_book_id=44)
    rows = await list_opportunities(db)
    assert len(rows) == 3


async def test_record_handles_null_owned_score(db):
    """Owned book with no quality metadata yet (pre-feature library)
    still gets an opportunity row when format alone says it's worse."""
    inserted = await record_opportunity(
        db,
        candidate_grab_id=101,
        candidate_mam_torrent_id="9001",
        candidate_format="m4b",
        candidate_score=(0, 0, 0),
        owned_library_slug="my-library",
        owned_book_id=42,
        owned_mam_torrent_id=None,
        owned_format="mp3",
        owned_score=None,
        media_type="audiobook",
    )
    await db.commit()
    assert inserted is True
    rows = await list_opportunities(db)
    assert rows[0]["owned_score"] is None
    assert rows[0]["owned_mam_torrent_id"] is None


# ─── list / get ──────────────────────────────────────────────


async def test_list_filters_by_status(db):
    await _record(db, candidate_grab_id=101)
    await _record(db, candidate_grab_id=102)
    rows = await list_opportunities(db)
    op_id = rows[0]["id"]
    await update_status(db, op_id, status="dismissed", acted_by="user")
    await db.commit()

    detected = await list_opportunities(db, status="detected")
    assert len(detected) == 1
    dismissed = await list_opportunities(db, status="dismissed")
    assert len(dismissed) == 1
    all_rows = await list_opportunities(db, status=None)
    assert len(all_rows) == 2


async def test_list_filters_by_library_slug(db):
    await _record(db, candidate_grab_id=101, owned_library_slug="lib-a")
    await _record(db, candidate_grab_id=102, owned_library_slug="lib-b")
    rows = await list_opportunities(db, library_slug="lib-a")
    assert len(rows) == 1
    assert rows[0]["owned_library_slug"] == "lib-a"


async def test_list_orders_newest_first(db):
    await _record(db, candidate_grab_id=101)
    await _record(db, candidate_grab_id=102)
    await _record(db, candidate_grab_id=103)
    rows = await list_opportunities(db)
    grabs = [r["candidate_grab_id"] for r in rows]
    assert grabs == sorted(grabs, reverse=True)


async def test_get_returns_full_row(db):
    await _record(db)
    rows = await list_opportunities(db)
    op_id = rows[0]["id"]
    got = await get_opportunity(db, op_id)
    assert got is not None
    assert got["candidate_grab_id"] == 101
    assert got["candidate_score"] == [0, 0, 0]


async def test_get_returns_none_for_unknown_id(db):
    got = await get_opportunity(db, 99999)
    assert got is None


# ─── update_status ───────────────────────────────────────────


async def test_update_status_marks_dismissed(db):
    await _record(db)
    op_id = (await list_opportunities(db))[0]["id"]
    changed = await update_status(db, op_id, status="dismissed", acted_by="user")
    await db.commit()
    assert changed is True
    row = await get_opportunity(db, op_id)
    assert row["status"] == "dismissed"
    assert row["acted_by"] == "user"
    assert row["acted_at"] is not None


async def test_update_status_marks_enacted(db):
    """Phase 5b will mark opportunities as 'enacted' when the file
    swap completes. Tested here so the storage layer is ready."""
    await _record(db)
    op_id = (await list_opportunities(db))[0]["id"]
    await update_status(db, op_id, status="enacted", acted_by="auto")
    await db.commit()
    row = await get_opportunity(db, op_id)
    assert row["status"] == "enacted"
    assert row["acted_by"] == "auto"


async def test_update_status_rejects_invalid_value(db):
    await _record(db)
    op_id = (await list_opportunities(db))[0]["id"]
    with pytest.raises(ValueError):
        await update_status(db, op_id, status="bogus")


async def test_update_status_unknown_id_returns_false(db):
    changed = await update_status(db, 99999, status="dismissed")
    await db.commit()
    assert changed is False


# ─── opportunity_counts ──────────────────────────────────────


async def test_counts_buckets_by_status(db):
    await _record(db, candidate_grab_id=101)
    await _record(db, candidate_grab_id=102)
    await _record(db, candidate_grab_id=103)
    rows = await list_opportunities(db)
    await update_status(db, rows[0]["id"], status="dismissed", acted_by="user")
    await update_status(db, rows[1]["id"], status="enacted", acted_by="auto")
    await db.commit()

    counts = await opportunity_counts(db)
    assert counts == {"detected": 1, "enacted": 1, "dismissed": 1}


async def test_counts_returns_zeros_when_empty(db):
    counts = await opportunity_counts(db)
    assert counts == {"detected": 0, "enacted": 0, "dismissed": 0}
