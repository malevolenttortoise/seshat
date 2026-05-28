# 0014. Heal unowned discovered books' contributors on scan-convergence (union)

- Status: Accepted
- Date: 2026-05-28 (effective from v3.0.1)

## Context

v3.0.0 Phase 3.6 set a "**no re-link on convergence**" rule: when a discovery scan MATCHes an existing book (rather than INSERTing a new one), `_merge_result` enriches metadata/URLs via `_update_existing` but does **not** touch the book's `book_authors`. The discovery-INSERT-time contributor set stands. That rule is correct for the case it was designed for — a *second* source re-encountering a book the *first* source already created should not duplicate or churn its links.

But it leaves a gap. Discovered (`owned=0`) books created **before** Phase 3 contributor-parsing existed carry only a **single** author in `book_authors`: the author that was being scanned. Nothing ever heals them. A co-authored series with such thin unowned members has its contributor-set **intersection** (the basis for [ADR-0010](0010-series-author-mode-taxonomy.md)'s `author_mode`) dragged down to 1, so the series computes `per_author` instead of `multi_author` and a real co-owner is mislabeled incidental. Live pathology: the "Able Bodied Soldier" series (id=217 on prod) — 3 owned Calibre books `{Chaney, Anspach}`, 3 unowned hardcover/amazon books `{Anspach}` only → intersection `{Anspach}` → `per_author` Anspach; Chaney shown as guest.

[ADR-0009](0009-merge-union-prune-overlap.md) established write-time contributor semantics for the two row-folding operations (merge **unions**; prune does **not**, because Calibre is authoritative), keyed on the question *"is the survivor authoritative over the other side's authorship?"*. Scan-convergence is a **third** write-time site that touches `book_authors`, and ADR-0009 didn't cover it.

## Decision

On scan-convergence — the MATCH path in `_merge_result` — **re-link contributors for UNOWNED (`owned=0`) discovered books by UNIONING the source's role-filtered contributors into the existing set, existing-first** (existing positions preserved, new co-authors appended). This is the same winner-first union ADR-0009 chose for merge, and rests on the same principle: a discovered row is **not** authoritative over any single source's authorship, so never silently drop a co-author one source found — only add.

Bounding rules:

- **Owned-guard.** `owned=1` books are **never** re-linked here — their `book_authors` is Calibre/ABS-authoritative via Phase-2 sync (the prune-side "Calibre-authoritative" half of ADR-0009). Owned thin-rows are reconciled separately, operator-approved, via the deferred owned-author-review-writeback work (write-back through `push_back`), not by a discovery scan silently overwriting library data. (`owned` must be added to the `_merge_result` dedup-prefilter SELECT so the MATCH branch can see it.)
- **Delta-only.** Resolve the source's contributors and union; **write + flag the series for `author_mode` recompute only when the union actually adds an author_id.** A re-scan of an already-complete row is a no-op — this preserves Phase 3.6's "convergence doesn't duplicate/churn links" guarantee for the multi-source case while still healing thin rows. A cheap pre-gate (≤1 author-role contributor from the source ⇒ skip) keeps the hot path bounded.
- **Append-only, never reorder.** Position 0 stays the scanned author. The series fix is set-based (reorder is irrelevant to it), stable primaries avoid display churn, and keeping position 0 == the scanned author preserves the row's re-healability (the MATCH branch only fires when `matched_row.author_id` == the scanned author).
- **Recompute is the load-bearing second half.** Re-linking alone is **inert** for the series taxonomy — no discovery-scan path triggered a series `author_mode` recompute before. Healing therefore batches the touched series ids and calls `_recompute_series_author` once at end-of-scan (via the function-level lazy import already used by `_backfill_series_author_mode`, keeping authority logic single-sourced and avoiding the database↔router import cycle).
- **Lazy/opportunistic, scan-scoped.** Healing fires as a side effect of a normal author re-scan, only for books where the scanned author is already a contributor. No broadened cross-author title matching (that reopens the over-aggressive normalized-title merge that forced the v2.30.1 hotfix), and no bulk one-shot backfill job (revisit only if lazy healing proves too slow in prod). Once a row heals to the full set, future scans of *either* co-author match it — a self-correcting cycle that also reduces duplicate creation over time.

This **reverses Phase 3.6's "no re-link on convergence" rule for UNOWNED books only**; owned books and the multi-source no-duplicate guarantee are both preserved.

## Consequences

- Pre-3.0 thin co-authored series self-correct on the next author re-scan; the id=217 pathology closes without a migration or bulk job.
- Owned author data stays library-authoritative — the owned counterpart (operator-reviewed write-back to Calibre/ABS) is deliberately a separate, deferred item.
- Bounded pollution risk identical to ADR-0009 merge: a spurious source co-author can union in (role-filtered + trusted-create-gated + manually correctable). "Never silently lose a real co-author" remains the priority over "never gain a wrong one."
- The Phase 3.6 convergence test (`tests/discovery/test_book_authors_merge_integration.py`) splits into an owned case (no re-link) and an unowned case (heal).
- Delta-only + batched recompute keep the added hot-path cost bounded: a clean re-scan does a few indexed author lookups and then no-ops.

## Related

- [0008](0008-book-authors-authoritative-on-reads.md) — `book_authors` authoritative on reads.
- [0009](0009-merge-union-prune-overlap.md) — write-time contributor semantics for merge/prune; this is the scan-convergence counterpart (the third write site), extending the union principle.
- [0010](0010-series-author-mode-taxonomy.md) — series `author_mode` by contributor-set intersection; the thin-row pathology this heals is an ADR-0010 intersection being dragged to 1.
