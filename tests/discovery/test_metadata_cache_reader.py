"""
v2.21.0 Phase C — metadata cache reader.

Covers `CachedSource` + `read_cached_author` + `ensure_enqueued`:

  - cache miss returns None AND enqueues with `lookup_miss` reason +
    user-bumped priority so the worker pops it before backfill rows
  - cache hit returns an AuthorResult with cached books, with
    read-time filters applied (language, format, owned-only)
  - cache hit empty-after-filters returns an *empty* AuthorResult
    (distinguishes from cache miss, lets the merge layer no-op
    cleanly instead of triggering another enqueue)
  - `search_author` is a no-op (returns None) — synchronous name→ID
    resolution is gone from the user-facing flow
  - legacy `amazon_id` of name-shape (pre-v2.11.0 state) returns
    None without enqueueing (we have no key to enqueue with)

The lookup-loop integration test confirms the dispatcher can swap
the live source for the cached source without breaking the
attribute-injection contract (`_on_book`, `_content_type`, etc.).
"""
from __future__ import annotations

import json

import pytest

from app.discovery.database import get_active_library, set_active_library
from app.discovery import metadata_cache, metadata_cache_reader


# ─── Shared fixture: empty cache DB under tmp_path ──────────────


@pytest.fixture
async def fresh_cache(tmp_path, monkeypatch):
    """Per-test cache DB + active library slug.

    Active library defaults to "books-lib" so the cache reader has a
    `library_slug` to key on. Tests that need a different slug call
    `set_active_library(...)` directly.
    """
    from app import config as app_config
    from app.discovery import database as disco_db
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(metadata_cache, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)

    await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
    prev = get_active_library()
    set_active_library("books-lib")
    yield tmp_path
    set_active_library(prev)


async def _seed_state_and_books(
    author_id: str,
    library_slug: str,
    books: list[dict],
) -> None:
    """Helper: insert one state row + N book rows for (id, slug)."""
    db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
    try:
        await db.execute(
            f"INSERT INTO {metadata_cache.state_table()} "
            f"(author_id, library_slug, last_scanned_at, last_outcome, book_count) "
            f"VALUES (?, ?, ?, ?, ?)",
            (author_id, library_slug, 12345.0, "ok", len(books)),
        )
        for b in books:
            await db.execute(
                f"INSERT INTO {metadata_cache.books_table()} "
                f"(author_id, library_slug, book_asin, title, series_name, "
                f"series_pos, pub_date, format, language, isbn, cover_url, "
                f"raw_json, cached_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    author_id, library_slug,
                    b["asin"], b.get("title", "T"),
                    b.get("series"), b.get("pos"),
                    b.get("pub_date"), b.get("format", "kindle_edition"),
                    b.get("language", "English"),
                    b.get("isbn"), b.get("cover_url"),
                    json.dumps(b.get("extra") or {}),
                    12345.0,
                ),
            )
        await db.commit()
    finally:
        await db.close()


# ─── Cache miss + enqueue ───────────────────────────────────────


class TestCacheMissEnqueues:
    async def test_miss_returns_none_and_enqueues(self, fresh_cache):
        source = metadata_cache_reader.make_amazon_cached_source()
        result = await source.get_author_books("B0AAAAAAAA")
        assert result is None
        # Queue picked up the cache-miss row (schema-v2: PK=author_id).
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT author_id, priority, enqueued_reason "
                f"FROM {metadata_cache.queue_table()}"
            )
            rows = await cur.fetchall()
        finally:
            await db.close()
        assert len(rows) == 1
        assert rows[0][0] == "B0AAAAAAAA"
        # User-bumped priority — pops before backfill rows (priority 100).
        assert rows[0][1] == 1000.0
        assert rows[0][2] == "lookup_miss"

    async def test_miss_with_existing_queue_row_does_not_duplicate(
        self, fresh_cache,
    ):
        # Pre-seed the queue at priority 100 (the v2.21.0 backfill
        # default). A subsequent cache miss must NOT duplicate the row
        # nor overwrite the priority — the queue PK + INSERT OR IGNORE
        # leave the existing row untouched.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            await db.execute(
                f"INSERT INTO {metadata_cache.queue_table()} "
                f"(author_id, priority, enqueued_reason, "
                f"next_scan_due_at) VALUES (?, ?, ?, ?)",
                ("B0BBBBBBBB", 100.0, "v2210_backfill", 0.0),
            )
            await db.commit()
        finally:
            await db.close()

        source = metadata_cache_reader.make_amazon_cached_source()
        await source.get_author_books("B0BBBBBBBB")

        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT priority, enqueued_reason FROM "
                f"{metadata_cache.queue_table()} "
                f"WHERE author_id = ?",
                ("B0BBBBBBBB",),
            )
            row = await cur.fetchone()
            count_cur = await db.execute(
                f"SELECT COUNT(*) FROM {metadata_cache.queue_table()} "
                f"WHERE author_id = ?",
                ("B0BBBBBBBB",),
            )
            count = (await count_cur.fetchone())[0]
        finally:
            await db.close()
        # Existing backfill row survives; no duplicate.
        assert count == 1
        assert row[0] == 100.0
        assert row[1] == "v2210_backfill"

    async def test_legacy_name_shape_returns_none_no_enqueue(self, fresh_cache):
        # Pre-v2.11.0 some authors.amazon_id values stored the author
        # NAME rather than a 10-char ID. The cache reader has nothing
        # to look up and nothing to enqueue (no real key); return None
        # silently. Worker (Phase D) Pass-B resolution handles it.
        source = metadata_cache_reader.make_amazon_cached_source()
        result = await source.get_author_books("Brandon Sanderson")
        assert result is None
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM {metadata_cache.queue_table()}"
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == 0


# ─── Cache hit + read-time filters ───────────────────────────────


class TestCacheHitReturnsBooks:
    async def test_hit_returns_books_grouped_by_series(self, fresh_cache):
        await _seed_state_and_books(
            "B0CCCCCCCC", "books-lib",
            [
                {"asin": "B0BOOK0001", "title": "Mistborn 1",
                 "series": "Mistborn", "pos": 1.0},
                {"asin": "B0BOOK0002", "title": "Mistborn 2",
                 "series": "Mistborn", "pos": 2.0},
                {"asin": "B0BOOK0099", "title": "Elantris"},
            ],
        )
        source = metadata_cache_reader.make_amazon_cached_source()
        result = await source.get_author_books("B0CCCCCCCC")
        assert result is not None
        assert result.external_id == "B0CCCCCCCC"
        # 1 standalone + 1 series with 2 books.
        assert len(result.books) == 1
        assert result.books[0].title == "Elantris"
        assert len(result.series) == 1
        assert result.series[0].name == "Mistborn"
        assert {b.title for b in result.series[0].books} == {
            "Mistborn 1", "Mistborn 2",
        }
        # external_id flows from `book_asin` so the merge layer routes
        # it to books.amazon_id.
        all_asins = (
            {b.external_id for b in result.books}
            | {b.external_id for s in result.series for b in s.books}
        )
        assert all_asins == {"B0BOOK0001", "B0BOOK0002", "B0BOOK0099"}

    async def test_language_filter_drops_non_matching(self, fresh_cache):
        await _seed_state_and_books(
            "B0DDDDDDDD", "books-lib",
            [
                {"asin": "B0L0001", "title": "EnglishOnly", "language": "English"},
                {"asin": "B0L0002", "title": "GermanEd",  "language": "German"},
                {"asin": "B0L0003", "title": "NoLang",    "language": None},
            ],
        )
        source = metadata_cache_reader.make_amazon_cached_source(
            language="English",
        )
        result = await source.get_author_books("B0DDDDDDDD")
        titles = {b.title for b in result.books}
        # English keeps, German drops, NoLang passes through (permissive).
        assert "EnglishOnly" in titles
        assert "NoLang" in titles
        assert "GermanEd" not in titles

    async def test_format_filter_drops_non_matching(self, fresh_cache):
        await _seed_state_and_books(
            "B0EEEEEEEE", "books-lib",
            [
                {"asin": "B0F0001", "title": "KindleBook",
                 "format": "kindle_edition"},
                {"asin": "B0F0002", "title": "PaperbackBook",
                 "format": "paperback"},
                {"asin": "B0F0003", "title": "NoFormat",
                 "format": None},
            ],
        )
        source = metadata_cache_reader.make_amazon_cached_source(
            format_filter="kindle_edition",
        )
        result = await source.get_author_books("B0EEEEEEEE")
        titles = {b.title for b in result.books}
        # Kindle keeps, paperback drops, NoFormat passes through.
        assert "KindleBook" in titles
        assert "NoFormat" in titles
        assert "PaperbackBook" not in titles

    async def test_audiobook_content_type_swaps_format_filter(
        self, fresh_cache,
    ):
        # NOTE: cached `format` stores the binding-symbol shape Amazon
        # stamps on a parsed product, not the filter-input shape.
        # `audible_audiobook` (filter input) → `audio_download`
        # (binding symbol stored in the cache). The reader's
        # FILTER_TO_BINDING translation is what lets the source's
        # input-shape config match the cached output-shape rows.
        await _seed_state_and_books(
            "B0FFFFFFFF", "books-lib",
            [
                {"asin": "B0A0001", "title": "AudibleBook",
                 "format": "audio_download"},
                {"asin": "B0A0002", "title": "KindleBook",
                 "format": "kindle_edition"},
            ],
        )
        source = metadata_cache_reader.make_amazon_cached_source(
            format_filter="kindle",
            audiobook_format_filter="audible_audiobook",
        )
        # Dispatcher sets `_content_type` before the call.
        source._content_type = "audiobook"
        result = await source.get_author_books("B0FFFFFFFF")
        titles = {b.title for b in result.books}
        # Audiobook scan reads audio_download books; kindle is filtered.
        assert titles == {"AudibleBook"}

    async def test_all_formats_disables_filter(self, fresh_cache):
        await _seed_state_and_books(
            "B0GGGGGGGG", "books-lib",
            [
                {"asin": "B0X0001", "title": "Kindle", "format": "kindle_edition"},
                {"asin": "B0X0002", "title": "Paperback", "format": "paperback"},
                {"asin": "B0X0003", "title": "Hardcover", "format": "hardcover"},
            ],
        )
        source = metadata_cache_reader.make_amazon_cached_source(
            format_filter="allFormats",
        )
        result = await source.get_author_books("B0GGGGGGGG")
        titles = {b.title for b in result.books}
        assert titles == {"Kindle", "Paperback", "Hardcover"}

    async def test_owned_only_drops_unowned(self, fresh_cache):
        await _seed_state_and_books(
            "B0HHHHHHHH", "books-lib",
            [
                {"asin": "B0O0001", "title": "OwnedOne"},
                {"asin": "B0O0002", "title": "OwnedTwo"},
                {"asin": "B0O0003", "title": "BrandNewBook"},
            ],
        )
        source = metadata_cache_reader.make_amazon_cached_source()
        result = await source.get_author_books(
            "B0HHHHHHHH",
            owned_titles=["OwnedOne", "OwnedTwo"],
            owned_only=True,
        )
        titles = {b.title for b in result.books}
        assert titles == {"OwnedOne", "OwnedTwo"}

    async def test_hit_with_no_books_after_filter_returns_empty_authorresult(
        self, fresh_cache,
    ):
        # State exists but every cached book is in German.
        await _seed_state_and_books(
            "B0IIIIIIII", "books-lib",
            [
                {"asin": "B0X0001", "title": "GermanOne", "language": "German"},
                {"asin": "B0X0002", "title": "GermanTwo", "language": "German"},
            ],
        )
        source = metadata_cache_reader.make_amazon_cached_source(
            language="English",
        )
        result = await source.get_author_books("B0IIIIIIII")
        # NOT None — it's a cache hit with 0 books surviving filters.
        # That signals "we know this author; just nothing to return"
        # so the merge layer no-ops rather than re-enqueueing.
        assert result is not None
        assert result.books == []
        assert result.series == []

    async def test_hit_does_not_enqueue(self, fresh_cache):
        # A cache HIT must NOT enqueue — there's nothing for the
        # worker to do, and double-queueing on every scan would
        # thrash the priority order.
        await _seed_state_and_books(
            "B0JJJJJJJJ", "books-lib",
            [{"asin": "B0X0001", "title": "Cached"}],
        )
        source = metadata_cache_reader.make_amazon_cached_source()
        await source.get_author_books("B0JJJJJJJJ")
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM {metadata_cache.queue_table()}"
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == 0, "cache hit must not produce a queue row"


# ─── search_author no-op ────────────────────────────────────────


class TestSearchAuthorIsNoOp:
    async def test_search_author_returns_none(self, fresh_cache):
        source = metadata_cache_reader.make_amazon_cached_source()
        assert await source.search_author("Brandon Sanderson") is None

    async def test_search_author_does_not_enqueue(self, fresh_cache):
        # search_author is the name→ID path; without an ID we have
        # no queue key. Must NOT enqueue under any path.
        source = metadata_cache_reader.make_amazon_cached_source()
        await source.search_author("Brandon Sanderson")
        db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
        try:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM {metadata_cache.queue_table()}"
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        assert row[0] == 0


# ─── Active-library handling ────────────────────────────────────


class TestActiveLibraryRouting:
    async def test_no_active_library_returns_none(
        self, fresh_cache, monkeypatch,
    ):
        # Reset active library after the fixture set it.
        prev = get_active_library()
        set_active_library(None)
        try:
            source = metadata_cache_reader.make_amazon_cached_source()
            result = await source.get_author_books("B0KKKKKKKK")
            assert result is None
            # Nothing in the queue either — we have no slug to key on.
            db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
            try:
                cur = await db.execute(
                    f"SELECT COUNT(*) FROM {metadata_cache.queue_table()}"
                )
                row = await cur.fetchone()
            finally:
                await db.close()
            assert row[0] == 0
        finally:
            set_active_library(prev)

    async def test_different_library_slugs_are_isolated(self, fresh_cache):
        # Same author_id cached in two libraries → switching the
        # active library returns only that library's books.
        await _seed_state_and_books(
            "B0LLLLLLLL", "books-lib",
            [{"asin": "B0X0001", "title": "EbookEdition"}],
        )
        await _seed_state_and_books(
            "B0LLLLLLLL", "audio-lib",
            [{"asin": "B0X0002", "title": "AudiobookEdition"}],
        )
        source = metadata_cache_reader.make_amazon_cached_source()
        # books-lib is the fixture default.
        r1 = await source.get_author_books("B0LLLLLLLL")
        assert {b.title for b in r1.books} == {"EbookEdition"}
        # Switch active library — same author_id, different books.
        set_active_library("audio-lib")
        r2 = await source.get_author_books("B0LLLLLLLL")
        assert {b.title for b in r2.books} == {"AudiobookEdition"}


# ─── Dispatcher contract (lookup.py compat) ─────────────────────


class TestDispatcherContract:
    """lookup.py drives sources by:
        - setting `_on_book`, `_on_new_candidate` callbacks
        - setting `_linked_author_names`, `_content_type`
        - calling `search_author(name)` or `get_author_books(id, ...)`
        - reading `.name`

    The cache reader must accept all of these without AttributeError
    or it'll break the dispatcher silently. This test pins the
    contract."""

    async def test_attribute_injection_succeeds(self, fresh_cache):
        source = metadata_cache_reader.make_amazon_cached_source()
        source._on_book = lambda title: None
        source._on_new_candidate = lambda: None
        source._linked_author_names = ["Pen Name"]
        source._content_type = "ebook"
        source._owned_titles = ["Owned"]
        source._owned_series_names = ["Mistborn"]
        # No AttributeError = pass.
        assert source.name == "amazon"

    async def test_get_author_books_accepts_all_dispatcher_kwargs(
        self, fresh_cache,
    ):
        # Mirrors the first call shape in _try_source (lookup.py:2895).
        source = metadata_cache_reader.make_amazon_cached_source()
        # Should not raise TypeError even with the start_at +
        # existing_titles kwargs the Goodreads-style call passes.
        result = await source.get_author_books(
            "B0PPPPPPPP",
            existing_titles={"Some Title"},
            owned_titles=["Owned"],
            owned_only=False,
            start_at=0,
        )
        assert result is None  # cache miss path


# ─── v3.4.0 slice 04 — Goodreads cache reader hybrid path ──────


import asyncio
import json
import time

import pytest

from app.discovery import metadata_cache
from app.discovery.metadata_cache_reader import (
    CachedSource, SOURCE_GOODREADS,
    read_cached_goodreads_raw_books,
)


@pytest.fixture
async def gr_reader_under(tmp_path, monkeypatch):
    """Init GR cache under tmp_path + redirect imports."""
    from app import config as app_config
    from app.discovery import database as disco_db
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(metadata_cache, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)

    await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
    yield tmp_path


async def _seed_gr_cache(
    *, author_id: str, library_slug: str,
    pages: dict[int, list[dict]],
) -> None:
    """Insert a state row + per-page list_page rows for GR cache."""
    db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
    try:
        await db.execute(
            f"INSERT INTO "
            f"{metadata_cache.state_table(metadata_cache.SOURCE_GOODREADS)} "
            f"(author_id, library_slug, last_scanned_at, last_outcome, "
            f" book_count) VALUES (?, ?, ?, ?, ?)",
            (author_id, library_slug, time.time(), "ok",
             sum(len(p) for p in pages.values())),
        )
        for page_num, records in pages.items():
            await db.execute(
                f"INSERT INTO "
                f"{metadata_cache.list_pages_table(metadata_cache.SOURCE_GOODREADS)} "
                f"(author_id, library_slug, page_num, fetched_at, "
                f" book_ids_json) VALUES (?, ?, ?, ?, ?)",
                (author_id, library_slug, page_num, time.time(),
                 json.dumps(records)),
            )
        await db.commit()
    finally:
        await db.close()


class TestReadCachedGoodreadsRawBooks:
    async def test_miss_returns_none(self, gr_reader_under):
        result = await read_cached_goodreads_raw_books(
            author_id="GR-MISS", library_slug="books-lib",
        )
        assert result is None

    async def test_hit_returns_flattened_records(self, gr_reader_under):
        await _seed_gr_cache(
            author_id="GR-700", library_slug="books-lib",
            pages={
                1: [{"book_id": "a", "title": "A"},
                    {"book_id": "b", "title": "B"}],
                2: [{"book_id": "c", "title": "C"}],
            },
        )
        result = await read_cached_goodreads_raw_books(
            author_id="GR-700", library_slug="books-lib",
        )
        assert [r["book_id"] for r in result] == ["a", "b", "c"]
        assert result[0]["title"] == "A"

    async def test_hit_with_zero_pages_returns_empty_list(
        self, gr_reader_under,
    ):
        """State row exists but no list_pages rows — the worker ran
        a successful scan but the author had zero books at GR."""
        await _seed_gr_cache(
            author_id="GR-EMPTY", library_slug="books-lib",
            pages={},
        )
        result = await read_cached_goodreads_raw_books(
            author_id="GR-EMPTY", library_slug="books-lib",
        )
        assert result == []

    async def test_legacy_bare_id_strings_coerce_to_dicts(
        self, gr_reader_under,
    ):
        """Slice 03 stored bare ID strings; slice 04 stores dicts.
        An install upgrading from slice-03-shipped-but-never-04-shipped
        would have bare strings. Coerce defensively so the cache-HIT
        path doesn't crash on the dict access."""
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            await db.execute(
                f"INSERT INTO "
                f"{metadata_cache.state_table(metadata_cache.SOURCE_GOODREADS)} "
                f"(author_id, library_slug, last_scanned_at, last_outcome) "
                f"VALUES (?, ?, ?, ?)",
                ("GR-LEGACY", "books-lib", time.time(), "ok"),
            )
            await db.execute(
                f"INSERT INTO "
                f"{metadata_cache.list_pages_table(metadata_cache.SOURCE_GOODREADS)} "
                f"(author_id, library_slug, page_num, fetched_at, "
                f" book_ids_json) VALUES (?, ?, ?, ?, ?)",
                ("GR-LEGACY", "books-lib", 1, time.time(),
                 json.dumps(["legacy-id-1", "legacy-id-2"])),
            )
            await db.commit()
        finally:
            await db.close()
        result = await read_cached_goodreads_raw_books(
            author_id="GR-LEGACY", library_slug="books-lib",
        )
        assert [r["book_id"] for r in result] == [
            "legacy-id-1", "legacy-id-2",
        ]


class TestCachedSourceGoodreads:
    async def test_cache_miss_enqueues_and_returns_none(
        self, gr_reader_under, monkeypatch,
    ):
        from app.discovery import database as disco_db
        monkeypatch.setattr(disco_db, "_active_library_slug", "books-lib")

        source = CachedSource(source_name=SOURCE_GOODREADS)
        result = await source.get_author_books("GR-MISS-2")
        assert result is None

        # Queue row landed with priority 1000 + reason lookup_miss.
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            cur = await db.execute(
                f"SELECT priority, enqueued_reason FROM "
                f"{metadata_cache.queue_table(metadata_cache.SOURCE_GOODREADS)} "
                f"WHERE author_id = ?",
                ("GR-MISS-2",),
            )
            q = await cur.fetchone()
        finally:
            await db.close()
        assert q[0] == 1000.0
        assert q[1] == "lookup_miss"

    async def test_cache_hit_calls_source_with_cached_raw_books(
        self, gr_reader_under, monkeypatch,
    ):
        from app.discovery import database as disco_db
        monkeypatch.setattr(disco_db, "_active_library_slug", "books-lib")

        # Seed a cache HIT.
        await _seed_gr_cache(
            author_id="GR-HIT-1", library_slug="books-lib",
            pages={1: [
                {"book_id": "h1", "title": "Hit One",
                 "list_series": None, "list_series_idx": None,
                 "list_cover": None, "is_audio_list": False},
                {"book_id": "h2", "title": "Hit Two",
                 "list_series": None, "list_series_idx": None,
                 "list_cover": None, "is_audio_list": False},
            ]},
        )

        # Capture what get_author_books gets called with — we stub
        # the LIVE GoodreadsSource so no HTTP fires; assert that the
        # cached records were passed through to it.
        observed: dict = {}
        from app.discovery.sources import goodreads as gr_mod

        from app.discovery.sources.base import AuthorResult
        async def _fake_get_author_books(
            self, author_id, existing_titles=None,
            owned_titles=None, owned_only=False, start_at=0,
            cached_raw_books=None,
        ):
            observed["author_id"] = author_id
            observed["cached_raw_books"] = cached_raw_books
            return AuthorResult(
                name=author_id, external_id=author_id,
                books=[], series=[],
            )

        monkeypatch.setattr(
            gr_mod.GoodreadsSource, "get_author_books",
            _fake_get_author_books,
        )

        source = CachedSource(source_name=SOURCE_GOODREADS)
        result = await source.get_author_books(
            "GR-HIT-1",
            existing_titles={"hit one"},
            owned_titles=["Hit One"],
        )
        assert result is not None
        assert observed["author_id"] == "GR-HIT-1"
        assert observed["cached_raw_books"] is not None
        assert [r["book_id"] for r in observed["cached_raw_books"]] == [
            "h1", "h2",
        ]

    async def test_no_active_library_skips_silently(
        self, gr_reader_under, monkeypatch,
    ):
        from app.discovery import database as disco_db
        monkeypatch.setattr(disco_db, "_active_library_slug", "")

        source = CachedSource(source_name=SOURCE_GOODREADS)
        result = await source.get_author_books("GR-ANY")
        assert result is None
