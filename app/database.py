"""
Database layer.

Single SQLite database under DATA_DIR for the pipeline domain. The
discovery domain uses its own per-library DBs; see
`app/discovery/database.py`.

Schema and migrations both live in this file. SCHEMA is the up-to-date
target shape; MIGRATIONS is the ordered list of statements that bring an
older database forward. `PRAGMA user_version` tracks how many migrations
have been applied so subsequent startups skip the work.

Connection pragmas:
  - WAL mode: keeps readers unblocked during writes (important for
    background workers + UI polling concurrency)
  - foreign_keys=ON: enforced at runtime, not just declared
  - busy_timeout=30s: long enough to wait out a slow background writer

Tables cover the full pipeline: author lists, announce audit log,
grabs + snatch ledger, book review queue, tentative/ignored capture,
calibre additions counter, and metadata enrichment support.
"""
import logging

import aiosqlite

from app.config import APP_DB_PATH

_log = logging.getLogger("seshat.database")


# ─── Schema ──────────────────────────────────────────────────
# CREATE TABLE IF NOT EXISTS is safe to run on every startup. Indexes
# follow the same pattern.
SCHEMA = """
CREATE TABLE IF NOT EXISTS authors_allowed (
    name              TEXT PRIMARY KEY,
    normalized        TEXT NOT NULL UNIQUE,
    source            TEXT NOT NULL,
    added_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS authors_ignored (
    name              TEXT PRIMARY KEY,
    normalized        TEXT NOT NULL UNIQUE,
    source            TEXT NOT NULL,
    added_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS authors_weekly_skip (
    name              TEXT PRIMARY KEY,
    normalized        TEXT NOT NULL UNIQUE,
    first_seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
    hits_count        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS announces (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    seen_at           TEXT NOT NULL DEFAULT (datetime('now')),
    raw               TEXT NOT NULL,
    torrent_id        TEXT,
    torrent_name      TEXT,
    category          TEXT,
    author_blob       TEXT,
    decision          TEXT NOT NULL,
    decision_reason   TEXT NOT NULL,
    matched_author    TEXT,
    -- v2.9.0 format-priority dedup: persist the IRC announce's
    -- `Filetype: ( xxx )` field so we can audit dedup decisions
    -- after the fact. Pre-v2.9.0 announces have this NULL.
    filetype          TEXT
);

CREATE TABLE IF NOT EXISTS grabs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    announce_id       INTEGER REFERENCES announces(id) ON DELETE SET NULL,
    mam_torrent_id    TEXT NOT NULL,
    torrent_name      TEXT NOT NULL,
    category          TEXT,
    author_blob       TEXT,
    torrent_file_path TEXT,
    qbit_hash         TEXT,
    state             TEXT NOT NULL,
    state_updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    grabbed_at        TEXT NOT NULL DEFAULT (datetime('now')),
    submitted_at      TEXT,
    completed_at      TEXT,
    failed_reason     TEXT,
    failed_with_cookie_id INTEGER,
    -- v2.8.0 reingest: 1 = this grab row was synthesized from an
    -- already-snatched torrent reingested from disk (the pre-Seshat
    -- "Already Snatched" case). No .torrent was fetched from MAM,
    -- no qBit submit was made, and no snatch budget was charged.
    -- The pipeline path past `STATE_DOWNLOADED` runs identically
    -- to a normal grab. is_reingest=0 on every legacy/normal row.
    is_reingest       INTEGER NOT NULL DEFAULT 0,
    -- v2.9.0 format-priority dedup. `book_format` is the lowercased
    -- file extension hint (epub, azw3, m4b, mp3, ...) taken from the
    -- announce's Filetype field at grab time. `dedup_key` is the
    -- normalized (title, first-author-surname) tuple used to find
    -- in-flight or owned siblings of the same book regardless of
    -- format. Both NULL on pre-v2.9.0 grabs — see migration backfill.
    book_format       TEXT,
    dedup_key         TEXT
);

CREATE TABLE IF NOT EXISTS snatch_ledger (
    grab_id                  INTEGER PRIMARY KEY REFERENCES grabs(id) ON DELETE CASCADE,
    qbit_hash                TEXT,
    seeding_seconds          INTEGER NOT NULL DEFAULT 0,
    last_check_at            TEXT,
    released_at              TEXT,
    released_reason          TEXT
);

CREATE TABLE IF NOT EXISTS pending_queue (
    grab_id     INTEGER PRIMARY KEY REFERENCES grabs(id) ON DELETE CASCADE,
    priority    INTEGER NOT NULL DEFAULT 0,
    queued_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mam_session (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cookie              TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_validated_at   TEXT,
    validation_ok       INTEGER NOT NULL DEFAULT 0,
    superseded_at       TEXT
);

-- Phase 2: post-download pipeline tracking.
-- One row per grab that has finished downloading and entered the
-- post-download pipeline. Tracks the file through staging, metadata
-- review, and sink delivery.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    grab_id           INTEGER NOT NULL REFERENCES grabs(id) ON DELETE CASCADE,
    qbit_hash         TEXT,
    source_path       TEXT,
    staged_path       TEXT,
    book_filename     TEXT,
    book_format       TEXT,
    metadata_title    TEXT,
    metadata_author   TEXT,
    metadata_series   TEXT,
    metadata_language TEXT,
    sink_name         TEXT,
    sink_result       TEXT,
    state             TEXT NOT NULL,
    state_updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    started_at        TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at      TEXT,
    error             TEXT
);

-- Tier 2: mandatory manual review queue for downloaded books.
-- Every successfully-downloaded book lands here after metadata
-- enrichment and BEFORE being delivered to the Calibre/CWA sink.
-- The user approves, rejects, or lets it time out (auto-add).
CREATE TABLE IF NOT EXISTS book_review_queue (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    grab_id           INTEGER NOT NULL REFERENCES grabs(id) ON DELETE CASCADE,
    pipeline_run_id   INTEGER REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    staged_path       TEXT NOT NULL,
    book_filename     TEXT NOT NULL,
    book_format       TEXT,
    metadata_json     TEXT NOT NULL,
    cover_path        TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at        TEXT,
    decision_note     TEXT,
    -- v2.7.0 bundle-aware pipeline: when a single torrent contains
    -- multiple distinct works (e.g. a 3-book MAM bundle), the pipeline
    -- fans out into N review entries instead of dropping the extras.
    -- Single-book grabs get bundle_total=1, bundle_index=0 (default
    -- shape — indistinguishable from pre-v2.7 rows after backfill).
    -- library_slug stamps every entry with its target library so
    -- delivery routes to the correct sink (multi-library safety —
    -- without this a bundle could deliver to the wrong library when
    -- two libraries share numeric ids).
    bundle_group_id      TEXT,
    bundle_index         INTEGER NOT NULL DEFAULT 0,
    bundle_total         INTEGER NOT NULL DEFAULT 1,
    library_slug         TEXT,
    bundle_parent_grab_id INTEGER
);

-- Tier 2: tentative torrent queue for announces that passed all
-- filters except the author allow-list. We scrape metadata and
-- stash the MAM URL so the user can decide later. No .torrent
-- file is fetched until approval — saves snatch budget.
CREATE TABLE IF NOT EXISTS tentative_torrents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mam_torrent_id      TEXT NOT NULL,
    torrent_name        TEXT NOT NULL,
    author_blob         TEXT NOT NULL,
    category            TEXT,
    language            TEXT,
    format              TEXT,
    vip                 INTEGER NOT NULL DEFAULT 0,
    scraped_metadata_json TEXT,
    cover_path          TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at          TEXT
);

-- Tier 2: 3-tier author taxonomy. When a tentative torrent is
-- REJECTED the relevant author goes here for one more pass of
-- weekly review before being auto-promoted to ignored.
CREATE TABLE IF NOT EXISTS authors_tentative_review (
    name              TEXT PRIMARY KEY,
    normalized        TEXT NOT NULL UNIQUE,
    source            TEXT NOT NULL,
    added_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Tier 2: capture ignored-author torrents for weekly review.
-- When an announce is skipped because the author is on the
-- ignored list, we still want to see the book (cover + metadata)
-- in case the user changes their mind. One row per announce seen.
CREATE TABLE IF NOT EXISTS ignored_torrents_seen (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    mam_torrent_id    TEXT NOT NULL,
    torrent_name      TEXT NOT NULL,
    author_blob       TEXT NOT NULL,
    category          TEXT,
    info_url          TEXT,
    cover_path        TEXT,
    seen_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Tier 2: counter for books successfully added to Calibre/CWA.
-- One row per successful sink delivery. Used by daily/weekly
-- digests to report throughput without reparsing pipeline_runs.
CREATE TABLE IF NOT EXISTS calibre_additions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    grab_id           INTEGER NOT NULL REFERENCES grabs(id) ON DELETE CASCADE,
    review_id         INTEGER REFERENCES book_review_queue(id) ON DELETE SET NULL,
    title             TEXT,
    author            TEXT,
    sink_name         TEXT,
    added_at          TEXT NOT NULL DEFAULT (datetime('now')),
    was_timeout       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_announces_seen_at ON announces(seen_at);
CREATE INDEX IF NOT EXISTS idx_announces_decision ON announces(decision);
CREATE INDEX IF NOT EXISTS idx_grabs_state ON grabs(state);
CREATE INDEX IF NOT EXISTS idx_grabs_torrent_id ON grabs(mam_torrent_id);
CREATE INDEX IF NOT EXISTS idx_snatch_ledger_released ON snatch_ledger(released_at);
CREATE INDEX IF NOT EXISTS idx_pending_queue_priority ON pending_queue(priority, queued_at);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_state ON pipeline_runs(state);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_grab_id ON pipeline_runs(grab_id);
CREATE INDEX IF NOT EXISTS idx_review_queue_status ON book_review_queue(status);
CREATE INDEX IF NOT EXISTS idx_review_queue_created_at ON book_review_queue(created_at);
-- NOTE: idx_review_queue_bundle_group is intentionally NOT declared
-- in SCHEMA. SCHEMA runs before MIGRATIONS, and on legacy v2.6.x
-- databases the bundle_group_id column doesn't exist yet — its
-- CREATE INDEX would crash at startup with "no such column"
-- (v2.7.0 regression). The index is created by the migration block
-- BELOW after the corresponding ALTER TABLE adds the column. Fresh
-- DBs reach the same end-state via the migration loop (user_version
-- starts at 0, so every migration runs once).
CREATE INDEX IF NOT EXISTS idx_tentative_status ON tentative_torrents(status);
CREATE INDEX IF NOT EXISTS idx_tentative_torrent_id ON tentative_torrents(mam_torrent_id);
CREATE INDEX IF NOT EXISTS idx_ignored_seen_at ON ignored_torrents_seen(seen_at);
CREATE INDEX IF NOT EXISTS idx_calibre_add_added_at ON calibre_additions(added_at);

-- ── Cross-library work linking ───────────────────────────────
-- `work_links` groups books from different libraries that represent
-- the same underlying work (e.g. an ebook in Calibre and its audiobook
-- equivalent in Audiobookshelf). Each row is one (library, book) →
-- work_id membership. Multiple rows share a work_id when they point
-- at different formats / libraries of the same work.
--
-- `book_id` references the per-library discovery DB's `books.id` —
-- NOT a foreign key here (can't FK across SQLite files). The auto-
-- matcher and reconcile pass handle orphan cleanup when a linked
-- book disappears from its source library.
CREATE TABLE IF NOT EXISTS work_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id         TEXT NOT NULL,
    library_slug    TEXT NOT NULL,
    book_id         INTEGER NOT NULL,
    content_type    TEXT NOT NULL,        -- "ebook" | "audiobook"
    link_source     TEXT NOT NULL DEFAULT 'auto',  -- "auto" | "manual"
    created_at      REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    UNIQUE(library_slug, book_id)
);
CREATE INDEX IF NOT EXISTS idx_work_links_work_id ON work_links(work_id);
CREATE INDEX IF NOT EXISTS idx_work_links_lib_book ON work_links(library_slug, book_id);
CREATE INDEX IF NOT EXISTS idx_work_links_content_type ON work_links(content_type);

-- ── Cross-library author identity (v2.20.0) ──────────────────
-- `persons` is the canonical "this is one human author" record;
-- `author_links` maps per-library `authors` rows (which live in their
-- own per-library DBs) onto a person_id. Together they replace the
-- de-facto "an author exists per library" model with "an author exists
-- once, libraries reference them," fixing the cross-library mirror
-- pain we hit during the Amazon and Goodreads audits.
--
-- `pen_name_links_v2` is the cross-library successor to per-library
-- `pen_name_links` — same intra-library aliasing concept, but keyed
-- on person_id so pen names are also cross-library by construction.
-- The per-library `pen_name_links` tables stay for safety; the
-- one-time migration in `app/discovery/author_identity.py` copies
-- their rows here, resolving both endpoints via author_links.
--
-- `author_id` references per-library `authors.id` — NOT a foreign
-- key here (can't FK across SQLite files). App-level helpers in
-- `app/discovery/author_identity.py` keep the cross-DB references
-- consistent and prune orphans.
CREATE TABLE IF NOT EXISTS persons (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name           TEXT NOT NULL,
    normalized_name          TEXT NOT NULL,
    display_name_override    TEXT,
    bio                      TEXT,
    image_url                TEXT,
    last_updated_at          REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    created_at               REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    UNIQUE(normalized_name)
);
CREATE INDEX IF NOT EXISTS idx_persons_normalized ON persons(normalized_name);

CREATE TABLE IF NOT EXISTS author_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id       INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    library_slug    TEXT NOT NULL,
    author_id       INTEGER NOT NULL,
    link_source     TEXT NOT NULL DEFAULT 'auto',
    link_confidence TEXT NOT NULL DEFAULT 'high',
    created_at      REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    UNIQUE(library_slug, author_id)
);
CREATE INDEX IF NOT EXISTS idx_author_links_person ON author_links(person_id);
CREATE INDEX IF NOT EXISTS idx_author_links_lib_author ON author_links(library_slug, author_id);

CREATE TABLE IF NOT EXISTS pen_name_links_v2 (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_person_id   INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    alias_person_id       INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    link_type             TEXT NOT NULL DEFAULT 'pen_name',
    created_at            REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    UNIQUE(canonical_person_id, alias_person_id)
);
CREATE INDEX IF NOT EXISTS idx_pen_name_v2_canonical ON pen_name_links_v2(canonical_person_id);
CREATE INDEX IF NOT EXISTS idx_pen_name_v2_alias ON pen_name_links_v2(alias_person_id);

-- v2.20.0 Phase 3 — audit log for source-ID edits made through the
-- author-detail badge UI. One row per PATCH /persons/{id}/source-id
-- write. Used by future "history of fixes" view + revert tooling.
CREATE TABLE IF NOT EXISTS author_id_audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id    INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    source_name  TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    changed_at   REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_author_id_audit_person ON author_id_audit_log(person_id);
CREATE INDEX IF NOT EXISTS idx_author_id_audit_changed ON author_id_audit_log(changed_at);

-- ── Per-author format preference ────────────────────────────
-- Keyed by normalized author name (lowercased, whitespace-collapsed)
-- so a preference set on "Brandon Sanderson" in a Calibre library
-- is also honored when the same author appears in an ABS library.
-- `tracking_mode`:
--   "ebook"     — missing-book detection counts only ebook absences
--   "audiobook" — only audiobook absences count
--   "both"      — owning either format satisfies (default)
-- NULL tracking_mode = fall back to the global `audiobook_tracking_mode`
-- setting (default "both").
CREATE TABLE IF NOT EXISTS author_format_preferences (
    normalized_name TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    tracking_mode   TEXT NOT NULL,
    updated_at      REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- ── MAM economy audit trail ────────────────────────────────
-- One row per economy decision: scheduled VIP/upload purchases
-- (success OR skip — skips are load-bearing for "why didn't you
-- buy last tick?" UI), manual buy-now clicks, personal-FL buys
-- attached to grabs, and buffer-gate blocks. The scheduler, the
-- router, and the dispatch buffer-gate all write here through
-- `app/storage/economy_audit.py`.
--
-- `amount` is TEXT on purpose — it holds "50" (GB) for upload,
-- "4" or "max" for VIP, NULL for personal-FL. Storing as a string
-- lets the UI echo the same value the user selected without
-- guessing numeric scale.
-- `cost_points` and `user_bonus_after` are REAL because the
-- bonusBuy.php response returns fractional seedbonus values.
CREATE TABLE IF NOT EXISTS economy_audit (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at        TEXT NOT NULL DEFAULT (datetime('now')),
    action             TEXT NOT NULL,       -- 'vip' | 'upload' | 'personal_fl' | 'buffer_gate_block'
    trigger            TEXT NOT NULL,       -- 'scheduled' | 'manual' | 'irc_autograb' | 'user_grab'
    mode               TEXT,                -- 'ratio' | 'buffer' | 'bonus' | NULL
    amount             TEXT,
    torrent_id         TEXT,
    outcome            TEXT NOT NULL,       -- 'success' | 'failure' | 'skip_*' | 'buffer_gate_block'
    tier               TEXT,                -- 'trigger:ratio' etc.; NULL for skips
    message            TEXT,
    cost_points        REAL,
    user_bonus_after   REAL
);
CREATE INDEX IF NOT EXISTS idx_economy_audit_occurred ON economy_audit (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_economy_audit_action ON economy_audit (action, occurred_at DESC);

-- v2.3.7 — acquisition link-back: link a downloaded book in a per-library
-- discovery DB back to the grab that produced it. Without this, a fresh
-- ABS-synced (or Calibre-synced) row arrives with mam_status=NULL even
-- though the grab table holds the exact mam_torrent_id. The next MAM
-- scan would then run a fuzzy `check_book` search whose match might
-- grade as 'not_found' or 'possible' — silently misclassifying books we
-- KNOW we got from MAM.
--
-- One row per linked grab. UNIQUE on (library_slug, book_id) blocks two
-- grabs from claiming the same row; PRIMARY KEY on grab_id blocks the
-- same grab from claiming two rows. Either side of the link being
-- pre-occupied means the auto-link skips and the book stays NULL,
-- letting MAM scans handle it the legacy way.
CREATE TABLE IF NOT EXISTS book_grab_links (
    grab_id      INTEGER PRIMARY KEY REFERENCES grabs(id) ON DELETE CASCADE,
    library_slug TEXT NOT NULL,
    book_id      INTEGER NOT NULL,
    linked_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(library_slug, book_id)
);
CREATE INDEX IF NOT EXISTS idx_book_grab_links_lookup ON book_grab_links (library_slug, book_id);

-- Part C — cover-image perceptual hash cache (MAM URL verification).
-- Lives in the global DB (not per-library) because torrent_id is
-- universal across libraries — same torrent evaluated against ebook
-- AND audiobook libraries reuses the same fetched cover. Stale rows
-- past the 30-day TTL in `app/mam/cover_hash.py` get silently re-fetched
-- on next read. Diagnostic columns (width/height/bytes) are useful when
-- investigating odd distance comparisons via SQL.
CREATE TABLE IF NOT EXISTS mam_cover_hashes (
    torrent_id  TEXT PRIMARY KEY,
    phash       TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    width       INTEGER,
    height      INTEGER,
    bytes       INTEGER
);

-- v2.9.0 — format-priority dedup hold queue.
-- When an announce arrives for a disabled-format with no in-flight or
-- owned sibling, we don't grab immediately. Instead we park it here
-- for `format_dedup_hold_seconds` (default 600s = 10 min) and let the
-- scheduler re-evaluate at `release_at`. If a higher-priority sibling
-- arrives during the hold window, the hold is dropped. If nothing
-- arrives, the hold is released and we inject the grab.
--
-- `dedup_key` mirrors `grabs.dedup_key` (normalized title + first-author
-- surname). `media_type` is "ebook" or "audiobook" — used to look up
-- the priority list. `book_format` is the lowercased filetype hint.
-- `torrent_id`, `torrent_name`, `category`, `author_blob` are stored
-- so the scheduler can call inject_grab at release time without
-- needing the source announce row to still exist.
-- `state` is one of: 'pending' (timer still running), 'released' (timer
-- fired, grab injected), 'dropped' (higher-priority sibling arrived).
CREATE TABLE IF NOT EXISTS pending_holds (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    announce_id       INTEGER REFERENCES announces(id) ON DELETE SET NULL,
    dedup_key         TEXT NOT NULL,
    media_type        TEXT NOT NULL,
    book_format       TEXT NOT NULL,
    torrent_id        TEXT NOT NULL,
    torrent_name      TEXT NOT NULL,
    category          TEXT,
    author_blob       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    release_at        TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'pending',
    resolved_at       TEXT,
    resolution_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_holds_state_release
    ON pending_holds(state, release_at);
CREATE INDEX IF NOT EXISTS idx_pending_holds_dedup_key
    ON pending_holds(dedup_key);
"""


# ─── Migrations ──────────────────────────────────────────────
# Append-only ordered list. Each entry is one SQL statement that brings
# an older database forward by exactly one step. `PRAGMA user_version`
# tracks how many entries have been applied.
#
# Empty in Phase 1 — the schema above is the v0 baseline. Migrations
# only get added when we need to evolve the schema after Seshat is
# running in production.
MIGRATIONS: list[str] = [
    # v1.1 — source-metadata handoff. Stores the JSON-encoded metadata
    # dict that the discovery domain (or external batch submitters)
    # sends alongside a grab. When present on a grab row, the
    # pipeline's _prepare_book uses it to skip the enricher call and
    # save 6 outbound scraper requests per book.
    "ALTER TABLE grabs ADD COLUMN source_metadata TEXT",
    # v1.2 — cross-library work linking (Phase 5). Tables and indexes
    # also exist in SCHEMA above, but older DBs need the migration step
    # to pick them up without a fresh init. CREATE TABLE IF NOT EXISTS
    # makes re-runs on fresh DBs a no-op.
    """CREATE TABLE IF NOT EXISTS work_links (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        work_id         TEXT NOT NULL,
        library_slug    TEXT NOT NULL,
        book_id         INTEGER NOT NULL,
        content_type    TEXT NOT NULL,
        link_source     TEXT NOT NULL DEFAULT 'auto',
        created_at      REAL NOT NULL DEFAULT (strftime('%s', 'now')),
        UNIQUE(library_slug, book_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_work_links_work_id ON work_links(work_id)",
    "CREATE INDEX IF NOT EXISTS idx_work_links_lib_book ON work_links(library_slug, book_id)",
    "CREATE INDEX IF NOT EXISTS idx_work_links_content_type ON work_links(content_type)",
    """CREATE TABLE IF NOT EXISTS author_format_preferences (
        normalized_name TEXT PRIMARY KEY,
        display_name    TEXT NOT NULL,
        tracking_mode   TEXT NOT NULL,
        updated_at      REAL NOT NULL DEFAULT (strftime('%s', 'now'))
    )""",
    # v1.3 — MAM economy audit trail (Tier 1 MouseSearch port). Mirrors
    # the CREATE block in SCHEMA above so new DBs pick it up on init
    # and legacy DBs get it applied here on next startup.
    """CREATE TABLE IF NOT EXISTS economy_audit (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at        TEXT NOT NULL DEFAULT (datetime('now')),
        action             TEXT NOT NULL,
        trigger            TEXT NOT NULL,
        mode               TEXT,
        amount             TEXT,
        torrent_id         TEXT,
        outcome            TEXT NOT NULL,
        tier               TEXT,
        message            TEXT,
        cost_points        REAL,
        user_bonus_after   REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_economy_audit_occurred ON economy_audit (occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_economy_audit_action ON economy_audit (action, occurred_at DESC)",
    # v2.3.7 — book_grab_links: links a downloaded grab to the per-library
    # discovery row it produced. Sync hooks read from this table to skip
    # already-linked grabs and write to it after a successful auto-link.
    """CREATE TABLE IF NOT EXISTS book_grab_links (
        grab_id      INTEGER PRIMARY KEY REFERENCES grabs(id) ON DELETE CASCADE,
        library_slug TEXT NOT NULL,
        book_id      INTEGER NOT NULL,
        linked_at    TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(library_slug, book_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_book_grab_links_lookup ON book_grab_links (library_slug, book_id)",
    # Part C — cover-image perceptual hash cache. Global (not per-library)
    # because torrent_id is universal across libraries. See full doc on
    # the SCHEMA block above and `app/mam/cover_hash.py`.
    """CREATE TABLE IF NOT EXISTS mam_cover_hashes (
        torrent_id  TEXT PRIMARY KEY,
        phash       TEXT NOT NULL,
        fetched_at  REAL NOT NULL,
        width       INTEGER,
        height      INTEGER,
        bytes       INTEGER
    )""",
    # v2.7.0 — bundle-aware pipeline. Five new columns on
    # book_review_queue: bundle_group_id (deterministic
    # `f"grab-{grab_id}"` per torrent), bundle_index (0-based child
    # position within the bundle), bundle_total (1 for single-book
    # grabs, N for bundles), library_slug (target library for sink
    # delivery — multi-library safety), bundle_parent_grab_id (set
    # only on bundle children; carries through approval into future
    # acquisition-linkback so the bundle MAM URL stays attached on
    # re-ingest). Legacy rows backfilled below with `bundle_total=1,
    # bundle_index=0, bundle_group_id="grab-<id>"`.
    "ALTER TABLE book_review_queue ADD COLUMN bundle_group_id TEXT",
    "ALTER TABLE book_review_queue ADD COLUMN bundle_index INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE book_review_queue ADD COLUMN bundle_total INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE book_review_queue ADD COLUMN library_slug TEXT",
    "ALTER TABLE book_review_queue ADD COLUMN bundle_parent_grab_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_review_queue_bundle_group "
    "ON book_review_queue(bundle_group_id)",
    "UPDATE book_review_queue "
    "SET bundle_group_id = 'grab-' || grab_id "
    "WHERE bundle_group_id IS NULL",
    # v2.8.0 — reingest grabs: distinguishes grab rows synthesized
    # from already-snatched-on-disk reingests (no MAM .torrent fetch,
    # no qBit submit, no snatch budget charge) from normal grabs.
    "ALTER TABLE grabs ADD COLUMN is_reingest INTEGER NOT NULL DEFAULT 0",
    # v2.9.0 — format-priority dedup. Persist the announce filetype
    # for audit; tag each grab with book_format + dedup_key so the
    # dedup gate can find in-flight siblings by normalized key. The
    # index on grabs(dedup_key) MUST live in MIGRATIONS only — putting
    # it in SCHEMA's CREATE INDEX block would crash legacy DBs the
    # same way the v2.7.0 bundle_group_id regression did (column not
    # yet ALTERed in when SCHEMA runs). See test_legacy_db_upgrade.py.
    "ALTER TABLE announces ADD COLUMN filetype TEXT",
    "ALTER TABLE grabs ADD COLUMN book_format TEXT",
    "ALTER TABLE grabs ADD COLUMN dedup_key TEXT",
    "CREATE INDEX IF NOT EXISTS idx_grabs_dedup_key ON grabs(dedup_key)",
    """CREATE TABLE IF NOT EXISTS pending_holds (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        announce_id       INTEGER REFERENCES announces(id) ON DELETE SET NULL,
        dedup_key         TEXT NOT NULL,
        media_type        TEXT NOT NULL,
        book_format       TEXT NOT NULL,
        torrent_id        TEXT NOT NULL,
        torrent_name      TEXT NOT NULL,
        category          TEXT,
        author_blob       TEXT,
        created_at        TEXT NOT NULL DEFAULT (datetime('now')),
        release_at        TEXT NOT NULL,
        state             TEXT NOT NULL DEFAULT 'pending',
        resolved_at       TEXT,
        resolution_reason TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pending_holds_state_release "
    "ON pending_holds(state, release_at)",
    "CREATE INDEX IF NOT EXISTS idx_pending_holds_dedup_key "
    "ON pending_holds(dedup_key)",
    # v2.20.0 — cross-library author identity. Tables also live in
    # SCHEMA above (CREATE IF NOT EXISTS makes the migration step a
    # no-op for fresh installs); the migration here is what brings
    # older DBs forward. The one-time DATA migration that walks
    # per-library `authors` tables and populates `persons` +
    # `author_links` lives in `app/discovery/author_identity.py` and
    # runs from `main.py` after all per-library `init_db()` calls
    # have completed — can't be expressed as a single SQL statement.
    """CREATE TABLE IF NOT EXISTS persons (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_name           TEXT NOT NULL,
        normalized_name          TEXT NOT NULL,
        display_name_override    TEXT,
        bio                      TEXT,
        image_url                TEXT,
        last_updated_at          REAL NOT NULL DEFAULT (strftime('%s', 'now')),
        created_at               REAL NOT NULL DEFAULT (strftime('%s', 'now')),
        UNIQUE(normalized_name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_persons_normalized ON persons(normalized_name)",
    """CREATE TABLE IF NOT EXISTS author_links (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id       INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
        library_slug    TEXT NOT NULL,
        author_id       INTEGER NOT NULL,
        link_source     TEXT NOT NULL DEFAULT 'auto',
        link_confidence TEXT NOT NULL DEFAULT 'high',
        created_at      REAL NOT NULL DEFAULT (strftime('%s', 'now')),
        UNIQUE(library_slug, author_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_author_links_person ON author_links(person_id)",
    "CREATE INDEX IF NOT EXISTS idx_author_links_lib_author "
    "ON author_links(library_slug, author_id)",
    """CREATE TABLE IF NOT EXISTS pen_name_links_v2 (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_person_id   INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
        alias_person_id       INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
        link_type             TEXT NOT NULL DEFAULT 'pen_name',
        created_at            REAL NOT NULL DEFAULT (strftime('%s', 'now')),
        UNIQUE(canonical_person_id, alias_person_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pen_name_v2_canonical "
    "ON pen_name_links_v2(canonical_person_id)",
    "CREATE INDEX IF NOT EXISTS idx_pen_name_v2_alias "
    "ON pen_name_links_v2(alias_person_id)",
    # v2.20.0 Phase 3 — audit log for source-ID badge edits.
    """CREATE TABLE IF NOT EXISTS author_id_audit_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id    INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
        source_name  TEXT NOT NULL,
        old_value    TEXT,
        new_value    TEXT,
        changed_at   REAL NOT NULL DEFAULT (strftime('%s', 'now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_author_id_audit_person "
    "ON author_id_audit_log(person_id)",
    "CREATE INDEX IF NOT EXISTS idx_author_id_audit_changed "
    "ON author_id_audit_log(changed_at)",
]


async def get_db() -> aiosqlite.Connection:
    """Open a connection with the standard pragmas applied."""
    db = await aiosqlite.connect(str(APP_DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=30000")
    return db


async def init_db():
    """Create schema and run migrations.

    Idempotent: safe to call on every startup. Skips already-applied
    migrations via PRAGMA user_version.
    """
    db = await get_db()
    try:
        # Read current schema version (0 for fresh DBs).
        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        current_version = row[0] if row else 0
        target_version = len(MIGRATIONS)

        # Always ensure base tables + indexes exist.
        await db.executescript(SCHEMA)
        await db.commit()

        # Apply only the migrations we haven't seen.
        if current_version < target_version:
            _log.info(
                f"Migrating database schema: v{current_version} → v{target_version}"
            )
            for i, migration in enumerate(MIGRATIONS):
                if i < current_version:
                    continue
                try:
                    await db.execute(migration)
                except aiosqlite.OperationalError as e:
                    msg = str(e).lower()
                    # Tolerate the harmless "already there" cases that show
                    # up when migrating a legacy database that had columns
                    # added by an older always-run loop.
                    if (
                        "duplicate column" in msg
                        or "already exists" in msg
                        or "no such column" in msg
                    ):
                        continue
                    _log.warning(
                        f"Migration #{i} failed unexpectedly: {e} "
                        f"(SQL: {migration[:80]}...)"
                    )
            await db.commit()
            await db.execute(f"PRAGMA user_version = {target_version}")
            await db.commit()
    finally:
        await db.close()
