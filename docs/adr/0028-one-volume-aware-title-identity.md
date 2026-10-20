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

## Related

- [0003](0003-bundle-dedup-prefer-duplicates.md) — the same duplicates-over-loss stance, for bundles.
- [0004](0004-format-priority-dedup.md) — format dedup's owned check now uses this identity (amended).
- [0013](0013-claim-for-owned-contributor-aware.md) — the claim's title gate is now this identity (amended).
- [0029](0029-filtered-discovery-rows-kept-hidden-with-a-reason.md) — what happens to a book the filters reject.
