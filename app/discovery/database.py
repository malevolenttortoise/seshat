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
    author_id INTEGER NOT NULL,
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
    finally:
        await db.close()
