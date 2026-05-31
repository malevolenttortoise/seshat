# Hygiene jobs

Data Hygiene is a single operator-triggered chain that runs twelve maintenance jobs against every configured library. It lives on the unified Dashboard as the **Data Hygiene** button in the Command Center; clicking it opens a confirmation modal listing the jobs, and once confirmed the chain spawns as a background task and reports its progress on the same banner that surfaces source scans and library syncs.

The chain is **manually triggered only** — no scheduled or on-startup run. Each click runs all twelve jobs in order across every library; there is no per-job toggle. Every job is **idempotent**: re-running back-to-back drops every counter to zero once a steady state is reached, so an operator who's worried after a click can safely click again to see what's stable.

The progress banner shows `N of 12: <job name> — <library>` while a job runs. The job name strings in this chapter match the trigger-time confirm modal (what you see before clicking "Run"). The progress banner labels them slightly differently in two places — noted inline where it matters.

Throughout this chapter, "Seshat-only" means a write that lands in Seshat's own working DBs (`seshat.db` + per-library `seshat_<slug>.db`) and never reaches the authoritative Calibre `metadata.db` or Audiobookshelf API. "Canonical-state writes" mean the opposite — the chain does not currently make any. Hygiene reads from Calibre/ABS via the same paths a normal scan uses, but it does not write back to them.

## Universal rules

Three invariants hold across every job:

- **Hidden books are skipped** by the cleanup + dedup jobs (working sets filter `hidden = 0`). Identifier-class writes — stamping a discovered `goodreads_id` onto an `authors` row, for example — ignore hidden state because the columns are scaffolding rather than user-curated content; that's the same rule the live scan layer follows.
- **Idempotent.** Counters drop to zero on a second run; nothing thrashes.
- **`authors_allowed` is preserved by name.** Job 1 will never delete an author whose normalized name appears in the global allow-list, even if every book of theirs has been removed.

## Job 1 — Empty author + series cleanup

**What it does.** Removes [authors](../../CONTEXT.md#library-identity-and-sync) with zero books in the library and series with zero member books. Series cleanup runs first so an author whose only book pointed at a now-defunct series doesn't get misidentified as empty during the author pass.

**When it runs.** Manual, per-library (fan across every configured library).

**Side effects.** Seshat-only — deletes rows from per-library `authors` and `series`, and cascade-deletes the global `author_links` rows for any deleted author. Not audit-logged; not reversible.

**Pre-flight notes.** Two cohorts are protected. The global `authors_allowed` allow-list is the user's authorial allow-list of record and is honoured by name. **Cross-library mirror rows** — authors who have ≥ 1 book in *any other* library — are also preserved, because the v2.12.1 dual-row pattern requires those mirrors for cross-format scans to surface audiobooks alongside ebooks. (Removing this guard against zero-in-this-library + not-allow-listed authors silently wiped 93 ABS mirror rows in UAT 2026-05-17.) A [contributor](../../CONTEXT.md#library-identity-and-sync) (co-author) with any `book_authors` link in the library is **not** empty; pure co-authors are protected.

## Job 2 — Hardcover identifier backfill

**What it does.** For every book carrying `hardcover_id` but missing `goodreads_id`, `openlibrary_id`, or `google_books_id`, batch-queries Hardcover's `book_mappings` table and COALESCE-fills the missing slots. Existing values are never overwritten.

**When it runs.** Manual, per-library.

**Side effects.** Seshat-only — `UPDATE books SET goodreads_id = COALESCE(goodreads_id, ?)` etc. on the per-library DB. Not audit-logged; existing values are preserved by COALESCE, so the only "write" is filling a NULL.

**Pre-flight notes.** No-op if Hardcover isn't configured. Reuses the standard 1-second Hardcover rate-limit; runs the GraphQL query in batches of 50 IDs.

> **Progress banner:** displayed as `Hardcover ID backfill` (the backend `JOB_NAMES` string). The confirm modal calls it `Hardcover identifier backfill`.

## Job 3 — Phase-2 author goodreads_id backfill

**What it does.** Reverse-lookup pass: for each author missing a `goodreads_id` but with at least one book that carries a resolvable identifier, look the author's ID up via Goodreads. Reuses the same `backfill_missing_author_ids` sweep used elsewhere in the codebase.

**When it runs.** Manual, per-library. Per-run budget is capped at 200 candidates per pass — first-run against a library with hundreds of audiobook-only authors finishes inside ~10–15 minutes wall-time at Goodreads' 5-second-plus-jitter rate-limit rather than ~70 minutes. Re-clicking Hygiene picks up the next batch.

**Side effects.** Seshat-only — fills `authors.goodreads_id` on the per-library DB. Soft-block-aware via the standard Goodreads resolver; skipped authors stay candidates for the next run.

## Job 4 — Book deduplication

**What it does.** Two passes against per-library `books`. **Pass A** merges any two rows sharing a non-null `goodreads_id`, `hardcover_id`, `isbn`, `amazon_id`, `audible_id`, or `asin`. **Pass B** runs `_dedupe_same_series_position` to catch the "Remnant II" vs "Remnant Book 2" case where two rows share `(series_id, series_index)` but the titles don't fuzzy-match.

**When it runs.** Manual, per-library.

**Side effects.** Seshat-only — deletes loser rows from per-library `books` after COALESCE-folding identity columns onto the winner. The winner keeps its row id, so any pre-existing references survive (work_links in the pipeline DB reconcile on the next works-matcher pass). Hidden books are excluded from comparison; merging a hidden row into an active row would surface the unwanted metadata under the kept id. Not audit-logged; logged at INFO level per merge.

## Job 5 — Series consolidation

**What it does.** Intra-author canonical-form series merge — collapses two series rows for the same author whose names canonicalize to the same form ("Mistborn" vs "The Mistborn Saga"). Reuses the helper that also runs at `init_db` time. A post-pass empty-series cleanup catches anything orphaned by the merge.

**When it runs.** Manual, per-library. Most productive after Jobs 2 and 4, because Hardcover-stamped IDs can newly join two siblings under the same author.

**Side effects.** Seshat-only — deletes loser series rows from per-library `series` after re-pointing `books.series_id`. Not audit-logged.

## Job 6 — ABS author cross-stamp

**What it does.** For every author in an audiobook library missing a `goodreads_id` / `hardcover_id` / `openlibrary_id` / `google_books_id`, looks up an author row from any ebook library with the same normalized name and COALESCE-fills the missing columns.

**When it runs.** Manual, runs once cross-library (not per-library). No-op if there isn't both ≥ 1 ebook library and ≥ 1 audiobook library.

**Side effects.** Seshat-only — fills `authors.{source}_id` columns on per-library audiobook DBs. Strictly name-equality; never overwrites a populated slot.

**Pre-flight notes.** This is the older, name-only cross-stamp. Job 8 is the [person-](../../CONTEXT.md#library-identity-and-sync) and `author_links`-aware successor and supersedes it in the steady state — but Job 6 is kept as a safety net for authors not yet linked to a person. Once Job 7 retrolinks them and Job 8 mirrors, Job 6 reaches no-op territory.

## Job 7 — Orphan author retrolink

**What it does.** Walks every library's `authors` and, for any row that lacks an `author_links` entry, calls `get_or_create_person` so it joins the cross-library identity graph. Catches stub rows created by the v2.12.1 dual-row mirror that bypassed the identity hook, and authors created by the live mirror path that no subsequent sync has touched.

**When it runs.** Manual, runs once cross-library.

**Side effects.** Inserts into the global `persons` and `author_links` tables when no link exists; reuses an existing `persons` row when a normalized-name match (exact or fuzzy ≥ 0.92) is available. Seshat-only. Not audit-logged.

**Pre-flight notes.** Runs before Job 8 so newly-linked orphans participate in the subsequent source-ID mirror pass.

## Job 8 — Cross-library person backfill

**What it does.** Walks every multi-link person and mirrors NULL source-ID values across linked sibling `authors` rows. For the rare case where two siblings hold *different* values for the same source, applies an **ebook-wins** conflict policy (tiebreak by slug, alphabetical) and audit-logs the displaced value to `author_id_audit_log` so it's recoverable. Then backfills `persons.bio` from the longest non-empty sibling bio, and recomputes `link_confidence` flags so the Author Triage page doesn't surface stale low-confidence cards after the mirror tightens identity.

**When it runs.** Manual, runs once cross-library.

**Side effects.** Mirror writes hit per-library `authors`; bio backfill writes `persons.bio`; conflict resolution writes `author_id_audit_log` and overwrites the losing sibling's column. Recomputed flags rewrite `author_links.link_confidence`. Seshat-only.

**Pre-flight notes.** Distinct from Job 9: Job 8 only *mirrors* source-ID values **across rows already linked to the same person**. It does **not** merge persons. See [ADR-0016](../adr/0016-author-image-source-rank-and-mirror.md) for the parallel image rules (which use a separate, rank-aware helper rather than this NULL-fill mirror).

> **Job 8 historical note (v3.2.0).** Pre-v3.2.0 Job 8 also performed a substring-blacklist clear of `/books/`-path author images — the John-Birmingham book-cover-as-author-photo workaround. That step was retired and replaced by the standalone Job 11. If an operator has dashboards keyed on the old Job 8 image-clear behaviour, the relevant stats now live on Job 11. See the stat-key migration note at Job 11.

## Job 9 — Consolidate persons by shared source ID

**What it does.** Finds groups of **distinct `persons` rows that share a `(source, source_id)` value via their linked per-library `authors` rows** and merges them into one. Winner is the lowest `person_id` in the group; losing-person `author_links` are repointed at the winner, then the losing `persons` rows are deleted. Audit-logged per pair to `person_merges` (winner, loser, anchoring `(source, source_id)`, moved link count, loser canonical name). See [ADR-0015](../adr/0015-source-id-aware-author-identity.md) — this is slice 05 of the source-ID-aware-identity arc and back-applies slice 03's ID-first runtime consolidation across already-existing data.

**When it runs.** Manual, runs once cross-library, immediately after Job 8 so the source-ID columns are at their most complete.

**Side effects.** Mutates `persons` (DELETE losers) and `author_links` (UPDATE person_id to winner) in the global DB. Audit-logged to `person_merges` and recoverable (see [merge audit tables](#what-to-do-if-a-job-fails)). For the multi-author/series context this job presupposes, see [Persons and `author_links`](./multi-author-and-series.md).

**Pre-flight notes (verbatim — must read before first-run after upgrade).** Job 9 mutates `persons` + `author_links` across the cross-library identity layer. Audit-logged to `person_merges` (recoverable) **but back up `seshat.db` and the per-library `seshat_<slug>.db` files before the first run.** The first run after upgrade back-applies the slice-03 ID-first consolidation across existing data — the dev-stack first run on Mark's library merged 5 real pre-existing prod split-persons in addition to a synthetic test case. The behaviour is correct: separate `persons` rows that already shared a source ID get folded into one. **Expect the `persons` row count to drop on first run, then stay flat on every subsequent run.**

Distinct from Job 8: Job 8 mirrors a source-ID *across siblings already linked to the same person*; Job 9 merges *separate persons* that share a source ID. The two are complementary — Job 8 has nothing to mirror onto if Job 7 hasn't yet linked the orphans, and Job 9 has nothing to merge until Job 8 has propagated the IDs. Order matters; the coordinator runs them 7 → 8 → 9.

## Job 10 — Prune orphan author_links

**What it does.** Drops global `author_links` rows whose per-library author row no longer exists, and removes any `persons` rows that become unreferenced as a result. Safety net for the rare case where an author was deleted via a path that bypassed Job 1's cascade.

**When it runs.** Manual, runs once cross-library.

**Side effects.** Seshat-only — DELETE on `author_links` and `persons` in the global DB. Not audit-logged; a logger.info line records the dropped count.

> **Progress banner:** displayed as `Prune orphan author links` (backend `JOB_NAMES` string). The confirm modal calls it `Prune orphan author_links`.

## Job 11 — Image URL health check

**What it does.** Walks every populated `authors.image_url` in every per-library DB and applies two clears. **(1) Substring blacklist:** any URL containing `/books/` or `nophoto` is NULLed — the former is the long-standing Goodreads book-cover-as-author-photo regression (the John-Birmingham failure mode that Job 8 used to handle); the latter is Goodreads' placeholder URLs for authors without a photo. **(2) HEAD-verify:** the remaining URLs are HEAD-requested in parallel; any non-200 (including timeouts and connection errors) NULLs the row. See [ADR-0016](../adr/0016-author-image-source-rank-and-mirror.md) §6.

This is **local-clear-only**: clears the per-library row in place; does **not** fan a NULL through linked siblings or the `persons` row. The next scan re-establishes coherence via `mirror_image_url`'s rank-aware overwrite. (Forcing a NULL fan-out would also clear siblings whose own URL might still return a 200, and operator-edited per-library overrides would also be wiped.)

**When it runs.** Manual, runs once cross-library, near the end of the chain.

**Side effects.** Seshat-only — UPDATEs `authors.image_url = NULL, image_url_source = NULL` on per-library DBs. Not audit-logged. Reversible in the sense that the next discovery scan will repopulate the slot from any source that captures an image; nothing destructive sits in the gap.

**Pre-flight notes (verbatim — must read before first-run after upgrade).** The first Hygiene click after upgrade HEAD-verifies ~1500 author images against CDNs (Goodreads `images.gr-assets.com`, Amazon `m.media-amazon.com`, etc.) using an 8-connection pool with 5-second timeouts; the full pass completes in seconds, not minutes. Substring-clears any `/books/`-path entries via the blacklist (legacy Goodreads selector bug). **Local-clear-only — no fan-out to siblings.**

> **Stat-key migration (v3.2.0).** The v2.22.0 `broken_image_urls_cleared` stat — the Job 8 substring workaround — has been **replaced by two separate buckets on Job 11**: `image_urls_blacklisted_path` (substring-cleared) and `image_urls_head_failed` (HEAD-verified dead). Dashboards and monitoring keyed on the old name will need to be re-pointed.

## Job 12 — Soft-delete retention sweep

**What it does.** Filesystem-only sweeper for the [active replacement](./active-replacement.md) opt-in upgrade flow. Walks every library's `.seshat-replaced/<timestamp>/` subdirectories and purges anything older than `active_replacement_soft_delete_retention_days` (default 30). Anything malformed (a non-timestamp directory name, an unreadable subtree) is counted separately and left in place.

**When it runs.** Manual, runs once cross-library, last in the chain.

**Side effects.** Filesystem deletes under each library's `.seshat-replaced/<ts>/` tree. Seshat-only — does not write to Calibre or ABS. The soft-deleted files exist outside of either canonical store; once purged, the upgrade is no longer reversible by restore. Logged at INFO level; per-job stats: `soft_deletes_purged`, `soft_deletes_kept`, `soft_deletes_malformed`, `soft_deletes_errors`.

**Pre-flight notes.** See [Active replacement](./active-replacement.md) for the soft-delete model — that chapter documents the quality-scoring inputs, the per-library opt-in, and the `.seshat-replaced/<ts>/` layout. **This sweeper deletes irreversibly outside the retention window**; if a recently-replaced book is one you want to roll back, run Hygiene only after restoring it.

> **Numbering note (v3.2.0).** Job 12 was Job 11 prior to v3.2.0. Adding the image URL health check at slot 11 pushed soft-delete down one position; `TOTAL_JOBS` rose from 11 to 12.

## What to do if a job fails

Hygiene jobs catch their own exceptions and continue. A failed job appends a `<job>: <ExceptionType>: <message>` entry to the chain's `errors` list rather than aborting the run; the chain finishes the remaining jobs, and the completion toast switches from `success` to `warning` with the same summary line.

**Where logs land.** The Python logger `seshat.discovery.hygiene` carries every per-job INFO / WARNING / EXCEPTION line in container logs. Job 9 person merges are logged at INFO under the same logger as `hygiene: consolidate-persons-by-source-id: merged loser_person_id=... into winner_person_id=... via <source>=<value>`. Job 11's blacklist/HEAD-fail counts log as `hygiene: image-url-health: blacklisted=N head_failed=N`. The coordinator's final summary logs as `hygiene: run_all complete: <stats dict>`.

**Live status.** `GET /api/discovery/hygiene/status` returns a point-in-time JSON snapshot (running flag, current job index + name + library, per-job summary). `POST /api/discovery/hygiene/cancel` cancels an in-flight chain.

**Audit tables (recoverable history).** Two tables retain merge history across runs:

- **`person_merges`** — one row per [Job 9](#job-9--consolidate-persons-by-shared-source-id) merge, with `(winner_person_id, loser_person_id, reason='consolidate_by_source_id', source, source_id, moved_links, loser_canonical_name, merged_at)`. Sufficient to reconstruct what merged into what and via which source-ID anchor; restoring a merged-away person means re-inserting the row from your `seshat.db` backup and repointing the `moved_links` count of `author_links` rows back at it. This is why the Job 9 pre-flight asks for the backup.
- **`book_merges`** — analogous shape for book-level merges (used by Job 4's identifier-keyed dedup at the per-library level; recorded on the per-library DB).

**When a job's counter looks wrong.** Re-run the chain. Every job is idempotent, so a second click is the cheapest way to confirm whether a non-zero count is real ongoing work or a one-time catch-up. If counts don't drop to zero on the second run, that's a signal worth opening an issue with — paste the per-job lines from `seshat.discovery.hygiene` and the `hygiene: run_all complete:` summary.
