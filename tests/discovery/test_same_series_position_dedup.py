"""
Tests for same-series-position book dedup — Bug C from the Tier 2
UAT follow-ups.

Covers the start-up check (`_check_series_positions` in
`app.discovery.database`, which no longer deletes: G215) and the
scans' title→series passes, which fold two books on one series number
only when the title identity calls them one book (G216).

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
            "INSERT INTO books (title, series_id, series_index, "
            "source, owned) VALUES (?, ?, ?, ?, ?)",
            (title, series_id, series_index, source, owned),
        )
        # v3.0.0 Phase 9: authorship lives in book_authors, not books.
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (?, ?, 0)",
            (cur.lastrowid, author_id),
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
            "SELECT b.id, b.title, b.series_id, b.series_index, b.owned "
            "FROM books b "
            "JOIN book_authors ba ON ba.book_id = b.id AND ba.position = 0 "
            "WHERE ba.author_id = ? ORDER BY b.series_index NULLS LAST, b.id",
            (author_id,),
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ─── Startup: _check_series_positions (G215) ──────────────────


async def _check():
    from app.discovery.database import _check_series_positions, get_db
    db = await get_db()
    try:
        return await _check_series_positions(db)
    finally:
        await db.close()


class TestStartupCheck:
    """A taken series number isn't a match: the start-up pass that
    deleted the unowned book on it is retired (G215). On the reference
    library it would have deleted "The Forge of God" for its Spanish
    edition at that number."""

    async def test_books_on_a_taken_number_all_stay(self, discovery_db):
        author_id = await _insert_author("Greg Bear")
        series_id = await _insert_series("The Forge of God", author_id)
        spanish = await _insert_book("La Fragua de Dios", author_id, series_id=series_id, series_index=1.0)
        english = await _insert_book("The Forge of God", author_id, series_id=series_id, series_index=1.0)
        owned = await _insert_book("Anvil of Stars", author_id, series_id=series_id, series_index=2.0,
                                   owned=1, source="calibre")
        other = await _insert_book("Anvil of Stars: Book 2", author_id, series_id=series_id, series_index=2.0)
        assert await _check() == 0
        assert sorted(r["id"] for r in await _book_rows(author_id)) == sorted([spanish, english, owned, other])

    async def test_a_restart_deletes_nothing(self, discovery_db):
        from app.discovery import database as disco_db
        author_id = await _insert_author("Randi Darren")
        series_id = await _insert_series("Remnant", author_id)
        await _insert_book("Remnant II", author_id, series_id=series_id, series_index=2.0, owned=1, source="calibre")
        await _insert_book("Remnant Book 2", author_id, series_id=series_id, series_index=2.0)
        await disco_db.init_db("test")
        assert len(await _book_rows(author_id)) == 2

    async def test_a_number_recovered_from_a_title_never_evicts_a_book(self, discovery_db):
        from app.discovery import database as disco_db
        author_id = await _insert_author("Kurtis Eckstein")
        series_id = await _insert_series("Paths of Akashic", author_id)
        held = await _insert_book("Paths of Akashic 5: The Expanse", author_id, series_id=series_id,
                                  series_index=5.0)
        taken = await _insert_book("Bloodprice (Paths of Akashic #5)", author_id, series_id=series_id)
        free = await _insert_book("Ashfall (Paths of Akashic #6)", author_id, series_id=series_id)
        await disco_db.init_db("test")
        rows = {r["id"]: r["series_index"] for r in await _book_rows(author_id)}
        assert rows == {held: 5.0, taken: None, free: 6.0}

    async def test_a_blank_series_number_is_scrubbed(self, discovery_db):
        from app.discovery.database import get_db
        author_id = await _insert_author("Randi Darren")
        series_id = await _insert_series("Remnant", author_id)
        bid = await _insert_book("Remnant", author_id, series_id=series_id, series_index=None)
        db = await get_db()
        try:
            await db.execute("UPDATE books SET series_index = '' WHERE id = ?", (bid,))
            await db.commit()
        finally:
            await db.close()
        await _check()
        assert (await _book_rows(author_id))[0]["series_index"] is None

    async def test_two_owned_books_on_one_number_are_reported(self, discovery_db, caplog):
        # The signal that Calibre's numbering needs a fix (the Witcher case).
        import logging
        author_id = await _insert_author("Andrzej Sapkowski")
        series_id = await _insert_series("The Witcher", author_id)
        await _insert_book("The Last Wish", author_id, series_id=series_id, series_index=1.0, owned=1,
                           source="calibre")
        await _insert_book("Blood of Elves", author_id, series_id=series_id, series_index=1.0, owned=1,
                           source="calibre")
        await _insert_book("Sword of Destiny", author_id, series_id=series_id, series_index=2.0, owned=1,
                           source="calibre")
        await _insert_book("The Witcher 2", author_id, series_id=series_id, series_index=2.0)
        with caplog.at_level(logging.WARNING, logger="seshat.discovery.database"):
            await _check()
        warned = [r.message for r in caplog.records if "Series-position collision" in r.message]
        assert len(warned) == 1 and "kept all 2" in warned[0] and "Blood of Elves" in warned[0]


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

    async def test_title_series_pass_dedups_against_owned(self, discovery_db):
        """
        ibdb / Hardcover sometimes return a series book as a STANDALONE
        (no series_index). `_merge_result` inserts it plainly, then
        `_title_to_series_pass` assigns series+index from the title
        pattern. Before this fix, that UPDATE could stick an incoming
        "Remnant Book 2" at the same (Remnant, 2) slot as the user's
        existing OWNED "Remnant II" — two rows at one series position.

        After: the pass detects the collision and DELETEs the
        incoming standalone instead of linking it.
        """
        from app.discovery.lookup import _title_to_series_pass

        author_id = await _insert_author("Randi Darren")
        series_id = await _insert_series("Remnant", author_id)
        owned_id = await _insert_book(
            "Remnant II", author_id,
            series_id=series_id, series_index=2.0, owned=1, source="calibre",
        )
        # Simulate ibdb's post-insert standalone (no series_id/index yet).
        standalone_id = await _insert_book(
            "Remnant Book 2", author_id,
            series_id=None, series_index=None, owned=0, source="ibdb",
        )

        await _title_to_series_pass(author_id)

        rows = await _book_rows(author_id)
        # Only the OWNED roman-numeral row survives at (series_id, 2).
        assert len(rows) == 1
        assert rows[0]["id"] == owned_id
        assert rows[0]["title"] == "Remnant II"
        # Confirm the incoming standalone row is actually gone.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            gone = await (await db.execute(
                "SELECT 1 FROM books WHERE id = ?", (standalone_id,),
            )).fetchone()
        finally:
            await db.close()
        assert gone is None

    async def test_title_series_pass_prefers_non_book_n_title(
        self, discovery_db,
    ):
        """
        When neither collision candidate is OWNED, the canonical
        (non-"Book N") title wins. Uses `_RX_TITLE_SERIES_IDX`-parsable
        titles on both sides so the collision path actually fires
        (roman numerals aren't extracted by that regex, so a
        "Remnant II" standalone wouldn't get a series_index and
        wouldn't collide).
        """
        from app.discovery.lookup import _title_to_series_pass

        author_id = await _insert_author("Randi Darren")
        series_id = await _insert_series("Remnant", author_id)
        # Existing non-canonical ("Book N") already at index 2.
        await _insert_book(
            "Remnant Book 2", author_id,
            series_id=series_id, series_index=2.0, owned=0, source="ibdb",
        )
        # Incoming standalone with a trailing-number pattern — regex
        # extracts index 2 → collision → non-"Book N" title wins.
        incoming_id = await _insert_book(
            "Remnant 2", author_id,
            series_id=None, series_index=None, owned=0, source="hardcover",
        )

        await _title_to_series_pass(author_id)

        rows = await _book_rows(author_id)
        assert len(rows) == 1
        assert rows[0]["id"] == incoming_id
        assert rows[0]["title"] == "Remnant 2"
        assert rows[0]["series_index"] == 2.0

    async def test_title_series_pass_finds_cross_author_pen_name_series(
        self, discovery_db,
    ):
        """
        Pen-name regression: Darren (id=49) and Arand (id=19) are
        linked. `_ensure_series` resolves "Incubus Inc." globally by
        name, so ALL of Darren's Incubus Inc. books end up pointing
        to Arand's series row (series_id=X, author_id=19) rather
        than their own.

        Before the fix, `_title_to_series_pass(author_id=49)` queried
        `series WHERE author_id = 49` and found ZERO series for
        Darren — so an ibdb-sourced standalone "Incubus Inc. Book 2"
        never got dedup'd against the Darren-scoped "Incubus Inc. II"
        sitting at the same series position in Arand's series row.

        The fix queries series by "any series my books reference"
        which correctly picks up cross-author series shared via
        pen-name links.
        """
        from app.discovery.lookup import _title_to_series_pass

        # Arand owns the series row.
        arand_id = await _insert_author("William D. Arand")
        shared_series_id = await _insert_series("Incubus Inc.", arand_id)

        # Darren links his books to Arand's series (pen-name
        # cross-assignment — the real DB situation).
        darren_id = await _insert_author("Randi Darren")
        canonical_id = await _insert_book(
            "Incubus Inc. II", darren_id,
            series_id=shared_series_id, series_index=2.0,
            owned=0, source="goodreads",
        )
        # ibdb-sourced standalone for the same series position.
        await _insert_book(
            "Incubus Inc. Book 2", darren_id,
            series_id=None, series_index=None,
            owned=0, source="ibdb",
        )

        await _title_to_series_pass(darren_id)

        rows = await _book_rows(darren_id)
        assert len(rows) == 1, f"expected dedup, got {rows!r}"
        assert rows[0]["id"] == canonical_id
        assert rows[0]["title"] == "Incubus Inc. II"

    async def test_title_series_pass_no_collision_links_normally(
        self, discovery_db,
    ):
        """
        Sanity: when no existing row occupies the target (series_id,
        series_index), the link proceeds normally — the fix mustn't
        break the non-collision path.
        """
        from app.discovery.lookup import _title_to_series_pass

        author_id = await _insert_author("Randi Darren")
        series_id = await _insert_series("Remnant", author_id)
        # Existing book at #1 only.
        await _insert_book(
            "Remnant", author_id,
            series_id=series_id, series_index=1.0, owned=1,
        )
        # Incoming standalone matching series position #4 — empty slot.
        standalone_id = await _insert_book(
            "Remnant Book 4", author_id,
            series_id=None, series_index=None, owned=0, source="ibdb",
        )

        await _title_to_series_pass(author_id)

        rows = await _book_rows(author_id)
        by_idx = {r["series_index"]: r for r in rows}
        assert 1.0 in by_idx
        assert 4.0 in by_idx
        assert by_idx[4.0]["id"] == standalone_id

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


class TestTakenNumberNotAMatch:
    """G216: the title→series passes fold two books on one series number
    only when the title identity calls them one book (of one filter
    family); a different book is filed beside the other."""

    async def test_a_boxed_set_isnt_the_owned_novel_on_its_number(self, discovery_db):
        from app.discovery.lookup import _title_to_series_pass
        author_id = await _insert_author("Brandon Sanderson")
        series_id = await _insert_series("The Stormlight Archive", author_id)
        owned = await _insert_book("Words of Radiance", author_id, series_id=series_id, series_index=2.0,
                                   owned=1, source="calibre")
        boxed = await _insert_book("The Stormlight Archive Boxed Set 2", author_id, source="kobo")
        await _title_to_series_pass(author_id)
        rows = {r["id"]: r for r in await _book_rows(author_id)}
        assert set(rows) == {owned, boxed}
        assert (rows[boxed]["series_id"], rows[boxed]["series_index"]) == (series_id, 2.0)

    async def test_another_book_on_a_taken_number_is_filed_beside_it(self, discovery_db):
        from app.discovery.lookup import _title_to_series_pass
        from app.discovery.database import get_db
        author_id = await _insert_author("Leigh Bardugo")
        series_id = await _insert_series("Six of Crows", author_id)
        owned = await _insert_book("Crooked Kingdom", author_id, series_id=series_id, series_index=2.0,
                                   owned=1, source="calibre")
        sampler = await _insert_book("Six of Crows - Chapters 1 and 2", author_id, source="goodreads")
        await _title_to_series_pass(author_id)
        assert {r["id"] for r in await _book_rows(author_id)} == {owned, sampler}
        db = await get_db()
        try:
            merges = await (await db.execute("SELECT COUNT(*) FROM book_merges")).fetchone()
        finally:
            await db.close()
        assert merges[0] == 0

    async def test_a_single_volume_isnt_the_omnibus_on_its_number(self, discovery_db):
        # One filter family per match, as in the merge (G197): the
        # identity alone reads "Slime 01" as "Slime Omnibus 1".
        from app.discovery.lookup import _title_to_series_pass
        author_id = await _insert_author("Fuse")
        series_id = await _insert_series("That Time I Got Reincarnated as a Slime", author_id)
        omnibus = await _insert_book("That Time I Got Reincarnated as a Slime Omnibus 1", author_id,
                                     series_id=series_id, series_index=1.0)
        single = await _insert_book("That Time I Got Reincarnated as a Slime 01", author_id, source="kobo")
        await _title_to_series_pass(author_id)
        rows = {r["id"]: r for r in await _book_rows(author_id)}
        assert set(rows) == {omnibus, single}
        assert (rows[single]["series_id"], rows[single]["series_index"]) == (series_id, 1.0)
