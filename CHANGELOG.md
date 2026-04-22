# Changelog

All notable changes to Seshat are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased] — 1.4.0 candidate

Audiobook integration. Adds Audiobookshelf as a first-class library
backend alongside Calibre, with cross-library work linking so the same
book in both libraries surfaces as one entity in discovery views. The
MAM pipeline now grabs audiobooks and routes them to Audiobookshelf via
a new sink. Settings consolidated and the Dashboard redesigned to
accommodate two library backends.

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
