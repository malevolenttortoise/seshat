# 0013. Claim-for-owned is contributor-aware (announce-primary × owned-any-contributor)

- Status: Accepted
- Date: 2026-05-28 (effective from v3.0.0 Phase 10)

## Context

**Claim-for-owned** (`owned_announce_claim.py`) runs on the reactive announce hot path: when a freshly-announced MAM torrent matches a book the user already owns but whose owned row has no confirmed MAM linkage (`mam_status != 'found'`), it writes the MAM URL/torrent_id onto the owned row in place and skips the grab — gaining the linkage without paying buffer ratio for a redundant snatch.

Through v3.0.0 Phase 9 the match was **primary-only on both sides**: the announce's primary author (`author_blob.split(",")[0]`) had to equal the owned book's primary author (`book_authors` position 0), and the title `dedup_key` (`match_key(first_author, title)`) folds in that same primary. So a co-authored book whose announce lists a *different* author first than the owned row's stored primary (co-author ordering differs) failed to match.

[ADR-0008](0008-book-authors-authoritative-on-reads.md) made a book **owned for every one of its contributors**, not just its primary. Primary-only claim-for-owned was the last ownership check still inconsistent with that premise.

The current gap **fails safe**: a missed claim just means the autograbber grabs a duplicate that lands in the review queue, where the existing duplicate-banner (also backed by `find_owned_matches`) surfaces it for one-click resolution. Making the match contributor-aware closes the gap but introduces a **fail-unsafe** possibility: a single wrong match writes a `'found'` MAM linkage onto the wrong owned book, and the existing 2+-match ambiguity gate does not catch a *lone* wrong match. This ADR records the deliberate decision to accept that trade.

## Decision

Make claim-for-owned **contributor-aware**, with a bounded, asymmetric shape:

- **Match the announce PRIMARY author against the owned book's ANY contributor** (`book_authors`, any position) — not primary-against-primary. Handles the motivating case (an Anspach-primary announce claiming a Chaney-primary owned book that Anspach co-wrote).
- **Title still gates**: recompute the owned-side `dedup_key` against the *matched* author (the announce primary) rather than the owned row's stored primary, so the key comparison reduces to a canonical title match once the author is confirmed a contributor.
- **Keep the exactly-1-match ambiguity gate** (2+ owned rows match → bail rather than guess) and the owned-row gate (`owned=1 AND hidden=0 AND mam_status != 'found'`).
- **No `torrent_info` fetch.** Claim-for-owned runs per *announce* (far more frequent than grabs), so it uses only the announce-provided `author_blob`; a per-announce MAM call would be an unacceptable economy cost. (Contrast ITEM 1's autotrain, which fetches `author_info` once per *grab*.)

Asymmetric (announce-primary, not the full announce authorlist) is the deliberate sweet spot: the announce primary is the most reliable signal, while widening the announce side to all announce authors × all owned contributors would enlarge the wrong-claim surface without proportionate benefit.

## Consequences

- Co-author-ordering re-grabs are now claimed in place instead of snatched as duplicates — consistent with ADR-0008's owned-for-every-contributor model.
- Accepted risk: a lone wrong match can write a `'found'` linkage onto the wrong owned book (active mis-linkage), which the ambiguity gate won't catch. Bounded by the canonical title gate + the owned-row gate + the announce-primary-only (not full-list) match. A future reader must NOT revert this to primary-only "for safety" without knowing the fail-unsafe trade was deliberate.
- Hot path stays free of new MAM calls (no `torrent_info` fetch in the claim path).
- `find_owned_matches` is shared with the review-queue duplicate banner; both surfaces become contributor-aware together.

## Related

- [0008](0008-book-authors-authoritative-on-reads.md) — owned for every contributor; this extends that to the claim path.
- [0009](0009-merge-union-prune-overlap.md) — write-time contributor-set semantics (merge/prune); claim-for-owned is the announce-time companion.
- [0012](0012-drop-books-author-id-position-0-canonical.md) — position 0 = primary; the owned-side match reads contributors from `book_authors`.
