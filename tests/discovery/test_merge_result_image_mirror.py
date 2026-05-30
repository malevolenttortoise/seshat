"""v3.x (ADR-0016 slice 02) — `_merge_result` scanned-author image write.

Verifies the wiring between `_merge_result` and `mirror_image_url`
under the **scanned-author** trust mode:

  - AuthorResult.image_url non-null  → helper called with
                                       trust='scanned', source=source_name.
  - AuthorResult.image_url None      → helper NOT called.
  - Rank-aware overwrite end-to-end  → higher-rank source replaces lower.
  - Rank-aware skip end-to-end       → lower-rank source preserved.
  - Unrecognized source              → helper logs warn + no-op; no
                                       exception propagates (best-effort).
  - Empty / whitespace               → no write (defensive — same
                                       guard mirror_bio uses).

Slice 01's helper unit tests cover the rank rule + fanout shape in
isolation; this file covers the call site under the real merge path.
"""
from __future__ import annotations

import pytest

from app.discovery.lookup import _merge_result
from app.discovery.sources.base import AuthorResult, BookResult


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    """Per-library + global DB + author_identity DATA_DIR all patched
    so `mirror_image_url` can run end-to-end inside `_merge_result`.

    Slice 01's `mirror_image_url` resolves person link via the global
    DB and (for unlinked authors) reads the per-library anchor over
    its own connection — both need their `DATA_DIR` aware of tmp_path."""
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
    """Insert an author. `extra` accepts image_url + image_url_source."""
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


def _scan(source: str, *, image_url=None, name="J.N. Chaney") -> AuthorResult:
    """Minimal AuthorResult driver for a single-book scan."""
    return AuthorResult(
        name=name,
        external_id="ext-1",
        image_url=image_url,
        books=[BookResult(title="Ruins of the Galaxy", source=source)],
    )


# ─── 1. Scanned-author write fires on non-null image_url ──────


async def test_scanned_writes_per_library_row(discovery_db):
    """AuthorResult.image_url set → after _merge_result, per-library row
    carries (image_url, image_url_source). Single library; unlinked
    author; no persons/sibling fanout (return count = 1 from helper)."""
    aid = await _insert_author("J.N. Chaney")
    await _merge_result(
        author_id=aid,
        result=_scan("amazon", image_url="https://amz/chaney.jpg"),
        source_name="amazon",
        languages=["English"],
    )
    assert await _read_image(aid) == {
        "image_url": "https://amz/chaney.jpg",
        "image_url_source": "amazon",
    }


# ─── 2. NULL image_url → no write, no exception ────────────────


async def test_null_image_url_no_write(discovery_db):
    """AuthorResult.image_url is None → mirror_image_url not called
    (the call site short-circuits on falsy result.image_url, same as
    the `if result.bio:` guard mirror_bio uses)."""
    aid = await _insert_author("J.N. Chaney")
    await _merge_result(
        author_id=aid,
        result=_scan("amazon", image_url=None),
        source_name="amazon",
        languages=["English"],
    )
    assert await _read_image(aid) == {"image_url": None, "image_url_source": None}


# ─── 3. Rank-aware overwrite end-to-end ────────────────────────


async def test_rank_aware_overwrite_replaces_lower(discovery_db):
    """Pre-populated with goodreads (rank 2); amazon scan (rank 1)
    overwrites both columns through the real merge path."""
    aid = await _insert_author(
        "Patrick Rothfuss",
        image_url="https://gr/rothfuss.jpg",
        image_url_source="goodreads",
    )
    await _merge_result(
        author_id=aid,
        result=_scan("amazon",
                     image_url="https://amz/rothfuss.jpg",
                     name="Patrick Rothfuss"),
        source_name="amazon",
        languages=["English"],
    )
    assert await _read_image(aid) == {
        "image_url": "https://amz/rothfuss.jpg",
        "image_url_source": "amazon",
    }


# ─── 4. Rank-aware skip end-to-end ─────────────────────────────


async def test_rank_aware_skip_preserves_higher(discovery_db):
    """Pre-populated with amazon (rank 1); goodreads scan (rank 2) is
    skipped end-to-end. Row stays on the amazon tuple."""
    aid = await _insert_author(
        "Patrick Rothfuss",
        image_url="https://amz/rothfuss.jpg",
        image_url_source="amazon",
    )
    await _merge_result(
        author_id=aid,
        result=_scan("goodreads",
                     image_url="https://gr/rothfuss.jpg",
                     name="Patrick Rothfuss"),
        source_name="goodreads",
        languages=["English"],
    )
    assert await _read_image(aid) == {
        "image_url": "https://amz/rothfuss.jpg",
        "image_url_source": "amazon",
    }


# ─── 5. Unrecognized source → best-effort no-op ────────────────


async def test_unrecognized_source_no_exception(discovery_db):
    """An AuthorResult flowing through `_merge_result` from a source
    NOT in IMAGE_SOURCE_RANK (e.g. openlibrary, google_books, MAM) →
    helper logs warn + returns 0 + no exception escapes the call site.
    Row stays untouched."""
    aid = await _insert_author("Author X")
    # No exception should propagate; the wiring's try/except + the
    # helper's own unknown-source guard combine to absorb it.
    await _merge_result(
        author_id=aid,
        result=_scan("google_books",
                     image_url="https://gb/x.jpg",
                     name="Author X"),
        source_name="google_books",
        languages=["English"],
    )
    assert await _read_image(aid) == {"image_url": None, "image_url_source": None}


# ─── 6. Blank/whitespace image_url → no write ──────────────────


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
async def test_blank_image_url_no_write(discovery_db, blank):
    """Whitespace-only image_url is treated as None at the helper layer
    (defensive against misbehaving scrapers). Pre-existing image is
    preserved if any."""
    aid = await _insert_author(
        "Stephen King",
        image_url="https://gr/king.jpg",
        image_url_source="goodreads",
    )
    await _merge_result(
        author_id=aid,
        result=_scan("amazon", image_url=blank, name="Stephen King"),
        source_name="amazon",
        languages=["English"],
    )
    assert await _read_image(aid) == {
        "image_url": "https://gr/king.jpg",
        "image_url_source": "goodreads",
    }


# ─── 7. Same-source refresh — URL update ───────────────────────


async def test_same_source_url_refresh(discovery_db):
    """A subsequent amazon scan with a different URL (CDN rehash, image
    replaced) overwrites under the rank-equality case."""
    aid = await _insert_author(
        "Brandon Sanderson",
        image_url="https://amz/old.jpg",
        image_url_source="amazon",
    )
    await _merge_result(
        author_id=aid,
        result=_scan("amazon",
                     image_url="https://amz/new.jpg",
                     name="Brandon Sanderson"),
        source_name="amazon",
        languages=["English"],
    )
    assert await _read_image(aid) == {
        "image_url": "https://amz/new.jpg",
        "image_url_source": "amazon",
    }


# ─── 8. NULL-source legacy row upgraded ────────────────────────


async def test_null_source_legacy_row_upgraded(discovery_db):
    """Pre-ADR-0016 row: image_url populated but image_url_source NULL
    (e.g. the lone bad book-cover entry the hygiene workaround used to
    catch). Any recognized source upgrades it under scanned-author."""
    aid = await _insert_author(
        "Legacy Pat",
        image_url="https://gr/photo.jpg",
        image_url_source=None,                       # legacy NULL
    )
    await _merge_result(
        author_id=aid,
        result=_scan("audible",                      # rank 4, lowest of the named
                     image_url="https://audnex/photo.jpg",
                     name="Legacy Pat"),
        source_name="audible",
        languages=["English"],
    )
    assert await _read_image(aid) == {
        "image_url": "https://audnex/photo.jpg",
        "image_url_source": "audible",
    }
