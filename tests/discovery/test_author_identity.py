"""
Tests for cross-library author identity (v2.20.0 Phase 1).

Covers:
  - get_or_create_person creates persons + author_links
  - get_or_create_person reuses existing person by normalized_name
  - get_or_create_person is idempotent (re-linking returns same id)
  - mirror_source_id refuses non-mirrorable columns
  - mirror_source_id writes to every linked per-library row
  - mirror_source_id is a no-op when the caller isn't linked
  - migrate_to_cross_library_identity walks every library, populates
    persons + author_links, runs the consolidation tiebreak
  - migration is idempotent (re-run with same data is a no-op)
  - low-confidence flagging triggers when two libraries share a
    normalized_name with NO source-ID overlap
  - pen_name_links → pen_name_links_v2 cross-library promotion
  - prune_orphan_links drops dangling link rows

These tests bypass the production init_db / migration system and build
fresh per-library DBs directly so the test environment is fully self-
contained — no DATA_DIR pollution, no startup-order assumptions.
"""
from __future__ import annotations

import aiosqlite
import pytest

from app import config, database
from app.discovery import author_identity
from app.discovery.author_identity import (
    KNOWN_SOURCE_ID_COLUMNS,
    MIRRORABLE_SOURCE_ID_COLUMNS,
    get_or_create_person,
    linked_authors,
    mirror_source_id,
    migrate_to_cross_library_identity,
    person_id_for,
    prune_orphan_links,
)


# Minimal per-library `authors` table schema for tests. Mirrors the
# real schema's source-ID columns plus the bio / image_url / norm
# fields the migration reads. Skips columns the migration doesn't
# touch.
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
CREATE TABLE books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author_id INTEGER REFERENCES authors(id)
);
CREATE TABLE pen_name_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_author_id INTEGER NOT NULL,
    alias_author_id INTEGER NOT NULL,
    link_type TEXT NOT NULL DEFAULT 'pen_name'
);
"""


# ─── Fixtures ────────────────────────────────────────────────


@pytest.fixture
async def cross_lib_env(tmp_path, monkeypatch):
    """Set up two per-library DBs + a global seshat.db, all monkey-
    patched onto the production DATA_DIR / APP_DB_PATH constants.

    Yields a dict with helper functions tests can use to add authors,
    add books, add pen-name links, etc.
    """
    # Monkey-patch the global APP_DB_PATH so author_identity.get_global_db
    # talks to our temp file.
    global_path = tmp_path / "seshat.db"
    monkeypatch.setattr(config, "APP_DB_PATH", global_path)
    monkeypatch.setattr(database, "APP_DB_PATH", global_path)

    # Monkey-patch DATA_DIR so author_identity._per_library_db_path
    # finds the per-library files.
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(author_identity, "DATA_DIR", tmp_path)

    # Init the global schema (creates persons + author_links + ...).
    await database.init_db()

    # Create per-library DBs at the monkey-patched DATA_DIR location.
    slugs = ["calibre-library", "abs-audio-library"]
    for slug in slugs:
        path = tmp_path / f"seshat_{slug}.db"
        db = await aiosqlite.connect(str(path))
        await db.executescript(_PER_LIB_AUTHORS_DDL)
        await db.commit()
        await db.close()

    async def add_author(
        slug: str, name: str, *,
        normalized_name: str | None = None,
        bio: str | None = None,
        image_url: str | None = None,
        amazon_id: str | None = None,
        goodreads_id: str | None = None,
        hardcover_id: str | None = None,
    ) -> int:
        """Insert an author row into a per-library DB. Returns
        author_id."""
        from app.metadata.author_names import normalize_author_name
        if normalized_name is None:
            normalized_name = normalize_author_name(name)
        path = tmp_path / f"seshat_{slug}.db"
        db = await aiosqlite.connect(str(path))
        try:
            cur = await db.execute(
                "INSERT INTO authors "
                "(name, normalized_name, bio, image_url, "
                "amazon_id, goodreads_id, hardcover_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, normalized_name, bio, image_url,
                 amazon_id, goodreads_id, hardcover_id),
            )
            await db.commit()
            return cur.lastrowid
        finally:
            await db.close()

    async def add_book(slug: str, title: str, author_id: int) -> int:
        path = tmp_path / f"seshat_{slug}.db"
        db = await aiosqlite.connect(str(path))
        try:
            cur = await db.execute(
                "INSERT INTO books (title, author_id) VALUES (?, ?)",
                (title, author_id),
            )
            await db.commit()
            return cur.lastrowid
        finally:
            await db.close()

    async def add_pen_link(
        slug: str, canonical_aid: int, alias_aid: int, link_type: str = "pen_name",
    ) -> None:
        path = tmp_path / f"seshat_{slug}.db"
        db = await aiosqlite.connect(str(path))
        try:
            await db.execute(
                "INSERT INTO pen_name_links "
                "(canonical_author_id, alias_author_id, link_type) "
                "VALUES (?, ?, ?)",
                (canonical_aid, alias_aid, link_type),
            )
            await db.commit()
        finally:
            await db.close()

    async def read_amazon_id(slug: str, author_id: int) -> str | None:
        path = tmp_path / f"seshat_{slug}.db"
        db = await aiosqlite.connect(str(path))
        try:
            cur = await db.execute(
                "SELECT amazon_id FROM authors WHERE id = ?", (author_id,),
            )
            row = await cur.fetchone()
            return row[0] if row else None
        finally:
            await db.close()

    yield {
        "tmp_path": tmp_path,
        "slugs": slugs,
        "add_author": add_author,
        "add_book": add_book,
        "add_pen_link": add_pen_link,
        "read_amazon_id": read_amazon_id,
    }


# ─── Static checks ───────────────────────────────────────────


class TestConstants:
    def test_mirrorable_is_subset_of_known(self):
        assert MIRRORABLE_SOURCE_ID_COLUMNS.issubset(KNOWN_SOURCE_ID_COLUMNS)

    def test_local_sync_ids_not_mirrorable(self):
        assert "audiobookshelf_id" not in MIRRORABLE_SOURCE_ID_COLUMNS
        assert "calibre_id" not in MIRRORABLE_SOURCE_ID_COLUMNS

    def test_web_sources_are_mirrorable(self):
        for col in ("amazon_id", "goodreads_id", "hardcover_id",
                    "kobo_id", "openlibrary_id"):
            assert col in MIRRORABLE_SOURCE_ID_COLUMNS


# ─── get_or_create_person ────────────────────────────────────


class TestGetOrCreatePerson:
    async def test_creates_person_and_link_for_new_author(self, cross_lib_env):
        aid = await cross_lib_env["add_author"]("calibre-library", "Brandon Sanderson")
        pid = await get_or_create_person("calibre-library", aid)
        assert pid > 0
        # Link should exist.
        looked_up = await person_id_for("calibre-library", aid)
        assert looked_up == pid

    async def test_reuses_person_across_libraries_by_normalized_name(self, cross_lib_env):
        aid1 = await cross_lib_env["add_author"]("calibre-library", "J. N. Chaney")
        aid2 = await cross_lib_env["add_author"]("abs-audio-library", "J.N. Chaney")
        pid1 = await get_or_create_person("calibre-library", aid1)
        pid2 = await get_or_create_person("abs-audio-library", aid2)
        # Same normalized_name → same person.
        assert pid1 == pid2

    async def test_idempotent_relink(self, cross_lib_env):
        aid = await cross_lib_env["add_author"]("calibre-library", "Brandon Sanderson")
        pid_first = await get_or_create_person("calibre-library", aid)
        pid_second = await get_or_create_person("calibre-library", aid)
        assert pid_first == pid_second
        # Only one link should exist.
        links = await linked_authors(pid_first)
        assert len(links) == 1

    async def test_links_both_libraries_for_same_person(self, cross_lib_env):
        aid1 = await cross_lib_env["add_author"]("calibre-library", "William D. Arand")
        aid2 = await cross_lib_env["add_author"]("abs-audio-library", "William D. Arand")
        pid1 = await get_or_create_person("calibre-library", aid1)
        pid2 = await get_or_create_person("abs-audio-library", aid2)
        assert pid1 == pid2
        links = sorted(await linked_authors(pid1))
        assert links == sorted([
            ("calibre-library", aid1),
            ("abs-audio-library", aid2),
        ])


# ─── mirror_source_id ────────────────────────────────────────


class TestMirrorSourceId:
    async def test_refuses_unmirrorable_column(self, cross_lib_env):
        aid = await cross_lib_env["add_author"]("calibre-library", "Test Author")
        await get_or_create_person("calibre-library", aid)
        with pytest.raises(ValueError, match="MIRRORABLE"):
            await mirror_source_id("calibre-library", aid, "audiobookshelf_id", "abs-123")
        with pytest.raises(ValueError, match="MIRRORABLE"):
            await mirror_source_id("calibre-library", aid, "calibre_id", "999")

    async def test_refuses_unknown_column(self, cross_lib_env):
        aid = await cross_lib_env["add_author"]("calibre-library", "Test Author")
        await get_or_create_person("calibre-library", aid)
        with pytest.raises(ValueError):
            await mirror_source_id("calibre-library", aid, "made_up_id", "x")

    async def test_writes_to_other_linked_libraries(self, cross_lib_env):
        """v2.20.1 — mirror writes to OTHER libraries (excluding the
        caller's own slug). The caller is expected to have already
        written its row; re-writing via a second connection would
        deadlock against the caller's still-open write transaction."""
        aid1 = await cross_lib_env["add_author"]("calibre-library", "William D. Arand")
        aid2 = await cross_lib_env["add_author"]("abs-audio-library", "William D. Arand")
        await get_or_create_person("calibre-library", aid1)
        await get_or_create_person("abs-audio-library", aid2)

        # Simulate the caller having already written its own row.
        # (In production this is the `UPDATE authors SET {source}_id`
        # immediately preceding the mirror call.)
        from app.discovery.author_identity import _open_per_library
        cdb = await _open_per_library("calibre-library")
        try:
            await cdb.execute(
                "UPDATE authors SET amazon_id=? WHERE id=?",
                ("B01AY7PSG4", aid1),
            )
            await cdb.commit()
        finally:
            await cdb.close()

        touched = await mirror_source_id(
            "calibre-library", aid1, "amazon_id", "B01AY7PSG4",
        )
        # Caller's slug skipped; ONE other library touched.
        assert touched == 1

        v1 = await cross_lib_env["read_amazon_id"]("calibre-library", aid1)
        v2 = await cross_lib_env["read_amazon_id"]("abs-audio-library", aid2)
        assert v1 == "B01AY7PSG4"
        assert v2 == "B01AY7PSG4"

    async def test_does_not_deadlock_against_open_caller_write(
        self, cross_lib_env,
    ):
        """v2.20.1 regression — when the caller holds an open write
        transaction on its per-library DB (the lookup.py pattern), the
        mirror MUST NOT try to re-open + write the same DB or it'll
        busy-timeout against the caller's lock. This test reproduces
        the original "database is locked" error from v2.20.0."""
        aid1 = await cross_lib_env["add_author"]("calibre-library", "William D. Arand")
        aid2 = await cross_lib_env["add_author"]("abs-audio-library", "William D. Arand")
        await get_or_create_person("calibre-library", aid1)
        await get_or_create_person("abs-audio-library", aid2)

        # Open a write transaction on calibre-library and DO NOT commit.
        # The mirror is invoked while this connection still holds the
        # write lock — pre-fix behavior was to deadlock + raise; post-
        # fix the mirror skips the caller's slug and completes cleanly.
        from app.discovery.author_identity import _open_per_library
        cdb = await _open_per_library("calibre-library")
        try:
            # Lower busy_timeout so a hypothetical regression fails
            # this test in milliseconds, not 30 seconds.
            await cdb.execute("PRAGMA busy_timeout=500")
            await cdb.execute(
                "UPDATE authors SET amazon_id=? WHERE id=?",
                ("B01AY7PSG4", aid1),
            )
            # NOTE: no commit here — write lock held.
            touched = await mirror_source_id(
                "calibre-library", aid1, "amazon_id", "B01AY7PSG4",
            )
            assert touched == 1  # only abs-audio-library
            await cdb.commit()
        finally:
            await cdb.close()

        # The other library got the value.
        assert await cross_lib_env["read_amazon_id"]("abs-audio-library", aid2) == "B01AY7PSG4"

    async def test_mirror_handles_null(self, cross_lib_env):
        """Mirror with value=None clears the column on every OTHER
        linked row (caller's row left alone — caller wrote its own
        NULL via its own connection)."""
        aid1 = await cross_lib_env["add_author"](
            "calibre-library", "Wataru Watari", amazon_id="B0GZYW93RP",
        )
        aid2 = await cross_lib_env["add_author"](
            "abs-audio-library", "Wataru Watari", amazon_id="B0GZYW93RP",
        )
        await get_or_create_person("calibre-library", aid1)
        await get_or_create_person("abs-audio-library", aid2)

        await mirror_source_id("calibre-library", aid1, "amazon_id", None)
        # Caller's row untouched (caller would have NULLed it themselves).
        assert await cross_lib_env["read_amazon_id"]("calibre-library", aid1) == "B0GZYW93RP"
        # Mirror cleared the OTHER library's value.
        assert await cross_lib_env["read_amazon_id"]("abs-audio-library", aid2) is None

    async def test_noop_when_not_linked(self, cross_lib_env):
        """If the caller's row hasn't been linked yet (pre-migration
        state), mirror returns 0 without raising."""
        aid = await cross_lib_env["add_author"]("calibre-library", "Unlinked Author")
        # NOTE: we deliberately did NOT call get_or_create_person here.
        touched = await mirror_source_id("calibre-library", aid, "amazon_id", "B0TEST00")
        assert touched == 0

    async def test_accepts_unsuffixed_source_name(self, cross_lib_env):
        """Convenience: 'amazon' is normalized to 'amazon_id' internally.
        Single-library author has no OTHER linked rows to mirror to
        (caller's own slug is skipped), so touched=0."""
        aid = await cross_lib_env["add_author"]("calibre-library", "Test Author")
        await get_or_create_person("calibre-library", aid)
        touched = await mirror_source_id("calibre-library", aid, "amazon", "B0TEST00")
        # Caller's slug skipped; no other linked libraries → 0 rows touched.
        assert touched == 0


# ─── migrate_to_cross_library_identity ───────────────────────


class TestMigration:
    async def test_walks_libraries_creates_persons(self, cross_lib_env):
        await cross_lib_env["add_author"]("calibre-library", "Brandon Sanderson")
        await cross_lib_env["add_author"]("calibre-library", "William D. Arand")
        await cross_lib_env["add_author"]("abs-audio-library", "Brandon Sanderson")

        result = await migrate_to_cross_library_identity(cross_lib_env["slugs"])
        assert not result["skipped"]
        # 2 unique persons (Sanderson + Arand), 3 author_links.
        assert result["created_persons"] == 2
        assert result["created_links"] == 3

    async def test_normalization_unifies_initial_variants(self, cross_lib_env):
        """`J. N. Chaney` and `J.N. Chaney` MUST collapse to one person
        despite the per-library normalized_name being computed
        differently at insert time."""
        await cross_lib_env["add_author"](
            "calibre-library", "J. N. Chaney",
            # Force a "wrong" stored normalized_name to prove the
            # migration re-normalizes it. The real normalize would
            # produce "jn chaney".
            normalized_name="j.n. chaney (wrong)",
        )
        await cross_lib_env["add_author"](
            "abs-audio-library", "J.N. Chaney",
            normalized_name="something else (wrong)",
        )
        result = await migrate_to_cross_library_identity(cross_lib_env["slugs"])
        assert result["created_persons"] == 1
        assert result["created_links"] == 2

    async def test_migration_is_idempotent(self, cross_lib_env):
        await cross_lib_env["add_author"]("calibre-library", "Brandon Sanderson")
        await migrate_to_cross_library_identity(cross_lib_env["slugs"])
        # Re-run should detect "already linked" and skip.
        result2 = await migrate_to_cross_library_identity(cross_lib_env["slugs"])
        assert result2["skipped"] is True

    async def test_consolidation_picks_richest_row_for_canonical_name(self, cross_lib_env):
        """The row with more source IDs should win the canonical_name tiebreak."""
        # Calibre row: more source IDs.
        await cross_lib_env["add_author"](
            "calibre-library", "William D. Arand",
            amazon_id="B01AY7PSG4",
            goodreads_id="14905104",
            hardcover_id="259414",
        )
        # ABS row: same name, ZERO source IDs.
        await cross_lib_env["add_author"](
            "abs-audio-library", "William D. Arand",
        )
        await migrate_to_cross_library_identity(cross_lib_env["slugs"])

        # Read persons.canonical_name — should be the richer row's name.
        gdb = await database.get_db()
        try:
            row = await (await gdb.execute(
                "SELECT canonical_name FROM persons "
                "WHERE normalized_name LIKE '%arand%'"
            )).fetchone()
            assert row is not None
            assert row[0] == "William D. Arand"
        finally:
            await gdb.close()

    async def test_low_confidence_flag_on_id_disagreement(self, cross_lib_env):
        """Two unrelated 'John Smith's in different libraries with
        disjoint source IDs get flagged low-confidence."""
        await cross_lib_env["add_author"](
            "calibre-library", "John Smith", amazon_id="B0CALIBR000",
        )
        await cross_lib_env["add_author"](
            "abs-audio-library", "John Smith", amazon_id="B0ABS00000",
        )
        await migrate_to_cross_library_identity(cross_lib_env["slugs"])

        # Both author_links rows should now be link_confidence='low'.
        gdb = await database.get_db()
        try:
            confidences = [r[0] for r in await (await gdb.execute(
                "SELECT link_confidence FROM author_links"
            )).fetchall()]
            assert all(c == "low" for c in confidences)
        finally:
            await gdb.close()

    async def test_high_confidence_when_source_id_agrees(self, cross_lib_env):
        """Two library rows sharing ANY source_id keep high confidence."""
        await cross_lib_env["add_author"](
            "calibre-library", "William D. Arand", amazon_id="B01AY7PSG4",
        )
        await cross_lib_env["add_author"](
            "abs-audio-library", "William D. Arand", amazon_id="B01AY7PSG4",
        )
        await migrate_to_cross_library_identity(cross_lib_env["slugs"])
        gdb = await database.get_db()
        try:
            confidences = [r[0] for r in await (await gdb.execute(
                "SELECT link_confidence FROM author_links"
            )).fetchall()]
            assert all(c == "high" for c in confidences)
        finally:
            await gdb.close()

    async def test_high_confidence_when_no_ids_but_names_identical(
        self, cross_lib_env,
    ):
        """v2.22.2 — Mephisto case: two library rows with identical
        normalized_name and ZERO source IDs anywhere shouldn't be
        flagged 'low'. The auto-link is exact-name, and absence of
        enrichment isn't evidence of a collision."""
        await cross_lib_env["add_author"](
            "calibre-library", "Mephisto",
        )
        await cross_lib_env["add_author"](
            "abs-audio-library", "Mephisto",
        )
        await migrate_to_cross_library_identity(cross_lib_env["slugs"])
        gdb = await database.get_db()
        try:
            confidences = [r[0] for r in await (await gdb.execute(
                "SELECT link_confidence FROM author_links"
            )).fetchall()]
            assert all(c == "high" for c in confidences), (
                f"Expected 'high' on both links, got: {confidences}"
            )
        finally:
            await gdb.close()


# ─── Pen-name migration ──────────────────────────────────────


class TestPenNameMigration:
    async def test_promotes_intra_library_pen_link_to_cross_library(self, cross_lib_env):
        # Calibre: William D. Arand canonical, Randi Darren alias.
        arand_cal = await cross_lib_env["add_author"](
            "calibre-library", "William D. Arand", amazon_id="B01AY7PSG4",
        )
        darren_cal = await cross_lib_env["add_author"](
            "calibre-library", "Randi Darren", amazon_id="B0RANDIDAR",
        )
        await cross_lib_env["add_pen_link"]("calibre-library", arand_cal, darren_cal)

        # ABS: same two authors, different rows.
        arand_abs = await cross_lib_env["add_author"](
            "abs-audio-library", "William D. Arand", amazon_id="B01AY7PSG4",
        )
        darren_abs = await cross_lib_env["add_author"](
            "abs-audio-library", "Randi Darren", amazon_id="B0RANDIDAR",
        )

        result = await migrate_to_cross_library_identity(cross_lib_env["slugs"])
        # 2 persons (Arand + Darren), 4 links, 1 pen-name v2 row.
        assert result["created_persons"] == 2
        assert result["pen_name_migrated"] == 1

        # Verify the v2 row points at the two distinct person_ids.
        gdb = await database.get_db()
        try:
            row = await (await gdb.execute(
                "SELECT canonical_person_id, alias_person_id, link_type "
                "FROM pen_name_links_v2"
            )).fetchone()
            assert row is not None
            assert row[0] != row[1]  # different persons
            assert row[2] == "pen_name"
        finally:
            await gdb.close()


# ─── Orphan-link cleanup ─────────────────────────────────────


class TestPruneOrphanLinks:
    async def test_drops_link_for_missing_author(self, cross_lib_env):
        aid = await cross_lib_env["add_author"]("calibre-library", "Going Away")
        await get_or_create_person("calibre-library", aid)
        # Manually delete the per-library author row to simulate a
        # Database Manager delete.
        import sqlite3 as _sqlite3
        with _sqlite3.connect(
            cross_lib_env["tmp_path"] / "seshat_calibre-library.db"
        ) as conn:
            conn.execute("DELETE FROM authors WHERE id = ?", (aid,))

        dropped = await prune_orphan_links()
        assert dropped == 1
        # And the person row should be gone too (was the only link).
        gdb = await database.get_db()
        try:
            cnt = (await (await gdb.execute(
                "SELECT COUNT(*) FROM persons"
            )).fetchone())[0]
            assert cnt == 0
        finally:
            await gdb.close()

    async def test_keeps_person_when_other_links_exist(self, cross_lib_env):
        aid1 = await cross_lib_env["add_author"]("calibre-library", "Multi Link")
        aid2 = await cross_lib_env["add_author"]("abs-audio-library", "Multi Link")
        await get_or_create_person("calibre-library", aid1)
        await get_or_create_person("abs-audio-library", aid2)

        # Delete the calibre row only.
        import sqlite3 as _sqlite3
        with _sqlite3.connect(
            cross_lib_env["tmp_path"] / "seshat_calibre-library.db"
        ) as conn:
            conn.execute("DELETE FROM authors WHERE id = ?", (aid1,))

        dropped = await prune_orphan_links()
        assert dropped == 1
        # Person should still exist (abs link remains).
        gdb = await database.get_db()
        try:
            cnt = (await (await gdb.execute(
                "SELECT COUNT(*) FROM persons"
            )).fetchone())[0]
            assert cnt == 1
        finally:
            await gdb.close()
