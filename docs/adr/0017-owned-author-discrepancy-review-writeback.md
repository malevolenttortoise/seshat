# 0017. Owned-book author discrepancy: enqueue at scan-convergence, union write-back, operator-reviewed

- Status: Accepted
- Date: 2026-05-30 (effective from v3.3.0)

## Context

[ADR-0014](0014-heal-contributors-on-scan-convergence.md) heals UNOWNED discovered books' `book_authors` on scan-convergence by unioning the source's role-filtered contributors into the existing set. It deliberately stops at owned (`owned=1`) books — Calibre/ABS are authoritative there per [ADR-0009](0009-merge-union-prune-overlap.md)'s prune principle, so a discovery scan must **never** silently overwrite library data. ADR-0014 explicitly named "the deferred owned-author-review-writeback work (write-back through `push_back`)" as the owned counterpart.

That leaves a real gap. An owned book's `book_authors` can be thin or wrong — a Calibre row that listed "Chaney & Anspach" but only Chaney made it into `book_authors` during Phase 2 sync, a co-author the library never recorded, a source-confirmed primary that contradicts the library's primary. The v3.0.1 owned-guard refuses to fix any of it. The operator has no in-product path to reconcile an owned book's authors short of editing Calibre by hand.

Authors are also the **first** set-shaped proposed-change in `metadata_review_queue`. The table today stores scalar field diffs — `metadata_review_queue(book_id, field, old_value, new_value, source, …)` UNIQUE on `(book_id, field, source)`, both value columns TEXT. An authors proposal is a *set* of contributors, not a scalar, so the data model needs a deliberate decision: extend the existing table or build a new one.

A further constraint: [ADR-0015](0015-source-id-aware-author-identity.md) made author identity ID-aware. A proposal that carries only contributor *names* — no per-source IDs — would resolve through name-only matching when applied, reintroducing exactly the split-person gap ADR-0015 closed. The proposal payload has to carry IDs, not just names.

Sibling backlog item: the small UI change "Metadata Manager field-level filters" (chip filters above the review list) was originally a standalone warm-up. Pairing it with this work as a single v3.3.0 arc forces the chip taxonomy to cover "Authors" from day one rather than retrofitting later — the chip set otherwise would have shipped as the 6-field scalar enum and grown a 7th chip with a divergent data shape after the fact.

## Decision

Surface owned-book author discrepancies as **authors proposed-changes** in Metadata Manager, with **detection at scan-convergence**, **union-only write-back semantics**, and **dispatch through the existing push-back machinery extended to author payloads**.

1. **Data model — JSON in `metadata_review_queue.new_value`, `field='authors'`.** No new table. The existing UNIQUE `(book_id, field='authors', source)` fits naturally: each source has one opinion about the authors set; a fresh scan UPSERTs. The payload is a JSON array of contributor records carrying both name and source ID:
   ```json
   [{"name": "Author X", "source_id": "abc123"}, {"name": "Author Y", "source_id": null}]
   ```
   `source_id` is namespaced to the proposal's `source` column (a goodreads proposal carries goodreads IDs; an amazon proposal carries ASINs). `old_value` snapshots `book_authors` at proposal time in the same shape. Snapshot semantics — not live-refresh on render — match the scalar-field UI today; drift between proposal time and accept time is acceptable (operator either applies or dismisses).

   Rejected alternatives: a separate `author_review_queue` table with row-per-contributor verb (`add`/`remove`/`reorder`) — adds per-contributor granularity at the cost of mixed-shape bulk dispatch and a duplicated UI surface, for a partial-approval use case that is rare in practice; a typed `new_value_set TEXT` column with explicit `kind ∈ {scalar, set}` — overengineered for a single set-shaped field and reversible later if 2+ more arrive.

2. **Detection — live, in `_merge_result` MATCH path against owned books.** The v3.0.1 heal's owned-guard branch grows an enqueue step. Trigger condition: `source_contributor_set ⊄ current_book_authors_set` (source proposes ≥1 author not in current) OR `source[position 0] ≠ current[position 0]` (primary differs even if sets equal). Skip pure-removal diffs (source is a strict subset of current) — Calibre is authoritative on removals; operator prunes by hand. Skip ordering changes that don't touch primary (cosmetic).

   Source quality filter: enqueue from `goodreads`, `amazon`, `hardcover`, `audible` only. Link-only sources (`google_books`, `openlibrary`) have weak author data and are excluded. **MAM is deferred** — it has authoritative `author_info` for grabbed torrents and would be the single strongest signal, but MAM doesn't flow through `_merge_result` for owned books today; threading it requires a separate trigger point off `pipeline.py:train_authors_from_blob` (grab-completion), which is a self-contained follow-up.

   Rejected alternatives: a hygiene job comparing all owned `book_authors` against captured per-source contributor data — Phase 3 didn't persist per-source contributor snapshots for owned books, so a hygiene job would have to re-run scans (which is what the live path already does); a one-shot bulk-backfill scan of every owned author — burns MAM/Goodreads quota for marginal value over operator-driven per-author scans.

3. **Union semantics — source-primary-first ordering, additive-only.** When an authors proposal is enqueued, `new_value = source ∪ current`, ordered: source's primary first, then source's remaining contributors, then current's exclusives appended. The proposal is purely additive — operator can never approve a removal via a proposal. Removals require editing Calibre directly. This matches ADR-0014's "never silently lose a real co-author" spirit and ADR-0009's prune principle (Calibre is authoritative on the question "should this author still be here at all?"; sources are authoritative on the question "is there a co-author missing?").

   Pure primary swap (sets equal, position 0 differs) falls out naturally: source `[Y, X]` ∪ current `[X, Y]` → `[Y, X]` (source primary first; X is in current's exclusives, appended). Mixed (source has new + lacks some): source `[X, Z]` ∪ current `[X, Y]` → `[X, Z, Y]` — adds Z, **keeps Y**, source's primary takes position 0.

   Rejected alternative: replace semantics (`new_value = source exactly`) — forces explicit decisions on every current author per proposal; most are no-ops; chrome is wasteful and removal-by-proposal opens silent-loss surface.

4. **UI render variant — additive list-diff in `FieldDiff` (`DiscMetadataPage.tsx`).** Switch on `row.field === 'authors'`. Parse `old_value`/`new_value` JSON. Two-column layout same as scalar FieldDiff for visual consistency; per-row visual states: **added** (in new, not in old) tagged `(new)` using `t.grnb`/`t.grnt` for vocabulary continuity with the Persons & IDs panel; **position 0 change** marked `← primary` on each side; **unchanged** plain. Source-ID hidden in the diff by default — the proposal's `source` column already gives provenance, per-author IDs would clutter the operator's scan.

5. **Push-back — extend the existing three sinks to handle `authors`.** Add `"authors": "authors"` to each sink's field map (`_ABS_FIELD_MAP`, `_CALIBREDB_FIELD_MAP`, `_CWA_FIELD_MAP`) and a sink-specific formatter that emits position-ordered names from the post-approval `book_authors` rows:
   - **ABS** (`push_abs`): `[{"name": "X"}, {"name": "Y"}]` array on the PATCH body; ABS reuses existing author IDs by name and creates missing ones.
   - **calibredb** (`push_calibre_full`): `--field authors:"X & Y"` (Calibre canonical `&` separator; commas in a name like `"Smith, John"` don't conflict). Calibre auto-regenerates `author_sort`.
   - **CWA** (`push_cwa`): set `authors` in the merged form dict; the existing form-scrape-and-merge POST pattern handles the rest. CWA's exact field name + separator is verified against the live dev-stack edit form at implementation time (cheap; risk of mismatch = silently-ignored POST per the 2026-05-11 incident, caught immediately in UAT).

   Common validation: reject the push with `PushFailed` if the resolved `book_authors` for the book is empty after applying — both Calibre and ABS require ≥1 author.

6. **Re-sync — inline, using existing primitives.** After the sink push + snapshot refresh, immediately:
   1. Resolve each post-push author `(name, source_id)` via `resolve_or_create_author` — gets per-library author IDs, mints missing ones, captures source IDs on new rows so v3.1.0's ID-first matching applies.
   2. Call `write_book_authors(db, book_id, ordered_author_ids)` to overwrite `book_authors` for this book in position order.
   3. Lazy-import `_recompute_series_author(db, series_ids_for_this_book)` from `routers/series` so each series the book belongs to recomputes its `author_mode`.

   Atomic with the snapshot refresh; operator sees consistent state on next page load. Same primitives the full sync uses — no logic duplication, no scheduler dependency, no risk of divergence from `calibre_sync` / `audiobookshelf_sync` since both paths share the helper.

   Rejected alternatives: schedule a full library sync (wrong scope — thousands of books for one push); refactor sync paths to expose `sync_one_book(slug, book_id)` (overengineered for ~5 lines of inline primitives; extract later if a future need emerges).

7. **Chip filter UX — sibling shipped same arc.** The Metadata Manager chip filter ("show only authors / description / ISBN / …") ships alongside this work as a UI sibling. The chip semantic diverges from the Persons & IDs filter pattern that the original PRD invoked: each queue row is a single field-type (not a vector), so chips are **OR-within-the-set** multi-select (click "Authors" + "Description" → show both), not the 3-state cycle AND-across-chips Persons & IDs uses. Visual vocabulary (theme tokens, wrapping flex row, "Clear (N)") stays similar to keep the operator's place; mechanic matches the data. Per-chip counts derive from the full fetched list (not the filtered subset) so chips are stable as the operator filters. State is local `useState`, resets on tab change; bulk-select `selected` survives filter changes (filter is view scope, selection is action target). The "Authors" chip exists from day one of v3.3.0 — the paired-arc framing is what made this clean rather than retrofitting a 7th chip with a divergent data shape later.

8. **Scope cuts in v3.3.0 (out, but compatible):**
   - **MAM as enqueue source** — deferred per #2; needs a separate trigger off grab-completion.
   - **Per-contributor partial approval** — rejected per #1; operator approves the whole proposal or dismisses.
   - **Cross-source proposal merging** — three sources may each enqueue a different proposal for the same book; each is independent and reviewed/applied independently. No attempt to fuse them into a single "best" proposal — would require a trust-arbitration rule and discards the "operator decides" frame.
   - **Author removal via proposal** — rejected per #3; removals are a hand-edit-in-Calibre action.
   - **Hygiene-job-driven bulk backfill** — rejected per #2.

## Consequences

- Owned-book author discrepancies become a first-class operator review surface. The Phase 2 sync gap (Calibre had "Chaney & Anspach", `book_authors` only got Chaney) self-heals on the next operator-approved scan-converged proposal, restoring co-author signal to the series taxonomy ([ADR-0010](0010-series-author-mode-taxonomy.md)).
- `book_authors` is now writable by an operator action through the push-back machinery — the only path that does so. ADR-0009's "Calibre-authoritative" principle is preserved: the write reaches `book_authors` via Calibre/ABS first (Step 6 round-trips through `resolve_or_create_author` + `write_book_authors` driven by the post-push snapshot), not by direct mutation.
- The first set-shaped row lands in `metadata_review_queue`. Future set-shaped fields (e.g. a tags-as-set proposal) can follow the same α pattern; the rejected δ alternative (typed `kind` column) stays available if 2+ more arrive.
- v3.1.0's ID-first matching applies through the re-sync path because the proposal carries source IDs alongside names. A proposal landing a new co-author with a source ID consolidates correctly into an existing person if the ID is already on a sibling row; without the ID it falls back to name matching with the same INFO-logged behavior the v3.1.0 slice 03 documented.
- The chip filter's OR-within-multi-select diverges visibly from Persons & IDs' AND-across cycle. Operators using both surfaces have to learn the distinction; visual vocabulary continuity (theme tokens, layout) keeps the disorientation small. A future surface that needs the AND-across cycle can adopt the Persons & IDs pattern explicitly.
- **Reversibility:** the data model decision (#1) is the longest-lived — the table is shared with scalar fields, so flipping to a separate `author_review_queue` later means data migration + dispatcher refactor. The union semantics (#3) are reversible cheaply by changing the payload-build rule; UI render variant (#4) is one component; push-back additions (#5) are additive entries in three field maps; re-sync (#6) is local to push-back. The chip filter (#7) is pure UI and trivially flippable.
- **Operator note:** approving an authors proposal triggers an immediate push to Calibre/ABS — same blast radius as approving a scalar field proposal. The dev-stack UAT path mirrors the existing scalar push flow ([feedback-seshat-dev-stack-uat-workflow]).
- A v3.x follow-up captures **MAM as an enqueue source** when prioritized; threading point is `pipeline.py:train_authors_from_blob` (grab completion), payload shape is unchanged from #1.

## Related

- [ADR-0008](0008-book-authors-authoritative-on-reads.md) — `book_authors` authoritative on reads; the read-side counterpart this write path serves.
- [ADR-0009](0009-merge-union-prune-overlap.md) — write-time contributor semantics (merge unions, prune doesn't); this is the operator-reviewed write-back counterpart to those two unattended write sites.
- [ADR-0014](0014-heal-contributors-on-scan-convergence.md) — heal **unowned** discovered books on convergence; this ADR is the **owned** counterpart it explicitly deferred.
- [ADR-0015](0015-source-id-aware-author-identity.md) — source-ID-aware author identity; the reason the proposal payload (#1) carries source IDs alongside names and re-sync (#6) routes through `resolve_or_create_author`.
- [ADR-0010](0010-series-author-mode-taxonomy.md) — series `author_mode` by contributor-set intersection; the recompute in re-sync (#6 step 3) keeps the taxonomy consistent after push-back.
