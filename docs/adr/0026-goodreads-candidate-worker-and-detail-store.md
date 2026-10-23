# 0026. Goodreads: a candidate worker and a detail store; scans read only the caches

- Status: Accepted
- Date: 2026-10-09 (2026-10 audit, wave 4b; G-C, G73, G75, G76, G85–G103)
- Supersedes: [0018](0018-metadata-cache-goodreads-list-page.md) §6 (the Path C decision gate)

## Context

ADR-0018 cached Goodreads' author list pages (Path B) and left per-book detail (Path C) to a later decision. Scans
still fetched `/book/show/{id}` live for every list entry discovery didn't have.

The 2026-10 audit measured what that bought and what it cost:

- **Goodreads lists real books no other source has**, mostly small indie catalogues (Goodreads imports Kindle
  editions from Amazon): an estimated 600–850 on the reference install, yet its scans had created almost nothing
  since May.
- **Book pages are what AWS WAF blocks.** Goodreads sits behind AWS WAF Bot Control (via CloudFront), not
  Cloudflare. List pages pass (~400 a day, no blocks) and so does `book/auto_complete` (A5: 20 / 20; A5b: 40 / 40,
  30s apart). Book pages trip an IP-level block after a few in a row, for a few minutes, and fresh sessions don't
  escape it (A4, 2026-10-09). A scan's detail loop requested pages back to back and fed every one of them into the
  block.
- **Prod runs no scheduled source scans**, so books found only at scan time appear only when the operator scans that
  author by hand.
- **"Not in discovery" isn't "only on Goodreads".** 80% of the list entries discovery lacked belonged to authors no
  source scan had ever covered; a first scan by Hardcover / OpenLibrary / Amazon finds most of them for free.
- **Autocomplete confirmation is near-tautological.** Querying a list entry's own title returns that entry (31 of 40
  in A5b), translations, cover packs and namesakes' books included: of the 31, 18 were real English books by the right
  author. Hit-level checks (description snippet language, edition / import markers, 0 pages) took that to 18 of 25;
  the book page (language, format, set, genres) does the rest.

Options weighed (findings L6-06): drop Goodreads; create from list-page data alone (no language or format check);
autocomplete-only creation; a background book-page worker over every list entry (~90k pages: months at any tolerated
rate); grey routes (`/book/show/{id}.xml`, Goodreads' internal AppSync API) — ruled out by ADR-0025.

## Decision

1. **Scans read only the Goodreads caches while the cache worker is on** (G-C, G103). A scan takes list-page data
   (and a stored page's) for the books discovery already has and leaves the rest to the candidate worker; it sends
   Goodreads nothing. With the worker off (a fresh install's default) scans keep today's live path.

2. **A candidate worker checks the books Goodreads lists that discovery doesn't have** (G86, G94), only for authors a
   source scan has covered: the first fill takes authors listing at most 100 books; after it, any author's newly
   listed books. A candidate is created only after:
   - an autocomplete for its title (and one more for "title author" if the first finds nothing, G97) returns it under
     the same Goodreads author (G76),
   - the hit passes its checks (description language, edition / import markers, page count),
   - its book page loads and passes (English, not an audiobook / set / translation, not non-fiction unless the
     operator allows it, G95 / G96 / G101),
   and then it is merged at once (G102) through the scan's own merge path, as source `goodreads`.

3. **Book pages get their own gap**, 2 minutes by default (G89), for every caller, on top of the source rate: a
   second queue per kind inside the source gate, so a page waiting for its gap holds up no other kind.

4. **A detail store** (`metadata_cache_goodreads_books`) keeps, per Goodreads book ID, the autocomplete hit and the
   page (parsed the way discovery reads it and the way enrichment does). Enrichment reads a stored page under 90 days
   old before fetching one and stores every page it loads (G93).

5. **Progress is durable and ordered** (G73 / G75): each candidate's state is written as its step finishes; the
   author last worked on comes first, then authors a scan has just covered, then newly listed books, then the first
   fill, smallest catalogue first.

6. **Phase 2 (fetch every cached list entry's page) is built and off** (G88). The operator switches it on for a
   limited test once the first post-update checks pass, and off again early; it runs only when no candidate is
   waiting. Whether it stays depends on how book pages hold at the gap.

## Consequences

- A Goodreads-only book reaches discovery minutes to days after its author is scanned, not during the scan; a scan
  completes without waiting on Goodreads.
- Creation stalls whenever AWS WAF blocks book pages; autocomplete and list pages carry on. The per-kind counters
  (ADR-0025) show it, and the `.xml` route stays the only escalation, on the trial's evidence and the operator's
  ruling.
- Some namesake non-fiction still gets created: a book few people shelved has no Goodreads genres (≈ 1 in 10 in
  A5b). It is hidden by hand.
- The Goodreads cache DB gains four tables (`books`, `candidates`, `candidate_authors`, `phase2`); ADR-0018's
  "Goodreads has no `books` table" no longer holds — its `books` is the detail store, a different shape from Amazon's.
- An enrichment that needs a book page right after another one waits up to the gap (2 minutes), as a turn wait not
  counted against its timeouts.
- The worker's merge switches the process-wide active library, as scans and Hygiene do, and waits while either, or a
  library sync, is running.

## Amendment (2026-10-16, the wave-4 trial's verdict)

- **§3 — the book-page gap defaults to 400 seconds** (G176, G179). At 2 minutes pages were blocked on 24–54% of
  requests a day, with hour-long blocks whenever autocomplete ran alongside; at 10 minutes, 2–5% (9 blocks in ~284
  pages over four days, every one cleared at the next attempt), and enrichment's Goodreads match rate rose from 16 of
  31 grabs to 56 of 72. 400 seconds was tested next (2026-10-16 → 10-20): 4–8% a day (20 blocks in ~392 pages), the
  backoff never past its second step (5 minutes), and ~45% more pages a day than at 10 minutes. Most of the extra
  blocks came with other Goodreads traffic alongside: the weekly author-ID job running during the daily list refresh,
  and bursts of autocomplete checks; pages with neither nearby were blocked 0.4% of the time. So the weekly job moved
  out of the list refresh and the worker's autocomplete checks are spaced at least 3 minutes apart (G187). The cost
  of any gap is pace: grabs enriched back to back take one page turn each.
- **§2 — the first fill widens to authors listing at most 200 books** (G177), once the ≤ 100 fill has drained and
  the volume-aware merge (ADR-0028) is live: 22 more authors, ~1,000 entries. Wider is a later call on its results.
- **§6 — phase 2 runs, owned-first** (G178). Instead of "built and off", it runs whenever no candidate is waiting,
  walking authors by the number of books the operator owns (most first) and skipping owned books, pages already
  stored, translations, sets and audio. A grab still takes the next page turn. Of 82 grabs with a Goodreads match
  in the trial's month, 64 were on a cached author list, half of them by the 41 authors with the most owned books.
  The limited test G88 planned isn't run: the trial measured pages at the gap for days. A fresh install keeps
  phase 2 off, as it keeps the worker off.

## Amendment (2026-10-21, wave S's live check)

- **§3 — a book page also waits for 3 minutes of quiet** (G199): no other Goodreads request, from any caller, in the
  3 minutes before it, and the worker sends no list page or autocomplete in the 3 minutes before a page's turn. Wave S
  let pages go between the daily refresh's list pages and the ≤ 200 widening ran autocomplete at its 3-minute ceiling:
  pages were blocked ~25% of the time in the evening and 6 of 16 in the next refresh. Pages sent within 3 minutes of
  another kind were blocked 18 of 56 times, pages after 3 quiet minutes 0 of 14. Pages keep the gap's rhythm all day;
  list pages and autocomplete get the rest of each gap, so the daily refresh takes about twice as long. A list scan
  already running finishes; the page waits for its quiet after it.

## Related

- [0018](0018-metadata-cache-goodreads-list-page.md) — the list-page cache this builds on; its §6 is decided here.
- [0025](0025-metadata-source-access-tiers-and-one-gate.md) — tiers 0–1 and the source gate the book-page gap lives in.
- [0021](0021-roster-gates-discovery-author-creation.md) — the roster gate the worker's merge goes through.
- [0005](0005-backfill-attempted-set.md) — every step changes its row's state or attempt count, so the worker never
  spins on a candidate that keeps failing.
