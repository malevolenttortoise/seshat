# Metadata sources

Seshat enriches every book it touches by querying a chain of external
metadata providers in priority order. This document covers:

- The two-flow architecture (synchronous read flow + async worker flow)
- The per-source cache layer that decouples expensive scans from
  user-facing scans
- Each individual source — what it covers, where it fits, how it
  fails, and how to disable or tune it

The unified Metadata Sources panel (Settings → Sources) is the
authoritative editor for everything below. The legacy per-source flat
keys (`goodreads_enabled`, `rate_goodreads`, etc.) are still synced for
backward compatibility but should not be edited directly.

## Architecture overview

Most metadata sources (Goodreads, Hardcover, Calibre, ABS, IBDB,
Google Books, Open Library, Audible) are generous enough to be called
**synchronously, on demand** at user scan time. They're rate-limited
in the polite-friend sense — a few requests per second, no aggressive
bot management.

Amazon is fundamentally different. It sits behind Akamai with an
IP-level daily budget (~200-400 successful author scans/day),
multi-tier soft-block detection (HTTP 429, HTTP 202 sensor challenge,
thin-body interstitial, full-chrome CAPTCHA), and cumulative scoring
that penalizes density even when individual responses look clean.
Calling Amazon synchronously at scan time means:

1. A multi-author scan that touches Amazon once per author can cascade
   into a hard cooldown halfway through.
2. The cooldown logic has to thread through every layer of the lookup
   orchestration (search → enrich → author bibliography → ID
   resolution).
3. The user waits 30+ seconds per author for a network round-trip that
   may not even succeed.

Starting with **v2.21.0**, Amazon (and any future source that needs
similar treatment) is decoupled into a separate **read/write split**:

```
              ┌──────────────────────────────────────┐
 USER         │ Synchronous read flow                │
 SCAN ──────► │   lookup.py walks sources in order   │
              │   Goodreads / Hardcover / Calibre /  │
              │   ABS / IBDB / Google Books / OL /   │
              │   Audible — all called live.         │
              │                                      │
              │   Amazon: instead of a live call,    │
              │   read from metadata_cache_amazon.db │
              │   (last-known scan results).         │
              │   Cache miss → enqueue at priority   │
              │   1000, return None for this scan.   │
              └──────────────────────────────────────┘

                              ▲
                              │ reads cache
                              │
                              │ writes cache
              ┌───────────────────────────────────────┐
 BACKGROUND   │ Async worker flow                     │
 WORKER ────► │   metadata_cache_worker.py            │
              │   • Pop highest-priority queue row    │
              │   • Fresh chrome120 curl_cffi session │
              │   • Behavioral warmup (one GET to     │
              │     amazon.com/ first)                │
              │   • Scan via AmazonSource             │
              │   • Partition response per library    │
              │     (kindle → ebook, audible →        │
              │     audiobook)                        │
              │   • Write per-library state + books   │
              │   • Honor cooldown / 202 / escalation │
              │   • 30-90s think-time jitter          │
              │     between iterations                │
              └───────────────────────────────────────┘
```

### What lives where

| File | Responsibility |
|---|---|
| `app/discovery/sources/amazon.py` | Live `AmazonSource` — still used, but only by the worker. The synchronous lookup flow no longer instantiates it. |
| `app/discovery/metadata_cache.py` | Per-source SQLite DB scaffold. Owns the four tables: `metadata_cache_amazon_state`, `_books`, `_queue`, `_worker_state`. Schema migrations live here. |
| `app/discovery/metadata_cache_reader.py` | `CachedSource(source_name="amazon")` — drop-in for the live `AmazonSource` in `lookup.py`. Returns cached books on hit; enqueues + returns `None` on miss. Applies read-time filters (language, format, owned-only). |
| `app/discovery/metadata_cache_worker.py` | Background worker. One `tick()` per iteration: heartbeat, gate checks, queue pop, scan, fan-out, cache write, next-sleep jitter. Runs under `state.supervised_task` so a crash auto-restarts. |
| `app/routers/metadata_cache.py` | REST surface for the cache: `GET /status` (full state + queue + stats), `PATCH /settings` (enable/disable, format, language), `POST /reset-cooldown`, `GET /author/{author_id}`, `GET /recent-discoveries`. |
| `app/orchestrator/scheduler.py` | APScheduler jobs: `metadata_cache_amazon_stall_watch` (every 2 min, fires error ntfy if heartbeat older than `metadata_cache_stall_threshold_s`), `metadata_cache_amazon_daily_summary` (daily at `metadata_cache_daily_summary_hour`, zeros `today_*` counters + opt-in ntfy). |

### The cache DB on disk

`metadata_cache_amazon.db` lives in `DATA_DIR` alongside `seshat.db`.
Roughly 10-50 MB for a 600-author library. Four tables:

| Table | PK | Purpose |
|---|---|---|
| `metadata_cache_amazon_state` | `(author_id, library_slug)` | One row per (author × library). Last-scan timestamp + outcome + book count + error. |
| `metadata_cache_amazon_books` | `(author_id, library_slug, book_asin)` | The actual book rows the cache reader hands back. FK CASCADE from the state row. |
| `metadata_cache_amazon_queue` | `author_id` | Schema-v2: PK is author_id alone. Same author across two libraries collapses to one queue row; the worker scans once with `format_filter="allFormats"` and partitions per library at write time. |
| `metadata_cache_amazon_worker_state` | `id = 1` singleton | Heartbeat, cooldown state, today's scan + block counts. Survives restarts. |

You can inspect any of these in the Database Manager (Settings →
Database) — the cache DB shows up alongside the main library DBs.

### Schema-v2 dedup (2026-05-22)

Pre-v2.21.0 Phase B used `(author_id, library_slug)` as the queue PK.
For a 600-author library where ~all authors live in both calibre +
abs, that doubled the Akamai request budget. Schema-v2 collapses to
`author_id` only: one queue row per author, one `allFormats` scan
per iteration, partitioned downstream into per-library state + book
rows based on each library's `content_type`. Result: ~50% fewer
Amazon requests for the same coverage.

### Cooldown plumbing — three layers

The Amazon cooldown is shared across three call sites and persists
across container restarts (v2.20.3 fix):

1. **Module-level state** (`app/discovery/amazon_author_id_resolver.py`)
   — `_blocked_until`, `_block_reason`, `_block_count`. All Amazon
   call sites short-circuit when `is_amazon_blocked()` returns True.
2. **Persistence** — `settings.json` runtime-state keys
   `amazon_blocked_until` / `amazon_block_reason` / `amazon_blocked_since`.
   Hydrated on module import via `_load_persisted_block_state`.
   Protected from user PATCH alongside `goodreads_session_*`.
3. **Worker state** (`metadata_cache_amazon_worker_state` row) —
   `last_block_at`, `block_cooldown_s`, `consecutive_blocks`. The
   worker's tick reads this to decide escalation tier and to defer
   queue rows past the cooldown.

Soft-block triggers (any of these):

- HTTP 429 with `Retry-After`
- HTTP 202 sensor challenge (Akamai's "is this a bot?" interstitial)
- HTTP 200 with body ≥50KB but no `ProductGrid` marker — the
  full-chrome CAPTCHA shim, distinguished from "thin body" by size
  (parser raises `SoftBlockSuspectedError`)
- Thin-body interstitial (<50KB at any allbooks call site)

Escalation curve (within a 1h window): first block → 600s, second →
1800s, third → 3600s. Counter resets after 1h blockless.

### ntfy + observability

The worker emits structured log lines under the
`seshat.discovery.metadata_cache_worker.<source>` namespace. Every
tick reaches a `[scan]` summary line:

```
[scan] author=B0DTZ51PHW outcome=ok books=12 new=1 libraries=2 elapsed_ms=872 [calibre-library=8, abs-audio-library=4]
[scan] author=B0COOLDOWN outcome=soft_block consecutive=2 cooldown_s=1800 escalated=true elapsed_ms=251
[scan] author=B0FAIL0001 outcome=permanent_fail consecutive_failures=5 permanent=true elapsed_ms=412 error='HTTP 503'
```

ntfy notifications fire on four event keys (all default OFF for ntfy
itself unless you've configured `ntfy_url` + `ntfy_topic`):

| Event key | Tier | Default | Fires on |
|---|---|---|---|
| `notify_on_metadata_cache_error` | error (prio 5) | ON | Worker stall, cache-write failure, unrecovered tick crash |
| `notify_on_metadata_cache_warning` | warning (prio 4) | ON | Top-tier (3600s) cooldown escalation, author flipped to `failed_permanent` |
| `notify_on_metadata_cache_daily_summary` | info (prio 2) | OFF | Once-per-day digest of today's scans + blocks |
| `notify_on_metadata_cache_new_book` | info (prio 3) | OFF | Per-author celebration when the worker discovers a new ASIN (first-fill scans silenced) |

A separate APScheduler job (`metadata_cache_amazon_stall_watch`)
runs every 2 min and fires the error ntfy if the worker is enabled
but its heartbeat hasn't been updated within
`metadata_cache_stall_threshold_s` (default 300). Self-debounced via
the `metadata_cache.amazon.stall_notified_at` runtime-state key —
clears automatically when the worker recovers.

Optional rotated file handler (`metadata_cache_log_file_enabled`,
default OFF) writes to `DATA_DIR/logs/metadata_cache_worker.log`
with `RotatingFileHandler` defaults of 1 MB × 3 rotations.

### UI surfaces

- **Tier 1 — global status icon** in the navbar. Color-coded
  green/yellow/red/gray; click → Settings → Sources → Amazon. Stays
  out of your way when healthy.
- **Tier 2 — Amazon Cache Status card** under the Amazon row in the
  Metadata Sources panel. Worker enable toggle, queue depth, today's
  scan count, last block, cooldown reset button.
- **Tier 3 — per-author cache badge** on author detail pages.
  "Amazon: scanned 3d ago, 12 books cached" / "Amazon: in queue" /
  "Amazon: cooldown, retry in 8m" / "Amazon: never scanned".
- **Dashboard Amazon Cache rail** at the bottom of the Seshat Stats
  widget. Recent-discoveries list ("Found 'Honor of Duty 2' for
  A. R. Rend, 2h ago").

## At-a-glance source roster

| Source        | Role                              | Auth                | Rate (default) | Cached?  |
|---------------|-----------------------------------|---------------------|----------------|----------|
| MAM           | Owned-data ground truth           | MAM session         | 2.0s           | live     |
| Goodreads     | Authoritative book metadata       | none (v2.13.0 bypass) | **5.0s**     | resolver-only |
| Hardcover     | Rich metadata + Goodreads bridge  | Bearer API key      | 1.0s           | live     |
| Amazon        | Author-Store discovery + enricher | none (curl_cffi)    | worker-paced   | **full cache (v2.21.0)** |
| Open Library  | Free ISBN-keyed fallback          | none                | 1.0s           | live     |
| Google Books  | Broad metadata                    | API key (optional)  | 1.5s           | live     |
| Audible       | Audiobook primary source          | none                | 0.5s           | live     |
| Kobo          | Ebook storefront metadata         | none                | 3.0s           | live     |
| IBDB          | Indie publisher coverage          | none                | 1.0s           | live     |

MAM is always first and locked. Everything else is reorderable and
disable-able per content type (ebook vs audiobook) and per role
(enrich vs scan).

The "Cached?" column distinguishes:

- **live** — called synchronously at scan time
- **resolver-only** — the Goodreads resolver chain caches ID lookups
  in `seshat_id_cache.db` to minimize request volume; the actual
  `/book/show` HTML burst is still synchronous
- **full cache (v2.21.0)** — the source is wrapped in `CachedSource`
  in the synchronous flow; the live source only runs in the worker

## Goodreads (v2.13.0 Stage 6)

Goodreads has the most complete catalog of any of these sources but no
public API. Seshat scrapes the public HTML at `/book/show/{id}` and
`/author/list/{id}` — both **robots-allowed** for the `*` user-agent.
The `/search` endpoint is **explicitly disallowed** and Seshat never
hits it.

### The Cloudflare problem

Goodreads sits behind Cloudflare. From server-side Python clients
(`httpx`, `requests`), Cloudflare's bot manager rejects on the TLS
fingerprint (JA3 check) and returns:

- **HTTP 202** with an empty body, **OR**
- **HTTP 200** with an empty body

…before any real content is fetched. This isn't a rate limit, it isn't
a CAPTCHA, and retrying with the same client doesn't help. The wire-
level signature is the same handshake every request makes — what's
needed is a Chrome-shaped handshake.

### How v2.13.0 fixes it (Phase A)

Seshat now routes every Goodreads request through `curl_cffi`, which
drives `libcurl-impersonate` to replicate Chrome 120's TLS handshake
exactly (cipher suite ordering, BoringSSL extensions, ALPN, h2 frame
patterns). Cloudflare reads the connection as a real Chrome desktop,
the JA3 check passes, and the real page comes back at 1MB+ instead of
the thin-body block-page.

All Goodreads-touching code in Seshat now goes through one central
module — `app/metadata/goodreads_session.py` — so the TLS impersonation,
soft-block detection, runtime-state tracking, and rate-limit jitter are
uniform across:

- The discovery source (`app/discovery/sources/goodreads.py`,
  `/author/list/{id}` and `/book/show/{id}` HTML burst surface)
- The paste-URL importer (`app/discovery/routers/import_export.py`)
- The ID resolver chain's auto_complete tier
  (`app/metadata/goodreads_id_resolver.py`)

### Runtime state + the dispatcher skip

On any soft-block response (202 / empty 2xx), the session module flips
a runtime flag — `goodreads_session_state = "soft_blocked"` — visible
in `settings.json`. Both source-iteration loops (per-book enricher,
per-author scan) check this flag and **skip Goodreads entirely** when
set. Without this gate every iteration pays the full
request → soft-block → next-source roundtrip even after the first
soft-block already told us Goodreads is gated.

The flag clears to `active` automatically on the next successful 200
through the session module, or manually via:

- **Settings → Sources → Goodreads → "Run probe"** — one GET to a
  known-good book. Updates the flag based on the result.
- **Settings → Sources → Goodreads → "Run burst (10×)"** — 10 GETs
  against the canonical Phase-A probe pool at the configured rate
  limit. Surfaces density-based 202s that single probes miss.
- **Settings → Sources → Goodreads → "Mark as active"** — manually
  clear the flag without a probe (use after refreshing IP / waiting
  for Cloudflare's bot-score to decay).

### Weekly canary

A scheduled job (Mondays 03:00 local) does one GET to The Hobbit
(`/book/show/5907`) through the production session module. On 202 it
emits a ntfy notification gated on `notify_on_goodreads_canary_failed`
so users who don't open Settings still notice when Goodreads goes
silent.

### Per-author budget scaling (v2.20.3 Path A)

Sanderson-class authors (≥200 books on the GR list page) hit the
default 25-min per-author budget cap and silently dropped ~37% of
books. The retry loop now scales budget by list-page book count:

- `book_count > 100` → 600s per retry (was 300s), 30 min total budget
- `book_count > 200` → 900s per retry, 40 min total budget
- Default — 300s / 25 min

Helpers no-op for non-Goodreads sources and never shrink an
operator-raised value.

### Caching

To minimize Goodreads request volume regardless of bypass strategy,
Seshat caches resolver outcomes in `DATA_DIR/seshat_id_cache.db`:

- **Book ID** lookups: 30-day TTL on hits, 1-day on misses
- **Author bibliography** lookups: 7-day TTL on hits, 6-hour on misses

The cache is keyed identifier-first (ISBN > ASIN > normalized
title+author). Misses are cached too so dead-end ISBNs don't re-probe
Goodreads every scan within the miss-TTL window. The cache prunes
expired rows during the weekly canary tick.

A full Goodreads cache (mirroring the Amazon cache architecture) is
deferred to v2.22.0 — see the `project_seshat_v222_goodreads_cache_deferred`
note in internal planning if you're a contributor.

### The resolver chain

When the enricher (or any caller) needs a Goodreads book ID for a book
it doesn't already have one for, the ethical resolver runs three tiers
in order. First hit wins; the chain falls through on misses:

1. **Tier 1 — Goodreads `/book/auto_complete?q={isbn_or_asin}`** —
   undocumented JSON endpoint, NOT in the Disallow list. Identifier-
   based, not free-text. Handles most ebook imports since almost every
   epub/azw3 carries ISBN in file metadata.

2. **Tier 2 — Hardcover GraphQL `book_mappings`** (v2.13.0) — when a
   Hardcover API key is configured, one GraphQL roundtrip resolves
   ISBN/ASIN → Hardcover book → `book_mappings` filtered by
   `platform: { name: { _eq: "Goodreads" } }` → external_id. Returns
   the Goodreads ID without ever touching Goodreads. Skipped silently
   when no API key is set.

3. **Tier 3 — Open Library `?bibkeys=ISBN:{isbn}&jscmd=data`** —
   `identifiers.goodreads[0]` for books OL has cross-referenced. Free,
   no key required. Coverage is sparse for recent self-pub indie titles
   but reliable for older / well-cataloged books.

The chain explicitly does NOT fall back to the disallowed `/search`
endpoint, even though some Calibre plugins do. Holding a higher
standard is a deliberate choice.

### Rate limit + when to tune it

Goodreads's default rate is **5.0s + 0–1s jitter**. This is the Phase-A
conservative pace that gives the Chrome120 fingerprint clean headroom
under burst scans. If your burst probe shows zero soft-blocks at 5.0s,
you can dial down to 3.0s for faster scans. If you see soft-blocks
during the burst probe, dial up to 8.0s or higher; the bot manager is
flagging request density.

### What to do when Goodreads goes silent

1. **Run a probe** (Settings → Sources → Goodreads → Run probe). A 200
   means the bypass is working — flag auto-clears.
2. **If still 202**, run a burst probe. If single passes but burst
   fails, raise the rate limit (8s+).
3. **If both fail**, wait 4-12 hours and re-probe. Cloudflare's
   bot-score decays naturally; the Phase-A bypass works for most
   users after a cooldown.
4. **If failures persist for days**, file a GitHub issue. Phase B
   adds an encrypted cookie panel (paste `cf_clearance` + `_session_id2`
   + browser UA from a fresh browser session) — held in reserve for
   when curl_cffi alone stops being enough.

## Hardcover

Modern, ethical alternative to Goodreads. Smaller catalog (especially
in MAM-popular indie/self-pub genres) but **API-first** — no scraping,
high rate limits, rich data including ratings and social signals.

Required: Bearer API key from hardcover.app → Account → API.

Used by:
- The Hardcover metadata source (search by title+author → returns rich
  metadata)
- The v2.13.0 Goodreads ID resolver's Tier 2 (`book_mappings`
  cross-reference)

## Amazon (v2.21.0 — cache-backed)

Author-Store discovery via `/stores/author/{id}/allbooks` + `/juvec`
POSTs. Akamai bot-managed; bypassed via curl_cffi Chrome120
impersonation. As of v2.21.0, **the live source only runs in the
background worker** — synchronous lookups read from
`metadata_cache_amazon.db`.

See the [Architecture overview](#architecture-overview) above for the
read/write split and the cache layer. This section covers
Amazon-specific behavior.

### Settings (Settings → Sources → Amazon)

| Field | Default | Effect |
|---|---|---|
| `metadata_cache.amazon.enabled` | **False** | Worker on/off. Opt-in so a fresh v2.21.0 deploy doesn't blast Akamai before you've reviewed the panel. |
| `format` | `kindle` | Ebook scan filter. Choices: `kindle` / `paperback` / `hardcover` / `mass_market` / `allFormats`. |
| `audiobook_format` | `audible_audiobook` | Audiobook scan filter. Mostly informational under schema-v2 — the worker always scans `allFormats` and partitions per library at write time. |
| `language` | `English` | Server-side `authorFilters.language` value. |
| `rate_limit` | n/a (worker-paced) | Live source's `rate_limit` is unused under v2.21.0; the worker controls cadence via 30-90s jitter. |

### Worker pacing — the 6 hybrid behaviors

After the 2026-05-22 Akamai investigation (Arm 1 / Arm 3 experiments),
the worker uses six concrete tactics:

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

### Realistic throughput

Arm 3 sustained 5 successful GETs in 8 min. We don't sustain that
because Akamai's long-window scoring kicks in eventually, but
**200–400 successful scans/day** is realistic. For a 600-author
library:

- Full refresh cycle: 2–4 days
- High-priority authors (recent activity): roughly daily cadence
- Dormant authors: weekly-ish

### Inspecting + intervening

- **Settings → Sources → Amazon → Cache Status card** — worker
  enable toggle, queue depth, last block, today's scan count, "Reset
  cooldown" button.
- **Author detail page** — per-author cache badge shows last scan,
  book count, queue position, or cooldown reason.
- **Settings → Database** — `metadata_cache_amazon.db` inspect view
  + backup / wipe / vacuum.
- **API** — `GET /api/v1/metadata-cache/status`,
  `GET /api/v1/metadata-cache/author/{author_id}`,
  `GET /api/v1/metadata-cache/recent-discoveries`,
  `POST /api/v1/metadata-cache/reset-cooldown`.

### What happens on first scan after a fresh deploy

1. Lifespan backfills the queue from every author with an `amazon_id`
   (priority 100, default cadence).
2. Worker is OFF by default. You open Settings → Sources → Amazon and
   flip the enable toggle.
3. Worker starts ticking. First few iterations populate the cache for
   high-priority authors.
4. Synchronous scans against authors that have hit the cache return
   data immediately; misses enqueue at priority 1000 (front of queue)
   and return None for this scan.
5. Over the next 2–4 days the cache fills out for your full library.

## Open Library

Free, no-key, ISBN-keyed. Strongest signal for older or well-cataloged
books. Now both an enrichment source AND a tier in the Goodreads ID
resolver chain.

## Google Books

Optional API key (Google Cloud, Books API enabled). The unkeyed path
works but has a much lower daily quota.

## Audible

Primary audiobook source. Hydrates its catalog hits through Audnexus
internally — narrator, duration, ASIN. Region-aware (`audible_region`
setting, defaults to "us").

## Kobo

Ebook storefront perspective. Parallelized in v2.11.0 with a
configurable `concurrency` (default 4 workers each respecting
`rate_limit`).

## IBDB

Niche but high-quality for indie ebook publishers. Disabled by default;
enable per use case.

## Disabling a source

Settings → Sources → Metadata Sources panel. Each source has four
toggles per row:

- **Ebook Enrich** — query when enriching an ebook grab
- **Ebook Scan** — include in per-author ebook scans
- **Audiobook Enrich** — same, for audiobooks
- **Audiobook Scan** — same, for audiobook scans

Source-level disable is preferred over modifying priority order — a
disabled source contributes zero requests regardless of where it sits
in the chain.

For Amazon specifically: even with the source enabled in the priority
list, no Amazon traffic happens unless `metadata_cache.amazon.enabled`
is also True. The cache reader stays in the synchronous flow either
way — it just always returns "miss" if the worker isn't running.
