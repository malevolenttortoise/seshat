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


# ─── The endpoint's retraction path ───────────────────────────
#
# This is the gap that let a real bug ship: every test above exercised
# the blacklist MODULE, and the endpoint's retraction was wrapped in
# `except Exception: logger.exception(...)`. So a TypeError from calling
# `linked_authors(slug, author_id)` -- it takes a person_id, and one
# positional arg -- was swallowed on every click. The row was written,
# `books_retracted` stayed 0, and the UI reported success. A test that
# asserts on the RETURNED COUNT is what makes that visible.


@pytest.fixture
async def full_env(tmp_path, monkeypatch):
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    await database.init_db()
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    source_blacklist.invalidate()
    yield tmp_path
    disco_db.set_active_library(None)
    source_blacklist.invalidate()


async def _seed(slug, author_name="Nick Adams"):
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name, "
            "openlibrary_id) VALUES (?,?,?,?)",
            (author_name, author_name, author_name.lower(), "OL2719653A"))
        aid = cur.lastrowid
        ids = {}
        for title, owned, source in [
            ("The Medusa Fold", 1, "calibre"),
            ("Trump and Churchill", 0, "openlibrary"),
            ("Kenny the Koala", 0, "openlibrary"),
            ("A Real Find", 0, "hardcover"),
        ]:
            c = await db.execute(
                "INSERT INTO books (title, owned, source) VALUES (?,?,?)",
                (title, owned, source))
            ids[title] = c.lastrowid
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) "
                "VALUES (?,?,0)", (c.lastrowid, aid))
        await db.commit()
        return aid, ids
    finally:
        await db.close()


async def _titles(slug):
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        return {r[0] for r in await (await db.execute(
            "SELECT title FROM books")).fetchall()}
    finally:
        await db.close()


async def test_endpoint_actually_retracts_the_sources_books(full_env):
    """The regression: books_retracted must be non-zero and the rows
    must really be gone."""
    from app.discovery.routers.authors import add_source_blacklist

    aid, _ = await _seed("test")
    res = await add_source_blacklist({
        "source": "openlibrary", "source_author_id": "OL2719653A",
        "author_id": aid, "author_name": "Nick Adams", "slug": "test",
    })

    assert res["books_retracted"] == 2, (
        "the two OpenLibrary rows should have been retracted; a swallowed "
        "exception in the retraction path shows up exactly here"
    )
    remaining = await _titles("test")
    assert "Trump and Churchill" not in remaining
    assert "Kenny the Koala" not in remaining
    # Owned + other sources untouched.
    assert "The Medusa Fold" in remaining
    assert "A Real Find" in remaining
    assert await source_blacklist.is_blacklisted(
        "openlibrary", "OL2719653A")


async def test_endpoint_records_the_retracted_count(full_env):
    from app.discovery.routers.authors import add_source_blacklist
    aid, _ = await _seed("test")
    await add_source_blacklist({
        "source": "openlibrary", "source_author_id": "OL2719653A",
        "author_id": aid, "slug": "test",
    })
    entries = await source_blacklist.list_all()
    assert entries[0]["books_retracted"] == 2


async def test_endpoint_retracts_against_the_slug_not_the_active_library(
        full_env, monkeypatch):
    """`author_id` is per-library, so retracting against the active
    library when the operator was looking at another one would target a
    completely unrelated author."""
    from app.discovery import database as disco_db
    from app.discovery.routers.authors import add_source_blacklist

    await disco_db.init_db("other")
    aid, _ = await _seed("test")
    # Operator is viewing "test"; the ACTIVE library is something else.
    disco_db.set_active_library("other")

    res = await add_source_blacklist({
        "source": "openlibrary", "source_author_id": "OL2719653A",
        "author_id": aid, "slug": "test",
    })
    assert res["books_retracted"] == 2
    assert "Trump and Churchill" not in await _titles("test")


async def test_endpoint_without_author_id_only_records(full_env):
    """Blacklisting from a context with no author still writes the rule."""
    from app.discovery.routers.authors import add_source_blacklist
    res = await add_source_blacklist({
        "source": "openlibrary", "source_author_id": "OL999X",
    })
    assert res["books_retracted"] == 0
    assert await source_blacklist.is_blacklisted("openlibrary", "OL999X")
