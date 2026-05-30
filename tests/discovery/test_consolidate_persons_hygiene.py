"""v3.x (ADR-0015 slice 05) — hygiene job
``job_consolidate_persons_by_source_id`` tests.

The job walks every linked author row's mirrorable source-ID columns,
groups by (source, source_id) → set of person_ids, and merges every
multi-person group onto the lowest person_id. Each merge moves the
loser's author_links onto the winner, deletes the loser's persons
row, and records an audit row in ``person_merges``.

Tests cover:
  - Heinlein gap consolidation across two libraries (canonical case).
  - Idempotency: a second run is a no-op.
  - No source IDs in DB → zero merges, zero person_merges rows.
  - Single-person groups skipped (correct group → 0 merges).
  - Multi-iteration coalescence: the same loser participating in two
    (source, source_id) groups is folded onto one winner once; the
    second iteration finds the loser already gone and skips.
  - Audit row shape: winner, loser, source, source_id, moved_links,
    loser_canonical_name all present.

The fixture mirrors the slice-03 setup pattern: two per-library DBs
behind the production DATA_DIR; ``app.discovery.cross_library`` is
monkeypatched so the job's ``libraries_for("all")`` call sees our
test slugs.
"""
from __future__ import annotations

import aiosqlite
import pytest

from app import config, database
from app.discovery import author_identity, cross_library
from app.discovery import hygiene
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
async def hygiene_env(tmp_path, monkeypatch):
    """cwa-library + abs-audio-library per-library DBs + global DB
    with the slice-05 schema (person_merges) initialized."""
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

    # Monkeypatch the cross_library library lister so the hygiene job
    # finds our test slugs (instead of state._discovered_libraries
    # which is empty in unit tests).
    monkeypatch.setattr(
        cross_library, "libraries_for",
        lambda _kind: [{"slug": s} for s in slugs],
    )

    async def add_author(slug: str, name: str, **cols) -> int:
        from app.metadata.author_names import normalize_author_name
        normalized = cols.pop("normalized_name", normalize_author_name(name))
        valid = {
            "amazon_id", "goodreads_id", "hardcover_id", "kobo_id",
            "ibdb_id", "google_books_id", "openlibrary_id",
            "audible_id", "fictiondb_id",
        }
        extra = [c for c in cols if c in valid]
        sql_cols = ["name", "sort_name", "normalized_name"] + extra
        sql_vals = [name, name, normalized] + [cols[c] for c in extra]
        ph = ",".join("?" * len(sql_cols))
        db = await aiosqlite.connect(
            str(tmp_path / f"seshat_{slug}.db")
        )
        try:
            cur = await db.execute(
                f"INSERT INTO authors ({', '.join(sql_cols)}) "
                f"VALUES ({ph})",
                sql_vals,
            )
            await db.commit()
            return cur.lastrowid
        finally:
            await db.close()

    async def link(slug: str, aid: int, *, name: str | None = None) -> int:
        """Force-create a fresh person+link by going directly through
        get_or_create_person. Slice 03's ID rung is intentionally
        active here — most tests set up *with* IDs and then expect the
        person ladder to mint per-author persons so the hygiene job
        has multiple persons to merge. To get that, we patch the
        per-library row to add the source ID AFTER linking."""
        return await get_or_create_person(slug, aid, name=name)

    async def add_author_then_id(
        slug: str, name: str, *, after_link_ids: dict[str, str] | None = None,
    ) -> tuple[int, int]:
        """Add an author with no source IDs, link to mint a person,
        then back-fill the source IDs on the per-library row. This
        defeats slice 03's runtime ID-first matching (the IDs aren't
        there at link time), giving us pre-existing split persons
        that slice 05 should merge."""
        aid = await add_author(slug, name)
        pid = await link(slug, aid, name=name)
        if after_link_ids:
            db = await aiosqlite.connect(
                str(tmp_path / f"seshat_{slug}.db")
            )
            try:
                sets = ", ".join(f"{c} = ?" for c in after_link_ids)
                await db.execute(
                    f"UPDATE authors SET {sets} WHERE id = ?",
                    (*after_link_ids.values(), aid),
                )
                await db.commit()
            finally:
                await db.close()
        return aid, pid

    async def count_persons() -> int:
        gdb = await aiosqlite.connect(str(global_path))
        try:
            row = await (await gdb.execute(
                "SELECT COUNT(*) FROM persons"
            )).fetchone()
            return row[0]
        finally:
            await gdb.close()

    async def person_for(slug: str, aid: int) -> int | None:
        gdb = await aiosqlite.connect(str(global_path))
        gdb.row_factory = aiosqlite.Row
        try:
            row = await (await gdb.execute(
                "SELECT person_id FROM author_links "
                "WHERE library_slug = ? AND author_id = ?",
                (slug, aid),
            )).fetchone()
            return row["person_id"] if row else None
        finally:
            await gdb.close()

    async def list_audit() -> list[dict]:
        gdb = await aiosqlite.connect(str(global_path))
        gdb.row_factory = aiosqlite.Row
        try:
            rows = await (await gdb.execute(
                "SELECT winner_person_id, loser_person_id, reason, "
                "       source, source_id, moved_links, "
                "       loser_canonical_name "
                "FROM person_merges ORDER BY id"
            )).fetchall()
            return [dict(r) for r in rows]
        finally:
            await gdb.close()

    yield {
        "add_author": add_author,
        "link": link,
        "add_author_then_id": add_author_then_id,
        "count_persons": count_persons,
        "person_for": person_for,
        "list_audit": list_audit,
    }


def _stats() -> dict:
    return {
        "persons_merged_by_source_id": 0,
        "persons_merge_ambiguous": 0,
        "errors": [],
    }


# ─── 1. Heinlein-gap consolidation ─────────────────────────────


async def test_consolidates_split_persons_across_libraries(hygiene_env):
    """Two persons each anchored to their own library row, both
    carrying the same goodreads_id. The job groups by goodreads_id,
    finds 2 persons, merges loser → winner (lowest pid).

    Uses clearly-distinct names (no fuzzy-name collision) so that
    get_or_create_person mints two separate persons at link time —
    this is the **pre-slice-03** state we're modelling, with the
    backfill happening here. "Robert Heinlein" + "Robert A. Heinlein"
    would fuzzy-match via the runtime ladder, so the synthetic shape
    uses different-enough names instead."""
    _cal_aid, pid_cal = await hygiene_env["add_author_then_id"](
        "calibre-library", "Alpha Penname",
        after_link_ids={"goodreads_id": "GR-SHARED"},
    )
    abs_aid, pid_abs = await hygiene_env["add_author_then_id"](
        "abs-audio-library", "Beta Realname",
        after_link_ids={"goodreads_id": "GR-SHARED"},
    )
    assert pid_cal != pid_abs  # premise: two distinct persons

    stats = _stats()
    await hygiene.job_consolidate_persons_by_source_id(stats)
    assert stats["persons_merged_by_source_id"] == 1

    # Both author rows now link to the lowest of the two original pids.
    winner = min(pid_cal, pid_abs)
    loser = max(pid_cal, pid_abs)
    assert await hygiene_env["person_for"]("calibre-library", _cal_aid) == winner
    assert await hygiene_env["person_for"]("abs-audio-library", abs_aid) == winner
    # Loser persons row deleted.
    assert await hygiene_env["count_persons"]() == 1

    audit = await hygiene_env["list_audit"]()
    assert len(audit) == 1
    a = audit[0]
    assert a["winner_person_id"] == winner
    assert a["loser_person_id"] == loser
    assert a["reason"] == "consolidate_by_source_id"
    assert a["source"] == "goodreads"
    assert a["source_id"] == "GR-SHARED"
    assert a["moved_links"] == 1
    assert a["loser_canonical_name"]


# ─── 2. Idempotency — second run is a no-op ────────────────────


async def test_idempotent_second_run(hygiene_env):
    await hygiene_env["add_author_then_id"](
        "calibre-library", "Alpha Penname",
        after_link_ids={"goodreads_id": "GR-SHARED"},
    )
    await hygiene_env["add_author_then_id"](
        "abs-audio-library", "Beta Realname",
        after_link_ids={"goodreads_id": "GR-SHARED"},
    )
    stats1 = _stats()
    await hygiene.job_consolidate_persons_by_source_id(stats1)
    assert stats1["persons_merged_by_source_id"] == 1
    audit_after_first = await hygiene_env["list_audit"]()

    stats2 = _stats()
    await hygiene.job_consolidate_persons_by_source_id(stats2)
    assert stats2["persons_merged_by_source_id"] == 0
    audit_after_second = await hygiene_env["list_audit"]()
    # Audit table unchanged — second run wrote nothing.
    assert audit_after_second == audit_after_first


# ─── 3. No source IDs in DB → zero merges, zero audit rows ─────


async def test_no_source_ids_in_db_is_noop(hygiene_env):
    """Both author rows carry no source IDs; the job finds no
    groups → zero merges. (The fixture's persons exist because
    get_or_create_person ran, but they're already collapsed by the
    name rung onto one person if names match — so we use DISTINCT
    names to ensure two persons.)"""
    cal_aid, pid_cal = await hygiene_env["add_author_then_id"](
        "calibre-library", "Alice Author",
    )
    abs_aid, pid_abs = await hygiene_env["add_author_then_id"](
        "abs-audio-library", "Bob Bookwright",
    )
    assert pid_cal != pid_abs
    stats = _stats()
    await hygiene.job_consolidate_persons_by_source_id(stats)
    assert stats["persons_merged_by_source_id"] == 0
    assert await hygiene_env["list_audit"]() == []
    # Both persons still here.
    assert await hygiene_env["person_for"]("calibre-library", cal_aid) == pid_cal
    assert await hygiene_env["person_for"]("abs-audio-library", abs_aid) == pid_abs


# ─── 4. Single-person group skipped ────────────────────────────


async def test_single_person_group_skipped(hygiene_env):
    """One author row carries a goodreads_id, no other row does. The
    (goodreads, value) group has 1 person → no merge."""
    cal_aid, pid_cal = await hygiene_env["add_author_then_id"](
        "calibre-library", "Solo Author",
        after_link_ids={"goodreads_id": "GR-SOLO"},
    )
    stats = _stats()
    await hygiene.job_consolidate_persons_by_source_id(stats)
    assert stats["persons_merged_by_source_id"] == 0
    assert await hygiene_env["person_for"]("calibre-library", cal_aid) == pid_cal


# ─── 5. Multi-iteration coalescence — the same loser ───────────


async def test_multi_iteration_coalescence(hygiene_env):
    """Three persons share TWO source IDs in a chain: A has
    goodreads_id=X (shared with B), B has amazon_id=Y (shared with C).
    Walking the job in deterministic order, the goodreads group
    merges B→A, and then the amazon group sees only one person on B's
    row (which is now A's). The result: all three end up on the
    lowest pid. Exactly 2 merge audit rows."""
    cal_a, pid_a = await hygiene_env["add_author_then_id"](
        "calibre-library", "Author A",
        after_link_ids={"goodreads_id": "GR-X"},
    )
    cal_b, pid_b = await hygiene_env["add_author_then_id"](
        "calibre-library", "Author B",
        after_link_ids={"goodreads_id": "GR-X", "amazon_id": "B00-Y"},
    )
    cal_c, pid_c = await hygiene_env["add_author_then_id"](
        "calibre-library", "Author C",
        after_link_ids={"amazon_id": "B00-Y"},
    )
    assert len({pid_a, pid_b, pid_c}) == 3

    stats = _stats()
    await hygiene.job_consolidate_persons_by_source_id(stats)
    winner = min(pid_a, pid_b, pid_c)
    assert await hygiene_env["person_for"]("calibre-library", cal_a) == winner
    assert await hygiene_env["person_for"]("calibre-library", cal_b) == winner
    assert await hygiene_env["person_for"]("calibre-library", cal_c) == winner
    # Only one person left.
    assert await hygiene_env["count_persons"]() == 1
    # 2 merges (3 persons → 1; lowest absorbed two losers).
    assert stats["persons_merged_by_source_id"] == 2
    audit = await hygiene_env["list_audit"]()
    assert len(audit) == 2
    # Both audit rows have the same winner.
    assert {a["winner_person_id"] for a in audit} == {winner}


# ─── 6. Job exists in the catalogue under the documented name ──


def test_job_listed_in_catalogue():
    assert "Consolidate persons by shared source ID" in hygiene.JOB_NAMES
    # Inserted at index 8 (between Cross-library person backfill and
    # Prune orphan author links).
    assert hygiene.JOB_NAMES[8] == "Consolidate persons by shared source ID"
    assert hygiene.TOTAL_JOBS == 11
