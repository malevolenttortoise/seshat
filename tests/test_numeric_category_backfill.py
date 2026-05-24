"""Tests for the v2.26.1 numeric-MAM-category backfill.

Pre-v2.26.1, `app/discovery/sources/mam.py` read MAM's numeric
`category` field instead of `catname`. The value rode through
Send-to-Pipeline into `grabs.category` as the literal string `"63"`
or `"69"` instead of `"Ebooks - Fantasy"`. Cosmetic but breaks
the audiobook/ebook prefix check in the same source's filter and
shows raw IDs in any UI surface that renders the column.

These tests cover the backfill (idempotent, only touches digit-only
rows, leaves the human form alone) and the forward fix in mam.py.
"""
import aiosqlite
import pytest

from app.database import _backfill_numeric_grab_categories, get_db


class TestBackfillNumericGrabCategories:
    async def test_rewrites_known_numeric_ids(self, temp_db):
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO grabs (mam_torrent_id, torrent_name, "
                "category, state) VALUES "
                "('100', 'Book A', '63', 'completed'),"
                "('101', 'Book B', '69', 'completed'),"
                "('102', 'Book C', '61', 'completed')"
            )
            await db.commit()

            fixed = await _backfill_numeric_grab_categories(db)
            assert fixed == 3

            rows = await (await db.execute(
                "SELECT mam_torrent_id, category FROM grabs ORDER BY id"
            )).fetchall()
            cats = {r["mam_torrent_id"]: r["category"] for r in rows}
            assert cats["100"] == "Ebooks - Fantasy"
            assert cats["101"] == "Ebooks - Science Fiction"
            assert cats["102"] == "Ebooks - Comics/Graphic novels"
        finally:
            await db.close()

    async def test_leaves_human_form_untouched(self, temp_db):
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO grabs (mam_torrent_id, torrent_name, "
                "category, state) VALUES "
                "('200', 'Book A', 'Ebooks - Fantasy', 'completed'),"
                "('201', 'Book B', 'AudioBooks - Sci-Fi', 'completed')"
            )
            await db.commit()

            fixed = await _backfill_numeric_grab_categories(db)
            assert fixed == 0

            rows = await (await db.execute(
                "SELECT category FROM grabs ORDER BY id"
            )).fetchall()
            cats = [r["category"] for r in rows]
            assert cats == ["Ebooks - Fantasy", "AudioBooks - Sci-Fi"]
        finally:
            await db.close()

    async def test_idempotent_second_run_is_noop(self, temp_db):
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO grabs (mam_torrent_id, torrent_name, "
                "category, state) VALUES "
                "('300', 'Book A', '63', 'completed')"
            )
            await db.commit()

            assert await _backfill_numeric_grab_categories(db) == 1
            assert await _backfill_numeric_grab_categories(db) == 0
        finally:
            await db.close()

    async def test_skips_unknown_numeric_ids(self, temp_db):
        # If MAM ever returns an id that's not in our bundled snapshot,
        # leave the row alone rather than blanking it. Operator can
        # then refresh categories or hand-edit.
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO grabs (mam_torrent_id, torrent_name, "
                "category, state) VALUES "
                "('400', 'Book A', '999999', 'completed')"
            )
            await db.commit()

            fixed = await _backfill_numeric_grab_categories(db)
            assert fixed == 0

            row = await (await db.execute(
                "SELECT category FROM grabs WHERE mam_torrent_id = '400'"
            )).fetchone()
            assert row["category"] == "999999"
        finally:
            await db.close()

    async def test_no_grabs_at_all_returns_zero(self, temp_db):
        db = await get_db()
        try:
            assert await _backfill_numeric_grab_categories(db) == 0
        finally:
            await db.close()
