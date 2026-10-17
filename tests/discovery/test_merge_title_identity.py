"""Discovery fix pass S2 — `_merge_result` decides "same book?" with the
title identity (ADR-0028, G170), files a standalone book into the series
its title names (G171 / F14), and OpenLibrary never creates (D11).

G170: replaying Amazon's cache through `_try_source` (2026-10-12) showed
the old merge dropping 314 books as "norm dup" (128 of them absent from
the library) and merging 47 onto another volume — every Monster Hunter
International book onto its neighbour. The cases below are those shapes.
"""
from __future__ import annotations

import pytest

from app.discovery.lookup import _merge_result
from app.discovery.sources.base import AuthorResult, BookResult, SeriesResult

AUTHOR = "Test Author"


@pytest.fixture
async def library(tmp_path, monkeypatch):
    from app import config as app_config
    from app.discovery import database as disco_db
    from app.discovery import roster as roster_mod

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    roster_mod.invalidate()
    yield await _author_id()
    roster_mod.invalidate()
    disco_db.set_active_library(None)


async def _db():
    from app.discovery.database import get_db
    return await get_db()


async def _author_id() -> int:
    from app.metadata.author_names import normalize_author_name
    db = await _db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name) VALUES (?, ?, ?)",
            (AUTHOR, AUTHOR, normalize_author_name(AUTHOR)),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _book(author_id: int, title: str, *, series: str | None = None,
                index: float | None = None, owned: int = 0, isbn: str | None = None,
                source: str = "hardcover") -> int:
    db = await _db()
    try:
        series_id = None
        if series:
            row = await (await db.execute(
                "SELECT id FROM series WHERE name = ?", (series,),
            )).fetchone()
            if row:
                series_id = row["id"]
            else:
                cur = await db.execute(
                    "INSERT INTO series (name, author_id) VALUES (?, ?)", (series, author_id),
                )
                series_id = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO books (title, series_id, series_index, owned, hidden, source, "
            "                   isbn, source_url) VALUES (?, ?, ?, ?, 0, ?, ?, '{}')",
            (title, series_id, index, owned, source, isbn),
        )
        await db.execute(
            "INSERT INTO book_authors (book_id, author_id, position) VALUES (?, ?, 0)",
            (cur.lastrowid, author_id),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _rows() -> list[dict]:
    db = await _db()
    try:
        cur = await db.execute(
            "SELECT b.id, b.title, b.series_index, s.name AS series, b.source_url, "
            "       b.openlibrary_id FROM books b "
            "LEFT JOIN series s ON s.id = b.series_id ORDER BY b.id"
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


def _in_series(name: str, *books: tuple[str, float]) -> AuthorResult:
    return AuthorResult(name=AUTHOR, external_id="x", series=[SeriesResult(
        name=name,
        books=[BookResult(title=t, series_name=name, series_index=i, source="amazon",
                          source_url=f"https://amazon/{t}") for t, i in books],
    )])


def _standalone(*titles: str, source: str = "amazon", isbn: str | None = None) -> AuthorResult:
    return AuthorResult(name=AUTHOR, external_id="x", books=[
        BookResult(title=t, source=source, isbn=isbn, source_url=f"https://{source}/{t}")
        for t in titles
    ])


async def _merge(author_id: int, result: AuthorResult, source: str = "amazon"):
    return await _merge_result(author_id, result, source, ["English"])


class TestVolumesStayApart:
    async def test_other_volumes_are_created_not_dropped(self, library):
        """G170 (1): 'Faith is Earned' books 1 and 2 normalised to the
        same key as the Hardcover row for book 3; the position guard
        refused the match and the norm-dup skip dropped them."""
        await _book(library, "Faith is Earned: LitRPG, Progression Fantasy, Divine Advancement",
                    series="Faith is Earned", index=3)

        new, _ = await _merge(library, _in_series(
            "Faith is Earned",
            ("Faith is Earned: LitRPG, Progression Fantasy, Divine Advancement (Book 1)", 1),
            ("Faith is Earned: LitRPG, Progression Fantasy, Divine Advancement (Book 2)", 2),
        ))

        assert new == 2
        assert sorted(r["series_index"] for r in await _rows()) == [1, 2, 3]

    async def test_two_real_titles_on_one_position_are_two_books(self, library):
        """G170 (2): SAME-SERIES-POSITION MERGE put every Galaxy's Edge
        book on its neighbour when two sources numbered the series
        differently."""
        await _book(library, "Prisoners of Darkness", series="Galaxy's Edge", index=7)

        new, _ = await _merge(library, _in_series(
            "Galaxy's Edge", ("Turning Point (Galaxy's Edge Book 7)", 7),
        ))

        assert new == 1
        assert {r["title"] for r in await _rows()} == {
            "Prisoners of Darkness", "Turning Point (Galaxy's Edge Book 7)",
        }

    async def test_position_decides_when_one_side_has_no_real_title(self, library):
        await _book(library, "Southern Storm", series="Wild Wastes", index=3)

        new, _ = await _merge(library, _in_series("Wild Wastes", ("Wild Wastes 3", 3)))

        assert new == 0
        (row,) = await _rows()
        assert "amazon" in row["source_url"]

    async def test_a_reading_order_index_never_blocks_the_same_title(self, library):
        """Why the norm-dup skip existed: Hardcover files books under
        reading-order meta-series, so an identical title disagreed on its
        index. The identity compares an index only within one series."""
        await _book(library, "Shadow Puppets", series="Enderverse: Publication Order", index=7)

        new, _ = await _merge(library, _in_series(
            "The Shadow Saga", ("Shadow Puppets (The Shadow Saga Book 3)", 3),
        ))

        assert new == 0
        assert len(await _rows()) == 1

    async def test_a_subtitle_variant_merges(self, library):
        """Q1: one-word title + descriptive subtitle."""
        await _book(library, "Bloodwitch", series="The Witchlands", index=2, owned=1)

        new, _ = await _merge(library, _standalone("Bloodwitch: A Witchlands Novel"))

        assert new == 0
        assert len(await _rows()) == 1

    async def test_the_same_book_twice_in_one_result_is_created_once(self, library):
        new, _ = await _merge(library, _standalone(
            "Dragon Bound", "Dragon Bound: A Legacy Building Fantasy",
        ))

        assert new == 1
        assert [r["title"] for r in await _rows()] == ["Dragon Bound"]

    async def test_the_same_series_book_twice_in_one_result_is_created_once(self, library):
        new, _ = await _merge(library, _in_series(
            "Splinter Angel", ("Splinter Angel: Book 3", 3), ("Splinter Angel 3", 3),
        ))

        assert new == 1
        assert len(await _rows()) == 1

    async def test_isbn_wins_outright(self, library):
        await _book(library, "Leviathan Wakes", isbn="9780316129084")

        new, _ = await _merge(library, _standalone("The Expanse 1", isbn="978-0316129084"))

        assert new == 0

    async def test_an_omnibus_is_not_the_single_book(self, library):
        await _book(library, "Right of Retribution", owned=1)

        new, _ = await _merge(library, _standalone(
            "Right of Retribution: Compilation: The Starting Point",
        ))

        assert new == 1


class TestSeriesWrittenInTheTitle:
    """G171 / F14: a source that gives no series for a book whose title
    names one ("Griffin Academy 2 (Knights Of War #2)" was stored
    standalone)."""

    async def test_created_in_the_series_its_title_names(self, library):
        await _merge(library, _standalone("Contracts & Cats (Meow: Magical Emporium of Wares #1)"))

        (row,) = await _rows()
        assert (row["series"], row["series_index"]) == ("Meow: Magical Emporium of Wares", 1)
        assert row["title"] == "Contracts & Cats (Meow: Magical Emporium of Wares #1)"

    async def test_an_existing_series_is_reused(self, library):
        await _book(library, "Kingdom's Hope", series="Kingdom", index=4)

        await _merge(library, _standalone("Kingdom's Quest (Kingdom Series Book 5)"))

        rows = await _rows()
        assert [(r["series"], r["series_index"]) for r in rows] == [("Kingdom", 4), ("Kingdom", 5)]

    async def test_series_colon_book_n(self, library):
        await _merge(library, _standalone("Splinter Angel: Book 3"))

        (row,) = await _rows()
        assert (row["series"], row["series_index"]) == ("Splinter Angel", 3)

    async def test_no_number_no_series(self, library):
        await _merge(library, _standalone("Splashdown (Book 2)", "Dragon Bound"))

        assert [r["series"] for r in await _rows()] == [None, None]


class TestOpenLibraryNeverCreates:
    async def test_creates_nothing(self, library):
        """D11: 6 of 18 of its sampled rows belonged."""
        new, _ = await _merge(library, _standalone("Earthfall", source="openlibrary"), "openlibrary")

        assert new == 0
        assert await _rows() == []

    async def test_still_enriches_an_existing_book(self, library):
        await _book(library, "Earthfall")
        result = AuthorResult(name=AUTHOR, external_id="x", books=[BookResult(
            title="Earthfall", source="openlibrary", external_id="OL123W",
            source_url="https://openlibrary.org/works/OL123W",
        )])

        await _merge(library, result, "openlibrary")

        (row,) = await _rows()
        assert row["openlibrary_id"] == "OL123W"
