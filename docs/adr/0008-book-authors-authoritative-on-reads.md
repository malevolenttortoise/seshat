# 0008. `book_authors` is the authoritative author–book relation on reads

- Status: Accepted
- Date: 2026-05-27 (effective from v3.0.0 Phase 4)

## Context

The v3.0.0 multi-author rework introduces `book_authors(book_id, author_id, position, role)` — a per-library join replacing the single `books.author_id` column (position 0 = primary author). Phases 1–3 only *populate* it (owned-library sync + discovery contributor parsers) under a deliberate **dormancy contract**: zero user-visible change until a read path consumes it. Phase 4 is that moment — author detail, library/author counts, and the source-scan ownership/dedup prefilter flip from `books.author_id` to `book_authors`.

The blocker: the table is *sparse*. Owned books always have rows (Phase 1B/2), but a discovered book gets rows only when a source emitted contributors. In production most missing/upcoming discovered books have **zero** `book_authors` rows. A naive flip to a `book_authors` join would make those books vanish from author views and counts.

Two shapes resolve it:

- **A — COALESCE-fallback.** Read paths `LEFT JOIN book_authors` and fall back to `books.author_id` where no rows exist. The table stays sparse; every one of ~13 critical queries carries a dual-authority branch, permanently, until `books.author_id` is dropped in Phase 9. A query that forgets the fallback silently drops discovered books.
- **B — backfill-all + always-link.** A one-time idempotent per-library startup migration inserts `(book_id, author_id, 0)` for every book lacking *any* `book_authors` row, and the discovery INSERT path (`_link_discovered_contributors`) always links the scanned author even when the contributor list is empty. `book_authors` becomes a universal superset; read paths are clean joins with no fallback.

## Decision

Adopt **B**. `book_authors` is the authoritative author–book relation for all *read* paths from Phase 4 onward.

- **Backfill-all**: an idempotent startup migration (same mechanism as Phase 1B) links every zero-row book to its `author_id` at position 0. `"book has ≥1 book_authors row"` is the safe proxy for "fully linked" — owned books carry the complete set, and discovered-with-contributors always include the scanned author via the never-orphan append.
- **Always-link**: `_link_discovered_contributors` drops its `if not bk.contributors: return` early-out and always writes at least the scanned author. This **ends the dormancy contract** — the Phase 3.6 `test_empty_contributors_writes_no_book_authors` is rewritten to assert one row (scanned author, position 0).
- **Reads flip; writes stay dual.** Phase 4 changes read paths only. Writers keep maintaining `books.author_id` *and* `book_authors` until the column is dropped in Phase 9.
- **Scan prefilter** (`lookup.py` `_merge_result`) reads existing owned/known books purely from `book_authors` — `WHERE b.id IN (SELECT book_id FROM book_authors WHERE author_id IN (scanned + pen-name links))` — composing the two distinct relations (pen-name links via the id set; co-author links via the join). This is the root-cause fix for the co-author scan-duplication pathology that `cross_isbn_owners` only band-aided.
- **Count split.** Per-author counts join `book_authors` (a co-authored book counts once per author — intended). Whole-library totals count distinct `books` rows; never derive a library total by summing per-author counts. Joins that aren't strictly author-scoped use `COUNT(DISTINCT b.id)` against join fan-out.

## Consequences

- ~13 critical read queries become clean joins instead of carrying a permanent dual-authority branch; Phase 9's column drop becomes a no-op on reads.
- The dormancy contract retires exactly when it was always meant to (first read consumer). One 3.6 test is rewritten as a deliberate, documented transition — not a regression.
- Test fixtures that seed a book via `author_id` only and exercise a flipped read path (notably the scan prefilter, e.g. `test_pen_name_dedup.py`'s `_insert_book`) must now also seed a `book_authors` row — there is no startup backfill inside pytest.
- A book owned under author A surfaces as owned for every co-author (shared `books` row; owned/hidden/slug shared, consistent with the per-book hide of the rework). Pure co-authors minted in Phase 3 gain their real footprint.
- Performance: the author→books direction is covered by `idx_book_authors_author`; no new index.

## Related

- [0002](0002-multi-library-slug-routing.md) — `book_authors` is per-library, like `books`; the same slug discipline applies.
- [0006](0006-mam-not-found-is-permanent.md) — same "write a row rather than leave a gap that misbehaves on read" instinct.
