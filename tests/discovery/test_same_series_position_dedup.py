"""
Tests for same-series-position book dedup — Bug C from the Tier 2
UAT follow-ups.

Covers both the migration helper (`_dedupe_same_series_position`
in `app.discovery.database`) and the insert-time prevention added
to `_merge_result` in `app.discovery.lookup`.

The Remnant case: Mark owns "Remnant II" at series_index=2 in the
"Remnant" series. A source reported "Remnant Book 2" also at
series_index=2. Before the fix, fuzzy title match didn't fire
(titles too different) so a second book row was inserted at the
same series position. After the fix, the same-(series_id,
series_index) prefilter catches it.
"""
from __future__ import annotations

import pytest


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


async def _insert_author(name: str) -> int:
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name) "
            "VALUES (?, ?, ?)",
            (name, name, normalize_author_name(name)),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _insert_series(name: str, author_id: int) -> int:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO series (name, author_id) VALUES (?, ?)",
            (name, author_id),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _insert_book(
    title: str,
    author_id: int,
    *,
    series_id: int | None = None,
    series_index: float | None = None,
    owned: int = 0,
    source: str = "hardcover",
) -> int:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO books (title, author_id, series_id, series_index, "
            "source, owned) VALUES (?, ?, ?, ?, ?, ?)",
            (title, author_id, series_id, series_index, source, owned),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _book_rows(author_id: int) -> list[dict]:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT id, title, series_id, series_index, owned FROM books "
            "WHERE author_id = ? ORDER BY series_index NULLS LAST, id",
            (author_id,),
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ─── Migration: _dedupe_same_series_position ──────────────────

class TestMigration:
    async def test_remnant_case_owned_beats_new(self, discovery_db):
        """
        Mirror Mark's exact Remnant state: OWNED "Remnant II" + NEW
        "Remnant Book 2" both at series_index=2. Migration keeps the
        OWNED roman-numeral row.
        """
        from app.discovery.database import _dedupe_same_series_position, get_db

        author_id = await _insert_author("Randi Darren")
        series_id = await _insert_series("Remnant", author_id)
        owned_id = await _insert_book(
            "Remnant II", author_id,
            series_id=series_id, series_index=2.0, owned=1, source="calibre",
        )
        await _insert_book(
            "Remnant Book 2", author_id,
            series_id=series_id, series_index=2.0, owned=0, source="hardcover",
        )

        db = await get_db()
        try:
            deleted = await _dedupe_same_series_position(db)
        finally:
            await db.close()

        assert deleted == 1
        rows = await _book_rows(author_id)
        assert len(rows) == 1
        assert rows[0]["id"] == owned_id
        assert rows[0]["title"] == "Remnant II"

    async def test_book_n_suffix_loses_when_both_same_owned_flag(
        self, discovery_db,
    ):
        """
        Tiebreaker after OWNED: title without "Book N" suffix wins.
        Both NEW here, so OWNED flag ties — the non-"Book N" title
        is the canonical choice.
        """
        from app.discovery.database import _dedupe_same_series_position, get_db

        author_id = await _insert_author("Randi Darren")
        series_id = await _insert_series("Remnant", author_id)
        canonical_id = await _insert_book(
            "Remnant II", author_id,
            series_id=series_id, series_index=2.0, owned=0, source="hardcover",
        )
        await _insert_book(
            "Remnant Book 2", author_id,
            series_id=series_id, series_index=2.0, owned=0, source="ibdb",
        )

        db = await get_db()
        try:
            await _dedupe_same_series_position(db)
        finally:
            await db.close()

        rows = await _book_rows(author_id)
        assert len(rows) == 1
        assert rows[0]["id"] == canonical_id

    async def test_distinct_series_positions_untouched(self, discovery_db):
        """Sanity: books at different series_index values are not touched."""
        from app.discovery.database import _dedupe_same_series_position, get_db

        author_id = await _insert_author("Randi Darren")
        series_id = await _insert_series("Remnant", author_id)
        await _insert_book(
            "Remnant", author_id,
            series_id=series_id, series_index=1.0, owned=1,
        )
        await _insert_book(
            "Remnant II", author_id,
            series_id=series_id, series_index=2.0, owned=1,
        )
        await _insert_book(
            "Remnant III", author_id,
            series_id=series_id, series_index=3.0, owned=1,
        )

        db = await get_db()
        try:
            deleted = await _dedupe_same_series_position(db)
        finally:
            await db.close()

        assert deleted == 0
        rows = await _book_rows(author_id)
        assert len(rows) == 3

    async def test_books_without_series_index_untouched(self, discovery_db):
        """Books missing series_index or series_id are not affected."""
        from app.discovery.database import _dedupe_same_series_position, get_db

        author_id = await _insert_author("Randi Darren")
        # Two standalones (no series) with identical titles — NOT our
        # concern here. Bug C only collapses same-series-position.
        await _insert_book("Privateer's Commission", author_id, owned=1)
        await _insert_book("Privateer's Commission", author_id, owned=0)

        db = await get_db()
        try:
            deleted = await _dedupe_same_series_position(db)
        finally:
            await db.close()

        assert deleted == 0
        rows = await _book_rows(author_id)
        assert len(rows) == 2

    async def test_suggestions_cascade_on_delete(self, discovery_db):
        """
        book_series_suggestions has ON DELETE CASCADE on book_id — the
        loser's suggestion row should auto-drop with the book.
        """
        from app.discovery.database import _dedupe_same_series_position, get_db

        author_id = await _insert_author("Randi Darren")
        series_id = await _insert_series("Remnant", author_id)
        await _insert_book(
            "Remnant II", author_id,
            series_id=series_id, series_index=2.0, owned=1, source="calibre",
        )
        loser_id = await _insert_book(
            "Remnant Book 2", author_id,
            series_id=series_id, series_index=2.0, owned=0, source="hardcover",
        )

        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO book_series_suggestions "
                "(book_id, suggested_series_name, suggested_series_index, "
                "sources_agreeing) VALUES (?, ?, ?, ?)",
                (loser_id, "Remnant", 2.0, "hardcover"),
            )
            await db.commit()
            await _dedupe_same_series_position(db)
            sug = await (await db.execute(
                "SELECT COUNT(*) c FROM book_series_suggestions"
            )).fetchone()
        finally:
            await db.close()
        assert sug["c"] == 0

    async def test_idempotent(self, discovery_db):
        """Second run finds nothing to do."""
        from app.discovery.database import _dedupe_same_series_position, get_db

        author_id = await _insert_author("Randi Darren")
        series_id = await _insert_series("Remnant", author_id)
        await _insert_book(
            "Remnant II", author_id,
            series_id=series_id, series_index=2.0, owned=1,
        )
        await _insert_book(
            "Remnant Book 2", author_id,
            series_id=series_id, series_index=2.0, owned=0,
        )

        db = await get_db()
        try:
            first = await _dedupe_same_series_position(db)
            second = await _dedupe_same_series_position(db)
        finally:
            await db.close()
        assert first == 1
        assert second == 0


# ─── Insert-time prevention via _merge_result ─────────────────

class TestInsertTimePrevention:
    async def test_remnant_incoming_matches_existing_by_position(
        self, discovery_db,
    ):
        """
        Simulate the steady-state scenario AFTER the migration runs:
        "Remnant II" is the sole row. Next sync brings in "Remnant
        Book 2" at series_index=2. Without the fix it would insert
        as a duplicate; with the fix the (series_id, series_index)
        prefilter matches it to the existing "Remnant II" row and
        the UPDATE path fires instead.
        """
        from app.discovery.lookup import _merge_result
        from app.discovery.sources.base import (
            AuthorResult, BookResult, SeriesResult,
        )

        author_id = await _insert_author("Randi Darren")
        series_id = await _insert_series("Remnant", author_id)
        existing_id = await _insert_book(
            "Remnant II", author_id,
            series_id=series_id, series_index=2.0, owned=1, source="calibre",
        )

        result = AuthorResult(
            name="Randi Darren",
            external_id="darren-1",
            series=[
                SeriesResult(
                    name="Remnant",
                    books=[
                        BookResult(
                            title="Remnant Book 2",
                            series_name="Remnant",
                            series_index=2.0,
                            source="hardcover",
                        ),
                    ],
                ),
            ],
        )

        new, _ = await _merge_result(
            author_id=author_id,
            result=result,
            source_name="hardcover",
            languages=["English"],
        )

        rows = await _book_rows(author_id)
        # Still one book, title unchanged (OWNED/Calibre title is
        # locked per the _update_existing rules).
        assert len(rows) == 1
        assert rows[0]["id"] == existing_id
        assert rows[0]["title"] == "Remnant II"
        assert new == 0

    async def test_distinct_position_still_inserts(self, discovery_db):
        """
        Incoming book at a series position not yet occupied must still
        insert — the prefilter is keyed on (series_id, series_index)
        so a novel index sails through.
        """
        from app.discovery.lookup import _merge_result
        from app.discovery.sources.base import (
            AuthorResult, BookResult, SeriesResult,
        )

        author_id = await _insert_author("Randi Darren")
        series_id = await _insert_series("Remnant", author_id)
        await _insert_book(
            "Remnant II", author_id,
            series_id=series_id, series_index=2.0, owned=1,
        )

        result = AuthorResult(
            name="Randi Darren",
            external_id="darren-1",
            series=[
                SeriesResult(
                    name="Remnant",
                    books=[
                        BookResult(
                            title="Remnant IV",
                            series_name="Remnant",
                            series_index=4.0,
                            source="hardcover",
                        ),
                    ],
                ),
            ],
        )

        new, _ = await _merge_result(
            author_id=author_id,
            result=result,
            source_name="hardcover",
            languages=["English"],
        )

        rows = await _book_rows(author_id)
        titles = sorted(r["title"] for r in rows)
        assert titles == ["Remnant II", "Remnant IV"]
        assert new == 1
