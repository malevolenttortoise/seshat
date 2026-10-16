"""
The title identity's case table (ADR-0028).

Every case is a real pair from the 2026-10 discovery audit, the fix-pass
design replays (Amazon's cache through `_try_source`, 1,946 snatched
auto-grabs) or the build's parity runs, with the answer Mark or the
audit settled. A new failure becomes a case here before the rule
changes. The "different" cases are load-bearing: each one is a book a
looser rule lost (merge side) or a wanted volume it would have refused
to grab (grab side). Don't flip one to remove a duplicate without
re-running the replays (`.scratch/discovery-audit-2026-10/scripts/`).
"""
from __future__ import annotations

import pytest

from app.discovery.lookup import _norm_consensus_series
from app.works.title_identity import (
    TitleRef, author_labels, parse_title, same_book, series_key,
)

# (title a, series a, index a, title b, series b, index b, same?, why)
CASES = [
    # ── Seeded by the audit (2026-10-12) ──
    ("Splashdown (Pinwheel Book 2)", None, None,
     "Splashdown", "Pinwheel", 2.0, True, "series parenthetical = stored index"),
    ("Omega Force: Revolution (OF9)", "Omega Force", 9,
     "Revolution", "Omega Force", 9.0, True, "'Series: Title' = Title"),
    ("Shadow Puppets (The Shadow Saga Book 3)", "The Shadow Saga", 3,
     "Shadow Puppets", "Enderverse: Publication Order", 7, True,
     "reading-order meta-series index never blocks an identical title"),
    ("Faith is Earned: LitRPG, Progression Fantasy, Divine Advancement (Book 1)",
     "Faith is Earned", 1,
     "Faith is Earned: LitRPG, Progression Fantasy, Divine Advancement",
     "Faith is Earned", 3, False, "same series, different index (G170)"),
    ("Monster's Mercy", None, None,
     "Monster's Mercy 3", None, None, False, "a lone title number (Q7)"),
    ("Catgirl!: Volume Four - A LitRPG Isekai Adventure", None, 4,
     "Everyone's a Catgirl!: Volume One", None, 1, False, "volume 4 vs 1"),
    ("Paths of Akashic 6: Bloodprice", "Paths of Akashic", 6,
     "Paths of Akashic 5: The Expanse", "Paths of Akashic", 6, False,
     "title numbers win over a wrong stored index"),
    ("Book 06.0 - Blood Rites", "The Dresden Files", 6,
     "Blood Rites", "The Dresden Files", 6, True, "Hardcover's 'Book NN.N - ' prefix"),
    ("Bloodwitch: A Witchlands Novel", None, None,
     "Bloodwitch", "The Witchlands", 2, True, "generic subtitle dropped"),
    ("Dune: The Machine Crusade: Book Two of the Legends of Dune", None, None,
     "Dune: The Machine Crusade", "Legends of Dune", 2, True,
     "subtitle volume names the stored series"),
    ("No Quarter (Unification War Trilogy, Book 2) (Black Fleet Saga 8)",
     "Black Fleet Saga", 8, "No Quarter", "Black Fleet Trilogy", 8, True,
     "two series parentheticals"),
    ("Splinter Angel: Book 3", None, 3,
     "Splinter Angel: Book 2", "Splinter Angel", 2, False, "Book 3 vs Book 2"),
    ("Gorgon: Alex Hunter 5", "Alex Hunter", 5,
     "Gorgon", "Alex Hunter", 5, True, "'Title: Series N' = Title"),
    ("Hexworld 3: End: A Portal Fantasy Adventure", "Hexworld", 3,
     "End", "Hexworld", 3, True, "one-word title once the volumes agree"),
    ("Dragon Bound", None, None,
     "Dragon Bound: A Legacy Building Fantasy", None, None, True,
     "grab 4683: a wedge spent on an owned book"),
    ("Respire: A Science Fiction Short Story", None, None,
     "Respire: A Short Story", None, None, True, "B5's duplicate"),
    ("Classless Outworlder 1: A LitRPG", None, None,
     "Overpowered Novice 1: A LitRPG", None, None, False,
     "a shared generic subtitle is not a title"),
    ("Isles of the Emberdark: A Cosmere Novel (Secret Projects)", "Secret Projects", None,
     "Isles of the Emberdark", "Secret Projects", 5, True, "series with no number"),
    # ── Grab history, design session (2026-10-12): wanted volumes v2 blocked ──
    ("Re:Zero - Starting Life in Another World, Volume 29", None, None,
     "Re:ZERO -Starting Life in Another World-, Vol. 28",
     "Re:Zero - Starting Life in Another World", 28, False, "Vol. 29 vs owned 28"),
    ("Aquilon: The Water Mage (Book 1)", None, None,
     "Aquilon: The Water Mage #2", "The Water Mage", 2, False, "Book 1 vs owned #2"),
    ("Born to Rule: Rise of a Second Son", None, None,
     "Born to Rule: Rise of a Second Son 2: A Kingdom Building Fantasy",
     "Rise of a Second Son", 2, False, "book 1 vs owned 2"),
    ("He Who Fights with Monsters 13 ARC", None, None,
     "He Who Fights With Monsters", "He Who Fights with Monsters", 1, False,
     "'13 ARC' reads as 13"),
    ("Blade & Bastard: Saga of the Unknown Adventurers Volume 6", None, None,
     "BLADE & BASTARD: Dungeon Chronicle Volume 4", "Blade & Bastard (2024)", 4, False,
     "Vol. 6 vs owned 4"),
    ("Ascendance of a Bookworm: Fanbook 8", None, None,
     "Ascendance of a Bookworm: Hannelore's Fifth Year at the Royal Academy Volume 2",
     "Ascendance of a Bookworm: Hannelore's Fifth Year at the Royal Academy Light Novel",
     2, False, "Fanbook 8 vs Hannelore 2"),
    # ── Grab history: owned re-grabs the audit's rule missed ──
    ("The Flood", None, None, "Halo: The Flood", "Halo", 2, True, "'Series: Title'"),
    ("Moonrise - Rise of the Bloodmoon Witch, Book 1", None, None,
     "Moonrise", "Rise of the Bloodmoon Witch", 1, True, "' - Series, Book 1'"),
    ("Crimson & Ash: A Dragon Master's Ascension", None, None,
     "Crimson & Ash", "Dragon Master's Ascension", 1, True, "series as subtitle"),
    ("House Harkonnen", None, None,
     "Dune: House Harkonnen", "Prelude to Dune", 2, True, "bare title of an owned book"),
    ("Caliban's War (The Expanse Book 2)", None, None,
     "Caliban's War", "The Expanse", 2, True, "parenthetical = stored index"),
    ("Leviathan Wakes (The Expanse Book 1)", None, None,
     "Leviathan Wakes", None, None, True, "book 1 needs no number on the other side"),
    # ── v3's soft spots, tuned at build time (2026-10-16) ──
    ("Friends Like These (Shotguns & Sorcery Book 0)", None, None,
     "Friends Like These", None, None, True, "Book 0 says nothing on its own"),
    ("Kingdom's Quest (Kingdom Series Book 5)", "Kingdom", 5,
     "Kingdom's Quest", "Kingdom", None, True,
     "same series with no index: the titles decide"),
    ("The Siren's Lament: A Magical Papillon Mystery", None, None,
     "Whispers in the Attic: A Magical Papillon Mystery", None, None, False,
     "a shared '… Mystery' subtitle is not a title"),
    # ── Build parity runs (2026-10-16) ──
    ("Changes: The Dresden Files, Book Twelve", "The Dresden Files series", None,
     "Changes: The Dresden Files", "The Dresden Files", None, True,
     "subtitle volume, series stored with no index"),
    ("Eternity's Mind: The Saga of Shadows", "The Saga of Shadows", None,
     "Eternity's Mind: The Saga of Shadows, Book Three", "The Saga of Shadows", None, True,
     "subtitle volume, series stored with no index"),
    ("Splinter Angel (Splinter Angel Book 3)", None, None,
     "Splinter Angel", "Splinter Angel", None, False,
     "the titles decide only when the title isn't the series name"),
    ("Monster's Mercy 3", None, None,
     "Monster's Mercy", "Monster's Mercy", None, False,
     "a title number isn't a series tag"),
    # ── Formats ──
    ("Threshing Day [Dramatized Adaptation]", None, None,
     "Threshing Day", None, None, False,
     "a dramatised edition is its own product (Mark, 2026-10-16, grab 4714)"),
    ("Threshing Day [Dramatized Adaptation]", None, None,
     "Threshing Day [Dramatized Adaptation]", None, None, True,
     "two dramatised copies are one (grab 4720)"),
    ("Dark Age (3 of 3) [Dramatized Adaptation]", None, None,
     "Dark Age", "Red Rising", 5, False, "the design's 'different product'"),
    ("Campfire Cooking in Another World with My Absurd Skill (MANGA) Volume 5", None, None,
     "Campfire Cooking in Another World with My Absurd Skill: Volume 5", None, None, False,
     "a manga is never the prose book"),
    ("The Saga of Tanya the Evil, Vol. 1 (light novel)", None, None,
     "The Saga of Tanya the Evil, Vol. 1", None, None, True,
     "light-novel words are ignored"),
    # ── Series position alone; unsure means different ──
    ("Wild Wastes 3", "Wild Wastes", 3,
     "Southern Storm", "Wild Wastes", 3, True, "no real title: position decides"),
    ("Turning Point (Galaxy's Edge Book 7)", "Galaxy's Edge", 7,
     "Prisoners of Darkness", "Galaxy's Edge", 7, False,
     "two real titles on one position are two books"),
    ("Rapport: Friendship, Solidarity, Communion, Empathy", None, None,
     "Rapport: Friendship, Solidarity, Communion, Empath", None, None, True,
     "a truncated title (fuzzy ≥ 0.92)"),
    ("", None, None, "", None, None, False, "nothing to compare"),
]


@pytest.mark.parametrize(
    "a,a_series,a_index,b,b_series,b_index,expected,why",
    CASES,
    ids=[f"{c[0][:40]} ~ {c[3][:30]}" for c in CASES],
)
def test_case_table(a, a_series, a_index, b, b_series, b_index, expected, why):
    left, right = TitleRef(a, a_series, a_index), TitleRef(b, b_series, b_index)
    assert same_book(left, right) is expected, why
    assert same_book(right, left) is expected, f"{why} (reversed)"


class TestLabels:
    TITLES = [
        "Crucible: Star Wars Legends", "Abyss: Star Wars Legends",
        "Tatooine Ghost: Star Wars Legends", "Dune: House Harkonnen",
        "Dune: House Harkonnen", "Dune: House Atreides",
    ]

    def test_a_part_beside_three_titles_is_a_label(self):
        assert "star wars legends" in author_labels(self.TITLES)

    def test_one_book_listed_twice_makes_no_label(self):
        assert "dune" not in author_labels(self.TITLES)

    def test_a_label_never_identifies(self):
        a = TitleRef("Abyss: Star Wars Legends")
        b = TitleRef("Crucible: Star Wars Legends")
        assert same_book(a, b) is True          # the shared tail alone
        assert same_book(a, b, author_labels(self.TITLES)) is False


class TestParseTitle:
    def test_hardcover_prefix(self):
        p = parse_title("Book 06.0 - Blood Rites")
        assert (p.proper, p.prefix_volume, p.core) == ("Blood Rites", 6.0, 6.0)

    def test_series_parenthetical(self):
        p = parse_title("Shadow Puppets (The Shadow Saga Book 3)")
        assert p.proper == "Shadow Puppets"
        assert p.tags == (("The Shadow Saga", 3.0),)
        assert p.core is None

    def test_volume_in_the_title(self):
        assert parse_title("Splinter Angel: Book 3").core == 3.0
        assert parse_title("Monster's Mercy 3").core == 3.0
        assert parse_title("He Who Fights with Monsters 13 ARC").core == 13.0

    def test_subtitle_names_its_series(self):
        p = parse_title("Dune: The Machine Crusade: Book Two of the Legends of Dune")
        assert ("Legends of Dune", 2.0) in p.tags

    def test_a_year_is_not_a_volume(self):
        assert parse_title("Ghost Fleet 2034").core is None


@pytest.mark.parametrize("name", [
    "The Stormlight Archive", "Mistborn Saga Series", "86--EIGHTY-SIX (Light Novel)",
    "Enderverse: Publication Order", "The Dresden Files", "", None,
])
def test_series_key_is_series_consensus_normalisation(name):
    """`series_key` copies `lookup._norm_consensus_series`; they must agree."""
    assert series_key(name) == _norm_consensus_series(name)


def test_index_may_come_as_text_or_int():
    a = TitleRef("Splashdown (Pinwheel Book 2)")
    assert same_book(a, TitleRef("Splashdown", "Pinwheel", "2")) is True
    assert same_book(a, TitleRef("Splashdown", "Pinwheel", 3)) is False
