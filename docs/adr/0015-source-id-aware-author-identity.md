# 15. Source-ID-aware author identity

Status: Accepted

Date: 2026-05-29

## Context

v3.0.0 Phase 3 added a `Contributor` dataclass that captures, per credited author on a discovered book, the source's stable author ID (`source_author_id` — Goodreads `/author/show/<id>`, Hardcover `author{id}`, Amazon ASIN, Audnexus ASIN, OpenLibrary key) straight from the same DOM/JSON node as the name. But the consume path drops it: `_link_discovered_contributors` (`app/discovery/lookup.py`) resolves each co-author by **name only** via `resolve_or_create_author`, and the captured ID dies at link time.

Meanwhile author identity is resolved entirely by name:

- `resolve_or_create_author` (`app/discovery/database.py`) — exact name → exact `normalized_name` → fuzzy (`authors_match`, SequenceMatcher ≥ 0.92) → mint/None.
- `get_or_create_person` (`app/discovery/author_identity.py`) — `author_links` lookup → exact normalized `persons` → fuzzy persons (flagged `link_confidence='low'`) → mint+link.

Name-only matching produces the long-standing **split-person gap**: Calibre's "Robert Heinlein" and ABS's "Robert A. Heinlein" land on separate `persons` rows because the normalized names differ and fuzzy may not clear 0.92 — even when both author rows already carry the *same* `goodreads_id`. (The scanned-author path has been writing `authors.{source}_id` from `AuthorResult.external_id` all along, so prod already holds thousands of populated source IDs that nothing uses for *person* matching.)

A stable per-source author ID is stronger identity evidence than any name string. The question was how to fold it into identity resolution without corrupting existing canonical IDs on a name collision.

## Decision

Make author identity **source-ID-aware, ID-first**, reusing the existing single-column-per-source model on `authors` (no new `author_source_ids` table).

1. **Persist co-author IDs at link time.** `_link_discovered_contributors` threads each contributor's `source_author_id` + the scan's source name into `resolve_or_create_author`. On mint, the new row gets its `{source}_id`.

2. **Fill-if-empty write policy for co-authors.** When a co-author resolves to an *existing* row, set `{source}_id` only if it is currently NULL; **never overwrite** a populated column. This is a deliberate asymmetry with the *scanned-author* path (`_merge_result`, which overwrites `{source}_id`): the scanned author is the authoritative subject of the search (high confidence), whereas a co-author is byline-derived from a matched name string (lower confidence). Silently overwriting a canonical ID on a name collision is how identity gets corrupted.

3. **ID-first matching ladders.**
   - `resolve_or_create_author` gains `source` + `source_id` params; ladder becomes `WHERE {source}_id = ?` → exact name → normalized → fuzzy → mint (writing the ID).
   - `get_or_create_person` gains an ID-aware rung that joins **through `author_links`**: if any already-linked author row (any library) carries the same `{source}_id`, reuse that person (high confidence) before falling to name matching. (`persons` itself stays free of per-source ID columns — IDs remain authoritative on `authors`.)

4. **Surface ID conflicts instead of silently swallowing them.** Case 4 — incoming ID matched no row, but the incoming *name* matched a row already holding a *different* ID for that source — is a genuine identity ambiguity (wrong name-match, or a source split-author à la Goodreads' Tyler Burnworth two-profile case). Record it in a deduped `author_source_id_conflicts` table; surface it read-only in the Persons & IDs page with a dismiss action. Resolution uses the existing manual person-merge / source-ID-edit tools — no new pick-a-winner mutation.

5. **One-shot consolidation over existing data.** A new hygiene job ("Consolidate persons by shared source ID") applies the ID-aware person rung across existing `author_links` to **merge separate persons that share a source ID** — distinct from the existing Job 8, which only *mirrors* a source ID across rows already linked to the *same* person. Idempotent, audit-logged, conflict-recording. This cleans the split-person gap on day one using the already-populated scanned-author IDs, rather than waiting for co-author IDs to slowly accumulate.

Scope deliberately excludes: images (owned by the author-image-rework item — `image_url` columns exist but `mirror_image_url` and backfill belong there); MAM author IDs (enrichment-only, a separate wiring path, no `mam_id` column added speculatively); and the Goodreads list-page role-skip cleanup (a discovery-completeness concern folded into the Goodreads cache-extension item).

## Consequences

- The split-person gap closes: cross-library identities that share a source ID consolidate confidently instead of fuzzy-guessing on names.
- **Feedback loop by design** — ID-matching only helps once IDs are persisted, so new scans behave as before for co-authors and identity sharpens as IDs accumulate. The one-shot hygiene job front-loads the win for the scanned-author IDs that already exist.
- Sources without author IDs (Google Books, OpenLibrary link-only) simply skip the ID rung since `source_id` is absent — no special-casing.
- The fill-if-empty asymmetry means a co-author's freshly-captured ID will *not* correct a wrong ID already on a name-collided row; instead the conflict is recorded for operator review. This trades automatic correction (risky) for visibility (safe).
- New surface area is bounded: a few lines in `_link_discovered_contributors`, an ID rung in each of the two resolvers, one `author_source_id_conflicts` table + read endpoint + Persons & IDs panel + dismiss, and one hygiene job.
- Reversibility: ID *persistence* is additive and safe. Person *merges* from the consolidation job are hard to undo (manual re-split), which — together with the ID-before-name ordering being non-obvious — is why this is recorded as an ADR.

## Related

- [ADR-0008](0008-book-authors-authoritative-on-reads.md) — `book_authors` authoritative on reads (the contributor model this consumes).
- [ADR-0009](0009-merge-union-prune-overlap.md) — contributor-set union/overlap (the multi-author write semantics).
- [ADR-0013](0013-claim-for-owned-contributor-aware.md) — contributor-aware matching (the matching half this extends to IDs).
- [ADR-0014](0014-heal-contributors-on-scan-convergence.md) — heal unowned contributors on convergence (the discovered-book counterpart).
