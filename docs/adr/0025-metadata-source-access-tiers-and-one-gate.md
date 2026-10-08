# 0025. Metadata sources: access stays within tiers 0–1, through one gate per source

- Status: Accepted
- Date: 2026-10-08 (2026-10 audit, wave 4; G56–G60, G62, G71, G78–G81)

## Context

Seshat reads book data from sites that defend themselves against automated traffic: Amazon (Akamai Bot Manager)
and Goodreads (AWS WAF Bot Control behind CloudFront; Seshat's code called it Cloudflare until the 2026-10 audit
read the response headers). Kobo sits behind Cloudflare. The others (Hardcover, Google Books, OpenLibrary, IBDB,
Audible) are APIs with published or observed limits.

In May 2026, working out Amazon's soft blocks, Mark drew a line through the ways a client can get past such
defences (memory `project_seshat_amazon_softblock_investigation`):

| Tier | What it is | Example |
| --- | --- | --- |
| 0 | One client behaving like a person | one paced session, a real browser's TLS profile, backing off when told to |
| 1 | Discarding Seshat's own dirty state | a fresh session per author scan (Amazon's `_abck` scoring) |
| 2 | Multiplying identity | proxies, several accounts, rotating fingerprints |
| 3 | Defeating a control | solving JavaScript challenges, forging sensor data, headless browsers |

The line sits between 1 and 2. Nothing recorded it in the repo, and the 2026-10 audit's traffic inventory found
code on both sides of it and code that ignored tier 0's pacing:

- **Both Kobo sources used `cloudscraper`**, a library built to solve Cloudflare's JavaScript challenges (tier 3
  by intent), while Goodreads and Amazon used curl_cffi impersonation (tier 0).
- **`goodreads_session._build_cookie_header`** was an unbuilt hook for pasting a browser's `cf_clearance` cookie.
  It targets the wrong vendor; AWS WAF's equivalent, `aws-waf-token`, lasts 300 seconds by default and carries the
  browser's fingerprint, so it would be borrowing another client's identity (tier 2) for minutes at a time.
- **No source had one pace.** Each caller kept its own sleep or none: the enricher built its sources with
  constructor defaults (Amazon's configured 100s and Hardcover's rate were read by nothing), Hardcover's discovery
  POSTs skipped the sleep, the Goodreads ID resolver's autocomplete calls had their own unpaced client, and Kobo's
  four concurrent fetches each slept on their own, so its rate was four times the setting.
- **Nothing counted what came back.** Blocks, errors and timeouts lived in container stdout until the next restart,
  so "is this source worth its trouble" couldn't be answered from Seshat (findings L6-05).

Mark also asked what other tools do (findings L6-06, `wave4/web-research-sources.md`), grey routes included. Two
were probed once from the container on 2026-10-08 (PRD, "Probe results"): Goodreads' `/book/show/{id}.xml`
returns the same HTML page as the plain URL (its only use would be slipping past a WAF path rule), and Amazon's
mobile search was neither lighter nor less defended than the desktop one.

## Decision

1. **Seshat's source access stays within tiers 0–1.** Tier 0 and tier 1 techniques may be built; tier 2 and 3 may
   not. Rejected, with the reason recorded so they aren't re-proposed:
   - challenge solving of any kind: Kobo moves from `cloudscraper` to curl_cffi (G60); a challenge becomes a
     counted block and a backoff, and if kobo.com challenges every request Kobo stops working;
   - the Goodreads cookie hook (removed with the Goodreads fixes in wave 4 S3) and any pasted browser token;
   - Mark's own Amazon cookie (it ties the scraping to his Amazon account);
   - proxies, extra accounts, fingerprint rotation, headless browsers, sensor forging.

   **Grey routes are not built:** Goodreads' `.xml` page route, Goodreads' internal AppSync GraphQL API with the
   website's public key (Goodreads revoked it on 2026-08-29), the legacy Goodreads XML API with someone else's
   developer key, and Amazon's mobile search. `.xml` and mobile search stay noted as fallbacks, reconsidered only
   on the wave-4 trial's evidence: `.xml` if fresh-session Goodreads book pages are blocked on most attempts over a
   week while autocomplete holds (G58), mobile search if Amazon's desktop search returns captchas (G59).

2. **One gate per source** (`app/metadata/source_gate.py`) is how tier 0 is enforced. Every request to a metadata
   source, from any caller, waits its turn there:
   - **A source's Metadata Sources rate is the minimum gap between the starts of any two requests to that
     source** (G71), read before every request. A request after a quiet spell goes at once. Goodreads keeps the
     0–1s of jitter its session always added. Audnexus, which has no Metadata Sources entry, keeps its 0.2s floor.
   - **Enrichment, the Settings probe and URL import go ahead of scans and the cache workers** in the queue, and
     time enrichment spends waiting for a turn isn't counted against its 15s per-source timeout or its 60s per-book
     budget (G78). Without that, a source with a 30s or 100s rate could never be reached from enrichment.
   - Kobo's per-author scan cap rises from 180s to 600s (G72), since its concurrent fetches no longer multiply the
     rate.

3. **The gate counts every request** per local day × source × caller × kind into `source_counters` in the app
   database (G62): requests, OK, blocked, errors, timeouts; scans add the books created and updated and the times a
   source hit its scan time cap (G81). The caller is whoever started the work (G80: a scan, a cache worker,
   enrichment, the author-ID backfill, the probe, URL import; `resolver` only when nothing above named one).
   Goodreads splits by request kind (book page, list page, autocomplete, G79). Rows are kept 90 days. The Metadata
   Sources panel shows today by caller and the last 7 days per source. These counts are the instrument for every
   later ruling on a source (drop, demote, or a fallback above).

## Consequences

- Pacing changes for every source, observably: requests from different callers no longer overlap, Kobo runs at a
  quarter of its old request rate, and a source scan takes longer when a cache worker is busy with the same source.
  A grab whose enrichment has to reach Amazon live can wait minutes (G71's accepted cost).
- A request to a metadata source that bypasses the gate is a bug: it escapes both the pacing and the counts. New
  callers go through `source_gate.turn()` / `request()`, or through a source's `_get`, which does.
- Some data may be lost on purpose: Kobo if kobo.com challenges every plain request; Goodreads book pages if AWS WAF
  keeps blocking them. The counters show it; the fallbacks above are the only escalation path, and each needs
  Mark's ruling.
- Counts are collected in memory and written once a minute and at shutdown, so a crash loses at most a minute's.

## Related

- [0018](0018-metadata-cache-goodreads-list-page.md) — the Goodreads list-page cache; its §6 (Path C) is decided in
  wave 4b.
- [0005](0005-backfill-attempted-set.md) — the author-ID backfill keeps an attempted set (wave 4 S3).
