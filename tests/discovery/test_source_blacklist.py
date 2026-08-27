"""v3.10.0 — operator blacklist for collapsed source author records.

The reference case is OpenLibrary's OL2719653A: one author record holding
a sci-fi author's Fold novels, a political commentator's "Trump and
Churchill" and a children's author's "Kenny the Koala". `_validate_author`
passes on it honestly — the record really does contain owned books — so
the retract path is never reached and no automated signal can help.

Tests cover the module contract (idempotent add, cache invalidation,
fail-open), and the evidence panel's disambiguating-source weighting,
which is the part that would be quietly wrong if it counted google_books
and kobo as corroboration.
"""
from __future__ import annotations

import pytest

from app import config, database
from app.discovery import source_blacklist


@pytest.fixture
async def gdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "seshat.db")
    await database.init_db()
    source_blacklist.invalidate()
    yield tmp_path
    source_blacklist.invalidate()


# ─── Module contract ──────────────────────────────────────────


async def test_add_then_blacklisted(gdb):
    assert not await source_blacklist.is_blacklisted(
        "openlibrary", "OL2719653A")
    await source_blacklist.add(
        "openlibrary", "OL2719653A", author_name="Nick Adams",
        reason="collapsed record", books_retracted=30)
    assert await source_blacklist.is_blacklisted(
        "openlibrary", "OL2719653A")


async def test_blacklist_is_narrow(gdb):
    """Blacklisting one record says nothing about the source generally,
    nor about any other record."""
    await source_blacklist.add("openlibrary", "OL2719653A")
    assert not await source_blacklist.is_blacklisted(
        "openlibrary", "OL8787517A")
    assert not await source_blacklist.is_blacklisted(
        "hardcover", "OL2719653A")


async def test_add_is_idempotent_and_refreshes(gdb):
    a = await source_blacklist.add(
        "openlibrary", "OL1A", author_name="First", books_retracted=1)
    b = await source_blacklist.add(
        "openlibrary", "OL1A", author_name="Second", books_retracted=7)
    assert a == b
    entries = await source_blacklist.list_all()
    assert len(entries) == 1
    assert entries[0]["author_name"] == "Second"
    assert entries[0]["books_retracted"] == 7


async def test_source_matching_is_case_insensitive(gdb):
    await source_blacklist.add("OpenLibrary", "OL2719653A")
    assert await source_blacklist.is_blacklisted(
        "openlibrary", "OL2719653A")


async def test_remove(gdb):
    eid = await source_blacklist.add("openlibrary", "OL1A")
    assert await source_blacklist.remove(eid)
    assert not await source_blacklist.is_blacklisted("openlibrary", "OL1A")
    assert not await source_blacklist.remove(eid)


async def test_missing_id_is_never_blacklisted(gdb):
    assert not await source_blacklist.is_blacklisted("openlibrary", None)
    assert not await source_blacklist.is_blacklisted("openlibrary", "")
    assert not await source_blacklist.is_blacklisted("", "OL1A")


async def test_fails_open_when_unreadable(gdb, monkeypatch):
    """A blacklist we can't read must never suppress a source."""
    await source_blacklist.add("openlibrary", "OL2719653A")
    source_blacklist.invalidate()

    async def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr("app.database.get_db", boom)

    assert not await source_blacklist.is_blacklisted(
        "openlibrary", "OL2719653A")


async def test_write_invalidates_the_cache(gdb):
    assert not await source_blacklist.is_blacklisted("openlibrary", "OL1A")
    # populate the cache with the negative result, then write
    await source_blacklist.add("openlibrary", "OL1A")
    assert await source_blacklist.is_blacklisted("openlibrary", "OL1A")


# ─── The weighting that matters ───────────────────────────────


def test_name_as_id_sources_are_not_disambiguating():
    """google_books, kobo and ibdb use the author NAME as their external
    id, so their agreement carries no identity information. Every one of
    OpenLibrary's 14 'corroborated' Nick Adams rows was corroborated by
    google_books alone — counting those as corroboration would have made
    the evidence panel argue FOR keeping the junk."""
    d = source_blacklist.DISAMBIGUATING_SOURCES
    for weak in ("google_books", "kobo", "ibdb"):
        assert weak not in d, weak
    for strong in ("goodreads", "hardcover", "amazon", "openlibrary"):
        assert strong in d, strong


# ─── Regression: the composite-id 422 ─────────────────────────


async def test_breakdown_route_takes_a_bare_int_not_a_composite_id():
    """The author-detail page's `authorId` prop can be the composite
    "slug:id" form ("calibre-library:619") for a cross-library row. The
    panel shipped passing that straight through, so every request came
    back 422 and — because a failed load rendered nothing — the panel
    silently did not exist on the page. Six 422s in the live logs before
    anyone noticed.

    The contract is: this route takes the NUMERIC per-library id, exactly
    like the neighbouring /authors/{aid}/pen-names route, and callers
    split the composite themselves (`authorIdNum` / `authorSlug`).
    """
    import inspect
    from app.discovery.routers.authors import author_source_breakdown

    sig = inspect.signature(author_source_breakdown)
    assert sig.parameters["aid"].annotation is int, (
        "widening this to str would paper over the caller bug rather than "
        "fix it, and would diverge from every other author route"
    )


def test_frontend_passes_the_numeric_id_to_the_panel():
    """Guards the actual regression at the call site — the type system
    can't, because the page's own prop is legitimately `number | string`."""
    from pathlib import Path
    page = Path(__file__).resolve().parents[2] / (
        "frontend/src/pages/DiscAuthorDetailPage.tsx")
    src = page.read_text()
    idx = src.find("<SourceBreakdownPanel")
    assert idx != -1, "panel not mounted on the author detail page"
    block = src[idx:idx + 400]
    assert "authorId={authorIdNum}" in block, (
        "must pass the parsed numeric id, not the raw composite prop"
    )
    assert "authorId={authorId}" not in block
