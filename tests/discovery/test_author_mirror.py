"""Unit tests for the v2.12.1 dual-author-row pattern.

Two entry points:
  - `mirror_new_author_to_other_type_libs` called per-author from
    the sync paths.
  - `backfill_dual_author_rows` one-shot startup pass.

Both should be idempotent and respect content_type partitioning.
"""
from __future__ import annotations

import pytest


@pytest.fixture
async def two_lib_setup(tmp_path, monkeypatch):
    """Stand up one ebook lib + one audiobook lib in `tmp_path`."""
    from app import config as app_config
    from app.discovery import database as disco_db
    from app import state

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)

    # Init both lib DBs.
    await disco_db.init_db("ebook-lib")
    await disco_db.init_db("audio-lib")

    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "ebook-lib", "content_type": "ebook", "name": "Test Ebooks"},
        {"slug": "audio-lib", "content_type": "audiobook", "name": "Test ABS"},
    ])
    yield tmp_path
    disco_db.set_active_library(None)


async def _insert_author(slug: str, name: str, *, calibre_id: int | None = None,
                         audiobookshelf_id: str | None = None) -> int:
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, calibre_id, audiobookshelf_id) "
            "VALUES (?, ?, ?, ?)",
            (name, name, calibre_id, audiobookshelf_id),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _count_authors(slug: str, name: str) -> int:
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        rows = await (await db.execute(
            "SELECT id FROM authors WHERE name = ?", (name,),
        )).fetchall()
        return len(rows)
    finally:
        await db.close()


# ─── mirror_new_author_to_other_type_libs ─────────────────────────


async def test_mirror_ebook_creates_audiobook_stub(two_lib_setup):
    """Adding 'Brandon Sanderson' to the ebook lib should create a
    stub in the audiobook lib.
    """
    from app.discovery.author_mirror import mirror_new_author_to_other_type_libs

    await _insert_author("ebook-lib", "Brandon Sanderson", calibre_id=42)
    n = await mirror_new_author_to_other_type_libs(
        "Brandon Sanderson", source_content_type="ebook",
    )
    assert n == 1
    assert await _count_authors("audio-lib", "Brandon Sanderson") == 1
    # Source lib is untouched.
    assert await _count_authors("ebook-lib", "Brandon Sanderson") == 1


async def test_mirror_audiobook_creates_ebook_stub(two_lib_setup):
    """Symmetric direction — adding to audiobook lib creates ebook stub.
    """
    from app.discovery.author_mirror import mirror_new_author_to_other_type_libs

    await _insert_author("audio-lib", "James S. A. Corey",
                         audiobookshelf_id="abc-123")
    n = await mirror_new_author_to_other_type_libs(
        "James S. A. Corey", source_content_type="audiobook",
    )
    assert n == 1
    assert await _count_authors("ebook-lib", "James S. A. Corey") == 1


async def test_mirror_is_idempotent(two_lib_setup):
    """Calling mirror twice doesn't create duplicate stubs."""
    from app.discovery.author_mirror import mirror_new_author_to_other_type_libs

    await _insert_author("ebook-lib", "Wen Spencer", calibre_id=99)
    n1 = await mirror_new_author_to_other_type_libs(
        "Wen Spencer", source_content_type="ebook",
    )
    n2 = await mirror_new_author_to_other_type_libs(
        "Wen Spencer", source_content_type="ebook",
    )
    assert n1 == 1
    assert n2 == 0  # Second call finds existing stub, returns 0.
    assert await _count_authors("audio-lib", "Wen Spencer") == 1


async def test_mirror_skips_when_no_other_type_libs(tmp_path, monkeypatch):
    """User with only one library type — nothing to mirror."""
    from app import config as app_config
    from app.discovery import database as disco_db
    from app import state
    from app.discovery.author_mirror import mirror_new_author_to_other_type_libs

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    await disco_db.init_db("only-ebooks")
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "only-ebooks", "content_type": "ebook", "name": "Solo"},
    ])
    await _insert_author("only-ebooks", "Solo Author", calibre_id=1)
    n = await mirror_new_author_to_other_type_libs(
        "Solo Author", source_content_type="ebook",
    )
    assert n == 0


async def test_mirror_rejects_empty_name(two_lib_setup):
    """Empty / whitespace names refuse to mirror (silent no-op)."""
    from app.discovery.author_mirror import mirror_new_author_to_other_type_libs

    assert await mirror_new_author_to_other_type_libs(
        "", source_content_type="ebook",
    ) == 0
    assert await mirror_new_author_to_other_type_libs(
        "   ", source_content_type="ebook",
    ) == 0


async def test_mirror_unknown_content_type_refuses(two_lib_setup):
    """Future-type guard: 'comic' (or anything else) should refuse to
    mirror rather than fan into every other-type lib.
    """
    from app.discovery.author_mirror import mirror_new_author_to_other_type_libs

    n = await mirror_new_author_to_other_type_libs(
        "Some Author", source_content_type="comic",
    )
    assert n == 0


# ─── backfill_dual_author_rows ─────────────────────────────────────


async def test_backfill_creates_stubs_both_directions(two_lib_setup):
    """Author in ebook lib → stub in audiobook lib + vice versa."""
    from app.discovery.author_mirror import backfill_dual_author_rows

    await _insert_author("ebook-lib", "Author A", calibre_id=1)
    await _insert_author("audio-lib", "Author B", audiobookshelf_id="x")

    result = await backfill_dual_author_rows()
    assert result["stubs_inserted"] == 2
    assert await _count_authors("audio-lib", "Author A") == 1
    assert await _count_authors("ebook-lib", "Author B") == 1


async def test_backfill_is_idempotent(two_lib_setup):
    """Running backfill twice doesn't duplicate stubs."""
    from app.discovery.author_mirror import backfill_dual_author_rows

    await _insert_author("ebook-lib", "Author A", calibre_id=1)
    r1 = await backfill_dual_author_rows()
    r2 = await backfill_dual_author_rows()
    assert r1["stubs_inserted"] == 1
    assert r2["stubs_inserted"] == 0
    assert await _count_authors("audio-lib", "Author A") == 1


async def test_backfill_skips_when_one_type_missing(tmp_path, monkeypatch):
    """User with only ebook libs — backfill is a no-op + logs nothing."""
    from app import config as app_config
    from app.discovery import database as disco_db
    from app import state
    from app.discovery.author_mirror import backfill_dual_author_rows

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    await disco_db.init_db("only-ebooks")
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "only-ebooks", "content_type": "ebook", "name": "Solo"},
    ])
    await _insert_author("only-ebooks", "Author A", calibre_id=1)
    result = await backfill_dual_author_rows()
    assert result["stubs_inserted"] == 0


async def test_backfill_skips_existing_matches(two_lib_setup):
    """Author exists in BOTH libs already — backfill finds existing
    rows + skips.
    """
    from app.discovery.author_mirror import backfill_dual_author_rows

    await _insert_author("ebook-lib", "Shared Author", calibre_id=1)
    await _insert_author("audio-lib", "Shared Author", audiobookshelf_id="x")
    result = await backfill_dual_author_rows()
    assert result["stubs_inserted"] == 0
    # Each lib still has exactly one row for this name.
    assert await _count_authors("ebook-lib", "Shared Author") == 1
    assert await _count_authors("audio-lib", "Shared Author") == 1
