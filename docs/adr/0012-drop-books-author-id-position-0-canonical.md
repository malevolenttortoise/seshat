# 0012. Drop `books.author_id`; position 0 is the sole canonical primary author

- Status: Accepted
- Date: 2026-05-28 (effective from v3.0.0 Phase 9)

## Context

The v3.0.0 multi-author rework replaced the single `books.author_id` column with the `book_authors(book_id, author_id, position, role)` join. [ADR-0008](0008-book-authors-authoritative-on-reads.md) made `book_authors` authoritative on **reads** from Phase 4, kept **writes dual** (`author_id` *and* `book_authors`) until the column is dropped, and predicted that Phase 9's drop would be **"a no-op on reads."**

That prediction was wrong. A code audit at Phase 9 kickoff found ~40 live `b.author_id` read sites still on the legacy column — the "display-only author-name joins" ADR-0008 itself deferred to "Phase 7/9." Two distinct shapes:

- **Display joins** (the majority): `JOIN authors a ON b.author_id = a.id` producing each book row's single `author_name`. Phase 4 flipped the *membership filter* to `book_authors` but explicitly left the primary-name join on `author_id` (comment at `authors.py:345-348`); Phase 7 layered the multi-author byline (`attach_contributors`) on top without removing it.
- **Author-scoped filters / bulk writes**: `WHERE b.author_id IN/= …` meaning "books by author X" (the owned-series enrichment hint at `lookup.py:3329`, the needs-scan collectors at `authors.py:2587` / `mam.py:862`, and author-level bulk resets).

So dropping the column is **not** a read no-op: every one of those sites breaks unless re-pointed, and the column sits behind a table-level FK plus two indexes (`idx_books_author`, `idx_books_author_owned`), so SQLite's `ALTER TABLE … DROP COLUMN` refuses it outright — a table rebuild is forced.

A backfill invariant makes the re-point safe: `_backfill_book_authors` (database.py) links a position-0 row for **every book with a non-NULL `author_id`**, and a book with NULL `author_id` gets no row — exactly the set today's inner `b.author_id = a.id` join already drops. So an inner join through `book_authors WHERE position = 0` is **row-set-identical** to the legacy join; no fallback branch is needed.

## Decision

Drop `books.author_id` for real (not soft-deprecate), and make **`book_authors` position 0 the sole canonical source of a book's primary author** — there is no denormalized primary column anymore.

1. **Reads — inline position-0 join, not a view.** Each `JOIN authors a ON b.author_id = a.id` becomes `JOIN book_authors ba ON ba.book_id = b.id AND ba.position = 0 JOIN authors a ON a.id = ba.author_id`. Matches the established Phase 4 / `load_contributors` idiom; avoids a SQLite view's optimization-fence risk and keeps the position-0 semantics visible at each call site.
2. **Filters — contributor-aware.** "Books by author X" filters/bulk writes flip to `b.id IN (SELECT book_id FROM book_authors WHERE author_id IN (…))` (X is **any** contributor), consistent with Phase 4 membership and Phase 7's `?author_id` flip. This closes the Chaney/Anspach pathology in the scan/enrichment paths. Accepted bounded cost: a co-authored book can be enumerated under each co-author on the needs-scan collectors — a small MAM-economy risk mitigated by the existing attempted-set/dedup gates ([ADR-0005](0005-backfill-attempted-set.md)).
3. **The series multi_author anchor** ([ADR-0010](0010-series-author-mode-taxonomy.md)) redefines "most-common primary" as most-common **position-0** author from `book_authors`. `series.author_id` (the owner pointer) is **unaffected** — only `books.author_id` drops.
4. **Migration — irreversible table rebuild with safety rails**, mirroring `_migrate_series_author_nullable`: an idempotent Python migration guarded by `PRAGMA table_info(books)` (skip if already dropped), ordered **after** `_backfill_book_authors` (which still needs the column on first boot), with a **pre-flight assertion** that no book with a non-NULL `author_id` lacks a position-0 `book_authors` row — **abort the drop** if violated, never destroy a book's only author link. `foreign_keys = OFF` for the swap; the new `books` schema omits the column, its FK, and the two indexes. The base schema loses them too (fresh DBs never get the column). We do **not** assert `author_id == position-0 author`: divergence there is precisely the denorm drift this removes, and `book_authors` wins.
5. **Writes end with the drop, in one atomic phase.** The physical drop is what enforces the end of the dual-write: the ~6 writer sites that list `author_id` as a `books` column, and the author-merge reparenting `UPDATE books SET author_id = ? WHERE author_id = ?` (database.py:1357, which runs on *every* startup), must target `book_authors` instead — or the next boot crashes. `_backfill_book_authors` itself becomes column-presence-aware so it survives once the column is gone.

## Consequences

- Primary author has no denormalized home; any future code that wants "the primary author" must read `book_authors` position 0. A reintroduced `books.author_id` would be the drift this forbids.
- The drop is irreversible and runs on prod's first boot of the v3.0.0 image (the arc ships as one PR). Mitigated by the pre-flight assertion, the idempotent guard, and a release-note instruction to back up the DB before upgrading.
- The dead FK and the two `author_id` indexes go with the rebuild; "owned books by author" is now served by `idx_book_authors_author` + a join to `books`.
- ~40 read sites and ~6 writer sites change in one phase; the test suites that seed books via `author_id` (40 files `INSERT INTO books`, 67 reference `author_id`) migrate to inserting without the column and seeding `book_authors`.
- This refines [ADR-0008](0008-book-authors-authoritative-on-reads.md): its "Phase 9's column drop becomes a no-op on reads" consequence did not hold; the read re-point is the bulk of Phase 9.

## Related

- [0008](0008-book-authors-authoritative-on-reads.md) — `book_authors` authoritative on reads; this completes it and corrects its Phase 9 "no-op on reads" prediction.
- [0010](0010-series-author-mode-taxonomy.md) — series author_mode; its multi_author anchor moves from `books.author_id` to position 0 here.
- [0009](0009-merge-union-prune-overlap.md) — write-time contributor-set semantics; the merge/prune writers are among those finalized here.
- [0005](0005-backfill-attempted-set.md) — the attempted-set/dedup gates that bound the contributor-aware needs-scan cost.
