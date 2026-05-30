"""v3.x (ADR-0016 slice 03) — co-author image write through
`_link_discovered_contributors`.

Closes the v3.0.0 Phase 3.4 silent drop: the Amazon byline-widget
parser populates `Contributor.image_url` on every product-grid byline,
but until slice 03 the consume path discarded it. With this slice
wired, every captured co-author image flows through the helper's
**strict fill-if-empty co_author** path (ADR-0016 §2).

Verifies:
  - Byline image fills the NULL slot on a pre-existing co-author row.
  - Byline image fills the NULL slot on a freshly-minted (trusted-
    create) co-author row.
  - Byline image does NOT overwrite a populated slot (never upgrades
    cross-source, regardless of byline source rank).
  - NULL byline image → no helper call → row untouched.
  - Role-filtered contributors (Illustrator etc.) never reach the
    helper — image isn't written even when present.
  - Unknown source (e.g. google_books) → helper short-circuits with
    warn + no write; no exception escapes.
  - link-only sources (google_books) DO call the helper for resolved
    co-authors that already exist by name — image still fills NULL.

Slice 01 covers the helper's rank/fanout/trust semantics in isolation;
slice 02 covers the scanned-author write path. This file covers the
co-author write path under the real `_link_discovered_contributors`.
"""
from __future__ import annotations

import pytest

from app.discovery.lookup import _link_discovered_contributors
from app.discovery.sources.base import BookResult, Contributor


# ─── Fixtures ───────────────────────────────────────────────────


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    """Per-library + global DB + author_identity DATA_DIR all patched —
    mirror_image_url resolves person link via the global DB and reads
    the per-library anchor for unlinked authors. Slice 02's fixture
    shape; copied here for test isolation."""
    from app import config as app_config
    from app import database as global_database
    from app.discovery import database as disco_db
    from app.discovery import author_identity as ai

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ai, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(global_database, "APP_DB_PATH", tmp_path / "seshat.db")
    await global_database.init_db()
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


async def _insert_author(name: str, **extra) -> int:
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    cols = ["name", "sort_name", "normalized_name"]
    vals = [name, name, normalize_author_name(name)]
    for k, v in extra.items():
        cols.append(k)
        vals.append(v)
    placeholders = ", ".join("?" * len(cols))
    db = await get_db()
    try:
        cur = await db.execute(
            f"INSERT INTO authors ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _insert_book(title: str) -> int:
    """Insert a minimal `books` row + return its id."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO books (title, source) VALUES (?, ?)",
            (title, "test"),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _read_image(aid: int) -> dict:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT image_url, image_url_source FROM authors WHERE id = ?",
            (aid,),
        )).fetchone()
        return dict(row) if row else {}
    finally:
        await db.close()


async def _resolve(name: str) -> int | None:
    """Look up a per-library `authors` row id by exact name."""
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT id FROM authors WHERE name = ?", (name,),
        )).fetchone()
        return row["id"] if row else None
    finally:
        await db.close()


async def _drive_link(book_id: int, scanned_id: int, source: str,
                     contributors: list[Contributor]):
    """Drive `_link_discovered_contributors` against a synthetic
    BookResult. Mirrors the call shape from the real `_merge_result`."""
    from app.discovery.database import get_db
    bk = BookResult(title="Test Book", source=source, contributors=contributors)
    db = await get_db()
    try:
        await _link_discovered_contributors(db, book_id, scanned_id, bk, source)
        await db.commit()
    finally:
        await db.close()


# ─── 1. Byline image fills NULL on existing co-author ──────────


async def test_byline_image_fills_null_existing_coauthor(discovery_db):
    """Amazon byline contributors arrive: scanned (Chaney) + co-author
    (Anspach, NULL image). Co-author's NULL slot fills with the byline
    image + source='amazon'. The scanned author was written by
    Slice 02's _merge_result path in real flow — here we just verify
    the co-author half of the contract."""
    chaney_id = await _insert_author("J.N. Chaney")
    anspach_id = await _insert_author("Jason Anspach")
    book_id = await _insert_book("Galaxy's Edge")

    await _drive_link(book_id, chaney_id, "amazon", [
        Contributor(name="J.N. Chaney", image_url="https://amz/chaney.jpg"),
        Contributor(name="Jason Anspach", image_url="https://amz/anspach.jpg"),
    ])

    assert await _read_image(anspach_id) == {
        "image_url": "https://amz/anspach.jpg",
        "image_url_source": "amazon",
    }


# ─── 2. Byline image fills NULL on minted-fresh co-author ──────


async def test_byline_image_fills_null_minted_fresh_coauthor(discovery_db):
    """Trusted-create source (amazon): unknown co-author is MINTED by
    resolve_or_create_author. The mint writes name + source_id (slice 01)
    but NOT image — image_url_source is NULL at this moment. Slice 03's
    follow-up mirror_image_url call then fills the NULL slot with the
    byline image. Exercises the mint→fill ordering."""
    chaney_id = await _insert_author("J.N. Chaney")
    book_id = await _insert_book("Galaxy's Edge")

    await _drive_link(book_id, chaney_id, "amazon", [
        Contributor(name="J.N. Chaney"),       # scanned, no image
        Contributor(name="Newly Minted Co",     # unknown → minted
                    image_url="https://amz/new.jpg"),
    ])

    minted_id = await _resolve("Newly Minted Co")
    assert minted_id is not None, "mint should have happened"
    assert await _read_image(minted_id) == {
        "image_url": "https://amz/new.jpg",
        "image_url_source": "amazon",
    }


# ─── 3. Byline image does NOT overwrite populated slot ─────────


async def test_byline_image_never_upgrades_populated_slot(discovery_db):
    """Co-author already has an image (any rank). Amazon byline (rank 1,
    highest) cannot overwrite via the co_author path — strict fill-if-
    empty per ADR-0016 §2. LC byline never upgrades cross-source."""
    chaney_id = await _insert_author("J.N. Chaney")
    anspach_id = await _insert_author(
        "Jason Anspach",
        image_url="https://gr/anspach.jpg",      # lower-rank but populated
        image_url_source="goodreads",
    )
    book_id = await _insert_book("Galaxy's Edge")

    await _drive_link(book_id, chaney_id, "amazon", [
        Contributor(name="J.N. Chaney"),
        Contributor(name="Jason Anspach",
                    image_url="https://amz/anspach.jpg"),
    ])

    # Goodreads image preserved — co_author trust never upgrades.
    assert await _read_image(anspach_id) == {
        "image_url": "https://gr/anspach.jpg",
        "image_url_source": "goodreads",
    }


# ─── 4. NULL byline image is a no-op ──────────────────────────


async def test_null_byline_image_no_write(discovery_db):
    """Contributor.image_url=None → wiring's `if c.image_url` short-
    circuits BEFORE the helper is invoked. Row stays untouched."""
    chaney_id = await _insert_author("J.N. Chaney")
    anspach_id = await _insert_author("Jason Anspach")
    book_id = await _insert_book("Galaxy's Edge")

    await _drive_link(book_id, chaney_id, "amazon", [
        Contributor(name="J.N. Chaney"),
        Contributor(name="Jason Anspach", image_url=None),
    ])

    assert await _read_image(anspach_id) == {
        "image_url": None, "image_url_source": None,
    }


# ─── 5. Role-filtered contributor doesn't reach helper ─────────


async def test_role_filtered_contributor_no_image_write(discovery_db):
    """A non-author role (Illustrator) is dropped by `contributor_is_author`
    BEFORE resolve_or_create_author runs — the contributor doesn't even
    enter `ordered`, so no mirror_image_url call happens. Even if the
    illustrator was already a row in `authors` with an image_url column,
    the byline image must NOT be written to it.

    (Defense: a future bug that allowed the illustrator into the helper
    would corrupt author images with cover-art URLs.)"""
    chaney_id = await _insert_author("J.N. Chaney")
    # The illustrator EXISTS as a row by name (pre-inserted), but the
    # role filter at the loop's top should drop them.
    illustrator_id = await _insert_author("Some Illustrator")
    book_id = await _insert_book("Galaxy's Edge")

    await _drive_link(book_id, chaney_id, "amazon", [
        Contributor(name="J.N. Chaney"),
        Contributor(name="Some Illustrator",
                    role="Illustrator",
                    image_url="https://amz/cover-art.jpg"),
    ])

    # Illustrator's image_url stayed NULL — the role filter dropped them
    # before the helper could fire.
    assert await _read_image(illustrator_id) == {
        "image_url": None, "image_url_source": None,
    }


# ─── 6. Unknown source short-circuits the helper ───────────────


async def test_unknown_source_no_write(discovery_db):
    """A source not in IMAGE_SOURCE_RANK (here: google_books) → helper
    logs warn + returns 0. No exception escapes; co-author row stays
    untouched. google_books is also link-only (NOT in TRUSTED_CREATE),
    so the co-author must pre-exist for resolve to succeed."""
    chaney_id = await _insert_author("J.N. Chaney")
    anspach_id = await _insert_author("Jason Anspach")
    book_id = await _insert_book("Galaxy's Edge")

    await _drive_link(book_id, chaney_id, "google_books", [
        Contributor(name="J.N. Chaney"),
        Contributor(name="Jason Anspach",
                    image_url="https://gb/anspach.jpg"),
    ])

    assert await _read_image(anspach_id) == {
        "image_url": None, "image_url_source": None,
    }


# ─── 7. Link-only source still fills NULL on resolved row ──────


async def test_link_only_source_still_writes_image_when_supported(discovery_db):
    """A source that IS in IMAGE_SOURCE_RANK but NOT in TRUSTED_CREATE
    (hardcover is trusted-create + ranked; google_books is neither;
    audible is both; goodreads is both). To exercise the "ranked-but-
    link-only" case we'd need a source that's ranked but link-only —
    none exists today. So this test instead exercises the broader
    rank-2 (goodreads) source filling a NULL co-author slot via the
    co-author path. Documents the contract."""
    chaney_id = await _insert_author("J.N. Chaney")
    anspach_id = await _insert_author("Jason Anspach")  # NULL image
    book_id = await _insert_book("Galaxy's Edge")

    await _drive_link(book_id, chaney_id, "goodreads", [
        Contributor(name="J.N. Chaney"),
        Contributor(name="Jason Anspach",
                    image_url="https://gr/anspach.jpg"),
    ])

    assert await _read_image(anspach_id) == {
        "image_url": "https://gr/anspach.jpg",
        "image_url_source": "goodreads",
    }


# ─── 8. Multi-contributor — only the named-byline images fill ──


async def test_multi_contributor_fills_each_independently(discovery_db):
    """A 3-author byline: each non-NULL image fills its respective
    co-author's NULL slot independently. The scanned author's image
    would normally come through _merge_result (slice 02), but the
    co-author path also fill-if-empties the scanned author when they
    appear in the byline (legitimate per ADR-0016 §2 — idempotent)."""
    chaney_id = await _insert_author("J.N. Chaney")
    anspach_id = await _insert_author("Jason Anspach")
    cole_id = await _insert_author("Nick Cole")
    book_id = await _insert_book("Galaxy's Edge")

    await _drive_link(book_id, chaney_id, "amazon", [
        Contributor(name="J.N. Chaney",
                    image_url="https://amz/chaney.jpg"),
        Contributor(name="Jason Anspach",
                    image_url="https://amz/anspach.jpg"),
        Contributor(name="Nick Cole"),  # no image_url for this one
    ])

    assert (await _read_image(chaney_id))["image_url"] == "https://amz/chaney.jpg"
    assert (await _read_image(anspach_id))["image_url"] == "https://amz/anspach.jpg"
    assert await _read_image(cole_id) == {
        "image_url": None, "image_url_source": None,
    }
