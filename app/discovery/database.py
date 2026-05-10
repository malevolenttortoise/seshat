"""
Discovery database layer — per-library SQLite databases for book metadata.

Per-library databases live under DATA_DIR with filenames like
`seshat_{slug}.db`. The active library is selected at runtime via
`set_active_library` and read by `get_db` so every endpoint operates
against the right database without passing the slug explicitly.
"""
import logging
import re
from collections import defaultdict
import aiosqlite
from app.config import DATA_DIR

_db_logger = logging.getLogger("seshat.discovery.database")

# Common SQL filter constant used by routes that query books.
# Excludes hidden books from results. Apply as:
#     WHERE {HF} AND other_conditions...
HF = "b.hidden = 0"

# ─── Active Library Tracking ─────────────────────────────────
_active_library_slug = None


def set_active_library(slug):
    """Set the active library slug. All get_db() calls will use this library."""
    global _active_library_slug
    _active_library_slug = slug
    _db_logger.debug(f"Active library set to: {slug}")


def get_active_library():
    """Get the current active library slug."""
    return _active_library_slug


def get_db_path(slug=None):
    """Get the database file path for a library slug.

    If slug is provided, returns the per-library path.
    If slug is None, uses the active library slug.
    Falls back to seshat_default.db if no library is set.
    """
    effective_slug = slug or _active_library_slug or "default"
    return DATA_DIR / f"seshat_{effective_slug}.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort_name TEXT NOT NULL,
    calibre_id INTEGER,
    hardcover_id TEXT,
    goodreads_id TEXT,
    kobo_id TEXT,
    fictiondb_id TEXT,
    ibdb_id TEXT,
    google_books_id TEXT,
    image_url TEXT,
    bio TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    last_lookup_at REAL,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    -- author_id is nullable as of v2.3.0: NULL = shared series
    -- (Halo, Star Wars, etc.). Per-author rows still exist for the
    -- common case AND for genuine name collisions like Cressman's
    -- vs Savarovsky's "The Last Paladin". Calibre-sync auto-promotes
    -- to shared when one Calibre series id has books from 2+ authors.
    author_id INTEGER,
    hardcover_id TEXT,
    goodreads_id TEXT,
    kobo_id TEXT,
    fictiondb_id TEXT,
    total_books INTEGER,
    description TEXT,
    last_lookup_at REAL,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    FOREIGN KEY (author_id) REFERENCES authors(id),
    UNIQUE(name, author_id)
);

CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author_id INTEGER NOT NULL,
    series_id INTEGER,
    series_index REAL,
    isbn TEXT,
    hardcover_id TEXT,
    goodreads_id TEXT,
    fictiondb_id TEXT,
    kobo_id TEXT,
    cover_url TEXT,
    cover_path TEXT,
    pub_date TEXT,
    expected_date TEXT,
    is_unreleased INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    page_count INTEGER,
    source TEXT NOT NULL DEFAULT 'calibre',
    owned INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    calibre_id INTEGER,
    is_new INTEGER NOT NULL DEFAULT 0,
    language TEXT,
    rating REAL,
    tags TEXT,
    publisher TEXT,
    formats TEXT,
    mam_url TEXT,
    mam_status TEXT,
    mam_formats TEXT,
    mam_torrent_id TEXT,
    mam_has_multiple INTEGER NOT NULL DEFAULT 0,
    mam_my_snatched INTEGER NOT NULL DEFAULT 0,
    mam_is_bundle INTEGER NOT NULL DEFAULT 0,
    -- Part C — perceptual hash (16-char hex pHash) of the book's
    -- local/source cover image. Compared against MAM candidate covers
    -- during scan via `app.mam.cover_hash.hamming_distance`. NULL when
    -- the book has no cover available or hashing failed (e.g. malformed
    -- file). Populated by Calibre/ABS sync hooks on cover landing and
    -- by the source-scan write path when `cover_url` lands.
    cover_phash TEXT,
    -- Unix epoch seconds (REAL). Stamped on every successful MAM
    -- scan (FOUND/POSSIBLE/NOT_FOUND), NOT on auth_error or other
    -- transient failures. Drives the "skip recently-scanned books"
    -- eligibility filter so the scan front rotates through the full
    -- library instead of treading water on slow-moving Possible /
    -- Not Found tails. NULL means never scanned.
    mam_last_scanned_at REAL,
    -- source_url stores a JSON dict mapping source-plugin name to URL:
    --   {"goodreads": "https://www.goodreads.com/book/show/123",
    --    "hardcover": "https://hardcover.app/books/slug", ...}
    -- It's JSON because a single book can be enriched by multiple sources
    -- over time (each scan adds its own URL via _merge_source_urls in
    -- lookup.py). The frontend parses it in BookSidebar.jsx and
    -- BookViews.jsx and renders one badge per source. There is no
    -- migration that validates/repairs corrupt JSON — all writes go
    -- through json.dumps, so corruption would only arise from direct
    -- SQL editing or a botched import/export round-trip.
    source_url TEXT,
    first_seen_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    FOREIGN KEY (author_id) REFERENCES authors(id),
    FOREIGN KEY (series_id) REFERENCES series(id)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_type TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL DEFAULT 'running',
    books_found INTEGER DEFAULT 0,
    books_new INTEGER DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS mam_scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    total_books INTEGER NOT NULL DEFAULT 0,
    last_offset INTEGER NOT NULL DEFAULT 0,
    batch_size INTEGER NOT NULL DEFAULT 400,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL DEFAULT 'running',
    -- JSON array of book IDs captured at scan start. Each batch
    -- consumes a slice of this list rather than re-querying
    -- `WHERE mam_status IS NULL`, so a concurrent author scan
    -- adding new books mid-scan can NOT inflate the queue. Empty
    -- or null means a legacy pre-snapshot scan that should fall
    -- back to the old query path.
    book_ids_snapshot TEXT
);

-- Source-consensus series suggestions. One row per book with an
-- active suggestion. The merge layer populates this whenever 2+
-- sources independently agree on a (series_name, series_index) tuple
-- that differs from what's currently stored on the book.
--
-- Lifecycle:
--   pending  → the user hasn't reviewed yet
--   applied  → the user accepted; the book row was updated, and we
--              suppress re-suggestion of the same tuple forever
--   ignored  → the user rejected; we suppress THIS exact tuple but a
--              future scan that produces a DIFFERENT consensus
--              creates a fresh pending row
--
-- The `current_*` columns snapshot the book's series state at the
-- moment the suggestion was generated, so the UI can render
-- "currently: X → suggested: Y" diffs without re-reading the books
-- row (which may have changed by review time).
CREATE TABLE IF NOT EXISTS book_series_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL UNIQUE,
    suggested_series_name TEXT,
    suggested_series_index REAL,
    sources_agreeing TEXT NOT NULL,
    current_series_name TEXT,
    current_series_index REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at REAL,
    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
);

-- v2.3.0 dual-source-of-truth metadata tables. See
-- docs/v23_metadata_design.md for the full design rationale.
--
-- Each Calibre/ABS sync writes a frozen snapshot of every field per
-- book. The `books` row is the editable Seshat-live view that drifts
-- via enrichment + manual edits. The Compare/Metadata Manager UI
-- reads both sides to surface diffs for review.
CREATE TABLE IF NOT EXISTS books_calibre_snapshot (
    book_id INTEGER PRIMARY KEY,
    title TEXT,
    -- JSON array of {id, name, sort} from Calibre's authors table.
    -- Stored denormalized rather than FK'd so the snapshot stays a
    -- faithful reproduction of Calibre's view, independent of how
    -- Seshat resolves author identity (pen-name links, normalized
    -- name dedup, etc.).
    authors_json TEXT,
    series_name TEXT,
    series_index REAL,
    isbn TEXT,
    cover_path TEXT,
    description TEXT,
    tags TEXT,
    rating INTEGER,
    language TEXT,
    publisher TEXT,
    formats TEXT,
    pubdate TEXT,
    synced_at REAL NOT NULL,
    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS books_abs_snapshot (
    book_id INTEGER PRIMARY KEY,
    title TEXT,
    authors_json TEXT,
    series_name TEXT,
    series_index REAL,
    narrator TEXT,
    duration_sec REAL,
    abridged INTEGER,
    asin TEXT,
    description TEXT,
    tags TEXT,
    cover_path TEXT,
    language TEXT,
    publisher TEXT,
    audio_formats TEXT,
    pubdate TEXT,
    synced_at REAL NOT NULL,
    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
);

-- Unified review queue for diffs the user hasn't decided on yet.
-- Replaces book_series_suggestions semantically (the Suggestions UI
-- folds into Metadata Manager). One row per (book, field, source)
-- triple — a fresh proposal from the same source overwrites prior
-- ones via UPSERT, so the queue doesn't grow unboundedly under
-- repeated scans.
CREATE TABLE IF NOT EXISTS metadata_review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    source TEXT NOT NULL,
    proposed_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(book_id, field, source),
    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_books_author ON books(author_id);
CREATE INDEX IF NOT EXISTS idx_books_series ON books(series_id);
CREATE INDEX IF NOT EXISTS idx_books_owned ON books(owned);
CREATE INDEX IF NOT EXISTS idx_books_new ON books(is_new);
CREATE INDEX IF NOT EXISTS idx_books_hidden ON books(hidden);
CREATE INDEX IF NOT EXISTS idx_authors_name ON authors(name);
CREATE INDEX IF NOT EXISTS idx_books_mam_status ON books(mam_status);
-- Composite index for the most common combined filter across the app:
-- "all owned (or missing) books for a given author". Used heavily by
-- the author-detail page and the lookup-merge pass that runs once per
-- author during source scans.
CREATE INDEX IF NOT EXISTS idx_books_author_owned ON books(author_id, owned);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON book_series_suggestions(status);
CREATE INDEX IF NOT EXISTS idx_suggestions_book ON book_series_suggestions(book_id);
CREATE INDEX IF NOT EXISTS idx_review_queue_book ON metadata_review_queue(book_id);
CREATE INDEX IF NOT EXISTS idx_review_queue_source ON metadata_review_queue(source);
"""

# Migrations for existing databases
MIGRATIONS = [
    "ALTER TABLE books ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE books ADD COLUMN cover_path TEXT",
    "ALTER TABLE authors ADD COLUMN verified INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE authors ADD COLUMN fantasticfiction_id TEXT",
    "ALTER TABLE authors ADD COLUMN fictiondb_id TEXT",
    "ALTER TABLE series ADD COLUMN fantasticfiction_id TEXT",
    "ALTER TABLE series ADD COLUMN fictiondb_id TEXT",
    "ALTER TABLE books ADD COLUMN fantasticfiction_id TEXT",
    "ALTER TABLE books ADD COLUMN fictiondb_id TEXT",
    "ALTER TABLE books ADD COLUMN expected_date TEXT",
    "ALTER TABLE books ADD COLUMN is_unreleased INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE books ADD COLUMN language TEXT",
    "ALTER TABLE books ADD COLUMN rating REAL",
    "ALTER TABLE books ADD COLUMN tags TEXT",
    "ALTER TABLE books ADD COLUMN publisher TEXT",
    "ALTER TABLE books ADD COLUMN formats TEXT",
    "ALTER TABLE books ADD COLUMN source_url TEXT",
    "CREATE INDEX IF NOT EXISTS idx_books_hidden ON books(hidden)",
    "ALTER TABLE books ADD COLUMN mam_url TEXT",
    "ALTER TABLE books ADD COLUMN mam_status TEXT",
    "ALTER TABLE books ADD COLUMN mam_formats TEXT",
    "ALTER TABLE books ADD COLUMN mam_torrent_id TEXT",
    "ALTER TABLE books ADD COLUMN mam_has_multiple INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_books_mam_status ON books(mam_status)",
    "ALTER TABLE books ADD COLUMN mam_my_snatched INTEGER NOT NULL DEFAULT 0",
    # v1.1.5 accidentally placed `mam_category` here as a middle
    # insertion — silently skipped on every upgraded DB because the
    # runner keys on `PRAGMA user_version` = count of applied entries.
    # This slot now stays as a deliberate no-op to preserve index
    # alignment. The real migration is appended at the end of the list.
    # Running it twice on a fresh DB is safe: the error handler catches
    # "duplicate column". Never remove this entry — doing so shifts
    # every downstream index by one and re-breaks upgrades.
    "ALTER TABLE books ADD COLUMN mam_category TEXT",
    "CREATE INDEX IF NOT EXISTS idx_books_author_owned ON books(author_id, owned)",
    # ── FantasticFiction removal ─────────────────────────────────
    # FF was dropped as a source entirely (it duplicated coverage of
    # Goodreads/Hardcover/Kobo and was Cloudflare-blocked anyway). Null
    # any leftover IDs first, then drop the columns. SQLite 3.35+ is
    # required for DROP COLUMN; the migration loop tolerates "no such
    # column" and other expected errors via its existing exception
    # handling, so re-running on a fresh DB (where columns were never
    # added) is safe.
    "UPDATE authors SET fantasticfiction_id = NULL",
    "UPDATE series SET fantasticfiction_id = NULL",
    "UPDATE books SET fantasticfiction_id = NULL",
    "ALTER TABLE authors DROP COLUMN fantasticfiction_id",
    "ALTER TABLE series DROP COLUMN fantasticfiction_id",
    "ALTER TABLE books DROP COLUMN fantasticfiction_id",
    # ── Source-consensus series suggestions table ─────────────────
    # See SCHEMA above for the full lifecycle doc. Indexes are
    # created via the SCHEMA index block at startup, not here.
    """CREATE TABLE IF NOT EXISTS book_series_suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL UNIQUE,
        suggested_series_name TEXT,
        suggested_series_index REAL,
        sources_agreeing TEXT NOT NULL,
        current_series_name TEXT,
        current_series_index REAL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
        updated_at REAL,
        FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
    )""",
    # ── Orphan series cleanup ────────────────────────────────────
    # One-shot cleanup of phantom series rows that older scans could
    # leave behind when every book in a series got filtered out by
    # owned-only mode. Lookup's lazy series upsert prevents new
    # orphans, so this exists only to scrub historical residue.
    # Idempotent — re-running deletes nothing.
    "DELETE FROM series WHERE id NOT IN (SELECT DISTINCT series_id FROM books WHERE series_id IS NOT NULL)",
    # mam_scan_log.book_ids_snapshot column for the full MAM scan ID
    # snapshot. Tolerated by the migration loop's "duplicate column"
    # handler if it's already present.
    "ALTER TABLE mam_scan_log ADD COLUMN book_ids_snapshot TEXT",
    # Amazon source — add amazon_id columns for author/series/book tracking
    "ALTER TABLE authors ADD COLUMN amazon_id TEXT",
    "ALTER TABLE series ADD COLUMN amazon_id TEXT",
    "ALTER TABLE books ADD COLUMN amazon_id TEXT",
    # Omnibus flag — marks compilations/box-sets that should display
    # separately from numbered series entries (don't shift numbering).
    "ALTER TABLE books ADD COLUMN is_omnibus INTEGER NOT NULL DEFAULT 0",
    # Pen-name linking: maps author aliases to a canonical author.
    # When two authors are linked, source scans for either one check
    # owned books under BOTH for dedup and series matching.
    """CREATE TABLE IF NOT EXISTS pen_name_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_author_id INTEGER NOT NULL,
        alias_author_id INTEGER NOT NULL,
        created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
        FOREIGN KEY (canonical_author_id) REFERENCES authors(id) ON DELETE CASCADE,
        FOREIGN KEY (alias_author_id) REFERENCES authors(id) ON DELETE CASCADE,
        UNIQUE(canonical_author_id, alias_author_id)
    )""",
    # IBDB + Google Books sources — MUST be after pen_name_links (v40)
    # since existing DBs already have user_version=40 from Sprint 3.
    "ALTER TABLE books ADD COLUMN ibdb_id TEXT",
    "ALTER TABLE books ADD COLUMN google_books_id TEXT",
    # Repair: ensure ibdb_id exists on DBs that hit the reordering bug
    # (Sprint 4 initially placed ibdb_id before pen_name_links, which
    # caused it to be skipped on v40 DBs). Idempotent — "duplicate
    # column" is caught by the migration error handler.
    "ALTER TABLE books ADD COLUMN ibdb_id TEXT",
    # Sprint 7 — link_type discriminates pen-name links from
    # co-author links. Backend treats both identically (dedup books
    # across linked authors, scan as one identity). The label is
    # purely UX so the user can tell J.N. Chaney's co-author chain
    # ("with Christopher Hopper") apart from Arand ↔ Darren.
    "ALTER TABLE pen_name_links ADD COLUMN link_type TEXT NOT NULL DEFAULT 'pen_name'",
    # v1.1.5: MAM category captured during scan + forwarded to the pipeline.
    # Reminder for future migrations: this list is APPEND-ONLY — the
    # runner keys on PRAGMA user_version, which is the count of entries
    # applied. Inserting anywhere except the end means the new entry's
    # index falls below existing users' user_version and the migration
    # never runs. v1.1.5 initially put this inline with the other mam_*
    # entries, got silently skipped on every DB past v44, surfaced as
    # "no such column: mam_category" on the first MAM scan post-update.
    "ALTER TABLE books ADD COLUMN mam_category TEXT",
    # v1.1.9: authors table was missing ibdb_id / google_books_id —
    # columns landed on `books` in Sprint 4 but never on `authors`.
    # lookup.py's UPDATE authors SET {source}_id=? pattern raised
    # "no such column: ibdb_id" on every ibdb scan. google_books hit
    # the same path but was rate-limited out before it ever tried to
    # write, so the bug only surfaced via ibdb.
    "ALTER TABLE authors ADD COLUMN ibdb_id TEXT",
    "ALTER TABLE authors ADD COLUMN google_books_id TEXT",
    # Audiobookshelf integration — audiobook-specific columns. Null on
    # ebook-library DBs (Calibre), populated on ABS-library DBs. Keeps
    # a single schema across both library types so cross-library
    # matching queries (Phase 2) don't have to branch on schema shape.
    #   audiobookshelf_id — ABS library item UUID (stable across rescans)
    #   asin              — Audible ASIN (Amazon Standard Identification Number)
    #   narrator          — comma-separated, mirrors `authors` flattening
    #   duration_sec      — total runtime in seconds (float for fractional)
    #   abridged          — 0/1 flag; MAM & ABS both carry this bit
    #   audio_formats     — comma-separated extensions (m4b, mp3, m4a)
    "ALTER TABLE books ADD COLUMN audiobookshelf_id TEXT",
    "ALTER TABLE books ADD COLUMN asin TEXT",
    "ALTER TABLE books ADD COLUMN narrator TEXT",
    "ALTER TABLE books ADD COLUMN duration_sec REAL",
    "ALTER TABLE books ADD COLUMN abridged INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE books ADD COLUMN audio_formats TEXT",
    "ALTER TABLE authors ADD COLUMN audiobookshelf_id TEXT",
    "ALTER TABLE series ADD COLUMN audiobookshelf_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_books_abs ON books(audiobookshelf_id)",
    "CREATE INDEX IF NOT EXISTS idx_books_asin ON books(asin)",
    # Author-row dedup — Calibre can hold two separate author records
    # for the same person (e.g. "A. K. DuBoff" calibre_id=254 +
    # "A K DuBoff" calibre_id=1179) when books were imported at
    # different times with different punctuation. The Calibre UI
    # hides the duplicates but the metadata.db keeps both, and sync
    # used to mirror that into two separate Seshat rows. The new
    # `normalized_name` column (via `normalize_author_name`) groups
    # those variants so calibre_sync's upsert treats them as one.
    # Indexed because the sync upsert hits it per-author per-sync.
    "ALTER TABLE authors ADD COLUMN normalized_name TEXT",
    "CREATE INDEX IF NOT EXISTS idx_authors_normalized_name ON authors(normalized_name)",
    # ── v2.3.0 dual-source-of-truth metadata ─────────────────────
    # See docs/v23_metadata_design.md. Calibre/ABS syncs write to
    # snapshot tables; the `books` row is the editable Seshat-live
    # view. Per-book metadata source preference + per-field user-edit
    # provenance flag govern auto-flow vs review-queue routing on
    # subsequent diffs. Existing book_series_suggestions stays in
    # place during v2.3.0 (data path read-only) and folds into the
    # Metadata Manager page in v2.3.1.
    """CREATE TABLE IF NOT EXISTS books_calibre_snapshot (
        book_id INTEGER PRIMARY KEY,
        title TEXT,
        authors_json TEXT,
        series_name TEXT,
        series_index REAL,
        isbn TEXT,
        cover_path TEXT,
        description TEXT,
        tags TEXT,
        rating INTEGER,
        language TEXT,
        publisher TEXT,
        formats TEXT,
        pubdate TEXT,
        synced_at REAL NOT NULL,
        FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS books_abs_snapshot (
        book_id INTEGER PRIMARY KEY,
        title TEXT,
        authors_json TEXT,
        series_name TEXT,
        series_index REAL,
        narrator TEXT,
        duration_sec REAL,
        abridged INTEGER,
        asin TEXT,
        description TEXT,
        tags TEXT,
        cover_path TEXT,
        language TEXT,
        publisher TEXT,
        audio_formats TEXT,
        pubdate TEXT,
        synced_at REAL NOT NULL,
        FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS metadata_review_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL,
        field TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        source TEXT NOT NULL,
        proposed_at REAL NOT NULL DEFAULT (strftime('%s','now')),
        UNIQUE(book_id, field, source),
        FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_review_queue_book ON metadata_review_queue(book_id)",
    "CREATE INDEX IF NOT EXISTS idx_review_queue_source ON metadata_review_queue(source)",
    # Per-book metadata source preference + per-field user-edit map.
    # See docs/v23_metadata_design.md "Data model" section.
    "ALTER TABLE books ADD COLUMN metadata_source_pref TEXT NOT NULL DEFAULT 'seshat'",
    "ALTER TABLE books ADD COLUMN field_source_map TEXT",
    "ALTER TABLE books ADD COLUMN user_edited_fields TEXT NOT NULL DEFAULT '[]'",
    # mam_is_bundle: tags MAM results that are series/collection torrents
    # (multiple books in one upload) so the UI can show a "Series Bundle"
    # badge and the scan logic can avoid auto-promoting low-title-match
    # bundles to "Found". See _is_bundle in app/discovery/sources/mam.py.
    "ALTER TABLE books ADD COLUMN mam_is_bundle INTEGER NOT NULL DEFAULT 0",
    # mam_last_scanned_at: unix epoch seconds, stamped on successful
    # MAM scans (FOUND/POSSIBLE/NOT_FOUND). Drives the "skip recently-
    # scanned" eligibility filter — books scanned within the configured
    # window (default 7 days, see mam_recent_scan_skip_days) are
    # excluded from bulk scan eligibility so the queue front rotates
    # through the full library rather than re-evaluating the same
    # Possible/Not Found tail every cycle. Also drives oldest-first
    # ordering on the eligible set so libraries get full coverage over
    # time. Manual sidebar rescans bypass the filter (they hit
    # check_book directly, not the eligibility query).
    "ALTER TABLE books ADD COLUMN mam_last_scanned_at REAL",
    # cover_phash: 16-char hex pHash of the book's local/source cover
    # image. Compared against MAM candidate covers during scan
    # (`app.mam.cover_hash.hamming_distance`) to verify URL correctness
    # — low distance = strong promote signal. NULL when no cover or
    # hashing failed. Populated by Calibre/ABS sync hooks on cover
    # landing and by source-scan cover_url writes.
    "ALTER TABLE books ADD COLUMN cover_phash TEXT",
]


async def get_db(slug=None) -> aiosqlite.Connection:
    """Get a database connection for a specific library (or the active library).

    Args:
        slug: Library slug. If None, uses the active library.
    """
    path = get_db_path(slug)
    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    # 30s busy_timeout gives background writers (MAM scan batches,
    # author scans, UI mutations) plenty of room to wait out a
    # Calibre bulk-sync that's holding the write lock. A 2700-book
    # sync can take ~15s, so anything shorter than ~20s starts
    # producing "database is locked" errors on concurrent writers.
    # WAL mode keeps READERS unblocked regardless — this timeout
    # only matters for writer↔writer contention.
    await db.execute("PRAGMA busy_timeout=30000")
    return db


async def cleanup_empty_series(db=None):
    """Delete series rows with no associated books.

    Called after reset/clear operations that may leave orphaned series.
    If no db connection is passed, opens and closes one automatically.
    Returns the number of series rows deleted.
    """
    close_after = db is None
    if db is None:
        db = await get_db()
    try:
        cur = await db.execute(
            "DELETE FROM series WHERE id NOT IN "
            "(SELECT DISTINCT series_id FROM books WHERE series_id IS NOT NULL)"
        )
        if cur.rowcount > 0:
            await db.commit()
        return cur.rowcount
    finally:
        if close_after:
            await db.close()


# ─── Series-name normalization (mirrors lookup.py) ───────────
# Used by `_dedupe_intra_author_series` to detect series rows under
# the same author whose names canonicalize to the same form. Kept in
# this module rather than imported from `lookup.py` so the cleanup pass
# stays self-contained and the database layer has no import-cycle risk
# against the source-scan stack.
_RX_DEDUPE_LEAD = re.compile(r'^(the|a|an)\s+', re.IGNORECASE)
_RX_DEDUPE_TAIL = re.compile(
    r'\s+(saga|series|trilogy|cycle|chronicles|novels|books)\s*$',
    re.IGNORECASE,
)
_RX_DEDUPE_PUNCT = re.compile(r'[^\w\s]')


def _norm_series_name(name: str) -> str:
    """Normalize a series name for canonical-form comparison.

    Strips leading articles ("the", "a", "an"), trailing tail words
    ("Saga", "Series", "Trilogy", "Cycle", "Chronicles", "Novels",
    "Books"), all punctuation, and lowercases. Iterates the tail
    strip up to 3 times to handle stacked suffixes like "The Mistborn
    Saga Series". Returns "" for falsy input so callers can skip.
    """
    if not name:
        return ""
    n = name.strip()
    n = _RX_DEDUPE_LEAD.sub('', n)
    for _ in range(3):
        nn = _RX_DEDUPE_TAIL.sub('', n)
        if nn == n:
            break
        n = nn
    n = _RX_DEDUPE_PUNCT.sub(' ', n).lower()
    return re.sub(r'\s+', ' ', n).strip()


async def _backfill_series_index_from_title(db) -> tuple[int, int]:
    """Set `series_index` on rows whose title encodes a position the
    column lost.

    Two cases the helper repairs:
      - Source emitted "Series N: Title" / "Series Book N: Title" /
        "Title (Series #N)" but tagged the row as standalone, so the
        merge inserted with NULL series_index. The end-of-scan
        title→series pass linked the series_id but `_RX_TITLE_SERIES_IDX`
        only catches `#N`, `Book N`, or trailing `\\d+$` — none of which
        fit the prefix-style.
      - A duplicate row already sits at the canonical position; if so,
        we drop the loser (un-owned, longer "Book N"-style title, or
        higher id) instead of setting the index. Mirrors the dedup
        rules in `_title_to_series_pass`.

    Returns (rows_indexed, rows_deduped). Idempotent — runs each
    startup but the typical steady-state touch count is 0.
    """
    from app.discovery.lookup import (
        _RX_SERIES_PREFIX_TITLE,
        _RX_SERIES_PAREN_TITLE,
        _RX_BOOK_N_SUFFIX,
        _norm_consensus_series,
    )

    # Author → series-name → series_id, scoped per-author (a series
    # name belongs to one author in this DB schema).
    series_rows = await (await db.execute(
        "SELECT id, name, author_id FROM series"
    )).fetchall()
    sid_by_author_name: dict[tuple[int, str], int] = {}
    for s in series_rows:
        if not s["name"] or s["author_id"] is None:
            continue
        sid_by_author_name[(s["author_id"], s["name"].lower())] = s["id"]
        norm = _norm_consensus_series(s["name"])
        if norm:
            sid_by_author_name.setdefault((s["author_id"], norm), s["id"])

    targets = await (await db.execute(
        "SELECT id, title, author_id, series_id, owned "
        "FROM books "
        "WHERE series_id IS NOT NULL AND series_index IS NULL "
        "AND title IS NOT NULL"
    )).fetchall()

    indexed = 0
    deduped = 0
    for t in targets:
        title = t["title"]
        # Extract candidate (series_name, series_index) from the title.
        candidates: list[tuple[str, float]] = []
        mp = _RX_SERIES_PREFIX_TITLE.match(title)
        if mp:
            candidates.append((mp.group(1).strip(), float(mp.group(2))))
        mq = _RX_SERIES_PAREN_TITLE.search(title)
        if mq:
            candidates.append((mq.group(1).strip(), float(mq.group(2))))
        if not candidates:
            continue

        # Resolve at least one candidate to a known series for THIS
        # author and ensure it matches the row's already-set series_id
        # (defensive: don't relocate a row to a different series).
        idx = None
        for s_name, s_idx in candidates:
            sid = (
                sid_by_author_name.get((t["author_id"], s_name.lower()))
                or sid_by_author_name.get(
                    (t["author_id"], _norm_consensus_series(s_name))
                )
            )
            if sid == t["series_id"]:
                idx = s_idx
                break
        if idx is None:
            continue

        # Check whether another row already holds (author_id, series_id, idx).
        existing = await (await db.execute(
            "SELECT id, title, owned FROM books "
            "WHERE author_id = ? AND series_id = ? "
            "AND series_index = ? AND id != ?",
            (t["author_id"], t["series_id"], idx, t["id"]),
        )).fetchone()

        if existing is None:
            await db.execute(
                "UPDATE books SET series_index = ? WHERE id = ?",
                (idx, t["id"]),
            )
            indexed += 1
            continue

        # Same-position collision — pick a winner using the same rules
        # as `_title_to_series_pass`: owned wins, then non-Book-N
        # title, then lowest id. Higher tuple wins.
        ex_owned = int(existing["owned"] or 0)
        in_owned = int(t["owned"] or 0)
        ex_book_n = bool(_RX_BOOK_N_SUFFIX.search(existing["title"] or ""))
        in_book_n = bool(_RX_BOOK_N_SUFFIX.search(title or ""))
        ex_score = (ex_owned, 0 if ex_book_n else 1, -existing["id"])
        in_score = (in_owned, 0 if in_book_n else 1, -t["id"])
        if ex_score >= in_score:
            await db.execute("DELETE FROM books WHERE id = ?", (t["id"],))
        else:
            await db.execute("DELETE FROM books WHERE id = ?", (existing["id"],))
            await db.execute(
                "UPDATE books SET series_index = ? WHERE id = ?",
                (idx, t["id"]),
            )
        deduped += 1

    if indexed or deduped:
        await db.commit()
    return indexed, deduped


async def _backfill_omnibus_flag(db) -> int:
    """Set `is_omnibus=1` on existing rows whose title matches the omnibus pattern.

    Catches two pre-existing classes of mis-flagged rows:
      - Calibre-synced books (calibre_sync.py never calls _is_omnibus)
      - Books inserted before _RX_OMNIBUS gained a particular keyword

    Idempotent: each startup re-checks rows still at is_omnibus=0 and
    flips any that now match. The check is cheap (single indexed scan)
    and the typical steady-state touch count is 0. Also clears
    `series_index` on promoted rows so omnibus entries don't push
    other books out of position — mirrors the INSERT-path behavior
    in lookup.py._merge_result.

    The regex import lives inside the function body so the migration
    runner doesn't pay for a discovery-module import on every db get.
    Returns the number of rows touched.
    """
    from app.discovery.lookup import _RX_OMNIBUS

    rows = await (await db.execute(
        "SELECT id, title FROM books WHERE is_omnibus = 0 AND title IS NOT NULL"
    )).fetchall()
    touched = 0
    for r in rows:
        if _RX_OMNIBUS.search(r["title"] or ""):
            await db.execute(
                "UPDATE books SET is_omnibus = 1, series_index = NULL WHERE id = ?",
                (r["id"],),
            )
            touched += 1
    if touched:
        await db.commit()
    return touched


async def _backfill_normalized_author_names(db) -> int:
    """Populate `authors.normalized_name` for any rows missing it.

    Runs after the migration that adds the column. On a fresh DB the
    column exists and is populated on every INSERT; this helper only
    matters for upgraded DBs that already had authors before the
    migration ran, and for any legacy code path that still inserts
    without setting the column. Returns the number of rows touched.
    """
    from app.metadata.author_names import normalize_author_name

    rows = await (await db.execute(
        "SELECT id, name FROM authors WHERE normalized_name IS NULL OR normalized_name = ''"
    )).fetchall()
    touched = 0
    for r in rows:
        norm = normalize_author_name(r["name"] or "")
        if not norm:
            continue
        await db.execute(
            "UPDATE authors SET normalized_name = ? WHERE id = ?",
            (norm, r["id"]),
        )
        touched += 1
    if touched:
        await db.commit()
    return touched


async def _dedupe_author_rows(db) -> int:
    """Collapse author rows whose `normalized_name` matches.

    One-time cleanup mirroring the intra-author series dedup pattern.
    Triggered by Calibre holding separate records for the same person
    at different punctuation levels (e.g. calibre_id=254 "A. K. DuBoff"
    and calibre_id=1179 "A K DuBoff") which the historical sync code
    mirrored as two Seshat rows. The new `normalized_name` column
    prevents new drift on future syncs; this pass cleans up what's
    already there.

    Winner selection (option 4a):
      1. Most periods in the display name wins — matches external-
         source conventions (Goodreads renders the punctuated form).
      2. Tiebreak on most-books-attached so we keep the row that
         most of the user's library already references.
      3. Final tiebreak on lowest id (stable / deterministic).

    Reparents every FK that references `authors.id`:
      - books.author_id
      - series.author_id
      - pen_name_links.canonical_author_id
      - pen_name_links.alias_author_id

    After reparenting, the intra-author series dedup step that runs
    AFTER this function cleans up any series-row collisions the merge
    may have produced (two series with the same name now under the
    same author). Self-referencing pen_name_links rows (canonical ==
    alias post-merge) are dropped.

    Returns the number of author rows that were deleted, for logging.
    Inert on healthy databases.
    """
    rows = await (await db.execute(
        "SELECT id, name, normalized_name FROM authors "
        "WHERE normalized_name IS NOT NULL AND normalized_name != ''"
    )).fetchall()

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["normalized_name"]].append(
            {"id": r["id"], "name": r["name"]}
        )

    deleted = 0
    for norm, members in groups.items():
        if len(members) < 2:
            continue
        # Score: (period_count, book_count, -id). Sort descending —
        # the most-punctuated, most-linked, lowest-id row wins.
        scored = []
        for m in members:
            cnt_row = await (await db.execute(
                "SELECT COUNT(*) FROM books WHERE author_id = ?", (m["id"],)
            )).fetchone()
            book_count = cnt_row[0] if cnt_row else 0
            period_count = m["name"].count(".")
            scored.append((period_count, book_count, -m["id"], m))
        scored.sort(reverse=True)
        winner = scored[0][3]
        losers = [s[3] for s in scored[1:]]

        for loser in losers:
            # Reparent every FK reference before deleting the row.
            await db.execute(
                "UPDATE books SET author_id = ? WHERE author_id = ?",
                (winner["id"], loser["id"]),
            )
            await db.execute(
                "UPDATE series SET author_id = ? WHERE author_id = ?",
                (winner["id"], loser["id"]),
            )
            await db.execute(
                "UPDATE pen_name_links SET canonical_author_id = ? "
                "WHERE canonical_author_id = ?",
                (winner["id"], loser["id"]),
            )
            await db.execute(
                "UPDATE pen_name_links SET alias_author_id = ? "
                "WHERE alias_author_id = ?",
                (winner["id"], loser["id"]),
            )
            await db.execute(
                "DELETE FROM authors WHERE id = ?", (loser["id"],)
            )
            deleted += 1
            _db_logger.info(
                f"  Merged author '{loser['name']}' (id={loser['id']}) → "
                f"'{winner['name']}' (id={winner['id']}) "
                f"[normalized: {norm!r}]"
            )

        # Self-referencing pen_name_links rows post-merge: a link
        # between the winner and a former loser now points winner→
        # winner. Drop those.
        await db.execute(
            "DELETE FROM pen_name_links "
            "WHERE canonical_author_id = alias_author_id"
        )

    if deleted:
        await db.commit()
    return deleted


async def _migrate_series_author_nullable(db) -> bool:
    """Make `series.author_id` nullable if it isn't already.

    SQLite has no `ALTER COLUMN DROP NOT NULL`, so we recreate the
    table. Idempotent: PRAGMA table_info returns notnull=0 once the
    migration has run, and we early-out then.

    Runs inside a transaction with foreign_keys deferred so the
    `books.series_id → series.id` FK doesn't reject the DROP. The
    new table preserves all existing column data; row IDs are
    preserved via SELECT including `id`, so book FKs stay valid.

    Returns True if the table was rebuilt, False if no-op.
    """
    cols = await (await db.execute("PRAGMA table_info(series)")).fetchall()
    author_id_col = next((c for c in cols if c["name"] == "author_id"), None)
    if author_id_col is None:
        # Fresh DB — series table created by SCHEMA already has the
        # nullable column. Nothing to do.
        return False
    if author_id_col["notnull"] == 0:
        return False  # already nullable

    _db_logger.info(
        "Migrating series.author_id to nullable (recreating table)"
    )

    # Capture the actual column list from the live table so the
    # explicit-column INSERT stays in lockstep with whatever
    # ALTER TABLE ADD COLUMN entries were applied historically.
    col_names = [c["name"] for c in cols]
    col_list = ", ".join(col_names)

    # foreign_keys = OFF would leak — defer instead so we can still
    # detect actual FK violations on commit.
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        await db.execute("""
            CREATE TABLE series_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                author_id INTEGER,
                hardcover_id TEXT,
                goodreads_id TEXT,
                kobo_id TEXT,
                fictiondb_id TEXT,
                total_books INTEGER,
                description TEXT,
                last_lookup_at REAL,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                amazon_id TEXT,
                audiobookshelf_id TEXT,
                FOREIGN KEY (author_id) REFERENCES authors(id),
                UNIQUE(name, author_id)
            )
        """)
        await db.execute(
            f"INSERT INTO series_new ({col_list}) "
            f"SELECT {col_list} FROM series"
        )
        await db.execute("DROP TABLE series")
        await db.execute("ALTER TABLE series_new RENAME TO series")
        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys = ON")
    return True


async def _backfill_metadata_snapshots(db) -> tuple[int, int]:
    """Seed `books_calibre_snapshot` and `books_abs_snapshot` from the
    current `books` rows on first boot post-v2.3.0.

    Cold-start assumption: the existing `books` columns are a
    reasonable representation of what Calibre/ABS last said. They
    might have drift from prior source-scan overwrites, but that's
    acceptable — the next real Calibre/ABS sync corrects the snapshot.

    Idempotent: only inserts when the snapshot row doesn't exist.
    Returns `(calibre_inserted, abs_inserted)`.
    """
    import json as _json
    import time as _time

    now = _time.time()

    # Calibre snapshot — books with source='calibre' AND owned=1.
    # We pull author name via a join because the snapshot stores
    # denormalized authors_json (mirrors Calibre's POV, no
    # pen-name-resolution).
    cal_rows = await (await db.execute("""
        SELECT b.id, b.title, b.calibre_id, b.isbn, b.cover_path,
               b.description, b.tags, b.rating, b.language,
               b.publisher, b.formats, b.pub_date,
               b.series_index,
               a.name AS author_name, a.calibre_id AS author_calibre_id,
               s.name AS series_name
        FROM books b
        LEFT JOIN authors a ON a.id = b.author_id
        LEFT JOIN series s ON s.id = b.series_id
        WHERE b.source = 'calibre' AND b.owned = 1
          AND NOT EXISTS (
              SELECT 1 FROM books_calibre_snapshot cs WHERE cs.book_id = b.id
          )
    """)).fetchall()
    cal_inserted = 0
    for r in cal_rows:
        authors_json = _json.dumps([{
            "id": r["author_calibre_id"],
            "name": r["author_name"],
        }]) if r["author_name"] else None
        # Calibre stores rating 0-10 (half-star integers); our `books`
        # column uses REAL. Round to int for snapshot fidelity.
        rating_int = (
            int(round(r["rating"])) if r["rating"] is not None else None
        )
        await db.execute("""
            INSERT INTO books_calibre_snapshot
            (book_id, title, authors_json, series_name, series_index,
             isbn, cover_path, description, tags, rating, language,
             publisher, formats, pubdate, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r["id"], r["title"], authors_json, r["series_name"],
            r["series_index"], r["isbn"], r["cover_path"],
            r["description"], r["tags"], rating_int, r["language"],
            r["publisher"], r["formats"], r["pub_date"], now,
        ))
        cal_inserted += 1

    # ABS snapshot — books with audiobookshelf_id populated.
    abs_rows = await (await db.execute("""
        SELECT b.id, b.title, b.audiobookshelf_id, b.asin, b.narrator,
               b.duration_sec, b.abridged, b.description, b.tags,
               b.cover_path, b.language, b.publisher, b.audio_formats,
               b.pub_date, b.series_index,
               a.name AS author_name,
               s.name AS series_name
        FROM books b
        LEFT JOIN authors a ON a.id = b.author_id
        LEFT JOIN series s ON s.id = b.series_id
        WHERE b.audiobookshelf_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM books_abs_snapshot abs WHERE abs.book_id = b.id
          )
    """)).fetchall()
    abs_inserted = 0
    for r in abs_rows:
        authors_json = _json.dumps([{
            "id": None, "name": r["author_name"],
        }]) if r["author_name"] else None
        await db.execute("""
            INSERT INTO books_abs_snapshot
            (book_id, title, authors_json, series_name, series_index,
             narrator, duration_sec, abridged, asin, description,
             tags, cover_path, language, publisher, audio_formats,
             pubdate, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r["id"], r["title"], authors_json, r["series_name"],
            r["series_index"], r["narrator"], r["duration_sec"],
            r["abridged"], r["asin"], r["description"], r["tags"],
            r["cover_path"], r["language"], r["publisher"],
            r["audio_formats"], r["pub_date"], now,
        ))
        abs_inserted += 1

    if cal_inserted or abs_inserted:
        await db.commit()
    return cal_inserted, abs_inserted


async def _dedupe_same_series_position(db) -> int:
    """Collapse book rows that occupy the same `(series_id, series_index)`.

    The Remnant case: Mark owns "Remnant II" at series_index=2, and a
    source scan inserted "Remnant Book 2" also at series_index=2 as a
    separate book row. Both are the same book — just different title
    conventions — but they co-exist because Seshat's book-dedup was
    driven by fuzzy title match and those titles don't collide
    ("remnant book 2" vs "remnant ii" has low similarity).

    Winner selection:
      1. OWNED (owned=1) beats NEW (owned=0) — keep the user's actual
         library row.
      2. Titles without "Book N"/"Bk N" suffix win — matches the
         convention the canonical source rendered. "Remnant II" beats
         "Remnant Book 2".
      3. Stable tiebreak on lowest id.

    SAFETY GUARD (added 2026-05-09 after the Witcher case): when 2+
    candidates in the same group are OWNED (owned=1), this function
    refuses to delete any of them and logs a warning instead.
    Multiple owned books at the same series position is almost always
    a metadata error in the source-of-truth library (Calibre,
    Audiobookshelf) — e.g. Mark added "The Last Wish" and "Time of
    Contempt" to Calibre at the same series_index values that "Blood
    of Elves" and "Sword of Destiny" already occupied, so dedup was
    silently dropping a real Calibre book on every container restart
    until the next manual sync re-inserted it. Better to surface the
    collision than data-loss-by-tiebreaker; the user fixes the
    metadata in their library app.

    book_series_suggestions.book_id has ON DELETE CASCADE, so the
    loser's suggestion rows auto-drop. work_links in the pipeline DB
    become dangling but get reconciled by the next works-matcher
    run — same as every other book-delete path in discovery.

    The displayed "X of Y" series totals are live-COUNT'd in the
    series endpoint, so they self-correct once losers are deleted.
    `series.total_books` isn't consumed anywhere currently, so we
    skip maintaining it here.

    Inert on healthy databases. Returns the number of rows collapsed.
    """
    # Normalize any empty-string series_index to NULL before reading.
    # The PUT /books/{bid} edit path could store '' when the user
    # cleared the series-number input while setting a series name
    # (the omnibus-only case Mark hit on 2026-05-03). v2.2.5 added a
    # boundary coercion in `update_book`, but pre-existing DB rows
    # still need this one-time scrub or `float(r["series_index"])`
    # below crashes init_db for every library.
    await db.execute(
        "UPDATE books SET series_index = NULL "
        "WHERE series_index IS NOT NULL "
        "AND TRIM(CAST(series_index AS TEXT)) = ''"
    )
    await db.commit()

    rows = await (await db.execute(
        "SELECT id, title, author_id, series_id, series_index, owned "
        "FROM books WHERE series_id IS NOT NULL AND series_index IS NOT NULL"
    )).fetchall()

    groups: dict[tuple[int, int, float], list[dict]] = defaultdict(list)
    for r in rows:
        key = (r["author_id"], r["series_id"], float(r["series_index"]))
        groups[key].append({
            "id": r["id"],
            "title": r["title"] or "",
            "owned": int(r["owned"] or 0),
        })

    # Detect "Book N" / "Bk N" suffixes so we prefer the canonical
    # title variant. Trailing whitespace tolerated; number may be
    # decimal (rare — "Book 2.5" novellas).
    book_n_suffix_re = re.compile(
        r"\s+(book|bk)\s+\d+(\.\d+)?\s*$", re.IGNORECASE,
    )

    def _score(m: dict) -> tuple[int, int, int]:
        title_score = 0 if book_n_suffix_re.search(m["title"]) else 1
        return (m["owned"], title_score, -m["id"])

    deleted = 0
    for (_author_id, _series_id, _idx), members in groups.items():
        if len(members) < 2:
            continue
        # Owned-collision guard: keep all rows when 2+ members are
        # owned. The dedup picks ONE winner via title/id tiebreakers,
        # and applying that to two real owned books would silently
        # drop one of them. Surface the metadata conflict instead so
        # the user fixes the source-of-truth library; a warning here
        # plus the duplicate-rows-still-present state is recoverable,
        # the deletion isn't.
        owned_count = sum(1 for m in members if m["owned"])
        if owned_count >= 2:
            _db_logger.warning(
                f"Series-position collision (kept all {len(members)} "
                f"books): author_id={_author_id}, series_id={_series_id}, "
                f"index={_idx} — fix the series numbering in your "
                f"source library: "
                + ", ".join(
                    f"id={m['id']} '{m['title']}' (owned={m['owned']})"
                    for m in members
                )
            )
            continue
        scored = sorted(members, key=_score, reverse=True)
        winner = scored[0]
        losers = scored[1:]
        for loser in losers:
            await db.execute(
                "DELETE FROM books WHERE id = ?", (loser["id"],)
            )
            deleted += 1
            _db_logger.info(
                f"  Merged book '{loser['title']}' (id={loser['id']}) → "
                f"'{winner['title']}' (id={winner['id']}) "
                f"[series_id={_series_id}, index={_idx}]"
            )

    if deleted:
        await db.commit()
    return deleted


async def _dedupe_intra_author_series(db) -> int:
    """Collapse series rows under the same author whose names normalize equal.

    Walks every (author_id, normalized_name) group with 2+ rows and:
      1. Picks a canonical row — the one with the most linked books,
         tiebreaking on lowest id (stable, deterministic).
      2. Re-points every book in the duplicates at the canonical's id.
      3. Deletes the now-orphan duplicate rows.

    Inert on healthy databases (no groups means no work). Cross-author
    name collisions are intentionally LEFT ALONE because they represent
    different physical series that just happen to share a normalized
    form (e.g., one author's "Remnant" vs another's "The Remnant
    Chronicles"). Returns the number of duplicate rows that were
    collapsed, for logging.
    """
    rows = await (await db.execute(
        "SELECT id, name, author_id FROM series"
    )).fetchall()
    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for r in rows:
        norm = _norm_series_name(r["name"])
        if not norm:
            continue
        groups[(r["author_id"], norm)].append(
            {"id": r["id"], "name": r["name"]}
        )

    collapsed = 0
    for (author_id, norm), members in groups.items():
        if len(members) < 2:
            continue
        # Pick canonical: most-linked row wins, ties broken by lowest id.
        # Counting via a per-row scalar query is fine here — this loop
        # only fires for actual duplicate groups, which is a tiny set
        # on a healthy database.
        scored = []
        for m in members:
            cnt_row = await (await db.execute(
                "SELECT COUNT(*) FROM books WHERE series_id = ?", (m["id"],)
            )).fetchone()
            scored.append((cnt_row[0] if cnt_row else 0, -m["id"], m))
        scored.sort(reverse=True)
        canonical = scored[0][2]
        duplicates = [s[2] for s in scored[1:]]

        for dup in duplicates:
            await db.execute(
                "UPDATE books SET series_id = ? WHERE series_id = ?",
                (canonical["id"], dup["id"]),
            )
            await db.execute(
                "DELETE FROM series WHERE id = ?", (dup["id"],)
            )
            collapsed += 1
            _db_logger.info(
                f"  Collapsed series '{dup['name']}' (id={dup['id']}) → "
                f"'{canonical['name']}' (id={canonical['id']}) "
                f"for author_id={author_id}"
            )

    if collapsed:
        await db.commit()
    return collapsed


async def init_db(slug=None):
    """Initialize schema and run migrations for a library database.

    Uses PRAGMA user_version to track which migrations have been applied,
    so the migration loop is skipped on subsequent startups (avoiding
    redundant work and silent error swallowing).

    Adding a new migration: append to the MIGRATIONS list. The next startup
    will detect that user_version < len(MIGRATIONS) and run only the new
    entries, then update user_version.

    Args:
        slug: Library slug. If None, uses the active library / legacy path.
    """
    db = await get_db(slug)
    try:
        # ── Step 1: Read current schema version ────────────────────
        # PRAGMA user_version returns 0 for fresh databases or those that
        # were initialized before we started using version tracking.
        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        current_version = row[0] if row else 0
        target_version = len(MIGRATIONS)

        # ── Step 2: Always ensure base tables exist ────────────────
        # CREATE TABLE IF NOT EXISTS is cheap and safe — handles fresh DBs
        # without us needing a separate "first install" code path.
        tables_sql = SCHEMA.split("CREATE INDEX")[0]
        await db.executescript(tables_sql)
        await db.commit()

        # ── Step 3: Run only the migrations we haven't applied yet ─
        if current_version < target_version:
            _db_logger.info(
                f"Migrating database schema: v{current_version} → v{target_version}"
            )
            for i, migration in enumerate(MIGRATIONS):
                if i < current_version:
                    continue
                try:
                    await db.execute(migration)
                except aiosqlite.OperationalError as e:
                    # The "duplicate column" / "already exists" cases are
                    # expected when migrating a legacy database that already
                    # had columns added by the old always-run loop. Silently
                    # tolerate those, but log anything else as a warning so
                    # real migration failures don't disappear.
                    msg = str(e).lower()
                    if ("duplicate column" in msg or "already exists" in msg
                            or "no such column" in msg):
                        continue
                    _db_logger.warning(
                        f"Migration #{i} failed unexpectedly: {e} "
                        f"(SQL: {migration[:80]}...)"
                    )
            await db.commit()

            # Stamp the new version so we skip this loop next startup
            await db.execute(f"PRAGMA user_version = {target_version}")
            await db.commit()

        # ── Step 3.5: Ensure columns exist (migration-order safety net) ──
        # Some columns may have been skipped due to migration reordering
        # bugs (Sprint 4 ibdb_id issue). This runs every startup and is
        # idempotent — "duplicate column" is silently caught.
        _ensure_columns = [
            ("books", "ibdb_id", "TEXT"),
            ("books", "google_books_id", "TEXT"),
            ("books", "amazon_id", "TEXT"),
            ("books", "is_omnibus", "INTEGER NOT NULL DEFAULT 0"),
            ("pen_name_links", "link_type", "TEXT NOT NULL DEFAULT 'pen_name'"),
            ("authors", "ibdb_id", "TEXT"),
            ("authors", "google_books_id", "TEXT"),
            # v2.3.0 metadata source pref + user-edit map.
            ("books", "metadata_source_pref", "TEXT NOT NULL DEFAULT 'seshat'"),
            ("books", "field_source_map", "TEXT"),
            ("books", "user_edited_fields", "TEXT NOT NULL DEFAULT '[]'"),
        ]
        for table, col, coltype in _ensure_columns:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
                _db_logger.info(f"Added missing column {table}.{col}")
            except aiosqlite.OperationalError:
                pass  # already exists — expected

        # ── Step 4: Ensure indexes exist (cheap, idempotent) ───────
        # Indexes are always checked because adding a new index to SCHEMA
        # without a corresponding migration entry should still work.
        index_statements = [line.strip() for line in SCHEMA.split(";")
                           if "CREATE INDEX" in line]
        for idx_sql in index_statements:
            try:
                await db.execute(idx_sql)
            except aiosqlite.OperationalError as e:
                # "already exists" is the expected case for indexes
                if "already exists" not in str(e).lower():
                    _db_logger.warning(f"Index creation failed: {e}")
        await db.commit()

        # ── Step 4.4: v2.3.0 nullable series.author_id ────────────
        # Pre-v2.3.0 schema had author_id NOT NULL. Make it nullable
        # so shared series (Halo, Star Wars) can use NULL as the
        # "shared" sentinel. Idempotent — checks PRAGMA table_info
        # first and no-ops if already nullable.
        if await _migrate_series_author_nullable(db):
            _db_logger.info(
                "series.author_id is now nullable (v2.3.0 migration)"
            )

        # ── Step 4.45: Backfill cover_phash from cover_path ─────────
        # Part C cover-image MAM URL verification needs each book's
        # local cover hashed and stored once. File-based hashing is
        # fast (Pillow + DCT pHash, ~ms per cover), so eager backfill
        # of every Calibre cover_path is fine on startup. Idempotent —
        # only touches rows with NULL cover_phash. Remote-fetched
        # covers (ABS / source-discovered cover_url) populate lazily
        # via `cover_phash.ensure_cover_phash` at MAM-scan time.
        try:
            from app.discovery.cover_phash import (
                backfill_cover_phashes_from_paths,
            )
            await backfill_cover_phashes_from_paths(db)
        except Exception as e:
            # Backfill failure must not block startup — production
            # cover verification gracefully degrades to text-only
            # behavior when cover_phash is NULL.
            _db_logger.warning(f"cover_phash backfill failed: {e}")

        # ── Step 4.5: Backfill authors.normalized_name ─────────────
        # Post-migration, ensure every existing author row has a
        # non-null normalized_name so calibre_sync's normalized lookup
        # hits them on the next sync. No-op on fresh DBs (column
        # populated at INSERT time) and on already-backfilled DBs.
        touched = await _backfill_normalized_author_names(db)
        if touched:
            _db_logger.info(
                f"Backfilled normalized_name on {touched} author row(s)"
            )

        # ── Step 4.6: One-time dedup of duplicate author rows ──────
        # Collapses pre-existing duplicates created before the
        # normalized-name upsert was wired up. Runs BEFORE the series
        # dedup below because a merged pair of authors may produce
        # colliding series (e.g. "Starship of the Ancients" under
        # both rows), which the series dedup step then cleans up.
        merged_authors = await _dedupe_author_rows(db)
        if merged_authors:
            _db_logger.info(
                f"Author dedupe merged {merged_authors} duplicate row(s)"
            )

        # ── Step 5: Idempotent intra-author series dedupe ──────────
        # Collapses any historical residue where the same author has
        # multiple series rows whose names normalize to the same form
        # (e.g. "The Witcher" + "Witcher Series"). The lazy upsert in
        # lookup.py prevents NEW drift on the source-scan path, and
        # calibre_sync.py's normalized fallback prevents it on the
        # Calibre-sync path, so this cleanup is purely a one-shot
        # safety net for older installs. No-op when nothing matches.
        collapsed = await _dedupe_intra_author_series(db)
        if collapsed:
            _db_logger.info(
                f"Series dedupe collapsed {collapsed} duplicate "
                f"intra-author rows"
            )

        # ── Step 5.5: Same-series-position book dedupe ─────────────
        # Collapses duplicate book rows sharing the same
        # (author_id, series_id, series_index) — the Remnant case
        # where "Remnant II" and "Remnant Book 2" both live at index
        # 2. Runs AFTER the series dedup so any series merges that
        # just happened feed their books into this pass's grouping.
        collapsed_books = await _dedupe_same_series_position(db)
        if collapsed_books:
            _db_logger.info(
                f"Book-position dedupe collapsed {collapsed_books} "
                f"duplicate same-series-index rows"
            )

        # ── Step 6: Omnibus flag backfill ──────────────────────────
        # Idempotent rescan that flips is_omnibus=1 on books whose
        # title matches the omnibus regex but were inserted/synced
        # without the flag set (Calibre sync never sets it; older
        # source-scans inserted before _RX_OMNIBUS picked up newer
        # keywords). No-op once everything's been flagged.
        omni_touched = await _backfill_omnibus_flag(db)
        if omni_touched:
            _db_logger.info(
                f"Omnibus backfill flagged {omni_touched} previously-"
                f"unflagged book row(s)"
            )

        # ── Step 7: Series-index recovery from title ────────────────
        # Repairs rows that have series_id set but series_index NULL
        # because the source emitted them as standalone (Goodreads's
        # "(Paths of Akashic #5)" tagline is in the title but not
        # tagged in the API). Extracts the implicit index from the
        # title and either sets it on the row or — when a duplicate
        # already sits at that position — dedupes the pair.
        idx_touched, idx_deduped = await _backfill_series_index_from_title(db)
        if idx_touched or idx_deduped:
            _db_logger.info(
                f"Series-index recovery indexed {idx_touched} row(s), "
                f"deduped {idx_deduped} same-position pair(s)"
            )

        # ── Step 8: v2.3.0 metadata snapshot backfill ─────────────
        # Cold-start seed of books_calibre_snapshot / books_abs_snapshot
        # from current `books` rows. Idempotent — only inserts when no
        # snapshot row exists yet. The next real Calibre/ABS sync
        # corrects any drift between the seeded snapshot and the
        # actual source-of-truth state.
        cal_seeded, abs_seeded = await _backfill_metadata_snapshots(db)
        if cal_seeded or abs_seeded:
            _db_logger.info(
                f"Metadata snapshot backfill: {cal_seeded} Calibre + "
                f"{abs_seeded} ABS row(s) seeded"
            )
    finally:
        await db.close()
