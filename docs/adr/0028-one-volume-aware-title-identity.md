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
   side's only number is a series index in a differently named series, or the other side names the same series with
   no index (then the titles decide, as long as the title isn't just the series name).
3. **A `series_index` is compared only within the same normalised series name.** Hardcover's reading-order
   meta-series ("Enderverse: Publication Order" #7) never blocks an identical title.
4. **Title keys:** the whole title without parentheticals, generic subtitles (": A … Novel / Adventure / LitRPG"),
   volume words, articles and acronym dots; "Series: Title" → Title and "Title: Series N" → Title when the other part
   is a series name. A title part one author shares across 3+ different titles ("Star Wars Legends") is a label,
   never identity.
5. **Series position alone decides only when one side has no real title** ("Wild Wastes 3" = "Southern Storm"
   [Wild Wastes #3]). Two different real titles on one position are different books.
6. ISBN equality still wins outright.

The same parser reads "(Series Book N)" out of a title at creation (G171) and Hardcover's "Book NN.N - Title".

## Consequences

- **The bias is toward "different".** A wrong "same" loses a missing book (merge side) or a wanted volume (grab
  side), silently; a wrong "different" makes a visible duplicate (merge side) or a wasted snatch that review catches
  (grab side). Measured on the Amazon replay: ~102 lost books recreated, ~60 wrong merges undone, ~22–35 new
  duplicates; on the grab history: all 77 owned re-grabs caught, 0 wanted volumes blocked (by eye). The same trade as
  [ADR-0003](0003-bundle-dedup-prefer-duplicates.md). **Don't loosen a number rule to remove a duplicate** without
  re-running both replays: the duplicates are the price of not losing books.
- The rules live in one case table (`tests/works/test_title_identity.py`); a new failure becomes a case before a fix.
  Built 2026-10-16: the module agrees with the design's measured prototype on 24,173 same-author title pairs and on
  2,002 snatched auto-grabs except where the build tuned it (Book 0, same series without an index, "A … Mystery"
  subtitles, dramatised editions).
- Hardcover, OpenLibrary, Kobo, ibdb and Google Books keep no cache, so on 77% of the list's sources the rule is
  measured only by the first re-scan after the build.
- Not covered yet: the cross-library works matcher (`works/matcher.py`), acquisition link-back and the import check
  keep their own keys.

## Related

- [0003](0003-bundle-dedup-prefer-duplicates.md) — the same duplicates-over-loss stance, for bundles.
- [0004](0004-format-priority-dedup.md) — format dedup's owned check now uses this identity (amended).
- [0013](0013-claim-for-owned-contributor-aware.md) — the claim's title gate is now this identity (amended).
- [0029](0029-filtered-discovery-rows-kept-hidden-with-a-reason.md) — what happens to a book the filters reject.
