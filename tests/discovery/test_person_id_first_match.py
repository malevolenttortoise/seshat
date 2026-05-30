"""v3.x (ADR-0015 slice 03) — ID-first matching in
``get_or_create_person`` (cross-library).

After Step 1 (existing ``author_links`` check) and before the
normalized-name rung, the resolver now consults the per-library
author row's populated source IDs and joins them through
``author_links`` to find an already-anchored person. On a unique
ID match → reuse that person with ``link_confidence='high'``;
ambiguity (two persons tied) → drop to the name rung (no conflict
row recorded — see the docstring for the rationale).

This closes the v2.20.0 split-person gap where one author lands on
two persons because the normalized names differ ("Robert Heinlein"
vs "Robert A. Heinlein") even though both per-library rows already
carry the same Goodreads ID.

Slice 03 contract under test:

  - Clean ID consolidation: name-differing rows sharing a source ID
    end up on one person with ``link_confidence='high'``.
  - Multi-ID confluence: two source IDs that both point to the same
    person → still one resolution, no ambiguity.
  - Multi-ID ambiguity: source IDs pointing to different persons →
    drop to the name rung (NO row in ``author_source_id_conflicts``).
  - Same-library ID match: another row in THIS library carrying a
    matching ID is found too (the rung scans every library that
    hosts a linked author, not just other libraries).
  - No source IDs on the row → behavior unchanged (name rung wins).
  - Sources naturally absent on a row skip the rung (e.g.
    ``google_books_id`` is NULL → not in ``populated_ids``).
  - Override-args path still respected; ID rung still consults the
    per-library row's actual IDs.
"""
from __future__ import annotations

import aiosqlite
import pytest

from app import config, database
from app.discovery import author_identity
from app.discovery.author_identity import get_or_create_person


_PER_LIB_AUTHORS_DDL = """
CREATE TABLE authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort_name TEXT NOT NULL DEFAULT '',
    normalized_name TEXT,
    bio TEXT,
    image_url TEXT,
    amazon_id TEXT,
    goodreads_id TEXT,
    hardcover_id TEXT,
    kobo_id TEXT,
    ibdb_id TEXT,
    google_books_id TEXT,
    openlibrary_id TEXT,
    audible_id TEXT,
    audiobookshelf_id TEXT,
    fictiondb_id TEXT,
    calibre_id INTEGER,
    UNIQUE(name)
);
"""


@pytest.fixture
async def cross_lib(tmp_path, monkeypatch):
    """Two per-library DBs (calibre-library + abs-audio-library) +
    a global seshat.db, all wired through the production DATA_DIR /
    APP_DB_PATH. Mirrors ``cross_lib_env`` from
    ``test_author_identity.py`` but trimmed to what slice 03 needs."""
    global_path = tmp_path / "seshat.db"
    monkeypatch.setattr(config, "APP_DB_PATH", global_path)
    monkeypatch.setattr(database, "APP_DB_PATH", global_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(author_identity, "DATA_DIR", tmp_path)

    await database.init_db()

    slugs = ["calibre-library", "abs-audio-library"]
    for slug in slugs:
        db = await aiosqlite.connect(str(tmp_path / f"seshat_{slug}.db"))
        try:
            await db.executescript(_PER_LIB_AUTHORS_DDL)
            await db.commit()
        finally:
            await db.close()

    async def add_author(slug: str, name: str, **cols) -> int:
        from app.metadata.author_names import normalize_author_name
        normalized = cols.pop("normalized_name", normalize_author_name(name))
        valid = {
            "amazon_id", "goodreads_id", "hardcover_id", "kobo_id",
            "ibdb_id", "google_books_id", "openlibrary_id",
            "audible_id", "fictiondb_id",
        }
        extra_cols = [c for c in cols if c in valid]
        sql_cols = ["name", "normalized_name"] + extra_cols
        sql_vals = [name, normalized] + [cols[c] for c in extra_cols]
        placeholders = ",".join("?" * len(sql_cols))
        db = await aiosqlite.connect(
            str(tmp_path / f"seshat_{slug}.db")
        )
        try:
            cur = await db.execute(
                f"INSERT INTO authors ({', '.join(sql_cols)}) "
                f"VALUES ({placeholders})",
                sql_vals,
            )
            await db.commit()
            return cur.lastrowid
        finally:
            await db.close()

    async def link_confidence(slug: str, aid: int) -> str | None:
        gdb = await aiosqlite.connect(str(global_path))
        gdb.row_factory = aiosqlite.Row
        try:
            row = await (await gdb.execute(
                "SELECT link_confidence FROM author_links "
                "WHERE library_slug = ? AND author_id = ?",
                (slug, aid),
            )).fetchone()
            return row["link_confidence"] if row else None
        finally:
            await gdb.close()

    async def conflict_count() -> int:
        gdb = await aiosqlite.connect(str(global_path))
        try:
            row = await (await gdb.execute(
                "SELECT COUNT(*) FROM author_source_id_conflicts"
            )).fetchone()
            return row[0]
        finally:
            await gdb.close()

    yield {
        "tmp_path": tmp_path,
        "slugs": slugs,
        "add_author": add_author,
        "link_confidence": link_confidence,
        "conflict_count": conflict_count,
    }


# ─── 1. Clean cross-library ID consolidation — the Heinlein case ──


async def test_id_rung_consolidates_split_persons_across_libraries(cross_lib):
    """The motivating Heinlein-gap case. Calibre has "Robert Heinlein"
    + goodreads_id=X; ABS has "Robert A. Heinlein" + goodreads_id=X.
    Names normalize differently → without slice 03 they'd end up on
    two persons. With slice 03 they resolve to the same person via
    the shared goodreads_id, link_confidence='high'."""
    cal_aid = await cross_lib["add_author"](
        "calibre-library", "Robert Heinlein", goodreads_id="GR-HEINLEIN",
    )
    abs_aid = await cross_lib["add_author"](
        "abs-audio-library", "Robert A. Heinlein",
        goodreads_id="GR-HEINLEIN",
    )
    # Calibre wins the first call → its person becomes the anchor.
    pid_cal = await get_or_create_person("calibre-library", cal_aid)
    # ABS's call walks the ID rung, finds Calibre's row by
    # goodreads_id, and reuses its person.
    pid_abs = await get_or_create_person("abs-audio-library", abs_aid)
    assert pid_cal == pid_abs
    assert await cross_lib["link_confidence"]("abs-audio-library", abs_aid) == "high"
    # No Case-4 conflicts surfaced from slice 03's clean path.
    assert await cross_lib["conflict_count"]() == 0


# ─── 2. Multi-ID confluence pointing to the same person ──────────


async def test_id_rung_multi_id_same_person_resolves_cleanly(cross_lib):
    """Calibre row carries goodreads_id + amazon_id. ABS row carries
    BOTH of those values. The ID rung counts 2 matches on the Calibre
    person, no other person, so the resolution is unambiguous."""
    cal_aid = await cross_lib["add_author"](
        "calibre-library", "Octavia Butler",
        goodreads_id="GR-OCTAVIA", amazon_id="B00OCTAVIA",
    )
    abs_aid = await cross_lib["add_author"](
        "abs-audio-library", "Octavia E. Butler",
        goodreads_id="GR-OCTAVIA", amazon_id="B00OCTAVIA",
    )
    pid_cal = await get_or_create_person("calibre-library", cal_aid)
    pid_abs = await get_or_create_person("abs-audio-library", abs_aid)
    assert pid_cal == pid_abs
    assert await cross_lib["link_confidence"]("abs-audio-library", abs_aid) == "high"


# ─── 3. Multi-ID ambiguity → drop to name rung ───────────────────


async def test_id_rung_ambiguity_drops_to_name_rung(cross_lib):
    """Two pre-existing persons each anchored to one source ID; THIS
    row carries both values. Tie on matches → ID rung punts to the
    name rung. The fixture rows in calibre have distinct normalized
    names so the name rung mints a fresh person — we assert the
    ambiguity row didn't get clobbered onto either pre-existing
    person, and that no conflict row was recorded (slice 03's
    documented deviation from the AC's suggestion)."""
    # Pre-existing: person A is anchored by goodreads_id via Calibre row.
    cal_a_aid = await cross_lib["add_author"](
        "calibre-library", "Alice Author A", goodreads_id="GR-AMBI",
    )
    pid_a = await get_or_create_person("calibre-library", cal_a_aid)
    # Pre-existing: person B is anchored by amazon_id via Calibre row B.
    cal_b_aid = await cross_lib["add_author"](
        "calibre-library", "Brad Author B", amazon_id="B00AMBI",
    )
    pid_b = await get_or_create_person("calibre-library", cal_b_aid)
    assert pid_a != pid_b

    # New ABS row carries BOTH IDs — tie → ambiguity → name rung.
    abs_aid = await cross_lib["add_author"](
        "abs-audio-library", "Cassandra Different Name",
        goodreads_id="GR-AMBI", amazon_id="B00AMBI",
    )
    pid_abs = await get_or_create_person("abs-audio-library", abs_aid)
    # Must NOT be either pre-existing person.
    assert pid_abs != pid_a
    assert pid_abs != pid_b
    # Slice 03 deliberately does NOT record a conflict row for
    # ambiguity (see docstring).
    assert await cross_lib["conflict_count"]() == 0


# ─── 4. Same-library ID match ─────────────────────────────────────


async def test_id_rung_matches_within_same_library(cross_lib):
    """Two Calibre rows carry the same goodreads_id (a pre-existing
    duplicate from a legacy import). Linking the first creates a
    person; linking the second must reuse it via the ID rung — the
    rung walks the SAME library too, not only other ones."""
    a1 = await cross_lib["add_author"](
        "calibre-library", "Duplicate One", goodreads_id="GR-DUP",
    )
    a2 = await cross_lib["add_author"](
        "calibre-library", "Duplicate Two", goodreads_id="GR-DUP",
    )
    pid_one = await get_or_create_person("calibre-library", a1)
    pid_two = await get_or_create_person("calibre-library", a2)
    assert pid_one == pid_two
    assert await cross_lib["link_confidence"]("calibre-library", a2) == "high"


# ─── 5. No source IDs → name rung still drives ────────────────────


async def test_no_source_ids_falls_to_name_rung_unchanged(cross_lib):
    """Both rows lack any source ID. The ID rung's populated_ids list
    is empty → it's skipped. The name rung's exact-normalized match
    still links them to the same person (pre-slice behavior)."""
    a1 = await cross_lib["add_author"](
        "calibre-library", "Brandon Sanderson",
    )
    a2 = await cross_lib["add_author"](
        "abs-audio-library", "Brandon Sanderson",
    )
    pid1 = await get_or_create_person("calibre-library", a1)
    pid2 = await get_or_create_person("abs-audio-library", a2)
    assert pid1 == pid2


# ─── 6. Mix — one populated source, one absent — still works ──────


async def test_unpopulated_columns_skipped_naturally(cross_lib):
    """Calibre row carries goodreads_id only. ABS row carries
    goodreads_id (same value) and ALSO has google_books_id NULL.
    The rung skips google_books_id naturally because it's not in
    populated_ids. The goodreads_id match drives the consolidation."""
    cal_aid = await cross_lib["add_author"](
        "calibre-library", "Iain M. Banks", goodreads_id="GR-BANKS",
    )
    abs_aid = await cross_lib["add_author"](
        "abs-audio-library", "Iain Banks", goodreads_id="GR-BANKS",
    )
    pid_cal = await get_or_create_person("calibre-library", cal_aid)
    pid_abs = await get_or_create_person("abs-audio-library", abs_aid)
    assert pid_cal == pid_abs


# ─── 7. Existing link short-circuits the whole resolver ──────────


async def test_existing_link_short_circuits_before_id_rung(cross_lib):
    """Idempotency regression — re-linking the same author returns
    the existing link's person_id, never reaches the ID rung. Catches
    any future change that re-orders Step 1 below Step 2.5."""
    aid = await cross_lib["add_author"](
        "calibre-library", "Existing Linkee", goodreads_id="GR-EXIST",
    )
    pid_first = await get_or_create_person("calibre-library", aid)
    pid_second = await get_or_create_person("calibre-library", aid)
    assert pid_first == pid_second


# ─── 8. Override-args path keeps slice-03 behavior ────────────────


async def test_override_args_still_runs_id_rung(cross_lib):
    """Sync-insert callers pass `name=` to avoid a second roundtrip.
    Slice 03 reads the per-library row anyway because it needs the
    source IDs — verify the ID rung still fires even when name is
    supplied via override."""
    cal_aid = await cross_lib["add_author"](
        "calibre-library", "Anchor Author", goodreads_id="GR-ANCHOR",
    )
    pid_cal = await get_or_create_person("calibre-library", cal_aid)
    abs_aid = await cross_lib["add_author"](
        "abs-audio-library", "Different Display Name",
        goodreads_id="GR-ANCHOR",
    )
    # name override differs from the stored row; ID rung must still
    # consult the stored row's goodreads_id and find the anchor.
    pid_abs = await get_or_create_person(
        "abs-audio-library", abs_aid,
        name="Caller Supplied Override",
    )
    assert pid_cal == pid_abs
