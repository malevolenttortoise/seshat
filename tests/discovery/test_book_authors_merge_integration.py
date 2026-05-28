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


async def test_empty_contributors_links_scanned_author(discovery_db):
    """v3.0.0 Phase 4 (ADR-0008) retires the 3.1 dormancy contract. A
    source that emits NO contributors (a book page with no parsable
    contributor block, or a source not yet emitting them) no longer
    leaves book_authors empty: the discovery INSERT always links the
    scanned author at position 0, so the book is visible to the
    now-authoritative book_authors read paths (author detail, counts,
    scan prefilter) the instant it's inserted."""
    chaney_id = await _insert_author("J.N. Chaney")

    new, _ = await _merge_result(
        author_id=chaney_id,
        result=_scan("goodreads"),  # contributors == []
        source_name="goodreads",
        languages=["English"],
    )

    assert new == 1
    assert await _book_count("Ruins of the Galaxy") == 1
    # Always-link: exactly the scanned author at position 0.
    assert await _book_authors_for("Ruins of the Galaxy") == [(0, "J.N. Chaney")]


# ─── multi-source convergence ────────────────────────────────


async def test_multi_source_convergence_complete_set_no_churn(discovery_db):
    """A second source re-encountering a book whose contributor set is
    already COMPLETE is a no-op. v3.0.1 (ADR-0014) re-links on the MATCH
    path, but delta-only: the heal reads the existing set, unions the
    source's authors, and finds nothing new → no write, no churn, no
    duplicate or lost links. The discovery-INSERT-time set stands. This
    preserves Phase 3.6's "convergence doesn't duplicate links" guarantee
    for the multi-source case while still allowing thin rows to heal
    (covered by the heal tests below)."""
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


# ─── scan-dedup: the co-author duplication pathology (Phase 4) ─


async def test_coauthored_owned_book_not_duplicated_on_coauthor_scan(discovery_db):
    """v3.0.0 Phase 4 (ADR-0008) — the Chaney/Anspach fix. A book OWNED
    under primary author A with co-author B (book_authors=[A,B]) must NOT
    be re-inserted as a new discovered row when co-author B is scanned.

    Before Phase 4 the scan prefilter only saw books where
    `books.author_id = B`, so A's co-authored book was invisible to B's
    scan and got duplicated. Now the prefilter reads `book_authors`, so
    B's scan matches A's owned row and the cross-author dedup suppresses
    the insert."""
    from app.discovery.database import get_db
    chaney_id = await _insert_author("J.N. Chaney")      # A — primary / owner
    anspach_id = await _insert_author("Jason Anspach")    # B — co-author

    # Owned co-authored book under A, linked to BOTH via book_authors
    # (what backfill / sync produce in prod).
    db = await get_db()
    try:
        # v3.0.0: books.author_id dropped; book_authors is seeded directly.
        cur = await db.execute(
            "INSERT INTO books (title, source, owned) "
            "VALUES ('Able Bodied Soldier', 'calibre', 1)",
        )
        bid = cur.lastrowid
        for pos, aid in enumerate((chaney_id, anspach_id)):
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, ?)",
                (bid, aid, pos),
            )
        await db.commit()
    finally:
        await db.close()

    # Scan co-author B — the source returns the same title.
    new, _ = await _merge_result(
        author_id=anspach_id,
        result=_scan(
            "goodreads",
            Contributor(name="Jason Anspach"),
            Contributor(name="J.N. Chaney"),
            title="Able Bodied Soldier",
        ),
        source_name="goodreads",
        languages=["English"],
    )

    assert new == 0                                  # matched A's owned row, no dup
    assert await _book_count("Able Bodied Soldier") == 1
    # The owned row's links are untouched.
    assert await _book_authors_for("Able Bodied Soldier") == [
        (0, "J.N. Chaney"),
        (1, "Jason Anspach"),
    ]


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


# ─── v3.0.1 heal on convergence (ADR-0014) ───────────────────


async def _seed_book(title, author_ids, *, owned=0, series_id=None, source="hardcover"):
    """Seed a pre-existing book + its position-ordered book_authors links
    directly — the thin/owned rows a later heal scan encounters. Mirrors
    what a pre-Phase-3 scan (single author) or owned-library sync produced."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO books (title, source, owned, series_id) VALUES (?, ?, ?, ?)",
            (title, source, owned, series_id),
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


async def _seed_series(name, author_id):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO series (name, author_id) VALUES (?, ?)", (name, author_id),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _series_author_mode(sid):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        return (await (await db.execute(
            "SELECT author_mode FROM series WHERE id = ?", (sid,),
        )).fetchone())["author_mode"]
    finally:
        await db.close()


async def test_heal_thin_unowned_book_unions_coauthor(discovery_db):
    """ADR-0014: a pre-3.0 thin UNOWNED discovered book (only the scanned
    author linked) gains its missing co-author when a later scan returns
    the full byline — union, existing-first. The MATCH path now re-links."""
    chaney_id = await _insert_author("J.N. Chaney")
    await _seed_book("Ruins of the Galaxy", [chaney_id], owned=0)

    new, _ = await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "goodreads",
            Contributor(name="J.N. Chaney"),
            Contributor(name="Jason Anspach"),  # missing co-author → healed in
        ),
        source_name="goodreads",
        languages=["English"],
    )

    assert new == 0  # matched the thin row, not a new book
    assert await _book_authors_for("Ruins of the Galaxy") == [
        (0, "J.N. Chaney"),
        (1, "Jason Anspach"),
    ]


async def test_heal_skips_owned_book(discovery_db):
    """ADR-0014 owned-guard: an OWNED thin book is NEVER re-linked by a
    scan — its book_authors is Calibre/ABS-authoritative. The co-author
    the source reports is ignored here (reconciled separately via the
    operator-approved write-back path, not a silent scan overwrite)."""
    chaney_id = await _insert_author("J.N. Chaney")
    await _insert_author("Jason Anspach")  # exists, but must NOT be linked here
    await _seed_book("Owned Thin Book", [chaney_id], owned=1, source="calibre")

    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "goodreads",
            Contributor(name="J.N. Chaney"),
            Contributor(name="Jason Anspach"),
            title="Owned Thin Book",
        ),
        source_name="goodreads",
        languages=["English"],
    )

    # Owned row untouched — still just the scanned author.
    assert await _book_authors_for("Owned Thin Book") == [(0, "J.N. Chaney")]


async def test_heal_single_author_source_is_noop(discovery_db):
    """ADR-0014 cheap pre-gate: a source naming ≤1 author-role contributor
    has nothing to add (the scanned author is already position 0), so the
    heal short-circuits and the set is unchanged."""
    chaney_id = await _insert_author("J.N. Chaney")
    await _seed_book("Solo Title", [chaney_id], owned=0)

    await _merge_result(
        author_id=chaney_id,
        result=_scan(
            "goodreads",
            Contributor(name="J.N. Chaney"),  # only the scanned author
            title="Solo Title",
        ),
        source_name="goodreads",
        languages=["English"],
    )

    assert await _book_authors_for("Solo Title") == [(0, "J.N. Chaney")]


async def test_heal_append_only_does_not_reorder(discovery_db):
    """ADR-0014 append-only: the existing position 0 (the scanned author)
    is preserved even when the source lists a co-author FIRST. The new
    co-author is appended, never reordered to the source's primary — which
    keeps the row re-healable (the MATCH only fires when position 0 ==
    the scanned author)."""
    anspach_id = await _insert_author("Jason Anspach")
    await _seed_book("Galaxy's Edge 1", [anspach_id], owned=0)

    await _merge_result(
        author_id=anspach_id,
        result=_scan(
            "goodreads",
            Contributor(name="J.N. Chaney"),     # source lists Chaney FIRST
            Contributor(name="Jason Anspach"),   # scanned author second
            title="Galaxy's Edge 1",
        ),
        source_name="goodreads",
        languages=["English"],
    )

    # Anspach STAYS at 0 (not reordered to the source's Chaney-first);
    # Chaney appended at 1.
    assert await _book_authors_for("Galaxy's Edge 1") == [
        (0, "Jason Anspach"),
        (1, "J.N. Chaney"),
    ]


async def test_heal_recomputes_series_author_mode(discovery_db):
    """ADR-0014 end-to-end: healing thin UNOWNED series members flips the
    series author_mode per_author → multi_author. This is the live id=217
    pathology — the load-bearing recompute fires once at end-of-scan
    because a real delta was healed."""
    from app.discovery.routers.series import _recompute_series_author
    from app.discovery.database import get_db

    anspach_id = await _insert_author("Jason Anspach")
    await _insert_author("J.N. Chaney")  # exists; healed in
    sid = await _seed_series("Able Bodied Soldier", anspach_id)
    await _seed_book("ABS Book 1", [anspach_id], owned=0, series_id=sid)
    await _seed_book("ABS Book 2", [anspach_id], owned=0, series_id=sid)

    # Baseline: both books thin {Anspach} → intersection {Anspach} → per_author.
    db = await get_db()
    try:
        await _recompute_series_author(db, [sid])
        await db.commit()
    finally:
        await db.close()
    assert await _series_author_mode(sid) == "per_author"

    # Re-scan Anspach: source returns the full [Anspach, Chaney] byline.
    result = AuthorResult(
        name="scan-driver",
        external_id="ext-abs",
        books=[
            BookResult(
                title="ABS Book 1", source="goodreads",
                contributors=[Contributor(name="Jason Anspach"),
                              Contributor(name="J.N. Chaney")],
            ),
            BookResult(
                title="ABS Book 2", source="goodreads",
                contributors=[Contributor(name="Jason Anspach"),
                              Contributor(name="J.N. Chaney")],
            ),
        ],
    )
    await _merge_result(
        author_id=anspach_id, result=result,
        source_name="goodreads", languages=["English"],
    )

    # Both healed → intersection {Anspach, Chaney} → multi_author.
    assert await _book_authors_for("ABS Book 1") == [
        (0, "Jason Anspach"), (1, "J.N. Chaney"),
    ]
    assert await _book_authors_for("ABS Book 2") == [
        (0, "Jason Anspach"), (1, "J.N. Chaney"),
    ]
    assert await _series_author_mode(sid) == "multi_author"
