"""
Tests for app/quality/enactments.py — replacement enactment audit storage.

Three lifecycle paths: insert (record_enactment), mark_failed (rollback),
mark_restored (user restore). Plus the lookup helpers
(get_enactment, latest_active_enactment, list_enactments).
"""
from __future__ import annotations

import pytest

from app import database
from app.quality.enactments import (
    get_enactment,
    latest_active_enactment,
    list_enactments,
    mark_enactment_failed,
    mark_enactment_restored,
    record_enactment,
)
from app.quality.opportunities import record_opportunity


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "test.db")
    await database.init_db()
    conn = await database.get_db()
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_opportunity(db, **overrides) -> int:
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
    await record_opportunity(db, **defaults)
    await db.commit()
    cur = await db.execute(
        "SELECT id FROM replacement_opportunities ORDER BY id DESC LIMIT 1",
    )
    row = await cur.fetchone()
    return int(row[0])


async def _record(db, opportunity_id, **overrides):
    defaults = dict(
        opportunity_id=opportunity_id,
        acted_by="user",
        library_slug="my-library",
        owned_book_id_before=42,
        owned_path_before="/lib/Author/Title (42)",
        owned_path_after="/lib/.seshat-replaced/20260524-180000/Title (42)",
        owned_size_bytes=12345,
        candidate_path=None,
        candidate_size_bytes=None,
        sink_result=None,
    )
    defaults.update(overrides)
    new_id = await record_enactment(db, **defaults)
    await db.commit()
    return new_id


class TestRecordEnactment:
    async def test_inserts_row(self, db):
        opp_id = await _seed_opportunity(db)
        en_id = await _record(db, opp_id)
        assert en_id > 0
        row = await get_enactment(db, en_id)
        assert row is not None
        assert row["opportunity_id"] == opp_id
        assert row["library_slug"] == "my-library"
        assert row["acted_by"] == "user"
        assert row["failed_at"] is None
        assert row["restored_at"] is None
        assert row["enacted_at"] is not None  # stamped

    async def test_multiple_enactments_per_opportunity(self, db):
        """Restoring + re-enacting the same opportunity produces two
        audit rows — history survives the cycle."""
        opp_id = await _seed_opportunity(db)
        first = await _record(db, opp_id)
        # Mark first as restored.
        await mark_enactment_restored(db, first, restored_by="user")
        await db.commit()
        # Record a second enact.
        second = await _record(db, opp_id)
        assert second != first
        rows = await list_enactments(db, opportunity_id=opp_id)
        assert len(rows) == 2


class TestMarkFailed:
    async def test_stamps_failed_at_and_reason(self, db):
        opp_id = await _seed_opportunity(db)
        en_id = await _record(db, opp_id)

        ok = await mark_enactment_failed(
            db, en_id, reason="sink calibre returned exit 1",
        )
        await db.commit()
        assert ok is True

        row = await get_enactment(db, en_id)
        assert row["failed_at"] is not None
        assert "exit 1" in (row["failed_reason"] or "")

    async def test_returns_false_on_missing_row(self, db):
        ok = await mark_enactment_failed(db, 9999, reason="anything")
        assert ok is False


class TestMarkRestored:
    async def test_stamps_restored_at_and_user(self, db):
        opp_id = await _seed_opportunity(db)
        en_id = await _record(db, opp_id)

        ok = await mark_enactment_restored(db, en_id, restored_by="alice")
        await db.commit()
        assert ok is True

        row = await get_enactment(db, en_id)
        assert row["restored_at"] is not None
        assert row["restored_by"] == "alice"


class TestLatestActiveEnactment:
    async def test_returns_only_unfailed_unrestored(self, db):
        """Active = failed_at IS NULL AND restored_at IS NULL. A
        failed-and-rolled-back enactment + a restored one should NOT
        be returned even if they're newer than the active row."""
        opp_id = await _seed_opportunity(db)

        # First attempt: failed.
        failed = await _record(db, opp_id)
        await mark_enactment_failed(db, failed, reason="sink down")
        await db.commit()

        # Second attempt: succeeded (active).
        active = await _record(db, opp_id)

        # Third attempt: succeeded then restored.
        restored = await _record(db, opp_id)
        await mark_enactment_restored(db, restored, restored_by="user")
        await db.commit()

        row = await latest_active_enactment(db, opp_id)
        assert row is not None
        assert row["id"] == active

    async def test_returns_none_when_only_failed_and_restored_exist(self, db):
        opp_id = await _seed_opportunity(db)
        en_id = await _record(db, opp_id)
        await mark_enactment_failed(db, en_id, reason="x")
        await db.commit()
        row = await latest_active_enactment(db, opp_id)
        assert row is None


class TestListEnactments:
    async def test_filters_by_opportunity_id(self, db):
        opp_a = await _seed_opportunity(db, owned_book_id=1)
        opp_b = await _seed_opportunity(db, candidate_grab_id=102, owned_book_id=2)

        await _record(db, opp_a)
        await _record(db, opp_b)
        await _record(db, opp_b)

        a_rows = await list_enactments(db, opportunity_id=opp_a)
        b_rows = await list_enactments(db, opportunity_id=opp_b)
        assert len(a_rows) == 1
        assert len(b_rows) == 2

    async def test_filters_by_library_slug(self, db):
        opp_a = await _seed_opportunity(db, owned_library_slug="lib-a")
        opp_b = await _seed_opportunity(
            db, candidate_grab_id=102, owned_library_slug="lib-b",
        )
        await _record(db, opp_a, library_slug="lib-a")
        await _record(db, opp_b, library_slug="lib-b")

        rows = await list_enactments(db, library_slug="lib-a")
        assert len(rows) == 1
        assert rows[0]["library_slug"] == "lib-a"

    async def test_newest_first_ordering(self, db):
        opp = await _seed_opportunity(db)
        first = await _record(db, opp)
        second = await _record(db, opp)
        rows = await list_enactments(db)
        # Both rows present; newer one first.
        assert rows[0]["id"] == second
        assert rows[1]["id"] == first
