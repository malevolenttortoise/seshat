"""Operator-blacklisted source author records (v3.10.0).

Some sources collapse several real people into a single author record.
OpenLibrary's `OL2719653A` is the reference case: 41 works spanning a
sci-fi author (whose Fold novels the user owns), a political commentator
("Trump and Churchill", "Retaking America") and a children's author
("Kenny the Koala Comes to the USA").

**Why this can't be automated.** Per-source validation
(`lookup._validate_author`) asks whether ANY owned title matches ANY
title in the source's catalogue. For a collapsed record the answer is
honestly yes — the record really does contain the user's books — so
validation passes and the retract path is never reached. The ambiguity
lives *inside* one record, which is also why no cross-source signal
resolves it: measured on the reference install, the only sources
corroborating that record's junk were `google_books` and `kobo`, both of
which use the author NAME as their external id and perform no
author-entity resolution whatsoever. They corroborate the political
commentator exactly as readily as the sci-fi author.

So this is an operator verdict, recorded once per
`(source, source_author_id)` and applied on every subsequent scan. It is
deliberately narrow: blacklisting `OL2719653A` says nothing about
OpenLibrary generally, and nothing about any other author.

The cache is process-local and invalidated on write, matching
`app.discovery.roster`'s pattern.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("seshat.discovery.source_blacklist")

# Sources that resolve an author to a real, stable entity id. The rest
# (`kobo`, `google_books`, `ibdb`) use the author's NAME as the id, so
# their agreement carries no identity information — see the module
# docstring. Exposed because the UI's evidence panel weights by it.
DISAMBIGUATING_SOURCES = frozenset({
    "goodreads", "hardcover", "amazon", "openlibrary", "audible",
})

_cache: Optional[set[tuple[str, str]]] = None


def invalidate() -> None:
    """Drop the cached blacklist. Call after any write."""
    global _cache
    _cache = None


async def _load() -> set[tuple[str, str]]:
    global _cache
    if _cache is not None:
        return _cache
    from app.database import get_db
    entries: set[tuple[str, str]] = set()
    try:
        db = await get_db()
        try:
            rows = await (await db.execute(
                "SELECT source, source_author_id FROM source_author_blacklist"
            )).fetchall()
            entries = {
                (str(r["source"]).strip().lower(),
                 str(r["source_author_id"]).strip())
                for r in rows
                if r["source"] and r["source_author_id"]
            }
        finally:
            await db.close()
    except Exception:
        # A blacklist we can't read must not block discovery — fail open,
        # same contract as the language and validation paths.
        logger.exception("source-blacklist: load failed — treating as empty")
        entries = set()
    _cache = entries
    return entries


async def is_blacklisted(source: str, source_author_id: Optional[str]) -> bool:
    """True if this exact source author record is blacklisted.

    Fails open: an unreadable blacklist or a missing id returns False, so
    a storage problem can never silently suppress a whole source.
    """
    if not source or not source_author_id:
        return False
    entries = await _load()
    return (str(source).strip().lower(),
            str(source_author_id).strip()) in entries


async def add(
    source: str, source_author_id: str, *,
    author_name: Optional[str] = None,
    reason: Optional[str] = None,
    books_retracted: int = 0,
) -> int:
    """Record a blacklist verdict. Returns the row id.

    Idempotent on `(source, source_author_id)` — re-blacklisting an
    existing entry refreshes the metadata rather than raising.
    """
    from app.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO source_author_blacklist "
            "(source, source_author_id, author_name, reason, books_retracted) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source, source_author_id) DO UPDATE SET "
            "  author_name = COALESCE(excluded.author_name, author_name), "
            "  reason = COALESCE(excluded.reason, reason), "
            "  books_retracted = excluded.books_retracted",
            (str(source).strip().lower(), str(source_author_id).strip(),
             author_name, reason, int(books_retracted)),
        )
        await db.commit()
        row = await (await db.execute(
            "SELECT id FROM source_author_blacklist "
            "WHERE source = ? AND source_author_id = ?",
            (str(source).strip().lower(), str(source_author_id).strip()),
        )).fetchone()
        return int(row["id"]) if row else 0
    finally:
        await db.close()
        invalidate()


async def remove(entry_id: int) -> bool:
    """Drop a blacklist entry. Returns True if a row was removed.

    Removing does NOT restore retracted books — the next scan of that
    author re-imports them naturally, which is both simpler and more
    correct than trying to resurrect deleted rows.
    """
    from app.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "DELETE FROM source_author_blacklist WHERE id = ?", (entry_id,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()
        invalidate()


async def list_all() -> list[dict]:
    """Every blacklist entry, newest first."""
    from app.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT id, source, source_author_id, author_name, reason, "
            "books_retracted, created_at FROM source_author_blacklist "
            "ORDER BY created_at DESC, id DESC"
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()
