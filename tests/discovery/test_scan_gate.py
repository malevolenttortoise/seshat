"""Wave B S5 — one scan gate (D1 / F15) and D6's "list missing books"
switch (F8 / F18 / C6), ADR-0021 amended.

Scan-eligible = owns a book in the library, or allow-listed by Mark (not by
grab auto-train), and the switch on. Every scan path stops at
`lookup_author`'s gate, so "no source request" is asserted on the inner
scan never running.
"""
from __future__ import annotations

import time

import pytest

from app import config as app_config
from app import state


@pytest.fixture
async def env(tmp_path, monkeypatch):
    from app.discovery import database as disco_db
    from app.discovery import metadata_cache
    from app.discovery import roster as roster_mod

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(metadata_cache, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "SETTINGS_PATH", tmp_path / "settings.json")
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = None
    app_config.save_settings({})
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "test", "name": "Books", "content_type": "ebook"},
    ])
    monkeypatch.setattr(state, "_lookup_progress", {})
    monkeypatch.setattr(state, "_lookup_task", None)
    await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
    await disco_db.init_db("test")
    prev = disco_db.get_active_library()
    disco_db.set_active_library("test")
    roster_mod.invalidate()
    yield tmp_path
    roster_mod.invalidate()
    disco_db.set_active_library(prev)


async def _allow(data_dir, entries):
    """The pipeline DB's allow list: [(name, source)]."""
    import aiosqlite
    from app.filter.normalize import normalize_author

    con = await aiosqlite.connect(str(data_dir / "seshat.db"))
    try:
        await con.execute(
            "CREATE TABLE IF NOT EXISTS authors_allowed "
            "(name TEXT, normalized TEXT UNIQUE, source TEXT)")
        await con.execute(
            "CREATE TABLE IF NOT EXISTS authors_ignored (name TEXT, normalized TEXT UNIQUE)")
        for name, source in entries:
            await con.execute(
                "INSERT INTO authors_allowed (name, normalized, source) VALUES (?,?,?)",
                (name, normalize_author(name), source))
        await con.commit()
    finally:
        await con.close()


async def _author(aid, name, *, owned=0, missing=1, last_lookup=None):
    """An author with `owned` owned books and `missing` unowned ones."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name, last_lookup_at) "
            "VALUES (?,?,?,?,?)", (aid, name, name, name.lower(), last_lookup))
        for i in range(owned + missing):
            is_owned = 1 if i < owned else 0
            cur = await db.execute(
                "INSERT INTO books (title, owned, source) VALUES (?,?,?)",
                (f"{name} {i}", is_owned, "calibre" if is_owned else "hardcover"))
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) VALUES (?,?,0)",
                (cur.lastrowid, aid))
        await db.commit()
    finally:
        await db.close()


async def _rows(sql, args=()):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        return [tuple(r) for r in await (await db.execute(sql, args)).fetchall()]
    finally:
        await db.close()


async def _exec(sql, args=()):
    from app.discovery import roster
    from app.discovery.database import get_db
    db = await get_db()
    try:
        await db.execute(sql, args)
        await db.commit()
    finally:
        await db.close()
    roster.invalidate()


@pytest.fixture
def inner(monkeypatch):
    """`lookup_author`'s inner scan (every source request lives there)."""
    from app.discovery import lookup
    calls: list = []

    async def _inner(author_id, author_name, **kw):
        calls.append(author_name)
        return 0

    monkeypatch.setattr(lookup, "_lookup_author_inner", _inner)
    return calls


# ─── Who is scan-eligible ────────────────────────────────────


async def test_allow_list_sources_split_scanning_from_linking(env):
    """D1 / C5: grab auto-train entries pass the filter and link, but
    aren't scanned; the operator's own entries are."""
    from app.discovery import roster
    from app.discovery.database import get_db
    await _allow(env, [("Manual Pick", "manual"), ("Autobrr One", "autobrr_import"),
                       ("Co Trained", "coauthor_train"), ("Legacy Trained", "auto_train"),
                       ("Approved", "tentative_approve")])
    db = await get_db()
    try:
        r = await roster.load_roster(db, "test", force=True)
    finally:
        await db.close()
    for name in ("Manual Pick", "Autobrr One", "Approved"):
        assert r.scan_skip_reason(name, None) is None, name
    for name in ("Co Trained", "Legacy Trained"):
        assert r.admits(name, None) is True, name
        assert r.scan_skip_reason(name, None) == roster.SKIP_NOT_ELIGIBLE, name


async def test_the_switch_is_read_from_the_library_db(env):
    from app.discovery import roster
    from app.discovery.database import get_db
    await _author(1, "Tom Clancy", owned=1)
    await _exec("UPDATE authors SET list_missing = 0 WHERE id = 1")
    db = await get_db()
    try:
        r = await roster.load_roster(db, "test", force=True)
    finally:
        await db.close()
    assert r.scan_skip_reason("Tom Clancy", 1) == roster.SKIP_LIST_MISSING_OFF


# ─── The gate inside lookup_author ───────────────────────────


async def test_lookup_author_skips_an_author_not_scan_eligible(env, inner):
    from app.discovery import lookup
    await _allow(env, [("Co Trained", "coauthor_train")])
    await _author(1, "Co Trained")
    assert await lookup.lookup_author(1, "Co Trained") == 0
    assert inner == []
    assert state._lookup_progress["skipped"] == 1
    assert state._lookup_progress["skipped_authors"] == [
        {"name": "Co Trained", "reason": "not_scan_eligible"}]


async def test_lookup_author_skips_an_owner_with_the_switch_off(env, inner):
    from app.discovery import lookup
    await _author(1, "Tom Clancy", owned=1)
    await _exec("UPDATE authors SET list_missing = 0 WHERE id = 1")
    assert await lookup.lookup_author(1, "Tom Clancy") == 0
    assert inner == []
    assert state._lookup_progress["skipped_authors"][0]["reason"] == "list_missing_off"


async def test_lookup_author_scans_owners_and_allow_listed_authors(env, inner):
    from app.discovery import lookup
    await _allow(env, [("Manual Pick", "manual")])
    await _author(1, "Owner", owned=1)
    await _author(2, "Manual Pick")
    await lookup.lookup_author(1, "Owner")
    await lookup.lookup_author(2, "Manual Pick")
    assert inner == ["Owner", "Manual Pick"]
    assert "skipped" not in state._lookup_progress


async def test_full_rescan_skips_and_counts_authors_not_scan_eligible(env, monkeypatch, inner):
    from app.discovery import lookup
    monkeypatch.setattr(lookup, "reload_sources", lambda: None)
    await _author(1, "Owner", owned=1)
    await _author(2, "Stranger")
    await _author(3, "Off Owner", owned=1)
    await _exec("UPDATE authors SET list_missing = 0 WHERE id = 3")
    await lookup.run_full_rescan()
    assert inner == ["Owner"]
    assert state._lookup_progress["skipped"] == 2
    assert {s["name"]: s["reason"] for s in state._lookup_progress["skipped_authors"]} == {
        "Stranger": "not_scan_eligible", "Off Owner": "list_missing_off"}


async def test_scheduled_lookup_counts_the_skipped(env, monkeypatch, inner):
    from app.discovery import lookup
    monkeypatch.setattr(lookup, "reload_sources", lambda: None)
    await _author(1, "Owner", owned=1)
    await _author(2, "Stranger")
    await lookup.run_full_lookup()
    assert inner == ["Owner"]
    assert state._lookup_progress["skipped"] == 1


async def test_the_author_page_scan_says_why_and_starts_nothing(env, monkeypatch):
    from app.discovery.routers import authors as r
    spawned: list = []
    monkeypatch.setattr(r, "_spawn_lookup_task", lambda *a, **k: spawned.append(a))
    await _author(1, "Stranger")
    res = await r._trigger_single_author_scan(
        1, "test", None, full_scan=False,
        scan_type_single="single_author", scan_type_cross="single_author_cross")
    assert res["status"] == "skipped" and res["reason"] == "not_scan_eligible"
    assert res["message"] == "Stranger: not on your allow list and owns nothing; add them to scan"
    res = await r._trigger_single_author_scan(
        1, "test", "ebook", full_scan=True,
        scan_type_single="single_author_full", scan_type_cross="single_author_full_cross")
    assert res["status"] == "skipped"
    assert spawned == []


async def test_the_author_page_scan_starts_for_an_owner(env, monkeypatch):
    from app.discovery.routers import authors as r
    spawned: list = []
    monkeypatch.setattr(r, "_spawn_lookup_task", lambda *a, **k: spawned.append(a))
    await _author(1, "Owner", owned=1)
    res = await r._trigger_single_author_scan(
        1, "test", None, full_scan=False,
        scan_type_single="single_author", scan_type_cross="single_author_cross")
    assert res["status"] == "started" and len(spawned) == 1


# ─── The switch (D6 / F8 / C6) ───────────────────────────────


async def test_off_hides_the_missing_books_and_on_shows_them(env):
    from app.discovery import list_missing
    await _author(1, "Tom Clancy", owned=1, missing=3)
    assert (await list_missing.status(1, "test"))["missing"] == 3
    r = await list_missing.set_list_missing(1, False, "test")
    assert r == {"found": True, "on": False, "hidden": 3}
    assert await _rows(
        "SELECT owned, hidden, hidden_reason FROM books ORDER BY id") == [
        (1, 0, None), (0, 1, "author_off"), (0, 1, "author_off"), (0, 1, "author_off")]
    assert await list_missing.status(1, "test") == {"found": True, "on": False, "missing": 3}
    r = await list_missing.set_list_missing(1, True, "test")
    assert r == {"found": True, "on": True, "shown": 3}
    assert await _rows("SELECT hidden, hidden_reason FROM books WHERE owned = 0") == [
        (0, None)] * 3


async def test_off_leaves_rows_hidden_for_another_reason_alone(env):
    from app.discovery import list_missing
    await _author(1, "Tom Clancy", owned=1, missing=3)
    await _exec("UPDATE books SET hidden = 1, hidden_reason = NULL WHERE id = 2")      # Mark's hide
    await _exec("UPDATE books SET hidden = 1, hidden_reason = 'comic' WHERE id = 3")
    await list_missing.set_list_missing(1, False, "test")
    await list_missing.set_list_missing(1, True, "test")
    assert await _rows("SELECT id, hidden, hidden_reason FROM books WHERE owned = 0") == [
        (2, 1, None), (3, 1, "comic"), (4, 0, None)]


async def test_a_book_mark_showed_stays_shown(env):
    """F19: "Show" un-hides a row for good, the switch included."""
    from app.discovery import list_missing
    from app.discovery.filtered_rows import SHOW_SET
    await _author(1, "Tom Clancy", owned=0, missing=2)
    await list_missing.set_list_missing(1, False, "test")
    await _exec(f"UPDATE books SET {SHOW_SET} WHERE id = 1")
    await list_missing.set_list_missing(1, True, "test")
    await list_missing.set_list_missing(1, False, "test")
    assert await _rows("SELECT id, hidden, hidden_reason FROM books ORDER BY id") == [
        (1, 0, "shown"), (2, 1, "author_off")]


async def test_a_book_another_contributor_still_lists_stays(env):
    """C6: hidden only when none of its contributors still lists missing
    books; a co-author who isn't scan-eligible doesn't keep it."""
    from app.discovery import list_missing
    await _author(1, "Tom Clancy", owned=1, missing=0)
    await _author(2, "Co Owner", owned=1, missing=0)
    await _author(3, "Stranger", missing=0)
    for title, co in (("Shared With Owner", 2), ("Shared With Stranger", 3)):
        await _exec("INSERT INTO books (title, owned, source) VALUES (?, 0, 'hardcover')", (title,))
        bid = (await _rows("SELECT id FROM books WHERE title = ?", (title,)))[0][0]
        await _exec("INSERT INTO book_authors (book_id, author_id, position) VALUES (?,1,0)", (bid,))
        await _exec("INSERT INTO book_authors (book_id, author_id, position) VALUES (?,?,1)", (bid, co))
    r = await list_missing.set_list_missing(1, False, "test")
    assert r["hidden"] == 1
    assert await _rows("SELECT title, hidden FROM books WHERE owned = 0 ORDER BY title") == [
        ("Shared With Owner", 0), ("Shared With Stranger", 1)]


async def test_endpoints(env):
    from fastapi import HTTPException
    from app.discovery.routers import authors as r
    await _author(1, "Tom Clancy", owned=1, missing=2)
    assert await r.get_author_list_missing(1, slug="test") == {"on": True, "missing": 2}
    assert await r.put_author_list_missing(1, data={"on": False}, slug="test") == {
        "on": False, "hidden": 2}
    with pytest.raises(HTTPException) as e:
        await r.put_author_list_missing(1, data={"on": "no"}, slug="test")
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        await r.get_author_list_missing(99, slug="test")
    assert e.value.status_code == 404
    detail = await r._author_detail_for_slug("test", 1)
    assert detail["list_missing"] == 0
