# Changelog

All notable changes to Seshat are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.22.0] — 2026-05-23

### Added — Cross-library person identity hardening + Persons & IDs UI

Closing arc on the cross-library author work started in v2.20.0.
Audit of the production identity graph (727 multi-link persons across
calibre-library + abs-audio-library) revealed five distinct issues:

1. **~108 missing source-ID mirrors** across linked siblings (66 Goodreads,
   17 OpenLibrary, 14 Google Books, plus Amazon/Hardcover/etc.)
2. **One genuine ID conflict** (J Foster Ward — calibre and abs disagree
   on `goodreads_id`)
3. **10 orphan authors** with no `author_links` row at all
4. **Orphaned `author_links`** pointing to deleted authors (no FK
   cascade across DB files)
5. **Stub-creation churn** producing empty-`normalized_name` rows that
   hygiene's empty-cleanup then prunes, leaving links dangling

All five now closed in v2.22.0 via:

#### Source fixes (prevent recurrence)
- ABS sync's INSERT now populates `normalized_name` and falls back to
  a normalized-name lookup before creating a duplicate stub (the
  primary churn root cause). Three additional INSERT paths in
  `author_mirror.py`, `routers/books.py`, and `routers/import_export.py`
  also fixed.
- `get_or_create_person` gains a fuzzy fallback: when exact normalized
  lookup misses, scans `persons` via `authors_match` (`SequenceMatcher`
  ≥ 0.92) and reuses the existing person with `link_confidence='low'`
  so the new link surfaces on the Author Triage page for review.
- `mirror_bio()` helper analogous to `mirror_source_id()` — bio writes
  now propagate to linked siblings AND populate canonical `persons.bio`.
  `lookup.py`'s author-metadata write path calls it after the local
  write.
- Hygiene's empty-cleanup now cascades to `author_links` (deletes the
  global rows before dropping the per-library `authors` rows).

#### Cleanup (one-shot reconciliation via 3 new hygiene jobs)
- **Job 7 — Orphan author retrolink:** walks every per-library author
  lacking an `author_links` row and calls `get_or_create_person`.
  Idempotent. Closes the gap left by paths that bypassed the
  identity hook (`author_mirror.backfill_dual_author_rows`,
  routers/books, routers/import_export).
- **Job 8 — Cross-library person backfill:** the headline job. Walks
  every multi-link person and, for each `MIRRORABLE_SOURCE_ID_COLUMN`,
  COALESCE-fills NULL siblings. Two-or-more-unique-values conflicts
  resolve via **ebook-wins** policy with the displaced value logged
  to `author_id_audit_log` (so it's recoverable). Also backfills
  `persons.bio` from any non-null sibling bio (longest wins, matches
  `_consolidate_persons` tiebreak) and clears the broken John
  Birmingham `image_url` regression (`/books/...` path = book cover,
  not author photo).
- **Job 9 — Prune orphan `author_links`:** safety net wrapper around
  the existing `prune_orphan_links` function. Runs every hygiene pass.

#### UI (manage from one place)
- **Author Triage** now shows the per-library author's actual name
  next to each link so the low-confidence-link review surfaces name
  mismatches at a glance. Backend bulk-fetches names via a new
  two-pass query — no N+1.
- **Persons & IDs** (new page under Discovery → Tools) — table of
  every multi-link person with their union source IDs across linked
  libraries. Edit a single field, the existing
  `PATCH /persons/{id}/source-id` endpoint writes through to every
  linked library via `mirror_source_id`. Divergent values highlighted.

### Fixed — Metadata-cache "Queue" tile no longer pins at total enrollment

Pre-v2.22.0 the "Queue" stat tile displayed `pending` from the queue
table — but the worker re-enqueues every row to `status='pending'`
after a successful scan with `next_scan_due_at = now + rescan_cadence`,
so `pending` always equals enrolled-author-count once the steady
state is reached. During a fresh backfill the tile stayed pinned at
the target value (e.g. "645") instead of counting down.

Status endpoint now emits:
- `due_now` — pending AND `next_scan_due_at <= now()` (work the
  worker may pick up on the next tick)
- `scheduled_later` — pending AND `next_scan_due_at > now()` (cooling
  off until the next scheduled scan)

UI "Queue" tile reads `due_now` with `scheduled_later` shown as a
tooltip. "Authors cached" tile also rewired to show author-level
(de-duplicated across libraries) `unique_ok_authors / unique_total_authors`
so a 2-library setup no longer reports the doubled per-library row
count (e.g. "645 / 645" instead of "1289 / 1289").

### v3.x backlog — Author image rework

Audit confirmed author image scraping is functionally dead — only 1
of 737 calibre authors has an `image_url`, and that 1 is a Goodreads
book-cover URL (selector at `goodreads.py:519` matches the wrong
element). Job 8 clears it as a workaround. Proper rebuild
(re-derive selector + extend `metadata_cache_amazon_state` with
`author_image_url` + opportunistic Hardcover/Audible fallbacks)
queued for v3.x.

---

## [2.21.2] — 2026-05-23

### Fixed — `dry_run` and `mam_irc_enabled` are now real kill switches

Both toggles in Settings → MAM are operator-facing "stop grabbing"
controls. Pre-v2.21.2 they were both read **only at lifespan startup
/ dispatcher build time** and never re-read on a running container.
Flipping either in the UI saved the new value to `settings.json`
correctly but the running dispatcher and IRC listener task ignored
it — the toggles looked like they did something but didn't actually
stop grabs from happening until the next container restart.

Caught the hard way during a real incident 2026-05-23: an operator
tried disabling IRC listener, enabling dry_run, and disabling MAM
search to halt grabs in flight — none of them worked. The only
thing that stopped Seshat was changing the IRC NickServ password
to kill the protocol-level connection.

The fix wires a `_live_kill_switch_state()` helper at the top of
`handle_announce` and `inject_grab` that reads both settings from
`load_settings()` on every call. The settings are mtime-cached so
the per-announce cost is effectively zero.

**Behavior change:**

| Setting | Pre-v2.21.2 | v2.21.2 |
|---|---|---|
| `mam_irc_enabled = False` | Took effect only at container restart | Next IRC announce is skipped with `reason='irc_listener_disabled'`. Audit row still recorded. Manual injects unaffected (user-initiated). |
| `dry_run = True` | Took effect only at container restart (and only if `_build_dispatcher` ran with it set) | Next IRC announce + next manual inject both skip with `reason='dry_run_live'`. Audit row still recorded. |

The IRC listener task itself stays connected to MAM when
`mam_irc_enabled` is flipped off — announces are dropped at the
application layer with zero MAM-side side effects (no torrent fetch,
no qBit add). Actually disconnecting the IRC client on toggle is
deferred to a follow-up. The immediate "stop grabs" need is solved
by the dispatch-level gate.

**Note on `mam_enabled`:** this setting **only gates the
discovery-side MAM book search**, not the IRC → dispatch → qBit
pipeline. It always did, this just makes the contract explicit:
"disable MAM" in Settings → Sources doesn't stop IRC autograbs. Use
`mam_irc_enabled` for that.

5 new tests in `tests/orchestrator/test_dispatch.py::TestLiveKillSwitches`
cover the four combinations (irc-disabled + dry_run on/off, IRC announce
vs manual inject) plus the missing-settings default-permissive case.

### Operator note

If you've been bitten by this and have grabs in flight you didn't
intend, the new gates take effect on the next announce after this
upgrade. The legacy "restart Seshat to actually apply the toggle"
workaround still works as a belt-and-suspenders.

---

## [2.21.1] — 2026-05-22

### Fixed — qBittorrent 5.2 JSON response shape for `torrents/add`

qBit 5.2 (and the hotio image's current build) replaced the legacy
plain-text `Ok.` / `Fails.` response from `POST /api/v2/torrents/add`
with a JSON shape:

```json
{"added_torrent_ids": ["<info-hash>"], "failure_count": 0}
```

The client previously only recognized `Ok.` / `Fails.` and fell
through to the "unexpected response" path, marking the grab
`failed_unknown` even though qBit had accepted the torrent and was
downloading it. The dispatcher then never emitted "submitted",
never fired the `notify_on_grab` ntfy, and never proceeded to the
post-download pipeline — so successfully-grabbed torrents
silently disappeared from Seshat's view.

The client now recognizes the JSON shape:

- `added_torrent_ids` non-empty + `failure_count == 0` → success
- `added_torrent_ids` non-empty + `failure_count > 0` → success
  (we only send one torrent, so `added` is authoritative)
- `added_torrent_ids` empty + `failure_count > 0` → `rejected`
- Anything else → falls through to the existing "unexpected
  response" path so future qBit response variants surface cleanly
  instead of silently misclassifying

Matches the same-day `login()` 204 fix in v2.21.0 (`df69a79`) which
already covered the IP-whitelist bypass handshake. Together these
two patches restore parity with qBit 5.2's new `/api/v2/*` response
contract.

6 new tests in `tests/clients/test_qbittorrent.py` cover the
success / rejection / mixed-batch / unknown-shape / malformed-JSON
cases.

### Operator note

Grabs stuck in `failed_unknown` state from v2.21.0 with a
`failure_detail` starting with `unexpected qBit response: HTTP 200
body='{"added_torrent_ids":...` can be reconciled with:

```sql
UPDATE grabs
SET state='submitted',
    submitted_at=datetime('now'),
    failed_reason=NULL
WHERE state='failed_unknown'
  AND failure_reason LIKE 'unexpected qBit response: HTTP 200 body=''{%';
```

The completion watcher will then pick them up on its next sweep
(qBit already has the torrent + the hash matches Seshat's
`qbit_hash` column).

---

## [2.21.0] — 2026-05-22

Amazon metadata cache — biggest discovery-flow architectural change
since v2.13.0's Goodreads bypass. The live `AmazonSource` no longer
runs in the synchronous lookup path. A paced background worker scans
authors on its own clock, writes results to a separate cache DB
(`metadata_cache_amazon.db`), and the synchronous flow reads from the
cache instead. User-facing scans never block on Akamai, cooldowns no
longer cascade across the discovery flow, and a 600-author library
refreshes Amazon coverage in 2–4 days.

### Added — `metadata_cache_amazon.db` and the read/write split

A new SQLite file alongside `seshat.db` holds the cache:
`metadata_cache_amazon_state` (per author × library), `_books`
(actual book rows), `_queue` (scan queue, schema-v2 PK = author_id
only), `_worker_state` (singleton: heartbeat, cooldown, today's
counters). PRAGMA `user_version`-versioned migrations live in
`app/discovery/metadata_cache.py`.

The synchronous flow (`app/discovery/lookup.py`) now uses
`CachedSource(source_name="amazon")` in place of the live source.
Cache hits return data immediately; misses enqueue the author at
priority 1000 and return `None` for this scan. Read-time filters
(language, format, owned-only) apply on cache reads — changing
preferences doesn't require re-scanning Amazon.

### Added — `metadata_cache_worker.py` background worker

Runs under `state.supervised_task` (crash → auto-restart). One tick
per iteration: heartbeat → gates → queue pop → fresh chrome120
session → behavioral warmup → `AmazonSource` scan with
`format_filter="allFormats"` → per-library partition by content_type
binding sets → cache write → 30–90s jittered sleep. Six tactical
behaviors from the 2026-05-22 Akamai investigation (per-author
session rotation, behavioral warmup, adaptive cooldown escalation
600/1800/3600s, think-time jitter, priority queue with no exclusion,
HTTP 202 detection). Profile rotation deliberately dropped — Arm 1
experiment showed it's worse than Arm 3.

### Added — schema-v2 cross-library dedup

Pre-Phase-D the queue used `(author_id, library_slug)` as PK. For a
typical library where every author lives in both calibre + abs, that
doubled the Akamai budget. Schema-v2 collapses to `author_id` only;
each scan runs `allFormats` and downstream code partitions the
response into per-library state + book rows based on each library's
content_type bindings (kindle/paperback/hardcover/mass_market →
ebook; audio_download/audioCD/mp3_cd/preloaded → audiobook). Cuts
the request budget ~50%.

### Added — 3-tier UI surfaces

- **Navbar status icon** — color-coded green/yellow/red/gray with
  hover tooltip; click → Settings → Sources → Amazon.
- **Amazon Cache Status card** in the Metadata Sources panel —
  worker enable toggle, queue depth, today's scans, last block,
  Reset Cooldown button.
- **Per-author cache badge** on author detail pages — "scanned 3d
  ago, 12 books cached" / "in queue (position N)" / "cooldown,
  retry in 8m" / "never scanned".
- **Dashboard Amazon Cache rail** at the bottom of the Seshat Stats
  widget — recent-discoveries list ("Found 'Honor of Duty 2' for
  A. R. Rend, 2h ago").

### Added — structured logging + 3-tier ntfy (Phase G)

Per-source child logger
`seshat.discovery.metadata_cache_worker.<source>` emits grep-friendly
`[scan] author=… outcome=… books=… new=… libraries=… elapsed_ms=…`
on every tick. Four new ntfy event keys (source-agnostic, so a
v2.22.0 Goodreads cache reuses them):

- `notify_on_metadata_cache_error` (default ON, prio 5) — worker
  stall, cache-write failure, tick crash
- `notify_on_metadata_cache_warning` (default ON, prio 4) — top-tier
  (3600s) cooldown escalation, author flipped to `failed_permanent`
- `notify_on_metadata_cache_daily_summary` (default OFF) — once-per-day
  digest of today's scans + blocks
- `notify_on_metadata_cache_new_book` (default OFF) — per-author
  celebration when the worker discovers a new ASIN (first-fill scans
  silenced to avoid backfill spam)

Two APScheduler jobs: `metadata_cache_amazon_stall_watch` (every
2 min, fires error ntfy if heartbeat older than
`metadata_cache_stall_threshold_s`, self-debounced via runtime-state
key) and `metadata_cache_amazon_daily_summary` (daily at
`metadata_cache_daily_summary_hour`, zeros `today_*` counters + opt-in
ntfy). Optional rotated log file at
`/app/data/logs/metadata_cache_worker.log` via
`metadata_cache_log_file_enabled`.

### Added — daily counter rollover

`today_scan_count` and `today_block_count` reset at the local-TZ day
boundary inside `_record_scan_completed` /
`_record_block_in_worker_state` (using
`datetime.fromtimestamp(...).date()`), and again at the daily-summary
fire time. Pre-Phase-G the counters were monotonic since deploy.

### REST surface

New router `app/routers/metadata_cache.py` mounted at
`/api/v1/metadata-cache`:

- `GET /status` — full worker + cache state
- `PATCH /settings` — enable/disable, format, language
- `POST /reset-cooldown` — emergency override
- `GET /author/{author_id}` — per-author cache lookup with library
  synthesis when state rows are empty
- `GET /recent-discoveries` — feed for the Dashboard rail

### Known behavior

Worker is **OFF by default** on fresh deploy. Toggle on under
Settings → Sources → Amazon. First scan after enable populates the
cache for high-priority authors; over 2–4 days the worker drains
the full backlog.

### Fixed — qBittorrent 5.2 IP-whitelist 204 handshake

`login()` now treats HTTP 204 No Content as a successful
authentication, alongside the existing 200 "Ok." path. qBittorrent
5.2 returns 204 when the caller's IP is in `AuthSubnetWhitelist` —
credentials are ignored and the subnet match is authoritative.
Whitelisted setups (the hotio image's default in many configs) hit
a permanent "qBit login failed" loop on v2.20.x and earlier even
though qBit was granting access on every request.

---

## [2.20.3] — 2026-05-22

Three Phase-A hotfixes bundled ahead of the v2.21.0 Amazon cache
extraction.

### Added — HTTP 202 soft-block detection

Akamai's sensor-challenge response (HTTP 202 with a thin
interstitial body) was falling into the silent
"status != 200, log + return" branch alongside genuine 404s, so the
cooldown never tripped and author scans walked at full speed
straight into the IP-level penalty box. Now classified as a
soft-block at every amazon.com call site (tier-1 `/dp`, tier-2
vanity `/author`, tier-2 search `/s`, `/stores/.../allbooks`).

### Added — cooldown persistence across container restart

`_blocked_until` now mirrors into `settings.json` as three
runtime-state keys (`amazon_blocked_until`, `amazon_block_reason`,
`amazon_blocked_since`), protected from user PATCH alongside the
existing `goodreads_session_*` keys. Restored on module import via
`_load_persisted_block_state()`. Closes the `docker restart`
mid-penalty bypass — the prior assumption that the in-process
`_blocked_until` global survived a restart was wrong.

### Added — Goodreads per-author budget scaling (Path A)

Sanderson-class authors (≥200 books on the GR list page) silently
dropped ~37% of books to time-budget exhaustion across the per-source
300s + retry cycle. The retry loop now scales:

- `book_count > 100` → 600s per retry (was 300s), 30 min total budget
- `book_count > 200` → 900s per retry, 40 min total budget
- Default — 300s / 25 min

Helpers no-op for non-Goodreads specs and never shrink an
operator-raised value.

---

## [2.20.2] — 2026-05-21

### Fixed — Amazon CAPTCHA-shim cooldown detection

`_fetch_allbooks` now trips the soft-block cooldown when the
response is 200 OK + body ≥50KB + no `ProductGrid` widget marker
(Amazon's full-chrome CAPTCHA variant that bypassed the size-based
thin-body gate). New `SoftBlockSuspectedError(ParseError)` subclass
distinguishes "marker absent" (suspected soft-block → cooldown)
from "marker present but content malformed" (real parser break →
one-off). Discovered when an Amazon scan ~15 min after an initial
429 hit returned a 200 CAPTCHA page large enough to pass the 50KB
threshold; parser logged "may be a Captcha shim" but cooldown
didn't engage, leaving the door open for cascade.

---

## [2.20.1] — 2026-05-21

### Fixed — `mirror_source_id` self-deadlock against the caller

`mirror_source_id` now skips the caller's own slug when propagating
a resolved source ID. The previous behavior deadlocked against the
lookup path's still-open write transaction — every author scan
logged a cascade of DEBUG-level "database is locked" errors (one
per resolved source ID). Caller is expected to have pre-written
its own row; the mirror only propagates to OTHER libraries. The
PATCH endpoint updated to pre-write the entry row explicitly.

---

## [2.20.0] — 2026-05-21

Cross-library author identity. Source IDs (`amazon_id`,
`goodreads_id`, `hardcover_id`) discovered in one library now
propagate to every other library that holds the same author, with
a triage UI for ambiguous matches. Eliminates the
"discovered an `amazon_id` in calibre but it never reaches abs"
gap that left half the library unscannable in v2.19 even after a
clean GR audit.

### Added — `mirror_source_id` propagation

When the resolver chain lands a new source ID for an author, the
write now fans out to every other library that has an author row
matching the canonical name. Mirror writes skip libraries that
already have a different value (manual triage required); mirror
writes never overwrite existing populated source IDs without
explicit user intent.

### Added — cross-library author triage UI

Settings → Discovery → Author Triage surfaces every author whose
source IDs disagree across libraries. Pick a winner per row;
mirror it across.

### Added — source-ID badges on author detail pages

Per-author header now shows pill badges for each resolved external
ID (`amz`, `gr`, `hc`, `cwa`) with a tooltip showing the value.
Click → open the canonical source URL.

---

## [2.19.0] — 2026-05-20

Amazon Author-Store discovery hardened against Akamai soft-blocks.
Cuts cascading 429 failures + reduces per-author request count for
typical scans from 3 → 1. Reference incident: 2026-05-20 16:17 → 16:19
where two consecutive author scans (Hanako Arashi → Mark Arrows)
both returned HTTP 429 with identical 2296-byte CAPTCHA bodies
because nothing told the second scan the IP was jailed.

### Added — IP-level soft-block penalty box

When amazon.com returns HTTP 429, a thin-body CAPTCHA interstitial,
or a `JuvecError` carrying the soft-block signal, AmazonSource and
the author-ID resolver now stamp a shared module-level
`_blocked_until` timestamp and short-circuit every subsequent
amazon.com request until the cooldown expires. Default cooldown
600s (10 min); honors `Retry-After` when present, clamped to
[60s, 3600s]. The penalty box never shortens an in-flight cooldown
— only extends.

`search_author`, `get_author_books`, `resolve_amazon_author_id`,
and all three resolver tiers (book-pivot, vanity URL, /s search)
check `is_amazon_blocked()` at entry and return None immediately
with an INFO log. The Arashi → Arrows cascade pattern is now
impossible: the second scan would log
`SKIPPED — soft-block cooldown (Xs remaining)` and never touch
the wire.

### Added — SSR-bypass for non-prolific authors

`AmazonSource._collect_products` now skips the `/juvec`
filter-application POST entirely when the SSR `/stores/author/{id}/allbooks`
page already carries the full catalog
(`page_data.total_result_count <= len(page_data.products)`). The
Python-side format filter (Stage 3) handles binding-symbol matching
for SSR products, identical to what server-side filtering would
have produced. Cuts a typical author scan from 3 requests (allbooks
GET + filter POST + detail batch POST) to 1 — the largest
single reduction in amazon.com request density we can ship without
changing pipeline shape.

Pagination + /juvec still fire for prolific authors whose total
exceeds the page-1 product slice (~85 entries). The legacy
`default_filter` shortcut for the `allFormats / All Languages`
configuration is preserved as a redundant safety branch.

### Added — DuckDuckGo Tier-2c fallback for author-ID resolution

Opt-in via `metadata_sources.amazon.use_ddg_fallback` (default
False). When the amazon.com tiers (Tier 1 book pivot, Tier 2a
vanity URL, Tier 2b /s search) return clean misses, fall back to
a DuckDuckGo site-restricted search
(`site:amazon.com "{author}" /stores/author`) and parse the result
page for `/stores/.../author/{B0XXXXXXXX}` URLs. Handles both
direct hrefs and DDG's tracking-redirect shape (`/l/?uddg=...`).

Pattern borrowed verbatim from Calibre's `search_engines.py` — DDG
HTML endpoint, `kp=-2` safe-search off, transient `httpx` session.
Useful primarily when amazon.com's `/s` is degraded or genuinely
has no anchor matches for an author whose vanity slug doesn't
normalize cleanly.

### Changed — `JuvecError` carries structured failure metadata

`JuvecError` now exposes `status_code`, `retry_after_s`, and
`thin_body_bytes` so AmazonSource can distinguish "legitimate
scan miss" from "Akamai jailed our IP" without string-matching
the message. The new `is_soft_block_signal` property drives the
penalty-box recording in the `get_author_books` exception handler.

### Behavior change to note

`_tier1_book_pivot` returning a thin-body response now trips the
penalty box and short-circuits Tier 2 in the same call. The
previous behavior was to fall through to Tier 2 silently. The new
behavior matches the v2.19.0 design intent — once Akamai flags
the IP, every amazon.com endpoint will fail, so continuing wastes
request slots. The existing `test_tier1_thin_body_treated_as_failure`
test was retitled `test_tier1_thin_body_trips_penalty_box` and its
assertions inverted to reflect this.

---

## [2.17.2] — 2026-05-18

UAT-caught: Authors list now sorts by last name (matching the
alphabet sidebar's grouping).

### Fixed — name sort keys on last word, not Calibre `sort_name`

The cross-library Authors list's default name sort used
`sort_name` from each library's authors row. Calibre populates
this inconsistently — most rows store "Last, First" (Ann Aguirre
→ "Aguirre, Ann") but pseudonyms / some imports stay raw
"First Last" (Emrys Ambrosius → "Emrys Ambrosius", Aaron
Renfroe → "Aaron Renfroe"). Result: SQL ORDER BY sort_name placed
Emrys under the "E"s, page 5+ alphabetically, even though the
alphabet sidebar's `getLetterKey` correctly grouped them under
"A" via last-word extraction. UAT 2026-05-18 caught this: Emrys
was missing from page 1 of the All / Audiobooks tabs.

Fix: cross-library sort now extracts the last word from the
display name (matches the frontend's `getLastName` exactly).
Both layers agree on placement now.

One new test (`test_authors_list_sorts_by_last_name`) feeds a
mixed-convention set and asserts the order matches last-name
collation.

---

## [2.17.1] — 2026-05-18

UAT-caught hotfix to v2.17.0 Bug B / Bug A: Authors-page tile
counts are now GLOBAL on every format tab, not just All.

### Fixed — author tile counts now global on Audiobooks + Ebooks tabs too

v2.17.0's merge ran the cross-library aggregation only when the user
was on the "All" tab. The Audiobooks / Ebooks tabs queried just the
matching-type libraries, so a cross-format author's tile showed only
that format's counts. UAT 2026-05-18: Emrys Ambrosius (5 ebooks in
Calibre + 1 audiobook in ABS) showed "1 owned / 0 missing" on the
Audiobooks tab — the per-library count — instead of the global
"1 owned / 5 missing".

Fix: the `/authors` endpoint now ALWAYS calls `run_across_libraries`
with content_type="all" so the merged counts always sum every
library. The user-requested content_type is applied AFTER merge as
a list filter — an audiobook-only author is excluded from the
Ebooks tab, but a cross-format author shows their global counts on
every tab where they qualify.

Two new tests
(`test_authors_list_counts_are_global_on_every_tab` +
`test_authors_list_audiobook_only_excluded_from_ebook_tab`) pin
the new shape.

---

## [2.17.0] — 2026-05-18

Cross-library polish minor. Four user-reported items landed in one
release — two UX fixes that make audiobook authors actually visible
+ correctly counted on cross-library setups, plus bulk Hide/Delete
across every book and author listing.

### Fixed — Bug A: Authors "All" filter now shows format badges

Audiobook-only authors WERE coming back from the API on the "All"
tab; the bug was UX: `AuthorCard` / `AuthorRow` rendered nothing
to distinguish ebook-only / audiobook-only / mixed authors, so on
a Calibre-dominant library the audiobook side felt invisible.
Tiles now show a 📖 / 🎧 / 📖🎧 badge next to the author name when
the cross-library merge identifies content_types — explicit
visual differentiation without changing the data path.

The backend's merge step (`/authors?content_type=all`) now stamps
a parallel `content_types: [...]` list alongside the existing
`library_slugs` so the frontend can render the badge without a
second round-trip.

### Fixed — Bug B: Author-detail counts are now GLOBAL across libraries

`/api/discovery/authors/{aid}?include_cross_library=1` now returns
a `global_stats` field that sums `owned` / `missing` / `total` /
`series_count` across the primary library + every cross_library
entry. The top-of-page tile on both desktop (`DiscAuthorDetailPage`)
and mobile (`MobileAuthorDetailPage`) read this field. Before:
an audiobook-only author with 1 owned audiobook + 4 freshly-
discovered unowned ebooks rendered "1 owned, 0 missing" on the
ABS-side detail page (per-library count). After: "1 owned, 4
missing" — true global view that matches what the cross-format
scan actually produced.

The per-library counts inside each tab are unchanged — they're
still useful when the user is looking at one specific library's
slice. The bug was at the top-of-page summary.

Side effect cleanup: the mobile detail page previously read
`a.owned_count` / `a.total_books` / `a.missing_count` directly,
but those columns aren't populated by the detail endpoint (the
list endpoint computes them via JOIN aggregation). Mobile was
permanently rendering "0 / 0 owned" — also closed by this fix.

### Added — Feat C: Bulk Hide / Delete on every book + author page

Every book-listing page gains Hide and Delete buttons in the
multi-select bar:

- **Library, Missing, Upcoming** (`DiscBooksPage`): Hide moves
  selected books to the Hidden page; Delete removes unowned rows
  and silently skips Calibre / Audiobookshelf-synced ones.
- **Hidden** (`DiscHiddenPage`): full new multi-select bar with
  **Unhide** + **Delete** (the Hidden page is the one place
  where the bulk verb is Unhide, not Hide).
- **Authors** (`DiscAuthorsPage`): Hide and Delete cascade across
  every selected author's books. Hide sets `hidden=1` on all
  their book rows (across every configured library); Delete
  removes unowned rows only. Author rows themselves stay intact
  so the v2.12.1 dual-row mirror pattern keeps working.

New API surface:

- **`POST /api/discovery/books/bulk-unhide`** (new endpoint
  symmetric with the existing `bulk-hide`).
- **`POST /api/discovery/authors/bulk-hide-books`** — accepts
  `{author_names: [...]}` and cascades across every library.
- **`POST /api/discovery/authors/bulk-delete-books`** — symmetric;
  returns per-library `books_deleted` / `books_skipped` counts.

Two new tests pin the cascade behavior
(`test_bulk_hide_authors_books_cascades_across_libraries`,
`test_bulk_delete_authors_books_skips_library_synced`).

Mobile pages keep their existing "no bulk select bar on mobile"
design decision per the file-level comment in
`MobileBooksPage.tsx`. If bulk on mobile becomes a felt need,
it's a separate follow-up.

### Added — Feat D: Authors page "Sort: Owned" + sort-invert button

The Authors page sort dropdown gains an **Owned** option that
orders authors by their owned-book count. The new sort-invert
button (↑ / ↓ chevron next to the dropdown) flips the sort
direction; this is parity with the existing pattern on
`DiscBooksPage`. Backend's `_build_authors_sql` and cross-library
merge sort lambdas both honor `sort_dir` literally now (previously
count sorts forced DESC via a negative-key trick).

Frontend bug-fix bonus: the existing sort dropdown sent
`sort=books` which the backend treated as an unknown value and
silently fell through to ORDER BY sort_name. Renamed to
`sort=total` to match the backend's mapping; sorting by book count
actually works now.

---

## [2.16.3] — 2026-05-18

UAT follow-up: bound the Hygiene Job 3 Phase-2 sweep.

### Fixed — Hygiene Job 3 now caps the Phase-2 sweep at 200 candidates

v2.16.0's Hygiene chain called `backfill_missing_author_ids(limit=None)`,
leaving the Phase-2 cross-DB co-author sweep unbounded. UAT
2026-05-17 against Mark's library found **645 ABS Phase-2
candidates** × 7s each (Goodreads rate-limit) → projected ~70-minute
wall-time on first invocation, which is too long for an interactive
trigger.

Fix: one-line change to pass `limit=200`. The existing
`backfill_missing_author_ids` already honors `limit` across both
phases combined; Phase-1 (small, anchor-book-driven) runs first
and Phase-2 inherits the remaining budget. Idempotent re-runs
naturally drain the rest of the backlog over multiple chain
invocations. With ~30s per HTTP-bound author and a mix of fast
resolver-chain-dry skips, this caps the chain at ~10-15 min per
library wall-time even on first-run.

One new test (`TestJob3LimitCap.test_passes_non_none_limit`)
pins the kwarg so future refactors don't silently re-introduce
the unbounded path.

---

## [2.16.2] — 2026-05-18

UAT-caught hotfix to v2.16.0's Hardcover `book_mappings` extraction
GraphQL filter.

### Fixed — platform-name filter now lowercase (matches Hardcover's live API)

The BookData fragment in `app/discovery/sources/hardcover.py` and
the batched query in `app/discovery/hygiene.py` Job 2 used
TitleCase platform names (`["Goodreads", "OpenLibrary", "Google"]`)
in their `book_mappings.platform.name._in` filter. Hardcover's
live API returns these platform names **lowercase**
(`goodreads`, `openlibrary`, `google` — confirmed via direct probe
against book id 1945439 / SAO Novel 01). Hasura's `_in` operator
is case-sensitive, so the filter matched zero rows in production
even when the data was there.

UAT 2026-05-17 against Mark's library: Job 2 swept **5300
candidate books** with `hardcover_id IS NOT NULL` and stamped
**0 cross-source IDs** — silently. The extraction loop in
`hardcover.py` already case-folds platform names before comparison,
so the filter was the only place case bit. Fix is a one-line
swap to lowercase in both query strings, plus two regression
tests (`TestBookMappingsPlatformCase` in
`test_hardcover_search.py`, `TestHygieneHardcoverQueryShape` in
`test_hygiene.py`) that pin the lowercase form in both module
source strings.

### Known issue (not blocking v2.16.2) — Job 3 ABS Phase-2 wall-time

UAT also surfaced that `backfill_missing_author_ids`'s Phase-2
ABS sweep can touch 600+ author candidates at ~7s each (Goodreads
rate-limit), yielding ~70-minute wall-time on a real library.
Phase-1 still completes in a few minutes. v2.16.2 ships the
case fix; a follow-up (v2.17.x candidate) will add a per-library
`limit=` cap on Phase-2 so the Hygiene chain stays bounded.

---

## [2.16.1] — 2026-05-17

UAT-caught hotfix to v2.16.0's Data Hygiene Job 1.

### Fixed — empty-author cleanup now preserves cross-library mirror rows

v2.16.0's `job_empty_cleanup` deleted any author with 0 books in
**this** library that wasn't on `authors_allowed`. That rule
silently destroys the v2.12.1 dual-row mirror pattern: authors
who have ebooks in Calibre get an empty mirror row in ABS (and
vice versa) so cross-format scans surface audiobooks alongside
ebooks. UAT 2026-05-17 against prod caught **93 ABS-side mirror
rows + 20 Calibre-side rows** that the first cut would have
deleted (V. E. Schwab, J. J. Bookerson, etc.) before any chain
ran.

The fix: the Hygiene coordinator now pre-computes the union of
normalized author names that have ≥1 book in any library
(`_load_cross_library_book_names`) and passes it to every
per-library `job_empty_cleanup` call. An author is preserved if
their normalized name is in EITHER the global allowlist OR the
cross-library set. True orphans (no books anywhere, not
allowlisted) still get deleted.

The empty-cleanup log line now also reports `kept_by_cross_library=N`
so re-runs are observable. Three new tests pin the rule
(`test_preserves_cross_library_mirror_authors`,
`test_cross_library_does_not_protect_true_orphans`,
`test_load_cross_library_book_names_unions_across_libs`).

### Internal — UAT plan revision

`files/seshat-v2.16.0-uat-plan.md` carries through; the §4.2 Job 1
log-line check should now include `kept_by_cross_library` and the
spot-check should confirm `V. E. Schwab` (or any known cross-library
author) still has rows in BOTH per-library DBs after the run.

---

## [2.16.0] — 2026-05-17

Two prerequisite gap fixes uncovered by the 2026-05-16 stress test,
plus the **Data Hygiene** Command Center action — a single button
that fans 6 chained backfill / cleanup jobs across every configured
library.

### Added — `Data Hygiene` Command Center action

A new Quick Actions button on the Command Center (both desktop and
mobile dashboards) launches a confirmation modal listing all 6 jobs,
then fires them as a background chain. Progress surfaces in the
existing scan-status banner alongside Source / MAM / library syncs.

The 6 jobs, in order:

1. **Empty author + series cleanup** — deletes authors with 0 books
   (preserving the global `authors_allowed` allowlist by name) and
   series with 0 members.
2. **Hardcover identifier backfill** — for books with `hardcover_id`
   but missing `goodreads_id` / `openlibrary_id` / `google_books_id`,
   batch-queries Hardcover's `book_mappings` table and COALESCE-fills
   the missing columns. Unblocks Job 3 for audiobook-only authors.
3. **Phase-2 author goodreads_id backfill** — runs the existing
   sweep that reverse-looks-up author Goodreads IDs from any owned
   book carrying a resolvable identifier. Skipped if Cloudflare
   soft-block is active.
4. **Book deduplication** — merges book rows sharing any non-null
   identifier (Goodreads, Hardcover, ISBN, ASIN, Amazon, Audible).
   Then re-runs the existing `_dedupe_same_series_position` pass to
   catch "Remnant II" vs "Remnant Book 2" duplicates. Hidden rows
   are excluded from the comparison set.
5. **Series consolidation** — collapses series rows under the same
   author whose names canonicalize to the same form
   (`"Mistborn"` vs `"The Mistborn Saga"`), via the existing
   `_dedupe_intra_author_series` helper.
6. **ABS author cross-stamp** — copies `goodreads_id` /
   `hardcover_id` / `openlibrary_id` / `google_books_id` from
   enriched ebook authors to ABS authors with the same normalized
   name. Cheap-and-safe scope: only name-equality. Real ABS author
   enrichment (cross-library Goodreads resolution for ABS-only
   authors) is deferred to v2.17.x.

Universal rules:

- **Skip hidden items** where applicable (cleanup + dedup
  iteration). Identifier-class writes (stamping a discovered ID)
  ignore hidden state — same rule the live scan layer follows.
- **Idempotent**. Re-running back-to-back is a near-no-op.
- **Preserve `authors_allowed` by name**. The empty-cleanup job
  refuses to delete any author whose normalized name appears in the
  global allow-list — that table is the user's allowlist of record.

API:

- **`POST /api/discovery/hygiene/run`** — start the chain. Refuses
  overlap. Returns `{"status": "started"}` or `{"status": "running",
  "message": ...}`.
- **`POST /api/discovery/hygiene/cancel`** — cancel an in-flight
  chain.
- **`GET /api/discovery/hygiene/status`** — direct snapshot for
  operators tailing curl. The unified `/scan-status` endpoint also
  surfaces hygiene progress so the existing banner picks it up.

### Added — Gap 1: Hardcover `book_mappings` extraction

The Hardcover BookData GraphQL fragment now includes the
`book_mappings` table for Goodreads / OpenLibrary / Google
platforms. Three new optional fields on `BookResult`
(`goodreads_id`, `openlibrary_id`, `google_books_id`) COALESCE-fill
through every books-table write path in `lookup.py`.

**Why it matters**: the 2026-05-16 44-author stress test surfaced
1,526 Hardcover-stamped books with zero `goodreads_id` because our
query didn't ask for the mapping. Hardcover has it. Post-fix,
Phase-2 author-goodreads_id backfill (and Data Hygiene Job 3) can
reverse-resolve audiobook-only authors that previously had no
anchor book carrying a goodreads_id.

OpenLibrary path-form values (`/books/OL...M`) are stripped to the
bare OL key for column consistency with what `openlibrary.py`
itself writes; BookSidebar suffix-routes M-keys to `/books/` and
W-keys to `/works/` either way.

### Changed — Gap 2: Hardcover URL uses stable id-permalink

Hardcover regenerates slugs when titles change on their side, so
stored `source_url` and `hardcover_slug` go stale and 404 (UAT
2026-05-16). Live probe confirmed the singular
`https://hardcover.app/book/<hardcover_id>` form is a stable
permalink that 302s to the current canonical slug. BookSidebar now
prefers it over any stored Hardcover URL when `hardcover_id` is
known.

---

## [2.15.1] — 2026-05-16

Follow-up to v2.15.0 #B: two pieces of polish surfaced during UAT.

### Added — search result deep-linking

- **`GET /api/v1/discovery/books/{bid}?slug=…`** (new endpoint).
  Returns a single book row by id; the `slug` query param routes
  to the right library since book ids are per-library, not globally
  unique. Used by the global-search book click-through so the
  navbar can populate BookSidebar without a full listing fetch.
- **`DiscBooksPage` listens for `seshat:focus` (kind=book)**. When
  the global search routes to a book, the listener fetches the
  full Book row from the new endpoint and opens BookSidebar with
  it. v2.15.0 click-through landed on the library page without
  auto-opening — this closes that gap.
- **`DiscSeriesPage` listens for `seshat:focus` (kind=series)**.
  Pre-fills the page's existing search input with the series name
  so the list narrows to just that series. The 250ms search-debounce
  flushes the fetch automatically.
- **Series search results carry their name** through
  `SearchNavTarget` → `seshat:focus` detail, so the listener can
  apply it without a refetch.

### Changed — Settings search now cross-section

Reversed v2.15.0's two-tier UX decision. Settings page search now
renders every section during search instead of filtering only the
current one. Match criteria expanded:

- **Field label** (always was)
- **Field description** (always was)
- **Field example placeholder** (always was)
- **Section name** — typing "notif" → every SF in Notifications
  renders (because the section's label matches)
- **Section keywords** — each `SECTIONS` entry gained optional
  `keywords: string[]` (e.g. Notifications → `["ntfy","notify","alerts"]`,
  Download Client → `["qbittorrent","qbit","transmission"]`, etc.).
  Typing "ntfy" → Notifications matches → all its SFs render
  even though no individual SF has "ntfy" in its label.
- **Optional per-field `searchAlso` prop on SF** — call sites that
  want the search to also match the underlying settings key or
  current value can pass them as extra match strings. Not retro-
  applied; reserved for fields where the key/value is the natural
  search term.

UX additions:

- Each rendered SF in search mode shows a small **section chip**
  (e.g. "NOTIFICATIONS") above its label so the user sees where the
  match came from.
- Header title becomes "Settings · filtering '…'" in search mode
  (instead of the current section's label, which would be misleading
  with cross-section results visible).
- Sidebar section click clears the active search so navigation
  feels coherent.
- Helper banner updated to reflect cross-section behavior.

**Side-effect note**: each custom section component
(MetadataSourcesPanel, LibrarySection, AudiobookshelfSection,
DiscMamSection, DiscoveryDataSection, DataSection) fires its
mount-time data-fetch effects when first rendered during search.
With `[]` dep arrays they fire once per search-session
activation — acceptable cost for the wider search reach.

### Not yet matched

- **Settings field key / value** matching. Mark's UAT scope included
  these but SF doesn't have direct access — keys live in the
  children (`<STog on={s.mam_irc_enabled}>`). The opt-in `searchAlso`
  prop lets call sites add them per field, but no fields have been
  retro-fitted; this is a future polish pass once we know which
  keys users actually search by.

---

## [2.15.0] — 2026-05-16

Global + Settings search bars (#B). Two-tier search UX:

  - **Global navbar search**: persistent input in the top nav (mobile
    gets a search icon → fullscreen overlay). Searches across pages,
    Settings sections, authors, series, and books across every
    discovered library. ⌘K / Ctrl+K focuses it from anywhere.
  - **In-page Settings search**: input in the Settings header that
    filters fields within the active section. Two-tier together: the
    navbar finds the right section, the in-page search finds the
    right field within it.

### Added — Backend

- **`GET /api/v1/search?q=…&limit=8`** (new router
  `app/routers/search.py`). Searches books / authors / series across
  every discovered library via `run_across_libraries("all", …)`.
  Each category returns up to `limit` hits, ranked by owned-first
  (books only) then by prefix-match-first then alphabetical. Each
  hit carries `library_slug` + `author_name` + `series_name` so the
  dropdown can render where-it-lives + click-through-target without
  a second round-trip. Hidden books are filtered out. `q` is
  bounded to 100 chars at the validation layer.
- 14 tests in `tests/routers/test_search.py` covering cross-library
  aggregation, hidden-book exclusion, owned-rank-above-unowned,
  library_slug stamping, prefix-vs-substring ranking, limit cap,
  and empty-query rejection.

### Added — Frontend: GlobalSearchBar component

- **`frontend/src/components/GlobalSearchBar.tsx`**. Persistent
  input with magnifying-glass icon, 300ms debounce on the fetch.
  Empty / <2-char queries clear the dropdown immediately.
- **Categorized dropdown**: Pages → Settings → Authors → Series →
  Books. Pages + Settings come from in-file client-side indexes
  (`PAGE_INDEX` + `SETTINGS_SECTIONS_INDEX`); Authors / Series /
  Books come from `/api/v1/search`.
- **Keyboard nav**: ArrowUp / ArrowDown moves selection, Enter
  activates, Escape closes. Mouse hover also moves selection.
- **⌘K / Ctrl+K shortcut** focuses + selects the input from
  anywhere in the app.
- Click-outside-to-close on the wrapper.
- Structured `SearchNavTarget` discriminated union — caller decides
  routing.

### Added — App-level mounting

- **Desktop**: GlobalSearchBar sits between the section nav items
  and the right-rail tool icons in the navbar. Added `min-width: 0`
  to the section-nav-items flex child so its label list doesn't
  squeeze the search bar's width away.
- **Mobile**: search icon button next to the hamburger opens a
  fullscreen overlay (z-index 220, above MobileNavDrawer's 201)
  with `autoFocus` + `fullWidth` so the user lands ready to type.
  Backdrop tap or ✕ button closes; selecting a result auto-closes.
- **`navFromSearch`** in `App.tsx` translates `SearchNavTarget` into
  `nav()` calls. Page + Author results use existing nav patterns.
  Settings / Series / Book results also dispatch a window
  `seshat:focus` CustomEvent so target pages can scroll-to / open
  the matching element.

### Added — Settings page in-page search

- **`SettingsSearchContext`** + `useSettingsSearch()` hook in
  `SettingsPage.tsx`. SF reads the search string from context and
  self-hides (`return null`) when search is active and its label /
  desc / example don't match.
- **Search input** in the Settings content panel header with a
  clear-search ✕ button. Active state surfaces a helpful banner
  reminding the user that the in-page search is single-section
  scoped (navbar handles cross-section).
- **`seshat:focus` listener** with `kind=settings-section`: when
  the navbar search routes to a Settings section, the Settings
  page receives the focus event and switches to that section.

### Design decision: two-tier (not render-all)

The original plan was to render every Settings section during in-
page search so cross-section matches could surface in-place. We
reverted to two-tier instead: each custom section component
(MetadataSourcesPanel, LibrarySection, AudiobookshelfSection,
DiscMamSection, DiscoveryDataSection, DataSection) has its own
data-fetch effects, and render-all would have fired every fetch
on every search. Two-tier keeps the cross-section discovery in the
navbar (cheap — section names are client-side) and the in-page
search scoped to the current section. ⌘K is the bridge.

### Not yet wired (deferred)

- **Book / Series deep-link**. The `seshat:focus` event fires for
  Book + Series results, but no target page listens yet. v2.15.0
  ships with click-through landing on the library / series page
  without auto-opening BookSidebar or scrolling to a specific
  series row. Polish follow-up when there's demand.

---

## [2.14.2] — 2026-05-16

Hotfix for v2.14.1 Database Manager rework. UAT 2026-05-16 surfaced
two issues on the `books` table (15,954 rows, many wide columns):

  - **Page numbers unreachable**: the user had to either hide nearly
    every column or horizontally scroll the entire page to reach
    the pager at the right end of the toolbar.
  - **Search felt frozen**: typing a query made the table feel
    unresponsive to scrolling and page navigation.

Both traced to the same family of bugs:

### Fixed — grid layout (the actual layout bug)

- **`min-width: 0` on the right-pane grid item**. CSS Grid with
  `gridTemplateColumns: "220px 1fr"` has a long-standing trap: the
  `1fr` column doesn't actually constrain to `1fr` of remaining
  space — it expands to fit the min-content of its children. With
  a wide table inside (the books table easily 2000px+ of cumulative
  column widths), the grid cell grew beyond viewport, pushing the
  right-aligned top pager + bottom pagination row off-screen.
  Adding `min-width: 0` forces the cell to respect `1fr` and lets
  the table's own `overflow: auto` handle horizontal scrolling
  internally. Both pagers are now visible without horizontal page
  scroll.

### Fixed — search debounce

- **300ms debounce** on the search input. Pre-fix, every keystroke
  fired its own `/v1/db/table/{name}` fetch — typing "wolf" fired
  4 sequential requests, each one briefly disabling the pager
  buttons (`disabled={loading || …}`) and re-rendering the table.
  The combined effect felt like the page was frozen against
  scroll + paging. Debounced search waits 300ms after the last
  keystroke before firing, so a settled query is one request,
  not N. Empty-string clears flush immediately so clearing the
  search shows all-rows instantly.

Both fixes are frontend-only (DatabasePage.tsx). No backend or
schema changes; v2.14.1's sort + numeric-search behavior is
unchanged.

---

## [2.14.1] — 2026-05-16

Database Manager rework (#F) + floating "Jump to Top" button (#G).
Both backlog items from the v2.14.x candidate list. Bundled into a
single patch since the Jump to Top button is small enough that
isolating it as its own minor would have been overkill.

### Added — #F Database Manager rework

Backend (`app/routers/db_editor.py`):

- **`sort` + `sort_dir` query params** on
  `GET /api/v1/db/table/{name}`. Sort column is validated against
  the table's schema (`PRAGMA table_info`), so user-supplied
  identifiers never reach the interpolated `ORDER BY` — anything
  outside the column list silently falls back to natural order.
  `sort_dir` accepts `asc` or `desc`; anything else defaults to
  asc.
- **Numeric-aware `search`**. When the search input parses cleanly
  as an int or float AND the table has any INT/REAL/NUMERIC/FLOAT/
  DOUBLE columns, the WHERE clause unions equality matches on those
  columns with the existing TEXT LIKE clauses. Typing `42` now
  finds rows whose `id = 42` or `page_count = 42`, not just rows
  whose text columns happen to contain "42".

Frontend (`frontend/src/pages/DatabasePage.tsx`):

- **Per-column max-widths** via a `colMaxWidth(col, type)` helper.
  Numeric columns get 90px (fixed-width feel); wide-content text
  columns (description, source_url, cover_url, cover_phash, bio,
  file_path, image_url, mam_url, formats, tags, raw) get 280px;
  everything else gets 180px. Hover-title still surfaces the full
  value when ellipsis fires. Pre-v2.14.1 was a uniform 320px cap
  which made wide columns dominate and pushed narrow numeric
  columns off-screen.
- **Clickable column headers**. Click cycles natural → asc → desc
  → natural. Active column shows ↑/↓ + accent color + tinted bg.
  Page resets to 1 on sort change.
- **Bottom pagination row** when `totalPages > 1`. Shares the same
  pager JSX as the top toolbar via a hoisted `pagerJsx` const —
  no code duplication.
- **Column visibility menu**. "Columns" button in the toolbar
  opens a dropdown checklist; PK is locked visible (the row-action
  column needs a stable anchor). Hidden columns persist per-table
  in localStorage at key `seshat:db-hidden-cols:{table}`, so the
  user's preferences come back on revisit. "Show all" link clears
  the set + drops the localStorage entry. Click-outside-to-close.
- **Search placeholder** updated from "Search text columns…" to
  "Search text + numbers…" to advertise the numeric-aware
  behavior.

### Added — #G Floating "Jump to Top" button

- New `frontend/src/components/JumpToTop.tsx` mounted in App's root
  layout (inside `SseEventsProvider`, after `InstallPrompt`).
  44x44 circular button, `position: fixed; bottom: 16; right: 16`,
  z-index 50. Click → `window.scrollTo({top: 0, behavior: "smooth"})`.
  Always visible (no scroll-position gate — Mark prefers
  consistency: "every single page regardless if you can scroll
  down or not").
- z-index 50 keeps it below every overlay (BookSidebar 100,
  MobileNavDrawer 201, Toaster 9999, InstallPrompt 60) so the PWA
  banner / sidebar / toasts always cover it cleanly.

### Tests

- New `tests/routers/test_db_editor.py` — 12 tests across four
  classes covering sort asc/desc on int + text columns, sort-
  injection rejection, sort_dir fallback, numeric-search inclusion,
  numeric-vs-no-match, numeric+text-substring overlap, case-
  insensitive text search, and combined search + sort composition.

### Mobile

Mobile DatabasePage is unchanged. It uses cards (not a table) so
it has no horizontal-overflow or sort-by-column issue. Numeric
search comes through for free since both pages share the same API.

---

## [2.14.0] — 2026-05-16

Author Detail UX polish — three concrete pain points carried over from
v2.12.1 UAT 2026-05-14, bundled as a single minor: missing source
badges (OpenLibrary + Audible), no jump-to-section nav on long Combined
tabs, and the Author Detail page failing to refresh when a scan
completes. Backlog items #A, #C, #D from the v2.14.x candidate list.

### Added — #A source-badge audit

- **OpenLibrary badge** in BookSidebar. `openlibrary_id` is populated
  by the OpenLibrary discovery source and by Calibre identifier
  mining (`openlibrary` / `ol` types), but the previous badge map
  didn't include it — so cross-source URL panels silently omitted
  the OL link even when the data was there. URL builder is suffix-
  aware: work-keys (`OL…W`) → `/works/{id}`, edition-keys (`OL…M`)
  → `/books/{id}`.
- **Audible badge** in BookSidebar. Primary column is `audible_id`
  (populated by Audnexus / Calibre `audible:` type). For audiobook
  rows with no source-scanned `audible_id` (the common ABS-only
  case), falls back to `asin` — for an audiobook row, ASIN *is* the
  Audible identifier. Guarded on `isAudiobookRow` so an ebook row's
  `mobi-asin` doesn't accidentally resolve to an unrelated product
  on audible.com.

### Added — #C Combined-tab jump-to-section nav

- **Jump-to nav** on Author Detail's Combined tab (desktop + mobile).
  UAT 2026-05-14 surfaced that with multi-hundred-item Combined views
  (Sanderson: ~400 entries), the boundary between Ebook and Audiobook
  blocks vanished into the scroll. The new nav renders a "Jump to:"
  row above the blocks with one button per content type; click
  smooth-scrolls to that block. Only renders when ≥2 blocks are
  visible. Each block gets an `id` anchor + `scrollMarginTop` so the
  scroll lands the section heading below the sticky author header
  instead of clipped underneath.
- **Stronger section delineation**: per-content-type headers in
  Combined mode now use a wider color bar and larger label so the
  boundary is visible without relying on the nav.

### Fixed — #D scan-completion refresh

- **Author Detail now refreshes when a scan finishes.** The page
  used to listen for a `seshat:scan-completed` window event that the
  frontend never dispatches anywhere — the orphan listener was a
  vestige of a refactor. After clicking Re-sync / Scan Ebooks /
  Scan Audiobooks / Scan MAM, the user had to manually reload the
  page to see merged books or to clear the scan-button spinner.
- Replaced with a 3-second page-local poll of `/discovery/scan-status`
  (same cadence and pattern as `DiscBooksPage`'s MAM-scan poller).
  On a running→idle transition for `lookup` or `mam`, the page
  clears the corresponding spinner state, calls `loadA()` to refresh
  author + book data, and bumps the re-mount key so child series
  components pick up the new state. Mobile equivalent ships the
  same poll.

### Fixed — book_merge latent bug

- **`_IDENTITY_FIELDS`** in `app/discovery/book_merge.py` now
  includes `openlibrary_id` and `audible_id`. These columns were
  populated by their respective discovery sources but absent from
  the merge layer's identity field list — book merges silently
  dropped them when the value lived only on the loser row. The
  newly-added badges would have made that data loss user-visible.

### Internal

- `Book` TypeScript interface gains `openlibrary_id` and `audible_id`
  fields. The `b.*` SELECT in `_BOOKS_SELECT` already exposed them
  on the wire; the type just wasn't claiming them.
- `pyproject.toml` `version` field jumps from 2.13.1 → 2.14.0; the
  v2.13.2 bump was missed at release time (no runtime impact —
  nothing reads pyproject's version at runtime; the field is a
  build artifact).

### Sources audited (no changes needed)

- Goodreads, Hardcover, Kobo, Amazon, IBDB, Google Books — all
  had complete badge coverage prior to this release.
- Fictiondb / FantasticFiction — DB columns exist but no live
  discovery source writes to them, so no badge needed. FantasticFiction
  was dropped from the schema in an earlier migration; Fictiondb
  remains an empty column reserved for a future source.

---

## [2.13.2] — 2026-05-16

Goodreads enrichment rewire. Day-1 production logs after v2.13.1's
priority promotion showed Goodreads at **0/5 hit-rate** on per-book
enrichment — the enricher only invoked `search_book(title, author)`
which fell through to the disabled `/search` stub. The existing
`goodreads_id_resolver` chain (auto_complete + Hardcover + OpenLibrary)
was built for v2.13.0's discovery-side author backfill but never wired
into per-book metadata enrichment. v2.13.2 closes that gap and adds
two new tiers that exploit the v2.13.0 author-id backfill.

### Added

- **Resolver tier 4** — `/book/auto_complete?format=json&q={title}`
  with author-id post-filter. Same endpoint T1 uses for ISBN/ASIN;
  empirically cleaner with a title-only query (title+author polluted
  results with parodies and boxed sets). Picks the highest-`ratingsCount`
  result when multiple match the anchor author. Gated on
  `author_goodreads_id` being set — unconstrained title matches are
  too risky.
- **Resolver tier 5** — `/author/list/{author_goodreads_id}`
  bibliography walker. Lazy page-by-page, schema.org/Book microdata
  parse, fuzzy title match via existing `title_similarity()`. Reuses
  the v2.13.0-reserved `id_cache.author_bib` 7-day cache scope; meta
  header tracks `pages_walked` + `fully_indexed` so subsequent lookups
  for different titles by the same author resume from the prior walk
  (and skip HTTP entirely once fully indexed).
  - **`app/metadata/goodreads_bibliography.py`** — new module.
  - Pagination cap: 50 pages (1500 entries; Sanderson is 14).
  - Title-match threshold: 0.85.
- **`app/metadata/author_lookup.py`** — cross-library lookup helper
  that walks every discovered library's `authors` table for a given
  name and returns the first non-empty `goodreads_id`. Required
  because per-book enrichment runs library-agnostic (acquisition
  linkback happens later in the pipeline).
- **MAM ISBN/ASIN extraction** — `get_torrent_info` now requests
  the search API's optional `isbn` field by setting `"isbn": True`
  at the JSON payload root (probe-confirmed trigger flag). The
  returned value is classified via `_classify_identifier`:
  `ASIN:Xxx` prefix → `MetaRecord.asin`; bare `B0XXXXXXXX` pattern
  → `MetaRecord.asin` (defensive — uploader forgot the prefix);
  bare digits with optional dashes or `ISBN:` prefix →
  `MetaRecord.isbn`. Surfaces the identifier MAM's web UI displays
  under "Identifier" but the search API previously dropped on the
  floor. Critical multiplier for v2.13.2's value: T1-T3 of the
  resolver chain can now actually fire for books where MAM has the
  identifier set but no author goodreads_id is backfilled.

### Changed

- **`MetaSource.search_book()` signature** extended with
  `isbn=""`, `asin=""`, `author_goodreads_id=""` kwargs. All 11
  concrete sources accept (and most ignore via `**_`) the extras.
  Only `GoodreadsSource` consumes them today.
- **`GoodreadsSource.search_book()`** fully rewritten. Builds a
  `ResolveQuery`, runs the T0–T5 resolver chain, on hit fetches
  `/book/show/{book_id}` through `GoodreadsSession` (uniform
  curl_cffi + soft-block + rate-limit), parses via the existing
  `_merge_detail_page()`, returns a `MetaRecord` tagged with
  `source="goodreads"`, `source_url`, and `external_id=book_id`.
  The misleading `"v2.11.0 will wire..."` log line that Mark spotted
  on day 1 is gone.
- **Enricher plumbing** — `MetadataEnricher.enrich()` accepts
  `author_goodreads_id`; `_safe_search()` and both call sites
  forward `isbn` + `asin` from the in-progress merged record (prior
  sources' contributions) plus the resolved `author_goodreads_id`.
- **Pipeline + review-router callers** call the new
  `get_goodreads_id_for_author()` helper and pass the result into
  `enricher.enrich()`. Empty string when the author isn't backfilled
  — T4/T5 no-op cleanly in that case.
- **`is_cloudflare_soft_block()`** (in `goodreads_session`) extended
  to also flag HTTP 403 and 429. AWS CloudFront's bot-rate gate on
  `/book/auto_complete` and `/author/list/` returns these instead of
  Cloudflare's 202-with-empty-body, but the right action is the
  same: flip session to `soft_blocked` so the dispatcher skips
  remaining Goodreads tiers cleanly. Function name kept for
  call-site stability.

### Deferred (not in v2.13.2)

- **Edition disambiguation via `/work/editions/{work_id}`** —
  investigated and dropped. The page is React-hydrated (7KB shell,
  no edition data in the static HTML), Rails-style `.json` suffix
  returns 404, and no documented JSON endpoint exists. T1-T3 already
  do edition-accurate ISBN/ASIN matching when MAM provides an
  identifier; the only case where Phase 3 would add value is when
  MAM's ISBN misses T1-T3 but the work exists on Goodreads — narrow
  edge with marginal metadata-quality upside (page count / pub date
  per-edition vs canonical). Revisit if UAT surfaces real mismatches.
- **Title-only fallback when `author_goodreads_id` is unknown** —
  wrong-book risk outweighs coverage gain. Authors with no stored
  goodreads_id (12% of Mark's library) simply skip T4/T5; T1-T3
  still run on whatever ISBN/ASIN MAM supplies.

### Tests

- 17 new tests across three files:
  - `tests/metadata/test_goodreads_id_resolver.py` — T4 hit /
    author-id filter / wrong-author rejection / multi-match
    highest-rated tiebreaker / 403 soft-block flip; the no-search
    regression test now also exercises T4/T5.
  - `tests/metadata/test_goodreads_bibliography.py` — page parser /
    early-stop on page 1 / multi-page walk / exhaustion marks
    fully_indexed / fully_indexed cache skips HTTP / soft-block
    returns marker without poisoning cache / 403 treated as
    soft-block / empty inputs no-op.
  - `tests/metadata/test_goodreads_session.py` — `is_cloudflare_soft_block`
    extended to cover 403 + 429.
- Updated `tests/metadata/test_enricher.py` fake-source `search_book`
  methods to accept `**_` so they tolerate the new identifier kwargs
  the enricher now forwards.

Suite: targeted slices passing (47 Goodreads + 33 enricher + 16 pipeline
+ 336 metadata + canary). Full suite gated by the 30-min interactive cap.

---

## [2.13.1] — 2026-05-14

Priority promotion: Goodreads restored to #2 (ebook) / #3 (audiobook)
after v2.13.0's bypass + author-id backfill made it reliably reachable
again. The v2.11.0 demotion (Goodreads #4) was a Cloudflare-defensive
move; with curl_cffi Chrome120 impersonation + the Phase-1/2 backfill
giving 88% author `goodreads_id` coverage on real-world libraries,
that defensive posture is no longer needed.

### Changed

- **`_DEFAULT_EBOOK_PRIORITY`** — new order:
  `mam → goodreads → hardcover → openlibrary → google_books →
   kobo → amazon → ibdb → audible`
- **`_DEFAULT_AUDIOBOOK_PRIORITY`** — new order:
  `mam → audible → goodreads → hardcover → openlibrary →
   google_books → amazon → kobo → ibdb`

### Migration

Existing installs keep their saved priority order — no automatic
migration. Users who want the v2.13.1 defaults click **Settings →
Sources → Reset to defaults**. The reset is reversible (one PUT
back to the desired custom order if they change their mind).

### Tests

- Updated `test_reset_restores_default_priority_order` to pin the
  new ordering (Goodreads at slot 2).

Suite: 2604 passed / 7 skipped (unchanged from v2.13.0).

---

## [2.13.0] — 2026-05-14

Stage 6 Goodreads Cloudflare bypass (Phase A). Closes the long-deferred
v2.11.2 ↦ v2.12.0 ↦ v2.13.0 work item from the metadata-overhaul arc.
First minor since v2.12.2's stub-row hotfix.

### Phase A — what ships now

curl_cffi Chrome120 TLS impersonation for every Goodreads HTTP call,
plus the dispatcher + observability scaffolding that lets us measure
whether the bypass actually holds under realistic burst load. Cookie
injection (`cf_clearance` + `_session_id2`) is held in reserve as
Phase B — only built if Phase A UAT shows persistent 202s.

### Added

- **`app/metadata/goodreads_session.py`** — centralized HTTP plumbing
  for goodreads.com. Single source of truth for TLS impersonation,
  soft-block detection (HTTP 202 / empty 2xx body), runtime-state
  flag transitions, and the 5s + 0–1s jitter rate-limit. All 4 call
  sites (discovery source, metadata source, ID resolver, paste-URL
  importer) route through it.
- **Three runtime-state keys** in settings.json:
  `goodreads_session_state` (active / soft_blocked / unknown),
  `goodreads_session_state_since`, `goodreads_session_last_status`.
  All three protected via `_RUNTIME_STATE_KEYS` so PATCH can't
  clobber them.
- **Dispatcher skip** in both source-iteration loops (per-book
  enricher + per-author scan) when state is soft_blocked. Without
  this, every iteration paid the full request → 202 → next-source
  roundtrip after the first failure.
- **`app/metadata/id_cache.py`** — SQLite-backed cross-reference
  cache. 30-day book_id TTL on hits, 1-day on misses (so dead-end
  ISBNs don't re-probe Goodreads every scan). 7-day author_bib TTL
  reserved for future discovery-source integration. WAL mode so the
  canary's read never blocks a live scan's write.
- **Hardcover `book_mappings` Tier 2** in the goodreads_id_resolver
  chain. Single GraphQL roundtrip: editions filtered by ISBN-13 / ASIN
  → book → book_mappings restricted to `platform.name == "Goodreads"`
  → external_id. Resolves the Goodreads ID for many books without
  ever touching Goodreads itself, when a Hardcover API key is
  configured. Closes the long-deferred Phase-1.5 Tier 2 slot.
- **Probe endpoints** under `/api/v1/metadata/goodreads/`:
  - `GET /state` — current runtime-state shape
  - `POST /test` — probe Goodreads (`mode=single` or `mode=burst`).
    Burst uses a canonical 10-book pool sourced from Mark's prod
    library 2026-05-14 (mix of recent + older IDs, mainstream +
    indie).
  - `POST /mark-active` — manually clear the soft-block flag
- **`scripts/probe_goodreads.py`** — standalone host-side variant of
  the burst probe for UAT baseline runs. Mirrors the in-app code path
  exactly so wire-level results are comparable.
- **Frontend GoodreadsStatusCard** in MetadataSourcesPanel. Status
  pill (Active / Soft-blocked / Unknown), "Run probe" + "Run burst
  (10×)" buttons, "Mark as active" (only when soft-blocked), and
  inline result rendering. No cookie input fields in Phase A.
- **Weekly Goodreads canary** (Mondays 03:00 local). One GET to The
  Hobbit through the production session module. On 202 it flips the
  state flag (handled by the session module) AND emits a ntfy
  notification gated on `notify_on_goodreads_canary_failed` (new
  per-event toggle, default True). Also prunes expired id_cache rows.
- **`docs/metadata-sources.md`** — full Phase-7 docs file covering
  per-source coverage, the Cloudflare bypass story end-to-end, the
  resolver chain, and the "what to do when Goodreads goes silent"
  runbook.
- **README pointer** + bump from "7 metadata sources" to "9" (OL +
  Audible were missing from the count).

### Changed

- **Goodreads default rate limit: 2.0s → 5.0s + 0–1s jitter.**
  Conservative Phase-A default per the design memo. Existing installs
  keep their saved value; only fresh installs get 5.0s. A one-click
  migration banner in the panel offers existing users a bump to 5.0s.
- The duplicate `_is_cloudflare_soft_block` helpers in
  `app/discovery/sources/goodreads.py` and
  `app/metadata/sources/goodreads.py` are replaced by re-exports from
  the new `goodreads_session` module, so the detection logic exists
  in exactly one place.
- `notify_on_goodreads_canary_failed` (default True) joins the
  v2.12.0 per-event ntfy toggle family.

### Added — stored-id short-circuit + author backfill (mid-UAT expansion)

The original v2.13.0 design shipped the bypass infrastructure. UAT on
2026-05-14 surfaced that source-scans still couldn't drive Goodreads
to fire HTTP — `search_author` is policy-locked to no-op and the
discovery flow never short-circuited to `get_author_books(stored_id)`
even when `authors.goodreads_id` was populated. The mid-UAT expansion
closes that wiring and adds the missing v2.11.0 promise (author-id
reverse-lookup from owned books).

- **`_try_source` stored-id short-circuit** in `app/discovery/lookup.py`.
  When `authors.{source}_id` is populated and the source is in
  `_AUTHOR_ID_COLUMN_SOURCES = {"goodreads", "amazon"}`, fabricates an
  AuthorResult and skips `search_author()` — Goodreads's policy-locked
  no-op no longer gates `get_author_books`. Scoped to goodreads +
  amazon only: Hardcover / Kobo / IBDB / Google Books all pre-populate
  books inline from their search_author and would lose that data if
  short-circuited (verified by UAT regression).
- **Calibre identifier mining** in `app/discovery/calibre_sync.py`.
  Pre-v2.13.0 the sync only mined ISBN from Calibre's `identifiers`
  table. The full mining now harvests every useful Calibre identifier
  type — `goodreads`, `amazon`, `mobi-asin`, `asin`, `google`, `kobo`,
  `audible`, `fictiondb`, `hardcover`, `openlibrary` — and lands them
  in the matching `books.{source}_id` columns via the new
  `_apply_calibre_ids` helper. Only fills empty slots, never clobbers
  other-source writes. Mark's library: jumped 0 → 2859 books with a
  stored goodreads_id (96% of his Calibre rows).
- **`app/discovery/goodreads_author_backfill.py`** — author-id
  reverse-lookup module. Two complementary code paths:
  - **`resolve_author_goodreads_id(author_id)`** (Phase 1): picks the
    strongest-identifier book for this author from Seshat's books
    table, derives a goodreads_book_id directly (book.goodreads_id) or
    via the resolver chain (ISBN/ASIN → tier 1 auto_complete / tier 2
    Hardcover book_mappings / tier 3 OL), fetches /book/show/{id} via
    the bypass, parses `author[].url` from JSON-LD, persists to
    `authors.goodreads_id`.
  - **`resolve_author_via_calibre_coauthor(author_id, author_name)`**
    (Phase 2, cross-DB): catches co-authors and pen-name aliases whose
    Calibre books exist (Calibre's N:N `books_authors_link` preserves
    them) but Seshat's 1:1 `books.author_id` attributed to the primary
    author only — leaving Seshat with author rows but zero attributed
    books. Queries Calibre's `metadata.db` directly, picks any book
    they contributed to with a `goodreads` identifier, fetches
    /book/show, parses ALL JSON-LD `author[]` entries via new
    `_parse_all_authors_from_html`, name-matches via Seshat's
    `normalize_author_name`, persists.
  - **`backfill_missing_author_ids()`** sweeps both phases as a single
    fire-and-forget task after every Calibre sync completion. Rate-
    limited via `goodreads_session`; bails early on soft-block;
    returns stats with phase2 broken out.
- **`_resolve_calibre_db_path()`** helper — discovers the per-library
  metadata.db path via `state._discovered_libraries` instead of the
  legacy single-library fallback constant. Fixes a UAT-caught miss
  where Phase 2 tried `/calibre/metadata.db` (doesn't exist on
  multi-library installs) instead of `/calibre/calibre-library/metadata.db`.

### Live UAT results on Mark's library

| Metric | Before v2.13.0 | After Phase 1 | After Phase 2 |
|---|---|---|---|
| Authors with `goodreads_id` | 507 / 740 (68%) | 585 / 740 (79%) | **650 / 740 (88%)** |
| Resolved this run | — | +78 | +65 |
| Soft-blocks during sweep | — | 0 | 0 |
| Sustained Goodreads traffic | — | ~9 min | ~15 min |

Remaining 90 are the genuine long tail: 3 books with no Goodreads
author profile (parser-confirmed), and ~87 authors whose Calibre
metadata also lacks a goodreads identifier on any of their books.

### Tests

Final suite: **2604 passed / 7 skipped**. New coverage:
- `test_goodreads_session.py` — 15 tests (session module)
- `test_id_cache.py` — 18 tests (cache module)
- `test_goodreads_id_resolver.py` — +3 Hardcover tier 2 tests
- `test_goodreads_session_router.py` — 9 router tests
- `test_goodreads_canary.py` — 5 scheduler tests
- `test_enricher.py` — +2 dispatcher-skip tests
- `test_goodreads_author_backfill.py` — 19 tests (Phase 1 + Phase 2)

### Known limitations

- Phase A relies on TLS fingerprint alone. If Cloudflare tightens (or
  for users whose IP reputation is bad), the burst probe may show
  202s. The Phase-A recovery path is raise the rate limit (8s+) and/or
  wait for the bot-score to decay. Phase B (encrypted cookie panel)
  is the engineered fallback held in reserve.
- The id_cache integrates only into the resolver today. Author
  bibliography caching (the 7-day scope) is wired but not yet
  consumed by `app/discovery/sources/goodreads.py` — held for a
  future patch.
- The ~87 truly orphaned authors (no goodreads identifier on any
  Calibre book) cannot be filled without either (a) running source
  scans on each so Hardcover/Amazon/OL pick up an identifier first,
  or (b) a future Hardcover-author-search fallback.
- Backfilling CHANGELOG entries for v2.11.0 / v2.12.0 / v2.12.1 /
  v2.12.2 is out of this arc's scope.

---

## [2.10.10] — 2026-05-13

Security + reliability hygiene patch as the deck-clearing first step
toward v2.11.0's metadata-overhaul work. No semantic behavior change
to discovery priorities or source selection — three independent fixes
informed by the post-v2.10.9 validation harness run.

### Security — Google Books API key was leaking into operator logs

`httpx.HTTPStatusError`'s string representation includes the full
request URL with query params, so when the base-class WARN log in
`app/discovery/sources/base.py` formatted the exception, the key
appeared verbatim in container logs:

```
google_books: GIVING UP on https://www.googleapis.com/books/v1/volumes after 1 attempts:
Server error '503 Service Unavailable' for url
'https://www.googleapis.com/books/v1/volumes?q=...&key=AIzaSy0N0Um...'
```

Added a `_redact_sensitive(s)` helper that strips known-sensitive
query params (`key`, `apikey`, `api_key`, `token`, `password`,
`secret`) from any string, applied at every log site inside
`base._get`. The leak is closed for Google Books today and for any
future keyed source the same way.

### Reliability — Google Books 503 transients now retry

Pre-v2.10.10 `GoogleBooksSource._get` passed `retries=0` to its
parent unconditionally — meaning *every* failure surfaced as
terminal, including 503 Service Unavailable transients. The
2026-05-12 validation harness caught 3 such transients in a single
14-author run (Logan Jacobs, K.D. Robertson, Asato Asato all FAILed
on Google Books from a 503 with no second attempt).

Split the failure policy by HTTP code:

- **429 (quota exhausted)** — still fails fast and counts toward
  the circuit breaker. Retrying a 429 just burns the daily quota.
- **5xx (transient server error)** — retries up to 2 times with
  exponential backoff (3s, 6s). Matches the Hardcover / Open
  Library / Goodreads behavior.
- **Network errors (ConnectError, etc.)** — same retry-on-503 path.

### Preventive — `audible_id` columns added to authors / series / books

Same defensive migration shape that v2.10.9 added for
`openlibrary_id`. Currently `AudibleSource` doesn't set
`external_id` on individual `BookResult`s, but if/when that
changes the dynamic `f"{source_name}_id"` merge path in
`lookup.py` would crash on a missing column — the exact gotcha
that bit Open Library before v2.10.9. Adding the columns now is
zero behavior change today and immunizes the future rollout.

Columns added to:
- SCHEMA (authors, series, books)
- MIGRATIONS list (3 ALTER TABLE statements)
- `_ensure_columns` safety-net (3 entries — full coverage)

### Tests

`tests/discovery/sources/test_google_books_api_key.py` gains 13
new tests across two new classes:
- `TestRedactSensitive` (6 tests) — verifies the redaction helper
  catches `key=` / `token=` / `api_key=` / case variants /
  multi-param URLs, leaves non-sensitive params alone, and handles
  the actual exception-message-as-input shape.
- `TestRetryOn503NotOn429` (5 tests) — 429 fails fast, 503 retries
  to success, 503 exhausts retries cleanly, counter resets on
  success, network errors retry.

Suite: **2283 passing, 7 skipped** (up from 2272).

---

## [2.10.9] — 2026-05-13

Hotfix for the bug v2.10.8 surfaced — Open Library was running for
the first time but its merge crashed because the `openlibrary_id`
column was never added to `authors` / `series` / `books`. Every
Open Library author scan dropped its full contribution silently
(192 books for Sanderson in the v2.10.8 UAT).

### Fixed — `no such column: openlibrary_id` on every Open Library merge

The discovery merge layer in `app/discovery/lookup.py` uses
`f"{source_name}_id"` to dynamically build the column name when
writing source-specific external IDs. Open Library was added as a
discovery source in v2.10.6 and backfilled into upgraded installs
in v2.10.8, but the corresponding columns were never added.

`app/discovery/database.py`:
- Added `openlibrary_id TEXT` to the SCHEMA for fresh installs
  (authors + series + books tables).
- Three new ALTER TABLE statements at the end of MIGRATIONS for
  upgraded installs.

### Fixed — Enricher noisy WARN for discovery-only sources

After v2.10.8 backfilled `openlibrary` into the priority lists,
the enricher (per-book metadata pipeline) logged
`enricher: unknown source 'openlibrary' in priority list` on every
dispatcher rebuild because it has no MetaSource counterpart for
Open Library yet (planned for v2.11.0).

`app/metadata/enricher.py:_build_default_sources` now distinguishes:
- Sources known to `KNOWN_SOURCES` but missing from the enricher's
  `_SOURCE_REGISTRY` → DEBUG log ("discovery-only source, skipped")
- Genuinely unknown source names → WARN as before

### Tests

- `tests/discovery/test_openlibrary_id_migration.py` (new, 5 cases) —
  fresh-install schema includes `openlibrary_id` on all 3 tables;
  v2.10.9 migration ALTERs add it to legacy installs; smoke-test
  that the dynamic `f"{source}_id"` write pattern actually works
  post-migration.

Suite: **2272 passing, 7 skipped** (+5 from v2.10.8's 2267).

### Deferred to v2.11.0

- `app/metadata/sources/openlibrary.py` — a per-book MetaSource
  for Open Library so the enricher can use it for review-queue
  enrichment (currently OL only contributes via discovery scans).

---

## [2.10.8] — 2026-05-13

Three issues surfaced during the v2.10.7 Sanderson UAT scan, all
fixed in one bundle. Without these the Open Library source shipped
in v2.10.6 never actually ran for upgraded installs.

### Fixed — New sources not picked up by upgraded installs

`migrate_legacy_settings` had an idempotent-skip gate that returned
early once `metadata_sources` + `metadata_priority` were both
populated. That gate fires the first time settings are loaded after
a v2.10.x install, and never re-runs — so any source added in a
later release (e.g. `openlibrary` in v2.10.6) silently never
appears in the persisted settings dict, and the discovery scanner
filters it out of every scan.

**Fix:** new `_backfill_known_sources` pass that runs UNCONDITIONALLY
on every settings load (after the legacy migration's exit gate).
For each name in `KNOWN_SOURCES` not already in
`settings["metadata_sources"]`, adds an entry from
`_DEFAULT_NEW_INSTALL_STATE` + the `KNOWN_SOURCES[name].default_rate`
rate. For each priority list slot missing the source (and where the
source's `available_for` includes that content type), appends at the
END so user-curated head order is preserved.

Mark's specific symptom: Sanderson rescan showed 0 contribution
from Open Library because it wasn't in his `metadata_sources` dict
at all. Verified post-fix that the backfill adds it with the
shipped defaults.

### Added — Amazon thin-200 body warning

When Amazon returns a 200 OK with a body under 50KB on what should
be a search results page (~1MB+ in normal operation), it's almost
always a silent soft-block or layout shift — not a CAPTCHA page our
existing detector would catch. Pre-v2.10.8 we returned the body and
parsed 0 ASINs without any log signal, leaving operators puzzled
about why "amazon: no search results" fired for an author who
clearly has thousands of Amazon hits (Brandon Sanderson, etc.).

`app/discovery/sources/amazon.py:_fetch` now logs a WARNING when
the 200 body is suspiciously thin, so the case is visible in
container logs without turning on debug mode.

### Fixed — Kobo timed out on prolific authors

Kobo's per-source timeout was 120s through v2.10.7. The Sanderson
UAT scan timed out at exactly 120s with `keeping any partial
writes, moving on` — Kobo's per-book detail-fetch loop is sequential
under cloudscraper and 82 books × 1.5s rate-limit alone exceeds
that budget.

Bumped Kobo's `timeout_sec` to 300s (matching Goodreads' historical
primary-tier timeout). Long-term fix is parallelizing the detail
loop with a semaphore — deferred to v2.11.x.

### Tests

- `tests/metadata/test_source_config.py` — 4 new cases for the
  backfill (adds new known source to existing install, idempotent,
  preserves user-curated head order, doesn't add audiobook-only
  source to ebook priority).
- `tests/metadata/test_source_config.py::test_legacy_priority_preserved`
  updated to assert head-order preservation + tail backfill (was
  asserting the legacy list stayed exact, which the v2.10.8 backfill
  legitimately changes).
- `tests/discovery/sources/test_amazon_hardening.py` — 2 new cases
  for the thin-body warning (fires under 50KB, doesn't fire above).

Suite: **2267 passing, 7 skipped** (+6 from v2.10.7's 2261).

### How to verify the upgrade picked up Open Library

Run any author rescan after upgrading. The first scan triggers the
backfill, and you should see Open Library appear in the per-source
log lines. Pre-v2.10.8 it never fired; post-v2.10.8 it does.

Persisted settings get the new source added on the next save —
either an explicit Settings UI write or the next implicit save
the dispatcher does after a scan completes.

---

## [2.10.7] — 2026-05-13

Phase 3 of the v2.11.0 metadata-source overhaul: wires the Google
Books API key end-to-end. Pre-v2.10.7 the source hit Google's
no-key public endpoint which kept tripping its 429 circuit-breaker
(the anonymous quota is shared across every client on the same IP
and exhausts fast under modest scan load).

### Added — Google Books API key plumbing

- **`app/discovery/sources/google_books.py:GoogleBooksSource`**
  takes a new optional `api_key` constructor arg and exposes
  `update_api_key()` (mirrors the v2.10.5 HardcoverSource pattern).
  New `_request_params()` helper merges the key into every request
  params dict so the existing two query sites (`search_author` +
  `get_author_books`) both pick it up without further wiring.
- **`app/secrets.py:SECRET_KEYS`** — adds `google_books_api_key` so
  `_resolve_secrets` picks it up at startup like any other encrypted
  credential.
- **`app/discovery/lookup.py`** — `reload_sources` reads the key from
  settings and passes it to the GoogleBooksSource constructor; the
  per-author scan also runs a pre-flight `update_api_key()` injection
  with the freshest secrets-store value (same pattern as Hardcover)
  so a key rotation in the Settings UI takes effect on the next scan
  without restarting the process.
- **`frontend/src/pages/SettingsPage.tsx`** — new `CredField` for
  `google_books_api_key` rendered alongside the existing Hardcover
  key in Settings → Sources, with help text covering the IP
  restriction + quota tradeoff.

### Setup (operator)

Runbook at `/home/mbaker/Documents/Projects/files/google-books-api-setup.md`
(referenced by the v2.11.0 plan memory) covers the Google Cloud
Console steps end-to-end: project creation → Books API enable →
credentials → key creation → IP-restrict to server egress + scope
to Books API only → paste into Settings.

### Tests

- `tests/discovery/sources/test_google_books_api_key.py` (new, 9
  cases) — `_request_params` key injection (no-key, key-set,
  whitespace, empty-string, no-mutation), `update_api_key` lifecycle
  (changes subsequent params, can disable, strips whitespace,
  None-safe).

Suite: **2261 passing, 7 skipped** (+9 from v2.10.6's 2252).

### Live verification

Direct API test from the production container with the new key
returned `200 OK` with full payload. The same call without the key
returned `429 Quota exceeded` — confirming the keyed endpoint has
its own (much larger) quota allocation distinct from the throttled
anonymous bucket.

---

## [2.10.6] — 2026-05-13

Three discovery-source improvements bundled together: Open Library
joins as a free no-key supplementary source (Phase 2 of the v2.11.0
metadata-source overhaul), the Kobo author-match gate gets the same
v2.10.5 punctuation-insensitivity fix Hardcover received, and
Amazon's `_fetch` learns to detect CAPTCHA / robot-check responses
explicitly with jittered rate-limiting (Phase 4).

Google Books API-key wiring (Phase 3) lands in a follow-on once
Mark provisions the key per the runbook at
`/home/mbaker/Documents/Projects/files/google-books-api-setup.md`.

### Added — Open Library discovery source

`app/discovery/sources/openlibrary.py` (new) — sanctioned REST API
(no auth required), two-phase shape mirroring the v2.10.5
HardcoverSource pattern:

  1. `_resolve_author_key`: search authors by name via
     `/search/authors.json`, disambiguate by strict normalized-name
     match + `work_count` tiebreaker. Falls back to the top-ranked
     OL hit when no candidate passes the strict gate.
  2. `_fetch_all_author_works`: paginated walk of
     `/authors/{key}/works.json` (100 entries/page, 30-page max).

Series extraction is best-effort from title patterns
("Title (Series Name, #N)") with a reject-list for common edition
decorations ("Annotated", "Illustrated", "Nth Edition", "Boxed Set"
— exact-name lookup PLUS regex for ordinal-edition variants).

Live coverage check across the 14-author validation set:
- **Strong**: Brandon Sanderson 192, Jim Butcher 129, Corey 91,
  J. N. Chaney 95, Karen Traviss 80, Sabaa Tahir 43
- **Weak**: indie/non-Latin (Marcus Sloss 10, Hasekura 5)

Registered in `app/metadata/source_config.py` as supplementary
tier (`mandatory: false`, default-enabled). Slotted into the
default ebook + audiobook priority lists at position 8 / 8.

### Fixed — Kobo author-match drops books on space/period mismatch

Same root cause we fixed in HardcoverSource at v2.10.5: when the
queried author name's punctuation doesn't exactly match Kobo's
canonical spelling, every book gets silently dropped. Reproduced
during the v2.10.5 UAT scan: every "J.N. Chaney" Kobo result was
rejected because the queried name was "J. N. Chaney" (with spaces).

The matcher's previous tiers — exact, period-strip, parts-set —
all fail this case because period-strip alone leaves "j n chaney"
vs "jn chaney", which don't compare equal. Added a third tier
that strips punctuation AND whitespace; both forms now collapse
to "jnchaney" for comparison.

Refactored the previously-closure'd matcher into a module-level
`_kobo_author_matches(card_author, queried, linked)` so it's
unit-testable independently of the surrounding `get_author_books`
plumbing.

### Fixed — Amazon `_fetch` silently swallows CAPTCHA / robot-check responses

The Phase 0 baseline harness showed Amazon CAPTCHA-blocking
mid-batch (5 of 14 authors worked, then everything else returned
None with no clear log signal). Pre-fix `_fetch` returned None on
any non-200 with no distinction between "genuine miss" and "blocked
at the gate," and lacked jitter on the rate-limit which is itself
a fingerprint trigger.

`app/discovery/sources/amazon.py:_fetch`:

- **CAPTCHA detection** on 200 responses (`_is_captcha_page`).
  Amazon serves `/errors/validateCaptcha` interstitials with HTTP
  200, so a status-only check accepted them. Detected pages log
  distinctly ("CAPTCHA challenge detected ... soft-blocked") and
  return None without retry (retrying just hits the same gate).
- **Robot-check 503 detection** (`_is_robot_check_503`). 503s
  with "Robot Check" or "automated access" in the body are soft-
  blocks, not upstream errors — single attempt, no retry.
- **Genuine 5xx retry** with one 8s backoff. Network / upstream-
  transient failures retry once; persistent 5xx degrades to None.
- **Jittered rate-limit**: `rate_limit + uniform(0, 0.5)` so
  request cadence isn't perfectly periodic.
- **CAPTCHA re-check on retry path** — Amazon sometimes flips
  from 503 to a 200-CAPTCHA between calls.

### Tests

- `tests/discovery/sources/test_openlibrary.py` (new, 20 cases) —
  series-extraction edge cases, author-key resolution
  (single/multi/tiebreak/no-strict-match/period-normalization),
  pagination, end-to-end assembly, format-decoration rejection
  (Annotated, Nth Edition, Boxed Set, etc.).
- `tests/discovery/sources/test_kobo_author_matches.py` (new, 12
  cases) — every match tier including the v2.10.6 punct+ws regression
  case, pen-name aliases, partial-match rejection.
- `tests/discovery/sources/test_amazon_hardening.py` (new, 19
  cases) — pure helpers for CAPTCHA / robot-check detection, plus
  `_fetch` behavior tests (normal 200, CAPTCHA-no-retry, robot-503-
  no-retry, genuine 5xx with backoff, retry-CAPTCHA, 4xx-no-retry,
  jitter applied to rate-limit, network error).

Suite: **2252 passing, 7 skipped** (+51 from v2.10.5's 2201).

### Implications for v2.11.0

- Open Library is now a real participating source with sanctioned
  API access, no rate-limit risk, and meaningful trad-pub coverage.
  Adds a useful safety net when Hardcover misses a book and
  Goodreads is Cloudflare-blocked.
- The Kobo punctuation fix retroactively closes a coverage gap
  that's been silently dropping books for any author with
  initials. Worth a re-scan of any author with "X. Y." style names.
- Amazon hardening doesn't increase coverage but should reduce
  the noise when Amazon does block — distinct logs make
  diagnosing future blocks much faster.

---

## [2.10.5] — 2026-05-13

Rewrites `HardcoverSource` to use a direct two-phase author-id
lookup. Fixes a major coverage bug surfaced during Phase 0 of the
v2.11.0 metadata-source overhaul where Jim Butcher returned 0
books, Brandon Sanderson returned 3, J.N. Chaney returned 0 —
when their actual Hardcover catalogs are 146, 1000+, and 398
books respectively.

### Fixed — HardcoverSource grossly under-reported author bibliographies

**Root cause:** the pre-v2.10.5 implementation used Hardcover's
SEARCH (`query_type: "Book"`) endpoint which finds books with the
author's name in the **TITLE**, not books **BY** the author. For
Jim Butcher this returned 10 graphic novels titled "Jim Butcher's
The Dresden Files: …" and missed every actual Dresden Files
novel, every Codex Alera book, and every Cinder Spires entry
because their titles ("Storm Front", "Furies of Calderon",
"The Aeronaut's Windlass") don't contain the author's name.

**Fix:** replace book-search-by-name with direct author-id lookup
via the `contributions` GraphQL relation (the correct field name
per schema introspection; the previously-defined-but-unused
`AUTHOR_BOOKS_QUERY` referenced a non-existent `book_authors`
field). New flow:

1. **Phase 1 — `_resolve_author_id`** runs SEARCH_AUTHOR_QUERY
   (`query_type: "Author"`) to find candidate author IDs, then a
   batched AUTHORS_META lookup to disambiguate namesakes by
   strict normalized-name match, with books_count as tiebreaker.
2. **Phase 2 — `_fetch_all_author_books`** walks
   `authors(id={resolved}).contributions` paginated via
   `$limit`/`$offset` (100 rows per page, up to 20 pages).
3. **Per-book processing loop** stays unchanged — series picking,
   edition selection, cover, release date.

The old per-book authorship-name gate is retired (books came in
via the author's contributions relation, so they're definitionally
by them). This also fixes a punctuation-mismatch edge case where
"J. N. Chaney" (space-separated) failed to match Hardcover's
canonical "J.N. Chaney" (no space) and dropped every book.

**Verified against the 14-author ground-truth fixture (live API):**

| Author | Pre-fix | Post-fix | Hardcover books_count |
|---|---|---|---|
| J. N. Chaney | 0 | **511** | 398 |
| Marcus Sloss | 2 | 145 | — |
| Sabaa Tahir | 3 | 84 | — |
| James S. A. Corey | 5 | 257 | — |
| **Jim Butcher** | **0** | **445** | 146 |
| **Brandon Sanderson** | **3** | **1032** | — |
| William D. Arand | 0 | 47 | — |
| **Logan Jacobs** | **0** | **363** | — |
| Jon Messenger | 0 | 10 | 10 (Hardcover genuinely sparse) |
| Karen Traviss | 0 | 94 | — |
| Robyn Bee | 2 | 2 | — |
| K.D. Robertson | 0 | 34 | — |
| Asato Asato | 0 | 49 | — |
| Isuna Hasekura | 0 | 124 | — |

Post-fix counts exceed `books_count` because Hardcover returns
all editions / translations under the contributions relation;
downstream dedup (existing in lookup.py) handles edition
consolidation per the established pipeline pattern.

### Fixed — Validation harness undercount in `scripts/validate_sources.py`

The Phase 0 baseline harness counted only `result.books`
(standalone) and missed `result.series[*].books`. Hardcover
delivers most results under `series[]` (every Dresden Files
book gets grouped under "The Dresden Files" series), so the
harness reported 0 books for Jim Butcher when the actual
result was 10 books — masking the real shape of the fix
during pre-release verification. Harness now totals across
both fields.

### Fixed — Typo in YAML ground-truth fixture

`tests/fixtures/goodreads_ground_truth.yaml` had a comment on
K.D. Robertson's "Heretic Spellblade: Epilogue" that was
accidentally copy-pasted from the adjacent Asato Asato section
("English translated it is '86—EIGHTY-SIX, Vol. 10...'"). Removed
the stray comment.

### Tests

- `tests/discovery/sources/test_hardcover_search.py` (new,
  14 cases) — covers `_resolve_author_id` (single candidate,
  namesake disambiguation by name + books_count, no-match,
  fallback to top-ranked id), `_fetch_all_author_books`
  (single page, multi-page pagination with offset tracking,
  audiobook format_id routing, empty author), end-to-end
  `search_author` (Jim Butcher regression, punctuation-
  mismatch regression, no-key, resolver miss), and a
  policy regression test asserting `query_type: "Book"` is
  never used.

Suite: **2201 passing, 7 skipped** (+14 from v2.10.4's 2187).

### Diagnostic artifacts

`scripts/probe_hardcover.py` (new) — schema introspection +
multi-query probe used to diagnose the bug. Useful pattern for
future Hardcover schema changes. Run with
`docker exec Seshat python /app/scripts/probe_hardcover.py`.

### Implications for v2.11.0 priority decisions

This fix invalidates the Phase 0 baseline observations on
Hardcover coverage. The v2.11.0 plan should be re-run against
the fixed source before any default-priority decisions. Notes
in `memory/project_seshat_metadata_overhaul.md` updated to
reflect this.

---

## [2.10.4] — 2026-05-13

Phase 1 + 1.5 (tier 1+3) of the v2.11.0 metadata-source overhaul,
shipped as a standalone patch. Drops Goodreads' `/search` endpoint
(robots-disallowed for `*` user-agents) from every scraper path,
adds explicit Cloudflare-202 soft-block detection, and lands the
ethical `goodreads_book_id` resolver module ready for v2.11.0
wiring. No user-visible behavior change beyond clearer logs and
better error UX on the manual paste-URL import path.

### Fixed — Goodreads `/search` calls violate robots.txt

Goodreads' robots.txt explicitly contains `Disallow: /search` for
the `*` user-agent. Pre-v2.10.4 the enricher hit
`/search?q=…&search_type=books` for free-text title+author lookup,
and the discovery scanner hit the same endpoint to resolve
author-id from a name. Both paths are out-of-policy. Holding a
higher standard than Calibre's kiwidude plugin (which rotates a
fake browser UA to bypass `/search`), we removed both:

- **`app/metadata/sources/goodreads.py:search_book`** — returns
  None with an informational log instead of hitting `/search`.
  The `_merge_detail_page` parser for `/book/show/{id}` (the
  robots-permitted endpoint) is kept for v2.11.0 wiring. Dead
  helpers (`_parse_search_results`, `_strip_parentheticals`,
  `_upgrade_cover_url`) removed.
- **`app/discovery/sources/goodreads.py:search_author`** — same
  treatment. Returns None with a log pointing future ops at the
  v2.11.0 reverse-lookup path (extract `goodreads_author_id` from
  any owned book's JSON-LD). `_pick_author_from_book_search` and
  the variant-retry loop deleted.
- **`app/discovery/routers/import_export.py:_fetch_goodreads_book`**
  — the user-initiated paste-URL import is preserved (it uses
  `/book/show/{id}`, which IS robots-permitted). Added explicit
  Cloudflare-gate detection that surfaces a 503 with a clear
  "Goodreads is currently soft-blocking this server's IP — try
  Hardcover instead" error rather than silently returning an
  empty record.

### Added — Cloudflare soft-block detection helpers

`_is_cloudflare_soft_block(resp)` in both the enricher and
discovery Goodreads sources distinguishes Cloudflare's
202-with-empty-body interstitial from genuine "Goodreads doesn't
have this content" responses. Future cookie-refresh diagnostics
(v2.11.0 Phase 5.5) rely on this signal being clean.

### Added — Ethical `goodreads_book_id` resolver module

`app/metadata/goodreads_id_resolver.py` (new) implements the
three-tier ethical resolution chain documented in
`memory/project_seshat_metadata_overhaul.md` Phase 1.5:

1. **Tier 1** — `/book/auto_complete?format=json&q={isbn_or_asin}`
   (Goodreads' undocumented JSON endpoint, NOT in any Disallow
   block; identifier-based, not free-text)
2. **Tier 2** — Hardcover GraphQL `book_mappings` (stub; deferred
   to v2.11.0 once the Hardcover discovery client is mature)
3. **Tier 3** — Open Library `?bibkeys=ISBN:…&jscmd=data` →
   `identifiers.goodreads`

Module ships available-but-unused in v2.10.4 — wiring into the
enricher's dispatcher requires extending `search_book(title,
author)` to also pass ISBN/ASIN, which is part of the broader
v2.11.0 source-interface refactor. Resolver carries its own
Cloudflare 202 detection (`soft_blocked` flag on the
`ResolveResult`) so callers can distinguish "Goodreads doesn't
know this book" from "Goodreads gated us."

### Added — Phase 0 validation harness

`scripts/validate_sources.py` (new) runs each discovery source
against a fixed set of 14 benchmark authors (10 from the initial
v2.11.0 plan + Robyn Bee, K.D. Robertson, Asato Asato, Isuna
Hasekura for newer/non-Latin coverage). Outputs a Markdown
report to `docs/validation/sources-<timestamp>.md`. Run manually
to capture baseline + measure delta after each phase. Not part
of pytest. Optional `hardcover_api_key` is read from the secrets
store automatically when run inside the Seshat container.

Baseline pre-v2.10.4 captured at
`docs/validation/baseline-pre-v2.10.4-20260512.md`.

### Tests

- `tests/metadata/test_goodreads_id_resolver.py` (new, 12 cases) —
  per-tier hit/miss, soft-block detection, no-`/search`-regression
  proof.
- `tests/metadata/sources/test_goodreads.py` — replaced
  `TestParseSearchResults` with `TestSearchBookDisabled` (verifies
  zero HTTP from `search_book`) and `TestCloudflareSoftBlockDetection`.
- `tests/discovery/sources/test_goodreads_search.py` — replaced
  `_pick_author_from_book_search` parser tests with policy-
  regression tests asserting no `goodreads.com/search` URL ever
  appears in mock-transport call lists.

Suite: **2187 passing, 7 skipped** (up from 2174 at v2.10.3
→ +13 new tests).

### Deferred to v2.11.0

- Wiring the resolver into the enricher dispatcher (requires
  extending `search_book` signature to pass ISBN/ASIN)
- Hardcover `book_mappings` Tier 2 implementation (needs mature
  Hardcover discovery client)
- Goodreads author-id reverse-lookup via `/book/show/{id}`
  JSON-LD (the ethical alternative to the dropped
  `search_author` `/search` pivot)
- Cloudflare workaround via `curl_cffi` + cookie reuse (Phase 5.5)

---

## [2.10.3] — 2026-05-12

CWA delivery throttle to work around a CWA cps wedge, plus a UI
control for the v2.9.0 format-dedup hold window that was added to
the backend defaults but never exposed in Settings.

### Fixed — Overlapping CWA ingests wedge cps's HTTP listener

- **`app/sinks/_cwa_throttle.py`** (new) — per-ingest-path async
  lock + minimum-gap-since-last-release. CWA's post-import
  duplicate scan runs inside the single-threaded cps web process
  on a 5s default debounce; when Seshat dropped a second book
  into the watched ingest folder while that scan was pending or
  running, the second book's ingest-processor → cps web-API
  callbacks (`session_refresh`, `cache_invalidate`,
  `schedule_scan`) all hit a 5s read timeout and cps stopped
  accepting any HTTP at all until the container was restarted.
  Reproduced 2026-05-11 ~22:40 with a two-book approve-all from
  Seshat. The throttle serializes deliveries to a given CWA path
  and ensures consecutive drops are at least N seconds apart;
  multi-CWA setups with different ingest paths don't contend.
  Single deliveries pay zero wait (no prior release to gate
  against).

- **`app/sinks/cwa.py:CWASink`** — wraps the file copy/rename in
  the throttle context manager. New `min_gap_seconds` constructor
  arg (default 10s).

- **`app/orchestrator/dispatch.py:PipelineDeps`,
  `app/main.py`, `app/orchestrator/pipeline.py`,
  `app/orchestrator/budget_watcher.py`,
  `app/orchestrator/review_timeout.py`,
  `app/orchestrator/reingest.py`, `app/routers/review.py`** —
  threaded `cwa_min_inter_book_seconds` through the same
  topology as `cwa_ingest_path` so every entry-point that
  delivers to CWA (download-complete, review approval, review
  timeout, reingest) carries the user's setting through.

- **`app/config.py:cwa_min_inter_book_seconds`** — new setting,
  default 10. 0 disables (safe if you've turned off CWA's
  "Enable automatic duplicate scans"). Raise above 10 if you've
  increased CWA's `After import debounce` setting.

- **`frontend/src/pages/SettingsPage.tsx`** — new "CWA Inter-Book
  Delay" input in Sinks → Advanced paths. Numeric, 0–120
  seconds, with help text covering both the "disable" and
  "raise" cases.

### Fixed — v2.9.0 format-dedup hold window had no UI control

- **`frontend/src/pages/SettingsPage.tsx`** — new "Format Dedup
  Hold Window" input in Discovery → MAM, right after the Format
  Priorities row. Integer minutes, 1–60 range, persists as
  seconds (`format_dedup_hold_seconds`). The FiltersPage
  description text already pointed users at "Settings" for this
  knob — wiring it makes that hint true.

### Tests

- `tests/sinks/test_cwa_throttle.py` (new, 10 cases) — first
  delivery passes through, second within window waits the
  remainder of the gap, partial-elapsed subtracts, past-window
  no wait, different paths independent, gap=0 disables, negative
  gap treated as disabled, concurrent deliveries serialize.
- Two CWASink integration cases for the throttle wired into the
  sink's deliver path (gap=0 → no wait; gap > 0 → second
  delivery pays it).

Suite: **2174 passing, 7 skipped** (up from 2164 at v2.10.2).

---

## [2.10.2] — 2026-05-12

`_resolve_position_collision` now routes through `merge_books`
so the existing source-scan series-slot dedup carries over the
loser's identity fields and writes an audit row.

### Fixed — Pre-existing series-position dedup silently dropped loser identity fields

- **`app/discovery/lookup.py:_resolve_position_collision`** —
  pre-v2.10.2 this helper hard-DELETEd the loser when two books
  collided at the same `(author_id, series_id, series_index)`.
  Any identity fields the loser uniquely carried
  (`mam_torrent_id`, `goodreads_id`, `isbn`, `hardcover_id`, etc.)
  were silently discarded. The helper now folds via the v2.10.0
  `merge_books` engine — same winner policy (owned, non-Book-N
  title, lowest id), but the loser's data coalesces into the
  winner and a `book_merges` audit row is written with
  `reason="series_position_collision"`.

- **`app/discovery/lookup.py:_title_to_series_pass`** — second
  call site (previously had its own inline copy of the dedup
  logic) now also routes through `_resolve_position_collision`,
  eliminating the duplicate implementation.

  This was the path that silently cleaned up 5 of Mark's
  duplicate pairs on 2026-05-12 (the structural-mismatch ones —
  Right of Retribution 3 with `(Series #N)`, Super Sales 4 with
  `(Series #N)`, Swing Shift 2/3, Mickey7) — no harm done in his
  case because the calibre rows already had matching
  `mam_torrent_id` from `acquisition_linkback`, but the sharp
  edge could bite a future case where the loser holds the only
  copy of an external ID.

### Tests

- **`tests/discovery/test_series_collision_merge.py`** — 2 new
  tests covering both call sites: identity coalesce on a
  title→series collision (calibre winner) and on an
  orphan-promotion collision (lowest-id winner). Both assert
  the `book_merges` audit row with `series_position_collision`
  reason.

---

## [2.10.1] — 2026-05-12

End-of-sync legacy-duplicate heal pass — closes a v2.10.0 gap
that surfaced during Mark's UAT.

### Fixed — Per-UPDATE sweep silently no-ops when Calibre is quiet

- **`app/discovery/calibre_sync.py:_heal_legacy_duplicates`** —
  new pass that runs at end-of-sync regardless of mode and scans
  every existing Calibre row for an unowned non-Calibre row with
  an exact-title match (same article-stripping rule as the
  INSERT-path merge and per-UPDATE sweep). When exactly one
  match exists, it's folded in via `merge_books` with
  `reason="calibre_sync_legacy_heal"`.

  Why this is needed: the v2.10.0 per-UPDATE sweep only fires
  for books Calibre touched in the current sync run. Mark's
  2026-05-12 case: he fixed Calibre titles BEFORE v2.10.0
  deployed, so the post-deploy resync ran in incremental mode
  with `0 books, 0 new, 0 pruned` — every duplicate pair he
  expected to heal stayed stuck because no UPDATE event fired.
  The legacy heal pass closes that gap. Idempotent — re-running
  after every duplicate has been folded finds nothing.

### Tests

- **`tests/discovery/test_calibre_sync_merge_sweep.py`** — one
  new test (`test_legacy_heal_pass_runs_in_incremental_mode`)
  that pre-stages the duplicate state Mark hit, then runs a sync
  with `_read_calibre_db` mocked to return 0 books (the exact
  incremental-quiet scenario), and asserts the pair heals.

---

## [2.10.0] — 2026-05-12

Duplicate-row resolution: manual merge UI + automatic post-UPDATE
merge sweep in calibre_sync. Closes the loop on the title-mismatch
duplicate class that surfaced in Mark's 2026-05-11 sync run, where
11 of the 17 books he grabbed/reingested ended up as duplicate
pairs (one owned-Calibre row + one unowned-discovery row) because
Calibre's title (e.g. "Right of Retribution: Book 2") didn't
match the discovery scan's title (e.g. "Right of Retribution 2")
under the existing strict exact-match merge query in calibre_sync.

Two complementary paths now resolve these:

### Added — Post-UPDATE merge sweep in calibre_sync

- **`app/discovery/calibre_sync.py:_post_update_merge_sweep`** —
  runs after every UPDATE on an existing Calibre row. If the
  row's title now exactly matches a single unowned discovery row
  (same author, source != 'calibre', calibre_id IS NULL), the
  sweep folds the discovery row into the Calibre row via the
  shared `merge_books` helper. Same exact-title-match shape as
  the INSERT-path merge query — multi-match or no-match cases
  remain conservative no-ops.

  This heals the specific case where a user fixes a wrong Calibre
  title to match an existing discovery row's title and re-syncs.
  Pre-v2.10.0 the resync just rewrote the Calibre row's title
  field and left both rows in place forever. Of Mark's 11 stuck
  pairs from 2026-05-11, six (RoR 2, DD 2/3, SS 2/3, A
  Temperamental Enchantress) had their Calibre titles fixed by
  the user — those heal on the next sync after deploying this
  release.

### Added — Manual merge UI

- **`app/discovery/book_merge.py`** — shared `merge_books()`
  function used by both the calibre_sync sweep and the manual
  endpoint. Field-resolution rules: identity fields (mam_torrent_id,
  goodreads_id, isbn, etc.) coalesce winner-first, owned uses MAX,
  hidden uses MIN (visible wins), timestamps preserve earliest
  first_seen_at and latest mam_last_scanned_at. Two
  owned-Calibre-source rows are refused — that case is upstream
  duplicate-Calibre data that the user should remove from
  Calibre, not paper over in Seshat.

- **`POST /api/discovery/books/{bid}/merge?slug=…`** — manual
  merge endpoint. Body `{other_id}`. Backend deterministically
  picks the winner via `pick_winner_id` (calibre+owned > calibre
  > owned > rest, tiebreaks by lowest id) so the user doesn't
  have to choose. Returns the surviving row so the frontend can
  decide whether to refresh in place or navigate to the winner's
  id when the initiator was absorbed.

- **`frontend/src/components/MergeBookModal.tsx`** — search-and-pick
  modal opened from BookSidebar's new Merge button. Default search
  is scoped to the same author (the overwhelming case);
  "All authors" checkbox handles pen-name / mis-attribution edge
  cases. Clicking a result highlights it; "Approve Merge" submits
  and the parent list refetches.

- **`book_merges`** audit table — every merge writes a row with
  the full loser snapshot as JSON. Manual rollback is a hand-SQL
  job today; the audit row exists so forensic recovery is
  possible if a sweep ever folds the wrong row.

### Migration

- New `book_merges` table on each per-library books DB. Idempotent
  via the existing `MIGRATIONS` list mechanism.

### Tests

- **`tests/discovery/test_book_merge.py`** — 14 unit tests for
  the merge engine: field resolution (identity coalesce, hidden
  MIN, timestamp preservation), book_grab_links FK redirect with
  the UNIQUE-collision drop-loser-link fallback, audit row
  capture, error preconditions (same id, missing row, two
  owned-Calibre rows).
- **`tests/discovery/test_calibre_sync_merge_sweep.py`** — 3
  integration tests replaying Mark's exact scenario (heal a RoR
  2 duplicate via title fix + re-sync), a negative test
  (title still mismatched → no merge), and a multi-candidate
  ambiguity test.
- **`tests/discovery/test_book_merge_endpoint.py`** — 5 HTTP
  endpoint tests covering the winner-policy delegation, error
  shapes (400/404), and the symmetric initiator-from-either-side
  case.

---

## [2.9.1] — 2026-05-12

Reingest matcher hardening. Two interacting flaws in
`_name_score` let the v2.8 reingest pipeline match torrents
against the wrong files on disk.

### Fixed — Reingest false-positive on single-char directories

- **`app/orchestrator/reingest.py:_name_score`** — the substring
  tier (`b in a or a in b` → score 60) accepted matches of any
  length, so a directory basename of just `2` or `3` (common in
  multi-book bundle layouts like `collection/2/`,
  `collection/3/`) substring-matched every reingest target
  containing that digit. The directory's contents were then
  pulled in as a score-60 candidate. Mark's reingest of
  "Ghost Academy 2: Fall Term" matched
  `/[mam-complete]/[2024-02]/collection/2/` and treated a
  Warhammer Empire Army bundle's *Iron Company* by Chris Wraight
  as the snatch source. Same story for "Ghost Academy 3" →
  *Call to Arms* by Mitchel Scanlon out of `collection/3/`.
  The substring tier now requires the matched-substring side
  to be at least 4 characters long.

### Fixed — Reingest zero-padded numeric tokens

- **`app/orchestrator/reingest.py:_name_score`** — MAM torrent
  titles use plain numbers ("Ghost Academy 2") while many
  on-disk filenames use zero-padded indices ("Ghost Academy 02").
  The `_TOKEN_RX` word-tokenizer kept `02` and `2` as distinct
  tokens, dropping the Jaccard fallback below the 0.6 threshold
  on otherwise-obvious matches. A new `_normalize_numeric_padding`
  helper strips leading zeros from digit runs before
  comparison and tokenization, so `02` and `2` now match.
  Real years (`2024`) and round numbers (`100`) are
  unaffected — only single leading zeros are stripped, never
  internal zeros.

### Notes

These two fixes interact: with the false-positive guarded out
and the numeric normalization in place, "Ghost Academy 1"
(which previously returned `found=False` because no
single-character directory `1` happened to exist in Mark's
tree) now matches its legitimate Bacon file via the Jaccard
tier (5 shared tokens / 8 in union = 0.625 ≥ 0.6).

### Tests

- **`tests/orchestrator/test_reingest.py:TestNameScore`** — three
  new unit cases covering the substring-guard rejection, the
  zero-padded-token alignment, and the preservation of multi-digit
  numbers / years.
- **`tests/orchestrator/test_reingest.py:TestFsCandidates`** — one
  new integration case that plants the actual Ghost Academy +
  Warhammer fixture and asserts the Bacon file appears as a
  candidate while the `collection/2/` Iron Company files do not.

---

## [2.9.0] — 2026-05-11

Format-priority dedup. When two torrents of the same book in
different formats arrive on MAM within minutes of each other (a
slow uploader split-uploading EPUB + AZW3), pre-v2.9.0 Seshat
grabbed both — by the time the first reached "Owned" the second
had already snatched. The Keleros "The Delves" + "The Duchy"
incident on 2026-05-09 is the canary; both books shipped through
the pipeline twice that night, charging two snatches per book.

v2.9.0 adds a new gate in the announce pipeline that consults a
per-media-type format priority list. Enabled formats (e.g. EPUB,
M4B by default) grab immediately. Disabled formats (AZW3, MOBI,
PDF, MP3 by default) get parked in a `pending_holds` table for
10 minutes (configurable) and re-evaluated by a scheduler tick —
released as a grab if no higher-priority sibling arrived, or
dropped if one did. A higher-priority enabled-format arrival
during the window synchronously preempts any held lower-priority
sibling.

Plus filter-page cleanup Mark requested mid-design: the legacy
`accept_audiobook_announces` boolean is removed, replaced by
"audiobooks" membership in the (renamed) Media Type filter as
the single source of truth.

### Added — Format-priority dedup gate

- **`app/orchestrator/format_dedup.py`** — pure decision module:
  - `normalize_dedup_key(title, author_blob)` wraps the existing
    cross-library `match_key()` so an announce's dedup key lines
    up exactly with the (author, title) keys the works/matcher
    uses against per-library `books` rows. First author only;
    full Unicode + article + format-paren normalization inherited.
  - `media_type_from_category()` maps "Ebooks - Fantasy" →
    "ebook", "Audiobooks - X" → "audiobook". Other prefixes
    (comics, etc.) return None and the gate falls through.
  - `evaluate_format_dedup()` — pure function over the announce,
    the user's `format_priority` setting, the hold window, and a
    pre-fetched `SiblingMatch` list. Returns "allow" / "skip" /
    "hold" with a machine-stable reason and an optional
    `preempt_hold_ids` tuple. No I/O.
  - `lookup_dedup_siblings()` — async helper that scans `grabs`
    in-flight states, `pending_holds` in 'pending' state, and
    per-library `books` tables for owned matches. Owned-side
    filtered by media type so an audiobook copy never blocks an
    ebook announce.

### Added — Hold-release scheduler

- **`app/orchestrator/hold_release.py`** — supervised loop
  (default tick = 60s). For each `pending_holds` row with
  `release_at <= now()`:
  - Filters the hold itself out of the sibling lookup.
  - Re-runs the dedup gate against the *current* state of the
    world (the hold window can change everything).
  - "skip" → mark dropped (a blocking sibling arrived).
  - "allow" or "hold" at release time → inject the grab with
    `apply_format_dedup=False` (we ARE the dedup decision
    releasing; the gate already ran) and mark released.
  - Registered as `state._hold_release_task` from main.py
    lifespan.

### Added — Wired dedup behavior in dispatcher

- **`app/orchestrator/dispatch.py`** — `_dispatch_with_decision`
  now consults the dedup gate immediately after the filter says
  allow:
  - **skip** path: UPDATE the audit row's decision to 'skip'
    with the dedup reason (format_dedup_higher_priority_inflight
    / format_dedup_owned_sibling). No grab created.
  - **hold** path: UPDATE the audit row's decision to 'hold',
    insert a `pending_holds` row with `release_at = now +
    format_dedup_hold_seconds`, drop any preempted lower-
    priority holds. No grab created.
  - **allow** path: drop any preempted held lower-priority
    siblings (the Delves preempt case), continue to the existing
    policy/rate-limit/grab path. `create_grab` now stamps
    `book_format` + `dedup_key` so subsequent dedup lookups
    find this grab via `idx_grabs_dedup_key`.
- New `apply_format_dedup: bool = True` parameter on
  `inject_grab` (and threaded through `_dispatch_with_decision`).
  IRC announces always pass True; manual-inject / send-to-
  pipeline callers pass False to bypass dedup ("Snatch anyway"
  semantics).
- New `filetype` kwarg on `inject_grab` so manual-inject paths
  can hint the gate.

### Added — Schema + settings (idempotent migrations)

- `announces.filetype TEXT` — persists the IRC `Filetype: ( xxx )`
  field at announce-decision time so dedup decisions are
  auditable retroactively. Pre-v2.9.0 announces have this NULL.
- `grabs.book_format TEXT, grabs.dedup_key TEXT` — tag each
  grab with its filetype hint + normalized dedup key.
- New `pending_holds` table — the 10-min hold queue. Indexed on
  (state, release_at) for the scheduler tick and on dedup_key
  for the higher-priority-sibling preemption path.
- `idx_grabs_dedup_key` lives in MIGRATIONS only — same legacy-
  DB safety pattern as v2.7.0's bundle_group_id index.
- New settings: `format_priority` (per-media-type list of
  `{fmt, enabled}` entries; default ebook = EPUB-only-enabled,
  audiobook = M4B-only-enabled) and
  `format_dedup_hold_seconds: 600`.

### Added — `GET /api/v1/announces` endpoint

- Serves the persisted SQLite `announces` audit table — distinct
  from the in-memory log buffer. Query params: `decision`
  (comma-separated subset of allow/skip/hold), `reason`
  (substring match), `q` (substring against torrent_name /
  author_blob / category), `limit` (default 200, cap 1000).
- Returns rows newest-first plus a `decision_counts` dict.
  `decision_counts` honors q+reason filters but ignores
  `decision` itself so the chip UX can show "what would be
  visible if I clicked this".

### Added — UI: Format Priority + Announce Log

- **Filters page**: new "Format Priority" section with one
  sub-card per media type (Ebook / Audiobook). Each row shows
  rank, format name, Enabled checkbox, up/down reorder arrows.
  Same UX on Desktop + Mobile. Help text exposes the hold
  window so the rule is self-documenting.
- **Logs page → Announces tab**: data source swapped from the
  in-memory log buffer to the new `/v1/announces` endpoint.
  Structured rows with timestamp, color-coded decision pill
  (Allow = green, Skip = red, Hold = warn), filetype,
  torrent name, author, and decision_reason. Six-column grid
  on Desktop; stacked card layout on Mobile.
- New decision filter chips (All / Allow / Skip / Hold) with
  live counts. Substring filter input narrows server-side on
  this tab (debounced 250ms).

### Removed — Legacy `accept_audiobook_announces` toggle

- The boolean is gone. "audiobooks" being in the Media Type
  filter (renamed from "Formats" — setting key kept as
  `allowed_formats` for backwards-compat) is now the single
  source of truth. `_build_filter_config` derives:
  audiobook is accepted when allowed_formats is empty
  (= "accept all") OR contains "audiobooks". When accepted,
  `allowed_audiobook_categories` merges into the runtime
  category set (same behavior as the old toggle).
- The "Audiobook Announces" UI section on FiltersPage and
  MobileFiltersPage is removed.
- **Migration**: `_apply_legacy_settings_migrations` runs on
  every `load_settings()`. Legacy `accept_audiobook_announces=
  True` + non-empty `allowed_formats` without "audiobooks" →
  adds it. Then drops the key. Idempotent and auto-saves on
  change so disk + memory stay in sync.

### Added — Override flag on inject endpoints

- New `override_format_dedup: bool` on three entry points:
  `POST /api/v1/grabs/inject`, `POST /api/v1/grabs/inject-batch`
  (batch-level), and `POST /discovery/send-to-pipeline` (per-
  batch). Passes `apply_format_dedup=not override` through to
  `inject_grab`. send-to-pipeline also seeds the filetype hint
  from `books.mam_formats` (first CSV entry) so the gate
  actually fires on that path. UI checkbox not surfaced yet
  (matches existing `buy_personal_fl` / `use_wedge_override`
  convention — API-only); external bookmarklet/curl callers
  can pass it now.

### Tests

- `tests/orchestrator/test_format_dedup.py` (+41) — pure-helper
  unit tests plus the four v2.9.0 scenarios + the Delves
  preempt + disabled hold-replacement + lookup integration
  against real SQLite DBs. Real Keleros Delves/Duchy fixtures
  baked in as canaries.
- `tests/orchestrator/test_format_dedup_integration.py` (+12)
  — exercises the wired dispatcher flow + manual-inject
  override + the hold_release scheduler tick.
- `tests/routers/test_announces.py` (+11) — endpoint coverage
  for decision_counts UX, multi-value filters, q + reason
  combinations, limit cap, empty-table behavior.
- `tests/test_config.py` (+6) — every branch of the
  `accept_audiobook_announces` migration, idempotency,
  no-op-when-already-migrated.
- `tests/test_legacy_db_upgrade.py` (+2) — v2.8.1 → v2.9.0
  upgrade lands the new columns + indexes + `pending_holds`
  table cleanly. Settings-defaults shape pinned.

Suite: **2135 passing, 7 skipped** (up from 2063 / 7 at v2.8.1).
72 new tests across 5 files.

### Operator notes

- On first v2.9.0 startup, the legacy-settings migration runs
  silently and rewrites `settings.json` if `accept_audiobook_announces`
  was set. Backup not strictly needed but the change is logged
  at INFO level so the audit trail is in `docker logs Seshat`.
- The dedup gate is dormant until `format_priority` is
  populated. Default seeded values (EPUB-only enabled for ebook,
  M4B-only for audiobook) make the gate active immediately for
  Mark's current setup. Empty `format_priority={}` disables it
  entirely and preserves pre-v2.9.0 behavior.
- `pending_holds` rows are never auto-purged. The table will
  accumulate one row per disabled-format announce. At ~10/day
  volume that's negligible, but if it ever matters a future
  patch can add a TTL cleaner.

---

## [2.8.1] — 2026-05-11

Same-day polish + bugfix release on v2.8.0. Three issues surfaced
during Mark's reingest UAT, all in `app/orchestrator/reingest.py`.

### Fixed

- **qBit candidates with missing files now drop out at probe time.**
  Pre-v2.8.1 `find_qbit_candidates` trusted `list_torrent_files` as
  the source of truth for which files exist — but qBit reports the
  torrent's metadata-declared file list even when the user has
  moved/deleted those files on disk. Mark's Test 8 scenario hit
  this: file moved out of the download folder while qBit kept the
  paused torrent → reingest auto-started → process_completion
  failed inside staging → user saw a "Reingest started" success
  toast with a "Pipeline Failed" ntfy notification arriving
  seconds later. v2.8.1 fstat's each book file under the (translated)
  save_path inside `find_qbit_candidates` and drops candidates with
  zero existing files. Partial existence keeps the candidate with
  its `book_files` narrowed to what's actually there.
- **`process_completion`'s `ok` status now propagates through
  `start_reingest`.** Pre-v2.8.1 `start_reingest` awaited
  `process_completion`, captured its return value, then silently
  discarded it — returning only `(grab_id, pipeline_run_id)`. The
  endpoint thus claimed success for ANY auto-start, regardless of
  whether the pipeline actually staged a review row. v2.8.1
  changes the return shape to `(grab_id, pipeline_run_id, ok)`,
  threads `ok` through both probe and start endpoints as a new
  optional `error` field on `ProbeResponse` / `StartResponse`, and
  has the BookSidebar UI show an error toast (not success) when
  `error` is set. The grab + pipeline_run rows still exist as the
  audit-trail record of the failed attempt.
- **Tighter dedup in `find_candidates` collapses qBit + fs entries
  for the same files on disk.** Pre-v2.8.1 the dedup keyed on raw
  `save_path`. qBit reports the PARENT of a multi-file torrent
  (e.g. `/downloads/[mam-complete]/[2025-09]`) with the torrent
  dir prefixed onto each `book_files` entry, while fs walks the
  torrent's OWN dir (e.g.
  `/downloads/[mam-complete]/[2025-09]/A Tangle of Time (Josiah Bancroft)`)
  with bare basenames. The two `save_path` strings differ, so the
  old dedup kept both. Mark hit this on his "A Tangle of Time"
  reingest — the picker showed two functionally identical entries.
  v2.8.1 keys dedup on the SET of resolved absolute file paths
  each candidate produces; same physical file → same candidate.
  When one qBit and one fs entry overlap, the qBit candidate
  survives (authoritative file list + hash).

### Added

- 3 v2.8.1 regression test classes in
  `tests/orchestrator/test_reingest.py` covering the three fixes
  end-to-end: missing-files-filtered, partial-existence-narrows,
  qbit-parent-plus-fs-subdir-collapses, missing-file-returns-ok-false.
- New optional `error: str | None` field on `ProbeResponse` and
  `StartResponse` in `app/discovery/routers/reingest.py`. The
  frontend reads it as the source of truth for mid-pipeline
  failures and renders the same red banner the "not found
  anywhere" path uses.

### Backwards compat

- The `Candidate` dataclass shape is unchanged — only consumers of
  `start_reingest`'s tuple need to unpack 3 values instead of 2.
  The only internal caller is `app/discovery/routers/reingest.py`,
  updated in lockstep. Test code that constructed candidates
  manually is unchanged.

Suite: **2063 passing, 7 skipped** (up from 2059 / 7 at v2.8.0).

---

## [2.8.0] — 2026-05-11

Reingest already-snatched torrents from disk. The standard "Send to
pipeline" button hides on books MAM flags as `my_snatched=true` —
Seshat refuses to re-download a torrent we already grabbed (the
snatch-safety rule per `feedback_mam_snatch_safety.md`). v2.8.0
adds a parallel "Reingest from disk" button that finds the existing
files (in qBit or under the configured download folder) and runs
them through enrichment + manual review WITHOUT touching MAM.

Targets the "grandfather'd" case Mark flagged on 2026-05-11: books
snatched months before Seshat existed (no qBit hash recorded
anywhere, just files in the download dir) that he wants to flow
through the pipeline now. Also handles the post-Seshat case where
qBit still has the torrent loaded — the qBit-side resolver returns
the authoritative save_path + file list straight from the client.

### Added — Reingest module

- **`app/orchestrator/reingest.py`** — pure-function candidate
  resolution + pipeline kickoff:
  - `find_qbit_candidates(dispatcher, mam_torrent_name)` walks
    `qbit.list_torrents()`, matches by name (exact > prefix >
    substring > Jaccard), and fetches each match's
    `list_torrent_files` for the authoritative file list. Path-
    aliasing applied via `translate_path()` so Unraid-style
    qBit/Seshat mount differences resolve correctly.
  - `find_fs_candidates(download_root, mam_torrent_name)` recursively
    walks the configured `qbit_download_path` (translated to
    Seshat's mount) up to 6 levels deep, looking for files OR
    directories whose name matches. Directory matches gather every
    book-format file inside via `rglob`; file matches wrap the
    single file. Dedup prefers the directory entry when both shapes
    land on the same path.
  - `find_candidates(dispatcher, mam_torrent_name)` runs both, qBit
    biased above fs by +100 in the score so a tie always picks the
    authoritative qBit candidate. Caps at 5 entries.
  - `start_reingest(db, dispatcher, ..., candidate)` synthesizes a
    `grabs` row with `is_reingest=1`, `state=STATE_DOWNLOADED`, and
    the qBit hash (if from qBit), creates a `pipeline_run`, then
    calls `process_completion` directly — bypassing the MAM
    .torrent fetch + qBit submit + rate limiter that a normal
    grab would charge. No snatch budget cost.

### Added — Schema migration (`grabs.is_reingest`)

- New `is_reingest INTEGER NOT NULL DEFAULT 0` column on the
  `grabs` table. Legacy and normal rows default to 0; reingest-
  created rows are 1. Lets future audit/queries distinguish the
  two paths without joining against `snatch_ledger`. ALTER TABLE
  migration is appended to the standard MIGRATIONS list and
  follows the v2.7.1 rule: column declared in BOTH the SCHEMA
  CREATE TABLE (for fresh DBs) and a MIGRATIONS ALTER TABLE (for
  legacy DBs). No SCHEMA-level CREATE INDEX touches it.

### Added — Discovery router endpoints

- **`POST /api/discovery/books/{book_id}/reingest/probe?slug=`** —
  validates the book is `mam_status='found'` + `mam_my_snatched=1`
  + not owned, resolves the canonical torrent name via MAM's
  `torrent_info.php` (NOT a snatch — metadata-only call, cached
  per the standard `get_torrent_info` TTL), then runs
  `find_candidates`. Returns `{found: bool, candidates: [...]}`
  for the picker, OR auto-starts the pipeline when exactly one
  candidate is found (single-result auto-pick = the natural
  default).
- **`POST /api/discovery/books/{book_id}/reingest/start?slug=`** —
  commits a user-chosen candidate from a previous probe. Re-
  validates the book row state (something else could have changed
  it between probe and start), then calls `start_reingest()` and
  returns `{ok, grab_id, pipeline_run_id}`.
- Both endpoints require `?slug=` per the multi-library safety
  rule (`feedback_seshat_multi_library_slug.md`).

### Added — BookSidebar UI

- New "♻ Reingest from disk" button appears on the same row as
  Re-scan / Send to pipeline, but only when:
  `pipelineReady && mam_status==='found' && mam_my_snatched && !owned`.
  Owned books skip the button entirely (no need to reingest
  something already in Calibre/ABS).
- Inline error banner shows "Could not find this snatch anywhere we
  looked: qBit (live torrent list), filesystem: /downloads/..." when
  the probe returns no candidates — clear list of paths checked
  so the user can investigate (missing drive, wrong
  `qbit_download_path` setting, etc.).
- Multi-candidate picker renders below the button row when probe
  returns more than one match: up to 5 entries with source tag
  (qbit / fs), display path, file count, and total size. Click
  an entry → POST to `/reingest/start` with that candidate; toast
  on success.

### Design decisions recorded

Per the v2.8.0 design conversation:
1. **No auto-fallback to re-snatch** when files aren't found
   anywhere. Hard-fail with a clear error message. Option (a) from
   the design — keeps the snatch-safety rule airtight and the UX
   unambiguous. Users who want to re-snatch can do it manually
   through MAM after confirming the original is truly gone.
2. **Search scope: just the configured `qbit_download_path`.** No
   user-configurable "additional library directories" setting in
   v2.8.0 — keep the surface small and fight only the actual
   reported case first.
3. **Skip the button for owned books.** Owned means the book is
   in Calibre/ABS already and the library sync handles it. No
   reason to reingest something already linked.
4. **Multi-match prompts.** Per design Q3 — these are one-offs the
   user typically wants to be informed about and pick. Top-5 cap.
5. **qBit preferred over fs.** Authoritative file list + hash from
   qBit; fs is the fallback for grandfather'd snatches without a
   qBit record. qBit candidates carry their hash forward into the
   `grabs.qbit_hash` column for future status reconciliation.

### Tests

- **`tests/orchestrator/test_reingest.py`** — 20 tests covering
  `_name_score` tiering, `find_fs_candidates` (single-file, multi-
  file directory, no-match, missing root, 5-cap), `find_qbit_candidates`
  (exact match, path translation, non-book filter, no-qbit
  fallback), `find_candidates` (qbit > fs ranking), and
  `start_reingest` end-to-end (grabs row carries `is_reingest=1`
  and `qbit_hash`; review queue row created via `process_completion`).
- **`tests/test_legacy_db_upgrade.py`** — gained a `is_reingest`
  assertion on the v2.6.1 → v2.8.0 upgrade path so future
  migration additions can't regress legacy-DB startup.

Suite: **2059 passing, 7 skipped** (up from 2039 / 7 at v2.7.1).
20 new tests in `test_reingest.py` + 1 additional assertion in
`test_legacy_db_upgrade.py`.

---

## [2.7.1] — 2026-05-11

Same-day hotfix on v2.7.0. The v2.7.0 SCHEMA block declared a new
`idx_review_queue_bundle_group` index on `book_review_queue
(bundle_group_id)`, but `bundle_group_id` is added by an ALTER TABLE
migration that runs AFTER the SCHEMA `executescript`. Fresh DBs
survived because the SCHEMA's `CREATE TABLE` added the column before
the index ran. Legacy v2.6.x DBs crashed because `CREATE TABLE IF
NOT EXISTS` no-op'd on the pre-existing table — leaving the index
to hit `sqlite3.OperationalError: no such column: bundle_group_id`
during lifespan startup.

### Fixed

- **v2.7.0 startup crash on legacy databases.** Moved the
  `idx_review_queue_bundle_group` index out of SCHEMA. The index is
  still created on the same connection via the existing MIGRATIONS
  entry (position 17), which runs AFTER the ALTER TABLE ADD COLUMN
  migrations (positions 12-16) that add the column. Fresh DBs reach
  the same end-state via the migration loop — `user_version` starts
  at 0 so every migration runs once on first boot.

### Added

- **Two regression tests in `tests/test_legacy_db_upgrade.py`:**
  - `test_init_db_survives_v26_to_v27_upgrade` simulates a real v2.6.1
    legacy database (with the pre-bundle `book_review_queue` shape +
    one pending row) and asserts `init_db()` completes without
    raising, the bundle columns land, the bundle-group index gets
    created, and the legacy row's `bundle_group_id` is backfilled to
    `grab-1` by the UPDATE migration.
  - `test_schema_indexes_reference_declared_columns` parses SCHEMA
    for CREATE INDEX statements and verifies each referenced column
    is declared in the same SCHEMA block's CREATE TABLE. Catches
    typo'd column names in SCHEMA indexes (a weaker but
    complementary guard to the runtime upgrade test above).

When adding a new column to SCHEMA + an ALTER TABLE migration in
the future, also add a new test to that file that pre-creates the
prior-version shape and asserts `init_db()` lands cleanly.

Suite: **2039 passing, 7 skipped** (up from 2037 / 7 at v2.7.0).

---

## [2.7.0] — 2026-05-11

Bundle/collection torrent support. When a single MAM grab contains
several distinct works (e.g. a 3-book series bundle or a 4-audiobook
collection), the pipeline now fans out into N review-queue entries —
one per detected work — instead of silently dropping every file
except the primary. The classifier preserves all prior single-book
and multi-format-same-book and multi-part-audiobook handling
unchanged via deterministic stem-dedupe and audiobook-parts pre-
checks that run before any "split into N groups" decision.

Default-ON via the new `bundle_detection_enabled` setting (kill
switch available for hypothetical classifier misfires in production).

### Added — Bundle classifier

- **`app/orchestrator/bundle_classifier.py`** — pure-function module
  with `classify(book_files) -> list[BookGroup]`. Five-signal pipeline,
  cheapest first:
  1. Single-file short-circuit → 1 group.
  2. Stem dedupe → all files share a stem when format-suffix stripped
     (the `book.epub + book.mobi + book.azw3` multi-format case) → 1
     group. No embedded reads needed.
  3. Audiobook-parts safety net → same extension across all files plus
     a part/disc/chapter/track token in filenames → 1 group. Catches
     the 26-part m4b rip that the v1.3 `_backfill_audio_companions` fix
     was added to preserve.
  4. Embedded metadata grouping → extract title+author per file (uses
     existing `app.metadata.extract`), group by
     `normalize_author_name(author) + "|" + normalize_title(title)`.
     Distinct groups = bundle.
  5. Filename-token fallback for files where extraction yielded an
     empty title (PDFs especially) — longest common prefix +
     Jaccard ≥ 0.85 against existing keyed groups.
- 13 unit tests in `tests/orchestrator/test_bundle_classifier.py`
  covering all six representative cases from the design doc:
  1-file, multi-format-same-book, 3-book ebook bundle, mixed novel
  +novella, 26-part audiobook, 4-distinct-audiobook bundle, plus
  disabled-flag and filename-fallback paths.

### Added — Schema migration (user_version 12)

Five new columns on `book_review_queue`:
- `bundle_group_id` — deterministic `f"grab-{grab_id}"`; legacy rows
  backfilled by an idempotent UPDATE in the same migration step.
- `bundle_index` (default 0) and `bundle_total` (default 1) — single-
  book grabs land at 0/1 after backfill, indistinguishable from pre-
  v2.7 rows.
- `library_slug` — reserved for future multi-library sink routing
  (NULL today; bundle children stamp their parent grab's slug when
  the discovery side learns to attribute per-library routing).
- `bundle_parent_grab_id` — set only on bundle children; carries the
  parent grab id through approval so future `acquisition_linkback`
  work can preserve the bundle's MAM URL on re-ingest instead of
  attaching a wrong-torrent standalone MAM record.
- New index `idx_review_queue_bundle_group` on `(bundle_group_id)`
  for the grouped-query path. `list_pending` ordering extended to
  `(created_at, bundle_group_id, bundle_index)` so siblings come
  back adjacent and in correct index order for the UI grouping
  wrapper.
- Tests in `tests/orchestrator/test_review_queue_bundle.py` cover
  default-shape inserts, bundle-children round-trip, adjacent-row
  ordering across mixed bundle+single-book queues, and the legacy-
  row backfill UPDATE.

### Added — Pipeline fan-out (`_prepare_book` → `list[_PreparedBook]`)

- `_prepare_book` now returns a list of one-per-group `_PreparedBook`
  objects (was: a single optional). `_PreparedBook` gains
  `group_files`, `bundle_index`, `bundle_total`,
  `bundle_parent_grab_id`, `library_slug` slots. Single-book grabs
  still produce a 1-element list with `bundle_total=1` — shape-
  indistinguishable from pre-v2.7.
- New internal helper `_prepare_group` extracts the per-group
  metadata + enrichment + epub-patch path. Each group runs
  independently with its own temp directory.
- `process_completion` loops over the returned preps; one
  `_stage_for_review` (or `_deliver_prepared` in legacy review-off
  mode) call per group. Per-event download-complete notifications
  fire once per torrent (not per group) to keep ntfy noise down;
  per-group review/delivery notifications fire per group as before.
- `_stage_for_review` writes bundle children to
  `grab-<id>/group-<i>/` subdirs (single-book grabs stay at
  `grab-<id>/` for backwards compatibility with in-flight queues).
  Sibling-copy logic now consumes the per-group `group_files` list
  instead of iterating the whole torrent staging dir, so a bundle's
  audiobook siblings don't cross-contaminate other groups.
- 5 integration tests in `tests/orchestrator/test_pipeline_bundle.py`
  build real EPUB fixtures + run the full pipeline path: 3-book
  bundle → 3 review rows, single-book → 1 row (no regression),
  multi-format-same-book → 1 row, each child delivers independently.

### Added — Per-child enrichment with inverted MAM priority

For bundle children only, the metadata-enrichment chain inverts:

1. **Seed** from the parent grab's MAM data (bundle-level — author
   mostly; bundle title and cover are wrong per-book and get
   overwritten downstream).
2. **Goodreads / Hardcover / Audible** in the existing priority,
   **overriding** any seed field they return data for (the inverse
   of the single-book rule). Goodreads-wins because the seed
   describes the bundle as a whole, not the individual child.
3. **No per-child MAM search.** `MetadataEnricher.enrich` gains a
   new `skip_mam: bool = False` kwarg; bundle children pass
   `skip_mam=True`. Reason: a standalone MAM listing for a child
   book is a *different torrent* than the bundle we grabbed.
   Recording its `mam_url` on the child row would falsely claim we
   have the book from that torrent, and any future code acting on
   the stored URL (re-snatch, link-back match, status reconciliation)
   would do the wrong thing. The bundle's MAM URL flows through
   unchanged via `bundle_parent_grab_id` for the audit trail.

Single-book grabs are unaffected — the existing MAM > Goodreads >
Hardcover > Audible chain runs exactly as in v2.6.

### Added — Review UX (desktop + mobile)

- `/api/v1/review` response gains `bundle_group_id`, `bundle_index`,
  `bundle_total`, `bundle_parent_grab_id` on every item. List
  ordering already keeps siblings adjacent (DB-level), so the UI
  groups them without extra logic.
- **Desktop ReviewPage**: contiguous bundle siblings render inside
  one dashed-border wrapper card with a "📚 Bundle — N books from
  one torrent" header. Each child keeps its full action set
  (approve / save edits / re-enrich / reject) and a "i/N" badge in
  the card chip strip. Approve-some-reject-others is the natural
  default since each child is its own review entry.
- **Mobile ReviewPage**: each child stays a swipeable card (the
  v2.1.0 mobile pattern is already independent-per-card) with a
  "Bundle i of N · grab #" indicator bar at the top of bundle-child
  cards. No mobile layout redesign needed.

### Added — Delivery + slug correctness

- `_backfill_audio_companions` (the v1.3 multi-file repair) now
  skips bundle children. Each group's review staging dir was
  populated with only that group's files at staging time, so
  re-running the qBit-side backfill would re-introduce other
  bundle siblings' audio files into this child's staging dir
  (qBit reports every file in the torrent, not just this group's).
- Each child delivers as an independent book — `deliver_reviewed`
  already operates per review entry, so bundle fan-out works for
  free at the delivery layer. Approving child 1 doesn't disturb
  the still-pending siblings; their `group-<i>/` dirs stay intact
  until they're individually approved or rejected.

### Added — Setting

- `bundle_detection_enabled` (default **True** from v2.7.0). Flip
  to False to fall back to the pre-v2.7 single-group-per-torrent
  behavior if a classifier misfire ever surfaces in production.
  The flag is structurally safe by default because stem-dedupe and
  audiobook-parts pre-checks both run before any "split into N
  groups" decision can fire.

### Documented

- New reference memory: MAM rule that each format category (ebook,
  audiobook, comic, podcast, newsletter, etc.) MUST be uploaded as
  a separate torrent. Confirmed during the v2.7.0 design session
  and used to scope out the "mixed audiobook + ebook in one
  torrent" code path (which is impossible by MAM upload rules).

### Architecture notes

- **`pipeline_runs` stays 1:1 with grab.** Fan-out lives only in
  the review table. Keeps `auto_train`, `review_timeout`, and
  `economy_audit` join logic untouched. The first group's metadata
  advances the pipeline-run state (`PIPE_METADATA_DONE`); the
  last group transitions the run to `PIPE_AWAITING_REVIEW` and
  flips the grab to `STATE_PROCESSING`. External watchers see one
  state transition per torrent, not per child.
- **Backwards compat.** Legacy queue rows (pre-migration) drain
  through the existing single-book code paths because their
  `bundle_total=1` makes them indistinguishable to delivery code.
  Pre-v2.7 review rows in flight at upgrade-time keep working.

Suite: **2037 passing, 7 skipped** (up from 2016 / 7 at v2.6.1).
21 new tests across `test_bundle_classifier.py` (13),
`test_review_queue_bundle.py` (4), and `test_pipeline_bundle.py` (4).

---

## [2.6.1] — 2026-05-11

Same-day patch closing the CodeQL alert that surfaced from v2.6.0's
`_strip_subtitle` regex. The v2.6.0 fix for alert #35 dropped the
leading `\s*` from `\s*\([^)]*\)\s*$`, but alert #36 was filed
against a different worst-case input on the same line — many `(`
characters with no closing `)` still backtrack O(n²) through
`[^)]*`. Python's `re` doesn't support atomic groups, so the
permanent fix is to drop the regex entirely.

### Fixed

- **CodeQL #36 — py/polynomial-redos** (sibling of #35, same line).
  `_strip_subtitle`'s trailing-parenthetical fallback now uses a
  linear `rstrip` / `endswith` / `rfind` scan instead of a regex.
  Semantics preserved on every existing test case; no Python
  ReDoS-prone construct in this path. Two new regression tests
  (`test_many_open_parens_no_redos`,
  `test_many_spaces_then_paren_no_redos`) pin the new behavior.

Suite: **2016 passing, 7 skipped** (up from 2014 / 7 at v2.6.0).

---

## [2.6.0] — 2026-05-11

Incremental library sync, non-blocking startup, Dismiss tentative
action. Most Seshat container restarts no longer trigger a full
library re-scan — Calibre and Audiobookshelf both gain
`last_modified`/`updatedAt`-filtered incremental modes driven by a
new per-library `sync_state` shape (last_mtime, last_sync_ts,
last_full_sync_ts). For Mark's 2862-book Calibre + 132-item ABS
library this turns a 30-60s startup wall into a sub-second one on
day-to-day restarts. A 7-day safety net forces periodic full syncs;
a recorded failure resets the state so a partial incremental can't
leave drift in place.

The startup sync itself moved off the lifespan's critical path — it
runs as a supervised background task while FastAPI accepts requests
immediately, and the frontend renders a sticky progress banner (or
full-screen splash on first-ever boot) gated by a new
`startup_complete` field on `/scan-status`.

Two scheduler/display bugs adjacent to the sync work got bundled in:
the per-library interval gate's processing-time drift was making
configured 120m cadence behave as ~240m effective, and the "Last
Sync N min ago" indicator was counting skipped ticks as completions.

Dismiss action lands on the tentative torrent queue. Useful when an
uploader posts duplicate format variants (MP3 + M4B) and the user
wants one specific torrent without making a call about the author,
or when the offered book doesn't interest the user but the author's
other work might. Distinct from Reject (which queues the author for
weekly review) and Approve (which trains to allowed) — Dismiss
touches zero author tables.

CodeQL alert #35 (py/polynomial-redos in `_strip_subtitle`) closed
with a one-character regex anchor change.

### Added — Sync state machinery

- **`app/discovery/sync_state.py`** — per-library sync state with
  five helpers (`migrate_settings`, `get_state`, `record_completion`,
  `record_mtime_unchanged`, `record_failure`) and `resolve_threshold`
  for the incremental vs full decision. Four mode constants:
  `MODE_FULL_FIRST`, `MODE_FULL_WEEKLY`, `MODE_FULL_RECOVERY`,
  `MODE_INCREMENTAL`.
- **Migration** folds legacy `library_mtimes` into `library_sync_state`
  on startup (idempotent). Legacy `library_mtimes` continues to be
  written as a mirror for one release so a downgrade still works.
- **`record_failure`** zeros `last_sync_ts` on incremental crashes so
  the next sync escalates to full — prevents drift from a
  half-applied incremental.

### Added — Calibre incremental sync

- **`_read_calibre_db(last_modified_threshold=...)`** filters at the
  SQLite level via `julianday()` on Calibre's `books.last_modified`.
  `julianday()` normalizes Calibre's inconsistent timestamp formats
  (`2026-05-11 15:13:27.887295+00:00` vs `2026-05-10 19:57:29.995709`
  — both shapes observed in Mark's live library).
- **`_read_calibre_ids`** — full-library ID-only read for delete
  detection. Incremental's `last_modified > threshold` filter misses
  deletes; this cheap read diffs against the discovery DB's known
  `calibre_id` set.
- **`_read_calibre_series_authors`** — shallow full read of
  (book_id, authors\[id\], series\[id\]) for Pass 2's multi-author
  series detection. Required because incremental's filtered book set
  doesn't include every author of a shared Calibre series — without
  this Halo books contributed by different authors would be
  misclassified per-author.
- **`sync_calibre` orchestration** — resolves mode, dual-reads in
  incremental mode, pre-loads `author_map` from DB so authors only
  on un-modified books are still resolvable, returns
  `{"mode": "full" | "incremental", ...}`.

### Added — ABS incremental sync

- **`sync_audiobookshelf`** mirrors the Calibre flow with
  client-side `updatedAt` filtering of the (small) ABS item set.
  Pass 1+2 still iterate the full set (author / per-author series
  upserts are cheap and idempotent); only Pass 3's book-row writes
  are filtered.
- **Pass 4 prune uses raw item-id set** instead of flattened books.
  Closes a latent bug where items that fail `_flatten_item` (missing
  title or author) used to vanish from the discovery DB on the next
  sync even though they still existed in ABS.

### Added — ABS edit detection (`newest_item_updatedAt`)

- ABS `get_mtime` composite extended to
  `f"{lastUpdate}:{itemCount}:{newestUpdatedAt}"`. The third
  component catches metadata + cover edits that don't bump
  `library.lastUpdate` (covers and metadata live in ABS's
  `/metadata` folder, which the library watcher doesn't see) and
  don't change `itemCount`. Without this signal Seshat's scheduled
  sync silently missed every ABS metadata edit. Verified 2026-05-11
  against Mark's live library: tag edit, cover replace, external
  `calibredb set_metadata`, and KFX→EPUB conversion all bump the
  relevant timestamps.
- Single round trip via `list_items(limit=1, sort="updatedAt",
  desc=True)` returns both `total` and the newest item's
  `updatedAt`.
- Self-heals across the v2.5 → v2.6 composite-shape boundary.

### Added — Non-blocking startup + progress UI

- **`_run_startup_sync` supervised task** in `app/main.py`. Lifespan
  spawns the task and returns immediately; FastAPI accepts requests
  while sync runs in the background. `state._startup_sync_complete`
  flips True after the first pass through every library.
- **`/api/discovery/scan-status`** gains `startup_complete: bool` and
  `first_boot: bool`. `first_boot` is derived from the persisted
  `library_sync_state` (any library with `last_full_sync_ts > 0`
  means we've synced before), so the splash gate stays correct
  across upgrade-restarts even though `_library_sync_progress` is
  in-memory.
- **`LibrarySyncBanner` component** — full-screen splash on
  first-ever boot, sticky banner under the navbar on every
  subsequent sync. Polls `/scan-status` every 3s, auto-stops when
  nothing's running and startup is complete.

### Added — Dismiss tentative action

- **New `dismissed` status** in `tentative_storage` (distinct from
  `rejected` for clean audit).
- **`POST /api/v1/tentative/{id}/dismiss`** and
  **`POST /api/v1/tentative/bulk/dismiss`**. Drop a tentative
  torrent from the queue without touching any author table.
- **Dismiss buttons** on both desktop `TentativePage` and
  `MobileTentativePage`, per-row plus bulk-selected variants.

### Added — Cross-list cleanup on approve/reject

- **Approve** now removes the author from `authors_tentative_review`
  after training to allowed. Previously an author who had been
  rejected then later approved sat in BOTH lists, causing the
  weekly review UI to keep prompting about a settled author.
- **Reject** now skips adding to `authors_tentative_review` if the
  author is already on `authors_allowed`. A one-off "no thanks to
  this specific book" shouldn't drag a previously-validated author
  back into review.

### Added — mtime-skip backfill

- New `record_mtime_unchanged` helper in `sync_state.py`. When an
  mtime check confirms the library is current and the timestamps
  are still zero from the migration, backfill both to NOW so the
  next real sync goes incremental rather than falling back to
  `MODE_FULL_FIRST`. Conservative — never overwrites existing
  non-zero timestamps.

### Changed — Scheduler / display

- **Interval-gate drift slack** (5 min) in
  `app/discovery/scheduled_jobs.py`. The per-library interval gate
  used to compare `(now - last_at) < effective_interval` strictly,
  but `last_at` stamps at *completion* (which lags the APScheduler
  tick by processing time). Configured 120m therefore behaved as
  ~240m effective when both intervals matched and a real sync ran
  on the prior tick. Adding 5-min slack lets the gate honor the
  configured cadence without weakening longer per-library
  overrides.
- **`Last Sync` indicator** now tracks real completions only.
  Skipped ticks bump a separate `last_check_at` field; the
  Command Center can show "scheduler alive, last checked X ago"
  without lying about when a sync actually ran. Previously every
  skip stamped `completed_at = now`, producing "Last Sync 3 min
  ago" displays when the last real sync was 40 hours ago.

### Fixed — Security

- **CodeQL #35 — py/polynomial-redos** in
  `app/discovery/sources/mam.py`'s `_strip_subtitle`. The trailing-
  parenthetical regex `\s*\([^)]*\)\s*$` overlapped two greedy
  quantifiers (whitespace can be matched by either the leading
  `\s*` or `[^)]*`), giving the engine a polynomial-time
  backtracking path on pathological input. Dropped the leading
  `\s*`; output unchanged on every realistic input because the
  follow-up `.strip()` already trimmed the gap.

### Tests

39+ new across:

- `tests/discovery/test_sync_state.py` (20 tests — 17 helpers + 3
  backfill)
- `tests/discovery/test_calibre_sync_incremental.py` (8 tests)
- `tests/discovery/test_abs_sync_incremental.py` (5 tests)
- `tests/routers/test_tentative_endpoints.py` (8 tests — dismiss +
  cross-list cleanup)
- `tests/library_apps/test_audiobookshelf.py` (1 new + 2 updated
  for the 3-field composite)

Suite: **2014 passing, 7 skipped.** Up from 1996 / 7 at v2.5.0.

---

## [2.5.0] — 2026-05-11

MAM URL confidence — v2.4 follow-up arc. Across two days, ~458
combined owned + unowned phantom Possibles dropped to **12 genuine
Possibles** (~97% reduction) — UAT-pass declared on the remaining
12 as "same author wrong book" hedges that need manual review.

The arc landed Part D filename verification as the new primary
verification signal, plus seven targeted scoring refinements, two
new promote paths, the `_strip_subtitle` parenthetical fallback,
the manual-scan-callers `series_name` plumbing fix, CWA push
hardening (full-form replacement + post-POST verification),
ebook-format-priority for primary-file selection on the ingest
side, the bulk-scan async-task UX rewrite, and a security pass
closing 9 CodeQL + 3 Dependabot alerts.

### Added — Verification signals

- **Part D — scoped filename verification** as the primary
  verification signal. Wires MAM's inline `@(title,filenames) X
  @author Y` operator into production scoring ahead of cover and
  description verification (cheaper: 1 search vs N per-candidate
  fetches; more reliable than description on prose-only bundle
  layouts). Short-circuits cover + description fetches when
  filename verifies. New `_FILENAME_VERIFICATION_ENABLED` gate
  defaults True. Period-stripping in the `@author` segment
  (MAM's author index drops periods after initials).
- **Fix E — series-bundle promote**. New
  `would_promote_via_series_bundle_match` decision in
  `_try_evaluate`: when candidate is a bundle + author_matched +
  search's `series_name` appears (case-insensitive substring)
  in the bundle's mam_title, promote. Existing
  `volume_range_mismatch` short-circuit protects against
  search-Bk7-vs-bundle-Books-1-3 cases.
- **Fix F — strong-text-anchor promote**. New
  `would_promote_via_strong_text_anchor`: when ts >= 0.95 +
  author_matched + conf >= 0.65 + not bundle-cap-blocked + not
  volume_likely_mismatch, promote at the lower threshold. Catches
  exact-title-match singletons sitting just below the regular
  0.70 promote threshold.
- **Vol-range +0.10 boost**. When candidate's
  `_extract_volume_range` covers the searched vol, boost conf by
  +0.10. Resolves the "Domestic Decay 2 - 5" canary regression
  where the wrong-vol Bk1 sibling was outscoring the correct
  range bundle.
- **No-positive-signal demote** in `_try_evaluate` — the
  `best_possible` assignment site now refuses pure text-overlap
  candidates that have neither author confirmation nor any
  verification signal. Catches subtitle-template false positives
  (~20 from owned UAT) and Bk1-for-BkN false positives.
- **ts<0.10 demote** for "right author totally wrong work"
  pattern (DuBoff "Grand Theft Planet" surfacing "Fractured
  Empire" from the same author's different series).
- **`_strip_subtitle` parenthetical fallback** for Calibre's
  standard `<Book Title> (<Series Name> #<N>)` format. When no
  `:` / ` - ` / `|` delimiter exists, strips a trailing `(...)`
  segment so pass 4 (short title) fires properly. UAT canary:
  Tower Mage 2 (The Nine Magics #2).

### Changed — Author matching (`_author_match`)

Three tightenings to address overshoot in pre-arc author matching:

- **Per-author subset** instead of UNION-token-overlap. UAT
  canary: 59-author "Fantasy-Scifi Authors Starting With T"
  mega-collection was matching "Pierce Scott" via "Tamora Pierce"
  (single shared "pierce" token). Per-author subset means BOTH
  search tokens must overlap with ONE individual MAM author.
- **Empty `mam_authors` → False** (was True permissively).
  Generic mega-collections like "Sci-Fi Master Collection M-Z"
  with empty author_info no longer pass author_match. Real
  Cohort C cases (legitimate book missing metadata) still have
  cover-pHash + description-mention rescue paths upstream.
- **Reverse-subset surname guard**. Reverse match (m_tok ⊆
  cal_tok) requires the cal-side surname (last meaningful token)
  to be in m_tok. Catches first-name-vs-surname collisions on
  common names (UAT canary: "M J Scott" matching "Scott
  Reintgen"). Trade-off: loses first-name-only MAM uploads
  ("Brandon" matching "Brandon Sanderson") — accepted as rare.

### Changed — Description parser (3 fixes for structured-list bundles)

- Single-word title gate relaxed from `< 2 tokens` to `< 2 tokens
  AND < 5 chars` (lets "Chainfire" through).
- `_DESC_LEADING_RX` adds `\d+\s*[-–—]\s+` for `09 - Title`
  numbering convention.
- `_DESC_BLOCK_RX` adds `(?:&nbsp;|[ \t])\*(?:&nbsp;|[ \t])` for
  inline asterisk-bullet lists.

### Fixed — Production plumbing bugs surfaced by UAT

- **Manual scan callers were dropping `series_name`** when calling
  `mam_check_book`. Only the cascade scheduled scan passed it
  correctly; bulk-by-ids, single-book Re-scan, single-author scan,
  and authors-page bulk all silently dropped it — making Fix E's
  `bool(series_name)` gate fail immediately. UAT round 4 caught
  this when 17 of 27 expected Fix E promotes never fired. Fix:
  add `LEFT JOIN series s ON b.series_id = s.id` + select
  `s.name AS series_name` + pass through to `mam_check_book` in
  all four call sites (`books.py`, `mam.py`, `authors.py`).
- **Hyphen-digit normalization in scoped query** —
  `_scoped_filename_search` replaces `\w-\d` with `\w \d` before
  constructing the query. UAT canary: "The Redemption of
  Maribeth-5" returned 0 from MAM scoped because `Maribeth-5`
  tokenizes as a single token; `Maribeth 5` returns the bundle.
- **`series_name` not tagged user-edited** when set via the
  BookSidebar edit form. `TRACKED_FIELDS` omitted it AND the
  dedicated series_name update branch never added to
  `edited_now`. Combined with calibre_sync writing `series_id`
  through unconditionally as "structural", every local series
  edit got clobbered on next sync. Fix in
  `app/discovery/routers/books.py:update_book`.
- **CWA push silent failure**. Two compounding bugs: (1) CWA's
  `/admin/book/<id>` endpoint expects a COMPLETE form
  replacement, not a partial update — Seshat sent just the
  changed field + csrf, CWA silently re-rendered the form with
  validation errors and returned 200. (2) `CWAClient.push` only
  checked HTTP status, treated 200-with-validation-errors as
  success. Fix: `_parse_cwa_edit_form` (BeautifulSoup) scrapes
  the full form, merges changes on top, POSTs complete payload
  with `detail_view=on`, then re-fetches and verifies each
  pushed field actually persisted.

### Added — Pipeline UX

- **Bulk scan async-task pattern**. `/books/scan-mam` refactored
  from synchronous (blocked HTTP request for entire scan) to
  async-task pattern matching `/authors/scan-mam`. Returns
  `{status: "started", total: N}` immediately, registers in
  `state._mam_scan_progress` so Command Center sees live
  progress. `/scan/cancel` now works against bulk scans too.
- **DiscBooksPage scan progress banner**. New page-level effect
  polls `/discovery/mam/scan/status` every 3s; renders an
  accent-bordered banner with X/Y progress + current_book +
  found/possible/not_found counts when a scan is running. Lingers
  4s after completion as a summary then auto-clears.
- **Ebook format priority for primary-file selection** on the
  ingest side. UAT canary: "Methodology of Secrets" torrent had
  EPUB + PDF, file_copier picked PDF (largest-first) despite
  `mam_format_priority` listing epub first. Mirrored existing
  audiobook-priority plumbing for ebooks. New
  `_apply_ebook_priority` + `_EBOOK_EXTENSIONS` in `file_copier`,
  `ebook_format_priority` field on `DispatcherDeps`, threaded
  through pipeline + budget_watcher, sourced from existing
  `mam_format_priority` setting at startup.

### Added — Diagnostic surface

- **`?test_scoped=true` on `/api/v1/mam/debug-match`** runs five
  scoped-operator probe variants (with/without periods,
  with/without `@author`, broad vs narrow `srchIn`). Used
  throughout this arc's UAT.
- **`raw_error` surfacing** in debug-match trace. When MAM
  returns `{"error": "..."}` we now surface the message —
  previously couldn't distinguish "MAM rejected query" from
  "no matches".

### Security

- **3 Dependabot npm transitives bumped**: `fast-uri` 3.1.0 →
  3.1.2, `@babel/plugin-transform-modules-systemjs` 7.29.0 →
  7.29.4. Both dev-only build-pipeline deps.
- **6 CodeQL ReDoS warnings fixed** by bounding regex
  quantifiers (`\s+` → `\s{1,8}`, `\d+` → `\d{1,4}`, etc.) in
  scoring + author-parsing helpers.
- **2 CodeQL URL-substring sanitization warnings fixed** via new
  `_is_mam_url` helper using `urllib.parse.urlparse(url).hostname`
  instead of substring match. `_do_get` itself host-locked to
  MAM as defense-in-depth.
- 1 CodeQL SSRF auto-resolved by the `_do_get` lock-down. 2
  CodeQL SSRF dismissed (false-positive runtime sanitizer
  + by-design admin-gated debug endpoint).

### UAT outcomes

- **Owned-Possible journey**: 71 → 50 → 8 → **0** over the arc.
- **Unowned-Possible journey**: ~387 → 56 → 27 → 13 → **12**.
- The 12 remaining are all genuine "same author wrong book"
  hedges that need manual review — exactly what Possible status
  is for. UAT-pass declared.

### Suite

1972 passing, 7 skipped (pre-existing).

---

## [2.4.0] — 2026-05-09

Part C — cover-image perceptual-hash MAM URL verification, end-to-end.
Mark's hand-curated 29-book UAT dataset confirmed 29/29 expected
outcomes after a multi-round iteration. First minor-version bump
under the strict-SemVer policy that started at v2.4.0.

### Added — Cover-image MAM URL verification

- `app/mam/cover_hash.py` — pHash via the `imagehash` library (DCT-based,
  robust to JPEG quality / scale / mild color shift). Validated 2026-05-09
  against 16 image pairs from Mark's library: right-Possible covers
  cluster at distance 0-6, wrong-match covers at 28-36, with a
  22-bit empty band between. Pure helpers (`hash_image_bytes`,
  `hash_image_file`, `hamming_distance`), persistent cache
  (`get/store_cover_hash` against new global `mam_cover_hashes` table,
  30-day TTL), top-level `fetch_and_hash_mam_cover` that goes through
  the cookie-aware `_do_get`.
- `app/discovery/cover_phash.py` — per-library bridge:
  `backfill_cover_phashes_from_paths` (eager backfill of Calibre
  `cover_path`-based covers, runs as background task on startup so
  lifespan isn't blocked) + `ensure_cover_phash` (lazy compute for
  source-discovered books with `cover_url`). New `books.cover_phash`
  column auto-populated.
- `_annotate_candidate_covers` in `sources/mam.py` — top-N=10 non-bundle
  candidates by text confidence; fetches cover, computes Hamming
  distance, assigns signal (promote ≤10, demote ≥22, neutral, no_data,
  skipped_bundle, not_evaluated).
- `_try_evaluate` integration: cover-promoter winner replaces text
  winner regardless of conf; promoter-anchored demotion filters
  competing candidates; aggressive-demotion mode (default ON) filters
  even without promoter — gated by new
  `mam_aggressive_cover_demotion` setting (Settings → Discovery → MAM).

### Added — Cohort C rescue

- `_alternate_title_forms` + `_alternate_author_forms` — variant
  generators for passes 6+ in `check_book`. Fixes three MAM
  tokenization mismatches surfaced by UAT:
  - Trailing zero-padded volumes ("Right of Retribution 02" vs "2")
  - Multi-initial authors ("JJ Cross" vs "J J Cross")
  - Typographic apostrophe ("Warhawk’s" vs "Warhawk's")
- `_extract_volume_range` + range-mismatch short-circuit in
  `score_match_with_breakdown` — bundle "1-4" excludes vol-7 search.
- `_extract_volume` extended with Roman numeral pattern (II-XX, skips
  bare "I") + trailing arabic + strip-subtitle fallback. Range gate
  prevents false-extraction from bundles like "Domestic Decay 2 - 5".
- `_description_mentions_title_loose` + B3a single-torrent description
  verification — rescue Cohort C cases where the title appears in
  description (fetched via documented Search API per TOS).
- B3b volume disambiguation in `_try_evaluate`: orig has no vol but
  cand does → -0.20 penalty; orig vol >= 2 + cand has none → cap at 0.65.
- Confidence as secondary tiebreak in `_pick_best_result` after
  `match_pct` — ensures B3b volume-penalized siblings lose to
  the no-vol-marker right candidate (MMM Bk1 over MMM 6).
- Cohort C exemption (`_exempt_from_aggressive_demote`): candidates
  with ts ≥ 0.95 AND author_matched are exempt from aggressive
  filtering — protects MMM-class right matches.

### Changed

- `should_promote` in `_try_evaluate`: text-promote now requires
  `author_matched=True`. Blocks pass-5 cross-author false positives
  (UAT canary: Marvel "Infinity" by Hickman et al. would have
  text-promoted against Tabitha Lord's "Infinity").
- `_clean_title` + `_clean_title_loose` preserve apostrophes
  (ASCII + typographic). MAM's index tokenizes around apostrophes;
  stripping them turned "Warhawk's" into "Warhawks" matching
  nothing.
- Variant pass list (`_build_variant_pass_list`) pairs alt-authors
  with all interesting title shapes (full + short + core +
  sub_right), not just full title — fixes Veil where the right tid
  only surfaces with `(JJ Cross, "The Veil")`.
- `debug_check_book` brought to parity with `_try_evaluate`:
  variant passes 6+, vol disambiguation, author-match check,
  aggressive demotion filtering, Cohort C exemption all now
  visible in the trace.

### Removed (TOS cleanup)

- mbsc browser-session cookie scraping + filelist verification path.
  MAM staff (Perstephonie, 2026-05-09) confirmed mbsc-tier scraping
  isn't on Section 1.7's approved automation list. Description-based
  bundle verification (TOS-allowed via documented Search API) is
  retained as the sole bundle-content signal. Restoration design
  notes preserved if MAM ever exposes filelist via the documented API.

### Schema

- `app/database.py`: new `mam_cover_hashes` global-DB table
  (torrent_id PK, phash, fetched_at, width, height, bytes).
  Cross-library cache reuse — same torrent evaluated across
  ebook + audiobook libraries shares one fetch.
- `app/discovery/database.py`: new `books.cover_phash TEXT` column
  on per-library books table.

### Dependencies

- Pinned `Pillow==12.2.0` and `ImageHash==4.3.2` (Pillow was
  transitively pulled; pinning makes the runtime contract
  explicit).

### Tests

96 new across 6 test files. Suite: 1909 passing, 7 skipped.

### UAT — Mark's hand-curated 29-book dataset

Final result 29/29 ✓ across 14 PROMOTE_FOUND, 3 PROMOTE_VARIANT,
12 STAY_OR_NOTFOUND. One residual Cohort C case (Raw Bk1, ts=0
because of series-strip + empty-residue path) needs manual Approve
to lock in Found — accepted cost. Bonus discovery: Incarceron
auto-finds an alt MAM upload (tid 49394) whose cover matches
Mark's Calibre cover, producing a strictly-better URL than the
previously-stored Cohort C tid 174640.

---

## [2.3.7.1] — 2026-05-08

Feature-completing fast-follow on v2.3.7. Adds the third leg of the
Skip MAM trifecta: per-author-detail multi-select bulk verb. v2.3.7
shipped per-book (BookSidebar Skip button) and per-author-all
(Authors page bulk verb hits all of an author's books across every
library). v2.3.7.1 fills the gap — selecting specific books from
one author's detail page and bulk-N/A-ing only the selected subset.

### New

- `POST /api/discovery/books/bulk-skip-mam` — accepts
  `{ book_ids: [...] }` + `?slug=` (same routing contract as
  `/books/bulk-hide` / `/books/bulk-dismiss` / `/books/bulk-delete`).
  Sets `mam_status='not_applicable'` and clears `mam_url` /
  `mam_torrent_id` / `mam_formats` so a stale prior match doesn't
  linger on a row the user just declared irrelevant.
- "Skip MAM" verb in the multi-select bar on
  `DiscAuthorDetailPage` and `MobileAuthorDetailPage`. Sits next to
  Hide / Dismiss / Delete. Slug-aware via the existing
  `slugQuery(a?.active_library_slug)` plumbing — no cross-library
  id-collision risk.

### Tests

- 2 new tests in `tests/discovery/test_skip_mam.py` covering the
  bulk endpoint's flip behavior + empty-payload rejection.

Suite: 1593 passing.

---

## [2.3.7] — 2026-05-08

Three coordinated changes that came out of UAT after v2.3.6.1: a new
"Skip MAM" status for books that should never be scanned, full
multi-library coverage on the remaining MAM scan paths, and an
acquisition link-back that records the originating MAM URL on books
that came in through the IRC pipeline.

### Discovery — `mam_status='not_applicable'` (Skip MAM)

A new fifth MAM status value, set explicitly by the user (no scanner
ever produces it). Books in this state are excluded from every MAM
scan path — the v2.3.6 `_NEEDS_SCAN_*` predicates already match
`IS NULL OR IN ('possible','not_found')`, so `not_applicable` is
auto-skipped just like `found` is. Use case: free-on-the-web authors
(Snekguy etc.) whose works almost never end up on MAM, where v2.3.6's
widened rescan loop would otherwise keep retrying every tick on
known-impossible matches.

The BookSidebar's MAM decision row now adapts to the current status:

| status | Approve | Remove | Skip |
|---|---|---|---|
| NULL/unscanned | — | — | ✓ |
| possible | ✓ | ✓ | ✓ |
| found | — | ✓ | ✓ |
| not_found | — | — | ✓ |
| not_applicable | — | ✓ | — |

Skip writes `mam_status='not_applicable'` and clears the URL via a
new allowlisted branch in `update_book` (`{"mam_status":
"not_applicable"}` is the only direct status write the endpoint
accepts; every other transition still flows through `mam_url`).
Remove on a `not_applicable` row clears back to `not_found` so the
book becomes rescannable on the next tick. The Authors page
multi-select gains a "Skip MAM" bulk verb that hits a new
`POST /api/discovery/authors/skip-mam` endpoint with
`content_type='all'` so a single click covers every library —
Snekguy's 60+ books across both ebook and audiobook libraries flip
in one action.

`get_mam_stats` adds a `total_skipped` counter and excludes
`not_applicable` from `total_scanned` (the user set those, not the
scanner). The `mam_status` filter dropdowns on the Library /
Missing / Upcoming pages gain an "N/A" option, and the BookSidebar
status badge renders a neutral "N/A" pill for skipped rows.

### Discovery — full multi-library coverage on MAM scan paths

Pre-v2.3.7 only the manual `/api/mam/scan` endpoint and the
scheduled scheduler tick swept all libraries; the remaining four
paths quietly ran against the active library only. Multi-library
deployments (Mark's Calibre + ABS setup) had to flip active between
ticks to get parity. UAT 2026-05-08: a "Scan MAM" on a cross-
library author selection silently missed half the matching books.

Fixed in v2.3.7:

- `POST /api/discovery/authors/scan-mam` (bulk authors) — now
  accepts `content_type` and `author_names` matching the
  `/authors/scan-sources` + `/authors/clear-scan-data` contract.
  Iterates every matching library, resolves author names locally
  per-library to dodge the cross-library ID-collision class
  (`feedback_seshat_multi_library_slug.md`), routes per-library MAM
  category by each lib's own `content_type`. Snapshot per library
  taken upfront so concurrent author scans don't inflate the queue.
- `POST /api/mam/full-scan` — iterates every discovered library
  sequentially. Each library has its own `mam_scan_log` row
  (per-library DB) so snapshotting + resume work per-library;
  `_full_scan_loop` runs the existing batched scan-with-pause flow
  once per library before moving on. Pre-v2.3.7 a full scan only
  touched the active library — Calibre/ABS users had to run two
  full scans manually to get parity.
- `POST /api/discovery/books/scan-mam` — now accepts `?slug=` query
  param and prefers the requested library's `content_type` over the
  active library when set. The BookSidebar Re-scan button now
  passes `slug` so a sidebar opened from a cross-library view
  routes correctly (matches the v2.3.4.4 multi-library safety fix
  for `update_book`).
- `POST /api/mam/scan-book/{id}` (legacy) — same `?slug=` + per-lib
  content_type routing as above. The BookSidebar uses
  `/books/scan-mam` not this endpoint, but third-party integrations
  may still call it directly.

### Discovery — acquisition link-back (audiobook + ebook)

Audiobooks that came in through the IRC pipeline (tentative or
auto-approve) consistently ended up with `mam_status='not_found'`
on Mark's UAT — even though we acquired them from MAM with the
exact `mam_torrent_id` recorded in the `grabs` table. Root cause:
the discovery ABS/Calibre sync had no awareness of the global
grabs table, so it created a fresh book row with `mam_status=NULL`,
and the next MAM scan tick ran a fuzzy `check_book` title+author
search whose match commonly graded `not_found` or low-confidence
`possible` for IRC-acquired audiobooks (the cleaned ABS title
diverges from the raw MAM torrent name often enough for
`check_book` to mis-grade many of them).

v2.3.7 adds:

- A new `book_grab_links(grab_id, library_slug, book_id)` table in
  the global app DB that records which grab was attributed to which
  per-library book row. Migration appended to `MIGRATIONS`.
- A new helper module `app/discovery/acquisition_linkback.py` with
  `link_new_book(library_db, slug, book_id, title, author_name,
  is_audiobook=...)`. Conservative match: 60%+ overlap of >=3-char
  title tokens with the grab's `torrent_name`, AND author surname
  appearing in either `torrent_name` or `author_blob`. Tied top
  scores → log + bail rather than guess. 30-day lookback window.
  Audiobook books only consider audiobook-category grabs, ebooks
  only consider non-audiobook grabs.
- ABS sync (`audiobookshelf_sync.py`) calls `link_new_book` after
  each new-book INSERT. Calibre sync (`calibre_sync.py`) does the
  same for ebook symmetry.
- Skips on existing `mam_status` (don't stomp prior scans / user
  edits) and on already-linked grabs (UNIQUE on grab_id).

Books that don't match any recent grab fall back to the legacy MAM
scan path — the link-back is an opportunistic write, never a
prerequisite for `mam_status` to land.

### Tests

- 8 new tests in `tests/discovery/test_skip_mam.py` covering the
  PUT allowlist, filter helper, bulk endpoint, predicate exclusion,
  and stats counter.
- 9 new tests in `tests/discovery/test_acquisition_linkback.py`
  covering confident match, double-claim guard, author + title
  rejection, ambiguity bail, existing-status preservation,
  content-type filtering, and the 30-day lookback window.
- Existing `test_mam_multilibrary_scan.py` "no books need scanning"
  case carried forward from v2.3.6 (uses `mam_status='found'` since
  that's now the only terminal status).

Suite: 1591 passing.

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
