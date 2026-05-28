"""cover_phash backfill + ensure_cover_phash tests.

Per-library helpers that wrap `app.mam.cover_hash` for the books
table. Covers the four resolution paths:

  1. Already populated → returned as-is, no I/O
  2. cover_path → hash from disk, persist
  3. cover_url (non-MAM) → fetch via httpx, hash, persist
  4. nothing available → returns None (graceful degrade)

Backfill is exercised on a populated tmp DB to confirm idempotency.
"""
from io import BytesIO

import aiosqlite
import pytest
from PIL import Image, ImageDraw

from app.discovery import cover_phash as cph
from app.discovery.database import init_db


def _make_jpeg_bytes(*, color: tuple[int, int, int] = (200, 100, 50)) -> bytes:
    img = Image.new("RGB", (200, 300), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle((20, 30, 180, 270), outline=(0, 0, 0), width=4)
    draw.ellipse((60, 80, 140, 220), fill=(20, 200, 100))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


@pytest.fixture
async def disc_db(tmp_path, monkeypatch):
    """Per-library discovery DB with a fresh schema."""
    from app import config
    from app.discovery import database as ddb

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ddb, "DATA_DIR", tmp_path)
    ddb.set_active_library("test")
    await init_db("test")
    db = await ddb.get_db("test")
    # Books table needs a real author_id to insert into.
    await db.execute(
        "INSERT INTO authors (name, sort_name, normalized_name) "
        "VALUES ('Test Author', 'Test Author', 'testauthor')"
    )
    await db.commit()
    yield db
    await db.close()


# ─── ensure_cover_phash ──────────────────────────────────────────


class TestEnsureCoverPhash:
    @pytest.mark.asyncio
    async def test_returns_existing_phash_without_io(self, disc_db):
        await disc_db.execute(
            "INSERT INTO books (id, title, source, owned, cover_phash) "
            "VALUES (1, 'X', 'calibre', 1, 'abcd1234abcd1234')"
        )
        await disc_db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (1, 1, 0)"
        )
        await disc_db.commit()
        h = await cph.ensure_cover_phash(disc_db, 1)
        assert h == "abcd1234abcd1234"

    @pytest.mark.asyncio
    async def test_hashes_cover_path_when_phash_null(
        self, disc_db, tmp_path,
    ):
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(_make_jpeg_bytes(color=(123, 45, 67)))
        await disc_db.execute(
            "INSERT INTO books (id, title, source, owned, cover_path) "
            "VALUES (2, 'X', 'calibre', 1, ?)",
            (str(cover),),
        )
        await disc_db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (2, 1, 0)"
        )
        await disc_db.commit()
        h = await cph.ensure_cover_phash(disc_db, 2)
        assert h is not None and len(h) == 16
        # Persisted on the row
        row = await (await disc_db.execute(
            "SELECT cover_phash FROM books WHERE id=2"
        )).fetchone()
        assert row["cover_phash"] == h

    @pytest.mark.asyncio
    async def test_returns_none_when_no_cover_info(self, disc_db):
        await disc_db.execute(
            "INSERT INTO books (id, title, source, owned) "
            "VALUES (3, 'X', 'calibre', 1)"
        )
        await disc_db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (3, 1, 0)"
        )
        await disc_db.commit()
        assert await cph.ensure_cover_phash(disc_db, 3) is None

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_book(self, disc_db):
        assert await cph.ensure_cover_phash(disc_db, 9999) is None

    @pytest.mark.asyncio
    async def test_skips_persist_when_hash_fails(
        self, disc_db, tmp_path,
    ):
        cover = tmp_path / "broken.jpg"
        cover.write_bytes(b"definitely not an image" * 100)
        await disc_db.execute(
            "INSERT INTO books (id, title, source, owned, cover_path) "
            "VALUES (4, 'X', 'calibre', 1, ?)",
            (str(cover),),
        )
        await disc_db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (4, 1, 0)"
        )
        await disc_db.commit()
        h = await cph.ensure_cover_phash(disc_db, 4)
        assert h is None
        row = await (await disc_db.execute(
            "SELECT cover_phash FROM books WHERE id=4"
        )).fetchone()
        assert row["cover_phash"] is None  # left NULL for retry next time


# ─── backfill_cover_phashes_from_paths ───────────────────────────


class TestBackfill:
    @pytest.mark.asyncio
    async def test_populates_null_rows(self, disc_db, tmp_path):
        # Three books: one with cover, one with broken cover, one without.
        good = tmp_path / "good.jpg"
        good.write_bytes(_make_jpeg_bytes(color=(50, 100, 150)))
        bad = tmp_path / "bad.jpg"
        bad.write_bytes(b"not an image" * 50)
        await disc_db.execute(
            "INSERT INTO books (id, title, source, owned, cover_path) "
            "VALUES (10, 'A', 'calibre', 1, ?), "
            "       (11, 'B', 'calibre', 1, ?), "
            "       (12, 'C', 'calibre', 1, NULL)",
            (str(good), str(bad)),
        )
        for bid in (10, 11, 12):
            await disc_db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, 1, 0)", (bid,),
            )
        await disc_db.commit()
        updated, skipped = await cph.backfill_cover_phashes_from_paths(disc_db)
        assert updated == 1   # only `good` succeeds
        assert skipped == 1   # `bad` fails to decode
        # Row 12 (no cover_path) wasn't touched at all
        row = await (await disc_db.execute(
            "SELECT cover_phash FROM books WHERE id IN (10, 11, 12) ORDER BY id"
        )).fetchall()
        assert row[0]["cover_phash"] is not None
        assert row[1]["cover_phash"] is None
        assert row[2]["cover_phash"] is None

    @pytest.mark.asyncio
    async def test_idempotent(self, disc_db, tmp_path):
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(_make_jpeg_bytes())
        await disc_db.execute(
            "INSERT INTO books (id, title, source, owned, cover_path) "
            "VALUES (20, 'X', 'calibre', 1, ?)",
            (str(cover),),
        )
        await disc_db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (20, 1, 0)"
        )
        await disc_db.commit()
        u1, _ = await cph.backfill_cover_phashes_from_paths(disc_db)
        u2, _ = await cph.backfill_cover_phashes_from_paths(disc_db)
        assert u1 == 1
        assert u2 == 0  # row already populated, second pass is no-op
