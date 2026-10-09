"""Wave 5a S8 — every MAM content tag is kept (G124 / G125).

MAM's announce format (2026-08-11) carries several content tags per torrent
("Crime, Mystery, Thriller/Suspense"); the filter already reads them all,
but announces, tentative torrents and grabs stored only the first, in the
synthesized `category` string ("Ebooks - Crime"). Each now keeps the list
in `categories_json` beside it, and the announces / tentative / review APIs
return it.
"""
from __future__ import annotations

from app import state
from app.database import get_db, init_db
from app.filter.gate import Announce
from app.orchestrator import hold_release
from app.orchestrator.dispatch import handle_announce, inject_grab
from app.routers import announces as announces_router
from app.routers import review as review_router
from app.routers import tentative as tentative_router
from app.storage import grabs as grabs_storage
from app.storage import review_queue as review_storage
from app.storage import tentative as tentative_storage
from tests.orchestrator.test_dispatch import _make_deps
from tests.orchestrator.test_tentative_routing import _deps

TAGS = ("Fantasy", "LGBTQIA+", "Romance")


def _announce(tid: str, *, category="Ebooks - Fantasy", author="Nobody Famous"):
    return Announce(
        torrent_id=tid, torrent_name="A Book", category=category,
        author_blob=author, categories=TAGS,
    )


async def _announce_tags(tid: str):
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT categories_json FROM announces WHERE torrent_id = ? ORDER BY id DESC",
            (tid,),
        )).fetchone()
        return grabs_storage.parse_categories(row["categories_json"])
    finally:
        await db.close()


class TestWritten:
    async def test_a_tentative_keeps_every_tag(self, temp_db):
        result = await handle_announce(_deps(), _announce("9001"))
        assert result.reason == "author_not_allowlisted"

        assert await _announce_tags("9001") == list(TAGS)
        db = await get_db()
        try:
            [row] = await tentative_storage.list_tentative(db)
        finally:
            await db.close()
        assert row.categories == list(TAGS)
        assert row.category == "Ebooks - Fantasy"  # the single string stays

    async def test_a_skipped_announce_shows_why(self, temp_db):
        announce = Announce(
            torrent_id="9002", torrent_name="A Book", category="Ebooks - Romance",
            author_blob="Nobody Famous", categories=("Romance", "LGBTQIA+"),
        )
        result = await handle_announce(_deps(), announce)
        assert result.reason == "category_not_allowed"
        assert await _announce_tags("9002") == ["Romance", "LGBTQIA+"]

    async def test_an_injected_grab_carries_the_tags(self, temp_db):
        result = await inject_grab(
            _make_deps(), torrent_id="5150", torrent_name="A Book",
            category="Ebooks - Fantasy", author_blob="Someone",
            raw_line="tentative_approve:id=1", categories=list(TAGS),
        )
        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT categories_json FROM grabs WHERE id = ?", (result.grab_id,),
            )).fetchone()
        finally:
            await db.close()
        assert grabs_storage.parse_categories(row["categories_json"]) == list(TAGS)
        assert await _announce_tags("5150") == list(TAGS)

    async def test_one_known_category_stores_nothing_extra(self, temp_db):
        result = await inject_grab(_make_deps(), torrent_id="5151", category="Ebooks - Fantasy")
        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT categories_json FROM grabs WHERE id = ?", (result.grab_id,),
            )).fetchone()
        finally:
            await db.close()
        assert row["categories_json"] is None

    async def test_a_held_announce_hands_its_tags_on(self, temp_db):
        db = await get_db()
        try:
            from app.filter.gate import Decision
            aid = await grabs_storage.record_announce(
                db, raw="x", torrent_id="77", torrent_name="A Book",
                category="Ebooks - Fantasy", author_blob="A",
                decision=Decision(action="hold", reason="format_dedup_hold"),
                categories=TAGS,
            )
        finally:
            await db.close()
        assert await hold_release._announce_categories(aid) == list(TAGS)
        assert await hold_release._announce_categories(None) == []

    async def test_tentative_approve_passes_the_tags_to_the_grab(self, temp_db, monkeypatch):
        await handle_announce(_deps(), _announce("9003"))
        db = await get_db()
        try:
            [row] = await tentative_storage.list_tentative(db)
        finally:
            await db.close()
        seen = {}

        async def fake_inject(deps, **kwargs):
            seen.update(kwargs)
            from app.orchestrator.dispatch import DispatchResult
            return DispatchResult(action="submit", reason="ok", announce_id=1, grab_id=1)

        monkeypatch.setattr(tentative_router, "inject_grab", fake_inject)
        monkeypatch.setattr(state, "dispatcher", _deps())
        await tentative_router.approve(row.id, override_mam_snatched=False)

        assert list(seen["categories"]) == list(TAGS)


class TestShown:
    async def test_the_three_apis_return_the_list(self, temp_db):
        await handle_announce(_deps(), _announce("9004"))
        db = await get_db()
        try:
            gid = await grabs_storage.create_grab(
                db, announce_id=None, mam_torrent_id="9004", torrent_name="A Book",
                category="Ebooks - Fantasy", author_blob="A",
                state=grabs_storage.STATE_PROCESSING, categories=TAGS,
            )
            await review_storage.create_entry(
                db, grab_id=gid, pipeline_run_id=None, staged_path="/x",
                book_filename="a.epub", book_format="epub", metadata={},
            )
        finally:
            await db.close()

        listing = await announces_router.list_announces(
            limit=10, decision=None, reason=None, q=None,
        )
        assert listing.rows[0].categories == list(TAGS)
        assert (await tentative_router.list_pending()).items[0].categories == list(TAGS)
        assert (await review_router.list_pending()).items[0].categories == list(TAGS)


class TestBackfill:
    async def test_irc_lines_since_the_update_are_reparsed_once(self, temp_db):
        line = (
            "Do You See What I See?\x0304 By:\x0303 Peter Swanson\x0304 "
            "[\x0314English\x0304] [Ebook] [Fiction] [\x0314epub\x0304] "
            "[\x0303995.00 KiB\x0304] -\x03 Crime,  Mystery,  Thriller/Suspense"
            "\x0304 - \x0314https://www.myanonamouse.net/t/1275400\x0304 Normal"
        )
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO announces (raw, torrent_id, torrent_name, category, "
                "author_blob, decision, decision_reason) VALUES (?, '1275400', "
                "'Do You See What I See?', 'Ebooks - Crime', 'Peter Swanson', "
                "'skip', 'author_not_allowlisted')", (line,),
            )
            await db.execute(
                "INSERT INTO announces (raw, torrent_id, torrent_name, category, "
                "author_blob, decision, decision_reason) VALUES "
                "('tentative_approve:id=4', '1275400', 'x', 'Ebooks - Crime', 'P', "
                "'allow', 'manual_inject')",
            )
            await tentative_storage.upsert_tentative(
                db, mam_torrent_id="1275400", torrent_name="x", author_blob="P",
            )
            await grabs_storage.create_grab(
                db, announce_id=None, mam_torrent_id="1275400", torrent_name="x",
                category="Ebooks - Crime", author_blob="P", state="complete",
            )
        finally:
            await db.close()

        await init_db()

        want = ["Crime", "Mystery", "Thriller/Suspense"]
        db = await get_db()
        try:
            rows = await (await db.execute(
                "SELECT raw, categories_json FROM announces ORDER BY id",
            )).fetchall()
            [tentative] = await tentative_storage.list_tentative(db)
            grab = await (await db.execute(
                "SELECT categories_json FROM grabs",
            )).fetchone()
            again = await __import__("app.database", fromlist=["x"])._backfill_content_tags(db)
        finally:
            await db.close()
        assert grabs_storage.parse_categories(rows[0]["categories_json"]) == want
        assert rows[1]["categories_json"] is None  # not an IRC line
        assert tentative.categories == want
        assert grabs_storage.parse_categories(grab["categories_json"]) == want
        assert again == 0
