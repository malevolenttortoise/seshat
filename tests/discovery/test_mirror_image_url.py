"""v3.x (ADR-0016 slice 01) — `mirror_image_url` helper.

Verifies the rank-aware, trust-asymmetric, canonical-overwrite-all
write semantics locked in ADR-0016:

  - `trust='scanned'`   → overwrite iff `incoming_rank <= anchor_rank`
                         or anchor is NULL.
  - `trust='co_author'` → strict fill-if-empty (anchor MUST be NULL),
                         regardless of source rank.
  - On write: same `(image_url, image_url_source)` tuple to caller's
              row + every linked sibling + persons row (lockstep).
  - Anchor source = persons row if linked, per-library row if unlinked.
  - Defensive guards: blank/whitespace value → no write; unknown
              source → warning + no write; invalid trust → ValueError.

Diverges from `mirror_bio` deliberately on the fanout shape (no
COALESCE — image is invariant across libraries) and on the rank-
comparison location (helper makes the decision; caller doesn't
pre-write its own slug).
"""
from __future__ import annotations

import pytest


# ─── Fixtures ───────────────────────────────────────────────────


@pytest.fixture
async def single_lib(tmp_path, monkeypatch):
    """Single per-library DB + global DB initialized. Use for unlinked-
    author cases (no `author_links` row, anchor read from per-library)."""
    from app import config as app_config
    from app import database as global_database
    from app.discovery import database as disco_db
    from app.discovery import author_identity as ai

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    # `author_identity.py` imports DATA_DIR at module load — patch the
    # local binding so `_per_library_db_path` resolves into tmp_path.
    monkeypatch.setattr(ai, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(global_database, "APP_DB_PATH", tmp_path / "seshat.db")
    await global_database.init_db()
    disco_db.set_active_library("calibre")
    await disco_db.init_db("calibre")
    yield tmp_path
    disco_db.set_active_library(None)


@pytest.fixture
async def two_libs(tmp_path, monkeypatch):
    """Two per-library DBs + global DB. Use for linked-fanout cases
    (canonical-overwrite-all hits caller + sibling + persons row)."""
    from app import config as app_config
    from app import database as global_database
    from app.discovery import database as disco_db
    from app.discovery import author_identity as ai

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    # `author_identity.py` imports DATA_DIR at module load — patch the
    # local binding so `_per_library_db_path` resolves into tmp_path.
    monkeypatch.setattr(ai, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(global_database, "APP_DB_PATH", tmp_path / "seshat.db")
    await global_database.init_db()
    disco_db.set_active_library("calibre")
    await disco_db.init_db("calibre")
    await disco_db.init_db("abs")
    yield tmp_path
    disco_db.set_active_library(None)


# ─── Helpers ────────────────────────────────────────────────────


async def _insert_author(slug: str, aid: int, name: str,
                         image_url: str | None = None,
                         image_url_source: str | None = None) -> None:
    """Insert an author with explicit id + optional image state."""
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, image_url, image_url_source) "
            "VALUES (?, ?, ?, ?, ?)",
            (aid, name, name, image_url, image_url_source),
        )
        await db.commit()
    finally:
        await db.close()


async def _read_author(slug: str, aid: int) -> dict | None:
    """Return image_url + image_url_source for an author row."""
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        row = await (await db.execute(
            "SELECT image_url, image_url_source FROM authors WHERE id = ?",
            (aid,),
        )).fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def _read_person(person_id: int) -> dict | None:
    from app.database import get_db as get_global_db
    gdb = await get_global_db()
    try:
        row = await (await gdb.execute(
            "SELECT image_url, image_url_source FROM persons WHERE id = ?",
            (person_id,),
        )).fetchone()
        return dict(row) if row else None
    finally:
        await gdb.close()


async def _link_person(canonical_name: str, links: list[tuple[str, int]]) -> int:
    """Create a `persons` row + matching `author_links` for each
    (slug, author_id). Returns the new `person_id`."""
    from app.database import get_db as get_global_db
    from app.metadata.author_names import normalize_author_name

    gdb = await get_global_db()
    try:
        cur = await gdb.execute(
            "INSERT INTO persons (canonical_name, normalized_name) VALUES (?, ?)",
            (canonical_name, normalize_author_name(canonical_name)),
        )
        person_id = cur.lastrowid
        for slug, aid in links:
            await gdb.execute(
                "INSERT INTO author_links (person_id, library_slug, author_id) "
                "VALUES (?, ?, ?)",
                (person_id, slug, aid),
            )
        await gdb.commit()
        return person_id
    finally:
        await gdb.close()


# ─── 1. scanned: mint write — NULL anchor (no existing image) ──


async def test_scanned_writes_fresh_slot_unlinked(single_lib):
    """Unlinked author, no existing image → scanned write proceeds;
    per-library row gets the tuple. No persons row to write (no link).
    """
    from app.discovery.author_identity import mirror_image_url

    await _insert_author("calibre", 1, "Brandon Sanderson")

    n = await mirror_image_url(
        "calibre", 1, "amazon", "https://m.media-amazon.com/sanderson.jpg",
        trust="scanned",
    )
    assert n == 1                                  # caller's row only
    row = await _read_author("calibre", 1)
    assert row == {
        "image_url": "https://m.media-amazon.com/sanderson.jpg",
        "image_url_source": "amazon",
    }


async def test_scanned_writes_fresh_slot_linked(two_libs):
    """Linked author with one sibling → write fans out to caller + sibling
    + persons row. Three rows touched."""
    from app.discovery.author_identity import mirror_image_url

    await _insert_author("calibre", 1, "Brandon Sanderson")
    await _insert_author("abs",     2, "Brandon Sanderson")
    pid = await _link_person("Brandon Sanderson", [("calibre", 1), ("abs", 2)])

    n = await mirror_image_url(
        "calibre", 1, "amazon", "https://m.media-amazon.com/sanderson.jpg",
        trust="scanned",
    )
    assert n == 3                                  # caller + sibling + persons
    expected = {
        "image_url": "https://m.media-amazon.com/sanderson.jpg",
        "image_url_source": "amazon",
    }
    assert await _read_author("calibre", 1) == expected
    assert await _read_author("abs", 2)     == expected
    assert await _read_person(pid)          == expected


# ─── 2. scanned: rank-aware overwrite (higher wins) ────────────


async def test_scanned_higher_rank_overwrites(two_libs):
    """Amazon (rank 1) overwrites Goodreads (rank 2) — canonical-overwrite-all."""
    from app.discovery.author_identity import mirror_image_url

    # Two libs already populated with Goodreads.
    await _insert_author("calibre", 1, "Patrick Rothfuss",
                         "https://gr/rothfuss.jpg", "goodreads")
    await _insert_author("abs",     2, "Patrick Rothfuss",
                         "https://gr/rothfuss.jpg", "goodreads")
    pid = await _link_person("Patrick Rothfuss",
                             [("calibre", 1), ("abs", 2)])
    # Backfill persons row to match the linked-library state.
    from app.database import get_db as get_global_db
    gdb = await get_global_db()
    try:
        await gdb.execute(
            "UPDATE persons SET image_url=?, image_url_source=? WHERE id=?",
            ("https://gr/rothfuss.jpg", "goodreads", pid),
        )
        await gdb.commit()
    finally:
        await gdb.close()

    n = await mirror_image_url(
        "calibre", 1, "amazon", "https://amz/rothfuss.jpg",
        trust="scanned",
    )
    assert n == 3
    new = {"image_url": "https://amz/rothfuss.jpg", "image_url_source": "amazon"}
    assert await _read_author("calibre", 1) == new
    assert await _read_author("abs", 2)     == new
    assert await _read_person(pid)          == new


# ─── 3. scanned: rank-aware skip (lower loses) ─────────────────


async def test_scanned_lower_rank_skipped(two_libs):
    """Goodreads (rank 2) does NOT overwrite Amazon (rank 1)."""
    from app.discovery.author_identity import mirror_image_url

    await _insert_author("calibre", 1, "Patrick Rothfuss",
                         "https://amz/rothfuss.jpg", "amazon")
    await _insert_author("abs",     2, "Patrick Rothfuss",
                         "https://amz/rothfuss.jpg", "amazon")
    pid = await _link_person("Patrick Rothfuss",
                             [("calibre", 1), ("abs", 2)])
    from app.database import get_db as get_global_db
    gdb = await get_global_db()
    try:
        await gdb.execute(
            "UPDATE persons SET image_url=?, image_url_source=? WHERE id=?",
            ("https://amz/rothfuss.jpg", "amazon", pid),
        )
        await gdb.commit()
    finally:
        await gdb.close()

    n = await mirror_image_url(
        "calibre", 1, "goodreads", "https://gr/rothfuss.jpg",
        trust="scanned",
    )
    assert n == 0                                  # no rows touched
    kept = {"image_url": "https://amz/rothfuss.jpg", "image_url_source": "amazon"}
    assert await _read_author("calibre", 1) == kept
    assert await _read_author("abs", 2)     == kept
    assert await _read_person(pid)          == kept


# ─── 4. scanned: same-source refresh ───────────────────────────


async def test_scanned_same_source_refreshes(two_libs):
    """Same rank on both sides → write proceeds (refresh URL on CDN-rehash etc.)."""
    from app.discovery.author_identity import mirror_image_url

    await _insert_author("calibre", 1, "Brandon Sanderson",
                         "https://amz/old.jpg", "amazon")
    await _insert_author("abs",     2, "Brandon Sanderson",
                         "https://amz/old.jpg", "amazon")
    pid = await _link_person("Brandon Sanderson",
                             [("calibre", 1), ("abs", 2)])
    from app.database import get_db as get_global_db
    gdb = await get_global_db()
    try:
        await gdb.execute(
            "UPDATE persons SET image_url=?, image_url_source=? WHERE id=?",
            ("https://amz/old.jpg", "amazon", pid),
        )
        await gdb.commit()
    finally:
        await gdb.close()

    n = await mirror_image_url(
        "calibre", 1, "amazon", "https://amz/new.jpg",
        trust="scanned",
    )
    assert n == 3
    fresh = {"image_url": "https://amz/new.jpg", "image_url_source": "amazon"}
    assert await _read_author("calibre", 1) == fresh
    assert await _read_author("abs", 2)     == fresh
    assert await _read_person(pid)          == fresh


# ─── 5. scanned: NULL anchor source = lowest rank ──────────────


async def test_scanned_null_source_anchor_is_lowest_rank(single_lib):
    """Pre-ADR-0016 row: image populated but image_url_source NULL.
    Any recognized source upgrades it (NULL is treated as lowest)."""
    from app.discovery.author_identity import mirror_image_url

    # Image is set, but source provenance is missing (legacy state).
    await _insert_author("calibre", 1, "Legacy Author",
                         "https://old/photo.jpg", None)
    n = await mirror_image_url(
        "calibre", 1, "audible", "https://audnex/photo.jpg",
        trust="scanned",
    )
    # audible is rank 4 (lowest of the 4 named sources) but still
    # beats a NULL anchor. Write proceeds.
    assert n == 1
    assert await _read_author("calibre", 1) == {
        "image_url": "https://audnex/photo.jpg",
        "image_url_source": "audible",
    }


# ─── 6. co_author: fill-if-empty when slot is NULL ─────────────


async def test_co_author_fills_null_slot(two_libs):
    """Byline-derived co-author write fills a NULL slot — including the
    full canonical-overwrite-all fanout (caller + sibling + persons)."""
    from app.discovery.author_identity import mirror_image_url

    await _insert_author("calibre", 1, "Jason Anspach")
    await _insert_author("abs",     2, "Jason Anspach")
    pid = await _link_person("Jason Anspach",
                             [("calibre", 1), ("abs", 2)])

    n = await mirror_image_url(
        "calibre", 1, "amazon", "https://amz/anspach.jpg",
        trust="co_author",
    )
    assert n == 3
    expected = {"image_url": "https://amz/anspach.jpg", "image_url_source": "amazon"}
    assert await _read_author("calibre", 1) == expected
    assert await _read_author("abs", 2)     == expected
    assert await _read_person(pid)          == expected


# ─── 7. co_author: never upgrades, regardless of rank ──────────


async def test_co_author_never_upgrades_lower_source(two_libs):
    """Amazon byline (rank 1) does NOT overwrite an existing Goodreads
    image (rank 2). Co-author writes are strict fill-if-empty regardless
    of cross-source rank — a low-confidence byline can't upgrade."""
    from app.discovery.author_identity import mirror_image_url

    await _insert_author("calibre", 1, "Nick Cole",
                         "https://gr/cole.jpg", "goodreads")
    await _insert_author("abs",     2, "Nick Cole",
                         "https://gr/cole.jpg", "goodreads")
    pid = await _link_person("Nick Cole",
                             [("calibre", 1), ("abs", 2)])
    from app.database import get_db as get_global_db
    gdb = await get_global_db()
    try:
        await gdb.execute(
            "UPDATE persons SET image_url=?, image_url_source=? WHERE id=?",
            ("https://gr/cole.jpg", "goodreads", pid),
        )
        await gdb.commit()
    finally:
        await gdb.close()

    n = await mirror_image_url(
        "calibre", 1, "amazon", "https://amz/cole.jpg",
        trust="co_author",
    )
    assert n == 0
    kept = {"image_url": "https://gr/cole.jpg", "image_url_source": "goodreads"}
    assert await _read_author("calibre", 1) == kept
    assert await _read_author("abs", 2)     == kept
    assert await _read_person(pid)          == kept


# ─── 8. NULL / blank value preserves existing ──────────────────


@pytest.mark.parametrize("value", [None, "", "   ", "\t  \n"])
async def test_null_or_blank_value_preserves(single_lib, value):
    """NULL / empty / whitespace incoming preserves existing image.
    A misbehaving scraper that yields '' can't blank a working photo."""
    from app.discovery.author_identity import mirror_image_url

    await _insert_author("calibre", 1, "Stephen King",
                         "https://gr/king.jpg", "goodreads")
    n = await mirror_image_url(
        "calibre", 1, "amazon", value, trust="scanned",
    )
    assert n == 0
    assert await _read_author("calibre", 1) == {
        "image_url": "https://gr/king.jpg",
        "image_url_source": "goodreads",
    }


# ─── 9. unknown source → warn + skip ───────────────────────────


async def test_unknown_source_skips(single_lib, caplog):
    """An unrecognized source name is treated as a no-op + WARNING."""
    import logging
    from app.discovery.author_identity import mirror_image_url

    await _insert_author("calibre", 1, "Author X")
    with caplog.at_level(logging.WARNING, logger="seshat.discovery.author_identity"):
        n = await mirror_image_url(
            "calibre", 1, "fictiondb", "https://fdb/x.jpg",
            trust="scanned",
        )
    assert n == 0
    assert any("unknown source" in r.getMessage() for r in caplog.records)
    # Row was not touched.
    assert await _read_author("calibre", 1) == {
        "image_url": None, "image_url_source": None,
    }


# ─── 10. invalid trust → ValueError ────────────────────────────


async def test_invalid_trust_raises(single_lib):
    """Typo guard: trust must be 'scanned' or 'co_author'."""
    from app.discovery.author_identity import mirror_image_url

    await _insert_author("calibre", 1, "Author Y")
    with pytest.raises(ValueError, match="invalid trust"):
        await mirror_image_url(
            "calibre", 1, "amazon", "https://amz/y.jpg",
            trust="rogue",
        )


# ─── 11. idempotent re-write under scanned ─────────────────────


async def test_scanned_same_value_is_lockstep(two_libs):
    """Calling twice with the same value under 'scanned' touches the
    same rows each time (lockstep). Demonstrates the rank rule's
    equality case (`incoming_rank == anchor_rank` → overwrite OK)."""
    from app.discovery.author_identity import mirror_image_url

    await _insert_author("calibre", 1, "Same Author")
    await _insert_author("abs",     2, "Same Author")
    pid = await _link_person("Same Author",
                             [("calibre", 1), ("abs", 2)])

    n1 = await mirror_image_url(
        "calibre", 1, "amazon", "https://amz/same.jpg", trust="scanned",
    )
    n2 = await mirror_image_url(
        "calibre", 1, "amazon", "https://amz/same.jpg", trust="scanned",
    )
    assert n1 == 3 and n2 == 3                       # both writes proceed
    expected = {"image_url": "https://amz/same.jpg", "image_url_source": "amazon"}
    assert await _read_author("calibre", 1) == expected
    assert await _read_author("abs", 2)     == expected
    assert await _read_person(pid)          == expected


# ─── 12. caller's own slug not visited twice as a "sibling" ────


async def test_own_slug_not_visited_as_sibling(two_libs):
    """`linked_authors` includes the caller's own row. The fanout must
    skip it (the helper already wrote it as the caller's row at step 1).
    Otherwise the touched count would be inflated AND we'd attempt a
    second connection to the same DB."""
    from app.discovery.author_identity import mirror_image_url

    await _insert_author("calibre", 1, "Tucker Author")
    await _insert_author("abs",     2, "Tucker Author")
    pid = await _link_person("Tucker Author",
                             [("calibre", 1), ("abs", 2)])

    n = await mirror_image_url(
        "calibre", 1, "amazon", "https://amz/t.jpg", trust="scanned",
    )
    # Bound: caller (1) + one OTHER sibling (1) + persons (1) = 3.
    # If the own-slug skip is broken, n would be 4.
    assert n == 3


# ─── 13. caller-provided db connection (deadlock-avoidance path) ──


async def test_caller_db_param_is_reused(single_lib):
    """When the caller passes its already-open `db` connection, the
    helper uses it for the unlinked-anchor read AND own-row write
    (avoids opening a second connection to the same file)."""
    from app.discovery.database import get_db
    from app.discovery.author_identity import mirror_image_url

    await _insert_author("calibre", 1, "Author With Open Tx")
    db = await get_db("calibre")
    try:
        n = await mirror_image_url(
            "calibre", 1, "amazon", "https://amz/x.jpg",
            trust="scanned", db=db,
        )
        assert n == 1
        # The same `db` should still be live — helper didn't close it.
        row = await (await db.execute(
            "SELECT image_url FROM authors WHERE id = 1",
        )).fetchone()
        assert row["image_url"] == "https://amz/x.jpg"
    finally:
        await db.close()
