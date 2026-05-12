"""
v2.10.0 calibre_sync post-UPDATE merge sweep tests.

The merge sweep heals a specific class of duplicate that the
INSERT-path merge can't catch: when a Calibre row was previously
inserted with a wrong / mismatched title that didn't merge with
the existing discovery row, and the user later fixes the title in
Calibre. The next resync hits the UPDATE path (existing
calibre_id), so the INSERT-path merge query never runs — pre-v2.10
the duplicate stayed forever. The sweep re-runs the same
exact-title-match against unowned rows after every UPDATE and
folds in the single unambiguous match.

This was the exact pattern Mark hit with William D. Arand books
on 2026-05-11/12 — six pairs (Right of Retribution 2, Dungeon
Deposed 2/3, Super Sales 2/3, A Temperamental Enchantress) sat
duplicated even after his Calibre metadata fix until the manual-
merge UI shipped.
"""
from __future__ import annotations

import pytest


def _book(book_id, title, author_name="William D. Arand", author_id=100):
    return {
        "book_id": book_id,
        "title": title,
        "pubdate": "2024-01-01",
        "series_index": 2.0,
        "book_path": f"{author_name}/{title}",
        "cover_path": None,
        "isbn": None,
        "authors": [{"id": author_id, "name": author_name,
                     "sort": author_name}],
        "series": [],
        "tags": None,
        "rating": None,
        "description": None,
        "language": "eng",
        "publisher": None,
        "formats": "epub",
    }


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    """Per-test discovery DB + pipeline DB co-located under tmp_path."""
    from app import config as app_config
    from app import database as pipeline_database
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(pipeline_database, "APP_DB_PATH", tmp_path / "seshat.db")
    await pipeline_database.init_db()
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


async def _insert_unowned_discovery_row(*, author_id, title, mam_torrent_id,
                                        goodreads_id):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO books (title, author_id, source, owned, "
            "mam_torrent_id, goodreads_id) "
            "VALUES (?, ?, 'goodreads', 0, ?, ?)",
            (title, author_id, mam_torrent_id, goodreads_id),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _insert_author(name, calibre_id):
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name, "
            "calibre_id) VALUES (?, ?, ?, ?)",
            (name, name, normalize_author_name(name), calibre_id),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _book_row_by_calibre_id(calibre_id):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT * FROM books WHERE calibre_id = ?", (calibre_id,),
        )).fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def _audit_rows():
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT * FROM book_merges ORDER BY id",
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


class TestPostUpdateMergeSweep:
    """When a Calibre row's title is corrected to match an unowned
    discovery row, the next sync should fold the discovery row into
    the Calibre row."""

    async def test_heals_marks_arand_scenario(self, discovery_db,
                                              monkeypatch):
        """Reproduce Mark's Right of Retribution 2 case: pre-existing
        unowned Goodreads row + a Calibre row that was previously
        inserted with the wrong title, then user fixed the Calibre
        title to match. Sync should merge them."""
        from app.discovery import calibre_sync

        # Step 1: First sync — Calibre has the WRONG title.
        # This goes through the INSERT path (no existing calibre_id)
        # but the merge query misses because the title doesn't match
        # the Goodreads row exactly. Two rows result.
        author_id = await _insert_author("William D. Arand", calibre_id=1)
        await _insert_unowned_discovery_row(
            author_id=author_id,
            title="Right of Retribution 2",
            mam_torrent_id="713780",
            goodreads_id="57332968",
        )

        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(3897, "Right of Retribution: Book 2", author_id=1),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        # Sanity: post-first-sync we have 2 rows (the duplicate state
        # Mark experienced).
        from app.discovery.database import get_db
        db = await get_db()
        try:
            count = await (await db.execute(
                "SELECT COUNT(*) AS n FROM books WHERE author_id = ?",
                (author_id,),
            )).fetchone()
        finally:
            await db.close()
        assert count["n"] == 2

        # Step 2: User fixes Calibre title. Re-sync. The sweep fires.
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(3897, "Right of Retribution 2", author_id=1),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        # Post-sweep: exactly one row for this author.
        db = await get_db()
        try:
            count = await (await db.execute(
                "SELECT COUNT(*) AS n FROM books WHERE author_id = ?",
                (author_id,),
            )).fetchone()
        finally:
            await db.close()
        assert count["n"] == 1, (
            "post-update merge sweep should have folded the unowned "
            "discovery row into the Calibre row, leaving 1 books row"
        )

        # And the surviving row carries BOTH the calibre_id (from
        # the original Calibre row) AND the mam_torrent_id +
        # goodreads_id (from the absorbed Goodreads row).
        winner = await _book_row_by_calibre_id(3897)
        assert winner is not None
        assert winner["title"] == "Right of Retribution 2"
        assert winner["source"] == "calibre"
        assert winner["owned"] == 1
        assert winner["mam_torrent_id"] == "713780"
        assert winner["goodreads_id"] == "57332968"

        # An audit row records the merge with the calibre_sync reason.
        audits = await _audit_rows()
        assert len(audits) == 1
        assert audits[0]["reason"] == "calibre_sync_post_update"
        assert audits[0]["winner_id"] == winner["id"]

    async def test_no_merge_when_titles_still_differ(self, discovery_db,
                                                    monkeypatch):
        """If the Calibre title still doesn't match the Goodreads
        title, the sweep is a no-op. Both rows survive."""
        from app.discovery import calibre_sync

        author_id = await _insert_author("William D. Arand", calibre_id=1)
        await _insert_unowned_discovery_row(
            author_id=author_id,
            title="Swing Shift 2",
            mam_torrent_id="560100",
            goodreads_id="53166533",
        )

        # Sync once with the wrong title.
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(3908, "Swing Shift: Book 2", author_id=1),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")

        # Re-sync without fixing the title — the sweep should NOT merge
        # because exact-title-match fails.
        await calibre_sync.sync_calibre("x", "y")

        from app.discovery.database import get_db
        db = await get_db()
        try:
            count = await (await db.execute(
                "SELECT COUNT(*) AS n FROM books WHERE author_id = ?",
                (author_id,),
            )).fetchone()
        finally:
            await db.close()
        assert count["n"] == 2

        # No audit row — sweep didn't fire.
        audits = await _audit_rows()
        assert audits == []

    async def test_no_merge_when_multiple_candidates_match(
        self, discovery_db, monkeypatch,
    ):
        """Two unowned discovery rows share the same exact title (e.g.
        from different sources). The sweep refuses to pick — both stay,
        the Calibre row stays, three total. Conservative, matches the
        INSERT-path semantics."""
        from app.discovery import calibre_sync

        author_id = await _insert_author("William D. Arand", calibre_id=1)
        # Two unowned rows with the same title.
        await _insert_unowned_discovery_row(
            author_id=author_id, title="The Book",
            mam_torrent_id="111", goodreads_id="222",
        )
        await _insert_unowned_discovery_row(
            author_id=author_id, title="The Book",
            mam_torrent_id="333", goodreads_id="444",
        )

        # Insert the Calibre row with the right title via a first sync.
        # On this first sync the INSERT-path merge query also sees both
        # candidates so it goes down the INSERT branch — leaving us with
        # 3 rows (two discovery + one new calibre). The next sync hits
        # the UPDATE path; the sweep should again see both candidates
        # and refuse to merge.
        monkeypatch.setattr(
            calibre_sync, "_read_calibre_db",
            lambda *a, **kw: {"books": [
                _book(1, "The Book", author_id=1),
            ]},
        )
        await calibre_sync.sync_calibre("x", "y")
        await calibre_sync.sync_calibre("x", "y")

        from app.discovery.database import get_db
        db = await get_db()
        try:
            count = await (await db.execute(
                "SELECT COUNT(*) AS n FROM books WHERE author_id = ?",
                (author_id,),
            )).fetchone()
        finally:
            await db.close()
        assert count["n"] == 3
        audits = await _audit_rows()
        assert audits == []
