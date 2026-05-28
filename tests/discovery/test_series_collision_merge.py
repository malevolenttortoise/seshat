"""
v2.10.2 — `_resolve_position_collision` carries identity fields via merge_books.

Pre-v2.10.2 the helper hard-DELETEd the loser at a series-position
collision. Any identity fields the loser uniquely carried
(mam_torrent_id, goodreads_id, isbn) were lost. The 2026-05-12
diagnostic on Mark's library found 5 unowned discovery rows
(7553, 7552, 7545, 7546, 7671) that vanished via this path without
an audit trail — in their case the calibre side already had the
same mam_torrent_id from `acquisition_linkback` so nothing was
actually lost, but the sharp edge could bite the next case where
the loser has a unique ID. Now the helper routes through
`merge_books` so identity fields coalesce and an audit row
records the merge.
"""
from __future__ import annotations

import pytest


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
    disco_db.set_active_library("testlib")
    await disco_db.init_db("testlib")
    yield tmp_path
    disco_db.set_active_library(None)


async def _insert_author(name: str) -> int:
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name) "
            "VALUES (?, ?, ?)",
            (name, name, normalize_author_name(name)),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _insert_series(name: str, author_id: int) -> int:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO series (name, author_id) VALUES (?, ?)",
            (name, author_id),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _audit_rows():
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT * FROM book_merges ORDER BY id"
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


class TestSeriesPositionCollisionCarriesIdentity:
    """When the orphan-series promotion pass dedups two rows at the
    same (series_id, series_index), the loser's identity fields
    should land on the winner and the merge should appear in
    `book_merges` for forensic audit."""

    async def test_title_to_series_pass_carries_loser_identity(
        self, discovery_db, monkeypatch,
    ):
        """The realistic Mark case: a calibre row is already linked
        to a series at a given (sid, idx), and the title→series
        pass tries to link a goodreads standalone with the same
        parsed (sid, idx). The dedup helper fires, the calibre row
        wins (owned), the goodreads identity fields land on the
        calibre row, and an audit row is written."""
        from app.discovery import lookup
        from app.discovery.database import get_db

        author_id = await _insert_author("Eric Vall")
        sid = await _insert_series("Fantasy World Farm", author_id)

        # Calibre row already at series-position #4 — pre-existing
        # "owned" anchor with no Goodreads ID.
        db = await get_db()
        try:
            # v3.0.0: books.author_id dropped; seed book_authors instead.
            cur = await db.execute(
                "INSERT INTO books (title, series_id, "
                "series_index, source, owned) VALUES "
                "('Fantasy World Farm 4', ?, 4, 'calibre', 1)",
                (sid,),
            )
            calibre_id = cur.lastrowid
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, 0)", (calibre_id, author_id),
            )
            # Discovery standalone — same author, no series link
            # yet, title that the title→series pass parses as
            # (series='Fantasy World Farm', idx=4). Carries a
            # unique goodreads_id + mam_torrent_id + isbn the
            # calibre side doesn't have.
            cur = await db.execute(
                "INSERT INTO books (title, source, owned, "
                "mam_torrent_id, goodreads_id, isbn) VALUES "
                "('Fantasy World Farm 4 (Fantasy World Farm #4)', "
                "'goodreads', 0, 'mam_42', 'gr_42', 'isbn_42')",
            )
            discovery_id = cur.lastrowid
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, 0)", (discovery_id, author_id),
            )
            await db.commit()
        finally:
            await db.close()

        # Run the title→series pass — the standalone's title contains
        # the series name + a numeric index that the pass parses to
        # idx=4. It tries to link at (sid, 4) and hits the calibre
        # row's existing occupation of that slot. Dedup fires.
        await lookup._title_to_series_pass(author_id)

        # The Calibre row survives; the goodreads row is gone.
        db = await get_db()
        try:
            survivor = await (await db.execute(
                "SELECT id, mam_torrent_id, goodreads_id, isbn "
                "FROM books WHERE id = ?", (calibre_id,),
            )).fetchone()
            loser = await (await db.execute(
                "SELECT id FROM books WHERE id = ?", (discovery_id,),
            )).fetchone()
        finally:
            await db.close()

        assert loser is None, (
            "loser discovery row should be folded into the calibre row"
        )
        assert survivor is not None
        # The identity fields from the absorbed discovery row land
        # on the calibre row — the v2.10.2 win.
        assert survivor["mam_torrent_id"] == "mam_42", (
            "loser's mam_torrent_id should carry over via "
            "merge_books's identity coalesce"
        )
        assert survivor["goodreads_id"] == "gr_42"
        assert survivor["isbn"] == "isbn_42"

        audits = await _audit_rows()
        collision_merges = [a for a in audits
                            if a["reason"] == "series_position_collision"]
        assert len(collision_merges) == 1
        assert collision_merges[0]["winner_id"] == calibre_id
        assert collision_merges[0]["loser_id"] == discovery_id

    async def test_orphan_promotion_carries_loser_identity(
        self, discovery_db, monkeypatch,
    ):
        """Second call site: two unowned standalones cluster into a
        new series at the same index (e.g. one from Goodreads with
        `(Series #4)` parenthetical and one from another source
        with a plain numbered tail). When the second member of
        the cluster collides with the first at (sid, 4), the dedup
        fires through merge_books."""
        from app.discovery import lookup
        from app.discovery.database import get_db

        author_id = await _insert_author("Joe Author")

        # Two unowned discovery rows that the orphan-promotion pass
        # extracts as the same series at the same index. One uses
        # the (Series #N) parenthetical signal; the other doesn't
        # need to — the cluster needs ≥2 members with an index for
        # promotion to fire, so add a third companion at a
        # different index too.
        db = await get_db()
        try:
            # v3.0.0: books.author_id dropped; seed book_authors instead.
            cur = await db.execute(
                "INSERT INTO books (title, source, owned, "
                "goodreads_id) VALUES "
                "('Phantom Pack 1 (Phantom Pack #1)', 'goodreads', "
                "0, 'gr_first')",
            )
            first_id = cur.lastrowid
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, 0)", (first_id, author_id),
            )
            cur = await db.execute(
                "INSERT INTO books (title, source, owned, "
                "mam_torrent_id) VALUES "
                "('Phantom Pack 1 (Phantom Pack #1)', 'hardcover', "
                "0, 'mam_unique')",
            )
            second_id = cur.lastrowid
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, 0)", (second_id, author_id),
            )
            # Companion at idx=2 so the promotion's
            # `with_idx ≥ 2` gate passes.
            cur = await db.execute(
                "INSERT INTO books (title, source, owned) "
                "VALUES "
                "('Phantom Pack 2 (Phantom Pack #2)', 'goodreads', 0)",
            )
            companion_id = cur.lastrowid
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, 0)", (companion_id, author_id),
            )
            await db.commit()
        finally:
            await db.close()

        await lookup._orphan_series_promotion_pass(author_id)

        # One of (first_id, second_id) survives; the other is folded
        # in. By the dedup tuple (owned=0=0, no-Book-N tied, lowest
        # id wins) `first_id` keeps the slot. After merge it should
        # carry the absorbed row's unique mam_torrent_id.
        db = await get_db()
        try:
            survivor = await (await db.execute(
                "SELECT goodreads_id, mam_torrent_id FROM books "
                "WHERE id = ?", (first_id,),
            )).fetchone()
            loser = await (await db.execute(
                "SELECT id FROM books WHERE id = ?", (second_id,),
            )).fetchone()
        finally:
            await db.close()
        assert loser is None
        assert survivor is not None
        # Coalesce: first kept its own goodreads_id and absorbed
        # second's mam_torrent_id.
        assert survivor["goodreads_id"] == "gr_first"
        assert survivor["mam_torrent_id"] == "mam_unique"

        audits = await _audit_rows()
        assert any(a["reason"] == "series_position_collision"
                   for a in audits)
