"""v3.x (ADR-0015 slice 02) — ID-first matching in
``resolve_or_create_author``.

A captured ``Contributor.source_author_id`` now anchors per-library
author resolution: when an existing row already records the incoming
``{source}_id``, that row is returned **before** the name ladder runs.
This closes the silent name-collision risk that slice 01 surfaced —
two distinct real authors sharing a normalized name no longer collide
onto the first-encountered row.

Slice 02 contract:

  - ID hit returns the ID-matched row regardless of display name.
  - ID miss falls through to the slice-01 name ladder; the captured
    id is then persisted via fill-if-empty on the matched/minted row.
  - No source / no source_id supplied → pre-ADR behavior unchanged.
  - Unmapped source (``mam``) → ID rung skipped via ``source_id_column``.
  - When two authors share a normalized name but distinct ``{source}_id``,
    each resolves to its own row by ID — neither is name-collided.
"""
from __future__ import annotations

import pytest


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    """Per-library + global DB both initialized — same pattern as the
    slice-01 fixture in ``test_author_id_persistence.py``."""
    from app import config as app_config
    from app import database as global_database
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(global_database, "APP_DB_PATH", tmp_path / "seshat.db")
    await global_database.init_db()
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


async def _column(db, author_id: int, col: str):
    row = await (await db.execute(
        f"SELECT {col} FROM authors WHERE id = ?", (author_id,),
    )).fetchone()
    return row[col] if row else None


async def _conflicts(library_slug: str = "test") -> list[dict]:
    from app.database import get_db as get_global_db
    db = await get_global_db()
    try:
        rows = await (await db.execute(
            "SELECT author_id, source, existing_id, incoming_id, status "
            "FROM author_source_id_conflicts WHERE library_slug = ? "
            "ORDER BY id",
            (library_slug,),
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ─── 1. ID hit returns the ID-matched row (name ignored) ───────


async def test_id_hit_returns_id_row_ignoring_name(discovery_db):
    """The ID rung runs ahead of every name rung. A row whose display
    name differs from the incoming name but whose ``goodreads_id``
    matches must be returned — the canonical id anchors the row."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        # On-file row carries the ID under a different display name
        # (e.g. an earlier scan captured the author as "K. M. Weiland"
        # and this scan reports "Katie Marie Weiland").
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name, "
            "                     goodreads_id) "
            "VALUES (1, 'K. M. Weiland', 'Weiland, K. M.', "
            "        'k m weiland', 'GR-42')"
        )
        aid = await resolve_or_create_author(
            db, "Katie Marie Weiland", allow_create=False,
            source="goodreads", source_id="GR-42",
        )
        assert aid == 1
        # The on-file row's display name MUST be untouched — slice 02
        # is matching-only; it does not rename.
        row = await (await db.execute(
            "SELECT name FROM authors WHERE id = 1"
        )).fetchone()
        assert row["name"] == "K. M. Weiland"
    finally:
        await db.close()
    # ID match is the canonical no-conflict path.
    assert await _conflicts() == []


# ─── 2. ID hit beats normalized-name collision ─────────────────


async def test_id_hit_beats_normalized_name_collision(discovery_db):
    """The motivating ADR-0015 case: two distinct real authors share a
    normalized name (different display-name forms that normalize the
    same — e.g. "A K DuBoff" vs "A. K. DuBoff" both → "a k duboff").
    With slice 01 alone, the second-encountered byline would be
    name-collided onto the first row via the normalized-name rung and
    a Case-4 conflict recorded. Slice 02 routes each to its own row by
    ID — no conflict."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        # Two rows that normalize identically but carry different IDs.
        # (Realistic shape: Calibre's pre-normalization-era drift +
        # later enrichment that captured distinct Goodreads profiles.)
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name, "
            "                     goodreads_id) "
            "VALUES (1, 'A K DuBoff', 'DuBoff, A K', "
            "        'a k duboff', 'GR-FIRST')"
        )
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name, "
            "                     goodreads_id) "
            "VALUES (2, 'A. K. DuBoff', 'DuBoff, A. K.', "
            "        'a k duboff', 'GR-SECOND')"
        )
        # Without ID-first, "A. K. DuBoff" with GR-SECOND would have
        # rung-1 matched row 2 by display name (fine here), but
        # "A K DuBoff" with GR-SECOND would have collided onto row 1
        # via rung 1. With slice 02 the ID lookup wins every time.
        aid_second = await resolve_or_create_author(
            db, "A K DuBoff", allow_create=False,
            source="goodreads", source_id="GR-SECOND",
        )
        assert aid_second == 2
        # And vice versa.
        aid_first = await resolve_or_create_author(
            db, "A. K. DuBoff", allow_create=False,
            source="goodreads", source_id="GR-FIRST",
        )
        assert aid_first == 1
        # Neither column was mutated.
        assert await _column(db, 1, "goodreads_id") == "GR-FIRST"
        assert await _column(db, 2, "goodreads_id") == "GR-SECOND"
    finally:
        await db.close()
    # No conflict rows — both resolutions hit the ID rung cleanly.
    assert await _conflicts() == []


# ─── 3. ID miss → name ladder runs, captured id persists ───────


async def test_id_miss_falls_through_to_name_ladder_then_persists(
    discovery_db,
):
    """When no row carries the incoming ID, slice 02 is a no-op and
    the slice-01 name ladder runs. On a name match, fill-if-empty
    populates the column with the captured id."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        # Row exists with the right name but no captured goodreads_id.
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (5, 'Brandon Sanderson', 'Sanderson, Brandon', "
            "        'brandon sanderson')"
        )
        aid = await resolve_or_create_author(
            db, "Brandon Sanderson", allow_create=False,
            source="goodreads", source_id="GR-NEW-77",
        )
        assert aid == 5
        # Slice-01 fill-if-empty populated the column.
        assert await _column(db, aid, "goodreads_id") == "GR-NEW-77"
    finally:
        await db.close()
    assert await _conflicts() == []


# ─── 4. ID miss + no name match → mint with id ─────────────────


async def test_id_miss_no_name_match_mints_with_id(discovery_db):
    """Neither the ID rung nor any name rung matches, so allow_create
    mints a new row. Slice-01 mint already writes ``{source}_id`` on
    insert — verify slice 02 didn't disturb that path."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        aid = await resolve_or_create_author(
            db, "Fresh Author Name", allow_create=True,
            source="hardcover", source_id="HC-101",
        )
        assert aid is not None
        assert await _column(db, aid, "hardcover_id") == "HC-101"
        # No other source column should have been touched.
        assert await _column(db, aid, "goodreads_id") is None
    finally:
        await db.close()
    assert await _conflicts() == []


# ─── 5. No source/source_id → pre-slice behavior unchanged ─────


async def test_no_source_supplied_skips_id_rung(discovery_db):
    """Backwards-compat regression: a caller that doesn't pass
    source/source_id must hit the original name ladder, never the
    new ID query. A row that happens to carry a goodreads_id should
    NOT be considered."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        # Existing row with a captured goodreads_id. A no-source caller
        # asking for a DIFFERENT name must not pick this row up.
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name, "
            "                     goodreads_id) "
            "VALUES (9, 'Original Name', 'Name, Original', "
            "        'original name', 'GR-FORGOTTEN')"
        )
        # Same name → should match by name (rung 1), no ID lookup.
        aid_match = await resolve_or_create_author(
            db, "Original Name", allow_create=False,
        )
        assert aid_match == 9
        # Column untouched (no source persisted).
        assert await _column(db, 9, "goodreads_id") == "GR-FORGOTTEN"
        # Different name + allow_create=False → no match, no mint.
        aid_miss = await resolve_or_create_author(
            db, "Some Other Author", allow_create=False,
        )
        assert aid_miss is None
    finally:
        await db.close()
    assert await _conflicts() == []


# ─── 6. Source without ID column (mam) → ID rung skipped ───────


async def test_mam_source_skips_id_rung(discovery_db):
    """MAM has no ``mam_id`` column (ADR-0015 §"Out of scope"). The
    ``source_id_column`` gate returns None, so slice 02's ID rung is
    skipped and the name ladder decides."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (4, 'Nameless One', 'One, Nameless', 'nameless one')"
        )
        aid = await resolve_or_create_author(
            db, "Nameless One", allow_create=False,
            source="mam", source_id="mam-12345",
        )
        # Name rung 1 wins.
        assert aid == 4
    finally:
        await db.close()
    assert await _conflicts() == []


# ─── 7. source_id supplied but no source ──────────────────────


async def test_source_id_without_source_skips_id_rung(discovery_db):
    """Defensive — a caller that passes ``source_id`` without ``source``
    cannot resolve a column. The guard ``if source and source_id``
    inside the resolver keeps the ID rung off, so the name ladder
    decides."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name, "
            "                     amazon_id) "
            "VALUES (6, 'Real Author', 'Author, Real', "
            "        'real author', 'B00ASIN-X')"
        )
        # Different name + no source: would only match by name. Since
        # the name differs, this must return None even though the
        # source_id matches an on-file value.
        aid = await resolve_or_create_author(
            db, "Different Name", allow_create=False,
            source=None, source_id="B00ASIN-X",
        )
        assert aid is None
    finally:
        await db.close()
