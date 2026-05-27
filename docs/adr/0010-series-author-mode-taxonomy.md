# 0010. Series author-mode taxonomy (per_author / multi_author / shared)

- Status: Accepted
- Date: 2026-05-27 (effective from v3.0.0 Phase 6)

## Context

Before v3.0.0, a series was either *per-author* (`series.author_id` = an author) or *shared* (`author_id = NULL`), and `_recompute_series_author` decided between them by counting distinct **primary** `author_id` values across the series' books: 1 → per-author, 2+ → NULL/shared.

That single-author lens breaks for co-authored series. Galaxy's Edge — every book by J.N. Chaney **and** Jason Anspach — stores Chaney as each book's primary, so the recompute sees one distinct primary and calls the series per-author Chaney. Anspach, a co-author on every book, is invisible to the series: he can't be associated with it and it doesn't appear on his author detail. The NULL-means-shared convention also conflates two genuinely different things — a co-authored *team* series and an open *shared-world* series.

[ADR-0008](0008-book-authors-authoritative-on-reads.md) made `book_authors` (the full contributor set per book) authoritative on reads, so the recompute can now reason over contributor sets instead of the single primary.

## Decision

Add a `series.author_mode ∈ {per_author, multi_author, shared}` discriminator, computed from the **intersection of the per-book contributor sets**.

For a series' visible (`hidden = 0`) books, let `I` = the set of authors present in **every** book (the intersection of each book's `book_authors` set — the "owner set"):

- **per_author** — `|I| == 1`. Exactly one author is in every book; that author owns the series. Incidental guest co-authors on individual books do **not** change the mode (a Sanderson series with one guest-co-written novella stays per_author Sanderson — the guest is a contributor to one book, not a series owner).
- **multi_author** — `|I| ≥ 2`. A co-author *team* is in every book (Galaxy's Edge `{Chaney, Anspach}`). The team jointly owns it.
- **shared** — `|I| == 0`. No author is in every book (Halo — disjoint authors per book). No owner.

Keying on `|I|` (not the union, and not "all books have an identical set") is what correctly classifies the guest-novella case as per_author and a through-line author with rotating co-authors as per_author/multi_author rather than wrongly "shared."

`author_mode` is the explicit discriminator; `author_id` **coexists** as an "owner pointer" the recompute keeps consistent:

- per_author → `author_id` = the sole `I` member.
- multi_author → `author_id` = a deterministic **anchor** from `I` (the most-common primary across the series' books; tiebreak lowest `author_id`). **Non-NULL.**
- shared → `author_id` = NULL.

Keeping a non-NULL anchor for multi_author means `is_shared = (author_id IS NULL)` continues to mean *only* shared, so all existing author_id/NULL-keyed code (`promote`/`demote`, `UNIQUE(name, author_id)`, list filters) keeps working unchanged — `author_mode` is what newly distinguishes per_author from multi_author. The pre-existing graceful degradation for a `UNIQUE(name, author_id)` collision on the per-author flip (catch, log, leave authority as-is) covers the rare multi_author-anchor collision too.

Series-author association (`add_author_to_series`) validates membership by **contributor** (`book_authors`), not strict primary `author_id`, so a co-author can be added. `list_series` + author-detail flags read `author_mode` / `book_authors` rather than distinct-primary counts (closing the Phase 4 deferral that parked the `multi_author` hint on the legacy column).

## Consequences

- Co-authored series are first-class: Galaxy's Edge becomes `multi_author` with both Chaney and Anspach in `I`, so it surfaces on each owner's author detail — the motivating pathology is closed.
- An author in `I` is an **owner** (sees the full series); a contributor not in `I` is **incidental** (gets the "own entries + Shared-series badge" treatment from the v3.0.0 kickoff Decision 3). Phase 6 computes mode + owner; the owner-vs-incidental *display* is Phase 7.
- `author_mode` is backfilled for existing series at startup by running the recompute over all rows — **after** the `book_authors` backfill (the recompute now reads `book_authors`).
- The recompute moves from a single `COUNT(DISTINCT author_id)` SQL to a per-book contributor-set intersection (computed in Python; bounded — runs on membership mutation).
- The auto-flip regression suite (`test_series_authors.py`) and Series Manager tests need `book_authors` fixtures + new mode-boundary assertions.

## Related

- [0008](0008-book-authors-authoritative-on-reads.md) — `book_authors` authoritative on reads; the recompute reads contributor sets from it.
- [0009](0009-merge-union-prune-overlap.md) — write-time contributor-set semantics (merge/prune); this is the series-model counterpart.
