# v2.3 — Dual-source-of-truth metadata + Series Manager

Design doc for the v2.3 line. Captures decisions made in conversation
with Mark on 2026-05-06 so future sessions (and future Mark) have the
canonical spec to work against.

This is a living doc. Update it as decisions change during
implementation; don't let the doc drift from reality.

## Goals

1. **Calibre/ABS metadata is no longer overwritten by Seshat
   enrichment.** Sync flows pull into a snapshot table; the editable
   Seshat view drifts independently.
2. **Diffs between sources are surfaced for review**, not silently
   resolved. Replaces the existing Suggestions page with a unified
   Metadata Manager.
3. **Genuinely shared series (Halo, Star Wars, etc.) are
   first-class.** The v2.2.7 author-scope fix correctly prevented the
   Cressman/Savarovsky merge but fragmented real shared series. v2.3
   needs both behaviors.
4. **Optional push-back** so Seshat can write user-edited metadata
   back to Calibre/ABS where the platform supports it.

## Non-goals

- Real-time bidirectional sync. Push-back is an explicit user action,
  not automatic.
- Replacing Calibre or ABS as the underlying library. Both stay
  authoritative for files, ownership, and reading state.
- Multi-user metadata workflows. Single-admin model unchanged.

## Data model

### `books_calibre_snapshot`

Frozen snapshot of every Calibre-sourced field per book, refreshed on
each Calibre sync. Read-only from the user's perspective.

    CREATE TABLE books_calibre_snapshot (
        book_id        INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
        title          TEXT,
        authors_json   TEXT,    -- JSON array of {id, name, sort}
        series_name    TEXT,
        series_index   REAL,
        isbn           TEXT,
        cover_path     TEXT,    -- absolute path inside container
        description    TEXT,
        tags           TEXT,    -- comma-separated, mirrors Calibre
        rating         INTEGER, -- 0-10 in Calibre's scale
        language       TEXT,
        publisher      TEXT,
        formats        TEXT,    -- comma-separated extension list
        pubdate        TEXT,
        synced_at      REAL NOT NULL
    );

We store author/series as denormalized text (not FK to authors/series
in our schema) because the snapshot is meant to be a faithful
reproduction of Calibre's view, independent of how Seshat resolves
author/series identity.

### `books_abs_snapshot`

Same shape, ABS-specific fields included.

    CREATE TABLE books_abs_snapshot (
        book_id        INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
        title          TEXT,
        authors_json   TEXT,
        series_name    TEXT,
        series_index   REAL,
        narrator       TEXT,
        duration_sec   INTEGER,
        abridged       INTEGER, -- 0/1
        asin           TEXT,
        description    TEXT,
        tags           TEXT,
        cover_path     TEXT,    -- not always populated (covers via API)
        language       TEXT,
        publisher      TEXT,
        audio_formats  TEXT,
        pubdate        TEXT,
        synced_at      REAL NOT NULL
    );

### `books` (Seshat-live, existing table)

No structural change to existing columns. Add:

    ALTER TABLE books ADD COLUMN metadata_source_pref TEXT
        NOT NULL DEFAULT 'seshat';
        -- 'seshat' | 'calibre' | 'abs' | 'mixed'
        -- 'mixed' uses field_source_map below

    ALTER TABLE books ADD COLUMN field_source_map TEXT;
        -- JSON: {"title": "calibre", "description": "seshat", ...}
        -- Only populated when metadata_source_pref = 'mixed'.

    ALTER TABLE books ADD COLUMN user_edited_fields TEXT;
        -- JSON array of field names the user has manually edited.
        -- Used to decide which fields auto-flow on sync diff and
        -- which queue for review.

### `series.author_id` becomes nullable

    -- conceptual; SQLite needs ALTER TABLE workaround
    -- (rename table → recreate with new schema → copy → drop)

    series.author_id INTEGER NULL  -- NULL = shared series

`UNIQUE(name, author_id)` is preserved (NULL is treated as distinct
in SQLite's UNIQUE semantics, so multiple shared series with the
same name CAN exist — that's a bug we'd need to guard against in
the upsert path; treat `name + NULL` as a single "shared row" key).

### `metadata_review_queue`

Replaces the existing Suggestions table conceptually. Single queue
for all metadata diffs awaiting user review.

    CREATE TABLE metadata_review_queue (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id      INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        field        TEXT NOT NULL,        -- 'title', 'description', etc.
        old_value    TEXT,                 -- current Seshat-live value
        new_value    TEXT,                 -- proposed value
        source       TEXT NOT NULL,        -- 'calibre' | 'abs' | 'goodreads' | 'hardcover' | ...
        proposed_at  REAL NOT NULL,
        UNIQUE(book_id, field, source)     -- new scan from same source replaces prior proposal
    );

## Sync semantics

### Calibre / ABS sync (pull side)

Per book in metadata.db (or ABS API):

1. Write into the corresponding snapshot table (full overwrite).
2. For each field, compute diff vs Seshat-live.
3. **If the book is brand new** (no `books` row yet): INSERT the
   `books` row with all fields populated from snapshot. No review
   queue entries. Mark `user_edited_fields=[]`.
4. **If the book exists** (Seshat-live row already there):
   - For each field where snapshot's new value differs from
     Seshat-live:
     - If `field` is NOT in `user_edited_fields` → auto-flow:
       update Seshat-live's column directly.
     - If `field` IS in `user_edited_fields` → enqueue
       `metadata_review_queue` row.
5. Commit.

### Source-scan writes (Goodreads, Hardcover, ABDB, etc.)

Per book per source:

1. For each field the source returns:
   - If Seshat-live's column is NULL/empty → write through directly.
     This is the "first discovery" case; no review noise.
   - If Seshat-live's column has a value AND new value differs →
     enqueue `metadata_review_queue` row with `source` = the
     scanning source. Do NOT touch Seshat-live.

The empty-field-wins-immediately rule means a brand-new book
discovered via Goodreads gets fully populated on first scan, and
subsequent scans (or scans from other sources) only generate review
items for genuine differences.

### Push-back (Seshat → Calibre/ABS)

Triggered by explicit user action ("Push to Calibre" / "Push to
ABS" buttons in the compare view). Never automatic.

**ABS** (always available, all images):
- `PATCH /api/items/{id}/media` with the changed fields.
- After successful PATCH, update `books_abs_snapshot` from the
  response so Seshat-live and snapshot match again.

**Calibre, full image** (with calibredb installed):
- `calibredb set_metadata <id> --field title:"..." --field ...` for
  scalar fields, `--field cover:/path/to/cover.jpg` for cover.
- After success, re-read metadata.db for that book ID and update
  `books_calibre_snapshot`.

**Calibre, slim image** (no calibredb): RESEARCH NEEDED.
- Option α: CWA exposes a metadata-edit API. Need to inspect
  `crocodilestick/Calibre-Web-Automated` for the endpoint surface.
  If the underlying Calibre Web `/admin/book/edit/<id>` form POST
  is available, we can drive it (CSRF token + form fields).
- Option β: direct write to metadata.db + per-book metadata.opf +
  cover.jpg. Risky — getting the file/DB triplet out of sync leaves
  Calibre UI showing stale data. Effectively rewrites calibredb's
  job.
- Option γ: not viable — slim image deliberately omits Calibre.
- **Decision deferred to v2.3.2 implementation.** If neither α nor
  β is acceptable, slim users do not get Calibre push-back; that's
  a missing-feature, not a regression.

## Series Manager

### Auto-detect shared series during Calibre sync

In `calibre_sync.py` Pass 2, before the existing per-book series
upsert loop, build a map:

    calibre_series_id → set(seshat_author_id)

For each Calibre series ID:
- If the set has 1 member → upsert as `(name, author_id=that_one)`,
  current behavior.
- If the set has 2+ members → upsert as `(name, author_id=NULL)`
  (shared row). All books linked to it regardless of primary author.

This reproduces Halo's behavior (auto-shared) without re-merging
Cressman/Savarovsky (their Calibre series IDs are distinct).

### Series Manager page

Frontend page listing all series across the active library. Per row:

- Series name
- Authors involved (count + names if 2+)
- Book count
- Shared/per-author indicator

Actions:

- **Promote to shared**: select 2+ per-author rows with the same
  name → merge into one shared row. Books re-linked, old rows
  deleted.
- **Demote to per-author**: take a shared row → split into one row
  per contributing author. Books re-linked by their primary author.
- **Edit membership**: add/remove specific books from a series. (UI
  affordance for moving books between series, not just relabeling.)

For users without Calibre as a source-of-truth (or for
source-discovered books), the Series Manager is the only way to
mark a series as shared. For Calibre-backed libraries, auto-detect
covers the common case; Series Manager handles edge cases.

## UI surfaces

### Book sidebar (existing, modified)

Default display: Seshat-live values.

New: **Compare** button next to the sidebar header. Opens a panel
showing:
- Per-field columns: Seshat | Calibre snapshot | ABS snapshot.
- Differences highlighted.
- Per-field "← pull from Calibre" / "← pull from ABS" buttons that
  copy that field from snapshot to Seshat-live.
- "Push to Calibre" / "Push to ABS" buttons for fields the user
  changed locally (v2.3.2 only).

Per-field source toggle: deferred. Mark wants it; we'll evaluate
after the Compare panel ships whether per-field toggle is still
needed or whether per-field pull is enough.

### Metadata Manager page (new)

Replaces Suggestions. Three-panel layout:

- **Calibre diffs**: review_queue rows where source='calibre'.
- **ABS diffs**: source='abs'.
- **Source-scan diffs**: source IN ('goodreads', 'hardcover', ...).

Each row: book title, field, current vs proposed value, accept/reject
buttons. Bulk accept/reject across selected rows.

Settings page gets new toggles: "auto-accept Calibre diffs for these
fields" (cover, description, tags, rating) — but the default is
"all queue for review" since that's safer on first use.

### Series Manager page (new)

See above.

## Migration

One-time at first boot post-v2.3.0:

1. Snapshot tables created via standard MIGRATIONS list.
2. Backfill `books_calibre_snapshot` from current `books` rows where
   `source='calibre'`. Sets `synced_at=NOW()`.
3. Backfill `books_abs_snapshot` from current ABS-sourced books.
4. `metadata_source_pref` defaults to `'seshat'` for all books.
5. `user_edited_fields` defaults to `'[]'` (empty array).
6. `series.author_id` nullability migration: standard SQLite
   table-rename trick.
7. Existing Suggestions table is read on first boot, contents
   imported into `metadata_review_queue` with source='goodreads'
   (or whatever the original source was). Old table dropped after
   import.

The backfill assumes the current `books` columns ARE accurate
representations of what Calibre/ABS last said. That's almost true,
modulo any source-scan overwrites. Acceptable trade-off — the next
real Calibre/ABS sync corrects the snapshot.

## Phasing

**v2.3.0** — data model + Calibre/ABS dual-storage + Series Manager
(~1.5-2 weeks):
- Schema migrations (snapshot tables, nullable series.author_id,
  source pref, user_edited_fields, review queue).
- One-time backfill.
- Calibre/ABS sync rewrite to write to snapshots.
- Auto-flow vs queue-for-review logic per (3) above.
- Auto-detect shared series in Calibre sync.
- Series Manager page (browse + promote/demote + membership edit).
- No book sidebar UI changes yet — Seshat-live displayed everywhere
  as today.

**Source-scan rule (deferred to v2.3.4)**: rewriting `_merge_result`
to "write-through-on-empty + queue-on-populated" without the UI to
review queued items would accumulate unread queue rows that the user
can't act on. The owned-Calibre branch already implements the spirit
(per-field COALESCE-fill rules preserve user data), and the unowned
branch's full-overwrite has no curated data to protect. Net effect:
shipping the rule without the UI is risk without reward. v2.3.4
lands both together — see the v2.3.4 phasing section below for the
"Source-scan write rule rewrite" line item, which is exactly this
`_merge_result` change.

**v2.3.1 (shipped 2026-05-06)** — fast-follow patch from v2.2.14
UAT, before the larger UI work:
- ntfy `Title` header unicode crash fix (em-dash + smart quotes etc.
  folded to ASCII; bodies still UTF-8).
- Goodreads source-scan multi-retry loop with progress-stall
  detection. Eric Vall canary went from 174/359 (single retry) to
  expected near-completion.
- `PER_AUTHOR_BUDGET_SEC` 15min → 25min for prolific-author headroom.

The originally-planned v2.3.1 (Metadata Manager UI + per-field pull)
moved to v2.3.2 to keep the patch release small and shippable.

**v2.3.2 (shipped 2026-05-06)** — Scan quality + source URL editor:

The "scan-quality" release. Improves the per-source URL backfill
behavior and gives the user a friendly way to manage source URLs
per book. The Series Manager UX rebuild was originally planned to
ship here too but moved to v2.3.3 — the scan-quality work is a
coherent shippable unit on its own and the Series rebuild benefits
from independent UAT.

**Mandatory-source detail-fetch logic (the "Quarks and Qi" fix).**
Today the per-author `existing_titles` set fast-paths every source
on every known book — including sources that have no URL for that
book. So a book that already has a Kobo URL but no Goodreads URL
gets fast-pathed by Goodreads, which silently never tries again.

The fix: per-source-per-book gating, controlled by a new "Mandatory"
flag in `metadata_sources` settings.

- Compute two sets per author scan:
  - `per_source_existing_titles`: titles each source has URL'd
    already (parse `books.source_url` JSON per row).
  - `books_with_any_url`: titles with at least one URL from any
    enabled source.
- For each source, pass `existing_titles` as:
  - **Mandatory source** → `per_source_existing_titles[source]`.
    Fast-path only on books THIS source has URL'd. Books missing
    this source's URL still get a DETAIL fetch every scan.
  - **Non-mandatory source** → `books_with_any_url`. Fast-path on
    any book with at least one URL anywhere — preserves today's
    behavior for supplementary sources (Google Books, IBDB, Amazon).
- Default `mandatory: true` for the existing primary tier
  (Goodreads, Hardcover for ebook; Audible, Hardcover for audiobook).
  All others default `false`.
- Settings UI: new "Mandatory" checkbox column on the Metadata
  Sources panel with a tooltip explaining the trade-off.

Bounds the worst-case scan cost: mandatory_count × books rather
than total_sources × books. End state is stable — once mandatory
sources have URLs for a book, behavior settles back to today's
fast-path-everywhere.

**Source URL editor in book sidebar.** Currently editing source URLs
requires hand-writing the `{"goodreads": "...", "hardcover": "..."}`
JSON. New friendlier UX:

- One labeled input per source the book already has, with an "X"
  remove button to drop that source's URL entirely.
- One always-empty input at the bottom with a "+" button. User
  pastes any source URL → Seshat parses it, identifies the source,
  canonicalizes the URL (e.g. strips Goodreads's title slug, keeps
  only `/book/show/<id>`), and adds it to the appropriate slot.
- Backend helper: each source class gets a `parse_url(url) -> str |
  None` method that returns the canonical URL if it matches that
  source, None otherwise. UI calls each enabled source until one
  matches, then writes back to `books.source_url`.
- Per-source canonicalization rules:
  - Goodreads: `https://www.goodreads.com/book/show/<id>(-slug)?` →
    `https://www.goodreads.com/book/show/<id>`.
  - Hardcover: `https://hardcover.app/books/<slug>` → unchanged.
  - Kobo: country-domain variants → canonical `kobo.com` form.
  - Amazon: any format → strip to `amazon.com/dp/<ASIN>`.
  - Audible: strip to `audible.com/pd/<asin>`.
  - IBDB / Google Books: keep as-is (their URL shapes are already
    canonical).

**Scan-mode taxonomy** — codified contract for how each scan
entry point treats existing data. v2.3.2 verified that all entry
points behave per this table:

| Entry point | Scope | Behavior |
|---|---|---|
| Command Center "Source Scan" | All authors | Incremental — URL-backfill on books that already have non-mandatory URLs; DETAIL fetch on books missing a mandatory source's URL; full DETAIL on any book with 0 URLs. Discover any new books on each author's source pages. |
| Author detail "Re-sync" | One author | Same shape as Command Center, scoped to one author. |
| Author detail "Full Scan" | One author | **Full re-fetch.** Every book (owned, missing, hidden) gets a fresh DETAIL fetch on every enabled source. Mandatory flag ignored — everything is full-detail. Updates source URLs and re-merges all metadata. |
| Author page multi-select "Scan Sources" / "Scan Audio" | Selected authors | Same shape as Command Center, scoped to the selected authors. |
| Command Center "Full Re-scan" (v2.3.2 addendum) | All authors | `run_full_rescan` — same shape as Author "Full Scan", but spans every author. Used rarely; primarily for post-disaster recovery or schema-bump backfills. |

A library with completely cleared source data is a degenerate case:
every book has 0 URLs, so the incremental modes naturally do
full-DETAIL on every book — effectively a full scan without needing
to be invoked as one. No special-casing required.

In v2.3.4+, the same scan-mode shapes apply but writes route
through the dual-storage flow (Seshat-live + queue diffs for review)
instead of the current direct-write to `books`.

**v2.3.3** — Series Manager UX rebuild (~1-1.5 weeks):

- Drop the vague "promote / demote" verbs in favor of author-list
  membership semantics. The user's mental model is "this series has
  these authors", not "merge these rows together". The current
  per-row checkbox + bulk-Promote-to-shared button feels unclear in
  practice; drop it.
- Per-row "Manage members" action opens a modal with:
  - Current author list (computed via
    `SELECT DISTINCT author_id FROM books WHERE series_id = X`).
  - "Add author" affordance — pick an author, pick which of their
    books to add to this series.
  - "Remove author" affordance — drops every book by that author
    from the series in one shot.
- Auto-promote / auto-demote triggers off the resulting member
  count: 1 → 2+ flips `series.author_id` to NULL (shared);
  2+ → 1 flips it back to the single remaining author (per-author).
- Each series row gets a cover-image preview pulled from the first
  available book in the series (route through the existing
  `/api/discovery/covers/` endpoint).
- Existing rename + delete row actions stay.
- Backend: new `POST /api/discovery/series/{sid}/authors` and
  `DELETE /api/discovery/series/{sid}/authors/{author_id}`. The
  existing `/series/promote` and `/series/{sid}/demote` stay as
  low-level escape hatches + auto-detect path callers but are no
  longer user-facing.

**v2.3.4** — Metadata Manager UI + dual-storage UI + source-scan
write rule (~1.5-2 weeks):

- **Compare panel** in book sidebar — per-field side-by-side Seshat
  vs Calibre snapshot vs ABS snapshot, with per-field "← pull from
  Calibre" / "← pull from ABS" actions.
- **Metadata Manager page** replacing Suggestions — three review
  queues (Calibre diffs, ABS diffs, source-scan diffs), bulk
  accept/reject.
- **Per-field source toggle** (only if Compare panel doesn't cover
  the use case).
- **Sidebar edit UI populates `user_edited_fields`** when the user
  changes a field, otherwise auto-flow eats every edit on next sync.
- **Source-scan write rule rewrite**: write-through-on-empty +
  queue-on-populated for Goodreads/Hardcover/Kobo/IBDB. Lands
  alongside the UI so reviewer noise has somewhere to go.

**v2.3.5** — push-back (~1 week + research):
- ABS push-back via PATCH API.
- Calibre push-back via calibredb (full image).
- CWA push-back research; ship if feasible, otherwise document
  as slim-image limitation.

Total estimate: ~3-4 weeks from v2.3.3 onward, validated
incrementally.

## Open questions / decisions log

| Date | Question | Decision |
|---|---|---|
| 2026-05-06 | Per-field vs per-book toggle granularity | Per-field |
| 2026-05-06 | "Calibre as source of truth" semantics | Option C (per-field pull, no per-book toggle) |
| 2026-05-06 | Source-scan write rule | Write-through if Seshat-live field empty; review-queue otherwise |
| 2026-05-06 | Calibre diff auto-flow rule | Auto-flow on fields where `user_edited_fields` doesn't include the field; review queue otherwise |
| 2026-05-06 | Push-back support | Yes, deferred to v2.3.2; ABS always supported, Calibre full-image yes via calibredb, slim image research-deferred |
| 2026-05-06 | Bundle Series Manager into v2.3 | Yes, in v2.3.0 |
| 2026-05-06 | Halo-style shared series detection | Auto-detect from Calibre's books_series_link multi-author signal during sync |
| 2026-05-06 | Source-scan rule timing | Originally targeted v2.3.1; now v2.3.2 alongside the Metadata Manager UI. Owned-book branch already preserves user data via per-field rules (COALESCE-fill / smart-description / oldest-pub_date); unowned branch has no curated data to protect. Shipping queue routing without the UI to review = noise without value. |
| 2026-05-06 | v2.3.1 scope (mid-line) | Patch release with ntfy unicode fix + Goodreads multi-retry loop + per-author budget bump, ahead of the larger v2.3.2 UI work. Original v2.3.1 plan (Compare panel + Metadata Manager) shifted to v2.3.2; original v2.3.2 (push-back) shifted to v2.3.3. |
| 2026-05-06 | Series Manager UX model | Replace "promote / demote" verbs with author-list membership. The mental model is "this series has these authors", driven by per-row "Manage members" → add/remove authors. Promote/demote happens automatically based on the resulting distinct-author count (1 → 2+ = shared; 2+ → 1 = per-author). Drop the multi-row checkbox + bulk-Promote button; rename + delete row actions stay. Cover preview from first book in the series for visual identity. |
| 2026-05-06 | Series author add/remove semantics | "Add author Y to series X" = assign at least one book by Y to series X (user-selected from a book list). "Remove author Y" = detach every book by Y from series X. The series's author list is implicit, computed from `SELECT DISTINCT author_id FROM books WHERE series_id = X` — no separate `series_authors` table needed. |
| 2026-05-06 | Per-source URL-backfill gating | Per-source-per-book gate, controlled by a `mandatory: bool` flag on each entry in `metadata_sources` settings. Mandatory sources fast-path only on books THIS source has URL'd; non-mandatory fast-path on any book with at least one URL anywhere. Default mandatory=true for primary tier (Goodreads/Hardcover for ebook; Audible/Hardcover for audiobook). Bounds worst-case scan cost to mandatory_count × books rather than total_sources × books. |
| 2026-05-06 | Source URL editor UX (book sidebar) | Replace free-text JSON edit with a labeled-input-per-source list + "X" remove buttons + a single "+" add field. Each source class gets a `parse_url()` method that canonicalizes pasted URLs (e.g. strip Goodreads slug, normalize Amazon to /dp/<ASIN>). UI tries each enabled source in priority order until one matches the pasted URL. |
| 2026-05-06 | Scan-mode taxonomy | Four entry points enumerated. Three are "incremental" (Command Center, Author detail Re-sync, Author multi-select Scan Sources/Audio) and behave identically modulo scope: URL-backfill on non-mandatory sources, DETAIL on mandatory-but-missing, full-DETAIL on books with 0 URLs. The fourth (Author detail Full Scan) ignores the mandatory flag and re-fetches everything. Cleared-source-data state degenerates naturally into full-DETAIL via the 0-URLs branch — no special handling. |
| 2026-05-06 | v2.3.2 vs v2.3.3 split | v2.3.2 = scan quality + source URL editor + Series UX rebuild (validated in isolation, no dual-storage UI yet). v2.3.3 = Compare panel + Metadata Manager UI + sidebar populates user_edited_fields + source-scan write rule. v2.3.4 = push-back. Smaller, more incrementally-validatable releases beat one big v2.3.2. |
| 2026-05-06 | v2.3.2 narrowed further; +1 slot for everything else | v2.3.2 ships scan-quality (mandatory-source detail-fetch + per-source `existing_titles` gating) + source URL editor + scan-mode taxonomy verification only. Series Manager UX rebuild moves from v2.3.2 to v2.3.3 (it's a UX-only release; benefits from independent UAT). Metadata Manager UI moves to v2.3.4. Push-back to v2.3.5. Each release is smaller and more focused; the scan-quality work is the most user-visible change so it ships first. |

Update this table as decisions evolve during implementation.
