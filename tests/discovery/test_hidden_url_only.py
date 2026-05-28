"""
v2.3.4 hidden-book URL-only write rule.

Pre-v2.3.4 (per v2.2.3): hidden books were a true garbage bin —
`_merge_result` short-circuited every UPDATE on `_is_hidden`, so
source scans wrote nothing. v2.3.4 changes the rule: hidden books
get URL-only writes (source_url merge + {source}_id COALESCE-fill)
so future scans of huge-catalog authors (John Walker — 1,069 books
on Goodreads) can fast-path past hidden titles via URL match
instead of paying DETAIL on every unmatched one. Metadata, series
claims, and consensus contributions stay suppressed — hidden still
means "ignore for enrichment."

Tests target both `_merge_result` callsites (series-books path +
standalone path) and verify the `_update_existing_url_only` helper
fields exactly the right fields and nothing more.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


async def _insert_author(name: str) -> int:
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name) "
            "VALUES (?, ?, ?)",
            (name, name, normalize_author_name(name)),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _insert_book(
    title: str,
    author_id: int,
    *,
    hidden: int = 0,
    description: str | None = None,
    source_url: str | None = None,
    series_id: int | None = None,
    series_index: float | None = None,
) -> int:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO books (title, hidden, description, "
            "source_url, series_id, series_index, source, owned) "
            "VALUES (?, ?, ?, ?, ?, ?, 'goodreads', 0)",
            (title, hidden, description, source_url,
             series_id, series_index),
        )
        # v3.0.0 Phase 4 (ADR-0008): scan prefilter reads existing books
        # via book_authors — seed the author link (backfill/sync parity).
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (?, ?, 0)",
            (cur.lastrowid, author_id),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _book_row(book_id: int) -> dict:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT * FROM books WHERE id = ?", (book_id,),
        )).fetchone()
        return dict(row) if row else {}
    finally:
        await db.close()


# ── series-books path ────────────────────────────────────────────────


async def test_hidden_book_in_series_path_gets_url_only_write(discovery_db):
    """A scan that matches a hidden book on the series-books path
    writes the source URL but leaves metadata + series claims alone."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import (
        AuthorResult, BookResult, SeriesResult,
    )

    author_id = await _insert_author("John Walker")
    book_id = await _insert_book(
        "Hidden Book",
        author_id,
        hidden=1,
        description="Original description — must not change",
    )

    result = AuthorResult(
        name="John Walker",
        external_id="walker-1",
        series=[
            SeriesResult(
                name="Some Series",
                books=[
                    BookResult(
                        title="Hidden Book",
                        series_name="Some Series",
                        series_index=4.0,
                        source="goodreads",
                        external_id="gr-12345",
                        source_url="https://www.goodreads.com/book/show/12345",
                        description="A SOURCE description that should NOT be written",
                    ),
                ],
            ),
        ],
    )

    series_collector: dict = {}
    await _merge_result(
        author_id=author_id,
        result=result,
        source_name="goodreads",
        languages=["English"],
        series_collector=series_collector,
    )

    row = await _book_row(book_id)
    # URL written.
    urls = json.loads(row["source_url"])
    assert urls == {"goodreads": "https://www.goodreads.com/book/show/12345"}
    # External id COALESCE-filled.
    assert row["goodreads_id"] == "gr-12345"
    # Metadata untouched.
    assert row["description"] == "Original description — must not change"
    # Series untouched (the hidden short-circuit prevents the lazy
    # series upsert + series_id assignment).
    assert row["series_id"] is None
    assert row["series_index"] is None
    # Hidden flag intact.
    assert row["hidden"] == 1
    # Series-collector NOT updated (consensus suggestions don't see
    # hidden books).
    assert series_collector == {}


# ── standalone path ──────────────────────────────────────────────────


async def test_hidden_book_in_standalone_path_gets_url_only_write(discovery_db):
    """Same rule on the standalone-side branch of `_merge_result`."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    author_id = await _insert_author("John Walker")
    book_id = await _insert_book(
        "Standalone Hidden",
        author_id,
        hidden=1,
        description="Original",
    )

    result = AuthorResult(
        name="John Walker",
        external_id="walker-1",
        books=[
            BookResult(
                title="Standalone Hidden",
                source="goodreads",
                external_id="gr-99999",
                source_url="https://www.goodreads.com/book/show/99999",
                description="Source description (drop)",
            ),
        ],
    )

    series_collector: dict = {}
    await _merge_result(
        author_id=author_id,
        result=result,
        source_name="goodreads",
        languages=["English"],
        series_collector=series_collector,
    )

    row = await _book_row(book_id)
    urls = json.loads(row["source_url"])
    assert urls == {"goodreads": "https://www.goodreads.com/book/show/99999"}
    assert row["goodreads_id"] == "gr-99999"
    assert row["description"] == "Original"
    assert row["hidden"] == 1
    # Standalone branch normally adds (None, None) to series_collector.
    # For hidden books, even that signal is suppressed.
    assert series_collector == {}


# ── additive URL merge ───────────────────────────────────────────────


async def test_hidden_book_url_merges_additively(discovery_db):
    """Hidden book already has a Hardcover URL. Goodreads scan
    matches → both URLs end up in the source_url dict."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    author_id = await _insert_author("John Walker")
    book_id = await _insert_book(
        "Hidden",
        author_id,
        hidden=1,
        source_url=json.dumps({
            "hardcover": "https://hardcover.app/books/old",
        }),
    )

    result = AuthorResult(
        name="John Walker",
        external_id="walker-1",
        books=[
            BookResult(
                title="Hidden",
                source="goodreads",
                external_id="gr-1",
                source_url="https://www.goodreads.com/book/show/1",
            ),
        ],
    )

    await _merge_result(
        author_id=author_id,
        result=result,
        source_name="goodreads",
        languages=["English"],
    )

    row = await _book_row(book_id)
    urls = json.loads(row["source_url"])
    # Both URLs present.
    assert urls == {
        "hardcover": "https://hardcover.app/books/old",
        "goodreads": "https://www.goodreads.com/book/show/1",
    }


# ── no URL, no id → no-op ───────────────────────────────────────────


async def test_hidden_book_with_no_url_or_id_is_no_op(discovery_db):
    """If the source returns no URL and no external_id for a hidden
    book, the URL-only path returns (None, None) and nothing is
    written. Mostly a defensive case — sources almost always have
    at least one of the two — but the helper handles it gracefully."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    author_id = await _insert_author("John Walker")
    book_id = await _insert_book(
        "Hidden",
        author_id,
        hidden=1,
        source_url=None,
    )

    result = AuthorResult(
        name="John Walker",
        external_id="walker-1",
        books=[
            BookResult(
                title="Hidden",
                source="goodreads",
                external_id=None,
                source_url=None,
                description="Description that won't be written",
            ),
        ],
    )

    await _merge_result(
        author_id=author_id,
        result=result,
        source_name="goodreads",
        languages=["English"],
    )

    row = await _book_row(book_id)
    # source_url stays None; description untouched.
    assert row["source_url"] is None
    assert row["description"] is None
    assert row["hidden"] == 1


# ── visible books in incremental mode: same URL/id behavior ─────────


async def test_visible_book_incremental_writes_url_and_id_only(discovery_db):
    """In incremental mode (`full_scan=False`), `_update_existing`
    already writes URL + {source}_id only — no metadata. So a
    visible book in incremental mode gets the same URL/id-only
    write shape as a hidden book; the difference between visible
    and hidden is that visible also gets series_id + is_omnibus
    promotion + series-collector contribution. This test pins down
    the incremental contract so we don't accidentally start writing
    metadata fields here in some future change."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    author_id = await _insert_author("John Walker")
    book_id = await _insert_book(
        "Visible",
        author_id,
        hidden=0,
        description=None,
    )

    result = AuthorResult(
        name="John Walker",
        external_id="walker-1",
        books=[
            BookResult(
                title="Visible",
                source="goodreads",
                external_id="gr-2",
                source_url="https://www.goodreads.com/book/show/2",
                description="Should NOT be written in incremental mode",
            ),
        ],
    )

    await _merge_result(
        author_id=author_id,
        result=result,
        source_name="goodreads",
        languages=["English"],
        # full_scan=False (default) — incremental mode.
    )

    row = await _book_row(book_id)
    urls = json.loads(row["source_url"])
    assert urls == {"goodreads": "https://www.goodreads.com/book/show/2"}
    assert row["goodreads_id"] == "gr-2"
    # Description NOT written: incremental never writes metadata,
    # regardless of hidden state.
    assert row["description"] is None


# ── full_scan mode: hidden vs visible diverge ────────────────────────


async def test_full_scan_hidden_stays_url_only(discovery_db):
    """In `full_scan` mode, visible books get metadata overwrites.
    Hidden books — via the new URL-only path — must NOT get those
    metadata writes. This is the test that proves the v2.3.4 rule
    is meaningful in full_scan mode (where pre-v2.3.4 hidden books
    were dropped entirely; pre-v2.2.3 they got full overwrites)."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    author_id = await _insert_author("John Walker")
    hidden_id = await _insert_book(
        "Hidden", author_id, hidden=1,
        description="Hidden — leave alone",
    )
    visible_id = await _insert_book(
        "Visible", author_id, hidden=0,
        description=None,  # empty — fair game in full_scan
    )

    result = AuthorResult(
        name="John Walker",
        external_id="walker-1",
        books=[
            BookResult(
                title="Hidden",
                source="goodreads",
                external_id="gr-h",
                source_url="https://www.goodreads.com/book/show/h",
                description="Source description for hidden — drop",
            ),
            BookResult(
                title="Visible",
                source="goodreads",
                external_id="gr-v",
                source_url="https://www.goodreads.com/book/show/v",
                description="Source description for visible — write",
            ),
        ],
    )

    await _merge_result(
        author_id=author_id,
        result=result,
        source_name="goodreads",
        languages=["English"],
        full_scan=True,
    )

    hidden_row = await _book_row(hidden_id)
    visible_row = await _book_row(visible_id)

    # Hidden: URL written, description preserved.
    assert json.loads(hidden_row["source_url"]) == {
        "goodreads": "https://www.goodreads.com/book/show/h",
    }
    assert hidden_row["description"] == "Hidden — leave alone"
    # Visible: URL + description both written (full_scan unowned
    # path overwrites metadata).
    assert json.loads(visible_row["source_url"]) == {
        "goodreads": "https://www.goodreads.com/book/show/v",
    }
    assert visible_row["description"] == "Source description for visible — write"
