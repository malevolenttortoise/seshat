# Metadata cache

Two of Seshat's external sources — Amazon and Goodreads — sit behind
bot-management defenses that make synchronous, on-demand calls
unreliable. For both, Seshat keeps a **per-source metadata cache**
that decouples user-facing scan latency from source-side rate-limit
or throughput pain.

This chapter covers the cache architecture: the read/write split, the
on-disk shape, the two workers' divergent postures, first-fill UX,
the status UI, settings, and notifications. For per-source enrichment
behavior (the Goodreads Cloudflare bypass, the resolver chain, the
GR-goes-silent runbook), see
[metadata-sources.md](metadata-sources.md).

Cache terms used below — **metadata cache**, **cache worker**,
**first-fill**, **list-page cache**, **detail cache**, **budget
exhaust** — are defined in
[CONTEXT.md → Metadata caching](../../CONTEXT.md#metadata-caching).
The design rationale is in
[ADR-0018](../adr/0018-metadata-cache-goodreads-list-page.md).

## Architecture overview

Most sources (Hardcover, Audible, Open Library, Google Books, Kobo,
IBDB) are generous enough to be called synchronously at scan time —
a few requests per second, no aggressive bot management. Amazon and
Goodreads aren't:

- **Amazon** sits behind Akamai with an IP-level daily budget
  (~200–400 successful author scans/day), multi-tier soft-block
  detection (HTTP 429, HTTP 202 sensor challenge, thin-body
  interstitial, full-chrome CAPTCHA), and cumulative scoring that
  penalizes density even when individual responses look clean.
- **Goodreads** soft-degrades on detail-fetch density: a prolific
  author's scan can take 25–40 min and silently drop books when the
  per-author time budget exhausts before all detail fetches complete
  (the [budget-exhaust](../../CONTEXT.md#metadata-caching) failure
  mode).

For both, the synchronous scan reads from cache; the live source only
runs from a background worker on a paced schedule.

```
              ┌────────────────────────────────────────────┐
 USER         │ Synchronous read flow                      │
 SCAN ──────► │   lookup.py walks sources in order         │
              │   Hardcover / Audible / OpenLibrary /      │
              │   GoogleBooks / Kobo / IBDB — all live.    │
              │                                            │
              │   Amazon: CachedSource("amazon")           │
              │     reads metadata_cache_amazon.db         │
              │   Goodreads: CachedSource("goodreads")     │
              │     reads metadata_cache_goodreads.db      │
              │     for the list-page inventory; per-book  │
              │     detail still fetched live (Path B).    │
              │                                            │
              │   Cache miss → enqueue at priority 1000,   │
              │   return None for the current scan.        │
              └────────────────────────────────────────────┘

                                ▲
                                │ reads cache
                                │
                                │ writes cache
              ┌────────────────────────────────────────────┐
 BACKGROUND   │ Async worker flow                          │
              │                                            │
              │   Amazon worker (metadata_cache_worker)    │
              │   • cooldown escalation 600→1800→3600s     │
              │   • behavioral warmup, per-author session  │
              │     rotation (Chrome120)                   │
              │   • 30–90s think-time jitter               │
              │   • writes per-(author × library) book set │
              │                                            │
              │   Goodreads worker                         │
              │   • single 300s cooldown (no escalation)   │
              │   • list-page-only fetch (never /book/show)│
              │   • paced by Goodreads source's rate_limit │
              │   • writes per-page snapshot               │
              │                                            │
              │   Both: heartbeat, gate checks, priority   │
              │   pop, scan, fan-out, cache write, jitter, │
              │   honor cooldown — under state.supervised  │
              │   _task for auto-restart on crash.         │
              └────────────────────────────────────────────┘
```

Both workers are **disabled by default**. A fresh deploy reads cache
(always miss until you opt in), never hits Amazon or Goodreads's
list-page endpoint from the worker, and falls back to the
synchronous live path for Goodreads detail. Opt-in is per-source
via Settings → Sources → *source* → Cache Status card.

## What lives where

| File | Responsibility |
|---|---|
| `app/discovery/sources/amazon.py`, `app/discovery/sources/goodreads.py` | The live sources. Still used — but only by the workers under the cache architecture. The synchronous lookup flow no longer instantiates them directly. |
| `app/discovery/metadata_cache.py` | Per-source SQLite DB scaffold. Owns the source-templated table shape: `_TABLE_NAMES[source]`, the `SUPPORTED_SOURCES` frozenset, the migration list, and the `per_source_table_suffixes(source)` helper. |
| `app/discovery/metadata_cache_reader.py` | `CachedSource(source_name=...)` — drop-in for the live source in `lookup.py`. Returns cached records on hit; enqueues + returns `None` on miss. Applies read-time filters (language, format, owned-only) where applicable. |
| `app/discovery/metadata_cache_worker.py` | Background worker(s). One `tick()` per iteration: heartbeat, gate checks, queue pop, scan, fan-out, cache write, next-sleep jitter. One worker per enabled source. Runs under `state.supervised_task` so a crash auto-restarts. |
| `app/routers/metadata_cache.py` | REST surface: `GET /status` (full state + queue + stats), `PATCH /settings`, `POST /reset-cooldown`, `GET /author/{author_id}`, `GET /recent-discoveries`. All parameterized by `source`. |
| `app/orchestrator/scheduler.py` | APScheduler jobs: per-source stall watchdog (every 2 min, error ntfy if heartbeat stale), per-source daily summary (zeros `today_*` counters, opt-in ntfy). |

## The cache DBs on disk

Two DB files in `DATA_DIR`, alongside `seshat.db`. Roughly 10–50 MB
for Amazon (per-book detail) on a 600-author library; ~3.5 MB for
Goodreads (list-page-only) on the same library.

The two DBs have **the same outer shape** — state / queue /
worker_state — and **divergent inner data shape** — Amazon caches
per-book detail, Goodreads caches per-author-page snapshots. The
divergence is captured in `_TABLE_NAMES[source]` so cross-source
enumeration must use `per_source_table_suffixes(source)` rather than
hardcoded suffix tuples.

### `metadata_cache_amazon.db`

| Table | PK | Purpose |
|---|---|---|
| `metadata_cache_amazon_state` | `(author_id, library_slug)` | One row per (author × library). Last-scan timestamp + outcome + book count + error. |
| `metadata_cache_amazon_books` | `(author_id, library_slug, book_asin)` | The actual book rows the cache reader hands back. FK CASCADE from the state row. |
| `metadata_cache_amazon_queue` | `author_id` | Schema-v2: PK is `author_id` alone. Same author across two libraries collapses to one queue row; the worker scans once with `format_filter="allFormats"` and partitions per library at write time. |
| `metadata_cache_amazon_worker_state` | `id = 1` singleton | Heartbeat, cooldown state, today's scan + block counts. Survives restarts. |

### `metadata_cache_goodreads.db`

| Table | PK | Purpose |
|---|---|---|
| `metadata_cache_goodreads_state` | `(author_id, library_slug)` | One row per (author × library). Last-scan timestamp + outcome + page count + error. |
| `metadata_cache_goodreads_list_pages` | `(author_id, library_slug, page_num)` | One row per fetched list page. Stores `fetched_at` + `book_ids_json` — the cached **inventory**, not the books themselves. ~5 KB/author across all pages. |
| `metadata_cache_goodreads_queue` | `author_id` | Same dedup shape as Amazon: one queue row per author across libraries. |
| `metadata_cache_goodreads_worker_state` | `id = 1` singleton | Heartbeat, today's scan / block counts, and the `today_budget_exhaust_count` counter for operator visibility on the [budget-exhaust](../../CONTEXT.md#metadata-caching) signal. |

You can inspect any of these in the Database Manager (Settings →
Database) — both cache DBs show up alongside the main library DBs.

### Schema-v2 dedup

Pre-v2.21.0 Amazon used `(author_id, library_slug)` as the queue PK.
For a 600-author library where ~all authors live in both calibre +
abs, that doubled the Akamai request budget. Schema-v2 collapses to
`author_id` only: one queue row per author, one `allFormats` scan
per iteration, partitioned downstream into per-library state + book
rows based on each library's `content_type`. Result: ~50% fewer
Amazon requests for the same coverage. The Goodreads queue inherits
the same shape from day one.

## Worker behavior baseline

Both workers share a common tick loop:

1. Heartbeat the `worker_state` row.
2. Gate checks: worker enabled? schedule active (if `mode="scheduled"`)?
   not currently inside a cooldown window?
3. Pop the highest-priority queue row.
4. Scan via the live source (fresh session if applicable).
5. Fan-out into per-library state + data rows.
6. Persist the cache write atomically.
7. Sleep for the configured jitter window.

A crash inside the tick is caught by `state.supervised_task` and the
worker restarts; the queue row is unchanged so the next iteration
retries. The two workers differ in **what they scan**, **how they
pace**, and **how they react to soft-blocks**.

### Amazon worker posture

After the 2026-05-22 Akamai investigation (Arm 1 / Arm 3 experiments),
the Amazon worker uses six concrete tactics:

1. **Per-author session rotation** — fresh `AsyncSession` per author,
   single `chrome120` profile. The foundational technique
   (Arm 3 result: 5/5 OK).
2. **Behavioral warmup** — one GET to `amazon.com/` before the first
   `/stores/author` call on each new session. ~200ms cost.
3. **Adaptive cooldown escalation** — 600s → 1800s → 3600s within a
   1h window. Counter resets after 1h blockless.
4. **Think-time jitter** — 30–90s randomized spacing between
   iterations.
5. **Priority queue, no exclusion** — every author with an
   `amazon_id` lives in the queue. Recent activity, user manual
   forces, and GR-sparse coverage boost priority. Dormant authors
   still get reached; nobody is excluded.
6. **HTTP 202 detection** — Akamai's sensor challenge variant. Trips
   the cooldown alongside 429 / thin-body / full-chrome CAPTCHA
   classes.

Profile rotation (rotating impersonation profiles within one IP) was
explicitly **dropped** — Arm 1 was strictly worse than Arm 3, evidence
that profile churn is itself a bot signal.

The Amazon cooldown is shared across three layers and persists across
container restarts:

1. **Module-level state**
   (`app/discovery/amazon_author_id_resolver.py`) — `_blocked_until`,
   `_block_reason`, `_block_count`. All Amazon call sites short-circuit
   when `is_amazon_blocked()` returns True.
2. **Persistence** — `settings.json` runtime-state keys
   `amazon_blocked_until` / `amazon_block_reason` /
   `amazon_blocked_since`. Hydrated on module import via
   `_load_persisted_block_state`. Protected from user PATCH alongside
   `goodreads_session_*`.
3. **Worker state** (`metadata_cache_amazon_worker_state` row) —
   `last_block_at`, `block_cooldown_s`, `consecutive_blocks`. The
   worker's tick reads this to decide escalation tier and to defer
   queue rows past the cooldown.

Soft-block triggers, any of:

- HTTP 429 with `Retry-After`
- HTTP 202 sensor challenge (Akamai's "is this a bot?" interstitial)
- HTTP 200 with body ≥50KB but no `ProductGrid` marker — the
  full-chrome CAPTCHA shim, distinguished from "thin body" by size
  (parser raises `SoftBlockSuspectedError`)
- Thin-body interstitial (<50KB at any allbooks call site)

#### Realistic throughput

Arm 3 sustained 5 successful GETs in 8 min. We don't sustain that
because Akamai's long-window scoring kicks in eventually, but
**200–400 successful scans/day** is realistic. For a 600-author
library:

- Full refresh cycle: 2–4 days
- High-priority authors (recent activity): roughly daily cadence
- Dormant authors: weekly-ish

### Goodreads worker posture

The Goodreads worker has a **different cost shape**. Goodreads
doesn't have a hard wall like Akamai; it soft-degrades on detail
density. The worker's job is to keep the **list-page inventory**
fresh so scans are instant, not to pre-fetch detail.

- **Single 300s cooldown**, no escalation curve. Triggered by 202 /
  503 / empty 2xx, same family as the synchronous bypass.
- **List-page-only fetch.** The worker never hits `/book/show/{id}`;
  it scrapes `/author/list/{id}` and stores the page snapshot
  (`fetched_at` + `book_ids_json`). Per-book detail still goes
  through the live `GoodreadsSource` HTTP path under
  `metadata-sources.md`'s Chrome120 bypass.
- **Hybrid Path B read.** A cache HIT short-circuits both the
  list-page fetch and author validation; the live detail loop then
  runs against the cached IDs in priority order. This eliminates the
  Sanderson silent-drop gap (every list-page book ID is preserved
  across re-scans) without committing to per-book detail storage.
- **`today_budget_exhaust_count` counter** lives on the worker state
  row and is incremented at the
  `[goodreads] giving up on '<author>'` log point. The daily summary
  surfaces this so the
  [budget-exhaust](../../CONTEXT.md#metadata-caching) signal moves
  from log-grep range into operator-readable telemetry. It is the
  primary data signal for whether full per-book detail caching (Path
  C) is worth building in a later version — see
  [ADR-0018](../adr/0018-metadata-cache-goodreads-list-page.md).

Storage projection for a 700-author library: ~3.5 MB
(~5 KB/author × 700) of list-page snapshots.

When the Goodreads source goes silent at the live-detail layer (not
the worker's list-page layer), the operator runbook for probing /
clearing the soft_blocked flag is in
[metadata-sources.md → Runbook](metadata-sources.md#runbook--what-to-do-when-goodreads-goes-silent).

## First-fill UX

When a cache miss happens on a synchronous scan, the cache reader
**enqueues the author at priority 1000** (front of queue) and
returns `None` for that scan. The user-facing flow:

1. The first scan after enabling a worker returns nothing for any
   cached source — every author is a miss. Other live sources still
   contribute, so the scan isn't empty.
2. Author pages show a cache badge:
   - "*source*: never scanned" → priority 1000, will be next
   - "*source*: in queue" → behind one or more priority-1000 authors
3. The worker ticks: for Amazon, 30–90s jitter means ~2–3 authors per
   minute peak; for Goodreads, paced by the source's `rate_limit`
   (5.0s default).
4. As cache fills, subsequent scans short-circuit instantly for
   cached authors and return the cached book set.
5. Over **2–4 days** the Amazon cache reaches steady state for a
   600-author library. The Goodreads cache fills much faster (one
   list-page request per author, no per-book detail).

After steady state, scans complete in <2s for cached authors instead
of the 10–30s typical of the synchronous live path.

## Status UI

The cache surfaces in four tiers, each more focused than the last.
The per-source panel and the per-author badge are parameterized — a
`CacheStatusCard({sourceKey})` component renders the same shape for
both Amazon and Goodreads.

- **Tier 1 — Global status icon** in the navbar. Aggregates across
  all enabled cache sources. Color-coded green / yellow / red / gray.
  Click → Settings → Sources. Stays out of your way when healthy.
- **Tier 2 — Cache Status card** under each source row in the
  Metadata Sources panel. Worker enable toggle, queue depth, today's
  scan count, last block, cooldown reset button. Source-specific
  fields appear here too (Amazon's `format` / `language`; Goodreads's
  `today_budget_exhaust_count`).
- **Tier 3 — Per-author cache badge** on author detail pages. One
  badge per enabled cached source. Examples: "Amazon: scanned 3d
  ago, 12 books cached" / "Amazon: in queue" / "Amazon: cooldown,
  retry in 8m" / "Goodreads: list page cached 4h ago, 18 books"
  / "Goodreads: never scanned".
- **Dashboard cache rail** at the bottom of the Seshat Stats widget.
  Recent-discoveries list ("Found 'Honor of Duty 2' for A. R. Rend,
  2h ago" / "Cached GR list page for Brandon Sanderson, 1h ago").

## Operator interventions

- **Reset cooldown** — Cache Status card → Reset cooldown button.
  Clears `block_cooldown_s` and `consecutive_blocks` on the worker
  state row + the persisted `*_blocked_*` runtime-state keys. Use
  sparingly: cooldowns are usually justified by an actual block
  response, and resetting one mid-window often just re-triggers the
  same block at the next tick.
- **Enable / disable worker** — Cache Status card toggle, or
  `PATCH /api/v1/metadata-cache/settings` with `enabled=false`.
  Synchronous reads keep using `CachedSource`; cache stays warm; no
  new fills.
- **Format / language settings (Amazon)** — `format`, `language`,
  `audiobook_format` on the Amazon Cache Status card. Schema-v2
  always scans `allFormats` and partitions at write time, so the
  ebook `format` mostly drives read-time filtering rather than the
  live request shape.
- **Goodreads probe runbook** — when the Goodreads source goes
  silent at the live-detail layer, see
  [metadata-sources.md → Runbook](metadata-sources.md#runbook--what-to-do-when-goodreads-goes-silent).
  Probe / burst probe / mark-active live under the source panel,
  not the cache panel; the worker is downstream of whatever the
  session module reports.
- **Inspect via API** — `GET /api/v1/metadata-cache/status?source=...`,
  `GET /api/v1/metadata-cache/author/{author_id}?source=...`,
  `GET /api/v1/metadata-cache/recent-discoveries?source=...`,
  `POST /api/v1/metadata-cache/reset-cooldown?source=...`.
- **Database Manager** — Settings → Database surfaces both cache DBs
  for inspect / backup / wipe / vacuum alongside the main library DBs.

## Settings keys

Both sources have a **nested sub-tree** with identical shape, seeded
in `DEFAULT_SETTINGS["metadata_cache"]`:

```
metadata_cache.amazon.enabled = false
metadata_cache.amazon.mode = "disabled"
metadata_cache.amazon.schedule.active_hours = "10:00-22:00"
metadata_cache.amazon.schedule.timezone = ""
metadata_cache.amazon.format = "kindle"
metadata_cache.amazon.audiobook_format = "audible_audiobook"
metadata_cache.amazon.language = "English"

metadata_cache.goodreads.enabled = false
metadata_cache.goodreads.mode = "disabled"
metadata_cache.goodreads.schedule.active_hours = "10:00-22:00"
metadata_cache.goodreads.schedule.timezone = ""
```

Both default to `enabled=false` and `mode="disabled"` — **zero live
behavior change** on a v3.4.0 upgrade. Opt in per source via the
Cache Status card.

Existing installs are deep-seeded on first post-upgrade load: missing
sub-keys back-fill from defaults; saved sub-keys are preserved. A
saved `metadata_cache.amazon.mode = "scheduled"` keeps that value and
the new `goodreads` sub-tree appears alongside it without overwriting.

The legacy flat `metadata_cache_amazon_enabled` key still works as a
fallback — the worker reads the nested key first, then falls through
to the flat key, then to the default. Existing operators don't have
to migrate; new keys take precedence when they exist.

## Notifications

The cache emits four cache-specific notification event keys, both
sources sharing the shape:

- `cache.scan.*` — per-scan outcome events (block, soft_block,
  permanent_fail, new_book)
- `cache.stall.*` — stall-watchdog events fired from the
  APScheduler job when the worker is enabled but its heartbeat goes
  stale (default threshold 300s)
- `cache.daily.*` — once-per-day digest of today's scans + blocks +
  (Goodreads only) `today_budget_exhaust_count`

The dotted-namespace structure plugs into the v2.28.0 notification
taxonomy: a `cache.*` wildcard routes every cache event to one
channel; a `cache.stall.amazon` precise key routes one source's
stall alerts to a higher-priority channel. The full routing model
— event registry, wildcards, per-event priority, quiet hours — is in
[notifications.md](notifications.md). This chapter only names the
events; routing is owned by the notifications chapter.

Optional rotated file handler
(`metadata_cache_log_file_enabled`, default OFF) writes to
`DATA_DIR/logs/metadata_cache_worker.log` with `RotatingFileHandler`
defaults of 1 MB × 3 rotations.

## Telemetry log format

Every tick emits a `[scan]` summary line under the
`seshat.discovery.metadata_cache_worker.<source>` namespace:

```
[scan] author=B0DTZ51PHW outcome=ok books=12 new=1 libraries=2 elapsed_ms=872 [calibre-library=8, abs-audio-library=4]
[scan] author=B0COOLDOWN outcome=soft_block consecutive=2 cooldown_s=1800 escalated=true elapsed_ms=251
[scan] author=B0FAIL0001 outcome=permanent_fail consecutive_failures=5 permanent=true elapsed_ms=412 error='HTTP 503'
```

`outcome` values: `ok` / `soft_block` / `permanent_fail` /
`empty_result` / `skipped_cooldown`. Both workers share the format;
source-specific fields appear as extra key=value pairs.
