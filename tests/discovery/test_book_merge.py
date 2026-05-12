"""
Tests for the shared books-row merge logic
(`app.discovery.book_merge`) — the engine behind both the
calibre_sync post-UPDATE sweep and the manual-merge HTTP endpoint.

Coverage:
  - Field resolution (identity COALESCE, owned MAX, hidden MIN, etc.)
  - book_grab_links FK redirect across the slug, including the
    UNIQUE-collision case where the winner already has a link
  - Audit row written into book_merges with the loser snapshot
  - Loser row deleted; cascade FKs honored
  - Precondition errors (same id, missing row, two calibre+owned rows)
  - pick_winner_id policy across (owned × source='calibre')
"""
import json

import pytest

from app.discovery.book_merge import (
    MergeError,
    merge_books,
    pick_winner_id,
)


@pytest.fixture
async def merge_dbs(tmp_path, monkeypatch):
    """Fully-initialized discovery + pipeline DB pair on disk.

    Returns (discovery_conn, pipeline_conn) — both open. The fixture
    closes them on teardown. Tests own the data; this fixture only
    spins up empty schemas.
    """
    from app import config as app_config
    from app import database as pipeline_database
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(pipeline_database, "APP_DB_PATH", tmp_path / "seshat.db")

    await pipeline_database.init_db()
    disco_db.set_active_library("testlib")
    await disco_db.init_db("testlib")

    discovery = await disco_db.get_db(slug="testlib")
    pipeline = await pipeline_database.get_db()
    try:
        yield discovery, pipeline
    finally:
        await discovery.close()
        await pipeline.close()
        disco_db.set_active_library(None)


async def _insert_author(discovery, name: str) -> int:
    from app.metadata.author_names import normalize_author_name
    cur = await discovery.execute(
        "INSERT INTO authors (name, sort_name, normalized_name) "
        "VALUES (?, ?, ?)",
        (name, name, normalize_author_name(name)),
    )
    await discovery.commit()
    return cur.lastrowid


async def _insert_book(discovery, **fields) -> int:
    """Insert a books row with sensible defaults; returns the id."""
    defaults = {
        "title": "Untitled",
        "author_id": 1,
        "source": "goodreads",
        "owned": 0,
        "hidden": 0,
    }
    defaults.update(fields)
    cols = list(defaults.keys())
    placeholders = ", ".join("?" * len(cols))
    cur = await discovery.execute(
        f"INSERT INTO books ({', '.join(cols)}) VALUES ({placeholders})",
        list(defaults.values()),
    )
    await discovery.commit()
    return cur.lastrowid


async def _insert_grab_link(pipeline, *, grab_id, slug, book_id):
    # `grabs` has its own row required by the FK on book_grab_links.
    await pipeline.execute(
        "INSERT INTO grabs (id, mam_torrent_id, torrent_name, state) "
        "VALUES (?, ?, ?, 'complete')",
        (grab_id, f"t{grab_id}", f"grab {grab_id}"),
    )
    await pipeline.execute(
        "INSERT INTO book_grab_links (grab_id, library_slug, book_id) "
        "VALUES (?, ?, ?)",
        (grab_id, slug, book_id),
    )
    await pipeline.commit()


# ─── pick_winner_id policy ──────────────────────────────────


class TestPickWinner:
    def test_calibre_owned_beats_owned_goodreads(self):
        a = {"id": 100, "owned": 1, "source": "calibre"}
        b = {"id": 200, "owned": 1, "source": "goodreads"}
        assert pick_winner_id(a, b) == 100
        assert pick_winner_id(b, a) == 100

    def test_calibre_owned_beats_unowned_goodreads(self):
        a = {"id": 100, "owned": 1, "source": "calibre"}
        b = {"id": 200, "owned": 0, "source": "goodreads"}
        assert pick_winner_id(a, b) == 100

    def test_owned_goodreads_beats_unowned_goodreads(self):
        # Safety-net flipped row beats a stale discovery row.
        a = {"id": 100, "owned": 1, "source": "goodreads"}
        b = {"id": 200, "owned": 0, "source": "goodreads"}
        assert pick_winner_id(a, b) == 100

    def test_tiebreak_picks_lower_id(self):
        a = {"id": 200, "owned": 0, "source": "goodreads"}
        b = {"id": 100, "owned": 0, "source": "goodreads"}
        assert pick_winner_id(a, b) == 100


# ─── merge_books field resolution ───────────────────────────


class TestMergeFieldResolution:
    async def test_identity_coalesce_fills_missing_from_loser(self, merge_dbs):
        """The mark-bug case: winner is a calibre row with no
        mam_torrent_id / goodreads_id; loser is a discovery row that
        had them. Merge should carry both over."""
        discovery, pipeline = merge_dbs
        a_id = await _insert_author(discovery, "William D. Arand")
        winner = await _insert_book(
            discovery, title="Right of Retribution 2",
            author_id=a_id, source="calibre", owned=1,
            calibre_id=3897,
        )
        loser = await _insert_book(
            discovery, title="Right of Retribution 2",
            author_id=a_id, source="goodreads", owned=1,
            mam_torrent_id="713780", goodreads_id="57332968",
        )
        await merge_books(
            discovery, pipeline,
            library_slug="testlib",
            winner_id=winner, loser_id=loser,
            reason="test",
        )
        row = await (await discovery.execute(
            "SELECT mam_torrent_id, goodreads_id, calibre_id, source, "
            "owned FROM books WHERE id = ?", (winner,),
        )).fetchone()
        assert row["mam_torrent_id"] == "713780"
        assert row["goodreads_id"] == "57332968"
        assert row["calibre_id"] == 3897
        assert row["source"] == "calibre"
        assert row["owned"] == 1

    async def test_loser_row_deleted_after_merge(self, merge_dbs):
        discovery, pipeline = merge_dbs
        a_id = await _insert_author(discovery, "Arand")
        winner = await _insert_book(
            discovery, title="Book", author_id=a_id,
            source="calibre", owned=1, calibre_id=1,
        )
        loser = await _insert_book(
            discovery, title="Book", author_id=a_id,
            source="goodreads", owned=0,
        )
        await merge_books(
            discovery, pipeline,
            library_slug="testlib",
            winner_id=winner, loser_id=loser, reason="test",
        )
        row = await (await discovery.execute(
            "SELECT id FROM books WHERE id = ?", (loser,),
        )).fetchone()
        assert row is None

    async def test_hidden_min_keeps_visible(self, merge_dbs):
        """Winner is hidden=1, loser is hidden=0 → merged is hidden=0
        (visible wins; the loser was visible so the user evidently
        wanted the book showing up)."""
        discovery, pipeline = merge_dbs
        a_id = await _insert_author(discovery, "A")
        winner = await _insert_book(
            discovery, title="X", author_id=a_id,
            source="calibre", owned=1, hidden=1,
        )
        loser = await _insert_book(
            discovery, title="X", author_id=a_id,
            source="goodreads", owned=0, hidden=0,
        )
        await merge_books(
            discovery, pipeline,
            library_slug="testlib",
            winner_id=winner, loser_id=loser, reason="test",
        )
        row = await (await discovery.execute(
            "SELECT hidden FROM books WHERE id = ?", (winner,),
        )).fetchone()
        assert row["hidden"] == 0

    async def test_created_at_keeps_earliest(self, merge_dbs):
        discovery, pipeline = merge_dbs
        a_id = await _insert_author(discovery, "A")
        # Winner is the newer Calibre row (just synced); loser is the
        # old discovery row that's been in Seshat for weeks.
        winner = await _insert_book(
            discovery, title="X", author_id=a_id,
            source="calibre", owned=1, created_at=2000000.0,
            first_seen_at=2000000.0,
        )
        loser = await _insert_book(
            discovery, title="X", author_id=a_id,
            source="goodreads", owned=0, created_at=1000000.0,
            first_seen_at=1000000.0,
        )
        await merge_books(
            discovery, pipeline,
            library_slug="testlib",
            winner_id=winner, loser_id=loser, reason="test",
        )
        row = await (await discovery.execute(
            "SELECT created_at, first_seen_at FROM books WHERE id = ?",
            (winner,),
        )).fetchone()
        assert row["created_at"] == 1000000.0
        assert row["first_seen_at"] == 1000000.0


# ─── book_grab_links redirect ───────────────────────────────


class TestBookGrabLinksRedirect:
    async def test_links_redirect_to_winner(self, merge_dbs):
        discovery, pipeline = merge_dbs
        a_id = await _insert_author(discovery, "A")
        winner = await _insert_book(
            discovery, title="X", author_id=a_id,
            source="calibre", owned=1,
        )
        loser = await _insert_book(
            discovery, title="X", author_id=a_id,
            source="goodreads", owned=1,
        )
        await _insert_grab_link(
            pipeline, grab_id=42, slug="testlib", book_id=loser,
        )
        await merge_books(
            discovery, pipeline,
            library_slug="testlib",
            winner_id=winner, loser_id=loser, reason="test",
        )
        row = await (await pipeline.execute(
            "SELECT book_id FROM book_grab_links WHERE grab_id = ?",
            (42,),
        )).fetchone()
        assert row["book_id"] == winner

    async def test_unique_collision_drops_loser_link(self, merge_dbs):
        """Winner ALREADY has a link, loser also has one. Redirecting
        loser → winner would violate UNIQUE(library_slug, book_id), so
        the loser's link is dropped instead."""
        discovery, pipeline = merge_dbs
        a_id = await _insert_author(discovery, "A")
        winner = await _insert_book(
            discovery, title="X", author_id=a_id,
            source="calibre", owned=1,
        )
        loser = await _insert_book(
            discovery, title="X", author_id=a_id,
            source="goodreads", owned=1,
        )
        await _insert_grab_link(
            pipeline, grab_id=1, slug="testlib", book_id=winner,
        )
        await _insert_grab_link(
            pipeline, grab_id=2, slug="testlib", book_id=loser,
        )
        await merge_books(
            discovery, pipeline,
            library_slug="testlib",
            winner_id=winner, loser_id=loser, reason="test",
        )
        winner_links = await (await pipeline.execute(
            "SELECT grab_id, book_id FROM book_grab_links "
            "WHERE library_slug = ? AND book_id = ?",
            ("testlib", winner),
        )).fetchall()
        # Winner's original link survives. Loser's link is gone.
        ids = sorted(r["grab_id"] for r in winner_links)
        assert ids == [1]


# ─── Audit row + error preconditions ───────────────────────


class TestAuditAndErrors:
    async def test_audit_row_captures_loser_snapshot(self, merge_dbs):
        discovery, pipeline = merge_dbs
        a_id = await _insert_author(discovery, "A")
        winner = await _insert_book(
            discovery, title="X", author_id=a_id,
            source="calibre", owned=1,
        )
        loser = await _insert_book(
            discovery, title="X", author_id=a_id,
            source="goodreads", owned=0,
            goodreads_id="12345",
        )
        await merge_books(
            discovery, pipeline,
            library_slug="testlib",
            winner_id=winner, loser_id=loser, reason="manual",
        )
        audit = await (await discovery.execute(
            "SELECT winner_id, loser_id, reason, loser_snapshot_json "
            "FROM book_merges ORDER BY id DESC LIMIT 1",
        )).fetchone()
        assert audit["winner_id"] == winner
        assert audit["loser_id"] == loser
        assert audit["reason"] == "manual"
        snap = json.loads(audit["loser_snapshot_json"])
        assert snap["goodreads_id"] == "12345"
        assert snap["title"] == "X"

    async def test_same_id_rejected(self, merge_dbs):
        discovery, pipeline = merge_dbs
        with pytest.raises(MergeError, match="itself"):
            await merge_books(
                discovery, pipeline,
                library_slug="testlib",
                winner_id=5, loser_id=5, reason="test",
            )

    async def test_missing_row_rejected(self, merge_dbs):
        discovery, pipeline = merge_dbs
        a_id = await _insert_author(discovery, "A")
        winner = await _insert_book(
            discovery, title="X", author_id=a_id,
        )
        with pytest.raises(MergeError, match="not found"):
            await merge_books(
                discovery, pipeline,
                library_slug="testlib",
                winner_id=winner, loser_id=99999, reason="test",
            )

    async def test_two_calibre_owned_rows_rejected(self, merge_dbs):
        """The "Right of Retribution 3796/3897" upstream-duplicate case.
        Seshat refuses to auto-pick because the real fix is in Calibre."""
        discovery, pipeline = merge_dbs
        a_id = await _insert_author(discovery, "A")
        a = await _insert_book(
            discovery, title="X", author_id=a_id,
            source="calibre", owned=1, calibre_id=3796,
        )
        b = await _insert_book(
            discovery, title="X", author_id=a_id,
            source="calibre", owned=1, calibre_id=3897,
        )
        with pytest.raises(MergeError, match="owned Calibre rows"):
            await merge_books(
                discovery, pipeline,
                library_slug="testlib",
                winner_id=a, loser_id=b, reason="test",
            )
