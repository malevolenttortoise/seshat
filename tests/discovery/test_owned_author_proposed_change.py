"""v3.3.0 slice 01 (ADR-0017) — owned-book author discrepancy enqueue.

Drives the OWNED branch of the `_merge_result` MATCH path: instead of
silently overwriting Calibre/ABS-authoritative `book_authors`, the
scan-converged source's contributor disagreement is enqueued in
`metadata_review_queue` with `field='authors'` and a JSON payload
carrying source-IDs alongside names. The operator reviews and approves
via the slice 02 push-back path; slice 01 only enqueues.

Threshold (ADR-0017 §2): source_set ⊄ current_set OR source primary
differs from current primary. Pure-subset and cosmetic reorderings are
skipped — Calibre is authoritative on removals.

Union payload (ADR-0017 §3): source-primary-first, then source's
remaining, then current's exclusives appended (additive-only — never
silently removes a Calibre-asserted contributor).

Source-quality filter (ADR-0017 §2): only `goodreads`/`amazon`/`hardcover`/
`audible` enqueue; link-only sources excluded; MAM doesn't reach
`_merge_result` for owned books.
"""
from __future__ import annotations

import json

import pytest

from app.discovery.lookup import _merge_result
from app.discovery.sources.base import (
    AuthorResult,
    BookResult,
    Contributor,
)


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


# ─── helpers ─────────────────────────────────────────────────


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


async def _seed_owned_book(title, author_ids, *, source="calibre"):
    """Seed an OWNED book (owned=1) with position-ordered book_authors.
    Mirrors what Phase 2 sync produces from a Calibre row."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO books (title, source, owned) VALUES (?, ?, 1)",
            (title, source),
        )
        bid = cur.lastrowid
        for pos, aid in enumerate(author_ids):
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, ?)",
                (bid, aid, pos),
            )
        await db.commit()
        return bid
    finally:
        await db.close()


async def _queue_rows_for_book(book_id: int):
    """Return all metadata_review_queue rows for a book, ordered by
    `(field, source, proposed_at)` so assertions are stable."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT field, source, old_value, new_value, proposed_at "
            "FROM metadata_review_queue WHERE book_id = ? "
            "ORDER BY field, source, proposed_at",
            (book_id,),
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


def _scan(source: str, title: str, *contributors: Contributor, scan_name: str = "scan-driver") -> AuthorResult:
    """Single-standalone-book AuthorResult carrying a contributor list."""
    return AuthorResult(
        name=scan_name,
        external_id="ext-1",
        books=[BookResult(title=title, source=source, contributors=list(contributors))],
    )


# ─── threshold cases ─────────────────────────────────────────


async def test_pure_additive_source_has_missing_coauthor_enqueues(discovery_db):
    """ADR-0017 §2 trigger: source proposes ≥1 contributor not in current
    (set not a subset). Owned thin row + source naming a real co-author →
    proposal enqueued; book_authors itself untouched (owned-guard)."""
    chaney_id = await _insert_author("J.N. Chaney")
    bid = await _seed_owned_book("Owned Co-Authored", [chaney_id])

    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "goodreads",
            "Owned Co-Authored",
            Contributor(name="J.N. Chaney", source_author_id="gr-chaney"),
            Contributor(name="Jason Anspach", source_author_id="gr-anspach"),
        ),
        source_name="goodreads",
        languages=["English"],
    )

    rows = await _queue_rows_for_book(bid)
    assert len(rows) == 1
    row = rows[0]
    assert row["field"] == "authors"
    assert row["source"] == "goodreads"
    new_payload = json.loads(row["new_value"])
    old_payload = json.loads(row["old_value"])
    # Union, source-primary-first: source then current's exclusives.
    # Source had [Chaney, Anspach]; current had [Chaney]; no exclusives.
    assert new_payload == [
        {"name": "J.N. Chaney", "source_id": "gr-chaney"},
        {"name": "Jason Anspach", "source_id": "gr-anspach"},
    ]
    assert old_payload == [{"name": "J.N. Chaney", "source_id": None}]


async def test_primary_differs_enqueues_even_when_sets_equal(discovery_db):
    """ADR-0017 §2 second trigger: source contributor SET equals current but
    position 0 differs. Source-primary-first union puts the source's primary
    at position 0 in `new_value`; current's primary moves to a later slot.
    """
    chaney_id = await _insert_author("J.N. Chaney")
    anspach_id = await _insert_author("Jason Anspach")
    # Current order: Chaney primary, Anspach secondary.
    bid = await _seed_owned_book("Primary Swap", [chaney_id, anspach_id])

    # Source insists Anspach is primary, Chaney is secondary.
    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "goodreads",
            "Primary Swap",
            Contributor(name="Jason Anspach", source_author_id="gr-anspach"),
            Contributor(name="J.N. Chaney", source_author_id="gr-chaney"),
        ),
        source_name="goodreads",
        languages=["English"],
    )

    rows = await _queue_rows_for_book(bid)
    assert len(rows) == 1
    new_payload = json.loads(rows[0]["new_value"])
    assert new_payload == [
        {"name": "Jason Anspach", "source_id": "gr-anspach"},
        {"name": "J.N. Chaney", "source_id": "gr-chaney"},
    ]


async def test_mixed_diff_preserves_current_exclusives_via_union(discovery_db):
    """ADR-0017 §3 union additive-only: source has new + lacks some.
    Source [X, Z] + current [X, Y] → union [X, Z, Y]. Y survives because
    Calibre asserts it — the operator can only ADD via approval, never
    silently remove."""
    x_id = await _insert_author("X Author")
    y_id = await _insert_author("Y Author")
    await _insert_author("Z Author")
    bid = await _seed_owned_book("Mixed Diff", [x_id, y_id])

    await _merge_result(
        author_id=x_id,
        result=_scan(
            "hardcover",
            "Mixed Diff",
            Contributor(name="X Author", source_author_id="hc-x"),
            Contributor(name="Z Author", source_author_id="hc-z"),
        ),
        source_name="hardcover",
        languages=["English"],
    )

    rows = await _queue_rows_for_book(bid)
    assert len(rows) == 1
    new_payload = json.loads(rows[0]["new_value"])
    # Source first (X, Z), then current's exclusives (Y).
    assert new_payload == [
        {"name": "X Author", "source_id": "hc-x"},
        {"name": "Z Author", "source_id": "hc-z"},
        {"name": "Y Author", "source_id": None},
    ]


async def test_subset_source_skipped_no_enqueue(discovery_db):
    """ADR-0017 §2 skip-condition: source is a strict subset of current
    AND primary matches → Calibre is authoritative on removals; no
    proposal is enqueued."""
    x_id = await _insert_author("X Author")
    y_id = await _insert_author("Y Author")
    bid = await _seed_owned_book("Subset Source", [x_id, y_id])

    await _merge_result(
        author_id=x_id,
        result=_scan(
            "amazon",
            "Subset Source",
            Contributor(name="X Author", source_author_id="amzn-x"),
            # No Y — source only has the primary.
        ),
        source_name="amazon",
        languages=["English"],
    )

    rows = await _queue_rows_for_book(bid)
    assert rows == []


async def test_cosmetic_reorder_no_primary_change_skipped(discovery_db):
    """ADR-0017 §2 skip-condition: sets equal AND primary unchanged →
    no enqueue. Source reorders non-primary positions but current's
    position-0 still matches source's position-0. Cosmetic, skip."""
    x_id = await _insert_author("X Author")
    y_id = await _insert_author("Y Author")
    z_id = await _insert_author("Z Author")
    bid = await _seed_owned_book("Cosmetic Reorder", [x_id, y_id, z_id])

    await _merge_result(
        author_id=x_id,
        result=_scan(
            "audible",
            "Cosmetic Reorder",
            Contributor(name="X Author", source_author_id="aud-x"),
            Contributor(name="Z Author", source_author_id="aud-z"),
            Contributor(name="Y Author", source_author_id="aud-y"),
        ),
        source_name="audible",
        languages=["English"],
    )

    rows = await _queue_rows_for_book(bid)
    assert rows == []


# ─── source-quality filter ──────────────────────────────────


@pytest.mark.parametrize("source", ["google_books", "openlibrary"])
async def test_link_only_sources_never_enqueue(discovery_db, source):
    """ADR-0017 §2 source-quality filter: link-only sources have weak
    author data and don't enqueue, even with a real disagreement that
    would trigger for a trusted source."""
    chaney_id = await _insert_author("J.N. Chaney")
    bid = await _seed_owned_book("Link-Only Test", [chaney_id])

    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            source,
            "Link-Only Test",
            Contributor(name="J.N. Chaney"),
            Contributor(name="Jason Anspach"),
        ),
        source_name=source,
        languages=["English"],
    )

    rows = await _queue_rows_for_book(bid)
    assert rows == []


# ─── UPSERT semantics ──────────────────────────────────────


async def test_rescan_upserts_replaces_prior_proposal(discovery_db):
    """ADR-0017 §1 UPSERT: a fresh scan replaces the prior pending
    proposal (UNIQUE on `(book_id, field='authors', source)` via
    INSERT OR REPLACE) — proposals don't pile up across scans of the
    same author/source combination."""
    chaney_id = await _insert_author("J.N. Chaney")
    bid = await _seed_owned_book("UPSERT Target", [chaney_id])

    # First scan proposes Anspach.
    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "goodreads",
            "UPSERT Target",
            Contributor(name="J.N. Chaney", source_author_id="gr-chaney"),
            Contributor(name="Jason Anspach", source_author_id="gr-anspach"),
        ),
        source_name="goodreads",
        languages=["English"],
    )
    # Second scan from same source proposes a different co-author.
    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "goodreads",
            "UPSERT Target",
            Contributor(name="J.N. Chaney", source_author_id="gr-chaney"),
            Contributor(name="New Coauthor", source_author_id="gr-new"),
        ),
        source_name="goodreads",
        languages=["English"],
    )

    rows = await _queue_rows_for_book(bid)
    assert len(rows) == 1, "second scan should REPLACE, not duplicate"
    new_payload = json.loads(rows[0]["new_value"])
    # Latest proposal won — the prior Anspach proposal is gone.
    assert new_payload == [
        {"name": "J.N. Chaney", "source_id": "gr-chaney"},
        {"name": "New Coauthor", "source_id": "gr-new"},
    ]


async def test_different_sources_enqueue_independent_proposals(discovery_db):
    """ADR-0017 §1: UNIQUE is `(book_id, field, source)` — different
    sources enqueue independent proposals for the same book/field. The
    operator reviews each independently (rejected alternative: cross-
    source merging into one 'best' proposal would require trust
    arbitration and discards the 'operator decides' frame)."""
    chaney_id = await _insert_author("J.N. Chaney")
    bid = await _seed_owned_book("Multi-Source Target", [chaney_id])

    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "goodreads",
            "Multi-Source Target",
            Contributor(name="J.N. Chaney"),
            Contributor(name="Co A"),
        ),
        source_name="goodreads",
        languages=["English"],
    )
    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "amazon",
            "Multi-Source Target",
            Contributor(name="J.N. Chaney"),
            Contributor(name="Co B"),
        ),
        source_name="amazon",
        languages=["English"],
    )

    rows = await _queue_rows_for_book(bid)
    assert len(rows) == 2
    sources = sorted(r["source"] for r in rows)
    assert sources == ["amazon", "goodreads"]


# ─── owned-guard: book_authors itself untouched ────────────


async def test_owned_book_authors_unchanged_when_proposal_enqueued(discovery_db):
    """ADR-0017 §3 + caller's owned-guard: enqueueing a proposal must NOT
    mutate the owned book's book_authors. Calibre/ABS stays authoritative
    until the operator approves the proposal and slice 02's push-back
    + re-sync runs."""
    chaney_id = await _insert_author("J.N. Chaney")
    bid = await _seed_owned_book("Owned Untouched", [chaney_id])

    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "goodreads",
            "Owned Untouched",
            Contributor(name="J.N. Chaney"),
            Contributor(name="Jason Anspach"),
        ),
        source_name="goodreads",
        languages=["English"],
    )

    from app.discovery.database import get_db
    db = await get_db()
    try:
        ba_rows = await (await db.execute(
            "SELECT a.name, ba.position FROM book_authors ba "
            "JOIN authors a ON a.id = ba.author_id "
            "WHERE ba.book_id = ? ORDER BY ba.position",
            (bid,),
        )).fetchall()
        names = [(r["position"], r["name"]) for r in ba_rows]
    finally:
        await db.close()
    # Owned row's book_authors is the seeded baseline — proposal-only.
    assert names == [(0, "J.N. Chaney")]
