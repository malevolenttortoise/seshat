"""v3.0.0 Phase 3 (step 3.1) — discovery-side book_authors plumbing.

Step 3.1 lands the contract + wiring with ZERO behavior change until a
source populates `BookResult.contributors` (the 3.2+ parser work):

  - `Contributor` dataclass + `BookResult.contributors` field.
  - `contributor_is_author` role-filter — ALLOWLIST the empty/author
    role, DROP every other label (never blocklist; vocabularies are
    localized).
  - `resolve_or_create_author` — resolve a contributor name to a
    per-library `authors.id` (exact name → exact normalized → fuzzy),
    minting only for trusted-create sources.
  - `_link_discovered_contributors` — the lookup-side orchestration,
    dormant on an empty contributor list.

These cover the helpers in isolation + the dormant-vs-active wiring.
End-to-end per-source population is the 3.2+ work (and its dev-stack
UAT against the Chaney+Anspach / 7-author fixtures).
"""
from __future__ import annotations

import pytest

from app.discovery.sources.base import (
    BookResult,
    Contributor,
    contributor_is_author,
    TRUSTED_CREATE_SOURCES,
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


async def _book_authors(book_id: int) -> list[tuple[int, str]]:
    """Return (position, author_name) tuples in position order."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT ba.position, a.name "
            "FROM book_authors ba JOIN authors a ON a.id = ba.author_id "
            "WHERE ba.book_id = ? ORDER BY ba.position",
            (book_id,),
        )).fetchall()
        return [(r["position"], r["name"]) for r in rows]
    finally:
        await db.close()


# ─── contributor_is_author (role filter) ─────────────────────


@pytest.mark.parametrize("role,expected", [
    (None, True),                 # plain author (no role tag)
    ("", True),                   # empty label = author
    ("  ", True),                 # whitespace-only = author
    ("Author", True),
    ("author", True),
    (" AUTHOR ", True),
    ("Illustrator", False),
    ("Illustratore", False),      # localized — must still drop
    ("Translator", False),
    ("Artist", False),            # Amazon's word for illustrator
    ("Colorist", False),          # Goodreads
    ("Narrator", False),
])
def test_role_filter_allowlists_only_author(role, expected):
    assert contributor_is_author(role) is expected


def test_trusted_create_set_membership():
    """Trusted-create sources may mint; everyone else is link-only."""
    assert TRUSTED_CREATE_SOURCES == frozenset(
        {"goodreads", "amazon", "hardcover", "audible", "mam"}
    )
    assert "openlibrary" not in TRUSTED_CREATE_SOURCES
    assert "google_books" not in TRUSTED_CREATE_SOURCES


# ─── resolve_or_create_author ────────────────────────────────


async def test_resolve_exact_name(discovery_db):
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (1, 'J.N. Chaney', 'Chaney, J.N.', ?)",
            ("jn chaney",),
        )
        aid = await resolve_or_create_author(db, "J.N. Chaney", allow_create=False)
    finally:
        await db.close()
    assert aid == 1


async def test_resolve_normalized_punctuation_drift(discovery_db):
    """Exact-name misses but normalized_name matches (period drift)."""
    from app.discovery.database import get_db, resolve_or_create_author
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (1, 'A. K. DuBoff', 'DuBoff, A. K.', ?)",
            (normalize_author_name("A. K. DuBoff"),),
        )
        # Different punctuation/spacing, same person.
        aid = await resolve_or_create_author(db, "A K DuBoff", allow_create=False)
    finally:
        await db.close()
    assert aid == 1


async def test_resolve_fuzzy_match(discovery_db):
    """No exact/normalized hit, but authors_match (>=0.92) catches it."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        # Stored normalized form differs enough to miss the exact
        # normalized lookup, but is within SequenceMatcher 0.92.
        await db.execute(
            "INSERT INTO authors (id, name, sort_name, normalized_name) "
            "VALUES (1, 'Jonathan P. Brazee', 'Brazee, Jonathan P.', 'jonathan p brazee')"
        )
        aid = await resolve_or_create_author(
            db, "Jonathan P Brazee", allow_create=False,
        )
    finally:
        await db.close()
    assert aid == 1


async def test_resolve_miss_link_only_returns_none(discovery_db):
    """allow_create=False (link-only source) drops an unmatched name
    rather than minting an unvetted author row."""
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        aid = await resolve_or_create_author(
            db, "Someone Brand New", allow_create=False,
        )
        # Confirm no row was created.
        n = (await (await db.execute(
            "SELECT COUNT(*) AS n FROM authors"
        )).fetchone())["n"]
    finally:
        await db.close()
    assert aid is None
    assert n == 0


async def test_resolve_miss_trusted_create_mints_author(discovery_db):
    """allow_create=True (trusted source) mints a name-only author row."""
    from app.discovery.database import get_db, resolve_or_create_author
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        aid = await resolve_or_create_author(
            db, "Thomas Webb", allow_create=True,
        )
        row = await (await db.execute(
            "SELECT name, sort_name, normalized_name FROM authors WHERE id = ?",
            (aid,),
        )).fetchone()
    finally:
        await db.close()
    assert aid is not None
    assert row["name"] == "Thomas Webb"
    assert row["sort_name"] == "Thomas Webb"          # sort defaults to name
    assert row["normalized_name"] == normalize_author_name("Thomas Webb")


async def test_resolve_empty_name_returns_none(discovery_db):
    from app.discovery.database import get_db, resolve_or_create_author
    db = await get_db()
    try:
        assert await resolve_or_create_author(db, "", allow_create=True) is None
        assert await resolve_or_create_author(db, "   ", allow_create=True) is None
    finally:
        await db.close()


# ─── _link_discovered_contributors (lookup wiring) ───────────


async def _seed_book(db, *, author_id=1, author_name="J.N. Chaney"):
    await db.execute(
        "INSERT INTO authors (id, name, sort_name, normalized_name) "
        "VALUES (?, ?, ?, ?)",
        (author_id, author_name, author_name, author_name.lower().replace(".", "")),
    )
    cur = await db.execute(
        "INSERT INTO books (title, source, owned, is_new) "
        "VALUES ('Discovered Book', 'goodreads', 0, 1)",
    )
    book_id = cur.lastrowid
    await db.execute(
        "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, ?, 0)",
        (book_id, author_id),
    )
    return book_id


async def test_link_empty_contributors_links_scanned_author(discovery_db):
    """v3.0.0 Phase 4 (ADR-0008) retires the 3.1 dormancy contract: an
    empty contributor list no longer writes ZERO rows — the scanned
    author is always linked at position 0, so the discovered book is
    visible to the now-authoritative book_authors read paths the
    instant it's inserted."""
    from app.discovery.database import get_db
    from app.discovery.lookup import _link_discovered_contributors
    db = await get_db()
    try:
        book_id = await _seed_book(db)
        bk = BookResult(title="Discovered Book", source="goodreads")  # contributors == []
        await _link_discovered_contributors(db, book_id, 1, bk, "goodreads")
        await db.commit()
    finally:
        await db.close()
    assert await _book_authors(book_id) == [(0, "J.N. Chaney")]


async def test_link_trusted_source_mints_and_orders(discovery_db):
    """A trusted source's contributor list: role-filtered, resolved
    (minting unknowns), written in source order with the scanned
    author at position 0 (it leads the list)."""
    from app.discovery.database import get_db
    from app.discovery.lookup import _link_discovered_contributors
    db = await get_db()
    try:
        book_id = await _seed_book(db, author_id=1, author_name="J.N. Chaney")
        bk = BookResult(
            title="Discovered Book", source="goodreads",
            contributors=[
                Contributor(name="J.N. Chaney"),                 # = scanned (existing)
                Contributor(name="Jason Anspach"),               # new → minted
                Contributor(name="Some Illustrator", role="Illustrator"),  # dropped
            ],
        )
        await _link_discovered_contributors(db, book_id, 1, bk, "goodreads")
        await db.commit()
    finally:
        await db.close()
    assert await _book_authors(book_id) == [(0, "J.N. Chaney"), (1, "Jason Anspach")]


async def test_link_link_only_source_drops_unknown_coauthor(discovery_db):
    """A link-only source (OpenLibrary) must NOT mint an unmatched
    co-author. The unknown is dropped; the scanned author is still
    guaranteed a link so the book isn't orphaned."""
    from app.discovery.database import get_db
    from app.discovery.lookup import _link_discovered_contributors
    db = await get_db()
    try:
        book_id = await _seed_book(db, author_id=1, author_name="J.N. Chaney")
        bk = BookResult(
            title="Discovered Book", source="openlibrary",
            contributors=[
                Contributor(name="J.N. Chaney"),       # existing → resolves
                Contributor(name="Sam Sykes"),          # unknown → link-only drop
            ],
        )
        await _link_discovered_contributors(db, book_id, 1, bk, "openlibrary")
        await db.commit()
        n_authors = (await (await db.execute(
            "SELECT COUNT(*) AS n FROM authors"
        )).fetchone())["n"]
    finally:
        await db.close()
    assert await _book_authors(book_id) == [(0, "J.N. Chaney")]
    assert n_authors == 1  # Sam Sykes was NOT minted


async def test_link_scanned_author_appended_when_source_omits(discovery_db):
    """Safety net: if the source's contributor list doesn't include the
    scanned author at all, they're appended so the discovered book is
    never orphaned from the author whose scan surfaced it."""
    from app.discovery.database import get_db
    from app.discovery.lookup import _link_discovered_contributors
    db = await get_db()
    try:
        book_id = await _seed_book(db, author_id=1, author_name="J.N. Chaney")
        bk = BookResult(
            title="Discovered Book", source="goodreads",
            contributors=[Contributor(name="Jason Anspach")],  # scanned author missing
        )
        await _link_discovered_contributors(db, book_id, 1, bk, "goodreads")
        await db.commit()
    finally:
        await db.close()
    # Jason Anspach (minted) at 0, scanned J.N. Chaney appended at 1.
    assert await _book_authors(book_id) == [(0, "Jason Anspach"), (1, "J.N. Chaney")]
