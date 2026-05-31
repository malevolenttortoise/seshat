"""
v2.21.0 Phase B — Amazon metadata cache scaffolding.

Covers:
  - schema migration (4 tables created with source-templated names)
  - PRAGMA user_version advances + idempotent re-init
  - `db_summary` shape (size_bytes, last_modified, row_counts)
  - `backfill_amazon_queue_from_authors` enqueues authors carrying
    `amazon_id` across multiple libraries, idempotent on re-run
  - worker_state singleton row is seeded on init

The cache reader (Phase C) and worker (Phase D) live in separate
modules and have their own tests; this file only exercises the
underlying DB layer + backfill helper.
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.discovery import metadata_cache


# ─── Fixtures ───────────────────────────────────────────────────


@pytest.fixture
async def cache_under(tmp_path, monkeypatch):
    """Redirect both `app.config.DATA_DIR` and the metadata_cache
    module's own re-import so `get_db_path` lands under tmp_path.

    Mirrors the pattern in tests/routers/test_db_editor.py — every
    Seshat module that ``from app.config import DATA_DIR`` captures
    the value at import time, so monkey-patching the source isn't
    enough; we also have to patch the consumer.
    """
    from app import config as app_config
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(metadata_cache, "DATA_DIR", tmp_path)
    yield tmp_path


@pytest.fixture
async def fake_discovery_libraries(tmp_path, monkeypatch):
    """Create two discovery DBs under tmp_path each with a tiny
    `authors` table — enough to exercise the queue backfill.

    Returns a list of slugs the test should iterate. The fixture
    also redirects `app.discovery.database.DATA_DIR` so
    `get_discovery_db(slug=...)` opens the test files.
    """
    from app.discovery import database as disco_db
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)

    slugs = ["books-lib", "audio-lib"]
    for slug in slugs:
        await disco_db.init_db(slug)

    # Seed `authors` rows: two amazon_id authors per library + one
    # author with no amazon_id (must NOT enqueue).
    seed: dict[str, list[tuple[str, str | None]]] = {
        "books-lib": [
            ("Books Author One",   "B001AAAAAA"),
            ("Books Author Two",   "B002BBBBBB"),
            ("Books Author Three", None),
        ],
        "audio-lib": [
            ("Audio Author A", "B100AAAAAA"),
            ("Audio Author B", "B101BBBBBB"),
        ],
    }
    for slug, authors in seed.items():
        db = await disco_db.get_db(slug=slug)
        try:
            for name, amazon_id in authors:
                await db.execute(
                    "INSERT INTO authors (name, sort_name, normalized_name, amazon_id) "
                    "VALUES (?, ?, ?, ?)",
                    (name, name, name.lower(), amazon_id),
                )
            await db.commit()
        finally:
            await db.close()
    return slugs


# ─── Schema + lifecycle tests ───────────────────────────────────


class TestSchemaInit:
    async def test_init_creates_db_file(self, cache_under):
        path = metadata_cache.get_db_path(metadata_cache.SOURCE_AMAZON)
        assert not path.exists(), "precondition: fresh tmp dir"
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        assert path.exists()
        assert path.name == "metadata_cache_amazon.db"

    async def test_init_creates_all_four_tables(self, cache_under):
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'metadata_cache_amazon_%' "
                "ORDER BY name"
            )
            rows = await cur.fetchall()
        finally:
            await db.close()
        names = [r[0] for r in rows]
        assert names == [
            "metadata_cache_amazon_books",
            "metadata_cache_amazon_queue",
            "metadata_cache_amazon_state",
            "metadata_cache_amazon_worker_state",
        ]

    async def test_init_seeds_worker_state_singleton(self, cache_under):
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT id, last_block_at, block_cooldown_s "
                f"FROM {metadata_cache.worker_state_table()}"
            )
            rows = await cur.fetchall()
        finally:
            await db.close()
        # Exactly one row, with id=1 and default cooldown values.
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert rows[0][1] == 0
        assert rows[0][2] == 600

    async def test_init_stamps_user_version(self, cache_under):
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute("PRAGMA user_version")
            row = await cur.fetchone()
        finally:
            await db.close()
        # user_version should match the length of the migration list
        # so subsequent calls short-circuit. Derived rather than
        # hardcoded so adding new migrations (v3, v4, …) doesn't
        # require updating this assertion.
        expected = len(metadata_cache._MIGRATIONS[metadata_cache.SOURCE_AMAZON])
        assert row[0] == expected

    async def test_init_is_idempotent(self, cache_under):
        # Two back-to-back inits should leave the schema untouched.
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM {metadata_cache.worker_state_table()}"
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        # Singleton seed didn't double up; PRAGMA gate held.
        assert row[0] == 1

    async def test_state_table_pk_is_author_id_library_slug(self, cache_under):
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"PRAGMA table_info({metadata_cache.state_table()})"
            )
            cols = await cur.fetchall()
        finally:
            await db.close()
        pk_cols = sorted(
            (str(c[1]) for c in cols if c[5]), key=lambda n: n,
        )
        assert pk_cols == ["author_id", "library_slug"]

    async def test_books_fk_cascade_to_state(self, cache_under):
        # Delete a state row → books row for the same (author_id,
        # library_slug) must cascade-delete.
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"INSERT INTO {metadata_cache.state_table()} "
                f"(author_id, library_slug, last_scanned_at, last_outcome) "
                f"VALUES (?, ?, ?, ?)",
                ("BAUTHOR001", "books-lib", 12345.0, "ok"),
            )
            await db.execute(
                f"INSERT INTO {metadata_cache.books_table()} "
                f"(author_id, library_slug, book_asin, title, cached_at) "
                f"VALUES (?, ?, ?, ?, ?)",
                ("BAUTHOR001", "books-lib", "B0BOOK001", "Title", 12345.0),
            )
            await db.commit()
            await db.execute(
                f"DELETE FROM {metadata_cache.state_table()} "
                f"WHERE author_id = ? AND library_slug = ?",
                ("BAUTHOR001", "books-lib"),
            )
            await db.commit()
            cur = await db.execute(
                f"SELECT COUNT(*) FROM {metadata_cache.books_table()}"
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == 0, (
            "FK CASCADE didn't fire — books rows must follow state deletes"
        )


# ─── db_summary tests ──────────────────────────────────────────


class TestDbSummary:
    async def test_summary_reports_zero_when_db_missing(self, cache_under):
        summary = await metadata_cache.db_summary(metadata_cache.SOURCE_AMAZON)
        assert summary["size_bytes"] == 0
        assert summary["row_counts"] == {}
        assert summary["last_modified"] is None
        assert summary["source"] == "amazon"

    async def test_summary_reports_size_and_counts_after_init(self, cache_under):
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        summary = await metadata_cache.db_summary(metadata_cache.SOURCE_AMAZON)
        assert summary["size_bytes"] > 0
        assert summary["last_modified"] is not None
        rc = summary["row_counts"]
        # Empty cache: state/books/queue 0, worker_state seeded to 1.
        assert rc["metadata_cache_amazon_state"] == 0
        assert rc["metadata_cache_amazon_books"] == 0
        assert rc["metadata_cache_amazon_queue"] == 0
        assert rc["metadata_cache_amazon_worker_state"] == 1


# ─── Backfill tests ────────────────────────────────────────────


class TestBackfillFromAuthors:
    async def test_backfill_enqueues_amazon_id_authors(
        self, cache_under, fake_discovery_libraries,
    ):
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        counts = await metadata_cache.backfill_amazon_queue_from_authors(
            fake_discovery_libraries,
        )
        # books-lib has 2 amazon_id authors (the third had None and
        # must NOT enqueue); audio-lib has 2.
        assert counts == {"books-lib": 2, "audio-lib": 2}

    async def test_backfill_skips_authors_with_null_amazon_id(
        self, cache_under, fake_discovery_libraries,
    ):
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        await metadata_cache.backfill_amazon_queue_from_authors(
            fake_discovery_libraries,
        )
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT author_id FROM {metadata_cache.queue_table()} "
                f"ORDER BY author_id"
            )
            ids = [r[0] for r in await cur.fetchall()]
        finally:
            await db.close()
        # Sanity: only the 4 actual amazon_id authors landed.
        assert ids == [
            "B001AAAAAA", "B002BBBBBB", "B100AAAAAA", "B101BBBBBB",
        ]

    async def test_backfill_is_idempotent(
        self, cache_under, fake_discovery_libraries,
    ):
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        first = await metadata_cache.backfill_amazon_queue_from_authors(
            fake_discovery_libraries,
        )
        second = await metadata_cache.backfill_amazon_queue_from_authors(
            fake_discovery_libraries,
        )
        # Second call enqueues 0 new rows (existing PKs are IGNOREd).
        assert first == {"books-lib": 2, "audio-lib": 2}
        assert second == {"books-lib": 0, "audio-lib": 0}
        # And the total queue size is exactly the first-call total.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM {metadata_cache.queue_table()}"
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == 4

    async def test_backfill_records_queue_metadata(
        self, cache_under, fake_discovery_libraries,
    ):
        """v2 schema: queue is keyed by `author_id` only. Per-library
        seshat_author_id is resolved by the worker at scan time via
        `_libraries_for_author`. Backfill records priority + status +
        enqueued_reason on each unique amazon_id."""
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        await metadata_cache.backfill_amazon_queue_from_authors(
            fake_discovery_libraries,
        )
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT author_id, priority, status, enqueued_reason "
                f"FROM {metadata_cache.queue_table()} "
                f"ORDER BY author_id"
            )
            rows = await cur.fetchall()
        finally:
            await db.close()
        # All 4 distinct amazon_id authors from both libraries.
        assert len(rows) == 4
        for r in rows:
            assert r[1] == 100.0          # priority
            assert r[2] == "pending"      # status
            assert r[3] == "v2210_backfill"


# ─── v3.4.0 slice 01 — Goodreads list-page cache foundation ─────


class TestGoodreadsSchemaInit:
    async def test_init_creates_db_file(self, cache_under):
        path = metadata_cache.get_db_path(metadata_cache.SOURCE_GOODREADS)
        assert not path.exists(), "precondition: fresh tmp dir"
        await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
        assert path.exists()
        assert path.name == "metadata_cache_goodreads.db"

    async def test_init_creates_all_four_tables(self, cache_under):
        await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'metadata_cache_goodreads_%' "
                "ORDER BY name"
            )
            rows = await cur.fetchall()
        finally:
            await db.close()
        names = [r[0] for r in rows]
        # Path B: list_pages replaces Amazon's books table.
        assert names == [
            "metadata_cache_goodreads_list_pages",
            "metadata_cache_goodreads_queue",
            "metadata_cache_goodreads_state",
            "metadata_cache_goodreads_worker_state",
        ]

    async def test_init_does_not_create_amazon_shaped_books_table(
        self, cache_under,
    ):
        """GR is list-page-only — Amazon's `books` per-book detail
        table must NOT exist in the GR DB (see ADR-0018 §1)."""
        await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name = 'metadata_cache_goodreads_books'"
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row is None

    async def test_init_seeds_worker_state_singleton(self, cache_under):
        await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                f"SELECT id, last_block_at, block_cooldown_s "
                f"FROM {metadata_cache.worker_state_table(metadata_cache.SOURCE_GOODREADS)}"
            )
            rows = await cur.fetchall()
        finally:
            await db.close()
        # Exactly one row, with id=1. GR cooldown default is 300s
        # (vs Amazon's Akamai-tuned 600s) per ADR-0018.
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert rows[0][1] == 0
        assert rows[0][2] == 300

    async def test_init_is_idempotent(self, cache_under):
        await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
        await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM "
                f"{metadata_cache.worker_state_table(metadata_cache.SOURCE_GOODREADS)}"
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == 1

    async def test_state_table_pk_matches_amazon_shape(self, cache_under):
        """`state` is source-agnostic by design (ADR-0018 §2) so
        telemetry primitives reuse without source-specific branching."""
        await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                f"PRAGMA table_info("
                f"{metadata_cache.state_table(metadata_cache.SOURCE_GOODREADS)})"
            )
            cols = await cur.fetchall()
        finally:
            await db.close()
        pk_cols = sorted(str(c[1]) for c in cols if c[5])
        assert pk_cols == ["author_id", "library_slug"]

    async def test_queue_pk_is_author_id_only(self, cache_under):
        """Mirrors Amazon v2 — one queue row per author across all
        libraries (no double-scanning the same author for ebook +
        audio variants)."""
        await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                f"PRAGMA table_info("
                f"{metadata_cache.queue_table(metadata_cache.SOURCE_GOODREADS)})"
            )
            cols = await cur.fetchall()
        finally:
            await db.close()
        pk_cols = [str(c[1]) for c in cols if c[5]]
        assert pk_cols == ["author_id"]

    async def test_list_pages_fk_cascade_to_state(self, cache_under):
        """Delete a state row → list_pages snapshots for the same
        (author, library) must cascade-delete."""
        await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            await db.execute(
                f"INSERT INTO "
                f"{metadata_cache.state_table(metadata_cache.SOURCE_GOODREADS)} "
                f"(author_id, library_slug, last_scanned_at, last_outcome) "
                f"VALUES (?, ?, ?, ?)",
                ("GR-123", "books-lib", 12345.0, "ok"),
            )
            await db.execute(
                f"INSERT INTO "
                f"{metadata_cache.list_pages_table(metadata_cache.SOURCE_GOODREADS)} "
                f"(author_id, library_slug, page_num, fetched_at, book_ids_json) "
                f"VALUES (?, ?, ?, ?, ?)",
                ("GR-123", "books-lib", 1, 12345.0, "[\"100\", \"200\"]"),
            )
            await db.commit()
            await db.execute(
                f"DELETE FROM "
                f"{metadata_cache.state_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE author_id = ? AND library_slug = ?",
                ("GR-123", "books-lib"),
            )
            await db.commit()
            cur = await db.execute(
                f"SELECT COUNT(*) FROM "
                f"{metadata_cache.list_pages_table(metadata_cache.SOURCE_GOODREADS)}"
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == 0


class TestGoodreadsDbSummary:
    async def test_summary_iterates_goodreads_shape_not_amazon_shape(
        self, cache_under,
    ):
        """`db_summary` enumerates per-source tables — GR has
        list_pages, NOT books (ADR-0018 §2)."""
        await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
        summary = await metadata_cache.db_summary(
            metadata_cache.SOURCE_GOODREADS,
        )
        assert summary["size_bytes"] > 0
        rc = summary["row_counts"]
        assert "metadata_cache_goodreads_state" in rc
        assert "metadata_cache_goodreads_list_pages" in rc
        assert "metadata_cache_goodreads_queue" in rc
        assert "metadata_cache_goodreads_worker_state" in rc
        # `books` is the Amazon-shape detail table — must not appear
        # in the GR summary.
        assert "metadata_cache_goodreads_books" not in rc
        # Empty cache: state/list_pages/queue 0, worker_state seeded.
        assert rc["metadata_cache_goodreads_state"] == 0
        assert rc["metadata_cache_goodreads_list_pages"] == 0
        assert rc["metadata_cache_goodreads_queue"] == 0
        assert rc["metadata_cache_goodreads_worker_state"] == 1


class TestSourceShape:
    def test_supported_sources_includes_both(self):
        assert metadata_cache.SOURCE_AMAZON in metadata_cache.SUPPORTED_SOURCES
        assert metadata_cache.SOURCE_GOODREADS in metadata_cache.SUPPORTED_SOURCES

    def test_per_source_table_suffixes_diverge(self):
        """Amazon has `books`, Goodreads has `list_pages`. Other
        suffixes match. Callers like `db_summary` use this helper
        instead of a hardcoded tuple so the divergence is honored."""
        amz = metadata_cache.per_source_table_suffixes(
            metadata_cache.SOURCE_AMAZON,
        )
        gr = metadata_cache.per_source_table_suffixes(
            metadata_cache.SOURCE_GOODREADS,
        )
        assert "books" in amz
        assert "books" not in gr
        assert "list_pages" in gr
        assert "list_pages" not in amz
        # Source-agnostic tables are shared by both.
        for suffix in ("state", "queue", "worker_state"):
            assert suffix in amz
            assert suffix in gr

    def test_books_table_raises_for_goodreads(self):
        """Amazon-only helper. Calling for GR must raise so callers
        get a clean signal rather than silently building a SQL
        statement against a non-existent table."""
        with pytest.raises(ValueError):
            metadata_cache.books_table(metadata_cache.SOURCE_GOODREADS)

    def test_list_pages_table_raises_for_amazon(self):
        with pytest.raises(ValueError):
            metadata_cache.list_pages_table(metadata_cache.SOURCE_AMAZON)

    async def test_backfill_dedupes_same_amazon_id_across_libraries(
        self, cache_under, tmp_path, monkeypatch,
    ):
        """v2 schema: queue PK is `author_id` only, so the same
        amazon_id present in both calibre + abs collapses to ONE
        queue row. Confirms the 50% backfill-volume win flagged on
        2026-05-22."""
        from app.discovery import database as disco_db
        monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)

        slugs = ["books-lib", "audio-lib"]
        for slug in slugs:
            await disco_db.init_db(slug)

        # SAME amazon_id in both libraries (Sanderson-style cross-
        # library author), plus one unique-per-library author each.
        seed: dict[str, list[tuple[str, str]]] = {
            "books-lib": [
                ("Shared Author",  "B0SHARED01"),
                ("Books-Only One", "B0BOOKS001"),
            ],
            "audio-lib": [
                ("Shared Author Audio", "B0SHARED01"),  # same amazon_id
                ("Audio-Only One",      "B0AUDIO001"),
            ],
        }
        for slug, authors in seed.items():
            db = await disco_db.get_db(slug=slug)
            try:
                for name, amazon_id in authors:
                    await db.execute(
                        "INSERT INTO authors (name, sort_name, normalized_name, amazon_id) "
                        "VALUES (?, ?, ?, ?)",
                        (name, name, name.lower(), amazon_id),
                    )
                await db.commit()
            finally:
                await db.close()

        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        await metadata_cache.backfill_amazon_queue_from_authors(slugs)

        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT author_id FROM {metadata_cache.queue_table()} "
                f"ORDER BY author_id"
            )
            ids = [r[0] for r in await cur.fetchall()]
        finally:
            await db.close()
        # 3 unique amazon_ids, not 4 — Shared Author collapsed.
        assert ids == ["B0AUDIO001", "B0BOOKS001", "B0SHARED01"]

    async def test_backfill_handles_unknown_library_gracefully(
        self, cache_under, fake_discovery_libraries,
    ):
        # Mixing a real slug with a typo should not crash; the unknown
        # slug logs a warning and returns 0.
        await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
        counts = await metadata_cache.backfill_amazon_queue_from_authors(
            list(fake_discovery_libraries) + ["nope-typo"],
        )
        assert counts.get("nope-typo") == 0
        # Real slugs still got their backfill.
        assert counts["books-lib"] == 2
        assert counts["audio-lib"] == 2


# ─── Source validation ────────────────────────────────────────


class TestSourceValidation:
    def test_get_db_path_rejects_unknown_source(self, cache_under):
        with pytest.raises(ValueError, match="unknown metadata cache source"):
            metadata_cache.get_db_path("kindle-unlimited")

    async def test_get_db_rejects_unknown_source(self, cache_under):
        with pytest.raises(ValueError, match="unknown metadata cache source"):
            await metadata_cache.get_db("hardcover")
