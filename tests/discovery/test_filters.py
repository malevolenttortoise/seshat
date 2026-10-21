"""Discovery fix pass S3 — filtered books are created hidden with a reason
(ADR-0029: F4 / F6 / F7 / G197 / G198), not skipped and not listed.

The 2026-10 audit: ~2,100 of 9,272 missing rows were comics, box sets,
anthologies, foreign editions, splits, samplers, seller listings, study
guides, non-books and non-titles. Each rule was held to no false match among
the reference install's owned titles; the negative cases below are the owned
titles the first drafts caught.
"""
from __future__ import annotations

import pytest

from app.discovery import filters as F
from app.discovery.lookup import _merge_result
from app.discovery.sources.base import AuthorResult, BookResult, Contributor, SeriesResult

from tests.discovery.test_merge_title_identity import (  # noqa: F401  (fixture)
    AUTHOR, _book, _db, library,
)


# ─── The rules (pure) ────────────────────────────────────────

@pytest.mark.parametrize("title, reason", [
    # comics / manga (D2); light novels are prose (F12)
    ("Lucifer (2015-2017) #12", F.COMIC),
    ("Star Wars: Tales of the Jedi - The Golden Age #2 (of 5)", F.COMIC),
    ("X-Factor, Vol. 7:  Time and a Half", F.COMIC),
    ("X-Factor #36", F.COMIC),
    ("Watchmen (Graphic Novel)", F.COMIC),
    ("Mythos Academy Manga #1", F.COMIC),
    ("The 7th Time Loop (Light Novel) Vol. 2", None),
    ("Spider-Man 3: A Novelization", None),
    # box sets (D10)
    ("Monster Girl Tower: The Complete Collection", F.BOX_SET),
    ("Homeworld Omnibus: Books 1-3", F.BOX_SET),
    ("The Cruel Prince / The Lost Sisters / The Wicked King", F.BOX_SET),
    ("The Good Neighbors (3 book bind-up)", F.BOX_SET),
    ("Brooks Trilogy", F.BOX_SET),
    ("The Last Cyborg 3 -- Codename: ATLAS: The Thrilling Conclusion to the Last Cyborg Trilogy.", None),
    ("The Weight of Oblivion: The Land of Nowhere Book 3 (The Land of Nowhere Trilogy)", None),
    ("Jupiter War: The Owner Trilogy 3", None),
    # anthologies (D4 / D9 / F7)
    ("Galaxy's Edge Magazine: Issue 24, January 2017", F.ANTHOLOGY),
    ("The Year's Best Fantasy and Horror 2008", F.ANTHOLOGY),
    ("Gleanings: Stories From the Arc of a Scythe", F.ANTHOLOGY),
    ("The Quarterly (Volume 9, Number 2)", F.ANTHOLOGY),
    # foreign titles (F6; the language field is often empty or says English)
    ("Das Imperium", F.FOREIGN),
    ("Auf den Schwingen des Drachen", F.FOREIGN),
    ("Star Wars: Flucht ins Ungewisse", F.FOREIGN),
    ("L'Empire caché", F.FOREIGN),
    ("Red Rabbit, tome 1", F.FOREIGN),
    ("Der Werwolf: The Annals of Veight Volume 16", None),
    ("The Tides of Bára", None),
    ("Soul Harvest", None),
    ("Recruited (Para-Military Recruiter Book 5)", None),
    # splits, samplers, listings, study guides, non-books, junk (F6 / G198)
    ("Clear and Present Danger Part 2 of 2", F.SPLIT),
    ("Ender's Game Disc 4", F.SPLIT),
    ("Witch Who Came in from the Cold Season 2 Episode 13", F.SPLIT),
    ("Wild & Hunted: Episode 1", None),
    ("Dominion: Exclusive Sampler", F.SAMPLER),
    ("Hell Bent Sneak Peek", F.SAMPLER),
    ("The Teaser", None),
    ("Leviathan Falls - Carton of 10 Signed Copies", F.LISTING),
    ("Acts of War [Unabridged Audiobook]", F.LISTING),
    ("The Gathering Dark *Very Special Edition*", F.LISTING),
    ("SparkNotes Literature Guide to Ender's Game", F.STUDY_GUIDE),
    ("Paul Young Adult Coloring Book", F.NOT_A_BOOK),
    ("Marvel Multiverse Role-Playing Game: Playtest Rulebook", F.NOT_A_BOOK),
    ("Holly Black's Faerie Tarot Deck and Guidebook: From the World of Elfhame", F.NOT_A_BOOK),
    ("The Queen of Nothing 6c Signed Solid Prepack w/Enamel Pins - Indies", F.NOT_A_BOOK),
    ("Hunter Hunterson & Sons War Journal Volume One", None),
    ("Potion Master: I Got Isekai'd to an RPG & Used My Knowledge", None),
    ("AM4.Alvin Journeyman", F.JUNK),
    ("Untitled Pierce Brown 3", F.JUNK),
    ("The Expanse #3", F.JUNK),
    ("Organ Donor: Afterlife of Ebon Black #1", None),
    # real books
    ("Words of Radiance", None),
    ("The Well of Ascension: Book Two of Mistborn", None),
    # misfires found on the 2026-10-21 replays (owned titles among them)
    ("The Malevolent Seven: \"Terry Pratchett meets Deadpool\" in this darkly comic fantasy", None),
    ("Otherlife Dreams: The Selfless Hero Trilogy", None),
    ("Foundation and Chaos: The Second Foundation Trilogy (Second Foundation Trilogy Book 2)", None),
    ("Wrath of the Prophets (Star Trek: Deep Space Nine Book 20)", None),
    ("King's League: An Epic Lit RPG Adventure", None),
    ("Councilor (Para-Military Recruiter Book 12)", None),
    ("Village of Hawkshead: A GameLit/LitRPG Portal Fantasy Adventure", None),
    ("Carbon Copy: An alien AI Doppelgänger Story", None),
    ("Spider-Man: The Darkest Hours", F.COMIC),          # C23: shows under Filtered
    ("The Infernal Devices: Clockwork Princess, Chapter 18 (The Infernal Devices)", F.SPLIT),
    ("Geldnood: Waarom ons financiële systeem ons in de steek laat", F.FOREIGN),
    ("Letters To My Son: These notebooks make a unique gift", F.NOT_A_BOOK),
])
def test_the_rules(title, reason):
    assert F.filter_reason(title) == reason


def test_a_comic_issue_by_its_pages_or_its_run():
    """C24: same shape as a novel ("Organ Donor: Afterlife of Ebon Black #1",
    230 pages); 30 pages is an issue. A run named only in the series."""
    assert F.filter_reason("Red Rising: Sons of Ares #2", page_count=30) == F.COMIC
    assert F.filter_reason("Organ Donor: Afterlife of Ebon Black #1", page_count=230) is None
    assert F.filter_reason("Lucifer, Volume 1: Cold Heaven", series_name="Lucifer (2015-2017)") == F.COMIC
    # Matching reads the title alone: two sources name a run differently.
    assert F.book_class("Lucifer, Volume 1: Cold Heaven") is None


def test_a_seller_listing_names_the_author():
    assert F.filter_reason("Patriot Games by Tom Clancy", authors=["Tom Clancy"]) == F.LISTING
    assert F.filter_reason("Stand by Me", authors=["Tom Clancy"]) is None


def test_an_anthology_by_description_or_four_authors():
    assert F.filter_reason("Dead Man's Hand", description="An anthology of the weird west.") == F.ANTHOLOGY
    assert F.filter_reason("Engineering Infinity", n_authors=4) == F.ANTHOLOGY
    assert F.filter_reason("Engineering Infinity", n_authors=3) is None
    # A story's blurb naming the collection it came from isn't this book.
    assert F.filter_reason("Alpha and Omega", description=(
        "Anna is the alpha's mate. This novella was originally published in the "
        "anthology, On the Prowl.")) is None
    assert F.filter_reason("Randomize", description=(
        "Andy Weir's Randomize is part of Forward, a collection of six stories.")) is None


def test_settings_let_comics_and_box_sets_through():
    assert F.hides(F.COMIC, {}) and F.hides(F.BOX_SET, {})
    assert not F.hides(F.COMIC, {"hide_comics": False})
    assert not F.hides(F.BOX_SET, {"hide_box_sets": False})
    assert F.hides(F.SAMPLER, {"hide_comics": False})        # no setting: always
    assert not F.hides(None, {}) and not F.hides(F.SHOWN, {})


def test_a_books_class_for_matching_is_read_from_its_title():
    assert F.book_class("Lucifer (2015-2017) #1") == F.COMIC
    assert F.book_class("Lucifer") is None
    assert F.book_class("Hell Bent Sneak Peek") == F.SAMPLER
    assert F.book_class("Patriot Games by Tom Clancy", authors=["Tom Clancy"]) == F.LISTING


# ─── In the merge ────────────────────────────────────────────

def _standalone(*books: BookResult) -> AuthorResult:
    return AuthorResult(name=AUTHOR, external_id="x", books=list(books))


def _bk(title: str, **kw) -> BookResult:
    return BookResult(title=title, source="amazon", source_url=f"https://amazon/{title}", **kw)


async def _merge(author_id, result, source="amazon"):
    return await _merge_result(author_id, result, source, ["English"])


async def _all() -> dict[str, dict]:
    db = await _db()
    try:
        cur = await db.execute("SELECT id, title, hidden, hidden_reason, owned, source_url FROM books")
        return {r["title"]: dict(r) for r in await cur.fetchall()}
    finally:
        await db.close()


async def _count(title: str) -> int:
    db = await _db()
    try:
        return (await (await db.execute("SELECT COUNT(*) FROM books WHERE title = ?", (title,))).fetchone())[0]
    finally:
        await db.close()


class TestCreatedHidden:
    async def test_a_comic_is_created_hidden_with_its_reason(self, library):
        new, _ = await _merge(library, _standalone(_bk("Lucifer (2015-2017) #1"), _bk("A Real Novel")))
        rows = await _all()
        assert (rows["Lucifer (2015-2017) #1"]["hidden"], rows["Lucifer (2015-2017) #1"]["hidden_reason"]) == (1, F.COMIC)
        assert (rows["A Real Novel"]["hidden"], rows["A Real Novel"]["hidden_reason"]) == (0, None)
        assert new == 1                      # the comic isn't counted as a new missing book

    async def test_with_its_setting_off_a_comic_shows_and_keeps_its_reason(self, library, monkeypatch):
        from app.discovery import lookup
        monkeypatch.setattr(lookup, "load_settings", lambda: {"hide_comics": False})
        await _merge(library, _standalone(_bk("Lucifer (2015-2017) #1")))
        row = (await _all())["Lucifer (2015-2017) #1"]
        assert (row["hidden"], row["hidden_reason"]) == (0, F.COMIC)

    async def test_families_with_no_setting_are_always_hidden(self, library, monkeypatch):
        from app.discovery import lookup
        monkeypatch.setattr(lookup, "load_settings", lambda: {"hide_comics": False, "hide_box_sets": False})
        await _merge(library, _standalone(_bk("Hell Bent Sneak Peek"), _bk("Das Imperium")))
        rows = await _all()
        assert rows["Hell Bent Sneak Peek"]["hidden_reason"] == F.SAMPLER and rows["Hell Bent Sneak Peek"]["hidden"] == 1
        assert rows["Das Imperium"]["hidden_reason"] == F.FOREIGN and rows["Das Imperium"]["hidden"] == 1

    async def test_four_credited_authors_make_an_anthology(self, library):
        writers = [Contributor(name=n) for n in ("A One", "B Two", "C Three", "D Four")]
        await _merge(library, _standalone(_bk("Engineering Infinity", contributors=writers)))
        assert (await _all())["Engineering Infinity"]["hidden_reason"] == F.ANTHOLOGY

    async def test_an_anthology_known_by_its_authors_merges_into_its_row(self, library):
        """Matching reads titles only (C21): the existing row is this book,
        so it isn't created again, hidden beside it (F23 replay: 20 such)."""
        await _book(library, "Five by Five")
        writers = [Contributor(name=n) for n in ("A One", "B Two", "C Three", "D Four")]
        await _merge(library, _standalone(_bk("Five by Five", contributors=writers)))
        assert await _count("Five by Five") == 1
        assert (await _all())["Five by Five"]["hidden"] == 0
        # And a row created hidden that way is recognised by the next scan.
        await _merge(library, _standalone(_bk("Engineering Infinity", contributors=writers)))
        await _merge(library, _standalone(_bk("Engineering Infinity")))
        assert await _count("Engineering Infinity") == 1

    async def test_a_title_series_number_book_is_listed(self, library):
        """`_is_series_ref_title` dropped any title ending " #N" (Organ Donor,
        2026-10-20 live check)."""
        await _merge(library, _standalone(_bk("Organ Donor: Afterlife of Ebon Black #1")))
        row = (await _all())["Organ Donor: Afterlife of Ebon Black #1"]
        assert (row["hidden"], row["hidden_reason"]) == (0, None)

    async def test_a_series_number_title_is_junk_unless_the_author_owns_its_kind(self, library):
        """C22: "The Expanse #3" is a placeholder; "The Last Paladin #9" is a
        real book when the author's owned books carry that shape."""
        await _book(library, "The Last Paladin #7", owned=1)
        await _merge(library, _standalone(_bk("The Expanse #3"), _bk("The Last Paladin #9")))
        rows = await _all()
        assert rows["The Expanse #3"]["hidden_reason"] == F.JUNK
        assert (rows["The Last Paladin #9"]["hidden"], rows["The Last Paladin #9"]["hidden_reason"]) == (0, None)


class TestMatchingStaysInTheFamily:
    """G197: a hidden filtered row never absorbs the real book, and a
    filtered book never merges into a book you'd list."""

    async def test_a_hidden_comic_doesnt_swallow_the_prose_book(self, library):
        await _merge(library, _standalone(_bk("Lucifer (2015-2017) #1")))
        await _merge(library, _series("Lucifer", ("Lucifer", 1)))
        rows = await _all()
        assert rows["Lucifer"]["hidden"] == 0
        assert len(rows) == 2

    async def test_a_later_scan_recognises_the_filtered_row(self, library):
        await _merge(library, _standalone(_bk("Lucifer (2015-2017) #1")))
        await _merge(library, AuthorResult(name=AUTHOR, external_id="x", books=[
            BookResult(title="Lucifer (2015-2017) #1", source="hardcover",
                       source_url="https://hardcover/lucifer-1"),
        ]), source="hardcover")
        rows = await _all()
        assert len(rows) == 1
        assert "hardcover" in rows["Lucifer (2015-2017) #1"]["source_url"]   # URL filled, nothing new

    async def test_a_filtered_book_doesnt_merge_into_a_listed_one(self, library):
        await _book(library, "Shadow and Bone")
        await _merge(library, _standalone(_bk("Shadow and Bone: Sneak Peek")))
        rows = await _all()
        assert rows["Shadow and Bone"]["source_url"] == "{}"
        assert rows["Shadow and Bone: Sneak Peek"]["hidden_reason"] == F.SAMPLER

    async def test_an_owned_manga_matches_its_manga(self, library):
        await _book(library, "Mythos Academy Manga #1", owned=1)
        await _merge(library, _standalone(_bk("Mythos Academy Manga #1")))
        assert len(await _all()) == 1


def _series(name: str, *titles_idx) -> AuthorResult:
    return AuthorResult(name=AUTHOR, external_id="x", series=[SeriesResult(
        name=name, books=[BookResult(title=t, series_name=name, series_index=i, source="amazon",
                                     source_url=f"https://amazon/{t}") for t, i in titles_idx],
    )])


async def test_a_box_set_in_a_series_is_hidden_beside_the_books(library):
    await _merge(library, _series("Homeworld", ("Homeworld", 1), ("Homeworld Omnibus: Books 1-3", None)))
    rows = await _all()
    assert rows["Homeworld"]["hidden"] == 0
    assert (rows["Homeworld Omnibus: Books 1-3"]["hidden"], rows["Homeworld Omnibus: Books 1-3"]["hidden_reason"]) == (1, F.BOX_SET)


# ─── Settings, Show and Hide ─────────────────────────────────

@pytest.fixture
def one_library(library, monkeypatch):
    from app import state
    monkeypatch.setattr(state, "_discovered_libraries", [{"slug": "test", "content_type": "ebook"}])
    return library


async def test_turning_a_setting_off_and_on_shows_and_hides_its_rows(one_library):
    from app.discovery.filtered_rows import apply_family_settings
    await _merge(one_library, _standalone(_bk("Lucifer (2015-2017) #1"), _bk("Hell Bent Sneak Peek")))
    await _book(one_library, "Owned Manga Vol. 1 (Manga)", owned=1)

    assert await apply_family_settings({"hide_comics": False}) == {"test": 1}
    rows = await _all()
    assert rows["Lucifer (2015-2017) #1"]["hidden"] == 0
    assert rows["Hell Bent Sneak Peek"]["hidden"] == 1               # no setting: stays hidden
    assert rows["Owned Manga Vol. 1 (Manga)"]["hidden"] == 0         # owned: never touched

    await apply_family_settings({"hide_comics": True})
    assert (await _all())["Lucifer (2015-2017) #1"]["hidden"] == 1


async def test_show_sticks_and_your_own_hide_carries_no_reason(one_library):
    from app.discovery.filtered_rows import apply_family_settings
    from app.discovery.routers.books import hide, unhide
    await _merge(one_library, _standalone(_bk("Lucifer (2015-2017) #1"), _bk("A Real Novel")))
    rows = await _all()
    await unhide(rows["Lucifer (2015-2017) #1"]["id"], slug=None)
    await hide(rows["A Real Novel"]["id"], slug=None)
    rows = await _all()
    assert (rows["Lucifer (2015-2017) #1"]["hidden"], rows["Lucifer (2015-2017) #1"]["hidden_reason"]) == (0, F.SHOWN)
    assert (rows["A Real Novel"]["hidden"], rows["A Real Novel"]["hidden_reason"]) == (1, None)
    # The setting doesn't hide a row Mark showed.
    await apply_family_settings({"hide_comics": True})
    assert (await _all())["Lucifer (2015-2017) #1"]["hidden"] == 0


async def test_the_hidden_list_keeps_yours_apart(one_library):
    from app.discovery.routers.books import get_hidden, get_hidden_reasons, hide
    await _merge(one_library, _standalone(_bk("Lucifer (2015-2017) #1"), _bk("A Real Novel")))
    await hide((await _all())["A Real Novel"]["id"], slug=None)

    def titles(resp):
        return [b["title"] for b in resp["books"]]
    args = dict(search=None, sort="title", sort_dir="asc", page=1, per_page=60, content_type=None, owned=None)
    assert titles(await get_hidden(**args, filtered=True, reason=None)) == ["Lucifer (2015-2017) #1"]
    assert titles(await get_hidden(**args, filtered=False, reason=None)) == ["A Real Novel"]
    assert titles(await get_hidden(**args, filtered=None, reason=F.COMIC)) == ["Lucifer (2015-2017) #1"]
    assert (await get_hidden_reasons(content_type=None))["counts"] == {"comic": 1, "mine": 1}


async def test_a_foreign_title_is_filtered_only_for_english_readers(library):
    await _merge_result(library, _standalone(_bk("Das Imperium")), "amazon", ["German"])
    row = (await _all())["Das Imperium"]
    assert (row["hidden"], row["hidden_reason"]) == (0, None)


async def test_saving_a_filter_setting_applies_it(one_library, monkeypatch):
    from app.discovery import filtered_rows
    from app.routers import settings as settings_router
    applied = []

    async def fake_apply(settings, families=None):
        applied.append((settings.get("hide_comics"), list(families or [])))
        return {}
    monkeypatch.setattr(filtered_rows, "apply_family_settings", fake_apply)
    monkeypatch.setattr(settings_router, "_run_post_save_hooks", lambda s: _noop())
    try:
        await settings_router.apply_settings_patch({"hide_comics": False})
        assert applied == [(False, [F.COMIC])]
        applied.clear()
        await settings_router.apply_settings_patch({"verbose_logging": True})
        assert applied == []
    finally:
        # The suite shares one settings.json.
        await settings_router.apply_settings_patch({"hide_comics": True, "verbose_logging": False})


async def _noop():
    return None


async def test_your_hide_of_a_shown_filtered_row_is_yours(one_library, monkeypatch):
    """A comic the setting showed, then hidden by you: yours now, so the
    setting turning back on and off again never shows it."""
    from app.discovery import lookup
    from app.discovery.filtered_rows import apply_family_settings
    from app.discovery.routers.books import hide
    monkeypatch.setattr(lookup, "load_settings", lambda: {"hide_comics": False})
    await _merge(one_library, _standalone(_bk("Lucifer (2015-2017) #1")))
    await hide((await _all())["Lucifer (2015-2017) #1"]["id"], slug=None)
    assert (await _all())["Lucifer (2015-2017) #1"]["hidden_reason"] is None
    await apply_family_settings({"hide_comics": False})
    assert (await _all())["Lucifer (2015-2017) #1"]["hidden"] == 1


async def test_an_owned_book_is_never_hidden_by_a_setting(one_library):
    """A hidden comic you later bought keeps its reason until sync clears
    it; the setting never hides an owned book."""
    from app.discovery.filtered_rows import apply_family_settings
    await _merge(one_library, _standalone(_bk("Lucifer (2015-2017) #1")))
    db = await _db()
    try:
        await db.execute("UPDATE books SET owned = 1, hidden = 0")
        await db.commit()
    finally:
        await db.close()
    await apply_family_settings({"hide_comics": True})
    assert (await _all())["Lucifer (2015-2017) #1"]["hidden"] == 0
