"""ADR-0021 slice 5 — Hygiene Job 13, non-roster author cleanup.

The gate stops NEW pollution; this removes what a pre-gate install already
accumulated. The three cascade repairs below were each a real bug found
while cleaning the reference install by hand, and each is silent if
skipped — so they get explicit tests rather than a comment.
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
        await con.execute(
            "CREATE TABLE authors_allowed (name TEXT, normalized TEXT UNIQUE, source TEXT)")
        await con.execute(
            "CREATE TABLE authors_ignored (name TEXT, normalized TEXT UNIQUE)")
        await con.commit()
    finally:
        await con.close()
    yield tmp_path
    roster_mod.invalidate()
    disco_db.set_active_library(None)


def _stats():
    return {
        "non_roster_authors_deleted": 0, "non_roster_books_deleted": 0,
        "non_roster_books_renumbered": 0, "non_roster_series_unlinked": 0,
        "errors": [],
    }


LIBS = [{"slug": "test"}]


async def _allow(tmp_path, *names):
    from app.filter.normalize import normalize_author
    con = await aiosqlite.connect(str(tmp_path / "seshat.db"))
    try:
        for n in names:
            await con.execute(
                "INSERT OR IGNORE INTO authors_allowed VALUES (?,?,'test')",
                (n, normalize_author(n)),
            )
        await con.commit()
    finally:
        await con.close()


async def _author(db, aid, name, *, calibre_id=None):
    await db.execute(
        "INSERT INTO authors (id, name, sort_name, normalized_name, calibre_id) "
        "VALUES (?,?,?,?,?)", (aid, name, name, name.lower(), calibre_id),
    )


async def _book(db, title, author_ids, *, owned=0, series_id=None):
    cur = await db.execute(
        "INSERT INTO books (title, owned, source, series_id) VALUES (?,?,?,?)",
        (title, owned, "calibre" if owned else "hardcover", series_id),
    )
    for pos, aid in enumerate(author_ids):
        await db.execute(
            "INSERT INTO book_authors (book_id, author_id, position) VALUES (?,?,?)",
            (cur.lastrowid, aid, pos),
        )
    return cur.lastrowid


async def test_deletes_non_roster_authors_and_their_orphaned_books(libs):
    from app.discovery.database import get_db
    from app.discovery.hygiene import job_non_roster_cleanup

    await _allow(libs, "Keeper")
    db = await get_db("test")
    try:
        await _author(db, 1, "Keeper")
        await _author(db, 2, "Junk One")
        await _author(db, 3, "Junk Two")
        await _book(db, "Kept", [1])
        await _book(db, "Doomed", [2, 3])
        await db.commit()
    finally:
        await db.close()

    st = _stats()
    await job_non_roster_cleanup(st, LIBS)
    assert st["non_roster_authors_deleted"] == 2
    assert st["non_roster_books_deleted"] == 1

    db = await get_db("test")
    try:
        names = [r[0] for r in await (await db.execute(
            "SELECT name FROM authors ORDER BY name")).fetchall()]
        titles = [r[0] for r in await (await db.execute(
            "SELECT title FROM books")).fetchall()]
    finally:
        await db.close()
    assert names == ["Keeper"]
    assert titles == ["Kept"]


async def test_owned_books_are_structurally_safe(libs):
    """Every contributor of an owned book owns that book, so they're in the
    roster's owned half by definition. No owned book can lose a byline even
    with a completely empty allow list."""
    from app.discovery.database import get_db
    from app.discovery.hygiene import job_non_roster_cleanup

    db = await get_db("test")
    try:
        await _author(db, 1, "Owner Primary")
        await _author(db, 2, "Owner Coauthor")
        await _book(db, "Owned Co-write", [1, 2], owned=1)
        await db.commit()
    finally:
        await db.close()

    st = _stats()
    await job_non_roster_cleanup(st, LIBS)
    assert st["non_roster_authors_deleted"] == 0

    db = await get_db("test")
    try:
        n = (await (await db.execute(
            "SELECT COUNT(*) FROM book_authors")).fetchone())[0]
    finally:
        await db.close()
    assert n == 2


async def test_library_sourced_authors_are_never_deleted(libs):
    """ADR-0021 keeps library bylines intact — a Calibre author is real
    even when they're off the allow list."""
    from app.discovery.database import get_db
    from app.discovery.hygiene import job_non_roster_cleanup

    db = await get_db("test")
    try:
        await _author(db, 1, "Calibre Person", calibre_id=55)
        await db.commit()
    finally:
        await db.close()

    st = _stats()
    await job_non_roster_cleanup(st, LIBS)
    assert st["non_roster_authors_deleted"] == 0


async def test_repair_1_book_keeping_a_roster_author_survives(libs):
    from app.discovery.database import get_db
    from app.discovery.hygiene import job_non_roster_cleanup

    await _allow(libs, "Keeper")
    db = await get_db("test")
    try:
        await _author(db, 1, "Keeper")
        await _author(db, 2, "Junk")
        await _book(db, "Shared", [1, 2])
        await db.commit()
    finally:
        await db.close()

    st = _stats()
    await job_non_roster_cleanup(st, LIBS)
    assert st["non_roster_books_deleted"] == 0

    db = await get_db("test")
    try:
        rows = await (await db.execute(
            "SELECT a.name FROM book_authors ba JOIN authors a ON a.id=ba.author_id"
        )).fetchall()
    finally:
        await db.close()
    assert [r[0] for r in rows] == ["Keeper"]


async def test_repair_2_dangling_series_pointer_is_nulled(libs):
    """Deleting `series` by author_id strands surviving books that pointed
    at it — 151 rows on the reference install."""
    from app.discovery.database import get_db
    from app.discovery.hygiene import job_non_roster_cleanup

    await _allow(libs, "Keeper")
    db = await get_db("test")
    try:
        await _author(db, 1, "Keeper")
        await _author(db, 2, "Junk")
        cur = await db.execute(
            "INSERT INTO series (name, author_id) VALUES ('Junk Series', 2)")
        sid = cur.lastrowid
        await _book(db, "Survivor", [1], series_id=sid)
        await db.commit()
    finally:
        await db.close()

    st = _stats()
    await job_non_roster_cleanup(st, LIBS)
    assert st["non_roster_series_unlinked"] == 1

    db = await get_db("test")
    try:
        row = await (await db.execute(
            "SELECT series_id FROM books WHERE title='Survivor'")).fetchone()
        bad = await (await db.execute(
            "PRAGMA foreign_key_check")).fetchall()
    finally:
        await db.close()
    assert row[0] is None
    assert bad == []


async def test_repair_3_position_zero_invariant_restored(libs):
    """A book whose position-0 contributor is deleted must be densely
    renumbered — 528 rows on the reference install. ADR-0012 requires
    exactly one position 0."""
    from app.discovery.database import get_db
    from app.discovery.hygiene import job_non_roster_cleanup

    await _allow(libs, "Keeper")
    db = await get_db("test")
    try:
        await _author(db, 1, "Junk Primary")
        await _author(db, 2, "Keeper")
        await _book(db, "Lost Its Primary", [1, 2])  # junk at position 0
        await db.commit()
    finally:
        await db.close()

    st = _stats()
    await job_non_roster_cleanup(st, LIBS)
    assert st["non_roster_books_renumbered"] == 1

    db = await get_db("test")
    try:
        rows = await (await db.execute(
            "SELECT ba.position, a.name FROM book_authors ba "
            "JOIN authors a ON a.id=ba.author_id ORDER BY ba.position"
        )).fetchall()
    finally:
        await db.close()
    assert [(r[0], r[1]) for r in rows] == [(0, "Keeper")]


async def test_is_inert_on_a_clean_install(libs):
    from app.discovery.database import get_db
    from app.discovery.hygiene import job_non_roster_cleanup

    await _allow(libs, "Keeper")
    db = await get_db("test")
    try:
        await _author(db, 1, "Keeper")
        await _book(db, "Fine", [1])
        await db.commit()
    finally:
        await db.close()

    st = _stats()
    await job_non_roster_cleanup(st, LIBS)
    assert st["non_roster_authors_deleted"] == 0
    assert st["non_roster_books_deleted"] == 0
    assert st["errors"] == []


async def test_job_is_registered_as_job_13(libs):
    from app.discovery.hygiene import JOB_NAMES, TOTAL_JOBS
    assert TOTAL_JOBS == 13
    assert JOB_NAMES[12] == "Non-roster author cleanup"


async def test_cross_library_mirror_rows_are_preserved(libs, monkeypatch):
    """v2.12.1 dual-row pattern: an author is stubbed into every OTHER
    library with zero books so cross-format scans can reach them. Those
    stubs look identical to junk from inside one library. Job 1 already
    guards them -- omitting the guard wiped 93 ABS mirror rows in UAT
    2026-05-17, and the live dry-run showed Job 13 would have repeated it
    (Michael Anderle, Jeff Grubb, Jason Lambright).
    """
    from app.discovery.database import get_db
    from app.discovery import hygiene

    db = await get_db("test")
    try:
        # zero books, no source id, not allow-listed -> looks like junk
        await _author(db, 1, "Michael Anderle")
        await db.commit()
    finally:
        await db.close()

    # ...but they have books in ANOTHER library. Simulate a second
    # library reporting the name; the guard must union only the OTHER
    # libraries' sets, never the current one.
    async def _fake_cross(one_lib):
        slug = one_lib[0].get("slug")
        return frozenset({"michael anderle"}) if slug == "otherlib" else frozenset()
    monkeypatch.setattr(hygiene, "_load_cross_library_book_names", _fake_cross)

    st = _stats()
    await hygiene.job_non_roster_cleanup(st, LIBS + [{"slug": "otherlib"}])
    assert st["non_roster_authors_deleted"] == 0

    db = await get_db("test")
    try:
        n = (await (await db.execute("SELECT COUNT(*) FROM authors")).fetchone())[0]
    finally:
        await db.close()
    assert n == 1
