"""
v2.3.4 source-scan write rule: write-through-on-empty +
queue-on-populated.

Replaces the prior owned-Calibre per-field rules and the unowned
full-overwrite. Uniform behavior — relies on `books_calibre_snapshot`
to protect curated Calibre data, so populated→queue keeps user
content untouched until they accept in the Metadata Manager UI.

Rule (full_scan only — incremental still writes URL/id only):
  - existing column NULL/empty → write through to `books`.
  - existing has a value AND incoming differs → enqueue
    `metadata_review_queue` row (UPSERT on (book_id, field, source)).
  - matches → no-op.

Outside the rule:
  - source_url, {source}_id, series_id, is_omnibus → always-additive.
  - is_unreleased → always-overwrite-if-not-None (binary flag).
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
    description: str | None = None,
    pub_date: str | None = None,
    isbn: str | None = None,
    page_count: int | None = None,
    cover_url: str | None = None,
    source: str = "goodreads",
) -> int:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO books (title, author_id, description, pub_date, "
            "isbn, page_count, cover_url, source, owned) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (title, author_id, description, pub_date, isbn,
             page_count, cover_url, source),
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


async def _queue_rows(book_id: int) -> list[dict]:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT field, old_value, new_value, source "
            "FROM metadata_review_queue WHERE book_id = ? "
            "ORDER BY field, source", (book_id,),
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ── write-through on empty ───────────────────────────────────────────


async def test_writes_through_when_field_is_null(discovery_db):
    """Source returns description, book has NULL description →
    write through, no queue."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    aid = await _insert_author("A")
    bid = await _insert_book("Book", aid, description=None)

    result = AuthorResult(
        name="A", external_id="x",
        books=[BookResult(
            title="Book", source="goodreads",
            description="Source description",
        )],
    )
    await _merge_result(
        author_id=aid, result=result, source_name="goodreads",
        languages=["English"], full_scan=True,
    )

    row = await _book_row(bid)
    assert row["description"] == "Source description"
    assert await _queue_rows(bid) == []


async def test_writes_through_when_field_is_whitespace(discovery_db):
    """Whitespace-only existing value counts as empty for the
    write-through decision."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    aid = await _insert_author("A")
    bid = await _insert_book("Book", aid, description="   ")

    result = AuthorResult(
        name="A", external_id="x",
        books=[BookResult(
            title="Book", source="goodreads",
            description="Real description",
        )],
    )
    await _merge_result(
        author_id=aid, result=result, source_name="goodreads",
        languages=["English"], full_scan=True,
    )

    assert (await _book_row(bid))["description"] == "Real description"
    assert await _queue_rows(bid) == []


# ── queue when populated and differs ────────────────────────────────


async def test_queues_when_existing_value_differs(discovery_db):
    """Source returns a different description → queue row, not write."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    aid = await _insert_author("A")
    bid = await _insert_book(
        "Book", aid,
        description="USER-CURATED description, hands off",
    )

    result = AuthorResult(
        name="A", external_id="x",
        books=[BookResult(
            title="Book", source="goodreads",
            description="Source's different take",
        )],
    )
    await _merge_result(
        author_id=aid, result=result, source_name="goodreads",
        languages=["English"], full_scan=True,
    )

    row = await _book_row(bid)
    # Books table NOT updated.
    assert row["description"] == "USER-CURATED description, hands off"
    # Queue row exists.
    queue = await _queue_rows(bid)
    assert len(queue) == 1
    assert queue[0]["field"] == "description"
    assert queue[0]["old_value"] == "USER-CURATED description, hands off"
    assert queue[0]["new_value"] == "Source's different take"
    assert queue[0]["source"] == "goodreads"


async def test_no_op_when_existing_matches(discovery_db):
    """Source returns same description as existing → no write, no queue."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    aid = await _insert_author("A")
    bid = await _insert_book(
        "Book", aid, description="Same description",
    )

    result = AuthorResult(
        name="A", external_id="x",
        books=[BookResult(
            title="Book", source="goodreads",
            description="Same description",
        )],
    )
    await _merge_result(
        author_id=aid, result=result, source_name="goodreads",
        languages=["English"], full_scan=True,
    )

    assert (await _book_row(bid))["description"] == "Same description"
    assert await _queue_rows(bid) == []


# ── multi-field mixed routing ───────────────────────────────────────


async def test_mixed_fields_route_independently(discovery_db):
    """One scan can write-through some fields and queue others.
    Tests that the per-field decision is truly per-field, not
    all-or-nothing."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    aid = await _insert_author("A")
    bid = await _insert_book(
        "Book", aid,
        description=None,                    # empty → will write through
        pub_date="2024-01-01",                # populated, differs → queue
        isbn=None,                            # empty → will write through
        page_count=300,                       # populated, matches → no-op
    )

    result = AuthorResult(
        name="A", external_id="x",
        books=[BookResult(
            title="Book", source="goodreads",
            description="From source",
            pub_date="2020-05-15",
            isbn="9780000000000",
            page_count=300,
        )],
    )
    await _merge_result(
        author_id=aid, result=result, source_name="goodreads",
        languages=["English"], full_scan=True,
    )

    row = await _book_row(bid)
    # Empty fields: written through.
    assert row["description"] == "From source"
    assert row["isbn"] == "9780000000000"
    # Populated-and-differs: NOT written.
    assert row["pub_date"] == "2024-01-01"
    # Populated-and-matches: no-op.
    assert row["page_count"] == 300

    queue = await _queue_rows(bid)
    fields_in_queue = {q["field"] for q in queue}
    assert fields_in_queue == {"pub_date"}


# ── UPSERT on rerun ──────────────────────────────────────────────────


async def test_rerun_replaces_queue_row(discovery_db):
    """Same source rescanning the same book with a NEW proposal
    replaces the prior queue row instead of accumulating."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    aid = await _insert_author("A")
    bid = await _insert_book(
        "Book", aid, description="Original",
    )

    # First scan proposes "Proposal 1".
    await _merge_result(
        author_id=aid, source_name="goodreads", languages=["English"],
        full_scan=True,
        result=AuthorResult(
            name="A", external_id="x",
            books=[BookResult(
                title="Book", source="goodreads",
                description="Proposal 1",
            )],
        ),
    )
    queue1 = await _queue_rows(bid)
    assert len(queue1) == 1
    assert queue1[0]["new_value"] == "Proposal 1"

    # Second scan proposes "Proposal 2" — same book, same field, same source.
    await _merge_result(
        author_id=aid, source_name="goodreads", languages=["English"],
        full_scan=True,
        result=AuthorResult(
            name="A", external_id="x",
            books=[BookResult(
                title="Book", source="goodreads",
                description="Proposal 2",
            )],
        ),
    )
    queue2 = await _queue_rows(bid)
    # Still ONE row (not two), with the latest proposal.
    assert len(queue2) == 1
    assert queue2[0]["new_value"] == "Proposal 2"


# ── different sources keep separate rows ────────────────────────────


async def test_different_sources_get_separate_queue_rows(discovery_db):
    """Goodreads and Hardcover both proposing different descriptions
    → two queue rows so the user can pick between them."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    aid = await _insert_author("A")
    bid = await _insert_book(
        "Book", aid, description="Original",
    )

    for src, desc in [
        ("goodreads", "Goodreads version"),
        ("hardcover", "Hardcover version"),
    ]:
        await _merge_result(
            author_id=aid, source_name=src, languages=["English"],
            full_scan=True,
            result=AuthorResult(
                name="A", external_id="x",
                books=[BookResult(
                    title="Book", source=src, description=desc,
                )],
            ),
        )

    queue = await _queue_rows(bid)
    by_source = {q["source"]: q["new_value"] for q in queue}
    assert by_source == {
        "goodreads": "Goodreads version",
        "hardcover": "Hardcover version",
    }


# ── owned-Calibre book: same uniform rule ───────────────────────────


async def test_owned_calibre_book_follows_same_rule(discovery_db):
    """The pre-v2.3.4 code had separate per-field rules for owned-
    Calibre books (smart description stub-detection, oldest-pub_date,
    COALESCE-fill). v2.3.4 removes that branch — uniform behavior.
    Curated Calibre metadata is protected by `books_calibre_snapshot`,
    not by special-casing in the merge rule."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    aid = await _insert_author("A")
    # source='calibre' marks this row as owned-Calibre.
    bid = await _insert_book(
        "Book", aid,
        description="A short stub.",   # 3 words — pre-v2.3.4 would smart-overwrite if new ≥9 words
        source="calibre",
    )

    # Source returns a 30-word description. Pre-v2.3.4: smart-overwrite
    # would have written through (stub detected). v2.3.4: queue.
    long_desc = " ".join(f"word{i}" for i in range(30))
    await _merge_result(
        author_id=aid, source_name="goodreads", languages=["English"],
        full_scan=True,
        result=AuthorResult(
            name="A", external_id="x",
            books=[BookResult(
                title="Book", source="goodreads",
                description=long_desc,
            )],
        ),
    )

    row = await _book_row(bid)
    # Books table untouched — stub stays.
    assert row["description"] == "A short stub."
    # Queue has the proposal.
    queue = await _queue_rows(bid)
    assert len(queue) == 1
    assert queue[0]["new_value"] == long_desc


# ── incremental mode: no queue activity ─────────────────────────────


async def test_incremental_mode_does_not_queue(discovery_db):
    """The new write rule lives inside the `if full_scan:` branch.
    Incremental scans still only write source_url + {source}_id —
    no metadata writes, no queue inserts."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    aid = await _insert_author("A")
    bid = await _insert_book("Book", aid, description="Curated")

    await _merge_result(
        author_id=aid, source_name="goodreads", languages=["English"],
        # full_scan=False (default) — incremental.
        result=AuthorResult(
            name="A", external_id="x",
            books=[BookResult(
                title="Book", source="goodreads",
                description="Source proposal",
                source_url="https://www.goodreads.com/book/show/1",
                external_id="gr-1",
            )],
        ),
    )

    row = await _book_row(bid)
    # URL written.
    assert json.loads(row["source_url"]) == {
        "goodreads": "https://www.goodreads.com/book/show/1",
    }
    # Description untouched.
    assert row["description"] == "Curated"
    # No queue inserts.
    assert await _queue_rows(bid) == []


# ── is_unreleased stays outside the rule ────────────────────────────


async def test_is_unreleased_always_overwrites(discovery_db):
    """Binary flag, not a reviewable diff. Existing always-overwrite
    behavior preserved: source's is_unreleased value goes straight
    into `books` regardless of prior state, no queue row."""
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    aid = await _insert_author("A")
    bid = await _insert_book("Book", aid)

    result = AuthorResult(
        name="A", external_id="x",
        books=[BookResult(
            title="Book", source="goodreads",
            is_unreleased=True,
        )],
    )
    await _merge_result(
        author_id=aid, source_name="goodreads", languages=["English"],
        full_scan=True, result=result,
    )

    assert (await _book_row(bid))["is_unreleased"] == 1
    # No queue row for is_unreleased.
    queue = await _queue_rows(bid)
    assert all(q["field"] != "is_unreleased" for q in queue)
