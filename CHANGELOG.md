# Changelog

All notable changes to Seshat are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.2.3] — 2026-05-01

Patch release. Three small UAT-driven fixes — two UI papercuts on the
Authors view and one resource-saver on the source-scan path that
follows from how Mark has been using Hidden during his A→Z scan
walkthrough.

### UI — author detail header counts only this author's books in shared series

The header metric on the author detail page was reducing over
`series.book_count` (every book in the series) when computing total /
missing / progress-bar fill. For shared series like Halo (38 books
spread across Eric Nylund, William C. Dietz, Greg Bear, etc.) that
inflated the per-author total by every book NOT attributed to the
author. Result: Greg Bear's detail page showed "3 owned, 35 missing"
with a ~5% completion bar even though all 3 of his Halo entries were
owned. The browse-page Authors row showed the correct numbers
because it pulled from the cached author aggregate.

Backend already returned the per-author count as
`series.author_book_count` (computed in `authors.py`'s detail SQL).
Sum that instead, with `book_count` fallback for any single-author
series where the field is absent. `series.owned_count` and
`series.missing_count` were already author-scoped, so owned/missing
were correct — only the total was wrong.

### Discovery — Hidden books skip source-scan metadata refresh and detail fetches

Hidden has gradually become Mark's intentional "don't track this
book at all" signal — already filtered from UI counts, MAM scans,
and the scheduled MAM loop. Source scans (Re-sync, Full, the
scheduled lookup) were the last hold-out: they still issued
detail-page fetches AND fired UPDATE statements against hidden rows
on every author scan, wasting network + DB time on books the user
had explicitly trashed.

Now treated as a true garbage bin: hidden rows stay in the
dedup-title set so source results still can't reinsert them as
fresh unhidden duplicates, but every source-driven write path is
suppressed.

- `_lookup_author_inner` builds a `hidden_titles` subset alongside
  `existing_titles`. In `full_scan` mode `_try_source` passes
  `hidden_titles` (instead of `set()`) so hidden books fall into
  the URL-backfill fast path and never trigger a detail-page
  fetch. Non-hidden books still hit the slow DETAIL path — the
  whole point of full_scan is preserved.
- `_merge_result` SELECTs `hidden`. A new `_is_hidden` guard
  short-circuits BEFORE both `_update_existing` call sites
  (series-book and standalone-book paths), so URL merge / series
  promotion / omnibus-flag promotion / full_scan metadata refresh /
  per-source-id COALESCE — the entire UPDATE — never runs against
  hidden rows. The series-collector recording is also skipped on
  this path so consensus suggestions stop firing for hidden books.
- `_title_to_series_pass` filters its standalone candidates to
  `hidden=0` so the post-scan title→series linker stops promoting
  hidden books into series after a scan.

Un-hiding a book restores prior behavior automatically — the next
scan picks it up like any other known row.

### UI — Authors page remembers pagination across detail-page navigation

Letter / sort / search / format chip on the Authors page were
already sessionStorage-persisted via `usePersist`, so a round-trip
through an author's detail page restored the surrounding filter
context. Page number wasn't — it was plain `useState`. Result:
clicking into an author on page 2 of "B" and hitting "Back to
Authors" landed on page 1 of "B" instead of page 2, forcing a
re-page-forward.

Switched `pg` to `usePersist<number>("ap_pg", 1)` on both desktop
and mobile authors pages. The `Math.min(pg, totalPages)` clamp at
the read site handles stale stored pages (e.g. dataset shrunk
between visits). Existing reset-to-1 hooks on filter / sort / query
changes still fire normally.

---

## [2.2.2] — 2026-04-30

Patch release. Mark's A→Z author UAT walkthrough kept surfacing
silent-data-corruption bugs that were only visible because he was
manually paging through letters. This release closes the whole
class. Five fixes, all rooted in cross-library Authors view + bulk
selection interactions.

### UI — alphabetical scan order

`/authors/scan-sources` ran `SELECT WHERE id IN (?,?,?)` with no
`ORDER BY` clause, so SQLite returned rows in physical (rowid)
order — initial Calibre-sync insertion order. A multi-select
batch felt random. Now `ORDER BY sort_name` on both the active-
library and cross-library code paths so the scan progresses
alphabetically by last name (matching the user's mental model
from the Authors page list view).

### UI — bulk-selection clears on filter change

The `selectAllVisible` button is intentionally additive across
pages so users can build up multi-page selections by paging +
clicking Select All. The v2.2.0 commit that added this behavior
didn't account for what should happen when the filter context
CHANGES (letter sidebar, search box, sort, format chip). Selecting
under one filter, switching to another, and selecting again would
union the two — which the user does not expect. Same-letter paging
stays additive (the intended use case); context switches reset.

### UI — Scan Sources routes through cross-library by content_type

Authors / Library / MAM-page multi-select all defaulted to calling
`scan-sources` WITHOUT `content_type`, which routes through the
backend's active-library legacy path. That works fine for single-
library setups, but the Authors / Books pages are ALWAYS in cross-
library mode (the fmt chip defaults to "all") and the IDs in the
selection are merged-response IDs scoped to whichever library each
row was first encountered in. "Scan Sources" now passes
`content_type="ebook"` so it goes through the same cross-library
path "Scan Audio" already used.

### Backend — scan-sources accepts pre-resolved author_names

The cross-library backend path was still calling
`_resolve_names_for_ids` to translate IDs → names against the
active library. Same root cause as the UI fix above: IDs from a
cross-library merged response can collide with unrelated active-
library authors. The resolver picks up the wrong name and that
name gets scanned across every ebook library. Fix: the request
body now accepts `author_names` directly and the backend skips
the resolver step when names are supplied. Same for
`/books/scan-sources` (pre-resolved book→author map → names) and
`/authors/clear-scan-data` (clear-by-name across libraries).

### UI — selection key uses `library_slug:id` instead of bare id

The deepest bug, surfaced by Mark when Roger Black still got
scanned despite all the above. The cross-library Authors response
gives each merged author one numeric `id` from whichever library
was first encountered — and DIFFERENT authors can share the same
numeric id because each library numbers from 1 independently.
ABS lib id=17 is Touko Amekawa; ebook lib id=17 is Roger Black.
The frontend's `nameById = new Map(aus.map(a => [a.id, a.name]))`
last-write-wins behavior overwrote Touko's name with Roger Black's
after the alphabetical sort placed "Black, Roger" after "Amekawa,
Touko". When Mark clicked Touko, the frontend POSTed "Roger Black"
as her name. Symmetric bug with William D. Arand → "Fuse" silently
dropping Arand from the scan list because Fuse doesn't exist in
the ebook library. Fix: switch `sel: Set<number>` to
`sel: Set<string>` keyed by `${library_slug}:${id}`, which IS
globally unique. Selection check, `selectAllVisible`, and POST
payload all derive from `aus.filter(a => sel.has(authorKey(a)))`
rather than a Map lookup, so duplicate-id rows stay distinct.

### UI — bulk scan-sources progress count reflects actual scans

The cross-library scan-sources backend computed
`total_tasks = len(target_libs) * len(names)` before running the
per-library `WHERE name IN (...)` SQL filter. When a name in the
payload doesn't match any author in the target libraries (the
audiobook-only-in-ebook-scan case), no `lookup_author` call
fires for it but the progress total still expected one. Mark's
"26 selected" produced "x/26" progress that capped at 25 because
Touko Amekawa was filtered out. Fix: hoist the per-library SQL
upfront so `total_tasks` is the sum of actual matched authors.
Response also returns a `requested` field so the UI can show
"Scanning N of M authors" if there's a delta.

---

## [2.2.1] — 2026-04-30

Patch release. Two discovery-correctness fixes surfaced during the
v2.2.0 author-by-letter UAT walkthrough — Mark only reached the
A/B range before finding both. Both bugs share a "scan looked
successful but data was silently lost or corrupted" pattern.

### Discovery — orphan-series cleanup must consider cross-author book references

The orphan-series cleanup at the tail of `_merge_result` was
scoping its "is anyone referencing this series" subquery to the
SCANNED author's books only:

```sql
DELETE FROM series WHERE author_id = ?
AND id NOT IN (SELECT DISTINCT series_id FROM books
               WHERE series_id IS NOT NULL AND author_id = ?)
```

For pen-name-linked authors that's wrong. The architecture parks
books from one author against another author's series row —
Arand owns the "Incubus Inc." series row, Darren has 3 books
referencing it. A scan of Arand sees "no Arand books reference
Incubus Inc." → DELETE the row → trip the FK from Darren's books
(`books.series_id REFERENCES series(id)`, no ON DELETE) → SQLite
raises `FOREIGN KEY constraint failed` → the entire scan
transaction rolls back. User-visible: every source for Arand
logged an ERROR and the scan ended with 0 books added even
though the per-book MERGE UPDATE / MERGE NOOP debug lines all
succeeded earlier in the loop. The block dates back to commit
`dd22c43c` (2026-04-16) — latent for ~2 weeks but only fired on
authors with the cross-author shared-series shape.

Fix drops the `author_id = ?` filter from the subquery. A series
is orphaned iff no book anywhere references it; that's the
correct definition.

### Discovery — fuzzy-match guard rejects omnibus mismatches + title-extracted position conflicts

Two bugs surfaced together because they share the same root cause
(post-`_fuzzy_match` rejection gates were too narrow):

**Omnibus mis-flag.** Hardcover returned both 'Right of
Retribution' (book #1) and 'Right of Retribution: Compilation:
The Starting Point' for William D. Arand. The compilation
fuzzy-matched the user's owned standalone book #1 via prefix
containment. `_update_existing` then flipped `is_omnibus=1` on
the standalone because the OR-arm `_is_omnibus(bk.title)`
matched on `compilation`. The owned book got mis-flagged and
vanished from the series view as an "extra" omnibus row.

**Cross-position metadata stomp.** ibdb returned 'Super Sales on
Super Heroes 4' as a standalone (no series_index on the
BookResult; ibdb routinely emits series books that way). The
fuzzy matcher accepted it onto the existing 'Super Sales on
Super Heroes 2' row because the existing series-index conflict
guard only fires when BOTH sides carry an explicit series_index.
#4's description / pub_date / cover_url / isbn got merged onto
the #2 row, and #4 never landed as its own row — the series
view showed a gap at #4 because "#4 was found, just silently
overwritten onto #2". Same shape silently corrupted Save State
Hero #3 → #2 and likely an unknown number of other numbered
series entries on prior scans.

Single fix covers both:

- New `_title_extracted_index(title)` extracts a position via
  `_RX_TITLE_SERIES_IDX` (trailing number, "#N", "Book N").
- New `_fuzzy_match_blocked(bk, row)` returns a short reason
  code when a fuzzy match should be rejected:
  - `omnibus_mismatch` — `_is_omnibus(bk.title)` differs from
    `_is_omnibus(row['title'])`.
  - `position_conflict` — explicit-or-title-extracted
    `series_index` on bk differs from explicit-or-title-extracted
    `series_index` on row. Subsumes the old
    `_series_index_conflicts` check.
- Both fuzzy-match call sites in `_merge_result` (series-books
  path + standalone path) now use the unified blocker.
- Defensive belt-and-suspenders fix in `_update_existing`: the
  omnibus flag promotion now only checks the EXISTING title, not
  the incoming. With the upstream guard rejecting omnibus
  mismatches before they reach the merge, the OR-arm based on
  incoming title was redundant.

### Notes for users

After updating, **re-scan any author you've previously scanned**
where you suspect the series view looks short or where unexpected
metadata changed. The fuzzy-match bug had been silently merging
incoming series books onto wrong rows whenever a source returned
the book as a standalone — affecting any numbered series, not
just pen-name-linked authors. The orphan-cleanup FK bug was
narrower (only fired on canonical-side linked authors).

---

## [2.2.0] — 2026-04-30

Minor release. One omnibus correctness fix, two UI ergonomic
improvements, and a Docker image overhaul that lands a 41%
size reduction on the default image plus an opt-in
`:latest-slim` variant for users who don't need direct calibredb.

### Discovery — Stoham omnibus regression

- **Standalone INSERT now sets `is_omnibus`.** The previous omnibus
  fix added flag promotion via `_update_existing` and a startup
  backfill, but the standalone INSERT path had been silently dropping
  the column since the feature was first written. Goodreads emits
  titles like "Hero Support: Omnibus" / "Amazonian Master Omnibus" /
  "The Complete Deadland Saga" as standalones (no series tagging),
  so they hit that hole and landed at `is_omnibus=0`, then
  `_title_to_series_pass` parked them next to the real numbered
  volumes in the series. The series INSERT path was always correct;
  the standalone path now mirrors it.

### UI — selection ergonomics

- **Authors page Select All on Page.** The selection bar now opens
  in `selMode` regardless of whether anything is selected, with a
  "Select All on Page" button beside the count. Action buttons
  (Scan, ClearMenu, Link) are gated on having ≥1 selected; Link
  buttons stay gated on ≥2.
- **Cross-page additive selection.** `selectAllVisible` on the
  Authors, Library, and MAM pages now merges the visible page slice
  into the existing selection instead of replacing it. Page →
  Select All on Page → Page → Select All on Page builds a multi-
  page selection.

### Settings — Data Management

- **Per-author + global discovery clears.** The Data Management tab
  picks back up the AthenaScout UX: type-ahead author search with
  chip multi-select, per-author Clear Source / Clear MAM / Clear
  Both, and Wipe-All buttons for source data and MAM data. The
  pipeline-tables clears (tentative_torrents, book_review_queue,
  etc.) sit below a divider as before.

### Docker image — calibre tarball + slim variant

- **Default `:latest` switched from `apt-get install calibre` to
  Calibre's official self-contained binary tarball.** The apt path
  pulled 1.27GB of Qt5 + Mesa + the GUI dependency closure even
  though headless `calibredb add` / `list --for-machine` use almost
  none of it. Calibre's binary distribution bundles its own Python
  + Qt + libs and the apt deps drop to just `sqlite3 libxcb-cursor0
  libfontconfig1 libxrender1`. Image size 1.47GB → ~860MB, a 41%
  reduction. Calibre version is now pinned via `ARG CALIBRE_VERSION`
  with a Renovate annotation for automated bumps.

- **`libgl1` / `libegl1` / `libopengl0` deliberately omitted.** Those
  alone pull in libllvm19 (~127MB) and mesa-libgallium (~42MB) for
  the software OpenGL stack, which calibredb's `add` and `list`
  don't exercise. If a Calibre operation does fail on a missing
  GL/Qt symbol, `app/sinks/calibre.py:_detect_runtime_lib_failure`
  inspects calibredb's stderr and emits a structured diagnostic
  block (image variant, action, stderr snippet, escape-hatch hint)
  pointing the user at GitHub Issues — so we can collect data and
  add the lib back if it turns out we need to.

- **New `:latest-slim` image variant.** Drops Calibre entirely.
  ~200MB total — an 86% reduction vs the current 1.47GB. Pick this
  if you ingest via the CWA, ABS, or file-folder sinks. The full
  and slim variants build from the same commit via a workflow
  matrix; switching is a `docker pull` away.

---

## [2.1.1] — 2026-04-29

Patch release. Fixes four classes of source-scan correctness bugs
surfaced by an A→Z full author scan: Kobo false positives, cross-
author duplicates, mis-flagged omnibuses, and cross-format
duplicate book entries. Two idempotent startup backfills clean up
historical residue.

### Discovery — source scan correctness

- **Kobo author validation.** Kobo's `&fcsearchfield=Author` query
  returned books where the queried name appeared anywhere in
  credits — translator, foreword, contributor, anthology entries —
  and the source plugin trusted the result without filtering.
  Author "Bainin" pulled in 11 books by Greig Beck, Kate Rudolph,
  Yu Shimizu, etc.; "Baoshu" pulled in books by Liu Cixin and
  several others. Now: each title node is paired with its result-
  card `data-testid="authors"` element and rejected if the listed
  author doesn't match the queried name (or any linked pen-name /
  co-author). Fuzzy match mirrors Hardcover's `_check_contributor`
  (period-strip + parts-set).

- **Cross-author owned-ISBN dedup.** The merge candidate set was
  scoped to the scanned author + linked authors, so a book the
  user already owned under (say) "Various authors" with the same
  ISBN couldn't be deduplicated when a source attributed it to a
  contributor — "Halo: Evolutions" appeared as a duplicate under
  Tobias S. Buckell. Now: a cross-author owned-ISBN map (excluding
  the same-author candidate set) is consulted before INSERT in
  both the series and standalone paths. Conservative boundary —
  owned-only, so legitimate co-authored discovered rows still
  coexist for consensus reconciliation.

- **Linked-author dedup log clarity.** `pen_name_links` carries
  both `pen_name` and `co_author` types and the existing dedup
  window already pulled both in (so co-author dedup was already
  active for linked authors), but the log message labeled every
  hit as `PEN-NAME DEDUP` regardless. Renamed to `LINKED-AUTHOR
  DEDUP (<link_type>)` and renamed the expansion log to count pen
  names and co-authors separately.

- **Omnibus flag promotion.** `is_omnibus` was only set on the
  INSERT path; existing books (Calibre-synced or inserted before
  the regex matched their title) stayed at 0 forever. Now:
  `_update_existing` re-evaluates `_is_omnibus` against both the
  existing and incoming title and promotes the flag additively
  (never clears).

- **Cross-format series-position dedup.** Goodreads emits
  `"Title (Series #N)"`, Hardcover/Kobo emit `"Series N: Title"`.
  `_normalize` strips the parenthetical (Goodreads form) and the
  subtitle after `:` (Hardcover form) — so the two tokens are
  disjoint and SequenceMatcher gave a 0.24 ratio. Both layouts
  encode the same `(series_name, series_index)` tuple though.
  Now: a new `_extract_series_position()` parses either form,
  resolves it against the author's known series, and looks up
  `rows_by_series_pos`. A second pass extends the prefilter with
  title-extracted positions so Goodreads-inserted standalone rows
  (which carry NULL `series_index` because Goodreads emits them
  as standalone but encode the position in the title) still
  match when Hardcover/Kobo arrive later in the same scan.

### Discovery — startup backfills

- **Omnibus flag backfill** (`_backfill_omnibus_flag`). Idempotent
  rescan that flips `is_omnibus=1` on rows whose title matches
  the omnibus regex but were inserted/synced without the flag set
  (Calibre sync never sets it; older source-scans inserted before
  `_RX_OMNIBUS` picked up newer keywords). 86 rows flagged on
  first run against the live DB.

- **Series-index recovery** (`_backfill_series_index_from_title`).
  Idempotent rescan that walks rows where `series_id` is set but
  `series_index` is NULL, extracts the implicit index from the
  title (`"Series N: Title"` or `"Title (Series #N)"`), and
  either sets it on the row or — when a duplicate already sits
  at the canonical position — drops the loser using the same
  ranking rules `_title_to_series_pass` already uses (owned >
  non-Book-N suffix > lowest id). 10 rows indexed and 2 same-
  position pairs deduped on first run against the live DB,
  including the originally-reported Bainin "Paths of Akashic 5:
  The Expanse" / "The Expanse (Paths of Akashic #5)" collision.

---

## [2.1.0] — 2026-04-29

Mobile-redesign release. Every desktop page now branches at the top
via `useMobileCodepath()` and renders a purpose-built mobile variant
on phones, iPads, and any touch device — no more CSS-shrunken desktop
layouts. Adds multi-select bulk actions on the author detail page
plus a transitive-dependency security patch.

### Major features

- **Mobile-native pages across the entire app.** Six phases of
  ground-up mobile UI, replacing the CSS-responsive pass shipped in
  2.0.0. Every page declares its parent via `MobileBackButton to=…`
  for hierarchical navigation, and a 44pt minimum touch target is
  enforced via the new `components/mobile/tokens.ts` scale.

  - **Phase 1 — Dashboards.** New mobile primitives (`MobileBtn`,
    `MobileChip`, `MobileSection`, `MobileSheet`, `MobileBookCard`,
    etc.) and dashboard widgets (`LibraryHero`, `StatTile`,
    `HealthPill`, `MamAccount`, `SnatchBudget`, `ScanProgress`,
    `RecentActivity`). `MobileUnifiedDashboard`,
    `MobileDiscDashboard`, `MobilePipelineDashboard` compose these
    widgets into a vertical stack with health pills, library heroes,
    command center, and stats grid.
  - **Phase 2 — Discovery surfaces.** `MobileBooksPage` (Library /
    Missing / Upcoming via shared `apiPath` + `extraParams`),
    `MobileMAMPage`, `MobileHiddenPage`, `MobileSuggestionsPage`,
    `MobileAuthorsPage`, `MobileAuthorDetailPage`. Cards, format
    chips, sort sheets, MAM-status filters, lazy-loaded series
    sections, full-screen `BookSidebar` for tap-to-detail.
  - **Phase 3 — Pipeline pages.** `MobileReviewPage`,
    `MobileTentativePage`, `MobileIgnoredWeeklyPage`,
    `MobilePipelineAuthorsPage`, `MobileDelayedPage`. Card-per-item
    layouts, inline edit, bulk select chips, paste-to-add textareas.
  - **Phase 4 — Utility pages.** `MobileLogsPage`, `MobileWorksPage`,
    `MobileImportExportPage`, `MobileFiltersPage`,
    `MobileDatabasePage`, `MobileSettingsPage` (1218-line settings
    page broken into 12 collapsed sections with sticky save bar).
  - **Phase 5 — Modals + auth.** Mobile variants of `AddBookModal`,
    `ExportModal`, and `LoginPage`. Forms render as tall
    `MobileSheet` instances with sticky two-button footers; 16px
    input font + 44pt min-height to suppress iOS Safari zoom.
    `BookSidebar` and `SetupWizard` switch from raw width gates to
    `useMobileCodepath()` so iPad portrait + any touch device get
    the full-screen sheet.
  - **Phase 6 — PWA polish.** `apple-mobile-web-app-*` meta tags
    for iOS standalone mode, `viewport-fit=cover` + safe-area
    `env()` insets so the navbar clears the iOS notch,
    swipe-to-dismiss on `MobileSheet` (touch handlers track drag,
    scrim opacity fades proportionally, 100px threshold to close).
    `theme-color` per `prefers-color-scheme` so the address bar
    matches the app theme.

- **Multi-select + bulk actions on author detail (desktop +
  mobile).** New `Select` toggle on `DiscAuthorDetailPage` and
  `MobileAuthorDetailPage` flips book cards into selection mode.
  Per-section `Select series` / `Select standalone` buttons grab
  every book in that section in one click (Mark's stated use case:
  cleaning out the 7+ unwanted series of an author with hundreds of
  books). Selection persists across cross-library tabs since IDs
  are page-wide. Bulk action bar exposes Hide / Dismiss / Delete
  with count-aware confirms, plus Select All / Deselect All. Three
  new backend endpoints — `POST /api/discovery/books/bulk-hide`,
  `bulk-dismiss`, `bulk-delete` — operate on `{book_ids: [...]}`.
  `bulk-delete` partitions Calibre-synced rows out (silently
  skipped, surfaced in the response) so a partial selection still
  succeeds.

### Mobile redesign — supporting fixes

- **Hierarchical back button.** Replaced the in-memory navigation
  history stack with a parent-page map: each mobile page declares
  its parent via a `to` prop, so the back path is predictable
  regardless of how the user got there. Author Detail → Authors;
  every other main page → Dashboard. Unified Dashboard omits the
  button entirely (it's the root). Labels match the destination so
  the user can see at a glance.
- **Author Detail series fetch URL fix.** `MobileSeriesSection` was
  hitting a non-existent `/series/{id}/books` route; switched to
  `/series/{id}?slug=…` matching the desktop path.
- **Touch detection for iPad landscape.** Added `isTouch` to
  `useViewport` via `(pointer: coarse)` so iPad Pro 12.9"
  landscape (1366px, outside `isTablet`'s 1024px ceiling) still
  takes the mobile codepath. `matchMedia` change events handle
  Magic Keyboard attach/detach at runtime.
- **`BookSidebar` cover slot.** Two follow-ups after Phase 4 UAT:
  fixed `aspect-ratio: 2/3` with `flex-shrink: 0` and 600px max-
  height so unusual cover aspects (banner, square, common in
  self-pub) letterbox cleanly inside a consistently-sized slot
  with the blurred-self backdrop visible behind.
- **Re-enrich on mobile review cards.** Brings parity with the
  desktop action — chip alongside Edit, confirm before firing.
- **Phase 1 UAT round 1.** Hamburger nav on iPads + landscape
  phones (701–1024px), enlarged collapse caret to 44pt circular
  affordance, Hermes + Pipeline dashboard sections default open,
  health-pill row wraps instead of horizontal-scrolling.

### Security

- **postcss 8.5.9 → 8.5.12** (CVE-2026-41305 / GHSA-qx2v-qp2m-jg93,
  XSS via unescaped `</style>` in CSS Stringify output, medium).
- **serialize-javascript 6.0.2 → 7.0.5** (GHSA-5c6j-r48x-rmvq RCE
  via `RegExp.flags` and `Date.prototype.toISOString()`, high; plus
  CVE-2026-34043 / GHSA-qj8w-gfj5-8c6v CPU-exhaustion DoS via
  crafted array-like objects, medium).

  Both are pulled in transitively (postcss via `vite`,
  serialize-javascript via `vite-plugin-pwa → workbox-build →
  @rollup/plugin-terser`); upstream parents haven't shipped bumps
  yet, so this release uses npm `overrides` in
  `frontend/package.json` to force the patched versions across the
  tree. `npm audit` reports 0 vulnerabilities.

### Housekeeping

- **Drop shipped TIER1/TIER2 UAT plans.** One-shot manual test
  plans for the MouseSearch port (Tier 1 MAM economy + Tier 2 SSE
  live events). Both shipped and UAT-passed in 2.0.0; the plans no
  longer match the current code and were removed. History
  preserves them at `6662b51` (Tier 2) and `d5c92a6` (Tier 1).

---

## [2.0.0] — 2026-04-24

Major release. Three tiers of MouseSearch-port work (MAM economy bundle,
SSE live torrent polling, PWA), a full mobile-responsive pass, audiobook
integration, plus Phase 4-6 UX polish + template download paths +
documentation. Net effect: Seshat is now a polished single-package
discovery + acquisition platform with first-class audiobook support, real-
time pipeline visibility, and an installable PWA.

### Major features

- **MAM economy bundle (Tier 1).** VIP auto-buy + upload-credit auto-
  buy (3 triggers: ratio floor, periodic, on-demand) + pre-download
  buffer gate + per-grab personal-FL flag + per-grab wedge offer. Live
  policy controls, audited spend log, dashboard pills. Wired through a
  hardened `bonus_buy.py` against the live MAM v1 API (verified call
  shapes documented in code).

- **SSE live torrent polling (Tier 2).** Backend qBit-monitor loop
  diffs `list_torrents` snapshots and broadcasts `torrent-progress`,
  `client-status`, `mam-stats`, `toast` events through a per-client
  `asyncio.Queue` fanout (`sse_broadcast.py`). New `/api/v1/events`
  endpoint via `sse-starlette`. Frontend `useVisibleEventSource` hook
  + `SseEventsProvider` context replace the polling intervals on
  DiscMAMPage + BookSidebar. Visibility-API auto-pause + reconnect
  with exponential backoff. Toast events route to `lib/toast.ts`
  directly. Late-connecting tabs receive a state replay via
  `seed_new_subscriber()` so client-status + MAM stats render
  immediately instead of waiting for the next change event.

- **Progressive Web App (Tier 3).** `vite-plugin-pwa` + workbox.
  Manifest, service worker, runtime caching strategies (covers
  cache-first 7d, MAM status / lists stale-while-revalidate, SSE
  network-only, default API network-first), `useNetworkStatus` hook
  + sticky offline banner, custom install prompt with 30-second
  delay + 30-day dismissal sticky in localStorage. Service worker
  silently no-ops on plain-HTTP origins; PWA layer auto-activates
  once Seshat sits behind HTTPS.

- **Mobile responsive pass.** `useViewport` hook, `MobileNavDrawer`
  slide-out, hamburger nav at ≤700px, alphabet sidebar hidden on
  mobile, BookSidebar becomes 100vw fullscreen sheet, Dashboard
  collapses to single-column stack, BList table scrolls horizontally
  inside an `overflow-x:auto` wrapper, iOS 16px-input zoom fix,
  `.author-header` + `.author-controls` reflow via CSS media queries.
  Internal-overflow second pass: `.dash-stack`, `.dash-no-minwidth`,
  `.page-header-row`, `.page-header-controls`, `.seshat-search`
  classNames + media queries to fix widget-internal layouts that
  the structural pass left desktop-squished. Ground-up mobile
  redesign queued as a future project.

- **Audiobook integration.** Audiobookshelf as a first-class library
  backend alongside Calibre. New `app/library_apps/audiobookshelf.py`
  + `app/discovery/audiobookshelf_sync.py` (3-pass sync). Audnexus +
  Audible metadata sources for audiobook-specific fields (narrator,
  duration, ASIN, abridged). Cross-library `work_links` collapse the
  same book across both libraries to one entity. Per-author format
  preferences (ebook-only / audiobook-only / both). Format tabs on
  cross-library views. Audiobook MAM pipeline grabs route via a
  dedicated `AudiobookshelfSink`. Multi-file audiobook staging fix.
  Per-author / per-library scan + clear actions with `content_type`
  filtering. Audiobook-aware enricher with separate priority list.

- **Custom download-folder templates (Phase 5).** New `template`
  mode for `download_folder_structure` with `{author}`, `{series}`,
  `{title}` tokens. Empty segments are dropped automatically — a
  standalone book in `{author}/{series}/{title}` lands in
  `{author}/{title}` without manual conditionals. Setting
  `download_folder_template` (string, default empty = matches legacy
  "author" mode). 13 new tests cover normalization, FS-safe char
  stripping, doubled-slash collapse, unknown-token handling, and
  end-to-end template rendering.

### UX polish

- **ClearMenu dropdown.** Consolidated 3-5 inline Clear buttons
  (Clear Source / Clear Ebook Src / Clear Audio Src / Clear MAM /
  Clear Both) into a single split-button dropdown across Authors /
  Author detail / Books / MAM page. Variant-tinted rows + trailing
  scope hints + click-outside / Escape close.

- **Approve / Remove MAM buttons.** New action row on BookSidebar
  for "Possible" MAM matches — Approve flips status to Found
  against the existing URL, Remove clears it and marks Not Found.
  Closes the sidebar immediately on click + refreshes parent in
  background, so the click feels instant.

- **Skeleton card loaders** on book grids during initial fetch.
  Pulse animation, matches BCard's flex shape so the layout
  doesn't reshuffle when real cards land.

- **Hover-lift on book cards.** Subtle `translateY(-2px)` + drop
  shadow on hover (desktop only via `(hover: hover) and
  (pointer: fine)` so touch devices don't get sticky-elevated cards
  after every tap).

- **Cover backdrop in BookSidebar.** Blurred-self backdrop layer +
  gradient fade so covers feel embedded in the sidebar instead of
  floating on a flat surface.

- **PhotoSwipe cover lightbox.** Click any cover in the sidebar →
  fullscreen lightbox with zoom, pan, swipe-to-close, keyboard nav.
  Code-split via dynamic import so the ~80KB only loads on first
  click.

- **Native lazy-load + fade-in covers.** Off-screen cover requests
  defer until scroll. Loaded covers fade in 350ms instead of popping.
  On long Library pages, drops first-paint cover fetches from
  hundreds to ~12 (visible viewport).

### Discovery quality

Bug fixes that landed during Tier 1-3 UAT and weren't gated to a
specific tier. Each driven by a real production failure:

- **Hardcover edition filter respects content_type.** GraphQL
  `reading_format_id` was hardcoded to `[1, 4]` (physical + ebook),
  silently excluding audiobook editions. Audiobook scans now query
  with `[2]`. `exclude_audiobooks` setting also gated off for
  audiobook scans.

- **MAM scanner per-book commit.** Was committing every 10 books;
  with rate_mam=2s the writer transaction stayed open for ~20s
  between commits and user clicks (Hide / Dismiss / Approve MAM /
  Save) queued behind it for the 30s SQLite busy_timeout. Per-book
  commit drops hold time to milliseconds.

- **Goodreads `_series_from_title_paren` fallback.** Detail-page
  parser missed `(Series Name #N)` in the title when the structured
  seriesTitle div was missing or unparseable, leading the merge
  layer to URL-backfill onto a different book. Fallback now extracts
  the series + index from the trailing parenthetical.

- **Pen-name title→series scope.** `_title_to_series_pass` queried
  `series WHERE author_id=?` and missed series rows owned by linked
  pen-name authors, leaving title-matched standalones unlinked.
  Now queries `series.id IN (SELECT series_id FROM books WHERE
  author_id=?)` to follow pen-name links.

- **Goodreads URL-backfill cover drop.** `existing_titles` check
  used substring containment, treating "Monster's Mercy" as known
  because "Monster's Mercy 2" was owned, then emitted a minimal
  BookResult with a NULL cover_url. Tightened to exact normalized
  equality + added `list_cover` to the backfill BookResult.

- **Hardcover cover fallback.** `edition.image` was sometimes null
  when `book.image` had the data; new `_pick_hardcover_cover` helper
  prefers edition image with a book-level fallback.

- **Amazon + ibdb author byline gates.** Amazon's search-card filter
  required all tokens as substrings, rejecting legit hits, while the
  detail page had zero author verification. ibdb's `score_match` was
  called with `title=title` making the title component trivially
  1.0. Both replaced with the shared `authors_match()` helper.

- **Author name normalization.** New `app/metadata/author_names.py`
  consolidates "A.K. DuBoff" / "A K Duboff" / "AK Duboff" handling —
  diacritic strip, period strip, single-letter merge. Used by
  `authors_match()` + `author_name_variants()` for query retries.

- **Same-series-position dedup.** `_title_to_series_pass` now
  deduplicates on `(author_id, series_id, series_index)` when
  assigning indices to standalones, with OWNED > non-Book-N >
  lowest-id winner selection. Catches "Remnant II" + "Remnant Book 2"
  collisions that fuzzy title match misses.

- **Cover proxy + URL fallback.** `/api/discovery/covers/{bid}` got
  a third resolution path (`_proxy_cover_url`) that streams remote
  cover URLs for Goodreads / Hardcover / Amazon / ibdb books with
  no local cover_path or ABS ID. Realistic User-Agent so CDNs
  don't 403.

### Audiobook integration (originally 1.4.0 candidate)

The major new-features section that was sitting in Unreleased. Rolled
into 2.0.0 — these never shipped under a separate version tag.



### Added

- **Audiobookshelf library backend.** `app/library_apps/audiobookshelf.py`
  adds an ABS API client + library-app adapter matching the pattern
  `CalibreApp` uses; `app/discovery/audiobookshelf_sync.py` runs a
  3-pass sync (items → authors → series) populating the per-library
  DB. Works alongside Calibre — users can have either, both, or
  multiple of each. Covers proxy through
  `/api/discovery/covers/{slug}/{bid}` so ABS covers render in the
  Seshat UI without the browser ever seeing the API key.

- **Audiobook metadata sources.** `app/metadata/sources/audnexus.py`
  and `app/metadata/sources/audible.py` land audiobook-specific
  enrichment (narrator, duration, ASIN, abridged flag). Audible also
  has a discovery-side variant in `app/discovery/sources/audible.py`
  for author / series searches.

- **Cross-library works.** New `work_links` table (pipeline DB) links
  (library_slug, book_id, content_type) tuples across libraries —
  the same book in Calibre + ABS collapses to one "work". New
  `app/works/` module (normalize + storage + matcher + preferences),
  `/api/v1/works/*` router, and a Works browser page. The matcher
  is conservative by design: exact-match + strict " - Subtitle"
  loose variant only, no trailing-volume stripping (proven unsafe
  after a Spice & Wolf / Hero-Killing Bride false-merge incident).

- **Per-author format preferences.** Users can pin an author to
  "ebook only", "audiobook only", or "both". Keyed by normalized
  name so Calibre's "Brandon Sanderson" and ABS's "Brandon Sanderson"
  share one preference row. Global default comes from
  `audiobook_tracking_mode`; per-author overrides win. Feeds into
  the Missing / Upcoming filters so an "audiobook only" author
  stops surfacing ebook rows.

- **Format tabs on cross-library views.** `/books`, `/missing`,
  `/upcoming`, `/authors`, `/series-suggestions`, `/books/hidden`
  all accept `content_type=ebook|audiobook|all`. Omit → active
  library only (legacy). Pass → aggregate. Dashboard, Authors,
  Works, and the cross-library Author Detail page surface the
  tabs in the UI.

- **Audiobook pipeline grabs (Phase 6).** MAM announce filter
  accepts audiobook categories via `accept_audiobook_announces` +
  `allowed_audiobook_categories`. The enricher holds both ebook +
  audiobook source lists at construction and swaps via
  `enrich(audiobook=True)`. Grabs are routed to either
  `CalibreSink` or the new `AudiobookshelfSink` based on
  `_is_audiobook_grab(book_format, category)` + the presence of
  `audiobookshelf_library_path`. Review queue shows narrator /
  duration / ASIN / abridged badge. `adopt_orphan_torrents()`
  picks up manually-added torrents in the watch category and
  inserts grab rows.

- **Full audiobook MAM search path.** `_mam_search` accepts
  audiobook `main_cat` ("13") via caller-supplied `content_type`;
  `_evaluate_results` inverts its category/format gating based on
  content_type; `check_book` / `scan_books_batch` /
  `run_full_scan_batch` all thread content_type through. Routers
  derive content_type from the active library via
  `_active_content_type(slug)`. The MAM Search page gained a
  library selector.

- **Unified Metadata Sources panel.** New `/v1/metadata-sources`
  GET/PUT with a `metadata_sources` + `metadata_priority` shape
  replacing the scattered `*_enabled` bools + `rate_*` floats +
  dual priority lists. Settings UI panel with 2 tabs
  (Ebook/Audiobook), 2 checkboxes per row (Enrich/Scan), arrow
  reorder, and MAM locked at #1. Legacy keys stay shadow-synced
  via `sync_legacy_keys()` during the transition.

- **`audiobook_format_priority` setting.** Default
  `["m4b", "m4a", "mp3"]`. `file_copier` applies a stable re-rank
  after largest-first, so multi-file audiobooks pick the largest
  file *within* the user's preferred format instead of the
  largest file period.

- **`abs_sync_interval_minutes` setting.** ABS library sync now
  has an independent scheduler gate mirroring the Calibre sync
  interval, so users can dial ABS scan frequency separately.
  State tracked per-slug in `state._library_last_sync_at`.

- **Dashboard redesign.** Three-column grid with a Stats rail,
  stacked Athena + Command Center left, Hermes (absorbing MAM
  Activity) middle, Quick Actions full-width bottom. Per-library
  sync states (`state._library_sync_progress[slug]`) surface as
  dual Calibre + ABS rows in Command Center with their own
  triggers, progress bars, and last-sync timestamps.
  Audiobook-aware widgets (listening hours, narrators, abridged
  split) surface when an ABS library is connected.

- **Cross-format badges.** BCard + BListRow show a 🎧/📖 indicator
  when a book is part of a linked work with both formats. Also
  surfaced as an "Also Available" row inside `BookSidebar` via a
  new `get_siblings_for_books` bulk helper.

- **Cross-library Author Detail page.** Clicking a merged Authors
  row opens a unified view with Combined / Ebook / Audiobook tabs.
  `?include_cross_library=1&slug=X` routes `/authors/{id}` and
  `/series/{id}` to the right library since the same author can
  have different row IDs in each library's DB. Per-block Scan
  buttons trigger a library-scoped lookup / full rescan.

- **Logs Announces tab.** Dedicated `seshat.mam.announce` logger
  routes parsed announces to the Announces tab in the log viewer
  without mixing into the raw IRC feed.

- **Path aliasing for qBit ↔ Seshat mount differences.** New
  `translate_path()` helper maps `qbit_path_prefix` (e.g. `/data`)
  to `local_path_prefix` (e.g. `/downloads`) so the multi-file
  audiobook backfill can scan qBit-reported paths against the
  filesystem Seshat can see.

- **Phase 8 test coverage.** 64 new tests across
  `cross_library`, covers endpoint, `_apply_tracking_mode_filter`,
  Works router, and a skip-by-default live integration scaffold.

### Changed

- **Settings consolidated and pruned.** Four audit rounds retired
  ~15 dead / legacy keys (`monthly_download_folders`,
  `policy_lookup_torrent_info`, `weekly_audit_day`,
  `weekly_audit_hour`, `cookie_check_interval_hours`,
  `pipeline_irc_enabled`, `pipeline_qbit_watcher_enabled`,
  `pipeline_notifications_enabled`, the full `*_enabled` +
  `rate_*` source-toggle set, `SourceSpec.setting_key`). Calibre-Web
  and CWA Web URLs collapsed to one field
  (writes `cwa_web_url`, reads fall through to the legacy
  `calibre_web_url`). `abs_url` + `abs_web_url` auto-mirror with
  an override. Notifications split into master SF + dimmed
  dependents. Policy section now a 2×3 grid. Hardcover API key
  moved from Sinks to Metadata Sources. Previously-hidden but
  PATCH-whitelisted paths (`calibre_library_path`, `staging_path`,
  `review_staging_path`, `cwa_ingest_path`, `folder_sink_path`)
  surfaced in the UI.

- **Bulk route ordering.** `/bulk/*` handlers now declared before
  the generic `/{id}/*` handlers in the tentative + review
  routers — FastAPI's first-match semantics were routing
  `/bulk/reject` to `/{tentative_id}/reject` with
  `tentative_id="bulk"`, yielding a 422.

- **Secret redaction.** `_SECRET_KEYS` in the settings router now
  derives from `app.secrets.SECRET_KEYS` so every encrypted-store
  key (including `hardcover_api_key` + `abs_api_key`) is
  redacted from GET /api/v1/settings automatically. Previously
  these two had drifted out of the hardcoded redact list.

- **Runtime-state keys protected.** New `_RUNTIME_STATE_KEYS`
  frozenset blocks PATCH writes for keys that background jobs
  own (`qbit_orphan_adoption_since`, `mam_validation_ok`,
  `mam_last_validated_at`, `google_books_auto_disabled_at`).
  Prevents a user clobbering the orphan-adopt cutoff from
  flooding the pipeline with adopted grabs.

- **Matcher normalization conservative.** After the Spice & Wolf
  vs Hero-Killing Bride incident, we no longer strip trailing
  volume markers. Rely on exact + `" - Subtitle"` loose variant
  plus manual linking.

- **Capitalization pass.** Title Case across every page header,
  tab, card title, modal header, and sidebar label. Live
  examples: "Tentative torrents" → "Tentative Torrents",
  "Review queue" → "Review Queue".

### Fixed

- **Multi-file audiobooks only staged the first file.**
  `_stage_for_review` now mirrors every book-format sibling from
  the staging dir; `AudiobookshelfSink.deliver` scans
  `src.parent` for audio companions. A `_backfill_audio_companions`
  helper repairs existing broken reviews by querying qBit via
  `pipeline_runs.source_path` and translating the returned
  `save_path` through `translate_path()`.

- **Cross-library author detail opened the wrong person.**
  `/authors/{id}` used the active library's DB, but the ID from a
  cross-library view could be ABS's ID (a different author than
  Calibre's row with the same number). Fixed via a `?slug=X`
  query param + `"slug:id"` compound nav arg on the frontend.

- **F5 on detail pages spun forever.** `pageArg` wasn't persisted
  to localStorage (only `page` was), so the route re-rendered
  with an undefined arg. Persisted with numeric/string roundtrip.

- **Google Books circuit breaker silently no-op'd.** The Phase 7
  migration moved consumers to `metadata_sources`, but the breaker
  kept writing the retired `google_books_enabled` key. Migrated
  breaker to write into `metadata_sources["google_books"]`
  surfaces. Surfaced via a grep-everything audit after the bulk
  key retirement.

- **Spurious legacy-DB warning every startup.**
  `_find_legacy_db` false-matched Seshat's current pipeline DB
  (`seshat.db`) and tried to read a `books` table from it.
  Narrowed to look for AthenaScout's `athenascout.db` only.

- **`accept_audiobook_announces` + audiobook settings stripped
  on PATCH.** ABS keys weren't on the settings PATCH whitelist,
  so the UI could only read them, not save them. Added
  `abs_url`, `abs_web_url`, `abs_sink_library_id`,
  `abs_sync_interval_minutes`, `audiobookshelf_library_path`,
  `audiobook_tracking_mode`, `audiobook_format_priority`,
  `audible_region`, `accept_audiobook_announces`,
  `allowed_audiobook_categories` to the whitelist.

- **Announces tab populated with AttributeError noise.** Announce
  logging used wrong `Announce` dataclass field names (`.name`,
  `.format` instead of `.torrent_name`, `.filetype`, `.vip`).

- **qBit orphan adopter flooded the review queue on first boot.**
  No grandfather line meant every pre-existing qBit torrent in
  the watch category got adopted and routed to review. Added
  `qbit_orphan_adoption_since` — a cutoff timestamp written
  the first time the adopter runs; only torrents added after
  the cutoff are eligible.

- **Re-enrich endpoint ignored audiobook priority.** Re-enrich
  always used `metadata_provider_priority` — now consults the
  grab's format and swaps to the audiobook list when appropriate.

- **ABS cover proxy dropped webp content-type.** Streaming
  response hardcoded `image/jpeg`; fixed to preserve the
  upstream content-type header.

- **Works false-merges.** Volume markers ("Vol 1", "Book 2",
  "Part I") + trailing-series strips were unifying unrelated
  books. Dropped all trailing normalization in favor of exact +
  loose-subtitle only.

- **`discover_libraries()` early-return broke composability.**
  File-based apps (Priority 1) returned without letting
  API-based apps (Priority 2) contribute. Removed the
  early-return so both paths compose.

- **Route-ordering: `/api/v1/works/author-preferences`.** Same
  class of bug as the bulk/tentative fix: the generic
  `/{work_id}` handler swallowed the static-prefix
  `/author-preferences` route because it was declared first.
  Moved `/{work_id}` below every static-prefix route.

### Removed

- **Legacy settings keys** (listed under Changed — retired across
  four audit rounds).

- **`SourceSpec.setting_key`** (unused post Phase-7 consolidation).

- **Dashboard `pipeline_qbit_watcher_enabled` toggle** (UI-only,
  had no backend effect).

- **Inline `monthly_download_folders` code** (superseded by the
  `download_folder_structure` string setting added in v1.3.0).

- **Audnexus as a standalone enricher entry / Metadata Sources
  row.** Audnexus has no title/author search endpoint — its
  `search_book()` always returns None by design — so as a
  toggleable source it always logged "no match", which led users
  (and the Phase 7 memory notes) to conclude Audnexus coverage
  was unreliable. In fact `AudibleSource` instantiates its own
  `AudnexusSource` internally and hydrates every Audible catalog
  hit through `fetch_by_asin` — that's where the narrator /
  duration / ASIN fields come from. Toggling Audible toggles
  the whole Audible+Audnexus chain; a separate Audnexus toggle
  only created confusion. Investigated via J S Morin's "Lava &
  Lightning" review row where Audible matched at 1.0 with ASIN
  `B0FKHL8X9Q` while the log showed `audnexus → no match` right
  next to a successful `GET api.audnex.us/books/B0FKHL8X9Q` —
  the Audnexus call was Audible's hydration, not the standalone
  enricher entry. The class (`app/metadata/sources/audnexus.py`)
  and every internal caller are untouched; only the standalone
  registration is gone. A one-shot `_strip_retired_sources`
  migration drops `audnexus` from `metadata_sources` +
  `metadata_priority` on first boot after upgrade.

---

## [1.3.0] — 2026-04-15

Closes the v1.2 backlog. One new feature + polish across the board.

### Added

- **By-author download folder structure.** The existing
  `download_folder_structure` setting (previously only supported
  "monthly", "yearly", and "flat") now accepts `"author"`. When
  set, completed downloads land in a normalized author-name
  subfolder inside the qBit download path (e.g.,
  `/downloads/[mam-complete]/William D Arand/`). Author name comes
  from the grab's `author_blob` (the IRC announce). Dots are
  collapsed to spaces so "William D. Arand" and "William D Arand"
  share a folder. Empty/missing author names fall back to
  `_Unknown/`. The setting is exposed as a dropdown in
  Settings → Download Client (replacing the old description that
  only mentioned Monthly/Flat). Both the dispatcher submit path
  and the budget-watcher queue-resubmit path honor the new mode.

### Changed

- **Log levels recalibrated.** 15 adjustments across
  `budget_watcher.py`, `dispatch.py`, `pipeline.py`, and
  `enricher.py`. Pattern: per-book operational detail (staging,
  epub patching, enricher per-source results, queue pop details)
  demoted from INFO → DEBUG to reduce default-level noise.
  Folder pre-creation failure promoted from WARNING → ERROR
  (genuine operational failure). Client-unreachable queueing and
  enricher budget-exceeded demoted from WARNING → INFO (expected
  resilience, not exceptional). Policy/user_status lookup
  fallbacks demoted from WARNING → DEBUG (implementation detail).

### Fixed

- **`test_scoring.py::test_partial_overlap` assertion stale.**
  The upper bound expected `< 0.6` but the scoring function
  (changed in `a09d063`) now correctly weights substring
  containment higher, producing 0.714 for "Foundation" vs
  "Foundation and Empire". Updated to `0.6 < score < 0.8`.

- **`test_pipeline.py::test_no_book_files_fails` assertion
  didn't match v1.2.3 error message.** The v1.2.3 file-list
  feature changed the error wording from "no book files" to
  "no file matching '...'". Updated to accept either form.

### Removed

- **Orphaned `CredentialsPage.tsx` deleted.** Dead code since
  v1.1.2 when credential editing moved inline to SettingsPage
  via `CredField`. Never imported in `App.tsx`.

## [1.2.4] — 2026-04-15

### Fixed

- **ibdb enricher picked up stale API field names.** The source
  was written for a pre-2026 `ibdb.dev` response shape and
  expected snake_case keys (`isbn_13`, `publication_date`,
  `pages`) plus a bare URL string for `cover`/`image`/`thumbnail`.
  Verified live during the AthenaScout v1.1.9 cross-port review:
  the API now returns camelCase (`isbn13`, `synopsis`,
  `publicationDate`, `pageCount`) and `image` is a DICT
  `{id, url, width, height}` — so the old code was shoving a
  dict into `MetaRecord.cover_url`. AthenaScout's identical bug
  crashed sqlite3 parameter binding; Seshat's record path
  doesn't bind cover_url to SQL directly, but a dict-shaped URL
  would still have blown up once it reached the downloader or
  OPF serializer.

  Fix: prefer camelCase keys with snake_case as fallback,
  extract `image.url` when image is a dict (tolerate either
  shape with `isinstance()`), and type-guard `description` /
  `language` against non-string values landing in scalar slots.

## [1.2.3] — 2026-04-14

### Fixed

- **Staging picked the wrong book file when the torrent's on-disk
  name didn't match its announce name.** `_prepare_book` built
  `source = save_path / torrent_name`, and when that path didn't
  exist (single-file torrent with a different filename, multi-file
  torrent that drops loose files into the save_path, etc.) it tried
  `_find_torrent_file` with exact / prefix / substring matches
  against `torrent_name`. When *that* also missed — common for
  MAM torrents where qBit writes names like
  `Infinite_Warship_-_Scott_Bartlett.epub` for an announce titled
  "Infinite Warship", or where the save_path is shared across many
  torrents — the code fell through to scanning the whole save_path
  and picking the alphabetically/size-first book file as "primary".

  Blast radius: every grab that couldn't resolve its own file
  ended up staging whoever else happened to be in the save_path.
  In the user's case, a Tsukimichi pack of 37 loose epubs in the
  month folder meant every mis-resolving grab since got Tsukimichi
  Volume 14 staged — metadata enricher then ran its fuzzy search
  against "Tsukimichi Moonlit Fantasy Volume 14" instead of the
  actual book, and the review queue card showed the right cover
  (from the MAM exact-ID lookup on the intended torrent) next to
  a completely unrelated staged file.

  Fix: ask qBit for the actual file list of the completed torrent
  via a new `TorrentClient.list_torrent_files(hash)` method
  (`GET /api/v2/torrents/files`). The budget_watcher threads the
  result into `process_completion` → `_prepare_book` →
  `copy_to_staging`. File paths come from qBit's own view of what
  got written to disk, so there's no string-match step that can
  go wrong. The legacy heuristic path still runs when the client
  can't introspect (other clients stub `list_torrent_files` to
  `[]`), but now FAILS loudly instead of silently scanning the
  save_path — an unresolved file is a real error, not a reason
  to stage a random other book.

### Added

- **`TorrentClient.list_torrent_files(hash)` protocol method.**
  qBittorrent implements it against `/api/v2/torrents/files` and
  returns the relative file paths. Transmission / Deluge /
  rtorrent keep the default stub (empty list) until someone needs
  it there. The pipeline treats an empty return as "couldn't
  introspect" and falls through to the legacy name-match.

## [1.2.2] — 2026-04-14

### Fixed

- **Review-queue edits looked ignored because the UI preferred the
  enricher value.** The resolved display values in `ReviewPage`
  used `enriched.X || topLevel.X`, so when a user corrected the
  author through Edit → Save edits, the save landed correctly on
  the top-level metadata (and on `grabs.torrent_name` for titles)
  but the card still rendered the enricher's pre-edit value. Flip
  to `topLevel.X || enriched.X` — edits are authoritative; the
  enricher dict is reference/provenance.
- **Enriched descriptions + languages weren't patched into the
  staged epub.** The initial staging patch in `_stage_for_review`
  was passing `title / authors / series / series_index` only. The
  enricher-returned description and language stayed in the
  review-queue row + on the UI, but the on-disk epub handed to
  CWA / Calibre had a blank `<dc:description>`, so imported books
  showed up missing synopses even though Seshat had said the
  scrapers "found" them. Now passes `description` and `language`
  to `patch_epub_metadata` at staging time — matches the re-patch
  step added by v1.2.1.

### Added

- **AthenaScout `GrabItem.category`.** Seshat now accepts an
  optional `category` on each item in the `/from-athenascout`
  payload (pairs with AthenaScout v1.1.5, which captures MAM's
  category — e.g. "Ebooks - Fantasy" — during its MAM scan and
  forwards it). `inject_grab` receives the value so the grab row
  starts with a proper category instead of an empty string. No
  schema change needed — the column has always existed; pre-v1.1.5
  AS clients still work (empty string fallback).

## [1.2.1] — 2026-04-14

Follow-up to the v1.2.0 review-edit workflow. Two issues the user
hit after the first release:

### Fixed

- **Review-queue metadata edits never reached Calibre.** The epub
  file is patched at staging time (see `_stage_for_review`) with
  the pre-edit metadata — by the time the user corrects the title
  / author / description / etc. through the Review page, the
  staged file on disk is already baked. `deliver_reviewed` then
  handed that stale file straight to the sink, so CWA / Calibre
  imported a book with the old (wrong) metadata even though the
  review-queue row + dashboard reflected the edit. Fix:
  `deliver_reviewed` now re-patches a temp copy of the staged
  epub with the current review-queue metadata before delivery,
  same shape as `_stage_for_review`'s patch step. On patch
  failure the sink still gets the unpatched file rather than
  refusing delivery.

### Added

- **Description + Language editable in the Review page.** The
  v1.2.0 edit form only exposed title / author / series /
  series_index / isbn / publisher. Now also editable:
  - **Description** — inline textarea with resize handle
  - **Language** — small input (defaults to `en`)
  `patch_epub_metadata` gained a `description` parameter so the
  `<dc:description>` element in the OPF gets the edit too.

## [1.2.0] — 2026-04-14

Operator-tooling release. Two interlocking editors — one in the
Review queue, one in the DB browser — so the user can fix bad
metadata or surgically repair a bad row without SSHing into the
container.

### Added

- **Review-queue edit workflow.** The Review page's existing Edit
  button now also offers **Save edits** (persist the metadata
  changes without approving yet) and **Re-enrich** (apply pending
  edits, then rerun the metadata scraper chain against the new
  title + author and replace `metadata.enriched` with the fresh
  result). Title edits propagate into `grabs.torrent_name` so the
  Snatch Budget widget, Recent Activity feed, and review queue
  label all reflect the correction. Direct response to the stuck
  `manual_inject_1024455` row — edit → re-enrich → approve
  replaces the "reject + resend" workaround.
  - `POST /api/v1/review/{id}/save` — metadata-only edit
  - `POST /api/v1/review/{id}/re-enrich` — save + rerun enricher
  - `grabs.set_torrent_name()` storage helper
- **Database browser writes (plan item 4.3 completion).** The
  read-only MVP from v1.1 gains click-to-edit cells, a sticky
  "N pending changes · Commit / Revert" tray, and a per-row
  delete action.
  - `POST /api/v1/db/table/{name}/update` — batch cell updates,
    validated against `PRAGMA table_info` before any write commits
  - `POST /api/v1/db/table/{name}/add` — insert new row
  - `DELETE /api/v1/db/table/{name}/row/{id}` — delete by PK
  - Writes inherit the same `_TABLES` whitelist as the read
    endpoints, so the editor can never reach `sqlite_master` or
    anything outside the expected operational tables. FK-constraint
    violations on delete surface as a readable 409 ("delete or
    reassign the dependent rows first") instead of the raw
    sqlite3 error.

## [1.1.4] — 2026-04-14

### Fixed

- **AthenaScout sends landed with `manual_inject_<id>` as the title.**
  `/api/v1/grabs/from-athenascout` was calling `inject_grab` without
  a `torrent_name`, so the default `f"manual_inject_{torrent_id}"`
  placeholder got written to `grabs.torrent_name` and leaked into
  the Snatch Budget widget, Recent Activity, the review queue, and
  (worst of all) the metadata enricher's fuzzy search — which
  returned garbage against the placeholder title. The `GrabItem`
  schema now accepts an optional `title` field; AthenaScout v1.1.4
  populates it from its own `books.title` row. Absent-title payloads
  from pre-v1.1.4 AthenaScout clients still work — they just keep
  the old placeholder behavior.

## [1.1.3] — 2026-04-14

Urgent hotfix for two v1.1.1 regressions plus a latent enricher
bug that's been silent since v1.0.

### Fixed

- **Dispatcher broken after any credential or settings save.**
  `_build_dispatcher` became async in v1.1.1 (so the filter's
  allow/ignore author sets can be loaded from the DB). The two
  rebuild call sites — `routers/credentials.py::_apply_credential`
  and `routers/settings.py::update_settings` — kept their
  synchronous `state.dispatcher = _build_dispatcher(settings)` form,
  which silently produces a bare coroutine object instead of a
  `DispatcherDeps`. Every attribute access afterward raises
  `AttributeError: 'coroutine' object has no attribute …`.
  User-visible symptoms: Send-to-Seshat inject silently fails,
  `/api/v1/grabs/budget` returns 500, budget widget goes dark,
  and the IRC pipeline effectively stops processing announces as
  soon as the user touches any setting or credential. Fix: both
  sites now `await _build_dispatcher(settings, resolved_secrets)`,
  and both pull fresh secrets from the encrypted store via a new
  `_resolve_secrets()` helper rather than injecting only the one
  being updated.
- **Hardcover metadata enricher silently unauthenticated since
  Sprint 6.** `_build_default_sources` read `hardcover_api_key`
  via a fallback to `load_settings()` whenever the event loop is
  running (always true in the dispatcher build path). The Sprint 6
  encrypted-store migration blanked that field in `settings.json`,
  so Hardcover was being instantiated with `api_key=""` on every
  enrichment run and returning None for every lookup. Fix: the
  key is now plumbed through `_build_dispatcher` →
  `_build_metadata_enricher` → `MetadataEnricher.__init__` →
  `_build_default_sources` from `resolved_secrets`, same shape as
  qbit/mam credentials. Matches AthenaScout's v1.1.1 hotfix for
  the same storage-accessor-audit miss.
- **No-match sources were invisible in the log stream.** The
  enricher only emitted an INFO line on successful matches, so
  a book that hit Goodreads/Hardcover/IBDB with no result looked
  like those sources were never queried. Added a matching INFO
  line on the no-match path so the full provider chain is
  observable. (Timeouts and exceptions were already WARNING /
  ERROR; this only changes the silent-None case.)

## [1.1.2] — 2026-04-14

### Fixed

- **AthenaScout shared API key wasn't surfaced in the UI.** v1.1.1
  added the `athenascout_api_key` credential to the backend
  `SECRET_KEYS` but put the Generate button in `CredentialsPage.tsx`
  — which turns out to be orphaned dead code (never imported into
  `App.tsx`, no route, no nav entry). Credential editing actually
  happens inline inside `SettingsPage` via the `CredField` component
  filtered into mam / qbit / api buckets. Fix: extended `CredField`
  with a `canGenerate` prop (text-input + Generate button + copyable
  value pre-save) and surfaced the new key inside Settings → API
  Keys & Sink. The orphaned `CredentialsPage.tsx` is left as-is —
  harmless dead code, flagged for cleanup in v1.2.

## [1.1.1] — 2026-04-14

Post-release polish and a v1.0 latent bug fix. Bundles the three
patches that landed on `main` after the v1.1.0 tag plus a new
shared-API-key mechanism for AthenaScout's "Send to Seshat"
integration.

### Added

- **AthenaScout shared API key.** New `athenascout_api_key` entry
  in the Credentials page. Generates a 64-char hex token (browser
  `crypto.getRandomValues`) that the user copies into AthenaScout's
  Settings. The auth middleware now accepts this token via the
  `X-API-Key` header as an alternative to the session cookie, so
  AthenaScout → Seshat service-to-service calls don't need a
  login session. Constant-time compare to blunt timing oracles.
  Value is cached in `state.athenascout_api_key` and refreshed
  whenever the credential is set or deleted — no DB hit per request.

### Changed

- **Settings page column balance.** Grab Policy moved from the
  right column to the left (semantically pairs with Pipeline and
  Review — all "what gets grabbed / what gets approved" decisions).
  Snatch Budget moved from left to right (MAM-imposed infrastructure,
  pairs with MyAnonamouse and Download Client). Evens out column
  heights and tightens the mental grouping.

### Fixed

- **Author allow/ignore lists were never loaded into the filter at
  runtime.** Latent bug from v1.0: `_build_filter_config` hardcoded
  `allowed_authors=frozenset()` and `ignored_authors=frozenset()`,
  so every IRC announce was evaluated against empty sets regardless
  of what was in the DB. The symptom the user reported: James S A
  Corey was in the allowed list but an announce for his book went
  to tentative review. Fix: `_build_filter_config` now async, reads
  both sets from `load_normalized_sets()` at startup; every mutation
  site (authors router, auto-train, tentative-promotion cron, digest
  auto-promote) calls new `state.refresh_filter_authors()` so the
  live dispatcher sees changes immediately.
- **Epub metadata patch crashed on float `series_index`.** Python's
  XML writer raises `TypeError: argument of type 'float' is not
  iterable` in `_escape_attrib` when a non-string attribute value
  is passed. The enricher and the AthenaScout handoff both produce
  floats for `series_index`. Fix: `_set_meta` now coerces `content`
  to `str` up front and guards against `None`; handles both the
  "existing meta" update path and the "create new meta" path.
- **Log viewer missed overnight history.** Ring buffer was 5000
  records (~4–6 hours during active IRC periods) and the frontend
  requested 500 lines by default. Bumped to 20000 records / 2000
  lines so a user checking the log in the morning sees the full
  overnight activity window.
- **UI polish.** Dashboard navbar widened from `NARROW_WIDTH` to
  `WIDE_WIDTH` so it matches the content pages. Stat-tile grid
  tightened from `minmax(170px, 1fr)` to `minmax(150px, 1fr)` to
  reduce asymmetry when the tile count doesn't evenly divide the
  row width.

## [1.1.0] — 2026-04-14

Quality-of-life release. Thirteen items split across two sprints —
most are direct cross-ports from [AthenaScout](https://github.com/mnbaker117/AthenaScout)
v1.1.0's playbook plus Seshat-specific integration + operator
tooling.

### Added

#### AthenaScout integration

- **Metadata handoff (plan item 1.2)** — `/from-athenascout` grab
  submissions now accept an optional `metadata` dict carrying
  AthenaScout's already-scanned book metadata (title, author,
  series, ISBN, cover URL, description, etc.). The blob is
  persisted on the grab row (new `grabs.source_metadata` column)
  and the pipeline's `_prepare_book` uses it to skip Seshat's
  own enricher chain — saves ~6 outbound scraper requests per
  book and guarantees metadata consistency with AS.

#### Operator tooling

- **Database browser (plan item 4.3)** — new **🗄️ Database** page
  for inspecting Seshat's SQLite without SSH-ing into the
  container. Left pane: whitelisted table list with row counts.
  Right pane: paginated row grid (50/page) with case-insensitive
  text-column search. **Read-only for v1.1**; cell editing, inserts,
  and deletes arrive in v1.2.
- **Build SHA display (plan item 4.2)** — Settings page footer now
  shows the git commit the running container was built from.
  Dockerfile bakes `GIT_SHA` via build-arg; `/api/version` exposes
  it; the Settings page reads it at mount time. Makes "which
  version am I actually running?" unambiguous after a pull.
- **Log viewer tabs (plan item 2.3)** — the Logs page gains two
  new tabs: **Application** (everything NOT under `seshat.mam.irc`)
  and **IRC** (only `seshat.mam.irc.*`). Drives a new `category`
  query param on `/api/v1/logs` that filters by logger-name prefix.
- **Log viewer filter input (plan item 2.2)** — case-insensitive
  substring filter narrows the visible log rows in real time.
  Matches against both logger name and message body.

#### Notifications

- **Per-event ntfy gating (plan item 4.5)** — three ntfy call
  sites in the pipeline were firing regardless of the
  `per_event_notifications` setting. Now all success-path
  notifications (`notify_pipeline_complete`, `notify_download_complete`)
  honor the setting uniformly; `notify_error` stays always-on
  because errors aren't in the daily digest and shouldn't be
  suppressed.
- **Weekly Calibre audit (plan item 5.1)** — new
  `weekly_calibre_audit` APScheduler job fires Sundays at 22:30
  local (one hour before the weekly digest). Shells out to
  `calibredb list --for-machine`, compares to
  `calibre_additions` over the 7-day window, and flags any book
  that entered Calibre outside Seshat's knowledge (manual
  add, other tool, etc.). Silent when nothing's off.

#### Resilience

- **Global per-book enricher budget (plan item 6.1)** — on top of
  the existing per-source `asyncio.wait_for` timeouts, the
  enricher now enforces a wall-clock budget across an entire
  `enrich()` call (default 60s). Per-source timeouts are clamped
  to the remaining budget so a slow late-stage source can't
  single-handedly blow the cap. Source-log entries get
  `status: "budget_exceeded"` for anything skipped.

### Changed

- **Log noise reduction (plan item 2.1)** — new `_QuietAccessFilter`
  installed on `uvicorn.access` suppresses `/api/health` records
  (~2× per minute from the Docker healthcheck). Extensible list
  for future high-frequency paths.
- **Page Visibility API for polling (plan item 4.1)** — six pages
  (Dashboard, Review, Tentative, Migration, Logs, Mam) previously
  ran their `setInterval` pollers at full speed even when their
  tab was backgrounded. Extracted a reusable
  `useVisibleInterval()` hook that pauses the interval on
  `document.hidden` and fires a catch-up tick on visibility
  return. Saves idle network traffic + battery on parked tabs.
- **Per-page content widths (plan item 4.4)** — the hardcoded
  1400px main-content cap is now a per-page lookup: data-heavy
  pages get 1400px, form/config pages get 1120px, the navbar
  stays 1120px regardless. Mirrors AthenaScout's Sprint 7.3
  convention.
- **Amazon scraper hardening (plan item 1.3)** — backported from
  AthenaScout commit `423450b`:
  - Junk-listing pre-filter (regex for third-party seller
    titles, bracketed format suffixes, "By AUTHOR - Title" sham
    listings) runs at the search-result scoring step so garbage
    never reaches the detail-page fetch.
  - Audiobook detection in `_parse_detail_page` — checks RPI
    card text + `#productSubtitle` against keywords
    (`audible`, `audiobook`, `audio cd`, `listening length`).
    Audiobook hits return None so the enricher loop falls
    through to the next source.

### Fixed

- **MAM cookie rotation log leaked token prefix** — the INFO log
  on rotation included `{old[:8]}... → {new[:8]}...`. An 8-char
  prefix has enough entropy to correlate sessions across log
  aggregators, and `docker logs` readership is wider than
  "people authorized to see the MAM session." Dropped the
  snippets entirely; fact-of-rotation is the only diagnostic
  that matters. (Same lesson as AthenaScout v1.1.1 commit
  `23e01fd`.)

### Internals (no user-visible change)

- New `frontend/src/hooks/useVisibleInterval.ts` — reusable
  visibility-aware interval hook.
- New `app/routers/db_editor.py` — read-only SQLite browser.
- New `grabs.source_metadata` column (MIGRATIONS list's first
  entry).
- `EnrichmentConfig` gains `per_book_budget: float = 60.0`.
- `DigestContext` gains `calibre_library_path: str = ""` for the
  weekly audit.
- `app/storage/grabs.py` adds a thin `get_source_metadata(db,
  grab_id)` helper so the one consumer (pipeline._prepare_book)
  doesn't drag the column through the GrabRow dataclass.

---

## [1.0.0] — 2026-04-09

Initial public release. See release notes at
<https://github.com/mnbaker117/seshat/releases/tag/v1.0.0>.

[1.1.0]: https://github.com/mnbaker117/seshat/releases/tag/v1.1.0
[1.0.0]: https://github.com/mnbaker117/seshat/releases/tag/v1.0.0
