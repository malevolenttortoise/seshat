"""
Integration tests for the quality-metadata storage layer.

Walks the full schema path: schema init → grabs + book_grab_links
fixtures → upsert quality → query by torrent + by book → list missing
→ coverage stats. Uses a temp SQLite file (not memory) so the schema
init runs the real SQLite engine and surfaces any column-type errors.
"""
from __future__ import annotations

import time

import pytest

from app import database
from app.quality.extract import QualitySnapshot
from app.quality.storage import (
    get_quality,
    get_quality_for_book,
    list_missing_quality_torrent_ids,
    quality_coverage_stats,
    upsert_quality,
)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """Initialize a fresh DB at a tmp path + apply the schema.

    Patches `APP_DB_PATH` so the regular `init_db()` writes here, then
    opens a connection via `get_db()` (also picks up the patched path
    automatically). Tests use this connection directly to seed grabs +
    book_grab_links rows.
    """
    monkeypatch.setattr(database, "APP_DB_PATH", tmp_path / "test.db")
    await database.init_db()
    conn = await database.get_db()
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_grab_and_link(
    db,
    *,
    grab_id: int,
    mam_torrent_id: str,
    library_slug: str,
    book_id: int,
) -> None:
    """Seed an announce → grab → book_grab_link chain.

    Minimal fields only — the storage queries we test don't touch
    most grab columns. announce_id stays NULL via the SET NULL FK.
    """
    await db.execute(
        """
        INSERT INTO grabs (
            id, announce_id, mam_torrent_id, qbit_hash, state,
            torrent_name, category, author_blob
        ) VALUES (?, NULL, ?, ?, 'submitted',
                  'test', 'audio', 'test author')
        """,
        (grab_id, mam_torrent_id, f"hash_{grab_id}"),
    )
    await db.execute(
        """
        INSERT INTO book_grab_links (grab_id, library_slug, book_id)
        VALUES (?, ?, ?)
        """,
        (grab_id, library_slug, book_id),
    )
    await db.commit()


def _snapshot(mam_torrent_id: str, source: str = "mediainfo") -> QualitySnapshot:
    """Build a QualitySnapshot with representative non-null fields."""
    return QualitySnapshot(
        mam_torrent_id=mam_torrent_id,
        source=source,
        audio_format="AAC",
        audio_bitrate_kbps=126,
        audio_channels=2,
        audio_bitrate_mode="CBR",
        audio_sample_rate=44100,
        audio_compression="Lossy",
        audio_codec_id="2 / 40 / mp4a-40-2",
        audio_duration_sec=43507,
        audio_chapter_count=42,
        container_format="MPEG-4",
        num_files=1,
        total_size_bytes=int(658.5 * 1024 ** 2),
        seeders=183,
        times_completed=249,
        torrent_added_at="2026-04-24 12:21:59",
        raw_mediainfo='{"some": "blob"}',
        raw_tags="126 kbps m4b",
    )


# ─── Upsert + read by torrent_id ──────────────────────────────


async def test_upsert_and_get_round_trip(db):
    """Insert one row, read it back, every field matches."""
    snap = _snapshot("1237094")
    await upsert_quality(db, snap)
    await db.commit()

    row = await get_quality(db, "1237094")
    assert row is not None
    assert row["mam_torrent_id"] == "1237094"
    assert row["source"] == "mediainfo"
    assert row["audio_format"] == "AAC"
    assert row["audio_bitrate_kbps"] == 126
    assert row["audio_chapter_count"] == 42
    assert row["seeders"] == 183
    assert row["raw_mediainfo"] == '{"some": "blob"}'
    # extracted_at is set by upsert_quality to time.time(); should be
    # within a few seconds of now.
    assert abs(row["extracted_at"] - time.time()) < 10


async def test_upsert_overwrites_existing_row(db):
    """Re-extraction replaces the old row — last-write-wins."""
    await upsert_quality(db, _snapshot("x", source="tags"))
    await db.commit()
    await upsert_quality(db, _snapshot("x", source="mediainfo"))
    await db.commit()
    row = await get_quality(db, "x")
    assert row["source"] == "mediainfo"


async def test_get_quality_returns_none_for_unknown_torrent(db):
    assert await get_quality(db, "does-not-exist") is None


# ─── Join: book → torrent → quality ───────────────────────────


async def test_get_quality_for_book_walks_full_join(db):
    """Owned book → grab → mam_torrent_id → quality row, end-to-end."""
    await _seed_grab_and_link(
        db, grab_id=1, mam_torrent_id="1237094",
        library_slug="library_calibre", book_id=42,
    )
    await upsert_quality(db, _snapshot("1237094"))
    await db.commit()

    row = await get_quality_for_book(db, "library_calibre", 42)
    assert row is not None
    assert row["mam_torrent_id"] == "1237094"
    assert row["audio_bitrate_kbps"] == 126


async def test_get_quality_for_book_returns_none_when_unlinked(db):
    """Book has no grab link → None."""
    assert await get_quality_for_book(db, "library_calibre", 99) is None


async def test_get_quality_for_book_returns_none_when_torrent_unextracted(db):
    """Book → grab exists but torrent's quality row not yet extracted."""
    await _seed_grab_and_link(
        db, grab_id=2, mam_torrent_id="not-yet",
        library_slug="library_calibre", book_id=43,
    )
    assert await get_quality_for_book(db, "library_calibre", 43) is None


# ─── Backfill list + stats ────────────────────────────────────


async def test_list_missing_returns_only_unextracted(db):
    """Two grabs linked, only one has quality data → other is listed."""
    await _seed_grab_and_link(
        db, grab_id=10, mam_torrent_id="tid_extracted",
        library_slug="lib", book_id=100,
    )
    await _seed_grab_and_link(
        db, grab_id=11, mam_torrent_id="tid_missing",
        library_slug="lib", book_id=101,
    )
    await upsert_quality(db, _snapshot("tid_extracted"))
    await db.commit()

    missing = await list_missing_quality_torrent_ids(db, limit=50)
    assert missing == ["tid_missing"]


async def test_list_missing_excludes_unlinked_grabs(db):
    """A grab with no book_grab_link row is NOT listed for backfill.

    We only care about owned-book torrents — failed / abandoned grabs
    that never linked to a book aren't worth a MAM call.
    """
    # Seed a grab WITHOUT a book_grab_link row.
    await db.execute(
        """
        INSERT INTO grabs (
            id, announce_id, mam_torrent_id, qbit_hash, state,
            torrent_name, category, author_blob
        ) VALUES (?, NULL, ?, ?, 'failed',
                  'test', 'audio', 'test author')
        """,
        (99, "tid_orphan", "hash_orphan"),
    )
    await db.commit()
    assert await list_missing_quality_torrent_ids(db, limit=50) == []


async def test_coverage_stats(db):
    """Stats reflect linked + extracted + by-source breakdown."""
    await _seed_grab_and_link(
        db, grab_id=20, mam_torrent_id="tid_a",
        library_slug="lib", book_id=200,
    )
    await _seed_grab_and_link(
        db, grab_id=21, mam_torrent_id="tid_b",
        library_slug="lib", book_id=201,
    )
    await _seed_grab_and_link(
        db, grab_id=22, mam_torrent_id="tid_c",
        library_slug="lib", book_id=202,
    )
    await upsert_quality(db, _snapshot("tid_a", source="mediainfo"))
    await upsert_quality(db, _snapshot("tid_b", source="description"))
    # tid_c left unextracted → counts as missing.
    await db.commit()

    stats = await quality_coverage_stats(db)
    assert stats["linked_torrents"] == 3
    assert stats["extracted"] == 2
    assert stats["missing"] == 1
    assert stats["by_source"]["mediainfo"] == 1
    assert stats["by_source"]["description"] == 1
    assert stats["by_source"]["tags"] == 0
    assert stats["by_source"]["unavailable"] == 0


async def test_unavailable_stub_row_counts_as_extracted(db):
    """v2.25.0 hotfix — `source='unavailable'` stub rows are written
    when MAM no longer returns the torrent. They should:
      1. Count as extracted (not missing), preventing re-entry into
         the backfill loop on subsequent runs.
      2. Appear in by_source.unavailable for at-a-glance visibility.
    """
    await _seed_grab_and_link(
        db, grab_id=30, mam_torrent_id="dead_torrent",
        library_slug="lib", book_id=300,
    )
    stub = QualitySnapshot(
        mam_torrent_id="dead_torrent",
        source="unavailable",
    )
    await upsert_quality(db, stub)
    await db.commit()

    # Backfill list MUST exclude it now.
    missing = await list_missing_quality_torrent_ids(db, limit=50)
    assert "dead_torrent" not in missing

    # Stats roll it into extracted + the unavailable bucket.
    stats = await quality_coverage_stats(db)
    assert stats["missing"] == 0
    assert stats["extracted"] == 1
    assert stats["by_source"]["unavailable"] == 1
