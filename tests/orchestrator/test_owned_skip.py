"""D7 / G172 — an IRC announce of a book already owned in that media type
is never grabbed (ADR-0004 and ADR-0013 amended, ADR-0028).

Before the discovery fix pass, 77 of 2,002 snatched auto-grabs were books
Mark already owned: claim-for-owned compared `match_key` (subtitles kept,
so "Dragon Bound" missed owned "Dragon Bound: A Legacy Building Fantasy",
a wedge spent) and skipped owned rows already linked to MAM by design, and
format dedup let every enabled-format announce through (grab 4691, Rhythm
of War audiobook, another wedge).

Every "not grabbed" case is proven by asserting `fetch_torrent` was never
called — never by fetching anything. The positive controls (another
volume, the other media type, a grab the user sent, active replacement
on) prove the guard doesn't over-block.
"""
from __future__ import annotations

import pytest

from app import config as app_config
from app import state
from app.database import get_db
from app.discovery import database as disco_db
from app.filter.gate import Announce
from app.orchestrator.dispatch import handle_announce, inject_grab
from app.orchestrator.hold_release import tick as hold_release_tick
from app.storage import tentative as tentative_storage
from tests.orchestrator.test_dispatch import _make_deps, _make_filter_config
from tests.orchestrator.test_snatch_guard import mam_says  # noqa: F401

EBOOKS = "calibre-library"
AUDIO = "abs-audio-library"
AUTHOR = "Eric Ugland"


@pytest.fixture
async def libs(tmp_path, monkeypatch, temp_db, mam_says):  # noqa: F811
    """An ebook and an audiobook library; MAM's search answers from
    `mam_says` (no network)."""
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    await disco_db.init_db(EBOOKS)
    await disco_db.init_db(AUDIO)
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": EBOOKS, "content_type": "ebook", "app_type": "calibre"},
        {"slug": AUDIO, "content_type": "audiobook", "app_type": "audiobookshelf"},
    ])
    yield


async def _owned(
    slug: str, title: str, *, authors: tuple[str, ...] = (AUTHOR,),
    linked_tid: str | None = None, series: str | None = None,
    index: float | None = None, owned: int = 1,
) -> int:
    """An owned (or listed) book; `linked_tid` = already linked to MAM."""
    from app.metadata.author_names import normalize_author_name

    db = await disco_db.get_db(slug)
    try:
        series_id = None
        if series:
            cur = await db.execute("INSERT INTO series (name) VALUES (?)", (series,))
            series_id = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO books (title, source, owned, hidden, mam_status, "
            "                   mam_torrent_id, series_id, series_index) "
            "VALUES (?, 'calibre', ?, 0, ?, ?, ?, ?)",
            (title, owned, "found" if linked_tid else "not_found",
             linked_tid, series_id, index),
        )
        book_id = cur.lastrowid
        for pos, name in enumerate(authors):
            await db.execute(
                "INSERT OR IGNORE INTO authors (name, sort_name, normalized_name) "
                "VALUES (?, ?, ?)",
                (name, name, normalize_author_name(name)),
            )
            aid = (await (await db.execute(
                "SELECT id FROM authors WHERE name = ?", (name,),
            )).fetchone())["id"]
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, ?)", (book_id, aid, pos),
            )
        await db.commit()
        return book_id
    finally:
        await db.close()


async def _linkage(slug: str, book_id: int) -> tuple[str, str]:
    db = await disco_db.get_db(slug)
    try:
        row = await (await db.execute(
            "SELECT mam_status, mam_torrent_id FROM books WHERE id = ?", (book_id,),
        )).fetchone()
        return row["mam_status"], row["mam_torrent_id"]
    finally:
        await db.close()


async def _decision(announce_id: int) -> tuple[str, str]:
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT decision, decision_reason FROM announces WHERE id = ?",
            (announce_id,),
        )).fetchone()
        return row["decision"], row["decision_reason"]
    finally:
        await db.close()


async def _grab_count() -> int:
    db = await get_db()
    try:
        return (await (await db.execute("SELECT COUNT(*) AS n FROM grabs")).fetchone())["n"]
    finally:
        await db.close()


def _deps():
    return _make_deps(filter_config=_make_filter_config(allowed=[AUTHOR]))


def _announce(title: str, *, tid: str = "1276245", audio: bool = False,
              author: str = AUTHOR, filetype: str | None = None) -> Announce:
    return Announce(
        torrent_id=tid,
        torrent_name=title,
        category="Audiobooks - Fantasy" if audio else "Ebooks - Fantasy",
        author_blob=author,
        filetype=filetype or ("m4b" if audio else "epub"),
    )


# ─── IRC announces of owned books: never fetched ──────────────


class TestOwnedAnnounceIsNotGrabbed:
    async def test_linked_owned_book_is_skipped(self, libs):
        """G172 hole (b): the owned row is already linked to MAM, so the
        claim never looked at it, and epub is an enabled format."""
        book = await _owned(EBOOKS, "Dragon Bound", linked_tid="1111")
        deps = _deps()

        result = await handle_announce(deps, _announce("Dragon Bound"))

        assert result.action == "skip"
        assert result.reason == "owned_same_format"
        assert deps.fetch_torrent.calls == []
        assert await _grab_count() == 0
        assert await _decision(result.announce_id) == ("skip", "owned_same_format")
        # The linkage on file is never overwritten.
        assert await _linkage(EBOOKS, book) == ("found", "1111")

    async def test_owned_audiobook_is_skipped(self, libs):
        """Grab 4691: a new upload of an owned, linked audiobook."""
        await _owned(AUDIO, "Rhythm of War", authors=("Brandon Sanderson",),
                     linked_tid="1246843", series="The Stormlight Archive", index=4)
        deps = _make_deps(filter_config=_make_filter_config(allowed=["Brandon Sanderson"]))

        result = await handle_announce(
            deps, _announce("Rhythm of War", audio=True, author="Brandon Sanderson"),
        )

        assert result.reason == "owned_same_format"
        assert deps.fetch_torrent.calls == []

    async def test_subtitle_no_longer_hides_an_owned_book(self, libs):
        """G172 hole (a), grab 4683: an unlinked owned row under a
        subtitle is now claimed instead of grabbed."""
        book = await _owned(EBOOKS, "Dragon Bound: A Legacy Building Fantasy")
        deps = _deps()

        result = await handle_announce(deps, _announce("Dragon Bound", tid="1234567"))

        assert result.reason == "claimed_for_owned"
        assert deps.fetch_torrent.calls == []
        assert await _linkage(EBOOKS, book) == ("found", "1234567")

    async def test_two_owned_matches_skip_and_claim_nothing(self, libs):
        """F24: the claim never guesses between rows, and the grab is
        skipped anyway."""
        a = await _owned(EBOOKS, "Dragon Bound")
        b = await _owned(EBOOKS, "Dragon Bound: A Legacy Building Fantasy")
        deps = _deps()

        result = await handle_announce(deps, _announce("Dragon Bound"))

        assert result.reason == "owned_same_format"
        assert deps.fetch_torrent.calls == []
        assert await _linkage(EBOOKS, a) == ("not_found", None)
        assert await _linkage(EBOOKS, b) == ("not_found", None)

    async def test_owned_as_coauthor(self, libs):
        """Contributor-aware (ADR-0013): the announce's primary author
        is the owned book's second author."""
        await _owned(EBOOKS, "Galaxy's Edge: Legionnaire",
                     authors=("J.N. Chaney", AUTHOR), linked_tid="2222")
        deps = _deps()

        result = await handle_announce(deps, _announce("Galaxy's Edge: Legionnaire"))

        assert result.reason == "owned_same_format"
        assert deps.fetch_torrent.calls == []

    async def test_disabled_format_of_an_owned_book_is_skipped(self, libs):
        """With a format priority set, an azw3 announce of an owned book
        stops at the owned skip before format dedup holds it."""
        await _owned(EBOOKS, "Dragon Bound", linked_tid="1111")
        deps = _deps()
        deps.format_priority = {"ebook": [
            {"fmt": "epub", "enabled": True}, {"fmt": "azw3", "enabled": False},
        ]}

        result = await handle_announce(deps, _announce("Dragon Bound", filetype="azw3"))

        assert result.reason == "owned_same_format"
        assert deps.fetch_torrent.calls == []


# ─── What still grabs ─────────────────────────────────────────


class TestStillGrabbed:
    async def test_another_volume_is_grabbed(self, libs):
        """Q7: owned 'Monster's Mercy' (book 1) doesn't block book 3."""
        await _owned(EBOOKS, "Monster's Mercy", linked_tid="1111")
        deps = _deps()

        result = await handle_announce(deps, _announce("Monster's Mercy 3"))

        assert result.action == "submit"
        assert len(deps.fetch_torrent.calls) == 1

    async def test_owned_in_the_other_media_type_is_grabbed(self, libs):
        """Owning the ebook doesn't stop the audiobook (and vice versa)."""
        await _owned(EBOOKS, "Dragon Bound", linked_tid="1111")
        deps = _deps()

        result = await handle_announce(deps, _announce("Dragon Bound", audio=True))

        assert result.action == "submit"
        assert len(deps.fetch_torrent.calls) == 1

    async def test_a_dramatised_edition_is_grabbed(self, libs):
        """F25 (Mark, 2026-10-16): grab 4714 'Threshing Day [Dramatized
        Adaptation]' was wanted beside the owned audiobook."""
        await _owned(AUDIO, "Threshing Day", linked_tid="1272846")
        deps = _deps()

        result = await handle_announce(
            deps, _announce("Threshing Day [Dramatized Adaptation]", audio=True),
        )

        assert result.action == "submit"
        assert len(deps.fetch_torrent.calls) == 1

    async def test_a_listed_missing_book_is_grabbed(self, libs):
        await _owned(EBOOKS, "Dragon Bound", owned=0)
        deps = _deps()

        result = await handle_announce(deps, _announce("Dragon Bound"))

        assert result.action == "submit"

    async def test_a_grab_the_user_sends_goes_through(self, libs):
        """F5: Send to pipeline / the inject API are the user's choice."""
        await _owned(EBOOKS, "Dragon Bound", linked_tid="1111")
        deps = _deps()

        result = await inject_grab(
            deps, torrent_id="1276245", torrent_name="Dragon Bound",
            category="Ebooks - Fantasy", author_blob=AUTHOR, filetype="epub",
        )

        assert result.action == "submit"
        assert len(deps.fetch_torrent.calls) == 1

    async def test_steps_aside_where_active_replacement_is_on(self, libs, monkeypatch):
        """F3: the replacement detector compares editions after a grab."""
        settings = dict(app_config.load_settings())
        settings["active_replacement_enabled_by_slug"] = {EBOOKS: True}
        monkeypatch.setattr(app_config, "load_settings", lambda: settings)
        await _owned(EBOOKS, "Dragon Bound", linked_tid="1111")
        deps = _deps()

        result = await handle_announce(deps, _announce("Dragon Bound"))

        assert result.action == "submit"
        assert len(deps.fetch_torrent.calls) == 1


# ─── IRC announces that come back later ───────────────────────


class TestHeldAnnounces:
    async def test_tentative_approve_of_an_owned_book_is_not_grabbed(
        self, libs, monkeypatch,
    ):
        """F5: a tentative author's announce is still an IRC announce."""
        from app.routers.tentative import approve

        await _owned(EBOOKS, "Dragon Bound", linked_tid="1111")
        deps = _deps()
        monkeypatch.setattr(state, "dispatcher", deps)
        db = await get_db()
        try:
            tid = await tentative_storage.upsert_tentative(
                db, mam_torrent_id="1276245", torrent_name="Dragon Bound",
                author_blob=AUTHOR, category="Ebooks - Fantasy", format="epub",
            )
        finally:
            await db.close()

        await approve(tid)

        assert deps.fetch_torrent.calls == []
        assert await _grab_count() == 0

    async def test_released_hold_of_an_owned_book_is_not_grabbed(self, libs):
        """A held lower-priority format whose book became owned during
        the hold (e.g. the epub landed in Calibre) is dropped."""
        deps = _deps()
        deps.format_priority = {"ebook": [
            {"fmt": "epub", "enabled": True}, {"fmt": "azw3", "enabled": False},
        ]}
        held = await handle_announce(deps, _announce("Dragon Bound", filetype="azw3"))
        assert held.reason == "format_dedup_hold"
        await _owned(EBOOKS, "Dragon Bound: A Legacy Building Fantasy", linked_tid="1111")
        db = await get_db()
        try:
            await db.execute("UPDATE pending_holds SET release_at = '2000-01-01 00:00:00'")
            await db.commit()
        finally:
            await db.close()

        await hold_release_tick(deps)

        assert deps.fetch_torrent.calls == []
        assert await _grab_count() == 0
        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT state, resolution_reason FROM pending_holds",
            )).fetchone()
        finally:
            await db.close()
        assert row["state"] == "dropped"
        assert "format_dedup_owned_sibling" in row["resolution_reason"]

    async def test_released_hold_whose_format_was_enabled_meanwhile(self, libs):
        """The format dedup re-check at release allows an enabled format
        without an owned rule (C4); the owned skip still runs on the
        grab it releases into."""
        deps = _deps()
        deps.format_priority = {"ebook": [
            {"fmt": "epub", "enabled": True}, {"fmt": "azw3", "enabled": False},
        ]}
        held = await handle_announce(deps, _announce("Dragon Bound", filetype="azw3"))
        assert held.reason == "format_dedup_hold"
        await _owned(EBOOKS, "Dragon Bound", linked_tid="1111")
        deps.format_priority = {"ebook": [
            {"fmt": "epub", "enabled": True}, {"fmt": "azw3", "enabled": True},
        ]}
        db = await get_db()
        try:
            await db.execute("UPDATE pending_holds SET release_at = '2000-01-01 00:00:00'")
            await db.commit()
        finally:
            await db.close()

        await hold_release_tick(deps)

        assert deps.fetch_torrent.calls == []
        assert await _grab_count() == 0
