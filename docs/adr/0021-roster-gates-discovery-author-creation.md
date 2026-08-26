# 0021. The roster gates discovery author creation and scan eligibility

- Status: Accepted
- Date: 2026-08-26

## Context

[ADR-0009](0009-merge-union-prune-overlap.md) and
[ADR-0014](0014-heal-contributors-on-scan-convergence.md) both settled a
priority for write-time contributor handling, and both stated it explicitly:

> "Never silently lose a real co-author is the priority over never gain a
> wrong one." — ADR-0009
>
> "Bounded pollution risk identical to ADR-0009 merge: a spurious source
> co-author can union in (role-filtered + trusted-create-gated + manually
> correctable)." — ADR-0014

Both called the resulting pollution **bounded**. On 2026-08-25/26 that word was
falsified on production data.

An operator ran source scans across A-surname then B-surname authors in
`calibre-library`. In roughly 36 hours:

| | before | after |
| --- | --- | --- |
| authors | 896 | **7,214** (+6,318) |
| books | ~3,590 | **13,668** (+10,079, all unowned) |
| series | ~1,780 | 2,670 |

**13 of the 6,318 new authors were on the allow list. 6,305 were not.**

Three properties combined to produce this:

1. `_link_discovered_contributors` and `_heal_contributors` call
   `resolve_or_create_author(allow_create=True)` for every author-role
   contributor, because `TRUSTED_CREATE_SOURCES` includes Goodreads, Amazon,
   Hardcover, Audible and MAM. A Hardcover anthology or magazine issue carries
   20+ contributors, so one such row mints 20+ authors.
2. A minted author immediately has `book_authors` rows, and the scan-eligibility
   query (`lookup.py`, mirrored in `routers/scan.py`) tested only
   `last_lookup_at < cutoff AND id IN (SELECT author_id FROM book_authors)`.
   So the mint **promoted a stranger to a scan target** with no operator action.
3. That closes a loop. Live example: `Clive Barker` was created at
   2026-08-25 17:58 as *position 22* of the anthology *"Dark Delicacies III:
   Haunted"*, scanned at 2026-08-26 05:22, and produced **439 books** including
   Greek, French and German editions — each minting the next generation.

Critically, **the bylines were not wrong**. Barker really did contribute to that
anthology. The failure was not bad parsing; it was that a *correct* byline entry
silently conferred roster membership. `authors` rows serve two distinct
purposes — "credited on this book" and "an author we track and fetch" — and
nothing separated them.

Measurement also showed the library-sync path is **not** implicated: only 16 of
780 Calibre-sourced authors and 59 of 383 ABS-sourced authors are off the allow
list, and dropping them would orphan 23 owned books entirely.

## Decision

Introduce the **roster** as an explicit domain concept, and gate on it.

An author is **in the roster** for a library when either:

- their normalized name is on the `authors_allowed` allow list, **or**
- they own ≥1 book in that library, in **any** contributor position (per
  *co-authored ownership*, [ADR-0008](0008-book-authors-authoritative-on-reads.md)
  — a co-author counts, not only a primary).

The roster gates two operations, using the **same predicate in both**:

1. **Discovery author creation.** A contributor found by a source scan is
   linked only if it is in the roster. An allow-listed name with no author row
   **is minted** (the operator asked for that author). A non-allow-listed name
   that resolves to an existing roster row is **linked, never minted**.
   Anything else is **skipped entirely** — no author row, no `book_authors`
   link. This applies to `_link_discovered_contributors` and, by construction,
   to `_heal_contributors`.
2. **Scan eligibility.** The scan-due queries admit only roster members, so a
   non-roster row that already exists (from a pre-upgrade install) goes inert
   rather than continuing to cascade.

Bounding rules:

- **Library sync is unchanged.** Calibre/ABS sync keeps linking every real
  contributor, roster or not. The library is authoritative over its own
  bylines — the prune-side half of ADR-0009 — and gating here would orphan
  owned books for no pollution prevented. Non-roster *library* authors are
  instead surfaced into the existing `authors_tentative_review` list so the
  operator can promote them to the allow list. Bounded by construction (75 rows
  on the reference install).
- **Source-scan skips are silent.** Skipped contributors are counted and logged
  per scan, but written nowhere. Surfacing them for review would have generated
  6,303 review rows in the reference window — the same flood in a different
  table. Library-sync surfacing is bounded because it is operator-curated data;
  anthology contributor lists are not.
- **Normalization.** Roster membership is tested with
  `app/filter/normalize.py::normalize_author` (the allow-list normalizer), NOT
  `app/metadata/author_names.py::normalize_author_name` (the author-row
  normalizer). They genuinely disagree — "J.R.R. Tolkien" normalizes to
  `j r r tolkien` versus `jrr tolkien` — and using the wrong one silently
  rejects allow-listed authors.
- **Empty allow list is safe.** On a fresh install the owned-in-library half
  admits every library author, so scanning behaves as it does today. The gate
  only ever excludes authors who are neither owned nor asked for.

This **amends ADR-0014 rather than superseding it.** Heal-on-convergence still
unions the source's contributors into unowned discovered rows under its
owned-guard, delta-only and append-only rules; it now unions only roster
members. [ADR-0009](0009-merge-union-prune-overlap.md) is untouched — merge and
prune-linkage operate on two rows that already exist and mint nothing.

## Consequences

- The stated priority inverts **for the discovery mint path only**: preventing
  roster pollution now outranks byline completeness on unowned discovered rows.
  Owned books, merge, and prune keep ADR-0009's "never lose a real co-author."
- An unowned discovered book's byline may be incomplete, showing only its
  roster contributors. Accepted: these are missing-book candidates, and the
  contributor that matters is the one the operator tracks.
- Roster membership is **derived, not stored** — no new column, no migration.
  The cost is a per-scan preloaded allow-list set plus an owned-books lookup,
  mirroring how `load_normalized_sets` already feeds the filter.
- A second-order win: `resolve_or_create_author`'s fuzzy rung full-table-scans
  `authors` and runs `SequenceMatcher` over every row on each miss. At 7,214
  authors a single 20-contributor anthology cost ~144k comparisons, so the
  bloated roster was also making every later scan slower. Capping the roster
  caps that cost.
- Already-polluted installs need remediation, shipped as a hygiene job. It must
  handle three failure modes found while cleaning the reference install:
  deleting `series` rows by `author_id` leaves surviving books with dangling
  `series_id`; `book_series_suggestions` rows dangle; and books that lose their
  position-0 contributor need dense renumbering to preserve the
  "exactly one position-0" invariant ([ADR-0012](0012-drop-books-author-id-position-0-canonical.md)).
- "Bounded pollution risk" as an accepted trade-off should not be restated in
  future ADRs without a mechanism that actually bounds it. Role-filtering and
  the trusted-create gate were both working as designed here; neither bounds
  anything when the source's contributor list is itself unbounded.

## Related

- [0008](0008-book-authors-authoritative-on-reads.md) — `book_authors`
  authoritative on reads; source of the "any position counts" ownership rule.
- [0009](0009-merge-union-prune-overlap.md) — merge/prune contributor
  semantics; untouched, and the origin of the "bounded" risk assessment.
- [0012](0012-drop-books-author-id-position-0-canonical.md) — the position-0
  invariant the cleanup job must preserve.
- [0014](0014-heal-contributors-on-scan-convergence.md) — **amended** here: the
  heal union now admits roster members only.
- [0015](0015-source-id-aware-author-identity.md) — the
  `resolve_or_create_author` matching ladder the gate sits at the end of.
