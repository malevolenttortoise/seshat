"""
Metadata-source cache layer (v2.21.0 Phase B).

Per-source SQLite caches that decouple expensive metadata-source scans
from the user-facing lookup flow. Initially built for Amazon (whose
Akamai cooldowns make synchronous scans unreliable), the schema is
source-templated so a future v2.22.0 Goodreads cache can reuse the
same shape without a rewrite.

## Storage layout

One DB file per source, alongside the main library DBs under DATA_DIR:

    DATA_DIR / metadata_cache_amazon.db
    DATA_DIR / metadata_cache_goodreads.db    # future v2.22.0 candidate

Each per-source file holds four tables. Table names are prefixed with
the source name (`metadata_cache_<source>_<suffix>`) so they remain
self-describing even if a future consolidation lands them in one DB:

    metadata_cache_<source>_state        — one row per (author, library)
    metadata_cache_<source>_books        — one row per cached book
    metadata_cache_<source>_queue        — pending author scans
    metadata_cache_<source>_worker_state — singleton: cooldown + heartbeat

## Read/write split

- The background worker (Phase D, separate module) is the only writer.
- The cache reader (Phase C, separate module) is the only reader the
  synchronous scan flow touches. It applies user filters (language,
  format, owned-only) at read time so a settings change doesn't
  invalidate the cache.

## Migrations

PRAGMA user_version + a per-source MIGRATIONS list, same pattern as
`app/discovery/database.py`. Appending a migration = bump `len(...)`,
which becomes the new target version at next startup. The migration
loop tolerates "already exists" / "duplicate column" errors so re-
applying old migrations against a partially-built DB never breaks
startup.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import aiosqlite

from app.config import DATA_DIR

_log = logging.getLogger("seshat.discovery.metadata_cache")


# ─── Source registry ────────────────────────────────────────────


# Sources for which a cache exists. Today: Amazon. Tomorrow (v2.22.0
# candidate): Goodreads. Adding a new source =
#   (1) extend SUPPORTED_SOURCES below
#   (2) add a MIGRATIONS list for it in `_MIGRATIONS`
#   (3) wire the worker class (Phase D) — out of scope for this module
SOURCE_AMAZON = "amazon"
SUPPORTED_SOURCES: frozenset[str] = frozenset({SOURCE_AMAZON})


# Static per-source DB filename map. Used instead of an f-string
# interpolation in `get_db_path` so CodeQL's `py/path-injection`
# taint analysis can see the filename comes from a closed set, not
# from user input. The runtime check `source not in SUPPORTED_SOURCES`
# already prevents injection, but the static map makes the safety
# property analytically obvious.
_DB_FILENAMES: dict[str, str] = {
    SOURCE_AMAZON: "metadata_cache_amazon.db",
}


# Same idea for table names — the SQL builder dropped the f-string
# interpolation in favor of static-per-suffix lookups so a future
# CodeQL pass on the (currently-fine) SQL paths can't surface a
# false-positive either. Adding a new source = one new dict entry
# below + an entry in `_DB_FILENAMES` + extending SUPPORTED_SOURCES.
_TABLE_NAMES: dict[str, dict[str, str]] = {
    SOURCE_AMAZON: {
        "state": "metadata_cache_amazon_state",
        "books": "metadata_cache_amazon_books",
        "queue": "metadata_cache_amazon_queue",
        "worker_state": "metadata_cache_amazon_worker_state",
    },
}


def _table_name(source: str, suffix: str) -> str:
    """Look up the source-prefixed table name for a logical suffix.

    Pulled from a static `(source, suffix) → table_name` map rather
    than f-string interpolation so the table identifier comes from a
    closed set even if `source` somehow bypasses the upstream
    SUPPORTED_SOURCES check.
    """
    try:
        return _TABLE_NAMES[source][suffix]
    except KeyError:
        raise ValueError(
            f"unknown metadata cache table: source={source!r} suffix={suffix!r}"
        )


def state_table(source: str = SOURCE_AMAZON) -> str:
    return _table_name(source, "state")


def books_table(source: str = SOURCE_AMAZON) -> str:
    return _table_name(source, "books")


def queue_table(source: str = SOURCE_AMAZON) -> str:
    return _table_name(source, "queue")


def worker_state_table(source: str = SOURCE_AMAZON) -> str:
    return _table_name(source, "worker_state")


def get_db_path(source: str = SOURCE_AMAZON) -> Path:
    """Per-source DB file path. One file per source so backups,
    vacuums, and wipes target a single source cleanly.

    The filename is looked up from a static map (`_DB_FILENAMES`)
    rather than f-string-interpolated so `source` never flows into a
    path expression. Closes a CodeQL `py/path-injection` finding —
    the upstream `SUPPORTED_SOURCES` check already prevented
    injection at runtime, but the static lookup makes the safety
    property analytically obvious.
    """
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"unknown metadata cache source: {source!r}")
    return DATA_DIR / _DB_FILENAMES[source]


# ─── Schema migrations (per-source, source-templated) ───────────


def _build_amazon_migrations() -> list[str]:
    """Migration list for the Amazon cache. Each entry takes the schema
    from version `i` to `i+1`; the loop in `_apply_migrations` runs
    only those after the stored `PRAGMA user_version`."""
    state = state_table(SOURCE_AMAZON)
    books = books_table(SOURCE_AMAZON)
    queue = queue_table(SOURCE_AMAZON)
    worker = worker_state_table(SOURCE_AMAZON)
    return [
        # v1 — core tables.
        f"""
        CREATE TABLE IF NOT EXISTS {state} (
            author_id        TEXT NOT NULL,
            library_slug     TEXT NOT NULL,
            seshat_author_id INTEGER,
            last_scanned_at  REAL,
            last_outcome     TEXT,
            last_error       TEXT,
            book_count       INTEGER,
            schema_version   INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (author_id, library_slug)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {books} (
            author_id    TEXT NOT NULL,
            library_slug TEXT NOT NULL,
            book_asin    TEXT NOT NULL,
            title        TEXT,
            series_name  TEXT,
            series_pos   REAL,
            pub_date     TEXT,
            format       TEXT,
            language     TEXT,
            isbn         TEXT,
            cover_url    TEXT,
            raw_json     TEXT,
            cached_at    REAL NOT NULL,
            PRIMARY KEY (author_id, library_slug, book_asin),
            FOREIGN KEY (author_id, library_slug)
                REFERENCES {state}(author_id, library_slug)
                ON DELETE CASCADE
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {queue} (
            author_id            TEXT NOT NULL,
            library_slug         TEXT NOT NULL,
            seshat_author_id     INTEGER,
            priority             REAL NOT NULL DEFAULT 100,
            status               TEXT NOT NULL DEFAULT 'pending',
            next_scan_due_at     REAL NOT NULL DEFAULT 0,
            last_attempt_at      REAL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            enqueued_reason      TEXT,
            PRIMARY KEY (author_id, library_slug)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {worker} (
            id                      INTEGER PRIMARY KEY CHECK (id = 1),
            last_block_at           REAL NOT NULL DEFAULT 0,
            block_cooldown_s        REAL NOT NULL DEFAULT 600,
            consecutive_blocks      INTEGER NOT NULL DEFAULT 0,
            last_heartbeat_at       REAL,
            last_scan_completed_at  REAL,
            today_scan_count        INTEGER NOT NULL DEFAULT 0,
            today_block_count       INTEGER NOT NULL DEFAULT 0
        )
        """,
        # The worker_state table is a singleton — seed the one row.
        f"INSERT OR IGNORE INTO {worker} (id) VALUES (1)",
        # Queue order: status=pending, then highest priority, then
        # earliest due. Worker pops via this index.
        f"""
        CREATE INDEX IF NOT EXISTS idx_{queue}_priority_due
            ON {queue} (status, priority DESC, next_scan_due_at ASC)
        """,
        # ─── v2: per-author queue (UAT 2026-05-22) ─────────────────
        # Mark's observation: with v1's `(author_id, library_slug)`
        # queue PK, every author present in both calibre + abs gets
        # scanned twice — once per library — even though Amazon's
        # mediaMatrix returns all format variants in a single call.
        # That doubled the Akamai request budget for ~99.8% of the
        # backfilled queue (644/645 unique authors were in both
        # libraries). v2 collapses to PK=`author_id` only; the
        # worker now scans each author ONCE and partitions the
        # results into per-library state + book rows from one
        # response. State + books keep their (author_id, library_slug)
        # PKs since each library still needs its own scan-state row
        # and per-content-type book set (kindle in calibre, audio
        # in abs).
        #
        # We wipe the v1 contents on this migration — the worker
        # state row (singleton, id=1) survives because we don't drop
        # `worker_state`. Discovery DB authors-with-amazon_id rows
        # will repopulate the new queue on next startup backfill.
        f"DROP TABLE IF EXISTS {books}",
        f"DROP TABLE IF EXISTS {queue}",
        f"DROP TABLE IF EXISTS {state}",
        f"""
        CREATE TABLE {state} (
            author_id        TEXT NOT NULL,
            library_slug     TEXT NOT NULL,
            seshat_author_id INTEGER,
            last_scanned_at  REAL,
            last_outcome     TEXT,
            last_error       TEXT,
            book_count       INTEGER,
            schema_version   INTEGER NOT NULL DEFAULT 2,
            PRIMARY KEY (author_id, library_slug)
        )
        """,
        f"""
        CREATE TABLE {books} (
            author_id    TEXT NOT NULL,
            library_slug TEXT NOT NULL,
            book_asin    TEXT NOT NULL,
            title        TEXT,
            series_name  TEXT,
            series_pos   REAL,
            pub_date     TEXT,
            format       TEXT,
            language     TEXT,
            isbn         TEXT,
            cover_url    TEXT,
            raw_json     TEXT,
            cached_at    REAL NOT NULL,
            PRIMARY KEY (author_id, library_slug, book_asin),
            FOREIGN KEY (author_id, library_slug)
                REFERENCES {state}(author_id, library_slug)
                ON DELETE CASCADE
        )
        """,
        f"""
        CREATE TABLE {queue} (
            author_id            TEXT PRIMARY KEY,
            priority             REAL NOT NULL DEFAULT 100,
            status               TEXT NOT NULL DEFAULT 'pending',
            next_scan_due_at     REAL NOT NULL DEFAULT 0,
            last_attempt_at      REAL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            enqueued_reason      TEXT
        )
        """,
        f"""
        CREATE INDEX IF NOT EXISTS idx_{queue}_priority_due
            ON {queue} (status, priority DESC, next_scan_due_at ASC)
        """,
    ]


_MIGRATIONS: dict[str, list[str]] = {
    SOURCE_AMAZON: _build_amazon_migrations(),
}


# ─── DB lifecycle ──────────────────────────────────────────────
#
# Per-call connections (matches the conventions in
# `app/discovery/database.py:get_db`). Callers `async with` the
# returned connection or `close()` it themselves. WAL + foreign-keys +
# busy_timeout get set on every connection; migrations run once via
# `init_db` at startup.


async def get_db(source: str = SOURCE_AMAZON) -> aiosqlite.Connection:
    """Return a fresh aiosqlite connection to the source cache DB.

    The connection has WAL journaling and FK enforcement enabled and a
    30s busy_timeout (mirrors discovery DB pattern). Caller is
    responsible for `close()`-ing — the cache reader (Phase C) and
    worker (Phase D) wrap their uses in try/finally just like the rest
    of Seshat's DB callers do.
    """
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"unknown metadata cache source: {source!r}")
    db_path = get_db_path(source)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=30000")
    return db


async def init_db(source: str = SOURCE_AMAZON) -> None:
    """Apply pending schema migrations for the per-source cache DB.

    Runs once at startup (called from lifespan after libraries are
    discovered). Subsequent calls are fast no-ops — PRAGMA user_version
    is consulted before any schema change.
    """
    db = await get_db(source)
    try:
        await _apply_migrations(db, source)
        await db.commit()
    finally:
        await db.close()


async def _apply_migrations(
    db: aiosqlite.Connection, source: str,
) -> None:
    """Bring the per-source schema up to date.

    Mirrors the conventions in `app/discovery/database.py:init_db`:
    PRAGMA user_version is the gate, MIGRATIONS list is the source of
    truth, "already exists" / "duplicate column" / "no such column"
    errors are silently tolerated so re-running against a partially-
    built DB never blocks startup.
    """
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    current = row[0] if row else 0
    migrations = _MIGRATIONS.get(source, [])
    target = len(migrations)
    if current >= target:
        return
    _log.info(
        "metadata_cache: migrating %s schema v%d → v%d",
        source, current, target,
    )
    for i, sql in enumerate(migrations):
        if i < current:
            continue
        try:
            await db.execute(sql)
        except aiosqlite.OperationalError as exc:
            msg = str(exc).lower()
            if (
                "duplicate column" in msg or "already exists" in msg
                or "no such column" in msg
            ):
                continue
            _log.warning(
                "metadata_cache: migration #%d for source=%s failed: %s "
                "(SQL: %s...)",
                i, source, exc, sql.strip()[:80],
            )
    await db.execute(f"PRAGMA user_version = {target}")


# ─── Queue backfill (v2.21.0 Phase B step 8) ────────────────────


async def backfill_amazon_queue_from_authors(
    library_slugs: Iterable[str],
    *,
    default_priority: float = 100.0,
    enqueued_reason: str = "v2210_backfill",
) -> dict[str, int]:
    """Enqueue every author with a stored ``amazon_id`` for a future
    worker scan.

    v2 schema (UAT 2026-05-22): queue PK is `author_id` only — the
    same Amazon Author Store ID across multiple libraries collapses
    to ONE queue row. The worker reads the per-library authors rows
    at scan time to partition the single Amazon response into
    per-library state + book rows.

    Returns per-library counts of *NEW* queue rows enqueued. An author
    that's in both calibre + abs only counts ONCE in the per-library
    total of whichever library was iterated first — but the total
    across libraries reflects accurate dedupe (sum of counts =
    unique-amazon-ids-newly-enqueued, not unique-authors-per-library).
    Idempotent: re-running enqueues 0 new rows.
    """
    from app.discovery.database import get_db as get_discovery_db
    cache_db = await get_db(SOURCE_AMAZON)
    qt = queue_table(SOURCE_AMAZON)
    counts: dict[str, int] = {}
    try:
        for slug in library_slugs:
            try:
                disc = await get_discovery_db(slug=slug)
            except Exception as exc:
                _log.warning(
                    "metadata_cache backfill: cannot open discovery DB %r (%s)",
                    slug, exc,
                )
                counts[slug] = 0
                continue
            try:
                cursor = await disc.execute(
                    "SELECT id, amazon_id FROM authors "
                    "WHERE amazon_id IS NOT NULL AND amazon_id != ''",
                )
                rows = await cursor.fetchall()
            except Exception as exc:
                _log.warning(
                    "metadata_cache backfill: read failed for %r (%s)",
                    slug, exc,
                )
                counts[slug] = 0
                continue
            finally:
                try:
                    await disc.close()
                except Exception:
                    pass
            if not rows:
                counts[slug] = 0
                continue
            before_cur = await cache_db.execute(
                f"SELECT COUNT(*) FROM {qt}"
            )
            before = (await before_cur.fetchone())[0]
            await cache_db.executemany(
                f"INSERT OR IGNORE INTO {qt} "
                f"(author_id, priority, enqueued_reason, next_scan_due_at) "
                f"VALUES (?, ?, ?, ?)",
                [
                    (
                        str(row[1]),
                        default_priority, enqueued_reason, 0.0,
                    )
                    for row in rows
                ],
            )
            await cache_db.commit()
            after_cur = await cache_db.execute(
                f"SELECT COUNT(*) FROM {qt}"
            )
            after = (await after_cur.fetchone())[0]
            counts[slug] = after - before
            if counts[slug]:
                _log.info(
                    "metadata_cache backfill: enqueued %d amazon authors "
                    "while iterating library %r (queue %d → %d, deduped "
                    "across libraries)",
                    counts[slug], slug, before, after,
                )
    finally:
        await cache_db.close()
    return counts


# ─── Read helpers (used by Database Manager surface + tests) ────


async def db_summary(source: str = SOURCE_AMAZON) -> dict[str, object]:
    """Lightweight summary of the per-source cache, exposed to the
    Database Manager UI.

    Returns size_bytes, last_modified, row_counts per table. Read-only;
    safe to call any time. Returns ``size_bytes=0`` and empty counts
    when the DB hasn't been opened yet (no file on disk).
    """
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"unknown metadata cache source: {source!r}")
    db_path = get_db_path(source)
    size_bytes = 0
    last_modified: float | None = None
    if db_path.exists():
        stat = db_path.stat()
        size_bytes = int(stat.st_size)
        last_modified = stat.st_mtime
    counts: dict[str, int] = {}
    if db_path.exists():
        db = await get_db(source)
        try:
            for suffix in ("state", "books", "queue", "worker_state"):
                table = _table_name(source, suffix)
                try:
                    cur = await db.execute(f"SELECT COUNT(*) FROM {table}")
                    row = await cur.fetchone()
                    counts[table] = int(row[0]) if row else 0
                except aiosqlite.OperationalError:
                    # Schema not yet migrated (init_db hasn't run, or the
                    # table list grew). Report 0 rather than 500.
                    counts[table] = 0
        finally:
            await db.close()
    return {
        "source": source,
        "db_path": str(db_path),
        "size_bytes": size_bytes,
        "last_modified": last_modified,
        "row_counts": counts,
    }
