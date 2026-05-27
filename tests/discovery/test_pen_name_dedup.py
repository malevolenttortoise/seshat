"""
Tests for pen-name dedup — the cross-linked-author book-merge gate
in `app.discovery.lookup._merge_result`.

The Incubus Inc. bug: Arand (id=19) has book "Incubus Inc. 3"
stored from a prior sync. When scanning the linked author Darren
(id=49), the discovery pipeline's fuzzy-title match accepts
"Incubus Inc." (series #1) against "Incubus Inc. 3" via
substring-containment, then correctly notices the authors are
linked and SUPPRESSES the insert — but the two are different books
in the same series (#1 vs #3), so the suppression is wrong.
Result: #1 silently dropped.

The fix adds `_series_index_conflicts` as a post-hoc guard on the
fuzzy match. If both sides declare a series_index and the indices
differ, the match is rejected so the insert goes through.
"""
from __future__ import annotations

import pytest

from app.discovery.lookup import (
    _fuzzy_match_blocked,
    _series_index_conflicts,
    _title_extracted_index,
)


# ─── _series_index_conflicts (pure) ──────────────────────────

class TestSeriesIndexConflicts:
    def test_both_none_does_not_conflict(self):
        # Standalone-vs-standalone — no conflict signal, defer to
        # fuzzy title match.
        assert not _series_index_conflicts(None, None)

    def test_one_none_does_not_conflict(self):
        # A source may legitimately report a series book without its
        # position. Can't prove conflict → defer to fuzzy match.
        assert not _series_index_conflicts(None, 3)
        assert not _series_index_conflicts(3, None)

    def test_matching_indices_do_not_conflict(self):
        assert not _series_index_conflicts(3, 3)
        assert not _series_index_conflicts(3.0, 3)
        assert not _series_index_conflicts(3, 3.0)

    def test_differing_indices_conflict(self):
        # The Incubus Inc. case: incoming #1, existing #3 → conflict.
        assert _series_index_conflicts(1, 3)
        assert _series_index_conflicts(3, 1)
        assert _series_index_conflicts(1.0, 3.0)

    def test_fractional_indices(self):
        # Novella at #2.5 vs book at #2 — different books.
        assert _series_index_conflicts(2, 2.5)
        # Same fractional → not a conflict.
        assert not _series_index_conflicts(2.5, 2.5)

    def test_non_numeric_defensive(self):
        # Should never happen given the dataclass typing, but defensive
        # against bad upstream data — treat as "can't prove" rather than
        # raising.
        assert not _series_index_conflicts("abc", 3)  # type: ignore[arg-type]
        assert not _series_index_conflicts(3, "abc")  # type: ignore[arg-type]


# ─── _title_extracted_index (pure) ───────────────────────────

class TestTitleExtractedIndex:
    def test_trailing_number(self):
        assert _title_extracted_index("Super Sales on Super Heroes 4") == 4.0

    def test_trailing_decimal(self):
        assert _title_extracted_index("Mistborn 2.5") == 2.5

    def test_hash_prefix(self):
        assert _title_extracted_index("Series Name #3") == 3.0

    def test_book_n_form(self):
        assert _title_extracted_index("Right of Retribution Book 2") == 2.0

    def test_no_number_returns_none(self):
        assert _title_extracted_index("Right of Retribution") is None

    def test_empty_returns_none(self):
        assert _title_extracted_index("") is None
        assert _title_extracted_index(None) is None  # type: ignore[arg-type]


# ─── _fuzzy_match_blocked (pure) ─────────────────────────────

class TestFuzzyMatchBlocked:
    """Two reject signals on top of the SequenceMatcher acceptance:
    omnibus mismatch and title-extracted series-position conflict."""

    def _row(self, title, series_index=None, is_omnibus=0):
        # Stand-in for an aiosqlite Row — duck-typed via __getitem__
        # and .keys() like the real one. Tests don't actually need a
        # real Row, just one that supports the same accessor surface
        # the production code uses.
        class _R:
            def __init__(self, **kw):
                self._d = kw
            def __getitem__(self, k):
                return self._d.get(k)
            def keys(self):
                return list(self._d.keys())
        return _R(title=title, series_index=series_index, is_omnibus=is_omnibus)

    def _bk(self, title, series_index=None):
        # Stand-in for BookResult — only needs the two attributes the
        # blocker reads.
        class _B:
            pass
        b = _B()
        b.title = title
        b.series_index = series_index
        return b

    # — Omnibus mismatch —

    def test_incoming_omnibus_blocks_match_to_standalone(self):
        """Hardcover's 'Right of Retribution: Compilation: The Starting
        Point' must NOT merge onto the user's owned 'Right of
        Retribution' standalone. Compilation matches `_RX_OMNIBUS`,
        the standalone doesn't."""
        bk = self._bk("Right of Retribution: Compilation: The Starting Point")
        row = self._row("Right of Retribution")
        assert _fuzzy_match_blocked(bk, row) == "omnibus_mismatch"

    def test_incoming_standalone_blocks_match_to_omnibus(self):
        """Symmetric: a standalone-shaped incoming should not merge
        onto an existing omnibus row either."""
        bk = self._bk("Right of Retribution")
        row = self._row("Right of Retribution: Compilation: The Starting Point")
        assert _fuzzy_match_blocked(bk, row) == "omnibus_mismatch"

    def test_both_omnibus_does_not_block(self):
        bk = self._bk("Hero Support: Omnibus")
        row = self._row("Hero Support Omnibus Edition")
        assert _fuzzy_match_blocked(bk, row) is None

    def test_both_standalone_does_not_block(self):
        bk = self._bk("Right of Retribution")
        row = self._row("Right of Retribution")
        assert _fuzzy_match_blocked(bk, row) is None

    # — Position conflict (title-extracted) —

    def test_incoming_title_4_vs_existing_index_2_blocks(self):
        """The Super Sales #4 case: ibdb returns #4 as a standalone
        (no series_index on the BookResult) but the title encodes the
        position. Existing row has series_index=2. Block."""
        bk = self._bk("Super Sales on Super Heroes 4", series_index=None)
        row = self._row("Super Sales on Super Heroes 2", series_index=2.0)
        assert _fuzzy_match_blocked(bk, row) == "position_conflict"

    def test_incoming_title_4_vs_existing_title_2_blocks(self):
        """Both sides untagged but title-extractable — block."""
        bk = self._bk("Super Sales on Super Heroes 4", series_index=None)
        row = self._row("Super Sales on Super Heroes 2", series_index=None)
        assert _fuzzy_match_blocked(bk, row) == "position_conflict"

    def test_matching_title_extracted_indices_does_not_block(self):
        bk = self._bk("Super Sales on Super Heroes 4", series_index=None)
        row = self._row("Super Sales on Super Heroes 4", series_index=None)
        assert _fuzzy_match_blocked(bk, row) is None

    def test_one_side_unindexed_does_not_block(self):
        """When only one side has any index signal, we can't prove
        they're different — defer to the fuzzy match. Source might
        legitimately have dropped the position; the existing row
        might be the canonical of the same book."""
        bk = self._bk("Right of Retribution", series_index=None)
        row = self._row("Right of Retribution", series_index=1.0)
        assert _fuzzy_match_blocked(bk, row) is None

    # — Combined / sanity —

    def test_explicit_series_index_takes_precedence_over_title(self):
        """If the BookResult has an explicit series_index, use it.
        Title-extracted index is a fallback for when the explicit
        one is missing."""
        bk = self._bk("Super Sales on Super Heroes 4", series_index=2.0)
        row = self._row("Super Sales on Super Heroes 2", series_index=2.0)
        assert _fuzzy_match_blocked(bk, row) is None  # both 2.0

    def test_returns_omnibus_reason_first_when_both_apply(self):
        """If a candidate trips both signals, we report omnibus_mismatch
        (it's the more specific signal). Either reason rejects the
        match; the choice only matters for log readability."""
        bk = self._bk("Super Sales Omnibus 4")
        row = self._row("Super Sales 2", series_index=2.0)
        assert _fuzzy_match_blocked(bk, row) == "omnibus_mismatch"

    # — AnimeCon Harem regression (v2.30.1) —

    def test_colon_book_n_subtitle_blocks_sibling_volume_match(self):
        """v2.30.1 regression: `_normalize("AnimeCon Harem: Book N")`
        strips the colon-Book-N subtitle via `_RX_GENERIC_SUBTITLE`,
        so all volumes of a series collapse to the same normalized
        key. The lookup's exact-normalized match path picks the
        lowest-numbered existing row and attaches every incoming
        sibling's source URLs to it — Books 2 and 3 from every source
        merged into Mark's owned Book 1 on the Fortysixtyfour scan
        (2026-05-25). `_fuzzy_match_blocked` correctly extracts the
        volume via `_title_extracted_index` and reports
        `position_conflict`; the fix routes the exact-match path
        through this same guard."""
        bk = self._bk("AnimeCon Harem: Book 2", series_index=None)
        row = self._row("AnimeCon Harem: Book 1", series_index=None)
        assert _fuzzy_match_blocked(bk, row) == "position_conflict"

    def test_colon_book_n_same_volume_does_not_block(self):
        """Symmetric: the same-volume colon-Book-N case (a re-encounter
        of Book 1 from a second source) must merge cleanly."""
        bk = self._bk("AnimeCon Harem: Book 1", series_index=None)
        row = self._row("AnimeCon Harem: Book 1", series_index=None)
        assert _fuzzy_match_blocked(bk, row) is None


# ─── _merge_result integration for pen-name dedup ────────────

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
        # v3.0.0 Phase 4 (ADR-0008): the scan dedup prefilter reads
        # existing books via book_authors, so a seeded book must carry
        # its author link — mirroring backfill-all / sync in prod.
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (?, ?, 0)",
            (cur.lastrowid, author_id),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _author_book_rows(author_id: int) -> list[dict]:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT title, series_id, series_index FROM books "
            "WHERE author_id = ? ORDER BY series_index NULLS LAST, title",
            (author_id,),
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def test_incubus_inc_regression(discovery_db):
    """
    Recreate the exact Incubus Inc. scenario from the UAT logs:
    Arand (linked) has book #3 stored. Darren scan brings in #1,
    #2, #3 with the series correctly tagged. Expected: all three
    rows land under Darren; the #3 under Arand stays untouched.
    """
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult, SeriesResult

    # Seed Arand + his owned "Incubus Inc. 3".
    arand_id = await _insert_author("William D. Arand")
    arand_series_id = await _insert_series("Incubus Inc.", arand_id)
    await _insert_book(
        "Incubus Inc. 3", arand_id,
        series_id=arand_series_id, series_index=3.0, owned=1,
        source="calibre",
    )

    # Seed Darren. No books under him yet.
    darren_id = await _insert_author("Randi Darren")

    # Drive a scan result for Darren: all 3 Incubus Inc. books
    # via Hardcover with the series tagged.
    result = AuthorResult(
        name="Randi Darren",
        external_id="darren-1",
        series=[
            SeriesResult(
                name="Incubus Inc.",
                books=[
                    BookResult(
                        title="Incubus Inc.",
                        series_name="Incubus Inc.",
                        series_index=1.0,
                        source="hardcover",
                    ),
                    BookResult(
                        title="Incubus Inc. II",
                        series_name="Incubus Inc.",
                        series_index=2.0,
                        source="hardcover",
                    ),
                    BookResult(
                        title="Incubus Inc. 3",
                        series_name="Incubus Inc.",
                        series_index=3.0,
                        source="hardcover",
                    ),
                ],
            ),
        ],
    )

    new, _ = await _merge_result(
        author_id=darren_id,
        result=result,
        source_name="hardcover",
        languages=["English"],
        linked_author_ids=[arand_id],
    )

    darren_books = await _author_book_rows(darren_id)
    # Before the fix: only 0 books would land (all three dedup'd
    # against Arand's "Incubus Inc. 3" via fuzzy match).
    # After the fix: books #1 and #2 land under Darren as new.
    # Book #3 is still dedup'd against Arand's row (same
    # series_index — correct pen-name dedup).
    titles = [b["title"] for b in darren_books]
    assert "Incubus Inc." in titles, (
        f"Book #1 should have been inserted — got {titles!r}"
    )
    assert "Incubus Inc. II" in titles, (
        f"Book #2 should have been inserted — got {titles!r}"
    )
    assert "Incubus Inc. 3" not in titles, (
        f"Book #3 should have been dedup'd against Arand's row — "
        f"got {titles!r}"
    )
    # Two legitimately new books landed.
    assert new == 2


async def test_matching_series_index_still_dedups(discovery_db):
    """
    Sanity: the fix must not break the genuine pen-name dedup case.
    Arand has "Incubus Inc. 3" at series_index=3. Darren scan
    brings in "Incubus Inc. 3" also at series_index=3 — that's the
    same book, dedup should fire and suppress the insert.
    """
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult, SeriesResult

    arand_id = await _insert_author("William D. Arand")
    arand_series_id = await _insert_series("Incubus Inc.", arand_id)
    await _insert_book(
        "Incubus Inc. 3", arand_id,
        series_id=arand_series_id, series_index=3.0, owned=1,
        source="calibre",
    )

    darren_id = await _insert_author("Randi Darren")

    result = AuthorResult(
        name="Randi Darren",
        external_id="darren-1",
        series=[
            SeriesResult(
                name="Incubus Inc.",
                books=[
                    BookResult(
                        title="Incubus Inc. 3",
                        series_name="Incubus Inc.",
                        series_index=3.0,
                        source="hardcover",
                    ),
                ],
            ),
        ],
    )

    new, _ = await _merge_result(
        author_id=darren_id,
        result=result,
        source_name="hardcover",
        languages=["English"],
        linked_author_ids=[arand_id],
    )

    darren_books = await _author_book_rows(darren_id)
    # Book #3 matched Arand's existing row by series_index → dedup'd,
    # not inserted under Darren.
    assert [b["title"] for b in darren_books] == []
    assert new == 0


async def test_orphan_series_cleanup_keeps_cross_author_referenced_series(discovery_db):
    """
    Regression: the orphan-series cleanup at the tail of `_merge_result`
    used to scope its "is anyone referencing this series" subquery to
    the SCANNED author's books only. For pen-name-linked pairs that
    leaves the cross-author references invisible — Arand owns the
    "Incubus Inc." series row, but only Darren's books reference it.
    A scan of Arand would see "no Arand books reference Incubus Inc."
    → DELETE the series row → trip the FK from Darren's books
    (`books.series_id REFERENCES series(id)`) → roll back the entire
    scan transaction so every successful merge in the loop is lost.

    After the fix the subquery is unscoped — any book anywhere
    referencing the series keeps it alive.
    """
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    # The exact production shape: Arand owns the "Incubus Inc." series
    # row but has no books referencing it. Darren has 3 books that do.
    arand_id = await _insert_author("William D. Arand")
    shared_series_id = await _insert_series("Incubus Inc.", arand_id)

    darren_id = await _insert_author("Randi Darren")
    for idx in (1.0, 2.0, 3.0):
        await _insert_book(
            f"Incubus Inc. {int(idx)}", darren_id,
            series_id=shared_series_id, series_index=idx,
            owned=0, source="hardcover",
        )

    # Drive a benign scan for Arand — one new owned book unrelated to
    # the shared series. Before the fix, the orphan cleanup at the end
    # raises FK-constraint-failed and `_merge_result` returns (0, 0)
    # via the outer caller's except clause, but the lookup itself
    # raises here so we assert directly.
    result = AuthorResult(
        name="William D. Arand",
        external_id="arand-1",
        books=[
            BookResult(
                title="Some Standalone",
                source="hardcover",
            ),
        ],
    )

    # Should NOT raise. Before fix: FK constraint failed.
    new, _ = await _merge_result(
        author_id=arand_id,
        result=result,
        source_name="hardcover",
        languages=["English"],
        linked_author_ids=[darren_id],
    )

    # The shared series row must still exist — Darren's books reference it.
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT id FROM series WHERE id = ?", (shared_series_id,)
        )).fetchone()
    finally:
        await db.close()
    assert row is not None, "Shared cross-author series was deleted"

    # The new standalone landed.
    assert new == 1


async def test_orphan_series_cleanup_still_drops_truly_orphaned_series(discovery_db):
    """
    Sanity: the fix must not regress the normal cleanup case. A series
    row owned by the scanned author with NO books referencing it from
    any author should still be dropped.
    """
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    arand_id = await _insert_author("William D. Arand")
    orphan_id = await _insert_series("Cancelled Saga", arand_id)
    # No books anywhere reference orphan_id.

    result = AuthorResult(
        name="William D. Arand",
        external_id="arand-1",
        books=[BookResult(title="Different Standalone", source="hardcover")],
    )
    await _merge_result(
        author_id=arand_id,
        result=result,
        source_name="hardcover",
        languages=["English"],
    )

    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT id FROM series WHERE id = ?", (orphan_id,)
        )).fetchone()
    finally:
        await db.close()
    assert row is None, "Truly orphaned series should have been deleted"


async def test_bare_standalone_fuzzy_match_still_works(discovery_db):
    """
    Sanity: fuzzy match WITHOUT series indices on either side should
    still succeed. Standalone vs standalone, title prefix match — the
    original _fuzzy_match behavior is preserved when series_index
    data is absent.
    """
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    arand_id = await _insert_author("William D. Arand")
    # Standalone under Arand, no series.
    await _insert_book(
        "Privateer's Commission", arand_id,
        series_id=None, series_index=None, owned=1, source="calibre",
    )

    darren_id = await _insert_author("Randi Darren")

    # Incoming standalone from a source with no series info.
    result = AuthorResult(
        name="Randi Darren",
        external_id="darren-1",
        books=[
            BookResult(
                title="Privateer's Commission",  # exact title
                series_name=None,
                series_index=None,
                source="hardcover",
            ),
        ],
    )

    new, _ = await _merge_result(
        author_id=darren_id,
        result=result,
        source_name="hardcover",
        languages=["English"],
        linked_author_ids=[arand_id],
    )

    # Exact title match → pen-name dedup fires → not inserted.
    darren_books = await _author_book_rows(darren_id)
    assert [b["title"] for b in darren_books] == []
    assert new == 0
