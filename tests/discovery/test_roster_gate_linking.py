"""ADR-0021 slice 2 — the roster gate on the two contributor-write paths.

`_link_discovered_contributors` (INSERT path) and `_heal_contributors`
(MATCH / scan-convergence path) both funnel through
`resolve_or_create_author`, which is where the 6,318-author explosion came
from. These lock down that:

  - an allow-listed name may still be MINTED,
  - a non-roster stranger is dropped with no row created,
  - a non-roster name that RESOLVES to an existing non-owning row is still
    dropped (the polluted-install case),
  - the scanned author is never dropped, so a book is never orphaned,
  - `roster=None` preserves pre-ADR-0021 behavior for direct callers.
"""
from __future__ import annotations

import pytest

from app.discovery.roster import Roster
from app.discovery.sources.base import BookResult, Contributor


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


def _roster(names=(), ids=()):
    from app.filter.normalize import normalize_author
    return Roster(
        allowed_names=frozenset(normalize_author(n) for n in names),
        owned_author_ids=frozenset(ids),
    )


async def _seed_scanned_author(db, aid=1, name="Brandon Sanderson"):
    await db.execute(
        "INSERT INTO authors (id, name, sort_name, normalized_name) "
        "VALUES (?,?,?,?)", (aid, name, name, name.lower()),
    )
    cur = await db.execute(
        "INSERT INTO books (title, owned, source) VALUES ('Seed', 0, 'goodreads')"
    )
    return cur.lastrowid


async def _names_on(db, book_id):
    rows = await (await db.execute(
        "SELECT a.name FROM book_authors ba JOIN authors a ON a.id=ba.author_id "
        "WHERE ba.book_id=? ORDER BY ba.position", (book_id,),
    )).fetchall()
    return [r[0] for r in rows]


async def _author_count(db):
    return (await (await db.execute("SELECT COUNT(*) FROM authors")).fetchone())[0]


# ─── _link_discovered_contributors ───────────────────────────


async def test_stranger_co_author_is_not_minted(discovery_db):
    """The anthology case that caused the incident."""
    from app.discovery.database import get_db
    from app.discovery.lookup import _link_discovered_contributors

    db = await get_db()
    try:
        bid = await _seed_scanned_author(db)
        bk = BookResult(title="Dark Delicacies III", contributors=[
            Contributor(name="Brandon Sanderson"),
            Contributor(name="Clive Barker"),
            Contributor(name="Ray Bradbury"),
        ])
        stats: dict = {}
        await _link_discovered_contributors(
            db, bid, 1, bk, "hardcover",
            roster=_roster(names={"Brandon Sanderson"}), stats=stats,
        )
        assert await _names_on(db, bid) == ["Brandon Sanderson"]
        assert await _author_count(db) == 1          # nothing minted
        assert stats["non_roster_skipped"] == 2
        assert set(stats["non_roster_names"]) == {"Clive Barker", "Ray Bradbury"}
    finally:
        await db.close()


async def test_allow_listed_co_author_is_minted(discovery_db):
    """An allow-listed name with no row yet SHOULD create one — that's an
    author the operator explicitly asked to track."""
    from app.discovery.database import get_db
    from app.discovery.lookup import _link_discovered_contributors

    db = await get_db()
    try:
        bid = await _seed_scanned_author(db)
        bk = BookResult(title="Co-written", contributors=[
            Contributor(name="Brandon Sanderson"),
            Contributor(name="Janci Patterson"),
        ])
        await _link_discovered_contributors(
            db, bid, 1, bk, "hardcover",
            roster=_roster(names={"Brandon Sanderson", "Janci Patterson"}),
        )
        assert await _names_on(db, bid) == ["Brandon Sanderson", "Janci Patterson"]
        assert await _author_count(db) == 2
    finally:
        await db.close()


async def test_owned_co_author_is_linked_never_minted(discovery_db):
    from app.discovery.database import get_db
    from app.discovery.lookup import _link_discovered_contributors

    db = await get_db()
    try:
        bid = await _seed_scanned_author(db)
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (7,'J.N. Chaney','Chaney','jn chaney')"
        )
        bk = BookResult(title="Co", contributors=[
            Contributor(name="Brandon Sanderson"),
            Contributor(name="J.N. Chaney"),
        ])
        await _link_discovered_contributors(
            db, bid, 1, bk, "hardcover",
            roster=_roster(names={"Brandon Sanderson"}, ids={7}),
        )
        assert await _names_on(db, bid) == ["Brandon Sanderson", "J.N. Chaney"]
        assert await _author_count(db) == 2  # linked the existing row, minted none
    finally:
        await db.close()


async def test_resolved_but_non_owning_row_is_still_rejected(discovery_db):
    """Polluted-install case: the name matches a junk author row created
    before ADR-0021. Resolution is not admission — linking it would keep
    that row alive as a scan target."""
    from app.discovery.database import get_db
    from app.discovery.lookup import _link_discovered_contributors

    db = await get_db()
    try:
        bid = await _seed_scanned_author(db)
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (99,'Clive Barker','Barker','clive barker')"
        )
        bk = BookResult(title="Anthology", contributors=[
            Contributor(name="Brandon Sanderson"),
            Contributor(name="Clive Barker"),
        ])
        stats: dict = {}
        await _link_discovered_contributors(
            db, bid, 1, bk, "hardcover",
            roster=_roster(names={"Brandon Sanderson"}), stats=stats,
        )
        assert await _names_on(db, bid) == ["Brandon Sanderson"]
        assert stats["non_roster_skipped"] == 1
    finally:
        await db.close()


async def test_scanned_author_always_linked_even_if_non_roster(discovery_db):
    """A discovered book must never be orphaned — position 0 is guaranteed."""
    from app.discovery.database import get_db
    from app.discovery.lookup import _link_discovered_contributors

    db = await get_db()
    try:
        bid = await _seed_scanned_author(db)
        bk = BookResult(title="X", contributors=[Contributor(name="Someone Else")])
        await _link_discovered_contributors(
            db, bid, 1, bk, "hardcover", roster=_roster(),  # empty roster
        )
        assert await _names_on(db, bid) == ["Brandon Sanderson"]
    finally:
        await db.close()


async def test_roster_none_preserves_pre_adr0021_behavior(discovery_db):
    from app.discovery.database import get_db
    from app.discovery.lookup import _link_discovered_contributors

    db = await get_db()
    try:
        bid = await _seed_scanned_author(db)
        bk = BookResult(title="X", contributors=[
            Contributor(name="Brandon Sanderson"),
            Contributor(name="Clive Barker"),
        ])
        await _link_discovered_contributors(db, bid, 1, bk, "hardcover", roster=None)
        assert await _names_on(db, bid) == ["Brandon Sanderson", "Clive Barker"]
    finally:
        await db.close()


async def test_link_only_source_miss_is_not_tallied_as_roster_skip(discovery_db):
    """openlibrary/google_books were already link-only; their misses are
    pre-existing behavior, not an ADR-0021 rejection."""
    from app.discovery.database import get_db
    from app.discovery.lookup import _link_discovered_contributors

    db = await get_db()
    try:
        bid = await _seed_scanned_author(db)
        bk = BookResult(title="X", contributors=[Contributor(name="Unknown Person")])
        stats: dict = {}
        await _link_discovered_contributors(
            db, bid, 1, bk, "openlibrary", roster=_roster(), stats=stats,
        )
        assert stats.get("non_roster_skipped") is None
    finally:
        await db.close()


# ─── _heal_contributors (ADR-0014 path) ──────────────────────


async def test_heal_unions_only_roster_members(discovery_db):
    """ADR-0014 is AMENDED not reversed: the union still happens, it just
    admits roster members only."""
    from app.discovery.database import get_db, write_book_authors
    from app.discovery.lookup import _heal_contributors

    db = await get_db()
    try:
        bid = await _seed_scanned_author(db)
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (7,'J.N. Chaney','Chaney','jn chaney')"
        )
        await write_book_authors(db, bid, [1])
        bk = BookResult(title="X", contributors=[
            Contributor(name="Brandon Sanderson"),
            Contributor(name="J.N. Chaney"),      # owned -> admitted
            Contributor(name="Random Stranger"),  # -> rejected
        ])
        stats: dict = {}
        added = await _heal_contributors(
            db, bid, bk, "hardcover",
            roster=_roster(names={"Brandon Sanderson"}, ids={7}), stats=stats,
        )
        assert added is True
        assert await _names_on(db, bid) == ["Brandon Sanderson", "J.N. Chaney"]
        assert stats["non_roster_skipped"] == 1
        assert await _author_count(db) == 2
    finally:
        await db.close()


async def test_heal_is_noop_when_every_extra_is_non_roster(discovery_db):
    """Delta-only still holds: nothing admitted => no write, no series
    recompute flag."""
    from app.discovery.database import get_db, write_book_authors
    from app.discovery.lookup import _heal_contributors

    db = await get_db()
    try:
        bid = await _seed_scanned_author(db)
        await write_book_authors(db, bid, [1])
        bk = BookResult(title="X", contributors=[
            Contributor(name="Brandon Sanderson"),
            Contributor(name="Stranger One"),
            Contributor(name="Stranger Two"),
        ])
        added = await _heal_contributors(
            db, bid, bk, "hardcover", roster=_roster(names={"Brandon Sanderson"}),
        )
        assert added is False
        assert await _names_on(db, bid) == ["Brandon Sanderson"]
        assert await _author_count(db) == 1
    finally:
        await db.close()
