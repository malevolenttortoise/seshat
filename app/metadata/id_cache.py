"""
SQLite-backed cross-reference cache for ID-resolver chains.

Stores resolver results so repeat lookups don't pay another HTTP
roundtrip to Goodreads / Open Library / Hardcover. Two scopes today:

  - **`book_id`** (30-day TTL) — input is (isbn, asin) + (title, author),
    output is the resolved goodreads_book_id (or `None` if all tiers
    missed). Cached because the resolver is called per-book during
    every enrichment + per-author scan, and the chain returns the
    same answer for the same identifier for months at a time.

  - **`author_bib`** (7-day TTL) — input is a Goodreads author ID,
    output is the parsed list of books on the author's
    `/author/list/{id}` page. Cached because per-author scans
    re-fetch the same bibliography on every scheduled scan even
    when the author hasn't released anything new. The 7-day TTL is
    a compromise: authors release new books on weeks-to-months
    cadence, but Mark wants new releases to surface quickly.

Cache lives under `DATA_DIR/seshat_id_cache.db`, separate from the
main per-library books DB so deleting a library never wipes the
cross-reference cache (it'd just need to repopulate on the next
scan — wasted Goodreads requests).

Schema (single table, scoped by `scope` column):

    CREATE TABLE id_cache (
        scope       TEXT NOT NULL,    -- 'book_id' | 'author_bib'
        key         TEXT NOT NULL,    -- normalized lookup key
        value       TEXT,             -- JSON payload (None for caching misses)
        cached_at   REAL NOT NULL,    -- unix timestamp
        expires_at  REAL NOT NULL,    -- cached_at + TTL
        PRIMARY KEY (scope, key)
    )

Reads on the hot path (resolver, source scan) so this needs to be
fast: PRIMARY KEY index, no JOINs, no triggers. SQLite is fine here
— the cache table sees at most a few thousand rows for typical
libraries.

Cache misses (resolver returned None) ARE cached too, with a shorter
TTL (1 day for `book_id` misses) so we don't hammer Goodreads on
every scan trying to resolve the same dead-end ISBN. The miss-TTL
is shorter than the hit-TTL because the dataset moves: a book that
didn't resolve last week may have been added to Goodreads since.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from app.config import DATA_DIR

_log = logging.getLogger("seshat.metadata.id_cache")

# TTLs in seconds. Hard-coded ethical guardrails (per Phase 5.5 design)
# rather than user-tunable — caching is part of the "be a good citizen"
# story, not a feature dial.
_BOOK_ID_HIT_TTL = 30 * 24 * 3600       # 30 days
_BOOK_ID_MISS_TTL = 1 * 24 * 3600       # 1 day (retry soon)
_AUTHOR_BIB_HIT_TTL = 7 * 24 * 3600     # 7 days
_AUTHOR_BIB_MISS_TTL = 6 * 3600         # 6 hours (active authors retry quickly)


def _db_path() -> Path:
    return DATA_DIR / "seshat_id_cache.db"


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection. Schema is created on demand if
    missing — first call after a fresh install builds the table; all
    subsequent calls are no-ops. WAL mode is enabled so concurrent
    reads from the canary + a live scan don't block each other."""
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db), timeout=10.0, isolation_level=None)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""
            CREATE TABLE IF NOT EXISTS id_cache (
                scope       TEXT NOT NULL,
                key         TEXT NOT NULL,
                value       TEXT,
                cached_at   REAL NOT NULL,
                expires_at  REAL NOT NULL,
                PRIMARY KEY (scope, key)
            )
        """)
        yield c
    finally:
        c.close()


def _normalize_book_id_key(
    *, isbn: str = "", asin: str = "", title: str = "", author: str = ""
) -> str:
    """Deterministic cache key for the book_id scope.

    Identifier-first: if ISBN is present, use it (strongest signal).
    Falls back to ASIN, then to a normalized title+author tuple for
    queries that have neither (rare, but the resolver accepts it).
    Returns the empty string when nothing useful is present — caller
    must check before doing a cache lookup.
    """
    if isbn:
        return f"isbn:{isbn.strip().replace('-', '').lower()}"
    if asin:
        return f"asin:{asin.strip().upper()}"
    if title or author:
        return f"ta:{title.strip().lower()}|{author.strip().lower()}"
    return ""


# ─── Public API: book_id scope ──────────────────────────────────────


def get_book_id(
    *, isbn: str = "", asin: str = "", title: str = "", author: str = ""
) -> Optional[tuple[Optional[str], str]]:
    """Cache lookup. Returns None on miss, `(book_id, tier)` on hit.

    A hit where `book_id is None` is a CACHED MISS — the resolver
    previously tried and got nothing. Honor that (don't re-run the
    chain) until the cache entry expires.
    """
    key = _normalize_book_id_key(isbn=isbn, asin=asin, title=title, author=author)
    if not key:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT value, expires_at FROM id_cache WHERE scope = ? AND key = ?",
            ("book_id", key),
        ).fetchone()
    if not row:
        return None
    value_json, expires_at = row
    if expires_at < time.time():
        return None  # expired — treat as cache miss
    try:
        payload = json.loads(value_json) if value_json else None
    except (TypeError, ValueError):
        return None
    if payload is None:
        return (None, "miss")
    return (payload.get("book_id"), payload.get("tier") or "")


def put_book_id(
    *,
    isbn: str = "",
    asin: str = "",
    title: str = "",
    author: str = "",
    book_id: Optional[str],
    tier: Optional[str],
) -> None:
    """Persist a resolver outcome. `book_id=None` caches a miss
    (so subsequent identical resolves don't pay another HTTP
    roundtrip until the miss-TTL expires)."""
    key = _normalize_book_id_key(isbn=isbn, asin=asin, title=title, author=author)
    if not key:
        return
    now = time.time()
    if book_id:
        ttl = _BOOK_ID_HIT_TTL
        value_json = json.dumps({"book_id": book_id, "tier": tier or ""})
    else:
        ttl = _BOOK_ID_MISS_TTL
        value_json = None
    expires_at = now + ttl
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO id_cache "
            "(scope, key, value, cached_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("book_id", key, value_json, now, expires_at),
        )


# ─── Public API: author_bib scope ───────────────────────────────────


def get_author_bib(author_id: str) -> Optional[list[dict]]:
    """Cache lookup for an author bibliography. Returns the cached
    book list on hit, None on miss/expiry."""
    if not author_id:
        return None
    key = author_id.strip()
    with _conn() as c:
        row = c.execute(
            "SELECT value, expires_at FROM id_cache WHERE scope = ? AND key = ?",
            ("author_bib", key),
        ).fetchone()
    if not row:
        return None
    value_json, expires_at = row
    if expires_at < time.time():
        return None
    try:
        return json.loads(value_json) if value_json else None
    except (TypeError, ValueError):
        return None


def put_author_bib(author_id: str, books: Optional[list[dict]]) -> None:
    """Persist an author bibliography fetch outcome. `books=None`
    caches a miss (e.g. /author/list/{id} 404'd) with a shorter TTL."""
    if not author_id:
        return
    key = author_id.strip()
    now = time.time()
    if books:
        ttl = _AUTHOR_BIB_HIT_TTL
        value_json = json.dumps(books)
    else:
        ttl = _AUTHOR_BIB_MISS_TTL
        value_json = None
    expires_at = now + ttl
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO id_cache "
            "(scope, key, value, cached_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("author_bib", key, value_json, now, expires_at),
        )


# ─── Maintenance ────────────────────────────────────────────────────


def prune_expired() -> int:
    """Drop expired rows. Called from the weekly canary so the table
    doesn't grow unbounded for users who let their library churn
    over months. Returns the count of rows removed (for log lines)."""
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM id_cache WHERE expires_at < ?", (now,),
        )
        removed = cur.rowcount
    if removed:
        _log.info("id_cache: pruned %d expired rows", removed)
    return removed


def clear_all() -> None:
    """Drop every row. Test hook + escape hatch for users who suspect
    cache poisoning. Settings UI exposes this via a button in v2.14+."""
    with _conn() as c:
        c.execute("DELETE FROM id_cache")
