"""v3.0.0 Phase 3.6 — multi-author contributor pipeline END-TO-END.

The per-source unit suites (3.2 Goodreads, 3.3 Hardcover, 3.4 Amazon,
3.5 Audnexus + Google Books) prove each parser emits the right
``BookResult.contributors``; `test_book_authors_discovery.py` proves
`_link_discovered_contributors` in isolation. This file is the
integration gate: it drives the **real** `_merge_result` INSERT path
(lookup.py:1836-1839) so the contract is verified through the same code
a live source scan runs — role filter + trusted-create/link-only gate +
never-orphan + ordered `book_authors` rows, plus multi-source
convergence (the discovery-INSERT-time write contract).

Fixtures mirror the dev-stack co-author shapes: the J.N. Chaney + Jason
Anspach trigger and a 7-author anthology (the Men's Romance gallery).
"""
from __future__ import annotations

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


async def _book_authors_for(title: str) -> list[tuple[int, str]]:
    """(position, author_name) tuples in position order for a book title."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT ba.position, a.name "
            "FROM book_authors ba "
            "JOIN authors a ON a.id = ba.author_id "
            "JOIN books b ON b.id = ba.book_id "
            "WHERE b.title = ? ORDER BY ba.position",
            (title,),
        )).fetchall()
        return [(r["position"], r["name"]) for r in rows]
    finally:
        await db.close()


async def _author_count() -> int:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        return (await (await db.execute(
            "SELECT COUNT(*) AS n FROM authors"
        )).fetchone())["n"]
    finally:
        await db.close()


async def _book_count(title: str) -> int:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        return (await (await db.execute(
            "SELECT COUNT(*) AS n FROM books WHERE title = ?", (title,),
        )).fetchone())["n"]
    finally:
        await db.close()


def _scan(source: str, *contributors: Contributor, title="Ruins of the Galaxy") -> AuthorResult:
    """A single-standalone-book AuthorResult carrying a contributor list."""
    return AuthorResult(
        name="scan-driver",
        external_id="ext-1",
        books=[BookResult(title=title, source=source, contributors=list(contributors))],
    )


# ─── trusted-create sources (mint unmatched co-authors) ──────


# MAM is in TRUSTED_CREATE_SOURCES but is NOT a discovery source — it has
# no search_author / AuthorResult and never drives `_merge_result` (Phase
# 3.5 locked: MAM is enrichment-only, its multi-author handling lives in
# the grab auto-train path, not the discovery merge). Its trusted-create
# membership is asserted at the unit level in test_book_authors_discovery.
# So the discovery-merge parametrize covers only the four sources that
# actually reach `_merge_result`.
@pytest.mark.parametrize("source", ["goodreads", "amazon", "hardcover", "audible"])
async def test_trusted_source_mints_coauthor_through_merge(discovery_db, source):
    """Every trusted-create DISCOVERY source: a role-clean co-author the
    library has never seen is MINTED and linked in source order, with the
    scanned author at position 0."""
    chaney_id = await _insert_author("J.N. Chaney")

    new, _ = await _merge_result(
        author_id=chaney_id,
        result=_scan(
            source,
            Contributor(name="J.N. Chaney"),      # = scanned (existing)
            Contributor(name="Jason Anspach"),     # unknown → minted
        ),
        source_name=source,
        languages=["English"],
    )

    assert new == 1
    assert await _book_authors_for("Ruins of the Galaxy") == [
        (0, "J.N. Chaney"),
        (1, "Jason Anspach"),
    ]
    assert await _author_count() == 2  # Anspach minted


async def test_role_filtered_contributor_dropped_through_merge(discovery_db):
    """A non-empty role label (illustrator / translator / narrator) is
    dropped through the real merge — only the authors are linked. Uses
    a localized label to confirm we allowlist, never blocklist."""
    chaney_id = await _insert_author("J.N. Chaney")

    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "hardcover",
            Contributor(name="J.N. Chaney"),
            Contributor(name="Jason Anspach"),
            Contributor(name="Some Illustrator", role="Illustratore"),  # localized → dropped
            Contributor(name="Some Translator", role="Translator"),
        ),
        source_name="hardcover",
        languages=["English"],
    )

    assert await _book_authors_for("Ruins of the Galaxy") == [
        (0, "J.N. Chaney"),
        (1, "Jason Anspach"),
    ]
    # Illustrator + Translator never minted.
    assert await _author_count() == 2


# ─── link-only sources (resolve existing, never mint) ────────


@pytest.mark.parametrize("source", ["openlibrary", "google_books"])
async def test_link_only_source_does_not_mint_through_merge(discovery_db, source):
    """A link-only source must NOT mint an unmatched co-author (its
    flat, untyped lists can lump illustrators into the author field).
    The unknown is dropped; the scanned author is still linked so the
    discovered book is never orphaned."""
    chaney_id = await _insert_author("J.N. Chaney")

    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            source,
            Contributor(name="J.N. Chaney"),
            Contributor(name="Totally Unknown Person"),  # link-only → dropped
        ),
        source_name=source,
        languages=["English"],
    )

    assert await _book_authors_for("Ruins of the Galaxy") == [(0, "J.N. Chaney")]
    assert await _author_count() == 1  # nothing minted


async def test_link_only_resolves_existing_coauthor_through_merge(discovery_db):
    """Link-only ≠ no-link: when the co-author already exists as a
    per-library row, a link-only source resolves and links it (it just
    won't CREATE a new one)."""
    chaney_id = await _insert_author("J.N. Chaney")
    await _insert_author("Jason Anspach")  # already in the library

    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "google_books",
            Contributor(name="J.N. Chaney"),
            Contributor(name="Jason Anspach"),  # exists → resolves + links
        ),
        source_name="google_books",
        languages=["English"],
    )

    assert await _book_authors_for("Ruins of the Galaxy") == [
        (0, "J.N. Chaney"),
        (1, "Jason Anspach"),
    ]
    assert await _author_count() == 2  # no new mint


# ─── never-orphan safety net ─────────────────────────────────


async def test_scanned_author_never_orphaned_through_merge(discovery_db):
    """If the source's contributor list omits the scanned author
    entirely (mis-spelling, partial parse), they are appended so the
    book is never orphaned from the author whose scan surfaced it."""
    chaney_id = await _insert_author("J.N. Chaney")

    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "goodreads",
            Contributor(name="Jason Anspach"),  # scanned author missing from list
        ),
        source_name="goodreads",
        languages=["English"],
    )

    # Anspach (minted) leads the source list at 0; scanned Chaney appended at 1.
    assert await _book_authors_for("Ruins of the Galaxy") == [
        (0, "Jason Anspach"),
        (1, "J.N. Chaney"),
    ]


async def test_empty_contributors_writes_no_book_authors(discovery_db):
    """The 3.1 dormancy contract, end-to-end: a source that emits no
    contributors (any pre-3.2 source, or a book page with no parsable
    contributor block) inserts the book under the legacy author_id but
    writes ZERO book_authors rows. No phantom single-author links."""
    chaney_id = await _insert_author("J.N. Chaney")

    new, _ = await _merge_result(
        author_id=chaney_id,
        result=_scan("goodreads"),  # contributors == []
        source_name="goodreads",
        languages=["English"],
    )

    assert new == 1
    assert await _book_count("Ruins of the Galaxy") == 1   # book still inserted
    assert await _book_authors_for("Ruins of the Galaxy") == []  # but no links


# ─── multi-source convergence ────────────────────────────────


async def test_multi_source_convergence_no_duplicate_links(discovery_db):
    """book_authors is written at discovery-INSERT time. A second
    source re-encountering the same title MATCHES the existing row
    (metadata/URL enrichment via `_update_existing`) and does NOT
    re-link — so the first scan's contributor set stands, with no
    duplicate or lost links. (Read/enrich rewire is Phase 4.)"""
    chaney_id = await _insert_author("J.N. Chaney")

    # First scan: Goodreads, full co-author list → mints + links both.
    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "goodreads",
            Contributor(name="J.N. Chaney"),
            Contributor(name="Jason Anspach"),
        ),
        source_name="goodreads",
        languages=["English"],
    )
    first = await _book_authors_for("Ruins of the Galaxy")
    assert first == [(0, "J.N. Chaney"), (1, "Jason Anspach")]

    # Second scan: Amazon, same title → matched row, no new book.
    new, _ = await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "amazon",
            Contributor(name="J.N. Chaney"),
            Contributor(name="Jason Anspach"),
        ),
        source_name="amazon",
        languages=["English"],
    )

    assert new == 0                          # re-encounter, not a new book
    assert await _book_count("Ruins of the Galaxy") == 1
    # No dupes, no loss — the discovery-INSERT-time set stands.
    assert await _book_authors_for("Ruins of the Galaxy") == first


# ─── scale / ordering — the 7-author anthology fixture ───────


async def test_seven_author_anthology_linked_in_source_order(discovery_db):
    """The Men's Romance gallery shape: a trusted source returns a book
    with seven authors. All seven are linked in source order
    (positions 0..6), the scanned author leading."""
    gallery = [
        "Misty Vixen", "Marcus Sloss", "Michael Dalton", "Neil Bimbeau",
        "Adam Lance", "Eric Vall", "Aaron Crash",
    ]
    scanned_id = await _insert_author(gallery[0])

    await _merge_result(
        author_id=scanned_id,
        result=_scan(
            "amazon",
            *[Contributor(name=n) for n in gallery],
            title="Harem Gallery",
        ),
        source_name="amazon",
        languages=["English"],
    )

    rows = await _book_authors_for("Harem Gallery")
    assert rows == list(enumerate(gallery))
    assert await _author_count() == 7  # 6 co-authors minted alongside the scanned one


async def test_duplicate_contributor_names_collapse(discovery_db):
    """A source listing the same author twice (e.g. once typed as
    author, once mis-tagged) collapses to a single link at the earlier
    position — `write_book_authors` dedups while preserving order."""
    chaney_id = await _insert_author("J.N. Chaney")

    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "goodreads",
            Contributor(name="J.N. Chaney"),
            Contributor(name="Jason Anspach"),
            Contributor(name="Jason Anspach"),  # duplicate
        ),
        source_name="goodreads",
        languages=["English"],
    )

    assert await _book_authors_for("Ruins of the Galaxy") == [
        (0, "J.N. Chaney"),
        (1, "Jason Anspach"),
    ]
