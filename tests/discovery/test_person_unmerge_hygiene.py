"""v3.10.0 — hygiene Job 14 ``job_person_unmerge`` tests.

The inverse of Job 9. Job 9 merges persons sharing a
``(source, source_id)``; when that ID was mis-stamped onto a co-authored
seed book's author row (the v3.6.2 incident class), it fuses two genuinely
different authors. This job splits them back apart and strips the bad ID.

Tests cover:
  - The Del Arroz case: a person holding its own pair PLUS another
    author's entire pair is split, and the intruder's two links land on
    ONE new person (not two — that would re-create the v2.20.0
    split-person bug).
  - The mis-stamped source ID shared with the retained rows is NULLed, so
    Job 9 cannot re-merge on the next pass. Measured on the reference
    install: all 25 groups shared at least one ID, i.e. without this the
    two jobs fight forever.
  - IDs unique to the detached row survive — they may be that author's own.
  - The stale-``normalized_name`` self-collision: 7 of 25 reference-install
    groups target the normalized name still held by the person being split
    (person 699 was canonical 'Jon Del Arroz' / normalized 'stick
    swinger'). The job repairs the source row first, then creates.
  - The four name pairs that must NOT split, including Mark's deliberate
    2026-05-23 hand-merge, which a fuzzy-only rule would undo.
  - ``link_source='manual'`` is never auto-split.
  - No-anchor guard: if EVERY link mismatches, leave the person alone.
  - Idempotency: a second run is a no-op.
  - Audit row shape + the documented reversal semantics.
"""
from __future__ import annotations

import aiosqlite
import pytest

from app import config, database
from app.discovery import author_identity, cross_library
from app.discovery import hygiene
from app.discovery.hygiene import _is_name_mismatch


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

SLUGS = ["calibre-library", "abs-audio-library"]


@pytest.fixture
async def env(tmp_path, monkeypatch):
    global_path = tmp_path / "seshat.db"
    monkeypatch.setattr(config, "APP_DB_PATH", global_path)
    monkeypatch.setattr(database, "APP_DB_PATH", global_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(author_identity, "DATA_DIR", tmp_path)

    await database.init_db()

    for slug in SLUGS:
        db = await aiosqlite.connect(str(tmp_path / f"seshat_{slug}.db"))
        try:
            await db.executescript(_PER_LIB_AUTHORS_DDL)
            await db.commit()
        finally:
            await db.close()

    monkeypatch.setattr(
        cross_library, "libraries_for",
        lambda _kind: [{"slug": s} for s in SLUGS],
    )

    async def add_author(slug: str, name: str, **ids) -> int:
        from app.metadata.author_names import normalize_author_name
        cols = ["name", "sort_name", "normalized_name"] + list(ids)
        vals = [name, name, normalize_author_name(name)] + list(ids.values())
        ph = ",".join("?" * len(cols))
        db = await aiosqlite.connect(str(tmp_path / f"seshat_{slug}.db"))
        try:
            cur = await db.execute(
                f"INSERT INTO authors ({', '.join(cols)}) VALUES ({ph})",
                vals,
            )
            await db.commit()
            return cur.lastrowid
        finally:
            await db.close()

    async def make_person(canonical: str, normalized: str | None = None) -> int:
        from app.metadata.author_names import normalize_author_name
        gdb = await aiosqlite.connect(str(global_path))
        try:
            cur = await gdb.execute(
                "INSERT INTO persons (canonical_name, normalized_name) "
                "VALUES (?, ?)",
                (canonical, normalized
                 if normalized is not None
                 else normalize_author_name(canonical)),
            )
            await gdb.commit()
            return cur.lastrowid
        finally:
            await gdb.close()

    async def link(pid: int, slug: str, aid: int, source: str = "auto") -> None:
        gdb = await aiosqlite.connect(str(global_path))
        try:
            await gdb.execute(
                "INSERT INTO author_links (person_id, library_slug, "
                "author_id, link_source) VALUES (?, ?, ?, ?)",
                (pid, slug, aid, source),
            )
            await gdb.commit()
        finally:
            await gdb.close()

    async def links_of(pid: int) -> list[tuple[str, int]]:
        gdb = await aiosqlite.connect(str(global_path))
        try:
            rows = await (await gdb.execute(
                "SELECT library_slug, author_id FROM author_links "
                "WHERE person_id = ? ORDER BY library_slug", (pid,),
            )).fetchall()
            return [(r[0], r[1]) for r in rows]
        finally:
            await gdb.close()

    async def person_for(slug: str, aid: int) -> int | None:
        gdb = await aiosqlite.connect(str(global_path))
        try:
            row = await (await gdb.execute(
                "SELECT person_id FROM author_links "
                "WHERE library_slug = ? AND author_id = ?", (slug, aid),
            )).fetchone()
            return row[0] if row else None
        finally:
            await gdb.close()

    async def person_row(pid: int) -> dict | None:
        gdb = await aiosqlite.connect(str(global_path))
        gdb.row_factory = aiosqlite.Row
        try:
            row = await (await gdb.execute(
                "SELECT id, canonical_name, normalized_name FROM persons "
                "WHERE id = ?", (pid,),
            )).fetchone()
            return dict(row) if row else None
        finally:
            await gdb.close()

    async def author_ids(slug: str, aid: int) -> dict:
        db = await aiosqlite.connect(str(tmp_path / f"seshat_{slug}.db"))
        db.row_factory = aiosqlite.Row
        try:
            row = await (await db.execute(
                "SELECT goodreads_id, amazon_id, hardcover_id "
                "FROM authors WHERE id = ?", (aid,),
            )).fetchone()
            return dict(row)
        finally:
            await db.close()

    async def audit() -> list[dict]:
        gdb = await aiosqlite.connect(str(global_path))
        gdb.row_factory = aiosqlite.Row
        try:
            rows = await (await gdb.execute(
                "SELECT winner_person_id, loser_person_id, reason, source, "
                "source_id, moved_links, loser_canonical_name "
                "FROM person_merges ORDER BY id"
            )).fetchall()
            return [dict(r) for r in rows]
        finally:
            await gdb.close()

    yield {
        "add_author": add_author, "make_person": make_person, "link": link,
        "links_of": links_of, "person_for": person_for,
        "person_row": person_row, "author_ids": author_ids, "audit": audit,
    }


def _stats() -> dict:
    return {
        "persons_unmerged": 0, "unmerge_links_detached": 0,
        "unmerge_source_ids_cleared": 0, "unmerge_norms_repaired": 0,
        "errors": [],
    }


# ─── 1. The split predicate ───────────────────────────────────


def test_keeps_the_four_validated_pairs():
    """These four were verified against live data. A fuzzy-only rule
    splits the first (Mark's deliberate 2026-05-23 hand-merge) and the
    second (~0.917, just under the 0.92 threshold)."""
    for lib, canon in [
        ("Tyler Burnworth", "Tyler E. C. Burnworth"),
        ("Aaron Bunce", "Aaron S. Bunce"),
        ("Kevin White", "W. Penn White"),
        ("Talia Beckett", "Talia Becket"),
    ]:
        assert not _is_name_mismatch(lib, canon), (lib, canon)


def test_detects_genuine_mismatches():
    for lib, canon in [
        ("Stick Swinger", "Jon Del Arroz"),
        ("Jeff Grubb", "Matt Forbeck"),
        ("Kerrie L. Hughes", "Jim Butcher"),
        ("Various", "Jack Bryce"),
    ]:
        assert _is_name_mismatch(lib, canon), (lib, canon)


def test_empty_names_never_split():
    assert not _is_name_mismatch("", "Jon Del Arroz")
    assert not _is_name_mismatch("Stick Swinger", "")


# ─── 2. The canonical Del Arroz split ─────────────────────────


async def test_del_arroz_case_splits_intruder_onto_one_person(env):
    """Person holds its own pair + Stick Swinger's entire pair. The two
    intruder links must land on ONE new person."""
    jda_c = await env["add_author"](
        "calibre-library", "Jon Del Arroz", goodreads_id="54844253")
    jda_a = await env["add_author"](
        "abs-audio-library", "Jon Del Arroz", goodreads_id="54844253")
    ss_c = await env["add_author"](
        "calibre-library", "Stick Swinger", goodreads_id="54844253")
    ss_a = await env["add_author"](
        "abs-audio-library", "Stick Swinger", goodreads_id="54844253")

    pid = await env["make_person"]("Jon Del Arroz")
    for slug, aid in [("calibre-library", jda_c), ("abs-audio-library", jda_a),
                      ("calibre-library", ss_c), ("abs-audio-library", ss_a)]:
        await env["link"](pid, slug, aid)

    stats = _stats()
    await hygiene.job_person_unmerge(stats)
    assert stats["errors"] == []

    assert stats["persons_unmerged"] == 1
    assert stats["unmerge_links_detached"] == 2

    # Del Arroz keeps his own pair.
    assert sorted(await env["links_of"](pid)) == sorted(
        [("calibre-library", jda_c), ("abs-audio-library", jda_a)])

    # Both Stick Swinger links landed on the SAME new person.
    p_c = await env["person_for"]("calibre-library", ss_c)
    p_a = await env["person_for"]("abs-audio-library", ss_a)
    assert p_c is not None and p_c == p_a and p_c != pid
    assert (await env["person_row"](p_c))["canonical_name"] == "Stick Swinger"


async def test_shared_source_id_is_stripped_from_detached_rows(env):
    """Without this Job 9 re-merges on the next pass — every one of the
    25 reference-install groups shared at least one ID."""
    jda_c = await env["add_author"](
        "calibre-library", "Jon Del Arroz", goodreads_id="54844253")
    ss_c = await env["add_author"](
        "calibre-library", "Stick Swinger",
        goodreads_id="54844253", amazon_id="B0OWNONLY")
    ss_a = await env["add_author"](
        "abs-audio-library", "Stick Swinger", goodreads_id="54844253")

    pid = await env["make_person"]("Jon Del Arroz")
    for slug, aid in [("calibre-library", jda_c), ("calibre-library", ss_c),
                      ("abs-audio-library", ss_a)]:
        await env["link"](pid, slug, aid)

    stats = _stats()
    await hygiene.job_person_unmerge(stats)

    ids = await env["author_ids"]("calibre-library", ss_c)
    assert ids["goodreads_id"] is None          # shared -> stripped
    assert ids["amazon_id"] == "B0OWNONLY"      # unique -> kept
    # The retained row is untouched.
    assert (await env["author_ids"](
        "calibre-library", jda_c))["goodreads_id"] == "54844253"
    assert stats["unmerge_source_ids_cleared"] >= 1


async def test_stale_normalized_name_is_repaired_then_reused(env):
    """The 7-of-25 self-collision: the person being split still carries
    the detached group's normalized_name from the old merge."""
    jda_c = await env["add_author"]("calibre-library", "Jon Del Arroz")
    ss_c = await env["add_author"]("calibre-library", "Stick Swinger")
    ss_a = await env["add_author"]("abs-audio-library", "Stick Swinger")

    # canonical says Del Arroz, normalized still says stick swinger
    pid = await env["make_person"]("Jon Del Arroz", normalized="stick swinger")
    for slug, aid in [("calibre-library", jda_c), ("calibre-library", ss_c),
                      ("abs-audio-library", ss_a)]:
        await env["link"](pid, slug, aid)

    stats = _stats()
    await hygiene.job_person_unmerge(stats)
    assert stats["errors"] == []
    assert stats["unmerge_norms_repaired"] == 1

    assert (await env["person_row"](pid))["normalized_name"] == "jon del arroz"
    new_pid = await env["person_for"]("calibre-library", ss_c)
    assert new_pid != pid
    assert (await env["person_row"](new_pid))["normalized_name"] == "stick swinger"


# ─── 3. Guards ────────────────────────────────────────────────


async def test_manual_links_are_never_auto_split(env):
    a = await env["add_author"]("calibre-library", "Tyler Burnworth")
    b = await env["add_author"]("abs-audio-library", "Completely Different")
    pid = await env["make_person"]("Tyler Burnworth")
    await env["link"](pid, "calibre-library", a)
    await env["link"](pid, "abs-audio-library", b, source="manual")

    stats = _stats()
    await hygiene.job_person_unmerge(stats)
    assert stats["persons_unmerged"] == 0
    assert len(await env["links_of"](pid)) == 2


async def test_no_anchor_means_no_split(env):
    """If EVERY link mismatches there is no trustworthy identity to keep
    — that's a wrong canonical_name, a different pathology."""
    a = await env["add_author"]("calibre-library", "Alice Author")
    b = await env["add_author"]("abs-audio-library", "Bob Writer")
    pid = await env["make_person"]("Zoe Nobody")
    await env["link"](pid, "calibre-library", a)
    await env["link"](pid, "abs-audio-library", b)

    stats = _stats()
    await hygiene.job_person_unmerge(stats)
    assert stats["persons_unmerged"] == 0
    assert stats["unmerge_links_detached"] == 0
    assert len(await env["links_of"](pid)) == 2


async def test_single_link_person_untouched(env):
    a = await env["add_author"]("calibre-library", "Solo Author")
    pid = await env["make_person"]("Someone Else")
    await env["link"](pid, "calibre-library", a)

    stats = _stats()
    await hygiene.job_person_unmerge(stats)
    assert stats["persons_unmerged"] == 0


async def test_clean_person_is_a_no_op(env):
    c = await env["add_author"]("calibre-library", "Brandon Sanderson")
    a = await env["add_author"]("abs-audio-library", "Brandon Sanderson")
    pid = await env["make_person"]("Brandon Sanderson")
    await env["link"](pid, "calibre-library", c)
    await env["link"](pid, "abs-audio-library", a)

    stats = _stats()
    await hygiene.job_person_unmerge(stats)
    assert stats["persons_unmerged"] == 0
    assert stats["unmerge_links_detached"] == 0
    assert await env["audit"]() == []


# ─── 4. Idempotency + audit ───────────────────────────────────


async def test_second_run_is_a_no_op(env):
    jda = await env["add_author"](
        "calibre-library", "Jon Del Arroz", goodreads_id="54844253")
    ss_c = await env["add_author"](
        "calibre-library", "Stick Swinger", goodreads_id="54844253")
    ss_a = await env["add_author"](
        "abs-audio-library", "Stick Swinger", goodreads_id="54844253")
    pid = await env["make_person"]("Jon Del Arroz")
    for slug, aid in [("calibre-library", jda), ("calibre-library", ss_c),
                      ("abs-audio-library", ss_a)]:
        await env["link"](pid, slug, aid)

    first = _stats()
    await hygiene.job_person_unmerge(first)
    assert first["unmerge_links_detached"] == 2

    second = _stats()
    await hygiene.job_person_unmerge(second)
    assert second["persons_unmerged"] == 0
    assert second["unmerge_links_detached"] == 0
    assert len(await env["audit"]()) == 1


async def test_audit_row_supports_reversal(env):
    """`winner` HOLDS the detached links, `loser` gave them up (and,
    unlike a merge, still exists). Reversal = move winner's links back."""
    jda = await env["add_author"]("calibre-library", "Jon Del Arroz")
    ss = await env["add_author"]("calibre-library", "Stick Swinger")
    a2 = await env["add_author"]("abs-audio-library", "Stick Swinger")
    pid = await env["make_person"]("Jon Del Arroz")
    for slug, aid in [("calibre-library", jda), ("calibre-library", ss),
                      ("abs-audio-library", a2)]:
        await env["link"](pid, slug, aid)

    await hygiene.job_person_unmerge(_stats())

    rows = await env["audit"]()
    assert len(rows) == 1
    r = rows[0]
    assert r["reason"] == "unmerge_name_mismatch"
    assert r["loser_person_id"] == pid
    assert r["loser_canonical_name"] == "Jon Del Arroz"
    assert r["source"] == "name_mismatch"
    assert r["source_id"] == "Stick Swinger"
    assert r["moved_links"] == 2
    assert r["winner_person_id"] == await env["person_for"](
        "calibre-library", ss)
    # loser still exists — the split did not delete it
    assert await env["person_row"](pid) is not None


# ─── 5. Registration ──────────────────────────────────────────


def test_job_is_registered_as_job_14():
    assert hygiene.TOTAL_JOBS == 14
    assert hygiene.JOB_NAMES[13] == "Person un-merge"
