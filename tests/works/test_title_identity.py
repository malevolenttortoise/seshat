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
    TitleIndex, TitleRef, author_labels, book_prefix, parse_title, same_book,
    series_in_title, series_key,
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
    ("It", None, None, "It", "Stephen King standalone", None, True, "a 2-letter title"),
    ("It", None, None, "Us", None, None, False, "two 2-letter titles"),
    # ── A shared half with the other halves disagreeing (build, 2026-10-16) ──
    ("To Love a Goddess: Spring", None, 1, "To Love a Goddess: Summer", None, 1, False,
     "two owned books sharing a head"),
    ("Old Guns: A Military Sci-Fi Adventure", None, 1,
     "Rogue Guns: A Military Sci-Fi Adventure", "Old Guns", 3, False,
     "the hyphen in 'Sci-Fi' is not a subtitle separator"),
    ("In Her Name: The Last War Trilogy", "In Her Name: The Last War", 1,
     "In Her Name: Redemption Trilogy", "In Her Name: Redemption", 1, False,
     "two owned omnibuses: 'Trilogy' tails still have to agree"),
    ("Otherlife Dreams", "The Selfless Hero Trilogy", 1,
     "Otherlife Dreams: The Selfless Hero Trilogy", "Otherlife", None, True,
     "'Title: Series Trilogy' is the book, not an omnibus (the old guard's 68 misfires)"),
    ("Rapport: Friendship, Solidarity, Communion, Empathy", None, None,
     "Rapport: Friendship, Solidarity, Communion, Empathy: A Tor Original (UAB)", None, None,
     True, "grab 4370: 'A Tor Original' is a descriptor"),
    ("Blind Man's Bluff", "Star Trek: New Frontier", 18,
     "Star Trek: New Frontier: Blind Man's Bluff", "Star Trek: New Frontier", 18, True,
     "in 'Series: Subseries: Title' the title is the last part"),
    ("Star Trek: New Frontier", None, None,
     "Star Trek: New Frontier: Blind Man's Bluff", None, None, False,
     "a sub-series name is not its book"),
    ("Star Wars: Dark Nest I: The Joiner King", "Star Wars: Dark Nest", None,
     "Star Wars: Dark Nest II: The Unseen Queen", "Star Wars: Dark Nest", None, False,
     "two books of one sub-series"),
    ("Fracture (Galaxy's Edge: Legacies Book 10)", "Galaxy's Edge: Legacies", 10,
     "Fracture", None, None, True,
     "a '(Series Book N)' number is a position; the other row gives none"),
    ("Tron", None, None, "Tron: Original Movie Adaptation", None, None, False,
     "only 'A/An … Original' is a descriptor"),
    ("Star Wars: Tales of the Jedi - Redemption (1998) #1 (of 5)", "Star Wars: Tales of the Jedi", 1,
     "Redemption", "Star Wars: Tales of the Jedi", 5, False, "'(of 5)' counts issues, it is no volume"),
    ("Supergirl: Book Two (Supergirl (1996-2003))", "Supergirl (1996-2003)", 2,
     "Supergirl", "Supergirl (1996-2003)", 4, False, "nested parentheses"),
    ("Star Wars: Tales of the Jedi - Dark Lords of the Sith 4: Death of a Dark Jedi",
     "Star Wars: Tales of the Jedi - Dark Lords of the Sith", 4,
     "Star Wars: Tales of the Jedi - The Golden Age of the Sith",
     "Star Wars: Tales of the Jedi - The Golden Age of the Sith (1996-1997)", 1, False,
     "'Tales' after a colon is not a description when a title follows"),
    ("He Who Fights with Monsters 13", None, None,
     "He Who Fights with Monsters 13: A LitRPG Adventure: He Who Fights with Monsters, Book 13",
     "He Who Fights with Monsters", 13, True, "grab 4645: description + 'Series, Book N' parts"),
    ("Apocalypse Cultivator", None, None,
     "Apocalypse Cultivator: The Complete Series: A Cultivation Novel", None, 1, True,
     "grab 3598: two descriptions in a row"),
    ("Dune: House Corrino, Vol. 2", "Dune: House Corrino", 2,
     "Dune: House Harkonnen, Volume Two", "Dune: Graphic Novel", 21, False,
     "'Title, Vol. N' as the last of two parts is the title"),
    ("Spider-Man Adventures", None, None, "Spider", None, None, False,
     "'Spider' + '-Man Adventures' is not a title and a description"),
    ("The Cruel Prince - Deleted Scenes", None, None,
     "The Wicked King - Deleted Scenes", None, None, False, "a shared tail"),
    ("Dean Koontz's Frankenstein, Book One: Prodigal Son", "Dean Koontz's Frankenstein", None,
     "Frankenstein: Prodigal Son: A Novel", "Frankenstein", 1, True,
     "a shared tail, one head inside the other"),
    ("Shadow Voices: 300 years of Irish Genre Fiction, A History in Stories", None, None,
     "Shadow Voices : 300 Years of Irish Genre Fiction", None, None, True,
     "a shared head, one tail inside the other"),
    # ── A series position written as a subtitle (F29, wave A's live check 2026-10-20) ──
    ("The Well of Ascension", None, None,
     "The Well of Ascension: Book Two of Mistborn", "The Cosmere", None, True,
     "'Title: Book N of Series' is a position (grab 4840 vs owned ABS 1269)"),
    ("Words of Radiance", None, None,
     "Words of Radiance: Book Two of the Stormlight Archive", "The Stormlight Archive", None,
     True, "'Book N of the Series', the series stored with no index"),
    ("The Anvil of Souls", None, None,
     "The Anvil of Souls: Book Two of the Forgemaster Cycle", "The Forgemaster Cycle", None,
     True, "merge duplicate 48630 / 44742"),
    ("Tomorrow's Demise: Salvation", "Tomorrow's Demise", None,
     "Tomorrow's Demise: Salvation: Book 2 of the Tomorrow's Demise Duology (Crimson Spiders)",
     "Crimson Spiders", None, True,
     "the real title is the middle part (merge duplicate 48603 / 47773)"),
    ("The Inquisition", None, None,
     "The Inquisition: Summoner Book Two", "The Summoner", 2, True,
     "'Title: Series Book N' with the book stored in that series"),
    ("Sua Sponte", None, None,
     "Sua Sponte: Forgotten Ruin, Book 9", "Forgotten Ruin", None, True,
     "'Title: Series, Book N' with the book stored in that series"),
    ("Mistborn", None, None,
     "Mistborn: Book Two of Mistborn", None, None, False,
     "no real title beside the series name: the number stays a volume"),
    ("Apocalypse", None, None,
     "Apocalypse: Regression, Book 8", "Apocalypse: Regression", 8, False,
     "the head is part of the stored series: book 8, not 'Apocalypse'"),
    ("Apocalypse: Regression", None, None,
     "Apocalypse: Regression, Book 8", "Apocalypse: Regression", 8, False,
     "the series' first book vs its book 8"),
    ("Starfall", None, None,
     "Starfall: Book Eight of Regression", "Starfall: Regression", None, False,
     "(constructed) the real title can't be a part of the stored series"),
    ("A LitRPG Adventure", None, None,
     "A LitRPG Adventure: Book Two of Dungeon Life", "Dungeon Life", None, False,
     "(constructed) a description is no real title"),
    ("Dead World", None, None,
     "Dead World: Convergence, Book 6", None, None, False,
     "'X, Book N' with no stored series to say X is a series: strict"),
    ("Red Rising", None, None,
     "Red Rising: Sons of Ares #5", "Sons of Ares", 5, False,
     "only 'Book' marks a position; '#5' stays an issue / volume"),
    ("7th Time Loop: The Villainess Enjoys a Carefree Life Married to Her Worst Enemy!", None, None,
     "7th Time Loop: The Villainess Enjoys a Carefree Life Married to Her Worst Enemy!, Vol. 3",
     "7th Time Loop: The Villainess Enjoys a Carefree Life Married to Her Worst Enemy!", 3,
     False, "'Vol. N' stays a volume"),
    ("Born to Rule", None, None,
     "Born to Rule: Rise of a Second Son 2", "Rise of a Second Son", 2, False,
     "a bare trailing number stays a volume"),
    ("The Well of Ascension: Book Two of Mistborn", None, None,
     "The Final Empire: Mistborn Book 1", None, None, False, "two positions, two titles"),
    # ── Wave B S3b (2026-10-21): residue from wave A's live check and F23 ──
    ("Witches be Crazy", "Shotgun Sorcery", 2,
     "Shotgun Sorcery 2: Witches Be Crazy, an Urban Fantasy Men's Adventure", "Shotgun Sorcery", 2,
     True, "', an … Adventure': a description after a comma (owned; F23)"),
    ("Coiled - She's at the end of her rope", None, None,
     "Coiled (Seduced and Ensnared Book 3)", "Seduced and Ensnared", 3,
     True, "' - She's …': a tagline starting with a pronoun"),
    ("Star Wars: Tales of the Jedi - The Golden Age of the Sith", None, None,
     "Star Wars: Tales of the Jedi - Redemption", None, None,
     False, "' - The …' is a title, not a tagline"),
    ("Geostorm The Shift: A Post-Apocalyptic EMP Survival Thriller", "Geostorm", 1,
     "The Shift", "Geostorm", 1, True, "series name as a prefix, same stored place"),
    ("Geostorm The Shift", "Geostorm", 1,
     "The Flood", "Geostorm", 4, False, "a prefix doesn't join two places"),
    ("Geostorm The Shift", "Geostorm", 1,
     "The Shift", "Geostorm", None, False, "the prefix needs both stored at that place"),
    ("Hulk: Visionaries - Peter David Vol. 4: Peter David - Volume 4", "Incredible Hulk (1962-1999)", 27,
     "X-Factor Visionaries: Peter David, Vol. 4", "X-Factor Visionaries", 4,
     False, "a place read from a title ('Peter David, Vol. 4') isn't a stored one"),
    ("Prelude to Dune 03 House Corrino", "Prelude to Dune", 3,
     "Dune: House Corrino", "Prelude to Dune", 3, True, "series prefix with a number"),
    ("The Voyage of Sable Keech", None, None,
     "The Voyage of the Sable Keech", "Spatterjay", 2, True, "an article inside the title"),
    ("Endgame", "Merovingen Nights", 7,
     "End Game (Fallen Empire)", "Volume", 8, False, "spaces aren't nothing: two books"),
    ("Blood & Aether", "The Scavenger Sovereign", 6,
     "The Scavenger Sovereign: Blood & Aether Book 6: A Monster Harem Litrpg Reincarnation Fantasy Adventure",
     "The Scavenger Sovereign", 6, True, "a long description as a third part"),
    ("A Ballad of Beauty and Blood: A Fantasy Romance", None, 1,
     "A Ballad of Beauty and Blood: A Fated Mates, Fallen Angel, Dark Fantasy Romance", None, 1,
     True, "two descriptions of one book (owned twice)"),
    ("Doctor Who: 12 Doctors, 12 Stories", "Doctor Who", None,
     "Doctor Who: Fifteen Doctors 15 Stories", "Doctor Who", None,
     False, "a counted subtitle is the title"),
    ("Star Trek: New Frontier", None, None,
     "Star Trek: New Frontier: Blind Man's Bluff", None, None,
     False, "a series name and its book (unchanged; 'Operation: Yellow Ribbon' stays a duplicate)"),
    # ── Wave D S26 (2026-10-23): wave C's gaps (G225–G229) and three bugs (C41) ──
    ("The School for Good and Evil #3: The Last Ever After: Now a Netflix Originals Movie",
     "The School for Good and Evil", 3, "The Last Ever After", "The School for Good and Evil", 3.0,
     True, "a marketing tail is a description (G225; owned)"),
    ("The End of Ordinary: The Humorous Science Fiction Thriller From the Author of Mickey 7", None, None,
     "The End of Ordinary", None, None, True, "'From the Author of Mickey 7': its 7 isn't a volume"),
    ("The School for Good and Evil #3: The Last Ever After: Now a Netflix Originals Movie",
     "The School for Good and Evil", 3, "A World Without Princes", "The School for Good and Evil", 2.0,
     False, "the tail doesn't join two books"),
    ("The Cannabis Preacher: Sermon Three", None, None,
     "The Cannabis Preacher, Sermon 3: A hunt for a ruthless killer, the uphill battle to rebrand a company",
     "The Cannabis Preacher", 3, True, "a spelled-out number in a subtitle (G226)"),
    ("The Cannabis Preacher: Sermon Three", None, None,
     "The Cannabis Preacher – Sermon Two: A financial thriller about resurrecting a failed company",
     "The Cannabis Preacher", 2, False, "three vs two"),
    ("The Cannabis Preacher: Sermon One: A financial thriller about building and losing the biggest Cannabis Operation",
     "The Cannabis Preacher", 1, "The Cannabis Preacher: Sermon Three", None, None,
     False, "one vs three: why 'one' counts too"),
    ("Down the Rabbit Hole | Collection 1", None, None,
     "Down the Rabbit Hole: Collection Two", None, None, False, "two collections (both owned)"),
    ("Ready Player One", None, None, "Ready Player Two", None, None, False, "a head keeps its words"),
    ("The War Ship", None, None, "The Warship", "Polity Universe: Rise of the Jain", 2,
     True, "spaces don't count when the placements agree (G227)"),
    ("Endgame", "Merovingen Nights", 7, "End Game", "Merovingen Nights", 8,
     False, "spaces count across two places of one series"),
    ("Rise of the Infernal Paladin, Book Three: Infernal Liberation (Rise of The Infernal Paladin - A LitRPG Apocalypse Adventure 3)",
     "Rise of The Infernal Paladin - A LitRPG Apocalypse Adventure", 3,
     "Rise of The Infernal Paladin: Infernal Liberation", None, None,
     True, "'Series, Book N: Title' is a position (G228)"),
    ("Rise of the Infernal Paladin, Book Two: World Eater (Rise of The Infernal Paladin - A LitRPG Apocalypse Adventure 2)",
     "Rise of The Infernal Paladin - A LitRPG Apocalypse Adventure", 2,
     "Rise of The Infernal Paladin: Infernal Liberation", None, None, False, "a position, two titles"),
    ("Zero Point: The Owner: Book Two", "Owner Trilogy", 2, "Zero Point", "Owner Trilogy", None,
     True, "'Title: Series: Book N' is a position (G228)"),
    ("Towerbound Is Not Just a Game", None, None,
     "Towerbound, Book 5: Towerbound Is Not Just a Game", "Towerbound", 5,
     True, "an owned re-grab (grab 4245)"),
    ("86—EIGHTY-SIX, Vol. 7: Mist", "86—EIGHTY-SIX", 7, "Mist", None, None,
     False, "only 'Book' is a position; 'Vol. 7' is a volume"),
    ("Turning of the Tides Daughters of the High Seas (Book 2)", "Daughters of the High Seas", 2,
     "Turning of the Tides (Daughters of the High Seas Book 2)", "Daughters of the High Seas", 2,
     True, "the stored series as a suffix (G229)"),
    ("Invasion (The Autumn War #1)", None, None,
     "The Autumn War: Volume 1: Invasion", "Pinwheel", 25,
     True, "a series named in the other title (G229; owned)"),
    ("Series: Operator Chronicles, Book 1", None, None,
     "Series: The Children of Earth, Book 1", None, None,
     False, "'Series' is never a title part (both owned)"),
    ("Roommate Control (Part Four)", None, None, "Roommate Control (Part Three)", None, None,
     False, "a parenthetical number of the title's own (C41; G213 overrode it)"),
    ("Roommate Control: A Novel", None, None, "Roommate Control (Part Four)", None, None,
     False, "the whole and a part"),
    ("NieR: Automata: Long Story Short", "Nier", None, "NieR:Automata: Short Story Long", "Nier", None,
     False, "the same words in another order (the description rule keeps 'Long Story Short')"),
    ("NieR:Automata", "Nier", None, "NieR: Automata: Long Story Short", "Nier", None,
     False, "'Long Story Short' is a title, not a '… Story' description"),
    ("Treachery", "Nightshade", 5.5,
     "Treachery: A Nightshade Novella A Penguin eSpecial from Philomel Books", "Nightshade", None,
     True, "a description goes on after its generic word ('Novella A Penguin …')"),
    ("2113", None, None, "2113: Stories Inspired by the Music of Rush", None, None,
     True, "'Stories Inspired by …' is a description"),
    ("Gridlinked", "Agent Cormac", 1,
     "Gridlinked: The thrilling first instalment of the epic Agent Cormac science fiction series", "Agent Cormac", None,
     True, "a description ending in a generic word"),
    ("Towerbound Book 5: Towerbound is Not Just a Game: LitRPG · Time Travel · Dystopia",
     "Towerbound (LITRPG + REGRESSOR + OPMC)", 6,
     "Towerbound, Book 5: Towerbound Is Not Just a Game", "Towerbound (LITRPG + REGRESSOR + OPMC)", 5,
     True, "a '·'-separated description"),
    ("Fathomless: A Cate Granger Novel 1", "Cate Granger", 1, "Fathomless", "Cate Granger", 1,
     True, "an article-led description that numbers the book"),
    ("The Star Trek: Deep Space Nine: The Siege", "Star Trek: Deep Space Nine", 2,
     "Star Trek Deep Space Nine - The Siege", "Star Trek: Deep Space Nine", 2,
     True, "a number word in an earlier part is a name (G226: 'Deep Space Nine')"),
    # ── Wave D, found reading S30's listed-copy folds (each was a wrong fold) ──
    ("Star Wars : Dark Nest III", None, None, "Star Wars : Dark Nest II", None, None,
     False, "a Roman numeral ending a subtitle is a number"),
    ("The Infernal Devices: Clockwork Princess", "The Infernal Devices", 2, "Clockwork Prince", None, None,
     False, "one different word isn't a spelling ('Prince' / 'Princess')"),
    ("NZ Frenzy: New Zealand South Island", None, None, "NZ Frenzy: New Zealand North Island", None, None,
     False, "word by word: 'South' isn't 'North'"),
    ("The Country Justice", None, None, "The Countrey Justice", None, None, True, "a misspelt word"),
    ("Alvin Jorneyman", None, None, "Alvin Journeyman", "Tales of Alvin Maker", 4, True, "a misspelt word"),
    ("Counterstrike: Offensive: Book 3", None, None, "Counter Strike", "Offensive", 3,
     True, "a space inside a word, 8+ letters (owned)"),
    ("Matt Forbeck's Brave New World: Revolution", None, None,
     "Matt Forbeck's Brave New World: Revelation", "Matt Forbeck's Brave New World", 2, False, "two books"),
    ("The World of Shannara", None, None, "The Sword of Shannara", "The Original Shannara Trilogy", 1,
     False, "'World' isn't 'Sword' (owned)"),
    ("Star Trek The Next Generation - Starfleet Academy - Worf's First Adventure", None, None,
     "I, Q: Star Trek The Next Generation", "Star Trek: Die nächste Generation", 68,
     False, "a description never spans a ' - ', and extra words in front aren't a spelling"),
    ("Star Guild : Book One - Episode One", None, None, "Star Guild: Episode Zero", None, None,
     False, "'Zero' is a number"),
    ("G.I. Joe: Tales From The Cobra Wars", None, None, "G.I. Joe: Hearts and Minds", None, None,
     False, "'Tales From …' starts a title, not a description"),
    ("Towerbound Book 5: Towerbound is Not Just a Game: LitRPG · Time Travel · Dystopia",
     "Towerbound (LITRPG + REGRESSOR + OPMC)", 6, "Towerbound is Not Just a Game", "Towerbound", 5,
     True, "a trailing description is no title part"),
    ("Space Fleet Academy: Year One (Biostellar Book 1)", "Biostellar", 1,
     "Space Fleet Academy: Year One", "Biostellar", 3,
     True, "G213 reads the titles as written, so 'Year One' stays one title"),
    ("Round One (Mixed Martial Cultivator #1)", "Mixed Martial Cultivator", 1,
     "Mixed Martial Cultivator: Round One", "Mixed Martial Cultivator", 1,
     True, "titles also compare as written (G226)"),
    ("The Malevolent Eight: The Explosive Sequel to the Massive Hit Fantasy Novel THE MALEVOLENT SEVEN",
     "The Malevolent Seven", 2, "The Malevolent Eight", "The Malevolent Seven", 2,
     True, "a description's number isn't the book's (owned)"),
    ("Omega Squad", "Omega Metahumans", 1, "Omega Apex: Omega Squad 3", None, None,
     False, "the number belongs to the other title (G229: 'Omega Squad' is a named series)"),
    # ── Wave D, Mark's sort of the cleanup preview ──
    ("Secrets of the Silent Witch",
     "サイレント・ウィッチ 沈黙の魔女の隠しごと [Silent Witch: Chinmoku no Mahou no Kakushigoto] (Light Novel)", 0,
     "Secrets of the Silent Witch, Vol. ", "Secrets of the Silent Witch (Light Novel)", 4.5,
     False, "a 'Vol.' that lost its number is some volume, not the plain-titled book"),
    ("Secrets of the Silent Witch, Vol. ", "Secrets of the Silent Witch", 3,
     "Secrets of the Silent Witch", "Secrets of the Silent Witch", 3,
     True, "… unless both are filed at one place in one series"),
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
        b = TitleRef("Star Wars Legends")
        assert same_book(a, b) is True          # the tail is b's whole title
        assert same_book(a, b, author_labels(self.TITLES)) is False

    def test_a_shared_half_needs_the_other_halves_to_agree(self):
        """Measured 2026-10-16: of 39 library pairs joined only by a shared
        head or tail, ~27 were different books."""
        assert same_book(
            TitleRef("Abyss: Star Wars Legends"), TitleRef("Crucible: Star Wars Legends"),
        ) is False


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
    assert same_book(a, TitleRef("Splashdown", "Pinwheel", 3), positions_decide=True) is False


class TestOneTitleWhateverTheNumbers:
    """G213 (2026-10-22): the same title in the same series is the same book
    when only the series numbers disagree. Wave C's cache re-merge listed
    17 owned books again: Amazon and Calibre number Halo, Pinwheel and
    "Mistborn: Secret History" differently, and the old merge left rows one
    place off."""

    @pytest.mark.parametrize("a,b", [
        (TitleRef("Outpost (Pinwheel Book 7)", "Pinwheel", 7), TitleRef("Outpost", "Pinwheel", 6)),
        (TitleRef("Halo: Hunters in the Dark", "Halo", 3), TitleRef("Halo: Hunters in the Dark", "Halo", 16)),
        (TitleRef("Mistborn: Secret History", "Mistborn", 8),
         TitleRef("Mistborn: Secret History", "The Mistborn Saga", 3.5)),
        (TitleRef("Sword of the Legion (Galaxy's Edge Book 5)", "Galaxy's Edge", 5),
         TitleRef("Sword of the Legion", "Galaxy's Edge", 6)),
        (TitleRef("Lost Souls: Sci-Fi Adventure with a sense of humor! (Get Lost Saga Book 1)", "Get Lost Saga", 1),
         TitleRef("Lost Souls", "Get Lost Saga", 2)),
    ])
    def test_one_title(self, a, b):
        assert same_book(a, b) and same_book(b, a)
        # The grab side keeps positions decisive (C35).
        assert not same_book(a, b, positions_decide=True)

    @pytest.mark.parametrize("a,b", [
        (TitleRef("Splinter Angel: Book 3"), TitleRef("Splinter Angel: Book 2")),
        (TitleRef("Halo: Evolutions, Volume II", "Halo", 7.2), TitleRef("Halo: Evolutions Volume I", "Halo", 7.1)),
        (TitleRef("Incredible Hulk Visionaries - Peter David, Vol. 3", "Incredible Hulk", 3),
         TitleRef("Incredible Hulk Visionaries - Peter David, Vol. 1", "Incredible Hulk", 1)),
        (TitleRef("JSA: Strange Adventures #4 (of 6)", "JSA: Strange Adventures", 4),
         TitleRef("JSA: Strange Adventures #5 (of 6)", "JSA: Strange Adventures", 5)),
        (TitleRef("Dune: House Harkonnen, Volume Three", "Dune: Graphic Novel", 22),
         TitleRef("Dune: House Harkonnen, Volume Two", "Dune: Graphic Novel", 21)),
        (TitleRef("High Risk: Gambit (Adrenaline Search & Rescue Book 3)", "Adrenaline Search & Rescue", 3),
         TitleRef("High Risk", "Adrenaline Search & Rescue", 1)),
        (TitleRef("Space Fleet Academy: Year Two (Biostellar Book 2)", "Biostellar", 2),
         TitleRef("Space Fleet Academy: Year One", "Biostellar", 3)),
        (TitleRef("The Dresden Files", "The Dresden Files", 1), TitleRef("The Dresden Files", "The Dresden Files", 2)),
        (TitleRef("Untitled", "X", 1), TitleRef("Untitled", "X", 2)),
    ])
    def test_numbers_of_the_title_still_decide(self, a, b):
        assert not same_book(a, b)

    def test_a_title_the_author_numbers_is_told_apart_by_its_numbers(self):
        """Mark's Calibre: four Blade Academy books all titled "Blade Angels"."""
        titles = ["Blade Angels", "Blade Angels (Blade Academy Book 2)", "Blade Angels (Blade Academy Book 3)"]
        labels = author_labels(titles)
        two = TitleRef("Blade Angels (Blade Academy Book 2)", "Blade Academy", 2)
        three = TitleRef("Blade Angels (Blade Academy Book 3)", "Blade Academy", 3)
        assert not same_book(two, three, labels)
        assert not same_book(TitleRef("Blade Angels", "Blade Academy", 1), two, labels)


def test_a_match_on_stored_series_numbers_alone():
    """The owned "Dune" (Dune #1) and "Dune: A Whisper of Caladan Seas" a
    source filed at Dune #1 are the same book only by position; an owned
    book doesn't take that row in (Calibre sync, the cleanup's fold)."""
    from app.works.title_identity import position_only_match
    dune = TitleRef("Dune", "Dune", 1)
    story = TitleRef("Dune: A Whisper of Caladan Seas", "Dune", 1)
    assert same_book(dune, story) and position_only_match(dune, story)
    # A title that writes its number, on either side, isn't position alone.
    hwfwm = TitleRef("He Who Fights with Monsters", "He Who Fights with Monsters", 1)
    assert not position_only_match(hwfwm, TitleRef("HE WHO FIGHTS WITH MONSTERS, BOOK 1: OUTWORLDER", None, None))
    assert not position_only_match(TitleRef("He Who Fights With Monsters 4", "He Who Fights with Monsters", 4),
                                   TitleRef("He Who Fights With Monsters, Book 4: Home", None, None))
    assert not position_only_match(dune, TitleRef("Dune", None, None))       # the titles match


class TestSeriesInTitle:
    """G171 / F14: the series a title names, for a source that gave none."""

    @pytest.mark.parametrize("title,expected", [
        ("Contracts & Cats (Meow: Magical Emporium of Wares #1)",
         ("Meow: Magical Emporium of Wares", 1.0)),
        ("Shadow Puppets (The Shadow Saga Book 3)", ("The Shadow Saga", 3.0)),
        ("Kingdom's Quest (Kingdom Series, Book 5)", ("Kingdom Series", 5.0)),
        ("No Quarter (Unification War Trilogy, Book 2) (Black Fleet Saga 8)",
         ("Black Fleet Saga", 8.0)),
        ("Splinter Angel: Book 3", ("Splinter Angel", 3.0)),
        ("Splashdown (Book 2)", None),                  # no series named
        ("Dragon Bound", None),
        ("Redemption (1998) #2 (of 5)", None),          # an issue count
        ("Ghost Fleet (2034)", None),                   # a year
    ])
    def test_cases(self, title, expected):
        assert series_in_title(title) == expected

    def test_a_head_ending_in_a_known_series_names_that_series(self):
        """G232: step 5 had filed this in a new series named after the whole head."""
        title = "Treachery at Martinique Isle Daughters of the High Seas: Book 1"
        known = ["Daughters of the High Seas", "Pirates"]
        assert series_in_title(title, known) == ("Daughters of the High Seas", 1.0)
        assert series_in_title(title) == ("Treachery at Martinique Isle Daughters of the High Seas", 1.0)
        # The head is itself a known series, or the ending is one word: unchanged.
        assert series_in_title("Splinter Angel: Book 3", ["Splinter Angel", "Angel"]) == ("Splinter Angel", 3.0)
        assert series_in_title("Star Wars Legends: Book 2", ["Legends"]) == ("Star Wars Legends", 2.0)


@pytest.mark.parametrize("title,expected", [
    ("Book 06.0 - Blood Rites", ("Blood Rites", 6.0)),
    ("Book 19.5 – White Wolf - Demon Accords Series", ("White Wolf - Demon Accords Series", 19.5)),
    ("Bookworm", ("Bookworm", None)),
    ("Book 3", ("Book 3", None)),
])
def test_book_prefix(title, expected):
    assert book_prefix(title) == expected


class TestTitleIndex:
    def _index(self):
        index = TitleIndex()
        for i, (t, s, n) in enumerate([
            ("Southern Storm", "Wild Wastes", 3),
            ("Monster's Mercy", "Monster's Mercy", 1),
            ("Truthwitch", "The Witchlands", 1),
            ("Rapport: Friendship, Solidarity, Communion, Empath", None, None),
        ]):
            index.add(TitleRef(t, s, n), i)
        return index

    def test_finds_what_same_book_accepts(self):
        index = self._index()
        assert index.find_all(TitleRef("Truthwitch: The Witchlands", "The Witchlands", 1)) == [2]
        assert index.find_all(TitleRef("Wild Wastes 3", "Wild Wastes", 3)) == [0]     # by position
        assert index.find_all(
            TitleRef("Rapport: Friendship, Solidarity, Communion, Empathy")) == [3]   # by spelling
        assert index.find_all(TitleRef("Monster's Mercy 3", "Monster's Mercy", 3)) == []

    def test_same_answers_as_checking_every_title(self):
        """The index is only a prefilter: on the case table it finds
        exactly what `same_book` over every title finds."""
        refs = [TitleRef(a, sa, ia) for a, sa, ia, *_ in CASES] + \
               [TitleRef(b, sb, ib) for _, _, _, b, sb, ib, *_ in CASES]
        index = TitleIndex()
        for i, r in enumerate(refs):
            index.add(r, i)
        for r in refs:
            assert index.find_all(r) == [i for i, o in enumerate(refs) if same_book(r, o)]
