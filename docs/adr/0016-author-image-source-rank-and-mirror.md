# 16. Author image: source-rank, link-time persist, canonical mirror

Status: Accepted

Date: 2026-05-30

## Context

v3.0.0 Phase 3 added a `Contributor` dataclass that captures, per credited author on a discovered book, an `image_url` straight from the same DOM/JSON node as the name (`app/discovery/sources/base.py`). Amazon's byline-widget parser populates it from the product-grid `byLine` (`app/discovery/sources/amazon_widget_parser.py` → `amazon.py`); Goodreads' author-list-page selector populates it on `AuthorResult` (`app/discovery/sources/goodreads.py:580`).

But the consume path drops it: `_link_discovered_contributors` (`app/discovery/lookup.py`) threads `c.source_author_id` to `resolve_or_create_author` (since [ADR-0015](0015-source-id-aware-author-identity.md)) but does *not* thread `c.image_url`. The resolver's signature does not accept an image. Captured images die at link time — the exact drop-shape that ADR-0015 fixed for source IDs.

Meanwhile two further wrinkles distinguish images from source IDs:

1. **Single column, cross-source contention.** Source IDs live per-source (`amazon_id`, `goodreads_id`, …) so a scanned Goodreads write *cannot* clobber a scanned Amazon write. Images share **one** column (`image_url`), so successive scans from different sources can oscillate the displayed image — Amazon writes Monday, Goodreads overwrites Tuesday, Amazon back Wednesday.
2. **URL rot.** Source IDs are stable identifiers; image URLs decay (CDN scheme changes, underlying image replaced/removed, the broken-Goodreads-selector regression that wrote `/books/<isbn>.jpg` book covers as author photos and required a substring-blacklist workaround in hygiene Job 8).

Background state at the time of decision: 737 calibre authors, 1 populated `image_url` and it's a book cover; 739 ABS authors, 0 populated. Frontend renders the per-library `authors.image_url` on four pages; `persons.image_url` and the matching per-library column both exist already. A `mirror_bio` helper (`app/discovery/author_identity.py:673`) is the established write-through-to-siblings precedent.

## Decision

Make author image **link-time persisted, source-rank-aware, and mirrored canonically**, reusing ADR-0015's pattern with two image-specific specializations for the cross-source-contention and URL-rot wrinkles.

1. **Persist captured images at link time** (the ADR-0015 spine, applied to images). `_link_discovered_contributors` threads each `Contributor.image_url` + the scan's source into the mirror helper, parallel to slice 01's source-ID threading. The scanned-author path (`_merge_result`) does the same for `AuthorResult.image_url`.

2. **Identity-trust gradient transfers, with no conflict surface.** The scanned-author path is high-confidence (the search subject); the co-author path is byline-derived and lower-confidence — same gradient that locked the asymmetry for source IDs. The **scanned-author** write is rank-aware overwrite (see #3); the **co-author** write is strict fill-if-empty: a captured image is written only when the on-file slot is NULL, regardless of byline source rank. A wrong co-author byline image therefore cannot corrupt an existing canonical value. Unlike ADR-0015, image disagreement does **not** earn a conflict-surface table or operator-review panel: image is decorative (rendered for identification, never read by business logic), so a wrong image is embarrassing but reversible by the next scan or by hygiene; the ROI of `author_image_conflicts` + a dismiss endpoint is below the bar.

3. **Source-rank ordering on a single column with provenance.** Because images share one `image_url` column, add an adjacent `image_url_source TEXT` column to record provenance. Locked rank (lower = higher priority): **`amazon` (1) → `goodreads` (2) → `hardcover` (3) → `audible` (4)**. Scanned-author write rule: incoming wins iff `incoming_rank ≤ on_file_rank` (or on-file is NULL). Co-author write rule (per #2) ignores rank and only fills NULL. NULL `image_url_source` on a populated `image_url` (pre-ADR-0016 rows) is treated as lowest rank — any new write upgrades it. Hardcover and audible are listed in the rank table but **reserved-but-inert** at this ADR's ship: their source parsers do not yet emit `image_url`, so the rank slots are forward-looking; adding them later is purely additive.

4. **`mirror_image_url` — one helper, canonical-overwrite-all fanout.** Signature `mirror_image_url(library_slug, author_id, source, value, trust)` where `trust ∈ {'scanned', 'co_author'}` picks the write rule from #2/#3. The rank decision is made once against the persons-row anchor (or the per-library row for unlinked authors); if the write proceeds, the helper propagates the same `(image_url, image_url_source)` tuple to **every linked sibling AND the persons row** in lockstep. This diverges from `mirror_bio`'s COALESCE-fill (which preserves sibling-specific values) because **a person's face is invariant across libraries** — sibling drift between Calibre and ABS would render the same person as different faces depending on which library a page loaded from. Bio plausibly differs by library/source; image does not. The divergence is principled, not a style break.

5. **`image_url_source` lives on both `persons` and per-library `authors`.** Two additive migrations (`ALTER TABLE … ADD COLUMN image_url_source TEXT`, nullable). The lockstep semantic of #4 keeps them aligned post-mirror; the per-library column handles the unlinked-author case where the persons row doesn't exist yet — the per-library `(image_url, image_url_source)` records the capture-time state and promotes to the persons row when the link is created.

6. **URL rot mitigation via hygiene, not write-time.** Write paths are strict per #2/#3 (no per-write HEAD; never inflates link operations with network). A new hygiene job — **Job 11 "Image URL health check"** — walks every populated `authors.image_url`, applies the substring blacklist (`/books/`, `nophoto`) inherited from the old Job 8 workaround, and HEAD-verifies the remainder. Non-200 / non-image responses → `image_url = NULL` (local-clear-only; do not fan out NULL through siblings — let the next scan re-establish coherence via rank-aware overwrite). Soft-delete sweep bumps from Job 11 → Job 12; `TOTAL_JOBS` becomes 12. The old Job 8 image-clear block is retired (Job 8 stays "cross-library person backfill" only — one job, one named responsibility).

7. **Organic-only backfill.** ADR-0015 slice 05 had a free win to harvest (existing populated `{source}_id` columns lying around from years of scanned-author writes — bulk consolidation cost zero network). For images, there is nothing equivalent to harvest: Goodreads was writing wrong values; Amazon was writing nothing; the rest never captured. A bulk active backfill would require fresh network activity against Akamai/Cloudflare for ~1500 authors. Image coverage is *completeness*, not correctness — placeholders render today and would render after a partial fill. Ship persistence + verification; observe organic fill over normal usage; revisit if coverage stalls.

8. **Scope cuts in v3.x (out, but compatible):**
   - **Goodreads book-page contributor image parity** — book-page byline DOM (`a.ContributorLink`) does not include `<img>` in the 2026-05-26 recon; fetching `/author/show/<id>` per byline contributor is the N+1 pattern that scoped OpenLibrary out of v3.0.0 Phase 3.5. Goodreads images flow scanned-author only; the rank rule (#3) absorbs the asymmetry.
   - **Hardcover + audible image extraction** — defer; rank slots reserved per #3. Audnexus needs additional audit for narrator-vs-author image confusion.
   - **Image conflict surface** — not built (per #2).
   - **Bulk backfill** — not built (per #7).
   - **Per-write HEAD / TTL-based staleness** — not built; rejected in favor of Job 11 (#6).

## Consequences

- The link-time silent drop closes for both write paths. Captured Amazon byline images persist; the Goodreads selector fix lets scanned-author writes start populating real photos.
- Display oscillation across scan cadences is prevented by the rank rule. Once Amazon has populated an image, only another Amazon scan can replace it; Goodreads/Hardcover scans either fill NULL slots or skip.
- The Job 8 `/books/`-path substring workaround retires into a more honest health check (HEAD-verify + blacklist). The blacklist remains as defense-in-depth against `image LIKE '/books/%'` URLs that still resolve 200 — HEAD alone would miss the semantic mismatch.
- Mirror divergence from `mirror_bio` is principled but is the first asymmetry between two helpers that previously read as siblings; the ADR captures the rationale (person-level vs library-level identity) so future readers don't try to "fix" it.
- **Reversibility:** all writes are additive (two `ADD COLUMN`s, the helper, the parser image-field reads, the new Job 11). The decision to *not* surface conflicts is reversible cheaply (add a table + endpoint + panel later if needed). The locked rank order is the longest-lived decision — changing it would invalidate every existing `image_url_source` row's relative priority, so it's harder to flip without a hygiene resweep.
- **Operator note:** Hygiene Job 11 makes network requests (~1500 × HEAD against CDNs per run). Cost is bounded and only on operator hygiene click; CDNs absorb it. Job runs after Job 8 (person backfill) and Job 9 (consolidate-by-source-id), before Job 10 (prune orphan author_links) and Job 12 (soft-delete sweep).
- A v3.x follow-up captures Hardcover + audible image extraction when prioritized; structure (#3 rank table) supports it without further ADR.

## Related

- [ADR-0008](0008-book-authors-authoritative-on-reads.md) — `book_authors` authoritative on reads (the contributor model this consumes).
- [ADR-0014](0014-heal-contributors-on-scan-convergence.md) — heal unowned contributors on convergence (the unowned-data correction precedent).
- [ADR-0015](0015-source-id-aware-author-identity.md) — source-ID-aware author identity (the persistence + match pattern this specializes for images; explicit asymmetry rationale).
