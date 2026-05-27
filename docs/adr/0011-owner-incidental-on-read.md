# 0011. Owner-vs-incidental is computed on read, not persisted

- Status: Accepted
- Date: 2026-05-27 (effective from v3.0.0 Phase 7)

## Context

[ADR-0010](0010-series-author-mode-taxonomy.md) classifies a series by its **author mode** (`per_author` / `multi_author` / `shared`) and keeps `series.author_id` as an *owner pointer*. For a `multi_author` series that pointer is a single deterministic **anchor** (most-common primary, tiebreak lowest id) — it is explicitly *not* the full owner set `I` (the authors present in every visible book). Galaxy's Edge stores Chaney as the anchor; Anspach, an equal co-owner, is nowhere in a stored column.

Phase 7's author-detail UI needs the owner-vs-incidental distinction from the v3.0.0 kickoff Decision 3: an author in `I` is an **owner** and sees the full series; a contributor outside `I` is **incidental** and sees only their own entries plus a badge. The anchor pointer can't answer "is *this* author an owner?" — Anspach is an owner but isn't the anchor. The full owner set has to be determined some other way.

Two shapes resolve it:

- **A — compute on read.** Determine ownership when the author-detail page is built, with no stored owner set.
- **B — persist the owner set.** Add a `series_owners(series_id, author_id)` table, rewritten on every membership change (alongside the existing `author_mode` recompute), and look it up on read.

## Decision

Adopt **A**. Per-(author, series) ownership is computed **on read**, folded into the author-detail aggregate query, and never persisted.

- **Count-equality test.** An author owns a series iff they contribute to *every* visible book of it — i.e. `(count of the series' visible books this author is a contributor on) == (count of the series' visible books)`. Equal → owner (show the full series, no badge); less → incidental (show only their entries + the "N of M" pill). This is exactly ADR-0010's `author ∈ I`, expressed as two counts over `book_authors` rather than a materialised set.
- **Folded into the existing query, not N+1.** The author-detail series aggregate already groups the author's series; the owner test is one extra conditional `COUNT` per series row (covered by `idx_book_authors_author`), not a per-series round-trip.
- **No persistence.** No `series_owners` table. Ownership is derived from `book_authors` every read, so there is no second derived structure to keep in sync across the membership-mutating paths (merge, prune, sync, add-to-series) and no drift risk.
- **Documented fallback.** If production ever shows real read cost, the escape hatch is **B** — a server-side owner set invalidated on membership change, computed by the same recompute that already maintains `author_mode`. The fallback is explicitly *not* a browser/session cache, whose invalidation (on every hide/delete/merge) would be harder than the recompute it tries to avoid.

The same count-equality basis must use the **visible (`hidden = 0`)** book set, matching ADR-0010's definition of `I`, so mode and ownership never disagree.

## Consequences

- The author-detail read stays a pure function of `book_authors`; Phase 9's `books.author_id` drop touches nothing here.
- A future reader who finds only `author_mode` + a single anchor stored will understand why there is no owner-set table: ownership is intentionally a read-time derivation.
- The "incidental author sees own entries only" filter reuses the now-contributor-aware `/discovery/books?author_id=X&series_id=S` (see ADR-0008's read-path flip) rather than needing a bespoke endpoint.
- Tests that assert owner-vs-incidental must seed `book_authors` (as for every read path since ADR-0008); a book seeded with `author_id` only is invisible to the count and would mis-classify ownership.

## Related

- [0010](0010-series-author-mode-taxonomy.md) — defines `author mode` + the owner set `I`; this ADR is the read-model for membership in `I`.
- [0008](0008-book-authors-authoritative-on-reads.md) — `book_authors` authoritative on reads; the count-equality test and the own-entries filter both read it.
