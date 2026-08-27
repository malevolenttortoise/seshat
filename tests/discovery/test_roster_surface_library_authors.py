"""ADR-0021 slice 4 — offer non-allow-listed LIBRARY authors for review.

Library sync deliberately keeps every real contributor on the byline
(Calibre/ABS are authoritative; gating there would orphan owned books). But
a library author who isn't on the allow list is a real gap, so they're
pushed into the existing `authors_tentative_review` surface.

Bounded on purpose: an install with a big library and an empty allow list
must not get one review row per author on first sync.
"""
from __future__ import annotations

import aiosqlite
import pytest


@pytest.fixture
async def libs(tmp_path, monkeypatch):
    from app import config as app_config
    from app.discovery import database as disco_db
    from app.discovery import roster as roster_mod

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    roster_mod.invalidate()

    con = await aiosqlite.connect(str(tmp_path / "seshat.db"))
    try:
        for t in ("authors_allowed", "authors_ignored",
                  "authors_tentative_review"):
            await con.execute(
                f"CREATE TABLE {t} (name TEXT, normalized TEXT UNIQUE, source TEXT)"
            )
        await con.commit()
    finally:
        await con.close()
    yield tmp_path
    roster_mod.invalidate()
    disco_db.set_active_library(None)


async def _add_authors(rows):
    """rows = [(name, calibre_id|None, abs_id|None)]"""
    from app.discovery.database import get_db
    db = await get_db("test")
    try:
        for i, (name, cid, aid) in enumerate(rows, start=1):
            await db.execute(
                "INSERT INTO authors (id, name, sort_name, normalized_name, "
                "calibre_id, audiobookshelf_id) VALUES (?,?,?,?,?,?)",
                (i, name, name, name.lower(), cid, aid),
            )
        await db.commit()
    finally:
        await db.close()


async def _pipeline_rows(tmp_path, table):
    con = await aiosqlite.connect(str(tmp_path / "seshat.db"))
    try:
        cur = await con.execute(f"SELECT name FROM {table}")
        return sorted(r[0] for r in await cur.fetchall())
    finally:
        await con.close()


async def _seed_pipeline(tmp_path, table, names):
    from app.filter.normalize import normalize_author
    con = await aiosqlite.connect(str(tmp_path / "seshat.db"))
    try:
        for n in names:
            await con.execute(
                f"INSERT OR IGNORE INTO {table} (name, normalized, source) "
                "VALUES (?,?,'test')", (n, normalize_author(n)),
            )
        await con.commit()
    finally:
        await con.close()


async def test_surfaces_only_library_authors_missing_from_allow_list(libs):
    from app.discovery.roster import surface_non_roster_library_authors

    await _add_authors([
        ("Brandon Sanderson", 1, None),   # allow-listed -> skip
        ("Obscure Coauthor", 2, None),    # surface
        ("Audio Only", None, "abs1"),     # surface
        ("Discovered Stranger", None, None),  # not library-sourced -> skip
    ])
    await _seed_pipeline(libs, "authors_allowed", ["Brandon Sanderson"])

    n = await surface_non_roster_library_authors("test")
    assert n == 2
    assert await _pipeline_rows(libs, "authors_tentative_review") == [
        "Audio Only", "Obscure Coauthor",
    ]


async def test_ignored_authors_are_never_resurfaced(libs):
    """A rejected author must not keep reappearing every sync."""
    from app.discovery.roster import surface_non_roster_library_authors

    await _add_authors([("Nope Nope", 1, None)])
    await _seed_pipeline(libs, "authors_ignored", ["Nope Nope"])

    assert await surface_non_roster_library_authors("test") == 0
    assert await _pipeline_rows(libs, "authors_tentative_review") == []


async def test_is_idempotent_across_syncs(libs):
    from app.discovery.roster import surface_non_roster_library_authors

    await _add_authors([("Repeat Author", 1, None)])
    assert await surface_non_roster_library_authors("test") == 1
    assert await surface_non_roster_library_authors("test") == 0
    assert await _pipeline_rows(libs, "authors_tentative_review") == ["Repeat Author"]


async def test_capped_per_sync_to_avoid_a_flood(libs, monkeypatch):
    """The CLAUDE.md grandfather rule in spirit: a fresh install with a
    large library and no allow list must not get one row per author."""
    from app.discovery import roster as roster_mod

    monkeypatch.setattr(roster_mod, "_MAX_SURFACED_PER_SYNC", 3)
    await _add_authors([(f"Author {i:02d}", i, None) for i in range(1, 11)])

    assert await roster_mod.surface_non_roster_library_authors("test") == 3
    # the rest drain on later syncs rather than all at once
    assert await roster_mod.surface_non_roster_library_authors("test") == 3
    assert len(await _pipeline_rows(libs, "authors_tentative_review")) == 6


async def test_no_pipeline_db_is_a_noop(libs):
    """Fresh install with no seshat.db yet must not explode."""
    import os
    from app.discovery.roster import surface_non_roster_library_authors

    await _add_authors([("Someone", 1, None)])
    os.remove(libs / "seshat.db")
    assert await surface_non_roster_library_authors("test") == 0
