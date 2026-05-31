# 0018. Metadata cache extended to Goodreads as a list-page cache

- Status: Accepted
- Date: 2026-05-30 (effective from v3.4.0)

## Context

The Amazon metadata cache landed in v2.21.0 ([architectural plumbing in `app/discovery/metadata_cache.py`](../../app/discovery/metadata_cache.py)) for a specific reason: Amazon's Akamai bot-manager imposes a **hard wall** at a 2–3 GET ceiling per session, with 600s+ cooldowns afterwards. Synchronous, live Amazon scans were unreliable; a paced background worker filling a per-source SQLite cache decoupled scan latency from Akamai's penalty surface. The architecture was source-templated from day one (table name prefix `metadata_cache_<source>_*`, per-source DB file, per-source migration list, `SUPPORTED_SOURCES` frozenset) so a future Goodreads cache could share the shape without a rewrite.

Goodreads has a **different cost shape**:

| | Amazon | Goodreads |
|---|---|---|
| Failure mode | Hard wall (Akamai `_abck` 2–3 GET ceiling) | Soft degradation (per-book 5–7s pacing eating per-author budget) |
| Cooldown semantics | 600s+ after a block, escalating | None — only retry-after on 202/503 |
| Recovery shape | Cooldown timer | Per-author budget exhaust at ~25–40 min, scan finalizes with partial results |
| Per-author data | ~15 KB structured (one mediaMatrix call) | 50–200 KB (one list page + per-book detail) |

The Sanderson stress-test (2026-05-22) quantified the GR gap: a real prolific-author scan completed in ~25 min but silently dropped ~149 / 399 books (37%) — every list-page book ID was visible, but per-book detail fetching at 5–7s/book exhausted the time budget before all 399 were resolved. The `[goodreads] giving up on '<author>' — processed N/M books` log line is the operator signal. Other sources (Hardcover, Kobo, OpenLibrary) masked the gap for typical use, but GR's *unique* contribution (series ordering, GR work IDs, ratings, comprehensive editions) was missing for those 149 books.

The motivation to build a GR cache, however, is **not pain-driven**. Path A (per-author budget scaling shipped in v2.21.0) bought back enough headroom that prod hasn't reported the gap in the 8 days between v2.20.3 and v3.3.0. The motivation is **positive-pull**: Mark wants the same shape of "scans return instantly, enrichment is cleaner, IDs are exact" for GR that v2.21.0 delivered for Amazon.

A latent gap also exists in Amazon's settings shape: the worker has been reading nested `metadata_cache.amazon.{mode, schedule.*}` via `mc.get(...)` fallbacks since v2.21.0, but `DEFAULT_SETTINGS` never seeded the sub-tree. Fresh installs work because every read has a default; existing installs work because the operator hand-writes the keys via the Settings UI. A symmetric seed for both sources closes that gap as a ride-along.

The `goodreads.py` list-page parser also carries a substring filter at `goodreads.py:690-706` + `765-773` that skips an entire row if `(translator)` or `(contributor)` appears anywhere in its flattened text. Phase 3.2 (`e7d3bea`, 2026-05-26) made detail-page `ContributorLink__role` parsing authoritative for role identification; the list-page substring filter is strictly weaker and is known to drop legitimate books whose blurb mentions "translator" or "contributor." Retiring it is orthogonal cleanup that pairs naturally with this work — the cache worker fetches list pages, so any code on the GR list-page path is in scope.

## Decision

Extend the v2.21.0 metadata-cache architecture to **Goodreads as a list-page-only cache** in v3.4.0 (Path B), with detail caching (Path C) **deferred to a decision at dev-stack UAT** rather than committed upfront.

1. **Path B, not Path C, for v3.4.0.** Cache the per-author **list-page book-ID inventory only** — not per-book detail. New `metadata_cache_goodreads.db` with four tables: `metadata_cache_goodreads_state` + `_list_pages` + `_queue` + `_worker_state`. The `_list_pages` table swaps Amazon's per-book `_books` shape for a per-page `(author_id, library_slug, page_num) → fetched_at + book_ids_json` snapshot — cheap to populate (one list-page fetch covers ~30 GR books), cheap to read (instant scan-dedup against existing local rows), gives the worker an iteration anchor for a future Path C extension.

   Per-book detail still goes live through the existing `GoodreadsSource` HTTP path. The cache stores the inventory; live fetch fills the structured data. This is enough to eliminate the Sanderson silent-drop gap (every list-page book ID is preserved across re-scans, returned instantly from cache, with detail fetched in priority order for cached IDs the local DB hasn't seen yet) without committing to ~130–150 MB of per-book detail storage and the worker complexity to maintain it.

   Storage projection for Mark's library (~700 authors): **~3.5 MB** (~5 KB/author × 700 authors of list-page snapshots). Trivial.

2. **Source-shape divergence is OK; source-agnostic mechanics stay shared.** `state`, `queue`, and `worker_state` shapes mirror Amazon one-for-one — so the existing worker telemetry primitives (daily summary, stall watchdog, heartbeat, `today_scan_count` / `today_block_count`) reuse without source-specific branching. The detail-data shape (`books` vs `list_pages`) diverges — captured in the `_TABLE_NAMES` per-source dict, with a new `per_source_table_suffixes(source)` helper for callers like `db_summary` that need to enumerate every table in a source's DB.

   Rejected alternative: unify Amazon + GR on a single `books` table shape, with GR rows degenerate (just the ID populated). Forces an awkward "is this row a real cached book or a list-page anchor?" predicate everywhere; loses the per-page TTL property (a Sanderson list page that grew from 399 → 412 needs to invalidate the *page* snapshot, not 412 individual book rows). Cleaner to let the shapes diverge.

3. **Settings symmetry seeded for both sources.** `DEFAULT_SETTINGS["metadata_cache"]` gains a nested sub-tree for **both `amazon` AND `goodreads`** with identical shape (`enabled: False`, `mode: "disabled"`, `schedule.{active_hours: "10:00-22:00", timezone: ""}`). Closes the latent Amazon fresh-install gap as a ride-along: every key the worker reads via `mc.get(...)` fallback now exists in the seed.

   Existing installs: `_apply_legacy_settings_migrations` deep-seeds missing nested sub-keys without overwriting user values. A user who has saved `metadata_cache.amazon.mode = "scheduled"` keeps that; the migration only fills in keys the saved dict doesn't already contain. This avoids the shallow-merge gotcha (where any saved `metadata_cache` sub-dict would otherwise mask the new goodreads sub-tree entirely).

   Both default to `mode="disabled"` on upgrade — **zero live behavior change**. The operator opts in per-source via the Settings panel (slice 06).

4. **List-page substring role filter retired (slice 02).** The `(translator)` / `(contributor)` substring skip in `goodreads.py` list-page parsing is removed. Detail-page `ContributorLink__role` parsing has been authoritative for role identification since Phase 3.2 (v3.0.0); the substring filter was a pre-Phase-3 hedge that produces false-positive drops on legitimate books. Cost of removal: one extra detail fetch per legitimate-translator/contributor book on first-fill, paid once by the background worker, never re-paid on cache reads. The cache makes this acceptable — pre-cache, every false-positive drop was a missed book; post-cache, every false-positive drop is a one-time fetch.

5. **Telemetry ride-along (slice 05).** The source-agnostic worker telemetry surfaces from v2.21.0 Phase G (daily summary ntfy, stall watchdog, `today_scan_count` / `today_block_count` counters) extend to GR for free — the shared `_worker_state` shape carries them. A new `goodreads_budget_exhaust_count` counter, incremented at `lookup.py:3915` (the `[goodreads] giving up on …` warning point), is added to the daily summary so the budget-exhaust signal moves out of log-grep range and into operator-readable telemetry. This counter is the **data signal for the Path C decision** in slice 07.

6. **Path C decision gate at v3.4.0 dev-stack UAT, NOT post-prod observation.** Run a **seeded Sanderson-class author** in the dev-stack against the Path B implementation. Four-bar acceptance:
   1. **Cache correctness** — list pages cached correctly; subsequent scans return cached IDs instantly (zero live HTTP); manual force-refresh + cache wipe + cold-fill all work.
   2. **Felt snappiness** — re-scan of any cached dev-stack author returns in <2s (currently 10–30s typical).
   3. **No silent drops** — every book ID on a cached list page is preserved across re-scans; `[goodreads] giving up on` never fires on a cached author.
   4. **First-fill UX** — cold-author scan enqueues + returns partial result + UI badge ("GR worker building cache").

   **Path C → v3.5.0 if and only if** the seeded-Sanderson UAT shows perceptible wastage on filter-rejected new books that Path B can't eliminate (foreign editions / anthologies / wrong format reaching detail-fetch then failing client-side filters). If Path B feels indistinguishable from Path C on Sanderson + the four-bar acceptance passes, Path C demotes to v5.x deferred. v3.5.0 would add `metadata_cache_goodreads_books` (per-book detail) + the worker's two-phase iteration (list → detail) + read-time filter application; ADR-0019 captures that decision separately.

   Rejected alternative: ship Path B + Path C atomically as a single v3.4.0 minor. Doubles the code surface in one arc; defers the Path C value question past the "do we actually need this?" filter that Path A failed (because it shipped before observing the real prolific-author shape). Slicing buys an honest "should we ship the detail cache?" data point.

## Consequences

- **Symmetric source layer.** Adding a third metadata-source cache (Hardcover, Audible) is now a smaller delta: `_TABLE_NAMES` entry + migration list + worker dispatch in slice 03's `_perform_<source>_scan` pattern.
- **Source-shape divergence is a maintained property.** `db_summary`, the metadata-cache router, and any future cross-source enumerator MUST use `per_source_table_suffixes(source)` rather than hardcoded suffix tuples. Amazon's `books` and GR's `list_pages` are not interchangeable; tooling that assumes one or the other will fail on the other source.
- **No live behavior change from this ADR alone.** Slice 01 ships the foundation (schema + settings symmetry + this ADR). The GR worker (slice 03) and `CachedSource("goodreads")` swap in `lookup.py` (slice 04) are what actually move scans through the cache. Both default disabled — first operator opt-in is via the Settings panel (slice 06).
- **List-page substring filter retirement (slice 02) is a behavior change in its own right** — independent of the cache. Books that were previously dropped because their blurb mentioned "translator" / "contributor" now reach detail-fetch and parse normally. Caught separately by the Phase 3.2 detail-page parser if the role is actually translator/contributor; otherwise enriched normally.
- **Settings file deep-seed runs once per install on first post-upgrade load.** Idempotent — subsequent boots are a no-op. Existing installs with a saved `metadata_cache.amazon` sub-dict are protected by the deep-merge (user values win, only missing keys get back-filled).
- **Operator backup recipe for v3.4.0 first-boot:** none. Slice 01 creates a new DB file (`metadata_cache_goodreads.db`) and back-fills missing settings keys without touching any existing data store. Per the v3.x backup convention, no pre-upgrade DB snapshot is required for this slice. (Slice 03's worker likewise adds rows without modifying existing tables; slice 07 covers full v3.4.0 release backup posture.)

## Supersedes / superseded by

- Builds on the source-agnostic architecture established in [v2.21.0 Phase B](../../app/discovery/metadata_cache.py) (no prior ADR — predates the `docs/adr/` adoption in 2026-05-26).
- The deferred Path C decision will be captured in **ADR-0019** if and only if slice 07's UAT triggers the v3.5.0 build.
