"""
v2.17.7 — announce-time claim-for-owned tests.

Covers the new helper that recognizes an announce for a book the
user already owns (with no confirmed MAM URL) and pins the torrent
URL directly to the owned row instead of grabbing a duplicate copy.
"""
from __future__ import annotations

import pytest

from app import state
from app.discovery import database as disco_db
from app.filter.gate import Announce
from app.orchestrator.owned_announce_claim import (
    OwnedClaimResult,
    find_owned_matches,
    try_claim_announce_for_owned,
    write_claim_to_owned,
)


def _announce(
    *,
    title: str = "Amber's Hollow: Home of the Homeless",
    author: str = "St Arkham",
    category: str = "Ebooks - Romance",
    filetype: str = "epub",
    torrent_id: str = "1243514",
) -> Announce:
    return Announce(
        torrent_id=torrent_id,
        torrent_name=title,
        category=category,
        author_blob=author,
        title=title,
        filetype=filetype,
    )


@pytest.fixture
async def ebook_library(tmp_path, monkeypatch, temp_db):
    from app import config as app_config

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    await disco_db.init_db("calibre-library")

    lib = {
        "slug": "calibre-library",
        "content_type": "ebook",
        "app_type": "calibre",
    }
    monkeypatch.setattr(state, "_discovered_libraries", [lib])
    yield lib


@pytest.fixture
async def audio_library(tmp_path, monkeypatch, temp_db):
    from app import config as app_config

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    await disco_db.init_db("abs-audio-library")

    lib = {
        "slug": "abs-audio-library",
        "content_type": "audiobook",
        "app_type": "audiobookshelf",
    }
    monkeypatch.setattr(state, "_discovered_libraries", [lib])
    yield lib


async def _seed_owned(
    slug: str, *,
    title: str, author: str,
    mam_status: str | None = None,
    source: str = "calibre",
) -> int:
    """Insert an author + owned book row, return book_id."""
    from app.metadata.author_names import normalize_author_name

    db = await disco_db.get_db(slug)
    try:
        await db.execute(
            "INSERT OR IGNORE INTO authors (name, sort_name, normalized_name) "
            "VALUES (?, ?, ?)",
            (author, author, normalize_author_name(author)),
        )
        row = await (await db.execute(
            "SELECT id FROM authors WHERE name = ?", (author,),
        )).fetchone()
        aid = row["id"]
        cur = await db.execute(
            "INSERT INTO books (title, author_id, source, owned, hidden, "
            "                   mam_status) "
            "VALUES (?, ?, ?, 1, 0, ?)",
            (title, aid, source, mam_status),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _read_book(slug: str, book_id: int) -> dict:
    db = await disco_db.get_db(slug)
    try:
        row = await (await db.execute(
            "SELECT mam_url, mam_torrent_id, mam_status "
            "FROM books WHERE id = ?", (book_id,),
        )).fetchone()
        return dict(row) if row else {}
    finally:
        await db.close()


# ─── find_owned_matches ─────────────────────────────────────────


class TestFindOwnedMatches:
    async def test_normalization_mismatch_still_matches(self, ebook_library):
        """The Mark/St. Arkham case — MAM says 'St Arkham', Calibre
        holds 'St. Arkham'. Author normalization must collapse them."""
        book_id = await _seed_owned(
            ebook_library["slug"],
            title="Amber's Hollow: Home of the Homeless",
            author="St. Arkham",
            mam_status="not_found",
        )
        matches = await find_owned_matches(
            title="Amber's Hollow: Home of the Homeless",
            author_blob="St Arkham",
            category="Ebooks - Romance",
        )
        assert len(matches) == 1
        assert matches[0].book_id == book_id
        assert matches[0].library_slug == ebook_library["slug"]

    async def test_unowned_row_excluded(self, ebook_library):
        # owned=0 rows aren't candidates regardless of title match.
        from app.metadata.author_names import normalize_author_name
        db = await disco_db.get_db(ebook_library["slug"])
        try:
            await db.execute(
                "INSERT INTO authors (name, sort_name, normalized_name) "
                "VALUES (?, ?, ?)",
                ("A", "A", normalize_author_name("A")),
            )
            await db.execute(
                "INSERT INTO books (title, author_id, source, owned, hidden) "
                "VALUES ('X', 1, 'goodreads', 0, 0)",
            )
            await db.commit()
        finally:
            await db.close()
        matches = await find_owned_matches(
            title="X", author_blob="A", category="Ebooks - Fantasy",
        )
        assert matches == []

    async def test_found_mam_status_excluded(self, ebook_library):
        # Already-linked owned rows don't surface as claim candidates.
        await _seed_owned(
            ebook_library["slug"], title="X", author="A",
            mam_status="found",
        )
        matches = await find_owned_matches(
            title="X", author_blob="A", category="Ebooks - Fantasy",
        )
        assert matches == []

    async def test_audiobook_category_skips_ebook_library(self, ebook_library):
        # Library content_type='ebook' so an audiobook announce
        # shouldn't see any matches even when title+author align.
        await _seed_owned(
            ebook_library["slug"], title="X", author="A",
            mam_status="not_found",
        )
        matches = await find_owned_matches(
            title="X", author_blob="A", category="Audiobooks - Fantasy",
        )
        assert matches == []

    async def test_audiobook_category_finds_in_audio_library(self, audio_library):
        book_id = await _seed_owned(
            audio_library["slug"], title="X", author="A",
            mam_status="not_found", source="audiobookshelf",
        )
        matches = await find_owned_matches(
            title="X", author_blob="A", category="Audiobooks - Fantasy",
        )
        assert len(matches) == 1
        assert matches[0].book_id == book_id

    async def test_no_libraries_returns_empty(self, monkeypatch, temp_db):
        monkeypatch.setattr(state, "_discovered_libraries", [])
        matches = await find_owned_matches(
            title="X", author_blob="A", category="Ebooks - Fantasy",
        )
        assert matches == []

    async def test_unsupported_category_returns_empty(self, ebook_library):
        await _seed_owned(
            ebook_library["slug"], title="X", author="A",
            mam_status="not_found",
        )
        matches = await find_owned_matches(
            title="X", author_blob="A", category="Comics - Whatever",
        )
        assert matches == []


# ─── write_claim_to_owned ──────────────────────────────────────


class TestWriteClaim:
    async def test_writes_mam_fields(self, ebook_library):
        book_id = await _seed_owned(
            ebook_library["slug"], title="X", author="A",
            mam_status="not_found",
        )
        ok = await write_claim_to_owned(
            library_slug=ebook_library["slug"], book_id=book_id,
            mam_torrent_id="1234", category="Ebooks - Fantasy",
        )
        assert ok is True
        row = await _read_book(ebook_library["slug"], book_id)
        assert row["mam_status"] == "found"
        assert row["mam_torrent_id"] == "1234"
        assert row["mam_url"] == "https://www.myanonamouse.net/t/1234"

    async def test_nonexistent_book_returns_false(self, ebook_library):
        ok = await write_claim_to_owned(
            library_slug=ebook_library["slug"], book_id=99999,
            mam_torrent_id="1234",
        )
        assert ok is False


# ─── try_claim_announce_for_owned ─────────────────────────────


class TestTryClaim:
    async def test_happy_path_writes_and_returns_claimed(self, ebook_library):
        book_id = await _seed_owned(
            ebook_library["slug"],
            title="Amber's Hollow: Home of the Homeless",
            author="St. Arkham",
            mam_status="not_found",
        )
        result = await try_claim_announce_for_owned(announce=_announce())
        assert isinstance(result, OwnedClaimResult)
        assert result.claimed is True
        assert result.book_id == book_id
        row = await _read_book(ebook_library["slug"], book_id)
        assert row["mam_torrent_id"] == "1243514"
        assert row["mam_status"] == "found"

    async def test_no_match_returns_unclaimed(self, ebook_library):
        result = await try_claim_announce_for_owned(announce=_announce())
        assert result.claimed is False
        assert result.reason == "no_owned_match"

    async def test_ambiguous_match_bails(self, ebook_library):
        # Two owned rows tied for the same title+author — the helper
        # must refuse to guess which to claim for.
        await _seed_owned(
            ebook_library["slug"], title="X", author="A",
            mam_status="not_found",
        )
        await _seed_owned(
            ebook_library["slug"], title="X", author="A",
            mam_status="not_found",
        )
        result = await try_claim_announce_for_owned(
            announce=_announce(title="X", author="A"),
        )
        assert result.claimed is False
        assert result.reason == "ambiguous_multi_match"

    async def test_format_gate_audiobook_into_ebook_library_skipped(
        self, ebook_library,
    ):
        # Audiobook announce shouldn't claim an ebook-library row even
        # if the title+author lines up.
        await _seed_owned(
            ebook_library["slug"], title="X", author="A",
            mam_status="not_found",
        )
        result = await try_claim_announce_for_owned(
            announce=_announce(
                title="X", author="A",
                category="Audiobooks - Fantasy", filetype="m4b",
            ),
        )
        assert result.claimed is False
        assert result.reason == "no_owned_match"
