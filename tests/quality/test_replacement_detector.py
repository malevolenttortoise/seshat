"""
Tests for app/quality/replacement_detector.py — Phase 5a detection.

Integration tests (real app DB + per-library DB) because the detector
joins across both. The detector itself is a single coordinated read+
write path; mocking the storage layer would obscure most of what's
being tested. Each test seeds a controlled grab + owned-book scenario
and asserts the resulting opportunities (or lack thereof).
"""
from __future__ import annotations

import pytest

from app import state
from app.database import get_db as get_app_db
from app.discovery import database as disco_db
from app.quality.extract import QualitySnapshot
from app.quality.opportunities import list_opportunities
from app.quality.replacement_detector import detect_for_grab
from app.quality.storage import upsert_quality


# ─── Fixtures ────────────────────────────────────────────────


@pytest.fixture
async def audiobook_library(tmp_path, monkeypatch, temp_db):
    """Audiobook library, isolated DB. Returns the library dict."""
    from app import config as app_config

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    slug = "abs-audiobooks"
    await disco_db.init_db(slug)

    lib = {
        "slug": slug,
        "name": "Audiobookshelf",
        "content_type": "audiobook",
        "app_type": "audiobookshelf",
        "library_path": "/audiobooks",
    }
    monkeypatch.setattr(state, "_discovered_libraries", [lib])
    yield lib


def _settings(*, slug: str = "abs-audiobooks", enabled: bool = True) -> dict:
    """Settings dict with active replacement enabled for `slug`,
    qBit path disjoint from /audiobooks so safety = SAFE."""
    return {
        "local_path_prefix": "/downloads",
        "active_replacement_enabled_by_slug": {slug: enabled} if enabled else {},
        "format_priority": {
            "audiobook": [
                {"fmt": "m4b", "enabled": True},
                {"fmt": "mp3", "enabled": False},
            ],
            "ebook": [{"fmt": "epub", "enabled": True}],
        },
        "quality_axes": {
            "audiobook": [
                {"axis": "audio_bitrate_kbps", "tiers": [
                    {"label": "320+", "min_value": 320},
                    {"label": "192+", "min_value": 192},
                    {"label": "128+", "min_value": 128},
                    {"label": "64+",  "min_value": 64},
                    {"label": "<64",  "min_value": 0},
                ]},
            ],
            "ebook": [],
        },
    }


async def _insert_grab(
    *,
    mam_torrent_id: str,
    torrent_name: str,
    author_blob: str,
    category: str = "Audiobooks - Sci-Fi",
    book_format: str = "m4b",
) -> int:
    """Insert a grab row. Returns its id."""
    db = await get_app_db()
    try:
        cur = await db.execute(
            "INSERT INTO grabs (mam_torrent_id, torrent_name, state, "
            "                   book_format, dedup_key, category, "
            "                   author_blob) "
            "VALUES (?, ?, 'complete', ?, ?, ?, ?)",
            (
                mam_torrent_id, torrent_name, book_format,
                f"{author_blob}|{torrent_name}",  # any string is fine; not asserted
                category, author_blob,
            ),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _link_grab_to_book(grab_id: int, library_slug: str, book_id: int) -> None:
    db = await get_app_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO book_grab_links "
            "(grab_id, library_slug, book_id) VALUES (?, ?, ?)",
            (grab_id, library_slug, book_id),
        )
        await db.commit()
    finally:
        await db.close()


async def _insert_owned_book(
    *,
    library_slug: str,
    title: str,
    author: str,
    formats: str = "m4b",
    mam_torrent_id: str = "",
) -> int:
    """Insert an owned book row in the per-library DB. Returns book_id."""
    db = await disco_db.get_db(library_slug)
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name) VALUES (?, ?)",
            (author, author),
        )
        author_id = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO books (title, source, owned, formats, "
            "                   hidden, mam_torrent_id) "
            "VALUES (?, 'audiobookshelf', 1, ?, 0, ?)",
            (title, formats, mam_torrent_id or None),
        )
        book_id = cur.lastrowid
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (?, ?, 0)", (book_id, author_id),
        )
        await db.commit()
        return book_id
    finally:
        await db.close()


async def _upsert_quality(mam_torrent_id: str, **kw) -> None:
    snap = QualitySnapshot(
        mam_torrent_id=mam_torrent_id,
        source="mediainfo",
        **kw,
    )
    db = await get_app_db()
    try:
        await upsert_quality(db, snap)
        await db.commit()
    finally:
        await db.close()


# ─── Detection scenarios ─────────────────────────────────────


async def test_higher_bitrate_records_opportunity(audiobook_library):
    """The headline case: owned m4b@64 + new grab m4b@320 = opportunity."""
    book_id = await _insert_owned_book(
        library_slug=audiobook_library["slug"],
        title="Red Rising", author="Pierce Brown",
        formats="m4b", mam_torrent_id="500",
    )
    owned_grab = await _insert_grab(
        mam_torrent_id="500",
        torrent_name="Red Rising",
        author_blob="Pierce Brown",
    )
    await _link_grab_to_book(owned_grab, audiobook_library["slug"], book_id)
    await _upsert_quality("500", audio_bitrate_kbps=64, audio_channels=2)

    candidate_grab = await _insert_grab(
        mam_torrent_id="900",
        torrent_name="Red Rising",
        author_blob="Pierce Brown",
    )
    await _upsert_quality("900", audio_bitrate_kbps=320, audio_channels=2)

    db = await get_app_db()
    try:
        recorded = await detect_for_grab(
            db, grab_id=candidate_grab,
            settings=_settings(slug=audiobook_library["slug"]),
            libraries=[audiobook_library],
        )
        assert recorded == 1
        rows = await list_opportunities(db)
    finally:
        await db.close()

    assert len(rows) == 1
    op = rows[0]
    assert op["candidate_grab_id"] == candidate_grab
    assert op["candidate_mam_torrent_id"] == "900"
    assert op["owned_library_slug"] == audiobook_library["slug"]
    assert op["owned_book_id"] == book_id
    assert op["owned_mam_torrent_id"] == "500"
    assert op["media_type"] == "audiobook"
    # Score tuples: (format_rank, bitrate_rank). m4b=0, 320=0, 64=3.
    assert op["candidate_score"] == [0, 0]
    assert op["owned_score"] == [0, 3]


async def test_same_quality_records_nothing(audiobook_library):
    """Equal score → no opportunity (we don't recommend lateral moves)."""
    book_id = await _insert_owned_book(
        library_slug=audiobook_library["slug"],
        title="Red Rising", author="Pierce Brown",
        mam_torrent_id="500",
    )
    owned_grab = await _insert_grab(
        mam_torrent_id="500", torrent_name="Red Rising",
        author_blob="Pierce Brown",
    )
    await _link_grab_to_book(owned_grab, audiobook_library["slug"], book_id)
    await _upsert_quality("500", audio_bitrate_kbps=192)

    candidate = await _insert_grab(
        mam_torrent_id="900", torrent_name="Red Rising",
        author_blob="Pierce Brown",
    )
    await _upsert_quality("900", audio_bitrate_kbps=192)

    db = await get_app_db()
    try:
        recorded = await detect_for_grab(
            db, grab_id=candidate,
            settings=_settings(slug=audiobook_library["slug"]),
            libraries=[audiobook_library],
        )
        assert recorded == 0
        assert await list_opportunities(db) == []
    finally:
        await db.close()


async def test_downgrade_records_nothing(audiobook_library):
    """Candidate worse than owned → no opportunity."""
    book_id = await _insert_owned_book(
        library_slug=audiobook_library["slug"],
        title="Red Rising", author="Pierce Brown",
        mam_torrent_id="500",
    )
    owned_grab = await _insert_grab(
        mam_torrent_id="500", torrent_name="Red Rising",
        author_blob="Pierce Brown",
    )
    await _link_grab_to_book(owned_grab, audiobook_library["slug"], book_id)
    await _upsert_quality("500", audio_bitrate_kbps=320)

    candidate = await _insert_grab(
        mam_torrent_id="900", torrent_name="Red Rising",
        author_blob="Pierce Brown",
    )
    await _upsert_quality("900", audio_bitrate_kbps=64)

    db = await get_app_db()
    try:
        recorded = await detect_for_grab(
            db, grab_id=candidate,
            settings=_settings(slug=audiobook_library["slug"]),
            libraries=[audiobook_library],
        )
        assert recorded == 0
    finally:
        await db.close()


async def test_replacement_disabled_records_nothing(audiobook_library):
    """Per-library opt-in off → safety irrelevant, detector skips."""
    book_id = await _insert_owned_book(
        library_slug=audiobook_library["slug"],
        title="Red Rising", author="Pierce Brown",
        mam_torrent_id="500",
    )
    await _link_grab_to_book(
        await _insert_grab(
            mam_torrent_id="500", torrent_name="Red Rising",
            author_blob="Pierce Brown",
        ),
        audiobook_library["slug"], book_id,
    )
    await _upsert_quality("500", audio_bitrate_kbps=64)
    candidate = await _insert_grab(
        mam_torrent_id="900", torrent_name="Red Rising",
        author_blob="Pierce Brown",
    )
    await _upsert_quality("900", audio_bitrate_kbps=320)

    db = await get_app_db()
    try:
        recorded = await detect_for_grab(
            db, grab_id=candidate,
            settings=_settings(slug=audiobook_library["slug"], enabled=False),
            libraries=[audiobook_library],
        )
        assert recorded == 0
    finally:
        await db.close()


async def test_path_overlap_blocks_detection(audiobook_library):
    """OVERLAP safety hard-disables replacement → no opportunity."""
    book_id = await _insert_owned_book(
        library_slug=audiobook_library["slug"],
        title="Red Rising", author="Pierce Brown",
        mam_torrent_id="500",
    )
    await _link_grab_to_book(
        await _insert_grab(
            mam_torrent_id="500", torrent_name="Red Rising",
            author_blob="Pierce Brown",
        ),
        audiobook_library["slug"], book_id,
    )
    await _upsert_quality("500", audio_bitrate_kbps=64)
    candidate = await _insert_grab(
        mam_torrent_id="900", torrent_name="Red Rising",
        author_blob="Pierce Brown",
    )
    await _upsert_quality("900", audio_bitrate_kbps=320)

    # Force overlap: library_path /audiobooks is now under qBit /audiobooks.
    overlap_lib = dict(audiobook_library, library_path="/downloads/audiobooks")
    s = _settings(slug=audiobook_library["slug"])
    db = await get_app_db()
    try:
        recorded = await detect_for_grab(
            db, grab_id=candidate, settings=s,
            libraries=[overlap_lib],
        )
        assert recorded == 0
    finally:
        await db.close()


async def test_self_comparison_skipped(audiobook_library):
    """If the candidate grab IS the same torrent as the owned grab
    (e.g. backfill replayed it), no self-opportunity."""
    book_id = await _insert_owned_book(
        library_slug=audiobook_library["slug"],
        title="Red Rising", author="Pierce Brown",
        mam_torrent_id="500",
    )
    owned_grab = await _insert_grab(
        mam_torrent_id="500", torrent_name="Red Rising",
        author_blob="Pierce Brown",
    )
    await _link_grab_to_book(owned_grab, audiobook_library["slug"], book_id)
    await _upsert_quality("500", audio_bitrate_kbps=64)

    # Second grab with the SAME mam_torrent_id (re-grab scenario)
    duplicate = await _insert_grab(
        mam_torrent_id="500", torrent_name="Red Rising",
        author_blob="Pierce Brown",
    )

    db = await get_app_db()
    try:
        recorded = await detect_for_grab(
            db, grab_id=duplicate,
            settings=_settings(slug=audiobook_library["slug"]),
            libraries=[audiobook_library],
        )
        assert recorded == 0
    finally:
        await db.close()


async def test_no_owned_match_records_nothing(audiobook_library):
    """New grab for a book the user doesn't own → no opportunity."""
    candidate = await _insert_grab(
        mam_torrent_id="900", torrent_name="Brand New Book",
        author_blob="New Author",
    )
    await _upsert_quality("900", audio_bitrate_kbps=320)

    db = await get_app_db()
    try:
        recorded = await detect_for_grab(
            db, grab_id=candidate,
            settings=_settings(slug=audiobook_library["slug"]),
            libraries=[audiobook_library],
        )
        assert recorded == 0
    finally:
        await db.close()


async def test_different_media_type_library_skipped(audiobook_library, monkeypatch, tmp_path):
    """An audiobook candidate doesn't fire detection against an ebook library."""
    from app import config as app_config

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    await disco_db.init_db("calibre-ebooks")
    ebook_lib = {
        "slug": "calibre-ebooks",
        "name": "Calibre",
        "content_type": "ebook",
        "app_type": "calibre",
        "library_path": "/calibre-library",
    }
    book_id = await _insert_owned_book(
        library_slug="calibre-ebooks",
        title="Red Rising", author="Pierce Brown",
        formats="epub",
    )
    candidate = await _insert_grab(
        mam_torrent_id="900",
        torrent_name="Red Rising",
        author_blob="Pierce Brown",
        category="Audiobooks - Sci-Fi",  # AUDIOBOOK candidate
        book_format="m4b",
    )
    await _upsert_quality("900", audio_bitrate_kbps=320)

    s = _settings(slug=audiobook_library["slug"])
    s["active_replacement_enabled_by_slug"]["calibre-ebooks"] = True
    db = await get_app_db()
    try:
        recorded = await detect_for_grab(
            db, grab_id=candidate, settings=s,
            libraries=[audiobook_library, ebook_lib],
        )
        # Detection only fires in the audiobook library; ebook lib is
        # the wrong content_type. The audiobook lib has no matching
        # owned row (book_id was inserted into the ebook lib's DB).
        assert recorded == 0
    finally:
        await db.close()


async def test_owned_without_quality_data_still_compared(audiobook_library):
    """Older owned book with no torrent_quality_metadata row → owned
    score is format-only with unknown numeric axes. A candidate with
    measured numeric data beats unknown — should still trigger."""
    book_id = await _insert_owned_book(
        library_slug=audiobook_library["slug"],
        title="Red Rising", author="Pierce Brown",
        formats="m4b", mam_torrent_id="500",
    )
    # No quality_metadata row for "500"; owned snapshot is None.
    candidate = await _insert_grab(
        mam_torrent_id="900", torrent_name="Red Rising",
        author_blob="Pierce Brown",
    )
    await _upsert_quality("900", audio_bitrate_kbps=192)

    db = await get_app_db()
    try:
        recorded = await detect_for_grab(
            db, grab_id=candidate,
            settings=_settings(slug=audiobook_library["slug"]),
            libraries=[audiobook_library],
        )
        assert recorded == 1
        rows = await list_opportunities(db)
        # candidate score: m4b=0, 192=1 → (0, 1)
        # owned score: m4b=0, unknown=5 (past worst, len of tier list) → (0, 5)
        assert rows[0]["candidate_score"] == [0, 1]
        assert rows[0]["owned_score"] == [0, 5]
    finally:
        await db.close()


async def test_idempotent_on_rerun(audiobook_library):
    """Running detect_for_grab twice for the same grab → 1 opportunity total."""
    book_id = await _insert_owned_book(
        library_slug=audiobook_library["slug"],
        title="Red Rising", author="Pierce Brown",
        mam_torrent_id="500",
    )
    await _link_grab_to_book(
        await _insert_grab(
            mam_torrent_id="500", torrent_name="Red Rising",
            author_blob="Pierce Brown",
        ),
        audiobook_library["slug"], book_id,
    )
    await _upsert_quality("500", audio_bitrate_kbps=64)
    candidate = await _insert_grab(
        mam_torrent_id="900", torrent_name="Red Rising",
        author_blob="Pierce Brown",
    )
    await _upsert_quality("900", audio_bitrate_kbps=320)

    db = await get_app_db()
    try:
        first = await detect_for_grab(
            db, grab_id=candidate,
            settings=_settings(slug=audiobook_library["slug"]),
            libraries=[audiobook_library],
        )
        second = await detect_for_grab(
            db, grab_id=candidate,
            settings=_settings(slug=audiobook_library["slug"]),
            libraries=[audiobook_library],
        )
        assert first == 1
        assert second == 0
        rows = await list_opportunities(db)
        assert len(rows) == 1
    finally:
        await db.close()


async def test_missing_grab_id_returns_zero(audiobook_library):
    db = await get_app_db()
    try:
        recorded = await detect_for_grab(
            db, grab_id=99999,
            settings=_settings(slug=audiobook_library["slug"]),
            libraries=[audiobook_library],
        )
        assert recorded == 0
    finally:
        await db.close()
