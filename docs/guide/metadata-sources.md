# Metadata sources

Seshat enriches every book it touches by querying a chain of external
metadata [sources](../../CONTEXT.md#discovery-review-surface) in
priority order. This chapter covers the per-source roster: what each
provider covers, how it authenticates, how it fails, and how to tune
or disable it.

The unified **Metadata Sources panel** (Settings → Sources) is the
authoritative editor for everything below. The legacy per-source flat
keys (`goodreads_enabled`, `rate_goodreads`, etc.) are still synced
for backward compatibility but should not be edited directly.

For the cache architecture that wraps Amazon and Goodreads in the
synchronous read flow — `CachedSource`, the background workers,
cooldown plumbing, and status UI — see
[metadata-cache.md](metadata-cache.md). This chapter only describes
per-source behavior; reads of a cached source go through the cache
reader described over there.

## At-a-glance source roster

| Source        | Role                              | Auth                  | Rate (default) | Read path |
|---------------|-----------------------------------|-----------------------|----------------|-----------|
| MAM           | Enrichment + matching             | MAM session           | 2.0s           | live (enrichment-only — never a discovery source) |
| Goodreads     | Authoritative book metadata       | none (Chrome120 bypass) | **5.0s**     | cache-backed list pages + live detail |
| Hardcover     | Rich metadata + Goodreads bridge  | Bearer API key        | 1.0s           | live     |
| Amazon        | Author-Store discovery + enricher | none (curl_cffi)      | worker-paced   | cache-backed (worker-only live) |
| Audible       | Audiobook primary source          | none                  | 0.5s           | live     |
| Open Library  | Free ISBN-keyed fallback          | none                  | 1.0s           | live     |
| Google Books  | Broad metadata                    | API key (optional)    | 1.5s           | live     |
| Kobo          | Ebook storefront metadata         | none                  | 3.0s           | live     |
| IBDB          | Indie publisher coverage          | none                  | 1.0s           | live     |

MAM is always first and locked. Everything else is reorderable and
disable-able per content type (ebook vs audiobook) and per role
(enrich vs scan).

The "Read path" column distinguishes:

- **live** — the source is called synchronously at scan time, paced
  by its per-source `rate_limit`.
- **cache-backed** — the synchronous flow reads from a per-source
  metadata cache; the live source only runs from a background
  worker. See [metadata-cache.md](metadata-cache.md) for the cache
  architecture and the two sources' divergent postures.

## MAM

MAM is **enrichment-only — never a discovery source**. It has no
`search_author()` surface and never inserts a candidate into the
review queue. What it does:

- Matches an incoming announce (or an owned book missing a MAM link)
  against torrents on the tracker and supplies the authoritative
  `author_info` blob for matching, scoring, and dedup.
- Trains the `authors_allowed` filter list at grab-completion with the
  full co-author list from `author_info` — MAM is trusted-create for
  *author names*, never for *book rows*.

The MAM-as-enrichment-only contract is load-bearing for the
multi-author model: discovered books come from real discovery sources
(Goodreads / Hardcover / Amazon / OpenLibrary / Google Books / Kobo /
IBDB / Audnexus), and MAM's authorlist flows onto owned books via the
**owned path** (Calibre/ABS ingest → Phase 2 sync), never as a
discovery insert. See
[multi-author-and-series.md](multi-author-and-series.md) for why
discovery never originates from MAM.

When a search returns no match, the row is permanently stubbed
(`source="unavailable"`) to stop retry storms; see
[ADR-0006](../adr/0006-mam-not-found-is-permanent.md).

## Goodreads

Goodreads has the most complete catalog of any of these sources but
no public API. Seshat scrapes the public HTML at `/book/show/{id}`
and `/author/list/{id}` — both **robots-allowed** for the `*`
user-agent. The `/search` endpoint is **explicitly disallowed** and
Seshat never hits it.

> See [metadata-cache.md](metadata-cache.md) for the Goodreads
> list-page cache architecture (Path B, v3.4.0). This section covers
> the per-source behavior — the Cloudflare bypass, the resolver
> chain, the runbook for when GR goes silent.

### The Cloudflare problem

Goodreads sits behind Cloudflare. From server-side Python clients
(`httpx`, `requests`), Cloudflare's bot manager rejects on the TLS
fingerprint (JA3 check) and returns:

- **HTTP 202** with an empty body, **OR**
- **HTTP 200** with an empty body

…before any real content is fetched. This isn't a rate limit, it
isn't a CAPTCHA, and retrying with the same client doesn't help. The
wire-level signature is the same handshake every request makes —
what's needed is a Chrome-shaped handshake.

### The Chrome120 bypass

Seshat routes every Goodreads request through `curl_cffi`, which
drives `libcurl-impersonate` to replicate Chrome 120's TLS handshake
exactly (cipher suite ordering, BoringSSL extensions, ALPN, h2 frame
patterns). Cloudflare reads the connection as a real Chrome desktop,
the JA3 check passes, and the real page comes back at 1MB+ instead of
the thin-body block-page.

All Goodreads-touching code now goes through one central module —
`app/metadata/goodreads_session.py` — so the TLS impersonation,
soft-block detection, runtime-state tracking, and rate-limit jitter
are uniform across:

- The discovery source (`app/discovery/sources/goodreads.py`,
  `/author/list/{id}` and `/book/show/{id}` HTML burst surface)
- The paste-URL importer (`app/discovery/routers/import_export.py`)
- The ID resolver chain's auto_complete tier
  (`app/metadata/goodreads_id_resolver.py`)

### Runtime state + the dispatcher skip

On any soft-block response (202 / empty 2xx), the session module
flips a runtime flag — `goodreads_session_state = "soft_blocked"` —
visible in `settings.json`. Both source-iteration loops (per-book
enricher, per-author scan) check this flag and **skip Goodreads
entirely** when set. Without this gate every iteration pays the full
request → soft-block → next-source roundtrip even after the first
soft-block already told us Goodreads is gated.

The flag clears to `active` automatically on the next successful 200
through the session module, or manually via:

- **Settings → Sources → Goodreads → "Run probe"** — one GET to a
  known-good book. Updates the flag based on the result.
- **Settings → Sources → Goodreads → "Run burst (10×)"** — 10 GETs
  against the canonical probe pool at the configured rate limit.
  Surfaces density-based 202s that single probes miss.
- **Settings → Sources → Goodreads → "Mark as active"** — manually
  clear the flag without a probe (use after refreshing IP / waiting
  for Cloudflare's bot-score to decay).

### Weekly canary

A scheduled job (Mondays 03:00 local) does one GET to The Hobbit
(`/book/show/5907`) through the production session module. On 202 it
emits a ntfy notification gated on
`notify_on_goodreads_canary_failed` so users who don't open Settings
still notice when Goodreads goes silent.

### Per-author budget scaling

Sanderson-class authors (≥200 books on the GR list page) used to hit
the default 25-min per-author budget cap and silently dropped ~37% of
books. The retry loop scales budget by list-page book count:

- `book_count > 100` → 600s per retry (was 300s), 30 min total budget
- `book_count > 200` → 900s per retry, 40 min total budget
- Default — 300s / 25 min

Helpers no-op for non-Goodreads sources and never shrink an
operator-raised value. When the budget *is* exhausted, the worker
emits a `[goodreads] giving up on '<author>' — processed N/M books`
log line and finalizes the scan with whatever was processed; this is
the [budget-exhaust](../../CONTEXT.md#metadata-caching) signal.

### The resolver chain

When the enricher (or any caller) needs a Goodreads book ID for a
book it doesn't already have one for, the ethical resolver runs
three tiers in order. First hit wins; the chain falls through on
misses:

1. **Tier 1 — Goodreads `/book/auto_complete?q={isbn_or_asin}`** —
   undocumented JSON endpoint, NOT in the Disallow list. Identifier-
   based, not free-text. Handles most ebook imports since almost
   every epub/azw3 carries ISBN in file metadata.

2. **Tier 2 — Hardcover GraphQL `book_mappings`** — when a Hardcover
   API key is configured, one GraphQL roundtrip resolves ISBN/ASIN →
   Hardcover book → `book_mappings` filtered by
   `platform: { name: { _eq: "Goodreads" } }` → external_id. Returns
   the Goodreads ID without ever touching Goodreads. Skipped silently
   when no API key is set.

3. **Tier 3 — Open Library `?bibkeys=ISBN:{isbn}&jscmd=data`** —
   `identifiers.goodreads[0]` for books OL has cross-referenced. Free,
   no key required. Coverage is sparse for recent self-pub indie
   titles but reliable for older / well-cataloged books.

The chain explicitly does NOT fall back to the disallowed `/search`
endpoint, even though some Calibre plugins do. Holding a higher
standard is a deliberate choice.

The resolver caches outcomes in `DATA_DIR/seshat_id_cache.db` to
minimize Goodreads request volume regardless of bypass strategy:

- **Book ID** lookups: 30-day TTL on hits, 1-day on misses
- **Author bibliography** lookups: 7-day TTL on hits, 6-hour on misses

The cache is keyed identifier-first (ISBN > ASIN > normalized
title+author). Misses are cached too so dead-end ISBNs don't re-probe
Goodreads every scan within the miss-TTL window. The cache prunes
expired rows during the weekly canary tick.

This resolver-level ID cache is independent of the list-page metadata
cache; see [metadata-cache.md](metadata-cache.md) for the latter.

### Rate limit + when to tune it

Goodreads's default rate is **5.0s + 0–1s jitter**. This is the
conservative pace that gives the Chrome120 fingerprint clean headroom
under burst scans. If your burst probe shows zero soft-blocks at 5.0s
you can dial down to 3.0s for faster scans. If you see soft-blocks
during the burst probe, dial up to 8.0s or higher; the bot manager
is flagging request density.

### Runbook — what to do when Goodreads goes silent

1. **Run a probe** (Settings → Sources → Goodreads → Run probe). A
   200 means the bypass is working — flag auto-clears.
2. **If still 202**, run a burst probe. If single passes but burst
   fails, raise the rate limit (8s+).
3. **If both fail**, wait 4–12 hours and re-probe. Cloudflare's
   bot-score decays naturally; the Chrome120 bypass works for most
   users after a cooldown.
4. **If failures persist for days**, file a GitHub issue. A reserve
   path adds an encrypted cookie panel (paste `cf_clearance` +
   `_session_id2` + browser UA from a fresh browser session) — held
   in reserve for when curl_cffi alone stops being enough.

## Hardcover

Modern, ethical alternative to Goodreads. Smaller catalog (especially
in MAM-popular indie/self-pub genres) but **API-first** — no
scraping, high rate limits, rich data including ratings and social
signals.

Required: Bearer API key from hardcover.app → Account → API.

Used by:

- The Hardcover metadata source (search by title+author → returns rich
  metadata)
- The Goodreads ID resolver's Tier 2 (`book_mappings` cross-reference)

## Amazon

Author-Store discovery via `/stores/author/{id}/allbooks` + `/juvec`
POSTs. Akamai bot-managed; bypassed via curl_cffi Chrome120
impersonation. Amazon is **cache-backed** in the synchronous read
flow: a cached read returns last-known author bibliography from
`metadata_cache_amazon.db`, while the live `AmazonSource` runs only
from the background worker.

> The cache architecture, cooldown escalation curve, schema-v2
> dedup, the six hybrid anti-block tactics, settings keys, and the
> Cache Status UI all live in
> [metadata-cache.md](metadata-cache.md). The Amazon worker is
> **disabled by default** — a fresh Seshat deploy reads cache (always
> miss until the worker runs) and never hits Amazon synchronously.

## Audible

Primary audiobook source. Hydrates its catalog hits through Audnexus
internally — narrator, duration, ASIN. Region-aware (`audible_region`
setting, defaults to "us").

## Open Library

Free, no-key, ISBN-keyed. Strongest signal for older or well-cataloged
books. Both an enrichment source AND a tier in the Goodreads ID
resolver chain.

## Google Books

Optional API key (Google Cloud, Books API enabled). The unkeyed path
works but has a much lower daily quota.

## Kobo

Ebook storefront perspective. Parallelized with a configurable
`concurrency` (default 4 workers each respecting `rate_limit`).

## IBDB

Niche but high-quality for indie ebook publishers. Disabled by
default; enable per use case.

## Disabling a source

Settings → Sources → Metadata Sources panel. Each source has four
toggles per row:

- **Ebook Enrich** — query when enriching an ebook grab
- **Ebook Scan** — include in per-author ebook scans
- **Audiobook Enrich** — same, for audiobooks
- **Audiobook Scan** — same, for audiobook scans

Source-level disable is preferred over modifying priority order — a
disabled source contributes zero requests regardless of where it
sits in the chain.

For cache-backed sources (Amazon, Goodreads): the priority-list
toggle gates the synchronous read against `CachedSource`. The
**worker** enable lives in a separate per-source panel (Settings →
Sources → *source* → Cache Status card). With the priority toggle ON
but the worker OFF, the cache reader stays in the synchronous flow
and always returns "miss" — no live source calls, no cache fills.
See [metadata-cache.md](metadata-cache.md) for the full settings-key
shape.
