# 0009. Merge unions the contributor set; prune-linkage matches by overlap

- Status: Accepted
- Date: 2026-05-27 (effective from v3.0.0 Phase 5)

## Context

[ADR-0008](0008-book-authors-authoritative-on-reads.md) made `book_authors` authoritative on read paths. Phase 5 is the write-time counterpart: the two operations that fold one book row into another must stop losing co-author links.

- **`merge_books`** (`book_merge.py`) folds a loser row into a winner, then deletes the loser. It carried zero `book_authors` awareness: `_resolve_fields` set the survivor's `author_id` to the winner's, and the loser's `book_authors` rows CASCADE-deleted with the loser — silently dropping any co-author only the loser had. `pick_winner_id` favors owned/Calibre and a precondition refuses two owned-Calibre rows, so the usual shape is **winner = owned-Calibre, loser = discovered duplicate**.
- **`transfer_linkage_before_prune`** (the "prune-linkage" helper) moves a disappearing Calibre row's MAM linkage onto an owned sibling before the row is pruned. It found that sibling by strict `WHERE author_id = ?` — so a co-authored disappearing row whose owned sibling has a *different* primary author was invisible, and its MAM linkage was lost.

The motivating pathology (J.N. Chaney + Jason Anspach co-authored books) is exactly a co-author link being dropped at merge/prune time.

## Decision

The two operations treat the contributor set **differently**, and that asymmetry is deliberate:

- **Merge UNIONS the contributor set, winner-first.** Before deleting the loser, the survivor's `book_authors` becomes winner's contributors (positions preserved, primary at 0) plus the loser's authors not already present, appended — `write_book_authors(winner, winner_ids + loser_ids)` (order-preserving dedup). Legacy `books.author_id` stays the winner's (== position 0); writes stay dual until Phase 9. Rationale: a merge asserts "these are the same book," and **neither side is authoritative over the other's authorship** — the discovered loser may have found a co-author the owned winner was missing. Union recovers it. The accepted cost: if the loser carries a *spurious* co-author, union pulls it onto the winner — bounded, because `book_authors` only holds role-filtered authors (translators/illustrators are dropped on ingest), and a stray link is manually recoverable. Never silently lose a real co-author is the priority.

- **Prune-linkage SEARCHES by overlapping contributor set, but does NOT union.** The sibling search becomes "owned Calibre book sharing ≥1 contributor via `book_authors` + title match," replacing strict `author_id =`, so co-authored rows find their sibling. But the disappearing row's authors are **not** unioned onto the survivor: the survivor is an owned Calibre row, and the row is being pruned precisely because CWA consolidated the duplicate inside Calibre — so the survivor's Calibre author tuple is the one the user kept and is **authoritative**. Unioning the stale pre-consolidation tuple would reintroduce an author the user removed. The dead row's links just CASCADE away.

- **Claim-for-owned is out of scope for Phase 5.** `write_claim_to_owned` matches an announce to an owned book by primary author and writes only MAM linkage — it touches no `book_authors`. Making its owned-book match contributor-aware is deferred; a pre-release evaluation decides whether it's a net-win for ownership validation/fill and worth its own phase.

The merge audit row (`book_merges`) additionally snapshots the loser's pre-merge `book_authors` so the union is forensically reversible.

## Consequences

- Co-authored books survive merge and prune; the Chaney/Anspach pathology is closed end-to-end (Phase 4 read/detect + Phase 5 write).
- The merge-vs-prune asymmetry (union vs no-union) is intentional and rests on "is the survivor authoritative over the other side's authorship?" — merge: no (union); prune: yes, Calibre-authoritative (don't union).
- Tests that seed books for merge/prune must seed `book_authors` (the Phase 4 fixture-migration class).
- Bounded pollution risk on merge (a spurious loser co-author), accepted in favor of never losing a real one.

## Related

- [0008](0008-book-authors-authoritative-on-reads.md) — reads; this is the write-time companion.
- [0003](0003-bundle-dedup-prefer-duplicates.md) — bundle dedup prefers duplicate children; merge no-steal preserves their contributors when children later merge.
