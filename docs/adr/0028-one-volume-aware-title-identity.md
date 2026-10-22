# 0028. One volume-aware title identity decides "same book?"

- Status: Accepted (design 2026-10-12; built in the discovery fix pass)
- Date: 2026-10-12

## Context

Seshat compared titles in five places with four different rules, and each was wrong in its own direction:

- **Scan merges** (`lookup._merge_result`) normalised with `_normalize`, which strips parentheticals, "Book N" /
  "Volume N" subtitles and any subtitle after a 3+-word head. Different volumes of one series shared a key. When the
  position guard rejected a match, the "SKIP (norm dup)" fallback dropped the book anyway; a candidate with no series
  index merged into another volume ("MERGE URL"); `SAME-SERIES-POSITION MERGE` merged two different real titles that
  two sources numbered differently; the fuzzy loop linked different series through a shared subtitle ("A Slice of
  Life Fantasy Adventure"). Replaying Amazon's cache through `_try_source` on a DB copy (2026-10-12): 314 books dropped
  as norm dups (128 absent from the library), 47 merged onto another volume, every Monster Hunter International book
  merged onto its neighbour. The Goodreads worker's 124 "existing" merges showed the same (~15–20 real books lost).
- **Claim-for-owned and format dedup** compared `match_key(author, title)`, which keeps subtitles: "Dragon Bound" ≠
  owned "Dragon Bound: A Legacy Building Fantasy" (a wedge spent).
- **Calibre-sync absorb** needed the same primary author and an exact, case-folded title.
- **The Goodreads worker's known-title check** keys a title whole and up to its first colon.

The loose rule lost books silently; the strict ones re-grabbed books Mark owns (77 of 1,946 snatched auto-grabs,
4.0%, 2026-04 → 10) and left owned books listed as missing (~55).

## Decision

One function, `app/works/title_identity.same_book`, decides whether two titles are the same book, for all five
comparisons: `_merge_result`'s match paths and its norm-dup skip, the Goodreads worker's known-title check,
Calibre-sync absorb (and its ownership flip), claim-for-owned, and format dedup's owned check. Its rules:

1. **Format first.** A manga / graphic novel / comic is never the prose book, and a dramatised / full-cast audio
   edition is never the plain one (Mark, 2026-10-16: grab 4714 "Threshing Day [Dramatized Adaptation]" was wanted beside
   the owned audiobook); "(Light Novel)" words are ignored.
2. **Numbers written in a title are compared strictly**, whatever series they name: the title proper ("Monster's
   Mercy 3", "Splinter Angel: Book 3", "13 ARC"), a series parenthetical ("(The Shadow Saga Book 3)"), a subtitle
   ("Book Two of the Legends of Dune"), Hardcover's "Book 06.0 - " prefix. Both titles carry numbers and share none →
   different. A number other than 0 or 1 on one side only must appear somewhere on the other side, unless the other
   side's only number is a series index in a differently named series. When the other side gives no index at all, the
   titles decide if this side's numbers are only "(Series Book N)" positions ("Fracture (Galaxy's Edge: Legacies Book
   10)" = "Fracture"), or if the other side names the same series; never when the title is just a series name. A
   number in the title proper stays strict ("Monster's Mercy 3" ≠ "Monster's Mercy"). "#2 (of 5)" counts issues.
3. **A `series_index` is compared only within the same normalised series name.** Hardcover's reading-order
   meta-series ("Enderverse: Publication Order" #7) never blocks an identical title.
4. **Title keys:** the whole title without parentheticals, trailing descriptions (": A … Novel / Adventure /
   LitRPG", "A Tor Original", a "Series, Book N" part after them; only a last part, so "Star Wars: Tales of the Jedi -
   Redemption" keeps its title), volume words, articles and acronym dots; "Series: Title" → Title and "Title:
   Series N" → Title when the other part is a series name (so "Tithe: A Modern Faerie Tale" is Tithe, not an omnibus).
   In a title in parts the title is the last real part ("Star Trek: New Frontier: Blind Man's Bluff"). A title part
   one author shares across 3+ different titles ("Star Wars Legends") is a label, never identity. Two titles sharing
   only one part are the same only when their other parts agree (missing, the same, or, one part each, one inside
   the other): of 39 library pairs joined by a shared half alone, ~27 were different books ("To Love a Goddess:
   Spring" / "… Summer"), and "Star Trek: New Frontier" is not its book "Blind Man's Bluff".
5. **Series position alone decides only when one side has no real title** ("Wild Wastes 3" = "Southern Storm"
   [Wild Wastes #3]). Two different real titles on one position are different books.
6. ISBN equality still wins outright.

The same parser reads "(Series Book N)" out of a title at creation (G171) and Hardcover's "Book NN.N - Title".

## Consequences

- **The bias is toward "different".** A wrong "same" loses a missing book (merge side) or a wanted volume (grab
  side), silently; a wrong "different" makes a visible duplicate (merge side) or a wasted snatch that review catches
  (grab side). Measured at design on the Amazon replay: ~102 lost books recreated, ~60 wrong merges undone, ~22–35
  new duplicates; at build (2026-10-16 copies, old merge vs new on 6,257 cached Amazon books): 104 books absent from
  the library recreated, 179 merges undone and 50 moved to the right row, 147 dropped books merged into their row,
  ~36 new duplicates; on the Goodreads worker's 188 "existing" merges: 66 created instead (~59 lost books, ~7
  duplicates of retitled books); on the grab history: all 77 owned re-grabs caught, 0 wanted volumes blocked (by
  eye). The same trade as
  [ADR-0003](0003-bundle-dedup-prefer-duplicates.md). **Don't loosen a number rule to remove a duplicate** without
  re-running both replays: the duplicates are the price of not losing books.
- The rules live in one case table (`tests/works/test_title_identity.py`); a new failure becomes a case before a fix.
  Built 2026-10-16 from the design's measured prototype, then tuned on the build's replays (Book 0, positions vs no
  index, "A … Mystery" and "A Tor Original" descriptions, dramatised editions, parts that must agree, the last part as
  the title, descriptions only as a last part, nested parentheses, issue counts). Against the prototype on 24,173
  same-author title pairs of the reference library: 265 change, 252 of them to "different" (tie-in sub-series and
  comic issues the prototype joined through a shared first or middle part, ~136 of them "Star Wars: Tales of the
  Jedi"), each read; the grab history is unchanged (77 owned re-grabs caught, no wanted volume blocked).
- The scan merge finds candidates through `TitleIndex` (a shared title word, word start or series position) and lets
  `same_book` decide; on the reference library it finds exactly what checking every title finds, and it made the
  Amazon replay ~7× faster (Peter David 5.9s → 0.5s). The "BookTitle: SeriesName ⇒ omnibus" merge guard is gone: it
  contradicted rule 4, and on the Amazon replay all 68 of its calls were single books ("Tithe: A Modern Faerie
  Tale"), which the old merge then dropped and the new one would have created as fake omnibus rows.
- Hardcover, OpenLibrary, Kobo, ibdb and Google Books keep no cache, so on 77% of the list's sources the rule is
  measured only by the first re-scan after the build.
- Not covered yet: the cross-library works matcher (`works/matcher.py`), acquisition link-back and the import check
  keep their own keys.

## Amendment (2026-10-20, wave A's live check)

- **A series position written as a subtitle is a position** (Mark, F29), like a "(Series Book N)" parenthetical
  (rule 2): when the other side gives no index, the titles decide for "Title: Book Two of Series" and "Title:
  Book 2 of the Series", and for "Title: Series Book 2" / "Title: Series, Book 2" when the book is stored in that
  series. The title must be a real part beside the subtitle (not a description, not inside the stored series), and
  only the word "Book" counts: "Vol. 3", "#5" and a bare trailing number stay volumes ("Apocalypse: Regression,
  Book 8", stored in "Apocalypse: Regression", is book 8). Found live: "The Well of Ascension" missed the owned "The
  Well of Ascension: Book Two of Mistborn" (~60 owned books carried the shape: Stormlight, Thrawn, Mistborn…), and
  scans created "The Anvil of Souls: Book Two of the Forgemaster Cycle" beside "The Anvil of Souls". Replays, old
  vs new: 3 of 25,097 same-author pairs change (all the same book), 2 Amazon results merge instead of duplicating,
  the Goodreads worker's merges and the grab history are unchanged (77 owned re-grabs caught, no wanted volume
  blocked).
- **MAM's "Dramatization/Full Cast" category marks a dramatised edition** (Mark, F28), the same as the title's
  words in rule 1: the owned check reads the announce's categories (`TitleRef.dramatised`). A full-cast upload of
  Platform Decay had been skipped as owned beside Kevin R. Free's narration.
- **Enrichment vetoes another volume or edition** (Mark, G189): rules 1–3 alone
  (`different_edition_or_volume`) stop a text-search match from merging, whatever its fuzzy score; exact-ID
  lookups (MAM by torrent ID, Audnexus by ASIN) are untouched. The score had given Hardcover's "My Dungeon Life:
  Volume 1" 1.00 for "My Dungeon Life Vol 2". The title rules stay out (the score judges titles, and rule 4's keys
  miss a description with no separator, "Aurora Rises A LitRPG Adventure"). On the 158 text-search matches merged
  2026-10-17 → 10-20 it refuses 8, all Hardcover's Volume 1 for volumes 2–9. Kobo's search uses it to pick a result.


## Amendment (2026-10-21, wave B S3b: the identity's residue)

- **Rule 4's keys also drop** a description after a comma that starts "a" / "an" (", an Urban Fantasy Men's
  Adventure": "Witches be Crazy" had been created beside the owned "Shotgun Sorcery 2: Witches Be Crazy, an
  Urban Fantasy Men's Adventure"), a " - " tagline that starts with a pronoun ("Coiled - She's at the end of her
  rope"; " - The Golden Age of the Sith" stays a title), and articles inside a title ("The Voyage of Sable Keech").
  **When both titles are stored at one place in one series**, that series' name as a prefix with no colon is
  dropped too ("Geostorm The Shift: A Post-Apocalyptic …" = "The Shift", Geostorm #1; "Prelude to Dune 03 House
  Corrino" = "Dune: House Corrino"); a place read from a title isn't enough ("Hulk: Visionaries - Peter David Vol.
  4" is not "X-Factor Visionaries: Peter David, Vol. 4").
- Tried and dropped on the measurements: spaces as nothing ("Endgame" and "End Game" are two books), commas inside
  a description ("Doctor Who: 12 Doctors, 12 Stories" would join "… Fifteen Doctors 15 Stories"), longer generic
  last parts (no pair changed). Left as visible duplicates: a short title against its three-part form
  ("Operation: Yellow Ribbon" vs "…: A Time For Peace", the shape of "Star Trek: New Frontier" vs "…: Blind
  Man's Bluff"), a title that is its series' name ("The Dragon Business").
- Measured old vs new: 21 of 1,019,235 same-author pairs change, all to "same" and all the same book; grab history
  unchanged (77 of 2,119 snatched auto-grabs stopped as owned); F23's Hardcover replay merges 8 books it had
  duplicated (5 Geostorm / Nuclear Winter, Voyage of Sable Keech, Witches be Crazy into the owned book), Amazon 11.

## Amendment (2026-10-21, wave B S6: the Calibre-sync absorb)

- **Who:** the discovery rows a Calibre book may absorb are the books any of its Calibre contributors, or an author
  linked to one (`pen_name_links`, pen name or co-author, the links a scan's dedup reads), is credited on in any
  position. Before, only the position-0 author's books counted. A pen-name split absorbs only once it's linked.
- **What:** this identity, within the book's filter family ([ADR-0029](0029-filtered-discovery-rows-kept-hidden-with-a-reason.md)).
  On the reference library the family check keeps 12 identity hits apart: signed-copy listings, French editions,
  comic issues. One match is merged into the Calibre row. Two or more are each flipped owned, as before. The
  end-of-sync legacy heal uses the same match. After an absorb merge, Calibre's own contributor list is written
  back: the merge's contributor union had credited "Rachel Caine" on Ann Aguirre's owned book until the next sync.
- Measured on a copy of the reference library (full sync, old vs new): 93 missing rows merged into the owned book
  and 26 flipped owned, against 0 for the old code. Every cell was read. About 75 are right (Atlantis University
  under Michael Todd / Anderle, Hex Kittens, Honor Among Thieves, Hardcover's "Book 06.0 - Blood Rites" rows,
  subtitle variants). The wrong ones are this identity's existing calls, which scans and the owned check already
  make (cost accepted at F10): comic and graphic-novel adaptations whose titles carry no comic word ("Ender's Game,
  Volume 1: Battle School", "Speaker for the Dead [adaptation]", "The Dresden Files: Storm Front, Vol. 1", Dune
  House volumes, "Cry Wolf #2"); "Dune" (title = series name) against two books a source filed at Dune #1; "The
  world of Shannara" read as a spelling variant of "The Sword of Shannara"; "Human Resources: Bisexual Edition"
  against the owned Hetero edition. A second sync changes nothing.

## Amendment (2026-10-22, wave C: several matches that aren't one book)

- **When a Calibre book matches two or more rows that aren't all the same book, it matches none** (G211): no merge,
  no flip. The ones above were exactly that shape (a graphic novel's Vol. 1 and Vol. 2 against the owned novel, two
  books a source filed at "Dune #1"), and flipping them owned hid real missing books and made the owned check stop
  their announces. Rows that are all one book ("Small Favour: The Dresden Files, Book Ten" and "Book 10.0 - Small
  Favor") still merge or flip. Rows that are all one book are compared in the owned book's series when a source gave
  them none ("Truthwitch: The Witchlands" and "Truthwitch: A Witchlands Novel" in The Witchlands). The missing-books
  cleanup's fold uses the same match, for Calibre and Audiobookshelf books alike.
- **For these matches a row's filter family is its stored reason first, then its title's** (G197 as ruled). A scan's
  merge keeps reading the title alone (C21, [ADR-0029](0029-filtered-discovery-rows-kept-hidden-with-a-reason.md)):
  there a refused match would create a hidden copy of the row, here it only leaves the row listed.
- Measured (full sync on a 2026-10-22 copy, old vs new): flips 26 → 8, every one of the 8 a copy of the owned book;
  the 18 no longer flipped are the graphic-novel volumes, the "Dune #1" pair, Arm of the Sphinx's two parts and three
  "Changes" rows whose stored series names make them read as different books (left listed).

## Amendment (2026-10-22, wave C S23: one title, whatever the series numbers say)

- **The same title in the same series is the same book even when the series numbers disagree** (G213): "Outpost
  (Pinwheel Book 7)" is the owned "Outpost" stored at Pinwheel #6, "Halo: Hunters in the Dark" Halo #3 the owned one at
  #16, "Mistborn: Secret History" #8 the one at #3.5. Amazon and Calibre number some series differently, and the old
  merge left rows one place off ("Sword of the Legion" at Galaxy's Edge #6), so a number clash between one real title
  had listed owned books again (17 in the cleanup's cache re-merge) and duplicated listed ones (~35).
- **What counts as one title:** the title proper (parentheticals out, descriptions stripped) is the same text, with no
  number or volume word in it (a title that names its volume, "Incredible Hulk Visionaries - Peter David, Vol. 3",
  "JSA: Strange Adventures #4", "Dune: House Harkonnen, Volume Three", is left to the number rules), not a Hardcover
  "Book NN -" title, not a placeholder or the series name alone, not one of the author's labels, and **not a title
  the author writes with two or more numbers** ("Blade Angels", "Blade Angels (Blade Academy Book 2)", "… Book 3":
  every book of the series carries one title).
- **The grab side keeps positions decisive** (C35, Claude's call): claim-for-owned, the owned skip and format dedup's
  owned check pass `positions_decide`, as a wrong "same" there skips a wanted volume as owned; MAM titles carry their
  numbers in the title itself, so G213 would rarely apply there anyway.
- Measured on 2026-10-22 copies: 1 of 1,010,366 same-author pairs changes ("System Incursion" stored at Super Genetics
  #3 and #5); grab history unchanged (77 of 2,129 snatched auto-grabs stopped, the same list); the cleanup's cache
  re-merge creates 32 fewer duplicates, every one read; a full Calibre sync is unchanged.

## Amendment (2026-10-22, wave C S24: a taken series number isn't a match)

- **Start-up deletes nothing by series number** (G215): two passes outside the merge still collapsed books sharing
  `(series_id, series_index)` on the number alone, by plain DELETE: the same-series-position dedup (every start, and
  Hygiene's "Book deduplication" pass B) and the series-index recovery from titles (on a taken number). After the
  missing-books cleanup they'd have deleted 163 + 10 rows at the next restart, among them "The Forge of God" for its
  Spanish edition at that number, "Harmonia" for "The Champions of Elan: Volume One" and "Just Over the Horizon (The
  Complete Short Fiction of Greg Bear Book 1)". The first now only scrubs blank numbers and logs owned books sharing
  one (a Calibre numbering slip, the Witcher case); the second leaves a row on a taken number as it is; Hygiene's pass
  B is gone. Owned copies fold at the Calibre sync and in the cleanup.
- **The scans' title→series passes fold only one book** (G216): when the series number a standalone's title names
  (or an orphan series promotion picks) is taken, `_resolve_position_collision` folds the two (merge_books, as before)
  only when this identity, read in that series, calls them one book of one filter family; otherwise the book is filed
  beside the other. It had folded 340 books since v2.10.2, ~3 a day; of the 154 whose winner still exists the identity
  agrees with 83 ("Remnant Book 3" into "Remnant III") and refuses 71, most of them wrong ("Six of Crows – Chapters 1
  and 2" into the owned "Crooked Kingdom", "The Stormlight Archive Boxed Set 2" into "Words of Radiance") and about a
  third copies the identity misses ("…: Now a Netflix Originals Movie"), which stay listed.
- Replayed over every author on 2026-10-22 copies: 6 folds → 1 (the one kept: "The Year's Best Fantasy 02" into "…
  Stories #2"); after the cleanup 9 → 0 (Saving A Life ×6 across its sub-series, "Hollow Earth Apocalypse: Arc 1" into
  the owned "Ashes Fall"). Missed: "Cazadores de sombras 3" (the family check) and "Turning of the Tides …(Book 2)".

## Related

- [0003](0003-bundle-dedup-prefer-duplicates.md) — the same duplicates-over-loss stance, for bundles.
- [0004](0004-format-priority-dedup.md) — format dedup's owned check now uses this identity (amended).
- [0013](0013-claim-for-owned-contributor-aware.md) — the claim's title gate is now this identity (amended).
- [0029](0029-filtered-discovery-rows-kept-hidden-with-a-reason.md) — what happens to a book the filters reject.
