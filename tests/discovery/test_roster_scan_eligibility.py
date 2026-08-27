"""ADR-0021 slice 3 — the roster gates scan eligibility.

Before ADR-0021 the scan-due query tested only "last_lookup_at is old" AND
"has at least one book_authors row". A freshly-minted anthology co-author
satisfied both the instant it was created, which is what turned a single
scan into a self-feeding cascade.

Both the scan loop and the router's pre-flight due-count now route through
`scan_eligible_authors`, so they cannot disagree.
"""
from __future__ import annotations

import pytest

from app.discovery.roster import Roster


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    from app import config as app_config
    from app.discovery import database as disco_db
    from app.discovery import roster as roster_mod

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    roster_mod.invalidate()
    yield tmp_path
    roster_mod.invalidate()
    disco_db.set_active_library(None)


def _patch_roster(monkeypatch, names=(), ids=()):
    """Pin the roster so the test doesn't depend on the global pipeline DB."""
    from app.filter.normalize import normalize_author
    from app.discovery import roster as roster_mod

    fixed = Roster(
        allowed_names=frozenset(normalize_author(n) for n in names),
        owned_author_ids=frozenset(ids),
    )

    async def _fake(db, slug=None, *, force=False):
        return fixed

    monkeypatch.setattr(roster_mod, "load_roster", _fake)
    return fixed


async def _seed(db, aid, name, *, owned, last_lookup=None):
    await db.execute(
        "INSERT INTO authors (id, name, sort_name, normalized_name, last_lookup_at) "
        "VALUES (?,?,?,?,?)", (aid, name, name, name.lower(), last_lookup),
    )
    cur = await db.execute(
        "INSERT INTO books (title, owned, source) VALUES (?,?,?)",
        (f"B{aid}", 1 if owned else 0, "calibre" if owned else "hardcover"),
    )
    await db.execute(
        "INSERT INTO book_authors (book_id, author_id, position) VALUES (?,?,0)",
        (cur.lastrowid, aid),
    )


async def test_non_roster_author_is_not_scan_eligible(discovery_db, monkeypatch):
    """The cascade-stopper: a minted stranger with books is skipped."""
    import time
    from app.discovery.database import get_db
    from app.discovery.roster import scan_eligible_authors

    _patch_roster(monkeypatch, names={"Brandon Sanderson"}, ids={1})
    db = await get_db()
    try:
        await _seed(db, 1, "Brandon Sanderson", owned=True)
        await _seed(db, 2, "Clive Barker", owned=False)   # minted stranger
        await db.commit()
        rows = await scan_eligible_authors(db, time.time())
    finally:
        await db.close()
    assert [r["name"] for r in rows] == ["Brandon Sanderson"]


async def test_owned_author_eligible_without_allow_list(discovery_db, monkeypatch):
    """Fresh-install safety — an empty allow list must not stop scanning."""
    import time
    from app.discovery.database import get_db
    from app.discovery.roster import scan_eligible_authors

    _patch_roster(monkeypatch, names=(), ids={1, 2})
    db = await get_db()
    try:
        await _seed(db, 1, "Owner One", owned=True)
        await _seed(db, 2, "Owner Two", owned=True)
        await db.commit()
        rows = await scan_eligible_authors(db, time.time())
    finally:
        await db.close()
    assert {r["name"] for r in rows} == {"Owner One", "Owner Two"}


async def test_orphan_author_still_excluded(discovery_db, monkeypatch):
    """Pre-existing rule survives: no linked books => nothing to merge into."""
    import time
    from app.discovery.database import get_db
    from app.discovery.roster import scan_eligible_authors

    _patch_roster(monkeypatch, names={"Ghost Author"})
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (5,'Ghost Author','Ghost','ghost author')"
        )
        await db.commit()
        rows = await scan_eligible_authors(db, time.time())
    finally:
        await db.close()
    assert rows == []


async def test_cutoff_still_applies(discovery_db, monkeypatch):
    """Roster membership doesn't override the lookup-interval cache window."""
    import time
    from app.discovery.database import get_db
    from app.discovery.roster import scan_eligible_authors

    now = time.time()
    _patch_roster(monkeypatch, ids={1, 2})
    db = await get_db()
    try:
        await _seed(db, 1, "Recent", owned=True, last_lookup=now)
        await _seed(db, 2, "Stale", owned=True, last_lookup=now - 99999)
        await db.commit()
        rows = await scan_eligible_authors(db, now - 3600)
    finally:
        await db.close()
    assert [r["name"] for r in rows] == ["Stale"]


async def test_due_count_matches_the_scan_loop(discovery_db, monkeypatch):
    """The pre-flight count and the loop must never disagree — they share
    one helper precisely so an operator isn't told '900 due' and then
    watches 120 get scanned."""
    import time
    from app.discovery.database import get_db
    from app.discovery.roster import scan_eligible_authors
    from app.discovery.routers.scan import _count_due_authors

    _patch_roster(monkeypatch, names={"Keeper"}, ids={1})
    db = await get_db()
    try:
        await _seed(db, 1, "Keeper", owned=True)
        await _seed(db, 2, "Stranger A", owned=False)
        await _seed(db, 3, "Stranger B", owned=False)
        await db.commit()
        cutoff = time.time()
        loop_rows = await scan_eligible_authors(db, cutoff)
    finally:
        await db.close()
    assert len(loop_rows) == await _count_due_authors(cutoff) == 1
