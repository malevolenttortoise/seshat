"""v3.x (ADR-0015 slice 01) — author source-ID persistence at link time.

Verifies that ``resolve_or_create_author`` persists a captured
``Contributor.source_author_id`` onto the resolved row's
``{source}_id`` column with **fill-if-empty** semantics:

  - mint with source/source_id        → column populated on the new row
  - match with NULL on the column     → column filled
  - match with equal id on the column → no-op
  - match with different id           → existing left alone, conflict row
                                        upserted into
                                        ``author_source_id_conflicts``
  - unmapped source (e.g. ``mam``)    → persistence path skipped silently
  - repeat conflict                   → one row, ``last_seen_at`` bumped
  - no source/source_id supplied      → pre-ADR behavior unchanged

The conflict path is the load-bearing piece of the locked design: the
on-file canonical id is NEVER overwritten by a byline-derived
contributor (lower confidence than the scanned-author overwrite path),
so a name-collision risk surfaces for operator review instead of
silently corrupting identity.
"""
from __future__ import annotations

import pytest


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    """Per-library + global DB both initialized — ``record_source_id_conflict``
    needs the global DB. Mirrors the pattern in
    ``test_calibre_sync_merge_sweep`` and ``test_phase8_series_detail``."""
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
    """All conflict rows for the test library, position-stable."""
    from app.database import get_db as get_global_db
    db = await get_global_db()
    try:
        rows = await (await db.execute(
            "SELECT author_id, source, existing_id, incoming_id, "
            "       incoming_name, status, first_seen_at, last_seen_at "
            "FROM author_source_id_conflicts "
            "WHERE library_slug = ? "
            "ORDER BY id",
            (library_slug,),
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ─── 1. Mint writes {source}_id ────────────────────────────────


async def test_mint_writes_source_id(discovery_db):
    """Trusted-create + source/source_id supplied → new row carries the ID."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        aid = await resolve_or_create_author(
            db, "Brand New Author", allow_create=True,
            source="goodreads", source_id="42",
        )
        assert aid is not None
        assert await _column(db, aid, "goodreads_id") == "42"
        # Other source columns must remain NULL.
        assert await _column(db, aid, "amazon_id") is None
        assert await _column(db, aid, "hardcover_id") is None
    finally:
        await db.close()


# ─── 2. Fill-if-empty on match with NULL column ────────────────


async def test_fill_if_empty_on_match_with_null(discovery_db):
    """Name-matched row whose `{source}_id` is NULL gets it filled."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (7, 'Maggie Pierce', 'Pierce, Maggie', 'maggie pierce')"
        )
        aid = await resolve_or_create_author(
            db, "Maggie Pierce", allow_create=False,
            source="hardcover", source_id="HC-913",
        )
        assert aid == 7
        assert await _column(db, aid, "hardcover_id") == "HC-913"
    finally:
        await db.close()
    assert await _conflicts() == []   # no conflict, this was a clean fill


# ─── 3. No-op when populated column already matches ────────────


async def test_noop_when_column_already_matches(discovery_db):
    """Matched row whose column equals the incoming id → no write, no conflict."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name, amazon_id) "
            "VALUES (3, 'Iris West', 'West, Iris', 'iris west', 'B00ASIN777')"
        )
        aid = await resolve_or_create_author(
            db, "Iris West", allow_create=False,
            source="amazon", source_id="B00ASIN777",
        )
        assert aid == 3
        assert await _column(db, aid, "amazon_id") == "B00ASIN777"  # unchanged
    finally:
        await db.close()
    assert await _conflicts() == []


# ─── 4. Case 4 — populated and different → record conflict ─────


async def test_case4_records_conflict_and_preserves_existing(discovery_db):
    """The headline ADR-0015 case: matched row's column is populated with
    a different id than the incoming source_author_id. The on-file
    canonical id MUST NOT be overwritten; a conflict row is upserted."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name, goodreads_id) "
            "VALUES (11, 'Robert Heinlein', 'Heinlein, Robert', "
            "        'robert heinlein', 'GR-CANONICAL-205')"
        )
        aid = await resolve_or_create_author(
            db, "Robert Heinlein", allow_create=False,
            source="goodreads", source_id="GR-OTHER-999",
        )
        assert aid == 11
        # On-file id MUST be untouched.
        assert await _column(db, aid, "goodreads_id") == "GR-CANONICAL-205"
    finally:
        await db.close()
    rows = await _conflicts()
    assert len(rows) == 1
    r = rows[0]
    assert r["author_id"] == 11
    assert r["source"] == "goodreads"
    assert r["existing_id"] == "GR-CANONICAL-205"
    assert r["incoming_id"] == "GR-OTHER-999"
    assert r["incoming_name"] == "Robert Heinlein"
    assert r["status"] == "open"


# ─── 5. Unmapped source (mam) skips persistence silently ───────


async def test_mam_source_skips_persistence(discovery_db):
    """MAM has no `mam_id` column by design (ADR-0015 §"Out of scope").
    A scan with source="mam" + source_id="..." must NOT error and must
    NOT touch any column — persistence path is skipped at the
    source_id_column() gate."""
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
        assert aid == 4
        # Sanity — every known source column stays NULL.
        for col in (
            "amazon_id", "goodreads_id", "hardcover_id",
            "audible_id", "openlibrary_id", "google_books_id",
        ):
            assert await _column(db, aid, col) is None
    finally:
        await db.close()
    assert await _conflicts() == []


# ─── 6. UPSERT dedup — repeat conflict bumps last_seen_at ──────


async def test_repeat_conflict_upserts_one_row(discovery_db):
    """The dedup key is (library_slug, author_id, source, incoming_id).
    The same conflict surfacing across multiple scans must collapse
    onto one row with `last_seen_at` bumped — not spam a row per scan."""
    import asyncio
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name, goodreads_id) "
            "VALUES (12, 'Octavia Butler', 'Butler, Octavia', "
            "        'octavia butler', 'GR-PRIMARY-1')"
        )
        for _ in range(3):
            await resolve_or_create_author(
                db, "Octavia Butler", allow_create=False,
                source="goodreads", source_id="GR-OTHER-2",
            )
            await asyncio.sleep(1.05)  # cross the 1-sec strftime resolution
    finally:
        await db.close()
    rows = await _conflicts()
    assert len(rows) == 1
    r = rows[0]
    assert r["existing_id"] == "GR-PRIMARY-1"
    assert r["incoming_id"] == "GR-OTHER-2"
    # last_seen_at must have moved past first_seen_at after repeats.
    assert r["last_seen_at"] > r["first_seen_at"]


# ─── 7. Backwards-compat — no source/source_id supplied ────────


async def test_no_source_supplied_is_pre_adr_behavior(discovery_db):
    """Calls without source/source_id behave exactly as before the slice:
    no persistence, no conflict-record, no errors. Protects every
    existing caller that hasn't been threaded through yet."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name, goodreads_id) "
            "VALUES (8, 'Ursula Le Guin', 'Le Guin, Ursula', "
            "        'ursula le guin', 'GR-LEGACY')"
        )
        # Match, no source — must NOT mutate the column, no conflict.
        aid = await resolve_or_create_author(
            db, "Ursula Le Guin", allow_create=False,
        )
        assert aid == 8
        assert await _column(db, aid, "goodreads_id") == "GR-LEGACY"
        # Mint, no source — must NOT include `_id` columns.
        aid2 = await resolve_or_create_author(
            db, "Some New Writer", allow_create=True,
        )
        assert aid2 is not None
        assert await _column(db, aid2, "goodreads_id") is None
    finally:
        await db.close()
    assert await _conflicts() == []


# ─── 8. source_id_column mapping (unit-level sanity) ───────────


def test_source_id_column_mapping():
    """The source→column mapping is the gate that decides whether
    persistence runs. ADR-0015 §"Out of scope" excludes mam."""
    from app.discovery.author_identity import source_id_column
    assert source_id_column("goodreads") == "goodreads_id"
    assert source_id_column("amazon") == "amazon_id"
    assert source_id_column("hardcover") == "hardcover_id"
    assert source_id_column("audible") == "audible_id"
    assert source_id_column("openlibrary") == "openlibrary_id"
    assert source_id_column("google_books") == "google_books_id"
    # MAM: no mam_id column by design.
    assert source_id_column("mam") is None
    # Unknown / empty.
    assert source_id_column(None) is None
    assert source_id_column("") is None
    assert source_id_column("nonsense") is None
