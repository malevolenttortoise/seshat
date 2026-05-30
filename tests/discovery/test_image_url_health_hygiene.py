"""v3.x (ADR-0016 slice 05) — hygiene ``job_image_url_health_check``.

Verifies the new Job 11:
  - Substring-clears `/books/`-path image URLs (the John-Birmingham
    failure mode that Job 8 used to substring-clear).
  - Substring-clears `nophoto` placeholder URLs.
  - HEAD-verifies remaining rows; non-200 → NULL.
  - Local-clear-only: does NOT fan a NULL through linked siblings or
    the persons row (ADR-0016 §6 D8(i)).
  - Idempotent: a second run finds nothing to clear.
  - JOB_NAMES position + TOTAL_JOBS=12 regression.
  - Job 8's image_url clear retired — only Job 11 touches images now.
"""
from __future__ import annotations

import pytest
import respx
import httpx

from app import config as app_config
from app import database as global_database
from app.discovery import database as disco_db
from app.discovery import author_identity as ai
from app.discovery import cross_library
from app.discovery import hygiene


@pytest.fixture
async def hygiene_env(tmp_path, monkeypatch):
    """Two per-library DBs (initialized via the real schema, so they
    carry slice 01's `image_url_source` column) + global DB + the
    cross_library lister monkeypatched to surface our test slugs."""
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ai, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(global_database, "APP_DB_PATH", tmp_path / "seshat.db")
    await global_database.init_db()
    slugs = ["calibre-library", "abs-audio-library"]
    for slug in slugs:
        await disco_db.init_db(slug)
    monkeypatch.setattr(
        cross_library, "libraries_for",
        lambda _kind: [{"slug": s} for s in slugs],
    )
    yield tmp_path


async def _insert_author(slug: str, name: str,
                        image_url: str | None = None,
                        image_url_source: str | None = None) -> int:
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db(slug)
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name, "
            "                     image_url, image_url_source) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, name, normalize_author_name(name),
             image_url, image_url_source),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _read_image(slug: str, aid: int) -> dict:
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        row = await (await db.execute(
            "SELECT image_url, image_url_source FROM authors WHERE id = ?",
            (aid,),
        )).fetchone()
        return dict(row) if row else {}
    finally:
        await db.close()


# ─── 1. JOB_NAMES + TOTAL_JOBS regression ─────────────────────


def test_job_catalogue_positions():
    """ADR-0016 slice 05 inserts Job 11 between Prune-orphans (Job 10)
    and Soft-delete (now Job 12). TOTAL_JOBS = 12."""
    assert hygiene.TOTAL_JOBS == 12
    assert hygiene.JOB_NAMES[9] == "Prune orphan author links"
    assert hygiene.JOB_NAMES[10] == "Image URL health check"
    assert hygiene.JOB_NAMES[11] == "Soft-delete retention sweep"


# ─── 2. Substring blacklist — /books/-path clears ────────────


@respx.mock
async def test_books_path_substring_cleared(hygiene_env):
    """The historical John-Birmingham failure mode: image_url contains
    `/books/` (a book-cover URL). Job 11 NULLs both `image_url` AND
    `image_url_source` via the substring blacklist (no HEAD needed —
    the pattern is a known false-positive)."""
    aid = await _insert_author(
        "calibre-library", "John Birmingham",
        image_url="https://i.gr-assets.com/images/S/.../books/12345/cover.jpg",
        image_url_source="goodreads",
    )
    stats = hygiene._zero_stats()
    await hygiene.job_image_url_health_check(stats)
    assert stats["image_urls_blacklisted_path"] == 1
    assert stats["image_urls_head_failed"] == 0
    assert await _read_image("calibre-library", aid) == {
        "image_url": None, "image_url_source": None,
    }


# ─── 3. Substring blacklist — nophoto clears ─────────────────


@respx.mock
async def test_nophoto_substring_cleared(hygiene_env):
    """A nophoto placeholder URL (Goodreads serves these for authors
    without a photo) is also substring-cleared. Discovery-side
    `_extract_author_photo` filters them at write time post-slice-04,
    but legacy rows + cross-source drift make defense-in-depth cheap."""
    aid = await _insert_author(
        "calibre-library", "Unknown Author",
        image_url="https://images.gr-assets.com/authors/nophoto/u_50x66.jpg",
        image_url_source="goodreads",
    )
    stats = hygiene._zero_stats()
    await hygiene.job_image_url_health_check(stats)
    assert stats["image_urls_blacklisted_path"] == 1
    assert await _read_image("calibre-library", aid) == {
        "image_url": None, "image_url_source": None,
    }


# ─── 4. HEAD 200 preserves ───────────────────────────────────


@respx.mock
async def test_head_200_preserves_image(hygiene_env):
    """A healthy URL (HEAD returns 200) stays put. Verifies the
    happy path doesn't false-NULL working images."""
    url = "https://images.gr-assets.com/authors/12345p2/678.jpg"
    respx.head(url).respond(status_code=200)
    aid = await _insert_author(
        "calibre-library", "Healthy Author",
        image_url=url, image_url_source="goodreads",
    )
    stats = hygiene._zero_stats()
    await hygiene.job_image_url_health_check(stats)
    assert stats["image_urls_blacklisted_path"] == 0
    assert stats["image_urls_head_failed"] == 0
    assert await _read_image("calibre-library", aid) == {
        "image_url": url, "image_url_source": "goodreads",
    }


# ─── 5. HEAD 404 NULLs ───────────────────────────────────────


@respx.mock
async def test_head_404_nulls_image(hygiene_env):
    """A 404 → NULL both columns. The canonical "URL is gone" signal."""
    url = "https://images.gr-assets.com/authors/12345p2/999.jpg"
    respx.head(url).respond(status_code=404)
    aid = await _insert_author(
        "calibre-library", "Dead URL Author",
        image_url=url, image_url_source="amazon",
    )
    stats = hygiene._zero_stats()
    await hygiene.job_image_url_health_check(stats)
    assert stats["image_urls_head_failed"] == 1
    assert stats["image_urls_blacklisted_path"] == 0
    assert await _read_image("calibre-library", aid) == {
        "image_url": None, "image_url_source": None,
    }


# ─── 6. HEAD connection error NULLs ──────────────────────────


@respx.mock
async def test_head_connection_error_nulls_image(hygiene_env):
    """Per ADR-0016 §6 (strict): any non-200 (including transport
    errors) NULLs the row. The next scan refills from any source —
    self-correcting; the cost of leaving a definitely-broken URL
    visible is higher than the cost of a brief placeholder."""
    url = "https://images.gr-assets.com/authors/CONN-ERR/x.jpg"
    respx.head(url).mock(side_effect=httpx.ConnectError("kaboom"))
    aid = await _insert_author(
        "calibre-library", "Conn Err Author",
        image_url=url, image_url_source="goodreads",
    )
    stats = hygiene._zero_stats()
    await hygiene.job_image_url_health_check(stats)
    assert stats["image_urls_head_failed"] == 1
    assert await _read_image("calibre-library", aid) == {
        "image_url": None, "image_url_source": None,
    }


# ─── 7. Mixed batch: blacklist + HEAD ────────────────────────


@respx.mock
async def test_mixed_batch_blacklist_and_head(hygiene_env):
    """Same-library mix: one bad-substring URL (skips HEAD), one 200
    healthy URL (preserved), one 404 (NULL'd via HEAD). Both stats
    increment correctly; HEAD is not called for the substring-cleared
    row."""
    bad_substring = "https://i.gr-assets.com/.../books/123/cover.jpg"
    healthy = "https://images.gr-assets.com/authors/12345p2/1.jpg"
    dead = "https://images.gr-assets.com/authors/67890p2/2.jpg"
    respx.head(healthy).respond(status_code=200)
    respx.head(dead).respond(status_code=404)
    # If respx receives a HEAD for `bad_substring` we'll know the
    # substring blacklist DIDN'T fire first — assert via call count.
    respx.head(bad_substring).respond(status_code=200)  # would falsely preserve

    aid_bad = await _insert_author(
        "calibre-library", "Bad Substring",
        image_url=bad_substring, image_url_source="goodreads",
    )
    aid_ok = await _insert_author(
        "calibre-library", "Healthy",
        image_url=healthy, image_url_source="amazon",
    )
    aid_dead = await _insert_author(
        "calibre-library", "Dead",
        image_url=dead, image_url_source="hardcover",
    )

    stats = hygiene._zero_stats()
    await hygiene.job_image_url_health_check(stats)

    assert stats["image_urls_blacklisted_path"] == 1
    assert stats["image_urls_head_failed"] == 1

    # bad-substring NULL'd via blacklist BEFORE HEAD (would otherwise
    # have been spared by the 200 stub).
    assert (await _read_image("calibre-library", aid_bad))["image_url"] is None
    # healthy preserved
    assert await _read_image("calibre-library", aid_ok) == {
        "image_url": healthy, "image_url_source": "amazon",
    }
    # dead NULL'd via HEAD
    assert (await _read_image("calibre-library", aid_dead))["image_url"] is None


# ─── 8. Local-clear-only — siblings preserved ────────────────


@respx.mock
async def test_local_clear_only_siblings_preserved(hygiene_env):
    """ADR-0016 §6 D8(i): clearing a per-library row does NOT fan a
    NULL through linked siblings or the persons row. The next scan
    re-establishes coherence via mirror_image_url's rank-aware
    overwrite. (This guard prevents stomping a sibling whose URL
    happens to still 200, and preserves any operator-edited per-
    library override.)"""
    dead_url = "https://images.gr-assets.com/authors/dead-cwa/x.jpg"
    sibling_url = "https://images.gr-assets.com/authors/healthy-abs/x.jpg"
    respx.head(dead_url).respond(status_code=404)
    respx.head(sibling_url).respond(status_code=200)

    aid_cwa = await _insert_author(
        "calibre-library", "Shared Author",
        image_url=dead_url, image_url_source="goodreads",
    )
    aid_abs = await _insert_author(
        "abs-audio-library", "Shared Author",
        image_url=sibling_url, image_url_source="goodreads",
    )

    stats = hygiene._zero_stats()
    await hygiene.job_image_url_health_check(stats)

    # The dead cwa row got NULL'd; the abs sibling stayed put.
    assert (await _read_image("calibre-library", aid_cwa))["image_url"] is None
    assert await _read_image("abs-audio-library", aid_abs) == {
        "image_url": sibling_url, "image_url_source": "goodreads",
    }


# ─── 9. Idempotent — second run is a no-op ───────────────────


@respx.mock
async def test_idempotent_second_run_zero_writes(hygiene_env):
    """Once the bad URLs have been cleared, a second run finds the
    rows are NULL → no HEAD requests, no clears, zero stats."""
    bad = "https://i.gr-assets.com/.../books/123/cover.jpg"
    aid = await _insert_author(
        "calibre-library", "John Birmingham",
        image_url=bad, image_url_source="goodreads",
    )

    s1 = hygiene._zero_stats()
    await hygiene.job_image_url_health_check(s1)
    assert s1["image_urls_blacklisted_path"] == 1
    assert (await _read_image("calibre-library", aid))["image_url"] is None

    s2 = hygiene._zero_stats()
    await hygiene.job_image_url_health_check(s2)
    assert s2["image_urls_blacklisted_path"] == 0
    assert s2["image_urls_head_failed"] == 0


# ─── 10. Job 8 image-clear retirement regression ─────────────


async def test_job8_no_longer_clears_books_path_images(hygiene_env):
    """The pre-slice-05 Job 8 (`job_cross_library_person_backfill`)
    had a `image_url LIKE '%/books/%'` clear block as a workaround
    for the broken Goodreads selector. Slice 05 retires it; the
    block now lives in Job 11 (image-health check). Job 8 must NOT
    touch image_url at all anymore.

    This test inserts a `/books/`-path URL, runs Job 8 (NOT Job 11),
    and asserts the URL stays put. Job 11 is what clears it now."""
    bad_url = "https://i.gr-assets.com/.../books/12345/cover.jpg"
    aid = await _insert_author(
        "calibre-library", "John Birmingham",
        image_url=bad_url, image_url_source="goodreads",
    )

    stats = hygiene._zero_stats()
    await hygiene.job_cross_library_person_backfill(stats)

    # Image untouched — Job 8 no longer has the substring-clear block.
    assert await _read_image("calibre-library", aid) == {
        "image_url": bad_url, "image_url_source": "goodreads",
    }
    # Stats key removed — `broken_image_urls_cleared` is gone from
    # `_zero_stats`; should not appear at all.
    assert "broken_image_urls_cleared" not in stats
