"""
v2.3.7 — acquisition link-back.

When an audiobook (or ebook) lands in the discovery DB via ABS or
Calibre sync after coming through the IRC pipeline, link the new
row's mam_url + mam_status='found' + mam_torrent_id directly from
the originating grab. Without this, MAM scans run a fuzzy
title+author search that often mis-grades known-from-MAM books as
not_found / possible.

The tests live below the helper itself (`link_new_book`) since the
sync-pass integration is heavy. Sync-pass coverage is exercised
through the existing `test_audiobookshelf_sync.py` /
`test_calibre_sync_*` files at higher levels; here we lock in:
  - Confident match writes mam fields + records the link
  - Already-linked grab is not reused
  - Author-mismatch + low-overlap rejected
  - Ambiguous tied scores bail rather than guess
  - Existing mam_status preserved (no stomp)
  - Wrong-content-type grab not considered
"""
from __future__ import annotations

import pytest


@pytest.fixture
async def both_dbs(tmp_path, monkeypatch):
    """Set up the global app DB + a per-library discovery DB.

    Both schemas come from production init helpers. Returns a
    namespace with `app_db` (open connection to the global DB) and
    `library_db` (open connection to one library's discovery DB),
    plus `slug` for the library.
    """
    from app import config as app_config, database as app_database
    from app.discovery import database as disco_db

    # Both DBs live under tmp_path so they're isolated per test.
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(app_database, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    await app_database.init_db()
    disco_db.set_active_library("ebooks")
    await disco_db.init_db("ebooks")
    library_db = await disco_db.get_db(slug="ebooks")
    app_db = await app_database.get_db()
    try:
        yield {
            "app_db": app_db,
            "library_db": library_db,
            "slug": "ebooks",
        }
    finally:
        await app_db.close()
        await library_db.close()
        disco_db.set_active_library(None)


async def _seed_grab(
    app_db, *,
    mam_torrent_id: str = "12345",
    torrent_name: str = "Snekguy - Free Companions [m4b]",
    author_blob: str = "Snekguy",
    category: str = "AudioBooks - Sci-Fi",
    grabbed_at: str = "datetime('now', '-1 days')",
) -> int:
    """Insert a grab row, return its id. `grabbed_at` is a SQL
    expression (datetime literal or modifier) so tests can
    parameterize recency."""
    cur = await app_db.execute(
        f"""
        INSERT INTO grabs (mam_torrent_id, torrent_name, category,
                           author_blob, state, grabbed_at)
        VALUES (?, ?, ?, ?, 'complete', {grabbed_at})
        """,
        (mam_torrent_id, torrent_name, category, author_blob),
    )
    await app_db.commit()
    return cur.lastrowid


async def _seed_book(
    library_db, *,
    title: str = "Free Companions",
    author_name: str = "Snekguy",
    mam_status: str | None = None,
) -> int:
    from app.metadata.author_names import normalize_author_name
    await library_db.execute(
        "INSERT OR IGNORE INTO authors (name, sort_name, normalized_name) "
        "VALUES (?, ?, ?)",
        (author_name, author_name, normalize_author_name(author_name)),
    )
    aid_row = await (await library_db.execute(
        "SELECT id FROM authors WHERE name=?", (author_name,)
    )).fetchone()
    aid = aid_row["id"]
    cur = await library_db.execute(
        "INSERT INTO books (title, source, owned, mam_status) "
        "VALUES (?, 'audiobookshelf', 1, ?)",
        (title, mam_status),
    )
    book_id = cur.lastrowid
    await library_db.execute(
        "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
        "VALUES (?, ?, 0)", (book_id, aid),
    )
    await library_db.commit()
    return book_id


async def _read_book(library_db, book_id: int) -> dict:
    row = await (await library_db.execute(
        "SELECT mam_url, mam_status, mam_torrent_id FROM books WHERE id=?",
        (book_id,),
    )).fetchone()
    return dict(row) if row else {}


# ─── Confident match ─────────────────────────────────────────────


async def test_confident_match_links_book(both_dbs):
    from app.discovery.acquisition_linkback import link_new_book

    grab_id = await _seed_grab(
        both_dbs["app_db"],
        mam_torrent_id="12345",
        torrent_name="Snekguy - Free Companions [m4b]",
        author_blob="Snekguy",
    )
    book_id = await _seed_book(both_dbs["library_db"])

    linked = await link_new_book(
        both_dbs["library_db"], both_dbs["slug"], book_id,
        "Free Companions", "Snekguy",
        is_audiobook=True,
    )
    await both_dbs["library_db"].commit()
    assert linked is True

    row = await _read_book(both_dbs["library_db"], book_id)
    assert row["mam_status"] == "found"
    assert row["mam_url"] == "https://www.myanonamouse.net/t/12345"
    assert row["mam_torrent_id"] == "12345"

    # The link record exists for next-time idempotency.
    link_row = await (await both_dbs["app_db"].execute(
        "SELECT grab_id, library_slug, book_id FROM book_grab_links"
    )).fetchone()
    assert link_row["grab_id"] == grab_id
    assert link_row["library_slug"] == "ebooks"
    assert link_row["book_id"] == book_id


# ─── No double-claim ─────────────────────────────────────────────


async def test_already_linked_grab_not_reused(both_dbs):
    from app.discovery.acquisition_linkback import link_new_book

    grab_id = await _seed_grab(both_dbs["app_db"])
    # Pre-link this grab to a hypothetical earlier book.
    await both_dbs["app_db"].execute(
        "INSERT INTO book_grab_links (grab_id, library_slug, book_id) "
        "VALUES (?, ?, ?)",
        (grab_id, both_dbs["slug"], 999),
    )
    await both_dbs["app_db"].commit()

    book_id = await _seed_book(both_dbs["library_db"])
    linked = await link_new_book(
        both_dbs["library_db"], both_dbs["slug"], book_id,
        "Free Companions", "Snekguy",
        is_audiobook=True,
    )
    assert linked is False
    row = await _read_book(both_dbs["library_db"], book_id)
    assert row["mam_status"] is None


# ─── Match quality gates ─────────────────────────────────────────


async def test_author_mismatch_rejected(both_dbs):
    from app.discovery.acquisition_linkback import link_new_book

    await _seed_grab(
        both_dbs["app_db"],
        torrent_name="Brandon Sanderson - The Way of Kings [m4b]",
        author_blob="Brandon Sanderson",
    )
    # New book is by Snekguy — author not in any candidate grab.
    book_id = await _seed_book(both_dbs["library_db"], title="Free Companions")

    linked = await link_new_book(
        both_dbs["library_db"], both_dbs["slug"], book_id,
        "Free Companions", "Snekguy",
        is_audiobook=True,
    )
    assert linked is False


async def test_low_title_overlap_rejected(both_dbs):
    from app.discovery.acquisition_linkback import link_new_book

    # Author matches, but the torrent name is for a totally different
    # title (no overlap with "Free Companions").
    await _seed_grab(
        both_dbs["app_db"],
        torrent_name="Snekguy - The Pyre Initiative [m4b]",
        author_blob="Snekguy",
    )
    book_id = await _seed_book(both_dbs["library_db"], title="Free Companions")

    linked = await link_new_book(
        both_dbs["library_db"], both_dbs["slug"], book_id,
        "Free Companions", "Snekguy",
        is_audiobook=True,
    )
    assert linked is False


# ─── Ambiguity ───────────────────────────────────────────────────


async def test_tied_top_scores_bail(both_dbs):
    from app.discovery.acquisition_linkback import link_new_book

    # Two grabs that score identically against the new book —
    # ambiguous, so we bail rather than guess.
    await _seed_grab(
        both_dbs["app_db"],
        mam_torrent_id="100",
        torrent_name="Snekguy - Free Companions [m4b]",
        author_blob="Snekguy",
    )
    await _seed_grab(
        both_dbs["app_db"],
        mam_torrent_id="200",
        torrent_name="Snekguy - Free Companions [mp3]",
        author_blob="Snekguy",
    )
    book_id = await _seed_book(both_dbs["library_db"])

    linked = await link_new_book(
        both_dbs["library_db"], both_dbs["slug"], book_id,
        "Free Companions", "Snekguy",
        is_audiobook=True,
    )
    assert linked is False
    row = await _read_book(both_dbs["library_db"], book_id)
    assert row["mam_status"] is None


# ─── Pre-existing status ─────────────────────────────────────────


async def test_existing_mam_status_preserved(both_dbs):
    from app.discovery.acquisition_linkback import link_new_book

    await _seed_grab(both_dbs["app_db"])
    # Book already has a status from a prior scan or user edit.
    book_id = await _seed_book(both_dbs["library_db"], mam_status="not_applicable")

    linked = await link_new_book(
        both_dbs["library_db"], both_dbs["slug"], book_id,
        "Free Companions", "Snekguy",
        is_audiobook=True,
    )
    assert linked is False
    row = await _read_book(both_dbs["library_db"], book_id)
    assert row["mam_status"] == "not_applicable"


# ─── Content-type filtering ──────────────────────────────────────


async def test_audiobook_book_does_not_match_ebook_grab(both_dbs):
    from app.discovery.acquisition_linkback import link_new_book

    # The grab is for an EBOOK; new audiobook book should not pull
    # this grab even though the title+author would overlap.
    await _seed_grab(
        both_dbs["app_db"],
        torrent_name="Snekguy - Free Companions [epub]",
        author_blob="Snekguy",
        category="Ebooks - Sci-Fi",
    )
    book_id = await _seed_book(both_dbs["library_db"])

    linked = await link_new_book(
        both_dbs["library_db"], both_dbs["slug"], book_id,
        "Free Companions", "Snekguy",
        is_audiobook=True,
    )
    assert linked is False


async def test_ebook_book_does_not_match_audiobook_grab(both_dbs):
    from app.discovery.acquisition_linkback import link_new_book

    await _seed_grab(
        both_dbs["app_db"],
        torrent_name="Snekguy - Free Companions [m4b]",
        author_blob="Snekguy",
        category="AudioBooks - Sci-Fi",
    )
    book_id = await _seed_book(both_dbs["library_db"])

    linked = await link_new_book(
        both_dbs["library_db"], both_dbs["slug"], book_id,
        "Free Companions", "Snekguy",
        is_audiobook=False,  # ebook context
    )
    assert linked is False


# ─── Recency window ──────────────────────────────────────────────


async def test_old_grab_outside_window_ignored(both_dbs):
    from app.discovery.acquisition_linkback import link_new_book

    # 90 days back — well outside the 30-day lookback.
    await _seed_grab(
        both_dbs["app_db"],
        grabbed_at="datetime('now', '-90 days')",
    )
    book_id = await _seed_book(both_dbs["library_db"])

    linked = await link_new_book(
        both_dbs["library_db"], both_dbs["slug"], book_id,
        "Free Companions", "Snekguy",
        is_audiobook=True,
    )
    assert linked is False
