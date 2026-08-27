"""ADR-0021 — the roster predicate.

An author is in the roster when their normalized name is on the global
``authors_allowed`` allow list, OR they own >=1 book in this library in ANY
contributor position. The allow-list half confers MINT permission; the owned
half confers LINK-only permission.

The regression these lock down: before ADR-0021, a source scan minted an
author row for every author-role contributor, and a minted row immediately
satisfied the scan-due query — so one anthology promoted 20+ strangers to
scan targets. See the module docstring on `app.discovery.roster`.
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


# ─── predicate semantics (pure, no DB) ───────────────────────


def _roster(names=(), ids=()):
    return Roster(allowed_names=frozenset(names), owned_author_ids=frozenset(ids))


def test_allow_listed_name_may_mint():
    r = _roster(names={"brandon sanderson"})
    assert r.may_mint("Brandon Sanderson") is True
    assert r.admits("Brandon Sanderson", None) is True


def test_owned_author_may_link_but_never_mint():
    """The owned half is LINK-only — an owner already has a row by
    definition, so minting on it would be meaningless."""
    r = _roster(ids={42})
    assert r.may_mint("Some Co Author") is False
    assert r.admits("Some Co Author", 42) is True
    # unresolved (no row) => nothing to link, and no mint permission
    assert r.admits("Some Co Author", None) is False


def test_stranger_is_rejected():
    """The anthology case: real byline, but not ours to track."""
    r = _roster(names={"brandon sanderson"}, ids={42})
    assert r.may_mint("Clive Barker") is False
    assert r.admits("Clive Barker", None) is False
    # even resolved to an existing NON-owning row (a pre-ADR-0021 junk
    # author on a polluted install) it stays out
    assert r.admits("Clive Barker", 9999) is False


def test_empty_name_never_admitted():
    r = _roster(names={"brandon sanderson"})
    for bad in ("", "   ", None):
        assert r.may_mint(bad) is False


def test_scan_eligibility_is_the_same_predicate():
    """ADR-0021: one predicate, two call sites."""
    r = _roster(names={"brandon sanderson"}, ids={42})
    for name, aid in (("Brandon Sanderson", None), ("X", 42), ("X", None)):
        assert r.is_scan_eligible(name, aid) == r.admits(name, aid)


def test_uses_the_filter_normalizer_not_the_author_row_one():
    """⚠️ The two normalizers disagree. `authors_allowed.normalized` is
    keyed on filter.normalize.normalize_author, which maps "J.R.R. Tolkien"
    to "j r r tolkien" — NOT author_names.normalize_author_name's
    "jrr tolkien". Using the wrong one silently rejects allow-listed
    authors, so pin the behavior here."""
    from app.filter.normalize import normalize_author
    from app.metadata.author_names import normalize_author_name

    assert normalize_author("J.R.R. Tolkien") != normalize_author_name("J.R.R. Tolkien")

    r = _roster(names={normalize_author("J.R.R. Tolkien")})
    assert r.may_mint("J.R.R. Tolkien") is True
    # the other normalizer's output must NOT be what we key on
    r_wrong = _roster(names={normalize_author_name("J.R.R. Tolkien")})
    assert r_wrong.may_mint("J.R.R. Tolkien") is False


def test_calibre_sort_name_form_matches():
    """normalize_author swaps 'Lastname, Firstname' — a Calibre-shaped
    allow-list entry must still match a source's display-name byline."""
    from app.filter.normalize import normalize_author
    r = _roster(names={normalize_author("Sanderson, Brandon")})
    assert r.may_mint("Brandon Sanderson") is True


# ─── load_roster (DB-backed) ─────────────────────────────────


async def _seed(owned_pairs):
    """Seed authors + books; `owned_pairs` = [(author_id, name, owned)]."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        for aid, name, owned in owned_pairs:
            await db.execute(
                "INSERT INTO authors (id, name, sort_name, normalized_name) "
                "VALUES (?,?,?,?)", (aid, name, name, name.lower()),
            )
            cur = await db.execute(
                "INSERT INTO books (title, owned, source) VALUES (?,?,?)",
                (f"Book by {name}", owned, "calibre" if owned else "goodreads"),
            )
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) "
                "VALUES (?,?,0)", (cur.lastrowid, aid),
            )
        await db.commit()
    finally:
        await db.close()


async def test_load_roster_owned_half(discovery_db):
    from app.discovery.database import get_db
    from app.discovery.roster import load_roster

    await _seed([(1, "Owner", 1), (2, "Stranger", 0)])
    db = await get_db()
    try:
        r = await load_roster(db, slug="test", force=True)
    finally:
        await db.close()
    assert 1 in r.owned_author_ids
    assert 2 not in r.owned_author_ids


async def test_owned_counts_any_position_not_just_primary(discovery_db):
    """ADR-0008 co-authored ownership: a book is owned for EVERY one of its
    contributors, so a co-author at position 3 is an owner too."""
    from app.discovery.database import get_db
    from app.discovery.roster import load_roster

    db = await get_db()
    try:
        for aid, nm in ((10, "Primary"), (11, "CoAuthor")):
            await db.execute(
                "INSERT INTO authors (id, name, sort_name, normalized_name) "
                "VALUES (?,?,?,?)", (aid, nm, nm, nm.lower()),
            )
        cur = await db.execute(
            "INSERT INTO books (title, owned, source) VALUES ('Co', 1, 'calibre')"
        )
        bid = cur.lastrowid
        await db.execute(
            "INSERT INTO book_authors (book_id, author_id, position) VALUES (?,10,0)", (bid,))
        await db.execute(
            "INSERT INTO book_authors (book_id, author_id, position) VALUES (?,11,3)", (bid,))
        await db.commit()
        r = await load_roster(db, slug="test", force=True)
    finally:
        await db.close()
    assert {10, 11} <= r.owned_author_ids


async def test_empty_allow_list_still_admits_library_authors(discovery_db):
    """Fresh-install safety: with no allow list, the owned half must still
    admit every library author so scanning behaves as it did pre-ADR-0021."""
    from app.discovery.database import get_db
    from app.discovery.roster import load_roster

    await _seed([(1, "Owner", 1)])
    db = await get_db()
    try:
        r = await load_roster(db, slug="test", force=True)
    finally:
        await db.close()
    assert r.admits("Owner", 1) is True


async def test_allow_list_half_read_from_pipeline_db(discovery_db, monkeypatch):
    """The allow-list half must actually be read when a pipeline DB exists,
    and must be keyed on the FILTER normalizer."""
    import aiosqlite
    from app import config as app_config
    from app.discovery.database import get_db
    from app.discovery.roster import load_roster
    from app.filter.normalize import normalize_author

    monkeypatch.setattr(app_config, "DATA_DIR", discovery_db)
    pdb_path = discovery_db / "seshat.db"
    con = await aiosqlite.connect(str(pdb_path))
    try:
        await con.execute(
            "CREATE TABLE authors_allowed (name TEXT, normalized TEXT, source TEXT)")
        await con.execute("CREATE TABLE authors_ignored (name TEXT, normalized TEXT)")
        await con.execute(
            "INSERT INTO authors_allowed VALUES (?,?,'manual')",
            ("Terry Brooks", normalize_author("Terry Brooks")),
        )
        await con.commit()
    finally:
        await con.close()

    db = await get_db()
    try:
        r = await load_roster(db, slug="test", force=True)
    finally:
        await db.close()
    assert r.may_mint("Terry Brooks") is True
    assert r.may_mint("Clive Barker") is False


async def test_missing_pipeline_db_degrades_to_owned_only(discovery_db, monkeypatch):
    """A tmp DATA_DIR with no seshat.db must NOT fall back to the real
    production database, and must not create a stray one."""
    from app import config as app_config
    from app.discovery.database import get_db
    from app.discovery.roster import load_roster

    monkeypatch.setattr(app_config, "DATA_DIR", discovery_db)
    await _seed([(1, "Owner", 1)])
    db = await get_db()
    try:
        r = await load_roster(db, slug="test", force=True)
    finally:
        await db.close()
    assert r.allowed_names == frozenset()
    assert 1 in r.owned_author_ids
    assert not (discovery_db / "seshat.db").exists()  # nothing created


async def test_cache_is_ttl_scoped_and_invalidatable(discovery_db):
    from app.discovery.database import get_db
    from app.discovery import roster as roster_mod

    await _seed([(1, "Owner", 1)])
    db = await get_db()
    try:
        first = await roster_mod.load_roster(db, slug="test", force=True)
        cached = await roster_mod.load_roster(db, slug="test")
        assert cached is first  # served from cache

        # a new owner appears; cache still serves the stale snapshot
        await _seed([(2, "Owner2", 1)])
        assert await roster_mod.load_roster(db, slug="test") is first

        roster_mod.invalidate("test")
        fresh = await roster_mod.load_roster(db, slug="test")
        assert fresh is not first
        assert 2 in fresh.owned_author_ids
    finally:
        await db.close()
