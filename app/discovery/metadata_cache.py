"""
Metadata-source cache layer (v2.21.0 Phase B; extended to Goodreads in
v3.4.0 as a list-page cache, see ADR-0018).

Per-source SQLite caches that decouple expensive metadata-source scans
from the user-facing lookup flow. Initially built for Amazon (whose
Akamai cooldowns make synchronous scans unreliable); extended in
v3.4.0 to Goodreads, which has a different cost shape (no hard wall —
soft throughput pressure from 5–7s/book detail-fetch pacing) and so
gets a list-page-only cache rather than a per-book detail cache.

## Storage layout

One DB file per source, alongside the main library DBs under DATA_DIR:

    DATA_DIR / metadata_cache_amazon.db
    DATA_DIR / metadata_cache_goodreads.db    # v3.4.0

Each per-source file holds four tables. Table names are prefixed with
the source name (`metadata_cache_<source>_<suffix>`) so they remain
self-describing even if a future consolidation lands them in one DB.

The per-table SHAPE differs by source — Amazon caches full per-book
detail (`_books`), Goodreads caches only the author-list-page book-ID
inventory (`_list_pages`). Both share `_state`, `_queue`, and
`_worker_state` shapes one-for-one.

    metadata_cache_amazon_state          — one row per (author, library)
    metadata_cache_amazon_books          — one row per cached book
    metadata_cache_amazon_queue          — pending author scans
    metadata_cache_amazon_worker_state   — singleton: cooldown + heartbeat

    metadata_cache_goodreads_state       — one row per (author, library)
    metadata_cache_goodreads_list_pages  — one row per (author, library, page)
    metadata_cache_goodreads_queue       — pending author scans
    metadata_cache_goodreads_worker_state — singleton: cooldown + heartbeat

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
from typing import Iterable, Optional

import aiosqlite

from app.config import DATA_DIR

_log = logging.getLogger("seshat.discovery.metadata_cache")


# ─── Source registry ────────────────────────────────────────────


# Sources for which a cache exists. v3.4.0 adds Goodreads alongside
# the original Amazon cache. Adding a new source =
#   (1) extend SUPPORTED_SOURCES below
#   (2) add a MIGRATIONS list for it in `_MIGRATIONS`
#   (3) wire the worker class (separate module) — out of scope here
SOURCE_AMAZON = "amazon"
SOURCE_GOODREADS = "goodreads"
SUPPORTED_SOURCES: frozenset[str] = frozenset({SOURCE_AMAZON, SOURCE_GOODREADS})


# Static per-source DB filename map. Used instead of an f-string
# interpolation in `get_db_path` so CodeQL's `py/path-injection`
# taint analysis can see the filename comes from a closed set, not
# from user input. The runtime check `source not in SUPPORTED_SOURCES`
# already prevents injection, but the static map makes the safety
# property analytically obvious.
_DB_FILENAMES: dict[str, str] = {
    SOURCE_AMAZON: "metadata_cache_amazon.db",
    SOURCE_GOODREADS: "metadata_cache_goodreads.db",
}


# Same idea for table names — the SQL builder dropped the f-string
# interpolation in favor of static-per-suffix lookups so a future
# CodeQL pass on the (currently-fine) SQL paths can't surface a
# false-positive either. Adding a new source = one new dict entry
# below + an entry in `_DB_FILENAMES` + extending SUPPORTED_SOURCES.
#
# Per-source SHAPES diverge intentionally: Amazon caches per-book
# detail (`books`), Goodreads caches only the author-list-page
# inventory (`list_pages`). See ADR-0018.
_TABLE_NAMES: dict[str, dict[str, str]] = {
    SOURCE_AMAZON: {
        "state": "metadata_cache_amazon_state",
        "books": "metadata_cache_amazon_books",
        "queue": "metadata_cache_amazon_queue",
        "worker_state": "metadata_cache_amazon_worker_state",
    },
    SOURCE_GOODREADS: {
        "state": "metadata_cache_goodreads_state",
        "list_pages": "metadata_cache_goodreads_list_pages",
        "queue": "metadata_cache_goodreads_queue",
        "worker_state": "metadata_cache_goodreads_worker_state",
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
    """Amazon-only — Goodreads caches list pages, not per-book detail.
    Calling for `goodreads` raises (see ADR-0018)."""
    return _table_name(source, "books")


def list_pages_table(source: str = SOURCE_GOODREADS) -> str:
    """Goodreads-only — Amazon's per-book detail table is `books`."""
    return _table_name(source, "list_pages")


def queue_table(source: str = SOURCE_AMAZON) -> str:
    return _table_name(source, "queue")


def worker_state_table(source: str = SOURCE_AMAZON) -> str:
    return _table_name(source, "worker_state")


def per_source_table_suffixes(source: str) -> tuple[str, ...]:
    """Logical-suffix list this source actually has, in canonical
    order. Iteration-friendly alternative to a hardcoded tuple for
    callers like `db_summary` that need to enumerate every table in
    a source's DB (which differs by shape — see ADR-0018)."""
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"unknown metadata cache source: {source!r}")
    return tuple(_TABLE_NAMES[source].keys())


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
        # ─── v3: circuit-breaker rest timestamp ─────────────────────
        # When the worker escalates to the top cooldown tier N times in
        # a row (a genuine Akamai wall, not a probationary blip), it
        # stops probing entirely and rests until this timestamp — a
        # single long uninterrupted quiet so the IP reputation can
        # decay. The global soft-block cooldown caps at 1h
        # (amazon_author_id_resolver._BLOCK_COOLDOWN_MAX_S), which is
        # too short to break the poke-while-hot oscillation that
        # prevents recovery, so the worker tracks a longer rest here.
        # 0 = breaker not engaged. Idempotent ADD COLUMN (the
        # `duplicate column` error is swallowed by _apply_migrations).
        f"ALTER TABLE {worker} ADD COLUMN circuit_breaker_until REAL NOT NULL DEFAULT 0",
        # `circuit_breaker_armed` = 1 once the breaker has tripped and
        # 0 again after the next successful scan. It stages the rest:
        # a re-trip while still armed (no clean scan since) means "still
        # walled after resting", so the worker escalates from the short
        # stage-1 rest to a rest-until-next-window backoff.
        f"ALTER TABLE {worker} ADD COLUMN circuit_breaker_armed INTEGER NOT NULL DEFAULT 0",
    ]


def _build_goodreads_migrations() -> list[str]:
    """Migration list for the Goodreads list-page cache (v3.4.0).

    Shape mirrors Amazon's v2 schema for `state`/`queue`/`worker_state`
    (so worker telemetry + queue mechanics stay source-agnostic), but
    swaps Amazon's per-book `books` table for a list-page-snapshot
    `list_pages` table — Goodreads caches the author-list-page
    book-ID inventory only, not per-book detail (ADR-0018).

    Queue PK is `author_id` (not `(author_id, library_slug)`) —
    matches Amazon's v2 collapse: an author present in both calibre +
    abs gets scanned once. GR has no cross-library variance to
    reconcile (list pages are global per Goodreads author), so per-
    library state rows still capture which libraries enqueued the
    scan but the scan itself runs once.

    Cooldown defaults diverge from Amazon — GR's soft 202/503 backoff
    starts at 300s (vs Amazon's 600s Akamai-tuned default). The
    worker (slice 03) refines this per-tick.
    """
    state = state_table(SOURCE_GOODREADS)
    list_pages = list_pages_table(SOURCE_GOODREADS)
    queue = queue_table(SOURCE_GOODREADS)
    worker = worker_state_table(SOURCE_GOODREADS)
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
        CREATE TABLE IF NOT EXISTS {list_pages} (
            author_id     TEXT NOT NULL,
            library_slug  TEXT NOT NULL,
            page_num      INTEGER NOT NULL,
            fetched_at    REAL NOT NULL,
            book_ids_json TEXT NOT NULL,
            PRIMARY KEY (author_id, library_slug, page_num),
            FOREIGN KEY (author_id, library_slug)
                REFERENCES {state}(author_id, library_slug)
                ON DELETE CASCADE
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {queue} (
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
        f"""
        CREATE TABLE IF NOT EXISTS {worker} (
            id                      INTEGER PRIMARY KEY CHECK (id = 1),
            last_block_at           REAL NOT NULL DEFAULT 0,
            block_cooldown_s        REAL NOT NULL DEFAULT 300,
            consecutive_blocks      INTEGER NOT NULL DEFAULT 0,
            last_heartbeat_at       REAL,
            last_scan_completed_at  REAL,
            today_scan_count        INTEGER NOT NULL DEFAULT 0,
            today_block_count       INTEGER NOT NULL DEFAULT 0
        )
        """,
        f"INSERT OR IGNORE INTO {worker} (id) VALUES (1)",
        # v3.4.0 slice 05 (migration v7) — budget-exhaust counter for
        # the daily-summary ntfy. Path A's wall-clock budget can
        # silently drop ~37% of a Sanderson-class author's books;
        # this counter lifts that signal from log-grep into operator-
        # visible telemetry, giving the v3.5.0 Path C decision a
        # data point. Idempotent: ADD COLUMN tolerates re-runs via
        # the `duplicate column` swallowed error in `_apply_migrations`.
        f"ALTER TABLE {worker} "
        f"ADD COLUMN today_budget_exhaust_count INTEGER NOT NULL DEFAULT 0",
    ]


_MIGRATIONS: dict[str, list[str]] = {
    SOURCE_AMAZON: _build_amazon_migrations(),
    SOURCE_GOODREADS: _build_goodreads_migrations(),
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


async def backfill_goodreads_queue_from_authors(
    library_slugs: Iterable[str],
    *,
    default_priority: float = 100.0,
    enqueued_reason: str = "v360_backfill",
) -> dict[str, int]:
    """Enqueue every author with a stored ``goodreads_id`` for a future
    GR cache worker scan.

    Mirror of ``backfill_amazon_queue_from_authors`` for the Goodreads
    list-page cache (ADR-0018). Closes the v3.4.0 deferral noted at
    ``main.py:885`` ("No queue backfill yet — slice 04 will populate
    the queue via cache-miss enqueues from lookup.py"): on a steady-
    state install the cache-miss path never fires unless discovery
    scans are actively running, so the queue stays empty forever.
    This boot-time one-shot fills it from the authors table the same
    way Amazon does.

    Queue PK is ``author_id`` only (matches the Amazon shape per ADR-
    0018 §2): the same GR author across multiple libraries collapses
    to ONE queue row. The worker reads per-library author rows at
    scan time to partition the single list-page response into per-
    library state + list_pages rows.

    Returns per-library counts of *NEW* queue rows enqueued.
    Idempotent — re-running enqueues 0 new rows.
    """
    from app.discovery.database import get_db as get_discovery_db
    cache_db = await get_db(SOURCE_GOODREADS)
    qt = queue_table(SOURCE_GOODREADS)
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
                    "SELECT id, goodreads_id FROM authors "
                    "WHERE goodreads_id IS NOT NULL AND goodreads_id != ''",
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
                    "metadata_cache backfill: enqueued %d goodreads authors "
                    "while iterating library %r (queue %d → %d, deduped "
                    "across libraries)",
                    counts[slug], slug, before, after,
                )
    finally:
        await cache_db.close()
    return counts


async def backfill_queues_for_library(
    slug: str, *, settings: Optional[dict] = None,
) -> dict[str, int]:
    """Run both Amazon + Goodreads queue backfills against one library.

    v3.6.2 — wired into the end-of-Calibre-sync and end-of-ABS-sync
    hooks so newly-added authors land in the worker queues
    immediately rather than waiting for the next container restart.
    Idempotent (the underlying ``backfill_*_queue_from_authors``
    functions use ``INSERT OR IGNORE`` on the queue PK), so safe to
    call repeatedly. Best-effort: each source's failure is logged
    and swallowed so a stale Goodreads enrichment can't tank Amazon
    enqueuing (or vice versa).

    Goodreads backfill is gated on the same
    ``settings.metadata_cache.goodreads.mode != "disabled"`` check
    used at startup so the call is a no-op when the operator hasn't
    opted into GR caching.

    Returns ``{"amazon": N, "goodreads": M}`` — N and M are the
    counts of NEW queue rows enqueued (0 when nothing new).
    """
    from app import config as _app_config
    s = settings or _app_config.load_settings()
    out: dict[str, int] = {"amazon": 0, "goodreads": 0}
    try:
        amz_counts = await backfill_amazon_queue_from_authors([slug])
        out["amazon"] = int(amz_counts.get(slug, 0) or 0)
    except Exception:
        _log.exception(
            "backfill_queues_for_library[amazon][%s] failed (non-fatal)",
            slug,
        )
    try:
        gr_mode = (
            (s.get("metadata_cache") or {}).get("goodreads") or {}
        ).get("mode", "disabled")
        if gr_mode != "disabled":
            gr_counts = await backfill_goodreads_queue_from_authors([slug])
            out["goodreads"] = int(gr_counts.get(slug, 0) or 0)
    except Exception:
        _log.exception(
            "backfill_queues_for_library[goodreads][%s] failed (non-fatal)",
            slug,
        )
    return out


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
            # Source-shape-aware iteration — amazon has `books`, goodreads
            # has `list_pages`; per_source_table_suffixes gives each
            # source's actual table list (see ADR-0018).
            for suffix in per_source_table_suffixes(source):
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


async def is_goodreads_id_known_unavailable(goodreads_id: str) -> bool:
    """True iff the GR cache has at least one state row stamped
    `unavailable_404` for `goodreads_id`.

    Mirrors the MAM "torrent deleted" lookup pattern (ADR-0006) for
    Goodreads. Callers (the GR backfill in particular) use this to
    avoid re-stamping a known-dead author ID onto `authors.goodreads_id`
    after the cache worker has retired it.

    Returns False if the cache DB hasn't been initialized yet (no
    file on disk), which preserves backfill behavior on a fresh
    install where the cache hasn't been built.
    """
    if not goodreads_id:
        return False
    db_path = get_db_path(SOURCE_GOODREADS)
    if not db_path.exists():
        return False
    db = await get_db(SOURCE_GOODREADS)
    try:
        cur = await db.execute(
            f"SELECT 1 FROM {state_table(SOURCE_GOODREADS)} "
            f"WHERE author_id = ? AND last_outcome = 'unavailable_404' "
            f"LIMIT 1",
            (str(goodreads_id),),
        )
        row = await cur.fetchone()
        return row is not None
    except aiosqlite.OperationalError:
        # Pre-migration shape — treat as "not known dead" so backfill
        # behaves as it did before this guard was added.
        return False
    finally:
        await db.close()
