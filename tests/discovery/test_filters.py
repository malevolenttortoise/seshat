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
    # G208: Goodreads' "#N-M" range (0 owned titles carry one on the 10-22 copy)
    ("Call of the Hunt (Alpha & Omega, #0.5-3)", F.BOX_SET),
    ("Snow-Walker (Snow-Walker #1-3)", F.BOX_SET),
    ("Expanse series 2 #4-6", F.BOX_SET),
    ("Dancing with the Cobra (Tales of the Carnal Adventurer #3)", None),
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
    ("Stahllied (German Edition)", F.FOREIGN),            # Amazon's marker, no foreign word
    ("L'ascesa di Senlin (Italian Edition)", F.FOREIGN),
    ("Paladino do Novo Mundo 1 (Portuguese Edition)", F.FOREIGN),
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


# ─── Wave D (2026-10-23): G233 foreign titles, G235 / G236 box sets, C43, C45 ──

@pytest.mark.parametrize("title, reason", [
    # G233: every word one no English blurb uses, or a function word list
    ("Gefallene Sonnen", F.FOREIGN),
    ("Un amore diabolico", F.FOREIGN),
    ("Vangin valtaistuin", F.FOREIGN),
    ("Meine Wiedergeburt als Schleim in einer anderen Welt Light Novel 03", F.FOREIGN),
    ("La scatola magica di Landover", F.FOREIGN),
    ("Le furie di Calderon", F.FOREIGN),
    # ...and the English titles the first drafts caught (owned or listed)
    ("Ready Player One", None),
    ("All Sin The Family (SinWhisper.com #2)", None),     # "sin" + "com" isn't Spanish + Portuguese
    ("Hopcross Jilly #2", F.JUNK),                        # invented names: another family first
    ("Neuropath", None),                                  # one unseen word isn't enough
    ("Conscientious Objector Returns", None),             # a word English blurbs use
    ("A Secondhand Spaceman", None),                      # an English function word
    ("Oathbreaker 2: Goblin's Grotto", None),             # owned: "goblin's" is "goblin"
    ("Nymph's Nirvana", None),                            # owned
    # G235: box sets the filter missed
    ("The Complete Morgaine (Morgaine Cycle Book 1)", F.BOX_SET),
    ("The Complete Wheel of Time", F.BOX_SET),
    ("The Complete Guide to Writing Science Fiction", None),
    ("Heir Chronicles: Books I-V", F.BOX_SET),
    ("The Captain's Table: Books One Through Six (Star Trek)", F.BOX_SET),
    ("The Ender Quintet", F.BOX_SET),
    ("Legends of Shannara Duology", F.BOX_SET),
    ("Galaxies and Ruins: A Sci-Fi Duology", None),          # owned
    ("A Dirge For Cascius: Part I of an Epic Space Opera Duology", None),   # owned
    # G236: a list of titles after the colon
    ("Mistborn: The Wax & Wayne Series: Alloy of Law, Shadows of Self, Bands of Mourning, The Lost Metal", F.BOX_SET),
    ("Mass Effect: The Complete Novels: Revelation, Ascension, Retribution, Deception", F.BOX_SET),
    ("Halo: The Fall of Reach, The Flood, First Strike", F.BOX_SET),
    ("Alcatraz vs. The Evil Librarians Series: (Alcatraz vs. The Evil Librarians, The Scrivener's Bones, "
     "The Knights of Crystallia, The Shattered Lens, The Dark ... (Alcatraz Versus the Evil Librarians)", F.BOX_SET),
    ("Home: Habitat, Range, Niche, Territory", None),                                     # owned
    ("Wolf & Parchment: New Theory Spice & Wolf, Vol. 7", None),                          # owned
    ("Petrodor: A Trial of Blood and Steel, Book 2", None),                               # owned
    ("Honey, I Found a Starship: Honey, I Found a Starship, Book 1", None),               # owned
    ("Aetherborn: A Paranormal Urban Fantasy Romance with Forced Proximity, Reluctant Allies, and Slow-Burn Heat", None),
    ("Monster Wrangler 3: A LitRPG Fantasy (Healer, Tamer, Builder, Book 3)", None),      # owned
    ("Faith is Earned: LitRPG, Progression Fantasy, Divine Advancement", None),
    ("Cece Loves Science: A STEM Picture Book About a Curious Girl, Her Dog, and Asking Questions", None),
    ("Flicker and the Forest Rescue: A Tale of Courage from the Sparkling Creatures Series "
     "(The Sparkling Creatures Series: Tales of Courage, Heart, and Magic)", None),   # the colon in its series
    ("The Game Master's Book of Astonishing Random Tables: 300+ Unique Roll Tables to Enhance Your "
     "Worldbuilding, Storytelling, Locations, Magic and More", None),
    ("86--EIGHTY-SIX Alter.2: Fight, Magical Girl Reina Lena! Go, Starship San Magnolia!", None),
    ("Earth Afire: Book 2 of the First Formic War by Card, Orson Scott, Johnston, Aaron", None),   # a byline, not titles
    ("Children of the Night: Stories of Ghosts, Vampires, Werewolves, and Lost Children", F.ANTHOLOGY),
    # C43
    ("You Might Be...: Word Search", F.NOT_A_BOOK),
    ("Kumo Desu ga, Nani ka? Chapter 001-050", F.SPLIT),
])
def test_wave_d_rules(title, reason):
    assert F.filter_reason(title) == reason


def test_an_authors_own_name_isnt_a_foreign_word():
    assert F.filter_reason("Inge Fredriksen") == F.FOREIGN
    assert F.filter_reason("Inge Fredriksen", authors=["Inge Fredriksen"]) is None


def test_a_foreign_blurb_decides_only_beside_an_unseen_title_word():
    """G233: Hardcover files English titles with a translation's blurb."""
    spanish = ("El joven hechicero debe enfrentarse a los enemigos de su pueblo y "
               "descubrir el secreto que su maestro le ocultó durante años para salvar el reino.")
    german = ("Der junge Held muss sich den Feinden seines Volkes stellen und das Geheimnis "
              "entdecken, das sein Meister ihm all die Jahre verschwiegen hat, um das Reich zu retten.")
    assert F.filter_reason("Hechicero", description=spanish) == F.FOREIGN
    assert F.filter_reason("Preludio a Dune", description=spanish) == F.FOREIGN
    assert F.filter_reason("The Wrath of Mulgarath", description=german) is None


def test_the_vocabulary_ships_and_a_missing_file_turns_its_test_off(monkeypatch, tmp_path):
    vocab = F.english_vocabulary()
    assert {"the", "dragon", "starship", "kingdom"} <= vocab and "gefallene" not in vocab
    monkeypatch.setattr(F, "_ENGLISH_VOCAB_FILE", tmp_path / "missing.txt")
    F.english_vocabulary.cache_clear()
    try:
        assert F.filter_reason("Gefallene Sonnen") is None
    finally:
        F.english_vocabulary.cache_clear()


def test_a_comic_adaptation_under_its_authors_name():
    """C45: "Patricia Briggs' Alpha & Omega: Cry Wolf #2" had folded into
    the owned novel; a prose tie-in carries no issue number."""
    briggs = ["Patricia Briggs"]
    assert F.filter_reason("Patricia Briggs' Alpha & Omega: Cry Wolf #2", authors=briggs) == F.COMIC
    assert F.filter_reason("Jim Butcher's The Dresden Files: Dog Men #5", authors=["Jim Butcher"]) == F.COMIC
    assert F.filter_reason("Dean Koontz's Frankenstein, Book One: Prodigal Son", authors=["Dean Koontz"]) is None
    assert F.filter_reason("Patricia Briggs' Alpha & Omega: Cry Wolf #2") is None   # whose name it is matters


def test_a_comic_issue_by_its_pages_or_its_run():
    """C24: same shape as a novel ("Organ Donor: Afterlife of Ebon Black #1",
    230 pages); 30 pages is an issue. A run named only in the series."""
    assert F.filter_reason("Red Rising: Sons of Ares #2", page_count=30) == F.COMIC
    assert F.filter_reason("Organ Donor: Afterlife of Ebon Black #1", page_count=230) is None
    assert F.filter_reason("Lucifer, Volume 1: Cold Heaven", series_name="Lucifer (2015-2017)") == F.COMIC
    # Matching reads the title alone: two sources name a run differently.
    assert F.book_class("Lucifer, Volume 1: Cold Heaven") is None


def test_a_range_with_a_year_is_no_box_set():
    """"Bst Sci Fic of Yr#2-1976" is one year's anthology, not books 2–1976."""
    assert F.filter_reason("Bst Sci Fic of Yr#2-1976") != F.BOX_SET


def test_a_comic_by_its_goodreads_genres():
    """G208 (2026-10-22): graphic novels with no comic word in the title,
    read from the page's top two genres (most-shelved first); a light novel
    genre anywhere keeps it prose."""
    assert F.filter_reason("Germ Warfare: A Very Graphic History",
                           genres=["Graphic Novels", "Science", "History"]) == F.COMIC
    assert F.filter_reason("The Good Neighbors", genres=["Graphic Novels", "Fantasy"]) == F.COMIC
    assert F.filter_reason("Campfire Cooking in Another World with My Absurd Skill: Sui's Great Adventure",
                           genres=["Manga"]) == F.COMIC
    # Third place caught a prose novel.
    assert F.filter_reason("Final Fantasy XIII-2: Fragments After",
                           genres=["Fiction", "Gaming", "Manga", "Fantasy"]) is None
    assert F.filter_reason("Campfire Cooking in Another World with My Absurd Skill: Volume 1",
                           genres=["Light Novel", "Fantasy", "Manga"]) is None
    # A light novel shelved Manga first is still prose (F12).
    assert F.filter_reason("Re:ZERO -Starting Life in Another World-, Vol. 29",
                           genres=["Manga", "Fantasy", "Light Novel"]) is None
    assert F.filter_reason("Germ Warfare: A Very Graphic History") is None
    # Matching still reads the title alone (C21).
    assert F.book_class("Germ Warfare: A Very Graphic History") is None


def test_a_seller_listing_names_the_author():
    assert F.filter_reason("Patriot Games by Tom Clancy", authors=["Tom Clancy"]) == F.LISTING
    assert F.filter_reason("Stand by Me", authors=["Tom Clancy"]) is None


def test_a_place_name_isnt_a_foreign_article():
    """Wave D F–Z sort: "Las" / "Los" in a place name English titles use."""
    for t in ("Viva, Las Vega IX", "Dirty Magick: Los Angeles", "The King of Las Vegas (Hammer Commission)"):
        assert F.filter_reason(t) is None, t
    assert F.filter_reason("Las Siete Plagas") == F.FOREIGN
    assert F.filter_reason("Las cenizas de Babilonia") == F.FOREIGN


def test_a_last_part_that_is_only_a_byline_is_a_listing():
    """Wave D: Amazon filed "Dracula : by Bram Stoker" under Holly Black;
    "By" starting a title-case subtitle is the title's own."""
    assert F.filter_reason("Dracula : by Bram Stoker", authors=["Holly Black"]) == F.LISTING
    assert F.filter_reason("Dracula: by BRAM STOKER") == F.LISTING
    assert F.filter_reason("Learn German: By Reading Urban Fantasy") is None
    assert F.filter_reason("Trading By Firelight") is None
    assert F.filter_reason("An Address To A Young Student, by Eumenes") is None


def test_an_authors_name_and_a_volume_make_a_comic():
    """C45, wave D: "Patricia Briggs' Mercy Thompson: Moon Called Vol. 1"
    is the graphic novel. Amazon's lone "(Volume 1)" tag isn't a comic's
    mark, nor is a magazine's volume."""
    briggs = ["Patricia Briggs"]
    assert F.filter_reason("Patricia Briggs' Mercy Thompson: Moon Called Vol. 1", authors=briggs) == F.COMIC
    assert F.filter_reason("Patricia Briggs' Mercy Thompson (Moon Called Volume 2)", authors=briggs) == F.COMIC
    assert F.filter_reason("Matt Forbeck's Brave New World: Revolution (Volume 1)",
                           authors=["Matt Forbeck"]) is None
    assert F.filter_reason("Isaac Asimov's Science Fiction Magazine, Volume 4, Issue 6, June 1980",
                           authors=["Isaac Asimov"]) == F.ANTHOLOGY
    assert F.filter_reason("Moon Called Vol. 1", authors=briggs) is None


def test_an_orientation_edition_is_hidden_but_matches_as_before():
    """G246: Snekguy's "Bisexual Edition" of a book Mark owns the other cut
    of is listed hidden; matching reads the title without the rule."""
    for t in ("Birds of Prey - Bisexual Edition (Pinwheel Book 20)", "Human Resources: Hetero Edition",
              "Shore Leave: Gay Edition"):
        assert F.filter_reason(t, authors=["Snekguy"]) == F.LISTING
        assert F.book_class(t, authors=["Snekguy"]) is None
    assert F.filter_reason("The Ship: Library Edition") is None
    assert F.filter_reason("Straight Up Edition Wars") is None


def test_a_series_or_episode_range_ending_a_title_is_a_box_set():
    """G254: Aer-Ki Jyr's 4-book "Origin Series" bundles."""
    for t in ("Star Force: Origin Series 13-16", "Star Force: Origin Series 97-100", "Hollow Kingdom (Episodes 1-4)",
              "Prime Cuts: Dottie For You Episodes 1-3 (Dottie For You Bundles Book 1)"):
        assert F.filter_reason(t) == F.BOX_SET, t
    assert F.filter_reason("Star Force: Origin Series 1: We found a starship, inside an alien battlefort...") is None
    assert F.filter_reason("The 1-2 Punch Series") is None


def test_a_uk_edition_is_a_listing_in_an_ebook_library():
    """G252: another cut of a book, ebook library only; matching unchanged."""
    t = "Backyard Dungeon: UK Edition 7"
    assert F.filter_reason(t) == F.LISTING
    assert F.filter_reason(t, content_type="audiobook") is None
    assert F.book_class(t) is None
    assert F.filter_reason("The Seven Stages of Drinking Martinis. (US Edition)") == F.LISTING
    assert F.filter_reason("Save Us") is None


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


@pytest.mark.parametrize("content_type,hidden", [("ebook", (1, "listing")), ("audiobook", (0, None))])
async def test_a_uk_edition_is_created_hidden_in_an_ebook_library(library, monkeypatch, content_type, hidden):
    """G252 at creation: the scan reads the active library's kind."""
    from app import state
    monkeypatch.setattr(state, "_discovered_libraries", [{"slug": "test", "content_type": content_type}])
    await _merge(library, _standalone(_bk("Backyard Dungeon: UK Edition 7")))
    row = (await _all())["Backyard Dungeon: UK Edition 7"]
    assert (row["hidden"], row["hidden_reason"]) == hidden


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
