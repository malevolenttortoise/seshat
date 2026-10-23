"""Wave 5a S4 — an owned box set vouches for its author (G122).

Gentry Race owns only "Cyberratum Trilogy Box Set" (Calibre keeps no
subtitle; series "Cyberratum"). Hardcover, Kobo and IBDB list Artifex,
Annulus and Axiom, which share no title with the box set, so every source
failed the author check and a scan created nothing (2026-10-09). When no
title matches, an owned box set's series or title stem matching a series
or title in the catalogue now passes it. Only owned box sets supply that
evidence, and only on an exact match after normalising.
"""
from __future__ import annotations

import pytest

from app.discovery.lookup import _box_set_key, _validate_author
from app.discovery.sources.base import AuthorResult, BookResult, SeriesResult


def _catalogue(series: dict[str, list[str]] = None, books: list[str] = ()):
    return AuthorResult(
        name="x", external_id="e",
        books=[BookResult(title=t, source="hardcover") for t in books],
        series=[
            SeriesResult(name=name, books=[BookResult(title=t, source="hardcover") for t in titles])
            for name, titles in (series or {}).items()
        ],
    )


GENTRY = ["Cyberratum Trilogy Box Set"]
GENTRY_SETS = [("Cyberratum Trilogy Box Set", "Cyberratum")]


async def test_the_box_sets_series_passes_the_check():
    hc = _catalogue({"Cyberratum": ["Artifex", "Annulus", "Axiom"]})
    assert await _validate_author("Gentry Race", GENTRY, hc, GENTRY_SETS) is True


async def test_without_the_box_set_evidence_it_still_fails():
    hc = _catalogue({"Cyberratum": ["Artifex", "Annulus", "Axiom"]})
    assert await _validate_author("Gentry Race", GENTRY, hc) is False


async def test_a_box_set_title_stem_matches_a_series_without_calibre_series():
    owned = ["Children of Titan Series: Books 1-4"]
    hc = _catalogue({"Children of Titan": ["Children of Titan", "Titan's Rise"]})
    assert await _validate_author(
        "Rhett C. Bruno", owned, hc, [(owned[0], None)],
    ) is True


async def test_a_book_level_series_name_counts_too():
    hc = _catalogue(books=["Artifex"])
    hc.books[0].series_name = "The Cyberratum Trilogy"
    assert await _validate_author("Gentry Race", GENTRY, hc, GENTRY_SETS) is True


async def test_a_namesake_catalogue_still_fails():
    kobo = _catalogue({"Legacy of Ash": ["Ashfall"]}, books=["A Cookbook"])
    assert await _validate_author("Gentry Race", GENTRY, kobo, GENTRY_SETS) is False


async def test_a_shared_word_is_not_a_match():
    """Exact after normalising: "Legacy" mustn't vouch for "Legacy of Ash"."""
    owned = ["Legacy Box Set"]
    src = _catalogue({"Legacy of Ash": ["Ashfall"]})
    assert await _validate_author("A", owned, src, [(owned[0], "Legacy")]) is False


async def test_a_too_short_key_is_ignored():
    owned = ["Ash Box Set"]
    src = _catalogue({"Ash": ["Ember"]})
    assert await _validate_author("A", owned, src, [(owned[0], "Ash")]) is False


@pytest.mark.parametrize("raw, key", [
    ("Cyberratum Trilogy Box Set", "cyberratum"),
    ("The Cyberratum Trilogy", "cyberratum"),
    ("Master of Swords Omnibus: Books 1-3", "master of swords"),
    ("Homeworld Omnibus Part 3", "homeworld"),
    ("Cheer Girls - the Complete Series: 4 Book Omnibus", "cheer girls"),
    (None, ""),
])
def test_box_set_key(raw, key):
    assert _box_set_key(raw) == key


# ─── through a scan ──────────────────────────────────────────


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    from app import config as app_config
    from app import database, state
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "seshat.db")
    await database.init_db()
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "test", "content_type": "ebook", "name": "Test"},
    ])
    yield tmp_path
    disco_db.set_active_library(None)


async def _seed_owned(title: str, series: str) -> int:
    from app.discovery.database import get_db

    db = await get_db("test")
    try:
        aid = (await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES ('Gentry Race', 'Race, Gentry')",
        )).lastrowid
        sid = (await db.execute(
            "INSERT INTO series (name, author_id) VALUES (?, ?)", (series, aid),
        )).lastrowid
        bid = (await db.execute(
            "INSERT INTO books (title, owned, source, series_id, series_index) "
            "VALUES (?, 1, 'calibre', ?, 1)", (title, sid),
        )).lastrowid
        await db.execute(
            "INSERT INTO book_authors (book_id, author_id, position) VALUES (?, ?, 0)",
            (bid, aid))
        await db.commit()
        return aid
    finally:
        await db.close()


async def _scan(monkeypatch, aid: int) -> list[str]:
    """One OpenLibrary-shaped source listing the Cyberratum series."""
    from app.discovery import lookup

    merged: list[str] = []

    async def fake_merge(author_id, result, source_name, *a, **k):
        merged.append(source_name)
        return (3, 0)

    class _OpenLibrary:
        name = "openlibrary"

        async def search_author(self, name, **kwargs):
            return AuthorResult(
                name=name, external_id="OL1A",
                series=[SeriesResult(name="Cyberratum", books=[
                    BookResult(title=t, source="openlibrary")
                    for t in ("Artifex", "Annulus", "Axiom")
                ])],
            )

    monkeypatch.setattr(lookup, "_merge_result", fake_merge)
    monkeypatch.setattr(lookup, "_sources_for_content_type", lambda *a, **k: [
        lookup.SourceSpec("openlibrary", "primary", 5, lambda: _OpenLibrary(), True),
    ])
    await lookup.lookup_author(aid, "Gentry Race")
    return merged


async def test_a_scan_of_a_box_set_only_author_merges(discovery_db, monkeypatch):
    """lookup_author reads the owned box set and its Calibre series from the
    library and the source's catalogue reaches the merge."""
    aid = await _seed_owned("Cyberratum Trilogy Box Set", "Cyberratum")
    assert await _scan(monkeypatch, aid) == ["openlibrary"]


async def test_an_ordinary_owned_books_series_does_not_vouch(discovery_db, monkeypatch):
    """Only box sets supply series evidence (G122): an owned single book in
    the same-named series, matching no title, still fails the check."""
    aid = await _seed_owned("Neon Requiem", "Cyberratum")
    assert await _scan(monkeypatch, aid) == []
