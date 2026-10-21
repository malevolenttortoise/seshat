"""
Unit tests for the auto-train module.
"""
from app.database import get_db
from app.orchestrator.auto_train import (
    train_author,
    train_authors_from_blob,
    coauthor_names,
    train_coauthors,
)


class TestTrainAuthor:
    async def test_adds_new_author(self, temp_db):
        db = await get_db()
        try:
            result = await train_author(db, "Brandon Sanderson")
            assert result is True

            cursor = await db.execute(
                "SELECT name, normalized, source FROM authors_allowed"
            )
            row = await cursor.fetchone()
            assert row["name"] == "Brandon Sanderson"
            assert row["normalized"] == "brandon sanderson"
            assert row["source"] == "auto_train"
        finally:
            await db.close()

    async def test_skips_existing_author(self, temp_db):
        db = await get_db()
        try:
            await train_author(db, "Brandon Sanderson")
            result = await train_author(db, "Brandon Sanderson")
            assert result is False

            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM authors_allowed"
            )
            row = await cursor.fetchone()
            assert row["cnt"] == 1
        finally:
            await db.close()

    async def test_skips_ignored_author(self, temp_db):
        db = await get_db()
        try:
            # Add to ignore list first.
            await db.execute(
                "INSERT INTO authors_ignored (name, normalized, source) "
                "VALUES (?, ?, ?)",
                ("Stephen King", "stephen king", "manual"),
            )
            await db.commit()

            result = await train_author(db, "Stephen King")
            assert result is False

            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM authors_allowed"
            )
            row = await cursor.fetchone()
            assert row["cnt"] == 0
        finally:
            await db.close()

    async def test_empty_name_skipped(self, temp_db):
        db = await get_db()
        try:
            assert await train_author(db, "") is False
            assert await train_author(db, "   ") is False
        finally:
            await db.close()

    async def test_normalizes_calibre_sort_form(self, temp_db):
        db = await get_db()
        try:
            await train_author(db, "Brandon Sanderson")
            # "Sanderson, Brandon" should match via normalization.
            result = await train_author(db, "Sanderson, Brandon")
            assert result is False
        finally:
            await db.close()

    async def test_custom_source(self, temp_db):
        db = await get_db()
        try:
            await train_author(db, "Test Author", source="calibre_sync")
            cursor = await db.execute(
                "SELECT source FROM authors_allowed WHERE normalized = ?",
                ("test author",),
            )
            row = await cursor.fetchone()
            assert row["source"] == "calibre_sync"
        finally:
            await db.close()


    async def test_lost_insert_race_leaves_no_open_transaction(
        self, temp_db, monkeypatch,
    ):
        # Another task inserts the author between our existence check and
        # our INSERT. The failed INSERT must not leave this connection
        # holding a write transaction (and SQLite's write lock).
        db = await get_db()
        other = await get_db()
        try:
            real_execute = db.execute

            async def racing_execute(sql, *args, **kwargs):
                if sql.lstrip().upper().startswith("INSERT INTO AUTHORS_ALLOWED"):
                    await other.execute(
                        "INSERT INTO authors_allowed (name, normalized, source) "
                        "VALUES ('Racer', 'racer', 'test')"
                    )
                    await other.commit()
                return await real_execute(sql, *args, **kwargs)

            monkeypatch.setattr(db, "execute", racing_execute)
            assert await train_author(db, "Racer") is False
            assert db.in_transaction is False
        finally:
            await db.close()
            await other.close()


class TestTrainAuthorsFromBlob:
    async def test_splits_and_trains(self, temp_db):
        db = await get_db()
        try:
            added = await train_authors_from_blob(
                db, "J N Chaney, Jason Anspach"
            )
            assert added == 2

            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM authors_allowed"
            )
            row = await cursor.fetchone()
            assert row["cnt"] == 2
        finally:
            await db.close()

    async def test_skips_already_trained(self, temp_db):
        db = await get_db()
        try:
            await train_authors_from_blob(db, "Author A, Author B")
            added = await train_authors_from_blob(db, "Author A, Author C")
            # Only Author C should be newly added.
            assert added == 1
        finally:
            await db.close()

    async def test_empty_blob(self, temp_db):
        db = await get_db()
        try:
            assert await train_authors_from_blob(db, "") == 0
        finally:
            await db.close()


class TestCoauthorNames:
    """v3.0.0 Phase 10 (ITEM 1) — the authoritative MAM authorlist, saved on
    the grab (S20) and trained at delivery."""

    async def test_reads_the_authoritative_authorlist(self, monkeypatch):
        import app.mam.torrent_info as ti

        class _FakeInfo:
            authors = {"1": "J.N. Chaney", "2": "Jason Anspach"}

        async def _fake_get(tid, **kw):
            return _FakeInfo()

        monkeypatch.setattr(ti, "get_torrent_info", _fake_get)
        assert await coauthor_names("12345", token="tok", fallback_blob="Ignored Fallback") == [
            "J.N. Chaney", "Jason Anspach"]

    async def test_falls_back_to_announce_blob_on_fetch_error(self, monkeypatch):
        import app.mam.torrent_info as ti

        async def _boom(tid, **kw):
            raise ti.TorrentInfoError("unavailable")

        monkeypatch.setattr(ti, "get_torrent_info", _boom)
        assert await coauthor_names("12345", token="tok", fallback_blob="Author A, Author B") == [
            "Author A", "Author B"]

    async def test_no_token_uses_fallback_blob(self):
        assert await coauthor_names("12345", token=None, fallback_blob="Solo Author") == ["Solo Author"]

    async def test_a_lookup_already_made_is_not_repeated(self, monkeypatch):
        import app.mam.torrent_info as ti

        async def _never(tid, **kw):
            raise AssertionError("fetched again")

        monkeypatch.setattr(ti, "get_torrent_info", _never)

        class _Info:
            authors = {"1": "Ann One"}

        assert await coauthor_names("1", token="tok", looked_up=True, info=_Info()) == ["Ann One"]
        assert await coauthor_names("1", token="tok", looked_up=True, info=None,
                                    fallback_blob="Blob Author") == ["Blob Author"]


class TestFourOrMoreAuthorsTrainNobody:
    """G204: a book credited to 4+ authors trains none of them."""

    async def test_coauthor_list(self, temp_db):
        db = await get_db()
        try:
            assert await train_coauthors(db, ["A One", "B Two", "C Three", "D Four"]) == 0
            assert await train_coauthors(db, ["A One", "B Two", "C Three"]) == 3
            rows = await (await db.execute(
                "SELECT source FROM authors_allowed")).fetchall()
            assert [r["source"] for r in rows] == ["coauthor_train"] * 3
        finally:
            await db.close()

    async def test_announce_blob(self, temp_db):
        db = await get_db()
        try:
            assert await train_authors_from_blob(db, "A One, B Two, C Three, D Four") == 0
            cnt = (await (await db.execute("SELECT COUNT(*) c FROM authors_allowed")).fetchone())["c"]
            assert cnt == 0
        finally:
            await db.close()
