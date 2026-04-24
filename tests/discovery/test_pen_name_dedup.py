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

from app.discovery.lookup import _series_index_conflicts


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
