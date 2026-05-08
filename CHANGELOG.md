# Changelog

All notable changes to Seshat are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.3.6.1] — 2026-05-08

Hotfix on top of v2.3.6.

### Discovery — "Approve MAM" button on `possible` matches now flips to `found`

The BookSidebar's "Approve MAM" / "Remove MAM" decision row sends
the existing `mam_url` back unchanged on Approve, and an empty
string on Remove. Remove always worked (empty != stored URL → diff
fires, status → `not_found`). Approve has been silently broken
since v2.2.6 (2026-05-03): that release added a diff-aware
`mam_url` check to fix a different bug — saves on unrelated
sidebar fields were 400-ing when a `not_found` row had a stored
search URL. The diff check correctly skipped the URL write on
unchanged URLs, but it also skipped the side-effect that flipped
`mam_status` from `possible` → `found`. The endpoint returned
`{"status": "no changes"}` and the row was left as-is; users saw
the success toast but nothing changed.

Fix: an additional branch in `update_book` — when the incoming URL
equals the stored URL AND the current status is `possible`, write
`mam_status='found'` only. Other statuses (`found`, `not_found`,
`NULL`) still no-op on a same-URL save, preserving the v2.2.6
behavior that this whole gate was added for.

4 new tests in `tests/discovery/test_user_edited_fields.py` cover
the flip, the no-op for already-`found` rows, the no-op for
`not_found` rows with stored search URLs (the v2.2.6 case), and
the Remove flow. Suite: 1574 passing.

---

## [2.3.6] — 2026-05-07

Discovery hygiene release — two small behavioral changes that close
loops where books would silently sit unscannable. Both flow through
the same per-library tick in `mam_scheduler_loop`, so they ship
together.

### Discovery — auto-release Upcoming books on expected_date arrival

When a source scan tags a book `is_unreleased=1` with a future
`expected_date`, that book lives in the Upcoming bucket and is
explicitly excluded from MAM scanning. Previously it stayed there
until a fresh source scan happened to rewrite the row — for books
whose source reference fell off (Goodreads delisting, Hardcover
edition reshuffle), that could be never. Now, every MAM scheduler
tick, books whose `expected_date` is today or earlier (server local
time) get `is_unreleased` cleared, transitioning them from Upcoming
to plain Missing in the same tick they age in. They become
MAM-scannable on the very next eligibility query.

The clear is a single cheap UPDATE in
`app/discovery/scheduled_jobs.py`'s per-library loop, scoped to
`is_unreleased=1 AND expected_date IS NOT NULL`. No new scheduler
plumbing — runs at `mam_scan_interval_minutes` cadence (default 6h).

### Discovery — MAM scan rescans `possible` and `not_found` rows

The "books needing MAM scan" predicate widens from
`mam_status IS NULL` to also include `mam_status IN ('possible',
'not_found')`. Catalog churn on MAM means a search that came up
empty or inconclusive last week may hit cleanly today, and there's
no upside to letting those rows sit terminal. `found` is now the
only truly terminal status — every other state retries.

Predicate definitions live in `app/discovery/sources/mam.py` (the
four `_NEEDS_SCAN_*` constants), and four sites that previously
duplicated the WHERE clause inline (`routers/mam.py` × 2,
`routers/authors.py`, `scheduled_jobs.py`) now use those constants
so the predicate has one canonical home.

The widening applies to both ebooks and audiobooks (single
unified `books` table) and to every scan path: scheduled tick,
manual `/scan`, single-author scan, full library scan.

---

## [2.3.5] — 2026-05-07

Push-back release — caps the v2.3 dual-source-of-truth arc. From
v2.4.0 onward, Seshat switches to strict SemVer per
`docs/v23_metadata_design.md`.

### Discovery — push edits from Seshat back to Calibre / ABS

The Compare panel gains per-field "→ push to Calibre / ABS" buttons
that mirror the existing pull arrows: where pull copies the
upstream value into Seshat, push sends Seshat's edit upstream. Two
new bulk verbs in the modal header — "Push all my edits" and "Pull
all my edits" — operate on the book's current `user_edited_fields`
array.

Three push paths, each routed by source + image variant:

- **ABS** → `PATCH /api/items/{id}/media`. Always available when
  the book has an `audiobookshelf_id`. Maps Seshat columns to ABS's
  `metadata.*` shape (narrators ← CSV split, series ← `[{name,
  sequence}]`, abridged ← bool, etc.). Snapshot refreshed from a
  follow-up `GET /api/items/{id}` so the post-push view reflects
  ABS's normalized values, not what we sent.
- **Calibre, full image** → `calibredb set_metadata <id> --field
  title:"…" --field comments:"…"`. One subprocess invocation per
  push, all fields batched. Snapshot refreshed by re-reading
  Calibre's `metadata.db` for that book id.
- **Calibre, slim image (CWA)** → drives Calibre-Web-Automated's
  `/admin/book/<calibre_id>` form POST handler (the same one CWA's
  SPA uses). Auth flow: `POST /login` → capture session cookie →
  `GET /admin/book/<id>` → scrape CSRF token from the rendered
  form → multipart POST with `X-CSRFToken` header. CSRF cached for
  the request lifetime, refreshed on stale-session.

The unified dispatcher in `app/discovery/routers/metadata.py:
book_push` tries Calibre via `calibredb` first; falls back to CWA
when `calibredb` isn't on PATH; returns 409 with a "configure CWA in
Settings → Sinks" prompt when neither path is available.

### Settings — CWA push-back configuration

New in **Library → Sinks**: `cwa_base_url` (e.g. `http://cwa:8083`)
and `cwa_username`. The matching password lives in the encrypted
secrets store under `cwa_password` (alongside the existing
`abs_api_key` / `hardcover_api_key` / etc.). Slim users wanting
Calibre push-back must configure all three before push will work.

### Metadata — `user_edited_fields` semantics: push-clears AND pull-clears

Both verbs now **clear** the named field from `user_edited_fields`
on success. Mental model: after a push or pull, both DBs agree on
that value — there's no edit divergence left to flag, so the
"watched" tag is dropped. Future upstream changes auto-flow on next
sync (no review queue). The user re-enters the watched state by
editing the field again in the sidebar (`PUT /books/{bid}` always
diff-tracks vs. stored and adds to `user_edited_fields`).

This is a **behavior change** to v2.3.4's `/pull` endpoint, which
previously *added* to `user_edited_fields`. The bulk "Pull all my
edits" verb only makes coherent sense under pull-clears: "I'm done
editing these; align with upstream and stop flagging future
changes."

### Discovery — "Pending manual edits" tab in the Metadata Manager

UAT gap: the existing Metadata Manager only surfaces *incoming*
proposals (Calibre/ABS sync diffs, source-scan diffs, series
moves). User edits made in the sidebar landed in
`user_edited_fields` and were only visible via the per-book
sidebar badge or Compare modal — there was no centralized "what
have I edited and not yet pushed?" view.

New 5th tab **Pending manual edits** lists every book with a
non-empty `user_edited_fields` array, cross-library. Per row:
book + author + library + edited-field chips + per-row actions:
**Compare…** opens the existing Compare modal for granular
control, plus shortcut **→ Push to Calibre / ABS** and **← Pull
from Calibre / ABS** buttons that bulk-act over the book's
`user_edited_fields`. Push/Pull buttons are only rendered for
sources that actually have a snapshot for that book.

New endpoint `GET /api/discovery/pending-edits` synthesizes the
view from books + snapshot tables via the existing
`run_across_libraries` helper (so the list naturally spans
multi-library setups). Stable alphabetical-by-title ordering;
client-paginated.

### Tests + suite total

- 31 new tests in `test_push_back.py` (dispatch, UEF clearing,
  bulk-verb intersection, translation helpers).
- 4 updated/added tests in `test_metadata_compare.py` for
  pull-clears semantics + bulk pull intersection + pending-edits
  endpoint.
- Suite total: **1565 passing** (was 1522 on v2.3.4.5).

### Pre-tag arc-cap checklist

This is the last release in the v2.3 arc. Per
`docs/v23_metadata_design.md` the checklist for capping the arc
runs alongside this release: full backend test suite green
(1563 passing), CodeQL audit, Dependabot audit, browser smoke test
on the live container.

---

## [2.3.4.5] — 2026-05-07

CI-only release. No code changes from v2.3.4.4 — this exists to
re-trigger the docker-publish workflow after fixing it.

### CI — workflow handles 4-segment version tags

The docker-publish workflow used `type=semver,pattern={{version}}`
to derive image tags from a git tag. metadata-action's semver
parser requires strict SemVer 2.0.0 — three numeric segments — so
4-segment tags like v2.3.4.1/2/3/4 silently emitted no version
tags. Combined with `type=raw,value=latest,enable={{is_default_branch}}`
(which only fires on the default branch, never on a tag push),
the tag-push workflow run on every v2.3.4.X release was producing
zero image tags, and `docker buildx build --push` failed with
"tag is needed when pushing to registry". The `:latest-slim` and
`:latest` tags still got emitted from the **branch-push** workflow
run (which fired on the same commit), so Mark's setup using
`:latest-slim` kept working — but no versioned `:2.3.4.X` images
ever made it to GHCR.

Switched to `type=match,pattern=v(\d+\.\d+\.\d+(?:\.\d+)?),group=1`
plus `type=match,pattern=v(\d+\.\d+),group=1`. Both match 3-segment
(v2.3.5) and 4-segment (v2.3.4.5) tags. With this in place, the
v2.3.4.5 tag-push will emit `:2.3.4.5`, `:2.3.4.5-slim`, `:2.3`,
`:2.3-slim`, and update `:latest` / `:latest-slim` from the
branch-push run.

Suite total: **1536 passing** (unchanged from v2.3.4.4 — no code
deltas).

---

## [2.3.4.4] — 2026-05-07

UAT-driven multi-library safety + Compare panel polish.

### Discovery — slug-routing on every per-book mutation

Mark's UAT canary: he edited an audiobook's MAM URL in the sidebar.
The save returned 200 but appeared to do nothing. Closer look: the
PUT landed on the **Calibre ebook** with the same numeric id (his
calibre-library and abs-audio-library both had a row at id=68 — the
ebook and audiobook of "Accidental Champion 5", and "Horizon" the
ebook by Tabitha Lord at calibre_id=70). Calibre's metadata.db
(authoritative for ebooks) was untouched, but Seshat's working copy
of Calibre book 68 ("Horizon") had its title, description,
pub_date, isbn, series_id, and series_index overwritten with the
audiobook's values. Manual recovery: cleared user_edited_fields,
restored fields from Calibre's metadata.db at calibre_id=70, fixed
the audiobook's MAM URL on the right library row.

The dual-storage architecture from v2.3.0 saved this — Calibre
remained the authoritative source and we could rebuild the Seshat
copy from it. Push-back (v2.3.5) is the only path that would have
written back to Calibre proper, and that's user-triggered.

Fix: every per-book mutation endpoint accepts an optional `slug`
query param. When provided, the backend routes to that library's
DB instead of the active library:

  - PUT  /books/{bid}                         (the canary path)
  - POST /books/{bid}/{hide,unhide,dismiss}
  - DELETE /books/{bid}
  - POST /books/{bid}/source-urls
  - DELETE /books/{bid}/source-urls/{source}
  - POST /books/bulk-{hide,dismiss,delete}
  - GET  /books/{bid}/compare
  - POST /books/{bid}/pull

Frontend: `BookSidebar` derives a `slugQs` from `book.library_slug`
and appends it to every mutation. `BookActionHandler` signature
gained an optional `slug` arg; the eight `onAction` implementations
across desktop + mobile pages were updated to pass it through. Bulk
handlers in `DiscAuthorDetailPage` / `MobileAuthorDetailPage` route
via the page's `active_library_slug`. New `slugQuery(slug?)` helper
in `api.ts` keeps the conditional `?slug=...` suffix in one place.

Self-healing: backwards compatible. Single-library installs and
legacy callers without `library_slug` fall through to the active
library — same behavior as before. Multi-library callers route
correctly.

### Discovery — diff comparison is type-aware

The BookSidebar form re-sends every field on every save. The
v2.3.4 user_edited_fields tracking did `v != current_row[k]` which
tripped on type roundtrips: `"1.0"` (form-string) vs `1.0` (DB
REAL) compared as different, and `""` (form-empty) vs `None`
(DB-NULL) compared as different. Mark's book 68 corruption
ended up with 5 false-positive flags in `user_edited_fields`
beyond the one real `title` change. New `_norm_for_diff(field, v)`
helper normalizes both sides before comparing — empty strings
become None; series_index gets float-coerced. Pure UI re-saves
of unchanged data no longer flag fields as user-edited.

### Discovery — Compare panel surfaces series name

Pre-v2.3.4.4 the Compare panel showed "Series #" (the index) but
not the series name itself. The books column is `series_id` (FK)
while the snapshot tables store `series_name` (text), so a direct
column-to-column comparison didn't apply. After Mark's recovery
of book 68, he had to re-attach the Horizon series manually
because Compare wouldn't show or pull it.

Fix: the Compare endpoint now resolves `books.series_id` via JOIN
to `series.name` and adds a synthetic "Series" row alongside the
existing rows. The Pull endpoint special-cases `field=="series_name"`:
finds-or-creates an author-scoped series row by name (mirroring
the calibre_sync upsert), then writes `books.series_id`. Empty
snapshot series_name → clears `books.series_id` to NULL.

### UI/UX — toasts on save + pull

`saveEdit` in BookSidebar fires `toast.success("Edit saved")`
after a successful save. `CompareModal.pull` fires
`toast.success("Pulled <field> from Calibre|ABS")` after each
successful pull. Mark's UAT request — until v2.3.4.4 the only
feedback for a successful save was the form closing, which made
it ambiguous whether anything actually saved.

### Tests

5 new across 2 files:

- `test_metadata_compare.py` (+4) — TestCompareSeries: synthetic
  series row appears in the Compare response with the right
  diff flags; pull series_name creates a series row + links the
  book; pull series_name reuses an existing author-scoped series
  row instead of creating a duplicate; pull empty series clears
  books.series_id.
- `test_user_edited_fields.py` (+1) — type-aware diff regression:
  series_index "1.0" string vs 1.0 REAL is NOT flagged; "" vs
  NULL on expected_date is NOT flagged.

Suite total: **1536 passing** (was 1531 on v2.3.4.3).

### Notes

- Single-library installs are unaffected — the `slug` param defaults
  to None which routes to the active library, matching pre-v2.3.4.4
  behavior.
- The hide/unhide/delete onAction-handler signature is now `(action,
  id, slug?)`. Existing callsites that pass only `(action, id)` keep
  working but won't be slug-aware until updated to pass the third arg.
  All call sites in this repo were updated; external consumers (none
  in v2.3.x) would need to extend.

---

## [2.3.4.3] — 2026-05-07

UAT polish — bulk-action toast grammar + Hidden page owned filter.

### Discovery — bulk-hide / bulk-dismiss toast grammar

The success toast read "Hided N book(s)" / "Dismissd N book(s)"
because the handler did `${labels[kind]}d` which appended a literal
"d" to the verb form ("Hide" → "Hided", "Dismiss" → "Dismissd").
Fixed in both `DiscAuthorDetailPage.tsx` and
`MobileAuthorDetailPage.tsx` by introducing a `pastLabels` map
({ hide: "Hidden", dismiss: "Dismissed", delete: "Deleted" }) so
the toast reads "Hidden 5 book(s)" / "Dismissed 3 book(s)" /
"Deleted 2 book(s)" correctly.

### Discovery — Hidden page `owned` filter

`GET /api/discovery/books/hidden` gained an `owned` query param —
`true` narrows to owned-and-hidden, `false` to discovered-and-
hidden, omitted returns both. `DiscBooksPage` and `MobileBooksPage`
gained a `showOwnedFilter` prop; when truthy, render an
All / Owned only / Discovered only tab/chip row above the list.
The Hidden route in `App.tsx` passes `showOwnedFilter` so the
filter appears on the Hidden page only — Library / Missing /
Upcoming continue to render without it.

UAT canary: Mark accidentally bulk-hid 19 of his Calibre-owned
books during multi-select. Pre-v2.3.4.3 the only way to find them
was scrolling past every discovered miss. The Owned-only tab
surfaces them directly so they can be un-hidden.

### Tests

4 new in `test_hidden_owned_filter.py` (default returns all hidden;
owned=true narrows; owned=false narrows; combined with search).
Suite total: **1531 passing**.

---

## [2.3.4.2] — 2026-05-07

Fast-follow patch from continuing v2.3.4 UAT. Two bug fixes.

### Discovery — book sidebar save 500 on every edit

`PUT /api/discovery/books/{bid}` was returning 500 on every save
that included `mam_url` in the payload — and the BookSidebar form
re-sends every field on every save, so this was every save in
practice. Mark hit it the moment he tried to edit a title to test
the v2.3.4 Compare panel diff flow.

Root cause: the v2.3.4 user_edited_fields tracking added a SELECT
at the top of `update_book` storing the row into `current_row`,
plus a read at the bottom that does
`current_row["user_edited_fields"]`. Pre-v2.3.4 there was already
a separate inner block that diffed `mam_url` and reassigned
`current_row` to a 1-column row of just `mam_url`. The
reassignment shadowed the outer row; when the bottom merge ran,
the row didn't have `user_edited_fields` and `sqlite3.Row`
subscript raised `IndexError: No item with that key`.

Pre-v2.3.4 the shadow was harmless (nothing later read other
columns). v2.3.4 made it lethal. Fix: rename the inner
`current_row` to `mam_row` so the outer scope stays intact.
Regression test added that simulates the BookSidebar save shape
(title change + mam_url field both in payload).

### Discovery — Series Manager hides empty / fully-hidden series

UAT canary: Mark's "2B Trilogy" by Ann Aguirre showed in the
Series Manager list with "0 books, 0 owned, 0 missing". The
series row had three real books linked, but he'd hidden all three
at some prior point. The list's `book_count` column already
filtered hidden books (via the `HF` macro), so the row showed
zeros — but the row itself stayed visible because the HAVING
clause didn't filter on count.

Same shape applied to genuinely-orphaned series (15 of them in
Mark's library) — series rows that auto-detect created but no
book ever linked to. Both kinds of series are unmanageable from
the Series Manager UI: there's no Manage Members content to act
on, and the Books page already excludes them.

Fix: list endpoint defaults to `HAVING book_count > 0`, hiding
both fully-hidden and zero-book series. New `?include_empty=true`
query param surfaces them for cleanup (delete row action still
works on every series regardless). `has_missing=true` already
implies a non-zero count so it's left as the tighter filter when
set. New `TestEmptySeriesFilter` class covers all four cases.

### Tests

5 new (4 in test_series_manager.py for the empty-series filter +
1 in test_user_edited_fields.py for the mam_url-shadow regression).
Three existing tests updated to seed visible books on series they
expect to surface (the v2.3.4.2 default filter requires it). Suite
total: **1527 passing** (was 1522 on v2.3.4.1).

### Notes for the user

- **Calibre count clarity.** Your "Seshat shows 2,832 vs Calibre
  2,844" was not a sync gap — `2832 = 2851 owned − 19 hidden`.
  Live SQLite count of Calibre's own metadata.db is 2,850 books
  (CWA may be ahead of the Calibre UI's count). v2.3.4.1's
  WAL-aware mtime fix is working. The 19 hidden owned books are
  ones you've explicitly hidden in Seshat over the course of UAT;
  Calibre's UI count includes them. The two views are now
  consistent given that distinction.

- **Empty Metadata Manager is intended.** Calibre / ABS / source-
  scan diff queues are populated only when (a) a sync proposes a
  change to a field present in `user_edited_fields` (which until
  this patch you couldn't populate due to the 500), or (b) a
  full-scan source pass finds a populated-and-different value on
  a book whose Seshat-live field already has data. With v2.3.4.2
  the save flow works again; once you do field edits on books
  that Calibre / ABS will later re-sync, those diffs will show up
  in the Metadata Manager queue.

---

## [2.3.4.1] — 2026-05-07

Fast-follow patch — scheduled syncs weren't catching new books on
either Calibre or ABS, even after container restart. Symptom from
Mark's UAT (still on v2.3.3 at the time): Calibre showed 2,844
books, Seshat showed 2,823 (21 short); ABS showed 113 audiobooks,
Seshat showed 108 (5 short). Manual sync from Command Center worked
fine — the mtime gate was the only broken piece.

### Discovery — Calibre WAL-aware mtime

`LibraryApp.get_mtime` (the file-based default Calibre uses) now
takes the **max** mtime across the SQLite triplet (`.db`, `.db-wal`,
`.db-shm`) instead of just the main `.db`.

Calibre and CWA run with SQLite WAL mode on by default. Writes land
in `metadata.db-wal` first and only checkpoint back to `metadata.db`
periodically. UAT capture: Mark's `metadata.db` mtime was stale by
~24h while `metadata.db-wal` had 4MB of pending writes including 21
newly-added books. `os.path.getmtime(metadata.db)` returned the
stale value, the scheduled sync compared it to the equally-stale
saved value, and skipped on every tick. Pulling .wal/.shm into the
max collapses to the main file's mtime when the library isn't in
WAL mode (those siblings just don't exist), so legacy setups keep
working unchanged.

### Discovery — ABS lastUpdate + item count composite

`AudiobookshelfApp.get_mtime` now returns a composite string
`f"{lastUpdate}:{numItems}"` instead of `lastUpdate` alone. ABS's
`lastUpdate` advances on library-settings changes but NOT reliably
when items are added — Mark added 5 audiobooks, ABS UI showed 113
items, but `lastUpdate` was 17 days stale. The scheduled sync read
the same `lastUpdate` on every tick and skipped, even though the
content had grown.

The composite signal also moves when item count changes, so a
post-add tick (lastUpdate flat, count 108 → 113) registers as
"changed" and triggers the sync. Item count fetched via
`/api/libraries/{id}/items?limit=0` (small response, just the
`total` field). On items-endpoint failure, degrades to lastUpdate-
only (the pre-v2.3.4.1 return shape) rather than blocking the sync.

`sync_all_libraries`'s comparison is `current_mtime == last_mtime`
which works for any hashable type — no caller change needed. The
saved `library_mtimes[slug]` from before this fix is a number; the
new return is a string; the `==` comparison fails once and triggers
a single re-sync that stores the new format. Self-healing.

### Tests

8 new across 2 files:

- `test_base_get_mtime.py` (+6) — main-db-only when no WAL siblings;
  picks WAL mtime when newer; picks SHM mtime when newest; main-db
  wins post-checkpoint; missing source_db_path → 0.0; no path → 0.0.
- `test_audiobookshelf.py` (+2, plus updates to existing) — composite
  signal returned; item-count change with stable lastUpdate
  triggers a different signal; item-endpoint failure degrades to
  lastUpdate float.

Suite total: **1522 passing** (was 1514 on v2.3.4).

---

## [2.3.4] — 2026-05-07

The "Metadata Manager + dual-storage UI" release. The v2.3 dual-
source-of-truth schema (snapshots + review queue, schema-only since
v2.2.14) gets its UI: a Compare panel in the book sidebar shows
Seshat-live vs Calibre snapshot vs ABS snapshot side-by-side, and a
new top-level Metadata Manager page reviews all pending diffs from
Calibre, ABS, source scans, and source-consensus series moves. The
old Suggestions page retires — its surface folds into Metadata
Manager's "Series moves" tab.

Two v2.3.3 fast-follow bug fixes ride along: hidden-book filtering
in the Series Manager queries and authority-recompute on hide /
unhide / delete. A scan-behavior change too: hidden books now
participate in URL-only backfill in incremental scans (mirrors
full_scan), giving huge-catalog authors (John Walker — 1,069 books
on Goodreads) a way to fast-path past hidden titles via per-source
URL match.

### Discovery — hidden-book correctness (v2.3.3 fast-follow)

`_recompute_series_author` and `GET /series/{sid}/authors` now
filter `b.hidden = 0`. So a per-author Alice series with one hidden
Bob book stays per-author Alice — pre-v2.3.4 the helper counted Bob
in the distinct-author set and wrongly flipped to shared, with Bob
appearing in the Manage Members modal (where the book picker hides
hidden books, leaving him with no removable interaction).

Hide / unhide / delete (single + bulk-hide + bulk-delete) now route
through `_recompute_series_author` on the affected series id(s)
after the toggle. Without this, even with the filter fix above the
helper's pre-computed `series.author_id` went stale on every
hide/unhide.

### Discovery — hidden-book scan behavior change

Pre-v2.3.4 (per v2.2.3): hidden books were a true garbage bin in
incremental mode — `_is_hidden` blocked every UPDATE. The merge
layer dropped writes, including URL-only backfills. Net effect:
hidden books accumulated zero per-source URL ownership across
scans, forcing future scans to pay DETAIL on every unmatched title.

v2.3.4 splits the `_is_hidden` short-circuit. New helper
`_update_existing_url_only` builds a minimal UPDATE that writes only
`source_url` (merged additively) and `{source}_id` (COALESCE-fill).
Both `_merge_result` callsites (series-books path + standalone path)
call it for hidden rows instead of `continue`-ing past every write.

Net effect: hidden books still don't get metadata writes, series
claims, or series-collector contributions — `hidden = ignore` for
enrichment is preserved. But per-source URLs now accumulate, so
subsequent scans can fast-path via URL match.

### Discovery — source-scan write rule rewrite

`_merge_result`'s `if full_scan:` branch is rewritten. Pre-v2.3.4
it had two branches: owned-Calibre with per-field rules (smart
description stub-detection, oldest pub_date, COALESCE-fill for
expected_date / page_count / isbn) and unowned with full overwrite.

v2.3.4 replaces both with a uniform write-through-on-empty +
queue-on-populated rule applied per field across {description,
pub_date, expected_date, cover_url, page_count, isbn}:

- existing column NULL/empty → write through to `books`.
- existing has a value AND incoming differs → enqueue
  `metadata_review_queue` row (UPSERT on (book_id, field, source)
  so re-running the same source against the same book replaces
  prior proposals rather than piling up).
- existing matches incoming → no-op.

The dual-storage Calibre snapshot (`books_calibre_snapshot`) is the
safety net for curated metadata — `_merge_result` writes to `books`
(Seshat-live) only, so the populated→queue path keeps user-edited
values untouched until they accept in the Metadata Manager UI.

`is_unreleased` stays outside the rule (binary flag, not reviewable).
`_update_existing` now returns `(sql, vals, queue_rows)`; both
callsites execute the UPDATE and pass `queue_rows` to a new
closure-scoped `_flush_queue_rows` helper.

### Discovery — sidebar edits populate user_edited_fields

`PUT /api/discovery/books/{bid}` (the BookSidebar save handler) now
diff-tracks each field against the stored row before adding it to
`books.user_edited_fields`. The form re-sends every field on every
save, so by-presence-in-payload would falsely flag every field on
every save — the diff makes the tracking truthful.

Tracked: title, description, pub_date, expected_date, isbn,
cover_url, series_index. `source_url` is excluded (its own dedicated
editor handles canonicalization). The merge is set-union, idempotent
on repeats.

The next Calibre / ABS sync's `_apply_calibre_diff` /
`_apply_abs_diff` reads this set and routes diffs on user-edited
fields to the review queue instead of auto-flowing.

### Discovery — Compare panel (book sidebar)

New `GET /api/discovery/books/{bid}/compare` returns Seshat-live +
Calibre snapshot + ABS snapshot side-by-side with per-field
`calibre_diff` / `abs_diff` flags + `user_edited` flags +
`*_synced_at` timestamps for the Compare panel header.

New `POST /api/discovery/books/{bid}/pull` `{source, fields}` copies
named snapshot fields into `books` and adds them to
`user_edited_fields` (the user explicitly chose the snapshot value
— treat as a manual edit so the next sync's auto-flow doesn't roll
it back).

Frontend: new `CompareModal.tsx` opens via a Compare button next to
the Edit button in the BookSidebar header. Three columns (Seshat /
Calibre / ABS), per-field "← pull from X" buttons on diff cells,
graceful empty-row skipping for fields that are null everywhere.

Field map covers the dual-storage common surface area: title,
description, pub_date, isbn, series_index, tags, language, publisher,
cover_path, rating, formats, narrator, duration_sec, abridged, asin,
audio_formats.

### Discovery — Metadata Manager page (replaces Suggestions)

New top-level page `DiscMetadataPage.tsx` at the `disc-metadata`
route (was `disc-suggestions`). Four tabs:

- **Calibre** — `metadata_review_queue` rows where source='calibre'.
- **ABS** — source='abs'.
- **Source scans** — source IN (goodreads, hardcover, kobo, ibdb,
  google_books, amazon, audible). Concurrent fetches per source,
  merged client-side and ordered by `proposed_at`.
- **Series moves** — pending rows from the legacy
  `book_series_suggestions` table (the existing series-suggestions
  endpoints stay; only the page changes).

Rows on tabs 1-3 group by book — one card per book with each
diffing field beneath, per-field accept/reject + checkbox for
multi-select. Multi-select bar: "Accept all" / "Reject all" /
"Clear" via the new bulk endpoint. Bulk endpoint reports per-id
success/failure so a partial failure (e.g. a queue row's book was
deleted concurrently) doesn't abandon the rest.

A history checkbox surfaces ignored / applied series-suggestions
(tabs 1-3 hard-delete on accept/reject, so the checkbox is a no-op
there for now — contract is in place for a future soft-delete).

Backend endpoints in new `app/discovery/routers/metadata.py`:
`GET /queue`, `POST /queue/{qid}/apply`, `POST /queue/{qid}/dismiss`,
`POST /queue/bulk`. Apply coerces TEXT-stored values back to numeric
column types (REAL series_index, INTEGER page_count, etc.) before
writing.

`DiscSuggestionsPage.tsx` and `MobileSuggestionsPage.tsx` deleted.
The `disc-suggestions` route id swapped to `disc-metadata` across
App.tsx, the WIDE_PAGES set, the discovery nav, and four dashboard
files (UnifiedDashboard, DiscDashboard, MobileUnifiedDashboard,
MobileDiscDashboard). Stat cards renamed "Suggestions" → "Metadata"
(icon 💡 → 📋).

### Tests

54 new across 5 files:

- `test_series_authors.py` (+7) — hidden books filtered out of
  Series Manager queries; hide/unhide/delete trigger authority
  recompute (single + bulk variants); standalone-book hide is safe.
- `test_hidden_url_only.py` (+6) — URL-only writes on hidden books
  in both `_merge_result` paths (series + standalone); URL merge
  stays additive across sources; visible-book regression guard
  for incremental + full_scan modes.
- `test_source_scan_queue.py` (+10) — write-through on NULL /
  whitespace existing values; queue on populated-and-differs; no-op
  on match; mixed routing (one scan can write some fields and queue
  others); UPSERT on rerun; different sources get separate rows;
  owned-Calibre books follow the same uniform rule (regression
  guard on the dropped per-field branch); incremental still writes
  URL/id only; is_unreleased stays outside the rule.
- `test_user_edited_fields.py` (+7) — adds fields when value
  changed; skips when unchanged; tracks multiple fields per save;
  set-union across repeat saves; idempotent on repeat-of-same-value;
  source_url excluded; 404 on unknown book.
- `test_metadata_compare.py` (+24) — Compare endpoint shape
  (Seshat-only when no snapshots; calibre_diff / abs_diff flags;
  three-way diff; user_edited flag; empty-row skipping; 404). Pull
  endpoint (single + multi field; user_edited marker; invalid
  source → 400; field not pullable from this source → 400; missing
  snapshot → 404). Queue list (book + author joined; source filter;
  pagination). Apply / dismiss (writes + deletes; user_edited
  marker on apply; numeric coercion; 404 on unknown id). Bulk
  apply / dismiss; bulk partial failure reports per-id.

Suite total: **1514 passing** (was 1460 on v2.3.3).

---

## [2.3.3] — 2026-05-07

The "Series Manager UX rebuild" release. The Series Manager page no
longer leans on the vague "promote / demote" verbs. Users now manage
*author membership* per series — the page's authority indicator
(per-author vs shared) flips automatically based on the resulting
distinct-author count, so the user never has to think about it
directly. Cover thumbnails, pagination, and book-title search round
out the page.

The legacy promote/demote endpoints stay alive — `calibre_sync`'s
auto-detect still calls them when a Calibre series turns out to span
multiple authors, and they remain a recovery hatch for power users —
but nothing on the page UI surfaces them anymore.

### Discovery — author-list membership (replaces promote / demote)

Backend: new helper `_recompute_series_author(db, sids)` in
`app/discovery/routers/series.py` is the single source of truth for
the auto-flip rule. Given any set of series ids, it counts distinct
authors among each series's books:

- **Exactly 1 distinct author** → `series.author_id` set to that
  author (per-author authority).
- **2+ distinct authors** → `series.author_id = NULL` (shared).
- **0 books** → no-op (orphaned series; we leave authority alone so
  a freshly-emptied row doesn't silently change shape before the
  caller gets a chance to delete it).

Three new endpoints under `/api/discovery/series/{sid}/`:

- `GET /authors` — distinct authors for the series with per-author
  book counts. Drives the modal's left panel.
- `POST /authors` `{author_id, book_ids}` — assigns one author's
  books to the series. Validates each `book_id` belongs to
  `author_id`. **Captures source `series_id`s before the UPDATE** so
  cross-series moves auto-flip both ends — a 2-author shared series
  whose only contribution by author B moves out flips back to
  per-author A automatically. `series_index` cleared on the moved
  books (the index is series-scoped; carrying #6 from the old series
  into a new one produces gibberish).
- `DELETE /authors/{author_id}` — detaches every book by that author
  from the series in one shot; recomputes authority on the series.
  404 when the author has no books on this series, so URL typos
  surface clearly.

Existing `POST /series/{sid}/books` and `DELETE /series/{sid}/books/
{book_id}` were wired to the same helper so authority stays consistent
regardless of which endpoint mutates membership. The book-level
endpoint preserves its caller-controlled `indices` contract — only
the new author-level endpoint clears index-on-move.

UNIQUE(name, author_id) collision on a shared→per-author flip (an
existing per-author row of the same name) is caught and logged at
WARNING; authority is left as NULL and the membership change still
lands. The user can manually resolve via rename or delete. We chose
this over auto-merging since auto-merging would be destructive
without consent.

### Discovery — list endpoint: covers, pagination, book-title search

`GET /api/discovery/series` extended:

- **`cover_book_id`** per row. Picks the most cover-worthy book in
  the series via correlated subquery — books with any cover signal
  (`cover_path` / `cover_url` / `audiobookshelf_id`) come first,
  then `series_index ASC NULLS-LAST` (via `COALESCE(idx, 9999)`),
  then `pub_date ASC`, then `id ASC`. Hidden books excluded so the
  list doesn't surface a cover the user explicitly pruned. The
  frontend hits `/api/discovery/covers/{cover_book_id}` directly.
- **Pagination** — new `limit` (1–200, default 50) + `offset`
  (≥0, default 0) query params. Response shape gained `total`,
  `limit`, `offset` alongside the existing `series` array. `total`
  is the post-filter pre-pagination count via `SELECT COUNT(*) FROM
  (... GROUP BY s.id ... HAVING ...)`.
- **Search** now matches series name OR primary author name OR
  **book title**. The book-title match goes through a `s.id IN
  (SELECT series_id FROM books WHERE title LIKE ?)` subquery — NOT
  a row-level `b.title LIKE` clause. The latter would have shrunk
  the GROUP BY's `book_count` to only the matching books (e.g.
  searching "Reach" on a 5-book Halo would have reported
  `book_count=1`). Regression-tested.

### Discovery — Series Manager page UX rebuild

`frontend/src/pages/DiscSeriesPage.tsx` rewritten:

- Dropped the per-row checkbox column + bulk "Promote to shared"
  button (the multi-row promote flow felt like a no-op in practice
  and confused the membership mental model).
- Larger row layout (~12px vertical padding) with a 72×108 cover
  thumbnail on the left, lazy-loaded against `/api/discovery/covers/
  {cover_book_id}` with a placeholder when null.
- Per-row **Manage members** button replaces the old promote /
  demote flow. Rename + delete row actions stay.
- Search input now mentions "series, author, or book title" and
  debounces (250ms) so each keystroke doesn't fire a request.
- Prev/next pagination at the bottom when `total > 50`. Shows
  "showing N–M of T" in the header so the user can orient.

New component `frontend/src/components/ManageMembersModal.tsx`:

- Top section: current authors with per-author book count and a
  Remove button per row. Remove confirms ("Detach K books by X
  from <series>?") then calls the new DELETE endpoint and refreshes.
- Bottom section: **Add author** flow — debounced author
  autocomplete against `/api/discovery/authors?search=`, then a
  book picker filtered to that author's full library (any series,
  standalone). Each book row shows a 36×54 cover thumbnail + the
  book's current series as a small badge ("currently in: <series>"
  or "standalone"). Books already on the destination series render
  as disabled "already on this series" rows so the user understands
  what's already done.
- Submit calls the new POST endpoint and refreshes both the modal
  and the parent list.
- Authority indicator (per-author / shared badge) updates live in
  the modal header as the count crosses 1 ↔ 2.

### Tests

28 new tests:

- `tests/discovery/test_series_authors.py` (18) — covers GET
  authors (distinct list + book counts, empty case, 404), POST
  authors (dest flip to shared, source flip back when emptied,
  wrong-author rejection, unknown book/author rejection, empty/
  missing-field rejection), DELETE authors (shared→per-author flip,
  0-book orphan no-op, 404 on no-author-on-series, 404 on unknown
  series), book-level endpoint auto-flip (add → dest flips, source
  flips back; remove → shared→per-author).
- `tests/discovery/test_series_manager.py` (10) — list pagination
  (response shape, paginate across 5 seeded series, limit > 200 →
  422), search (matches series name / author name / **book title**
  while preserving full `book_count`), `cover_book_id` (returns
  first by index, prefers books with covers, NULL for empty
  series).

Suite total: **1460 passing** (was 1432 on v2.3.2).

---

## [2.3.2] — 2026-05-06

The "scan-quality" release. Two user-visible improvements + one
tighter contract on how source scans are gated. Series Manager UX
rebuild moves to v2.3.3 — the scan-quality work is a coherent
shippable unit and the Series rebuild benefits from independent UAT.

### Discovery — mandatory-source detail-fetch (the "Quarks and Qi" fix)

Pre-v2.3.2 the per-author `existing_titles` set fast-pathed every
source on every known book — including sources that had no URL for
that book. Result: a book with a Kobo URL only would be silently
fast-pathed by Goodreads, which never tried again to find a
Goodreads match. Mark's "Quarks and Qi" by J.L. Williams was the
canary: in his library, no Goodreads URL despite Goodreads having a
matching page at `/book/show/246416427`.

New per-source `mandatory: bool` flag in `metadata_sources` settings
+ `is_source_mandatory(settings, name)` accessor. In
`_lookup_author_inner`:

- Compute `per_source_titles_with_url: dict[str, set[str]]` and
  `titles_with_any_url: set[str]` from each book's `source_url`
  JSON.
- Per source in the scan loop:
  - **`full_scan` mode** → existing behavior unchanged.
  - **Mandatory source** (incremental) →
    `per_source_titles_with_url[source]`. Books missing this
    source's URL trigger DETAIL fetch every scan until matched.
  - **Non-mandatory source** (incremental) → `titles_with_any_url`.
    Pre-v2.3.2 behavior preserved for supplementary sources where
    DETAIL on every unmatched book would be wasted effort.
- Mirrored in the multi-retry loop so retries inherit the same
  per-source gating.

Defaults: `mandatory=true` on the primary tier (Goodreads /
Hardcover for ebook; Audible / Hardcover for audiobook).
`mandatory=false` everywhere else. Settings → Metadata Sources panel
gains a "Mandatory" checkbox column with a tooltip explaining the
trade-off. `is_source_mandatory` falls back to the ship-with default
when an upgraded settings.json (pre-v2.3.2) lacks the field on
existing entries — keeps users behaving correctly without an
explicit migration write. MAM's checkbox is locked off (it's not
part of the source-scan registry).

Bounds worst-case scan cost: `mandatory_count × books` rather than
`total_sources × books`. End state stable — once mandatory sources
have URLs for a book, behavior settles back to today's
fast-path-everywhere.

### Discovery — source URL editor UX

Editing source URLs used to require hand-writing the
`{"goodreads": "...", "hardcover": "..."}` JSON in a free-text
field — a known papercut. Replaced with a structured editor in the
book sidebar:

- Each existing source URL gets a labeled row (Goodreads,
  Hardcover, Kobo, …) with the URL shown read-only + an "✕" remove
  button. Removing immediately calls
  `DELETE /api/discovery/books/{bid}/source-urls/{source}` and
  re-renders.
- One always-visible "Add" row at the bottom: paste any source URL
  + "+" button (or Enter). Backend
  (`POST /api/discovery/books/{bid}/source-urls`) identifies which
  source the URL belongs to + canonicalizes it before merging into
  the book's `source_url` JSON.
- Bad URLs surface a 400 with the backend's message inline beneath
  the input rather than via toast — user can fix the paste in
  place without losing what they typed.

New `app/discovery/source_urls.py` module with
`parse_url(url) -> (source_name, canonical_url) | None`.
Canonicalization rules per source:

- **Goodreads**: strips title slug, keeps `/book/show/<id>`.
- **Hardcover**: lowercases the slug.
- **Kobo**: drops `/<country>/<lang>/` prefix, normalizes to /us/en/.
- **Amazon**: any regional domain → `https://www.amazon.com/dp/<ASIN>`.
- **Audible**: any regional domain → `https://www.audible.com/pd/<ASIN>`.
- **IBDB**: keeps `?id=<n>`, strips other query params.
- **Google Books**: classic `/books?id=` and new `/books/edition/`
  URL shapes both fold into the canonical form.

Legacy plain-string `source_url` values (pre-v1.x format) surface
under a "manual" pseudo-source so they remain visible + removable;
adding any new URL silently overwrites the legacy string.
`source_url` removed from `EditFields` so other field edits no
longer round-trip the URL dict.

### Discovery — scan-mode taxonomy verified

Four scan entry points, three "incremental" + one "full":

| Entry point | Scope | Mode |
|---|---|---|
| Command Center "Source Scan" | All authors | Incremental |
| Author detail "Re-sync" | One author | Incremental |
| Author detail "Full Scan" | One author | Full re-fetch |
| Author multi-select "Scan Sources / Audio" | Selected authors | Incremental |

All flow through `lookup_author(..., full_scan=...)`; the v2.3.2
mandatory-source gating only kicks in on `full_scan=False`. Full
scans pass `existing_titles=hidden_titles` (pre-existing behavior)
so every non-hidden book gets DETAIL on every source regardless of
URL presence.

A fifth entry point exists internally (`run_full_rescan` —
`full_scan=True` across every author) for post-disaster recovery /
schema-bump backfills; documented in the design doc but not a
common UI surface.

Verified all four user-facing entry points hit the right `full_scan`
value. No code changes required.

### What's deferred

- **Series Manager UX rebuild** → v2.3.3 (was originally v2.3.2).
  Drop "promote / demote" verbs; per-row "Manage members" modal;
  cover preview; auto-promote/demote on member-count crossing.
- **Compare panel + Metadata Manager UI + source-scan write rule**
  → v2.3.4 (was v2.3.3).
- **Push-back to Calibre / ABS** → v2.3.5 (was v2.3.4). Each
  release shifts up one slot to accommodate.

### Tests

44 new across `test_source_config.py` (5), `test_source_urls.py`
(28), `test_source_url_editor.py` (11). Suite 1432 passing.

---

## [2.3.1] — 2026-05-06

Two fast-follow fixes after Mark's v2.2.14 rollout surfaced them.
This is a smaller patch release than the originally-planned v2.3.1
(Compare panel + Metadata Manager UI); that work moves to v2.3.2.

### Notify — daily digest no longer crashes on em-dash titles

`ntfy.send` was setting the notification's `Title` HTTP header to the
raw user-facing string. The daily digest's title contained an
em-dash ("Daily digest — N new books"); httpx defaults headers to
ASCII / latin-1 encoding and raised `UnicodeEncodeError`, which
`send` swallowed at the catch-all and returned False. The whole
notification dropped silently.

New `_ascii_header_safe` helper folds common typographic
punctuation (em-dash, en-dash, smart quotes, ellipsis, bullet,
arrows, NBSP) to ASCII equivalents and drops anything else via
`encode("ascii", "ignore")` rather than crashing. Bodies are still
sent UTF-8 in the request body, so notification content is
unaffected; only the Title header is folded. New tests cover the
em-dash, smart-quote/ellipsis, and unmapped-character (Japanese)
cases.

### Discovery — multi-retry loop for slow Goodreads days

Pre-2.3.1 the source-scan retry pass did exactly one retry per
timed-out source, then logged "retry ALSO timed out" and moved on.
Mark's manual scan of Eric Vall (359 books) hit this on a slow
Goodreads day: first attempt processed ~100 books, retry got to
~174, leaving ~185 books unscanned with the per-author budget still
half-full.

The retry pass is now a loop. Per source that timed out and
preserved `_partial_state`:

- Continues retrying as long as the per-author budget has at least
  30 seconds left AND the prior retry advanced the index.
- Each iteration resumes from the source's `_partial_state["index"]`
  with `min(spec.timeout_sec, remaining_budget)` as the timeout.
- Hard ceiling at 8 retries as a sanity guard; normally the loop
  exits via the budget gate or via a clean source completion.
- Stall detection: if two consecutive retries don't advance the
  index, treats it as a soft outage rather than a slowdown and bails
  rather than burning the rest of the budget on guaranteed timeouts.

Per-author budget bumped from 15 minutes → 25 minutes
(`PER_AUTHOR_BUDGET_SEC = 25 * 60`). Eric Vall at 3.5s/book on a
slow Goodreads day = ~21 minutes just for Goodreads; the old 15min
cap couldn't accommodate that even with multi-retry.

### What's deferred to v2.3.2

The Compare panel (per-book Seshat vs Calibre/ABS snapshot diff
with field-level pull) and Metadata Manager page (replaces
Suggestions, surfaces all three review queues) move to v2.3.2.
Source-scan write rule + sidebar edit UI populating
`user_edited_fields` move with them. Push-back to Calibre/ABS
moves to v2.3.3.

---

## [2.3.0] — 2026-05-06

First minor on the v2.3 line. Activates the dual-source-of-truth
metadata flow that the v2.2.14 schema groundwork put in place, plus
the user-facing Series Manager. See `docs/v23_metadata_design.md`
for the full design.

### Discovery — Calibre/ABS sync now writes to snapshot tables

Pre-v2.3, every Calibre or ABS sync overwrote the `books` row's
metadata columns directly. Manual edits or source-scan enrichment
got clobbered on the next sync. v2.3.0 splits the writes:

- `books_calibre_snapshot` / `books_abs_snapshot` get a full
  overwrite per sync — they faithfully mirror what Calibre / ABS
  said NOW. INSERT OR REPLACE; no merging.
- `books` (the editable Seshat-live row) is touched per-field by
  `_apply_calibre_diff` / `_apply_abs_diff`. For each field where
  the snapshot differs from Seshat-live:
  - If the field IS in `user_edited_fields` → INSERT OR REPLACE
    into `metadata_review_queue` with `source='calibre'` (or
    `'abs'`). UPSERT keyed by `(book_id, field, source)` so repeat
    contested syncs replace prior pending proposals rather than
    piling up.
  - Otherwise → UPDATE the books column directly (auto-flow).
- Structural fields (`author_id`, `series_id`, `owned`,
  `calibre_id` / `audiobookshelf_id`, `source`) always write
  through directly; they're identity-tier, not user-editable.

The Calibre helper guards against `cover_path=None` mid-sync
(ABS-style transient where the cover wasn't computed yet) — those
diffs are skipped rather than blowing away an existing cover with
NULL.

ABS-specific normalization: `abridged` flattens to bool/None from
the ABS API but the books column stores INTEGER NOT NULL DEFAULT 0.
`_normalize_abs_value` coerces both sides for the comparison, so a
no-op resync (False vs None) doesn't generate spurious queue rows.

Until manual edits and source scans start populating
`user_edited_fields` (which lands with the v2.3.1 sidebar edit UI
and source-scan rule rewrite), the queue stays empty and Calibre /
ABS auto-flow everything as before — preserving current behavior
while the snapshot tables get populated for future use.

### Series Manager

New page under Discovery → Series. Backend exposes six mutation
endpoints on the existing `/api/discovery/series` router:

- `POST /series/promote` — merge 2+ per-author rows into one shared
  row (`author_id=NULL`).
- `POST /series/{sid}/demote` — split a shared row into per-author
  rows; books re-link by primary author.
- `PATCH /series/{sid}` — rename. 409 on `(name, author_id)`
  conflict surfaces `conflict_id` so the caller can offer "merge
  into existing".
- `DELETE /series/{sid}` — remove the row; books fall back to
  standalone (`series_id=NULL, series_index=NULL`).
- `POST /series/{sid}/books` — bulk-add with optional per-book
  indices.
- `DELETE /series/{sid}/books/{book_id}` — detach a single book.

GET `/series` gains a `?shared=true|false` filter so the Series
Manager can list shared and per-author rows in distinct sections.

The frontend page lists every series with a multi-select for promote,
per-row demote (shared only) / rename / delete actions. Modal-quality
prompts use `window.alert / prompt / confirm` for v2.3.0 — the proper
modal experience lands with v2.3.1's Metadata Manager UI work.

These mutations exist alongside (not in place of) the auto-detect
path that v2.2.14 added to `calibre_sync`: Calibre's organization
handles the common shared case (Halo) without user intervention;
Series Manager covers the cases where Calibre's organization
doesn't tell us — source-discovered books not yet acquired,
coincidentally-named series merged in error, undoing an auto-decision
the user disagreed with.

### What's deferred to v2.3.1

- **Source-scan write rule** (Goodreads / Hardcover / Kobo / IBDB
  enrichment): the design called for "write through if Seshat-live
  empty; queue diff if populated". Shipping that without the UI to
  review queued items would just accumulate unread queue rows. The
  owned-Calibre branch in `_merge_result` already implements the
  spirit (per-field COALESCE-fill, smart-description, oldest-pub_date
  rules preserve user data), and the unowned branch's full-overwrite
  has no curated data to protect. Net effect: shipping the rule
  without the UI is risk without reward. v2.3.1 lands both together.
- **Compare panel + Metadata Manager UI** — the per-book diff view
  with field-level pull from snapshot to Seshat-live, plus the
  unified review queue page that replaces Suggestions.
- **Per-field source toggle** — depends on Compare panel; deferred.

### What's deferred to v2.3.2

- **Push-back to Calibre / ABS** — Seshat → ABS PATCH and
  Calibre `calibredb set_metadata` for full-image users. Slim image
  push-back contingent on CWA API research.

### Tests

26 new in `test_calibre_sync_snapshot_diff.py`,
`test_abs_sync_snapshot_diff.py`, and `test_series_manager.py` covering
snapshot writes, auto-flow vs queue routing on user-edited fields,
the cover-path NULL guard, abridged normalization, repeat-contested-sync
UPSERT semantics, and every Series Manager mutation (promote, demote,
rename including 409 conflict, delete, membership). Suite 1385
passing.

---

## [2.2.14] — 2026-05-06

Halo regression fix + forward-compatible schema groundwork for the
v2.3 line. The schema additions are inert in this release (no code
reads from the new tables/columns yet); they ship now so the v2.3
sync rewrite doesn't have to bundle a heavy migration with a heavy
behavior change.

### Discovery — Calibre sync auto-detects shared series

The v2.2.7 author-scope fix correctly prevented the Cressman/Savarovsky
"The Last Paladin" merge but had an unintended side-effect: genuinely
shared series — Halo (75 books across 15 authors), Star Wars Legends,
franchise novels — got fragmented into one per-author row each. Mark's
live DB ended up with 15 separate "Halo" series rows after Calibre
sync.

calibre_sync now pre-aggregates `calibre_series_id → set(seshat_author_id)`
before the Pass 2 series upsert. The decision per Calibre series id:

- 1 contributor → upsert as `(name, author_id=N)`. Per-author rows
  stay author-scoped (Cressman/Savarovsky case unchanged).
- 2+ contributors → upsert as `(name, author_id=NULL)`. Shared row;
  every book regardless of primary author links to it.

Pass 2 also re-points and deletes any pre-existing per-author rows
of the same name (the legacy v2.2.7 fragmentation state), restricted
to authors who actually contribute to *this* Calibre series so
unrelated same-named series elsewhere aren't swept up. Mark's live
Halo fragmentation will self-heal on the next Calibre sync after
upgrading.

### Schema — forward-compatible v2.3 groundwork

Schema-only changes; no behavior. The v2.3 sync rewrite needs these
in place before it can land, and getting the migrations out the door
in their own release lets us validate them in isolation.

- `series.author_id` becomes nullable (NULL = shared series).
  Migration uses a code-driven recreate-table dance because SQLite
  has no `ALTER COLUMN DROP NOT NULL`. Idempotent — checks
  `PRAGMA table_info` first; no-op once nullable.
- New `books_calibre_snapshot` and `books_abs_snapshot` tables —
  frozen Calibre/ABS metadata per book. Empty in v2.2.14; populated
  by the v2.3 sync rewrite.
- New `metadata_review_queue` table — unified diff queue with
  `UNIQUE(book_id, field, source)` so repeat scans replace prior
  proposals rather than piling up.
- New `books` columns: `metadata_source_pref` (default `'seshat'`),
  `field_source_map` (JSON, populated only in mixed mode),
  `user_edited_fields` (JSON array, default `'[]'`).
- Cold-start backfill seeds `books_calibre_snapshot` from current
  owned-Calibre `books` rows and `books_abs_snapshot` from rows
  with `audiobookshelf_id` populated. Idempotent — only INSERTs
  when no snapshot row exists. Runs once at first boot post-2.2.14.

Tests: 12 new in `test_v23_schema.py` + 3 new in
`test_calibre_sync_series_dedup.py` (multi-author shared, distinct
ids stay per-author, legacy collapse). Full suite 1359 passing.

See `docs/v23_metadata_design.md` for the v2.3 design spec these
foundations support.

---

## [2.2.13] — 2026-05-06

Three fixes from continuing UAT.

### Discovery — ABS scheduled sync was reading a stale startup-cached lastUpdate

`AudiobookshelfApp.get_mtime` returned `library["abs_last_update"]`,
populated once at startup from the `/api/libraries` discover() call
and never refreshed. After the first sync, every scheduled tick
compared the cached startup value against itself, perpetually
short-circuiting with "source unchanged, skipping." Mark added 66
audiobooks overnight, restarted multiple times for updates, and saw
zero scheduled syncs reflect them — only manual Command Center
syncs worked, because they bypass the mtime gate.

`LibraryApp.get_mtime` is now async on the base class. The Calibre
implementation still does `os.path.getmtime` (sync work inside an
async wrapper). The ABS override hits `/api/libraries` on every
call, finds the matching library by `abs_library_id`, returns its
current `lastUpdate`, and refreshes the cached value on `library`
in place. Falls back to the cached value on API failure / missing
key / library no longer in ABS — refusing to write 0 into
`library_mtimes` would force a full re-sync after a transient
outage, which is worse than a one-tick miss.

Three call sites updated to `await`: `main.py` (startup sync),
`discovery/scheduled_jobs.py` (interval tick), `discovery/routers/scan.py`
(manual sync mtime stamp). New tests: live API fetch updates the
cache, API-failure falls back to cache, no-API-key falls back to
cache, missing library returns 0.

### MAM — scheduled + manual scans now sweep all libraries

The scheduled MAM scan loop and POST `/api/discovery/mam/scan` both
operated on the active library only. Mark's audiobooks went
unscanned through both paths until manual scans against the active
ABS library — even when the schedule fired correctly.

`mam_scheduler_loop` iterates `state._discovered_libraries` per
tick. For each library: open its DB via `get_db(slug=...)` (avoids
flipping global active_library mid-tick and risking UI cross-talk),
count remaining `mam_status IS NULL` books, run a 150-book batch
with the library's content_type and the matching format_priority
(`audiobook_format_priority` vs `mam_format_priority`). Per-tick
budget: 150 × n_libraries. Aggregate progress accumulates across
libraries via per-library closure baselines. New `current_library`
field on the progress dict so the UI can label which library is
currently in flight.

POST `/scan` does the same: snapshots eligible book IDs from each
library's DB at scan start (preserving the snapshot guarantee per
library), then iterates libraries sequentially with 150-book
batches and 1-min inter-batch pauses. `limit` query param caps the
TOTAL across libraries — earlier libraries fill first, later ones
get whatever's left. Response: `{status, total, libraries}` so
callers can see which libraries were enrolled.

IP-registration failures break out of the loop early (every library
would hit the same wall). Per-book errors are tallied via
`on_progress` and don't abort. Cancel honors the same flag the
single-library code did — checked between libraries and on every
per-book boundary inside `mam_scan_batch` via `cancel_check`.

Out of scope: `/full-scan` (separate endpoint with per-library
`mam_scan_log` persistence; remains library-scoped for now),
`/test-scan` single-batch endpoint, per-author/per-book scans
(already library-specific by definition).

### Discovery — Cressman/Savarovsky calibre_sync re-merge fix (continued from v2.2.12)

v2.2.12 fixed the calibre_sync series fast-path to be author-scoped,
preventing a second-rename merge. The on-disk DB splits applied
during the v2.2.12 release got undone by a Calibre sync against the
v2.2.10 image (Mark hadn't pulled v2.2.12 yet). Re-applied at
v2.2.13 release time:

- `seshat_calibre-library.db`: id=609 → Cressman (5 books),
  id=1735 → Savarovsky (4 books).
- `seshat_books.db`: id=609 → Cressman, new id=1868 → Savarovsky.

With v2.2.13's calibre_sync code in place, future Calibre renames
of the Savarovsky series will not re-merge.

---

## [2.2.12] — 2026-05-06

Two discovery-correctness fixes from Mark's continuing UAT. Both
manifested through the same canary: renaming Roman Savarovsky's
"The Last Paladin" series in Calibre back to its real name (after
a manual rename to break the v2.2.6-era collision with John
Cressman's same-named series) silently re-merged both authors'
books onto a single Seshat series row, AND surfaced an unrelated
"Book" series row containing four of Savarovsky's standalones
("Guardian's Journey (Book #1/#3)", "The Last Paladin
(Book #4/#9)").

### Discovery — calibre_sync's series fast-path is now author-scoped

`calibre_sync.py:309` looked up incoming Calibre series by global
`LOWER(name)` before falling back to the per-author normalized
match. The block's own comment claimed cross-author hits were
"deliberately ignored" — but the implementation contradicted the
comment. v2.2.7 fixed the same pattern in
`_ensure_series_for_author` for the source-scan path; the Calibre
sync path was missed. So renaming Savarovsky's Calibre series back
to "The Last Paladin" caused the next sync to find Cressman's row
globally and assign Savarovsky's books to it.

The lookup is now author-scoped (`LOWER(name) = LOWER(?) AND
author_id = ?`). The Pass-2 normalized fallback was already
author-scoped, so this just brings the fast path in line. The
`series.UNIQUE(name, author_id)` composite already supports
per-author rows. New `tests/discovery/test_calibre_sync_series_dedup.py`
covers the Cressman/Savarovsky case + same-author idempotency.

### Discovery — `_extract_series_signal` rejects volume-marker series names

`_RX_PAREN_SERIES_REF` matches `(<name> #N)`. The `<name>` group
required only `len >= _MIN_PREFIX_LEN` (4). "Book", "Volume",
"Episode", "Chapter" are all exactly 4+ chars, so titles like
"Guardian's Journey (Book #1)" extracted `("Book", 1.0)` instead of
recognizing "(Book #N)" as a positional marker without a series
name. Four Savarovsky standalones across two real series clustered
into a single fictitious "Book" series row, plus another real-world
hit on Morgan Rice's "Born of Dragons (Age of the Sorcerers—Book
Three)" / "Turned" pair.

New `_VOLUME_MARKER_WORDS` denylist (book, bk, vol, vol., volume,
part, episode, ep, chapter, tome, installment) applied at three
return sites:
1. Arm 1 (parenthetical) — rejects volume-marker-only names AND
   captures the index as `volume_hint` for the bare-prefix arm
   below. So "Guardian's Journey (Book #3): subtitle" now yields
   `("Guardian's Journey", 3.0)` instead of `("Book", 3.0)`.
2. `_strip_prefix_marker` — won't return "Book" as a base from
   `_RX_PREFIX_TRAILING_NUM` matches against e.g. "Book 4".
3. Arm 2 bare-prefix and no-colon paths thread `volume_hint`
   through, so the colon and no-colon variants both extract a real
   series name + the captured index.

5 new positive parametrize cases (the 4 Savarovsky titles +
"Some Saga (Volume #2)"), 3 new negative cases ("Book 4",
"Volume 3", "(Book #1)").

### Data — one-off splits and bogus series cleanup

Live-DB projection applied at release time:

- `seshat_calibre-library.db`: series id=609 "The Last Paladin"
  reattributed to John Cressman (author 580); Savarovsky's
  previously-quarantined row id=1735 renamed back to "The Last
  Paladin" (author 549); Savarovsky's books moved from id=609 to
  id=1735.
- `seshat_books.db`: series id=609 reattributed to Cressman (571);
  new row inserted for Savarovsky (540) with id=1868; Savarovsky's
  books moved over.
- Bogus `name='Book'` series rows id=2799 (Savarovsky, 4 books)
  and id=2714 (Morgan Rice, 2 books) deleted; their books detached
  to standalone for the next discovery scan to re-cluster via the
  fixed logic.

Forward-only — earlier collapsed cross-author rows still need
manual splits if any others surface in continuing UAT. The
`UNIQUE(name, author_id)` composite means a fresh manual rename
in Calibre is now safe to undo.

---

## [2.2.11] — 2026-05-05

Repo owner rename. The GitHub account hosting Seshat moved from
`mnbaker117` to `malevolenttortoise`. All references in code,
docs, Docker image tags, badges, error messages, and the Hardcover
`User-Agent` header now point at the new owner.

GitHub redirects the old owner to the new one for ~1 year, so
existing pulls, links, and bookmarks keep working — but anything
new should use `ghcr.io/malevolenttortoise/seshat:latest` (or
`:latest-slim`) and `github.com/malevolenttortoise/seshat`.

No code changes, no data migration, identical container behavior.

### Changed

- All `ghcr.io/mnbaker117/seshat` references swapped to
  `ghcr.io/malevolenttortoise/seshat` (compose example, README,
  Dockerfiles, Unraid template, DEPLOY.md, calibredb error
  diagnostic block).
- All `github.com/mnbaker117/seshat` URLs swapped to
  `github.com/malevolenttortoise/seshat` (README badges, SECURITY
  advisory link, Unraid template support/project/icon URLs,
  CHANGELOG release links, NOTICE).
- `Hardcover` source `User-Agent` updated to the new repo URL.
- `tests/sinks/test_calibre.py` assertion updated in lockstep with
  the calibredb diagnostic.
- `LICENSE` + `NOTICE` copyright holder updated (same legal
  entity, new pseudonym).

---

## [2.2.10] — 2026-05-05

Security release. CodeQL triage on the now-public repo flagged 19
alerts; 9 of them mapped to 4 small code changes worth making, and
the other 10 were false positives matching the admin-trusted threat
model documented in `SECURITY.md`. This release ships those 4 fixes;
the false positives are dismissed in the GitHub Security tab with
the same reasoning.

### Security

- `metadata/writer.py`: replace deprecated `tempfile.mktemp()` with
  `NamedTemporaryFile(delete=False)`. Eliminates the create/use
  TOCTOU race; same atomic-replace semantics. Closes CodeQL #1
  (`py/insecure-temporary-file`).
- `routers/delayed.py`: new `_validate_filename()` helper rejects
  any path-traversal segments (`/`, `\`, NUL) and enforces the
  expected `<grab_id>_<mam_id>.torrent` regex BEFORE any filesystem
  call, in both `reinject` and `delete` handlers. Pre-fix, reinject
  ran the regex AFTER `fpath.exists()` and delete had no validation
  at all. Closes CodeQL #11–#14 (`py/path-injection`).
- `routers/covers.py`: switch the allowed-roots check from
  `str(target).startswith(str(root))` to `target.is_relative_to(root)`.
  Closes the prefix-match edge case (sibling `/staging-evil/` would
  have slipped past `startswith("/staging")`). Closes CodeQL #7–#10
  (`py/path-injection`).
- `mam/irc.py`: extend the auth-payload redaction in `_send` to
  cover `PASS` and `OPER` commands, not just `AUTHENTICATE` and
  `PRIVMSG NickServ`. Defense in depth. Closes CodeQL #5
  (`py/clear-text-logging-sensitive-data`).

No runtime behavior changes for normal operation — these are all
hardening of input-validation and resource-handling paths.

---

## [2.2.9] — 2026-05-05

Documentation and licensing release ahead of public visibility. No
runtime behavior changes — the image is functionally identical to
2.2.8.

### Changed

- License switched from MIT to Apache License 2.0. Apache 2.0 keeps
  Seshat fully open source while adding two protections MIT lacks:
  explicit attribution requirements (so "rename and resell" forks
  become a license violation) and a trademark clause covering the
  Seshat name and logo. Patent grant included.
- New `NOTICE` file at the repo root, required by Apache 2.0 §4(d)
  for downstream attribution.
- README badge row reworked for public release: dynamic GitHub
  release tag, CI build status, last-commit date, and GHCR image
  size badges (slim + full) replace the static placeholders. The
  static `tests-625_passing` badge was removed in favor of the live
  build status, which won't go stale as the test count grows.

---

## [2.2.8] — 2026-05-05

Bug fix: the Hermes Dashboard widget went blank and the MAM Status
page showed `HTTP 403 from jsonLoad.php`, even though every other MAM
operation (search, grab, IRC announce, scans) kept working. Root cause
was that `/api/v1/mam/status` and a handful of other endpoints were
reading `mam_session_id` straight from `settings.json` instead of the
live in-memory cookie that gets rotated on every MAM API call. The
settings.json copy was a stale plaintext value left over from before
the encrypted-store migration; MAM eventually rejected it as
`Invalid/missing cookie`. The rest of Seshat already used the canonical
in-memory token via `mam_cookie.get_current_token()`, which is why
only the read-only status surface broke.

### Fixed

- `routers/mam.py` `_build_status` and `validate` now read the active
  token via the new `mam.cookie.get_active_token()` helper
  (in-memory → encrypted store → settings.json fallback) instead of
  `settings["mam_session_id"]` directly. This is what unblocks the
  Hermes widget and the MAM Status page.
- Same change applied to the other endpoints that had the latent
  same-pattern bug and would have manifested it on the next migration:
  `routers/economy.py` `_require_token` (vip/upload/personal-FL buy +
  preflight), `routers/enums.py` `refresh_enums`, and the bulk
  multi-author / multi-book MAM scan endpoints under `discovery/routers/`.
- `secrets.migrate_from_settings()` now blanks `settings.json` for
  every secret key that has a live encrypted-store value, even when
  no fresh migration happened on this boot. The pre-2.2.8 routine only
  blanked on first migration, so a value already in the encrypted
  store on boot left its plaintext settings.json sibling stranded
  forever — that's how the stale `mam_session_id` got there.
- Lifespan keep-alive + cookie-retry gates and the discovery library
  config / multi-{author,book} scan gates now use the resolved token
  (`mam_cookie` / `_mam_ready`) rather than `settings["mam_session_id"]`,
  so they don't false-disable when the new migration blanks the field.

### Migration

No manual steps. On first boot of v2.2.8, `migrate_from_settings`
silently blanks the stranded plaintext copy in your `settings.json`
and the affected endpoints start using the live rotated cookie. The
Hermes widget will repopulate within a few seconds of the next status
poll.

---

## [2.2.7] — 2026-05-04

UAT-driven discovery improvements from Mark's continuing
author-by-letter walkthrough.

### Discovery — broader omnibus / collection detection

`_RX_OMNIBUS` was missing several real-world collection patterns,
leaving books "hanging at the end" of numbered series instead of
routing to the Omnibus / Collections sub-row. New arms:

- `Full Series` (literal, in subtitle or parenthetical)
- `Volume Set` anywhere
- `boxed set` (was only `box set`)
- `N-Book Collection / Set / Bundle / Omnibus` ("3-Book Boxed Set",
  "6-Book Collection")
- Widened `complete <terminator>` list (`anthology`, `stories`,
  `novellas`, `novels`, `chronicles`, `short fiction`, `graphic
  novel series`)
- Widened "complete X X X..." inter-word window from 1 to 1-5 +
  added more terminators (`collection`, `anthology`, `edition`,
  `set`, `bundle`, `tales`)
- Generic `: The Complete ...` subtitle (accepts the rare single-
  book FP — Mark prefers occasional over-flagging since FPs can be
  promoted back to a numbered entry manually)
- `(complete)` parenthetical anywhere — series-status annotation
  some sources pull into the title field

The existing startup `_backfill_omnibus_flag` reapplies the new
regex to `is_omnibus=0` rows on container restart, so no data
migration is needed — the upgrade is automatic.

Live-DB projection: 52 unflagged missing rows flip on next
backfill (0 owned — zero risk to the library).

### Discovery — orphan-series promotion (Warden Locke canary)

Some authors have entire series that every source returns as
standalones with no `series_index` tag. The Warden Locke case: 9
"Player Slayer: ... Episode N" books, 4 "Manassassin N (Manassassin
#N)" books, 3 "Soulless Rising N (Soulless Rising #N)" books — all
cataloged as standalones because no source asserted a series.
`_title_to_series_pass` only links to series that already exist,
so it couldn't help.

New `_orphan_series_promotion_pass` runs after
`_title_to_series_pass` and bootstraps series from clusters of
standalones with shared prefixes plus per-book numeric markers.
Two signal arms:

- **Parenthetical** — title contains literal `(SeriesName #N)`.
  Strongest signal; the source already named the series, the
  parser just dropped it.
- **Prefix + volume marker** — `<Prefix>: ... Episode N` /
  `Book N` / `Volume N` / `Vol N` / `Part N` / `Chapter N`, or
  `<Prefix> N`, or `<Prefix> Book N: subtitle` (volume marker
  stripped from prefix so the series name doesn't end in "Book"
  — Borgy60 canary: "The Last Legend Reborn Book 2: subtitle"
  correctly yields series "The Last Legend Reborn", not "...
  Reborn Book").

Clusters need ≥ 2 members with explicit numeric indices to
promote. Bare-prefix members ("Dungeon Depot: Slice of Life ...")
default to index 1. Owned, hidden, and `_is_omnibus`-matching
rows are skipped (the title-shape check protects against stale
`is_omnibus=0` columns from rows inserted before the latest regex
update).

Live-DB projection: 52 books promoted across 8 authors on next
scan. 0 owned books touched.

### Discovery — author-scoped `_ensure_series` (collision fix)

The John Cressman / Roman Savarovsky "The Last Paladin" collision:
both authors had a series with the same name, and the global
`LOWER(name) = LOWER(?)` lookup in `_ensure_series` collapsed them
into one row. Mark had to rename Savarovsky's manually to break
them apart.

`_ensure_series` is now author-scoped. Lookup checks (current
author + pen-name partners via `pen_name_links`) before falling
back to INSERT. Pen-name sharing is preserved (Darren and Arand
still share the "Incubus Inc." row). Unrelated authors with the
same series name now get their own per-author rows automatically.

The `series.UNIQUE(name, author_id)` composite already supported
this — the bug was just that the lookup wasn't scoping. With the
fix, Mark can rename Savarovsky's series back to "The Last
Paladin" and it'll coexist with Cressman's row indefinitely.

Existing collisions (already-collapsed rows) still need manual
splits; this fix is forward-only.

---

## [2.2.6] — 2026-05-03

UAT-driven fix surfaced after v2.2.5 stabilized the container. Mark
hit "Save does nothing" on the BookSidebar edit form. Latent bug
since the AthenaScout→Seshat merge (Phase 2 port at dd22c43); MAM
scan running today happened to push a book into the state that
exposes it.

### Discovery — book edit no longer 400s on system-stored search URLs

`check_book` writes a `/tor/browse.php?...` search URL into
`books.mam_url` when it returns `STATUS_NOT_FOUND`, so the user
can click through to MAM's search page and verify manually. The
`update_book` PUT handler then validated `mam_url` against a strict
torrent-URL regex (`/t/<id>`) on every save — even when the user
hadn't touched the field. Combined with the BookSidebar form
re-sending every field on every save, this rejected edits to
unrelated fields (title, series, etc.) with a 400.

Diff-aware now: the handler reads the current `mam_url`, compares
to the incoming value, and only validates / writes when the user
actually changed it. Two side benefits:

- An empty form field on a never-scanned row used to fall through
  to the "explicitly cleared" branch and stomp `mam_status` to
  `'not_found'` even though the user touched nothing. That can no
  longer happen.
- Search URLs already in the DB stay put without round-tripping
  through validation.

### UI — BookSidebar Save surfaces errors via toast

`saveEdit` used to silently swallow API errors with `catch {}`,
which is what made the 400 above look like "the button does
nothing." Now toasts the server error message (or a generic
fallback) so the next time something rejects a save, the user sees
why.

---

## [2.2.5] — 2026-05-03

Hot-fix release. v2.2.4 left a latent crash on the
`/discovery/books/{bid}` PUT path: the BookSidebar edit form sends
`series_index: ""` when the user clears the series-number field
(intended as "blank means I don't know the position / it's an
omnibus collection"), and the backend wrote it to SQLite verbatim.
A REAL column accepting a TEXT empty string is fine on write — but
the next container restart blows up at startup when
`_dedupe_same_series_position` reads the row and runs
`float(r["series_index"])`:

    ValueError: could not convert string to float: ''
    ERROR:    Application startup failed. Exiting.

The trace surfaced via FastAPI's lifespan-merge spam (one frame per
mounted router, because every router contributes a lifespan that
the merger nests). The actual culprit was a single line in
`init_db`'s startup-migration chain.

### Discovery — startup-migration scrub for empty series_index

`_dedupe_same_series_position` now runs a one-time
`UPDATE books SET series_index = NULL WHERE TRIM(...)=''` pass
before grouping. Idempotent; no-op on healthy DBs. Unsticks the
container without touching the user's actual series data — the
empty string conveyed no information anyway, so coercing to NULL
matches the intended "no position known" semantics.

### Discovery — boundary coercion in update_book

The PUT `/api/discovery/books/{bid}` handler now coerces
empty/whitespace `series_index` to `None` before writing. Stops new
bad rows from going in and matches every other code path
(`lookup.py`, `calibre_sync.py`, source modules) that already
treats absent series-position as NULL.

---

## [2.2.4] — 2026-05-03

Patch release. Two UAT-driven fixes from Mark's continued A→Z hide
walkthrough: a regressed Unhide button, and an omnibus-only series
that vanished from its author page when the user wired the omnibus
into the series.

### UI — Unhide button on the Hidden page now actually unhides

`disc-hidden` routes to `<DiscBooksPage>`, whose generic `onAction`
handler had branches for `hide / dismiss / delete` but **none for
`unhide`**. Clicking Unhide on a hidden book fired
`onAction("unhide", id)` from the BookSidebar, the if-chain missed,
no API call went out, the list reloaded with the book still hidden,
and the user saw the entry stay put. The dedicated `DiscHiddenPage`
component had the unhide call but is currently dead code on this
route. Same dead branch existed in `MobileBooksPage`.

Added the missing `if (act === "unhide") await api.post(.../unhide)`
branch in both desktop and mobile generic books-page handlers.

### Discovery — author detail surfaces omnibus-only series

Adding a series name to a Calibre-owned omnibus (e.g. setting
"Master of Thieves" on the omnibus "Master of Thieves: The Complete
Series") used to leave the book in limbo:

- It moved out of Standalone (`series_id` no longer NULL).
- It also did not appear under the series on the author detail page.

The author detail series query at `authors.py` filtered series with
`HAVING author_book_count > 0`, where `author_book_count` was a
non-omnibus, non-hidden count. The HAVING clause was originally
introduced to drop series whose every book by an author was hidden;
it accidentally also dropped series whose every book by an author
was an omnibus (the title-pattern detector flips `is_omnibus=1` on
"The Complete Series", "Box Set", "Trilogy: Omnibus", etc.).

The fix splits visibility from progress accounting: HAVING now
checks `author_visible_count > 0` (omnibus included) so the series
renders, while the displayed count badges keep using the
non-omnibus count so progress reflects actual entries rather than
collections. The IS section already had an "Omnibus / Collections"
sub-row to surface those rows once visible.

A new `author_omnibus_count` is also returned per series so the
IS count badge can show "Omnibus" instead of a misleading "0/0"
when this author's only contribution to the series is a collection.
The mobile author detail section gets the same treatment.

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
most are direct cross-ports from [AthenaScout](https://github.com/malevolenttortoise/AthenaScout)
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
<https://github.com/malevolenttortoise/seshat/releases/tag/v1.0.0>.

[1.1.0]: https://github.com/malevolenttortoise/seshat/releases/tag/v1.1.0
[1.0.0]: https://github.com/malevolenttortoise/seshat/releases/tag/v1.0.0
