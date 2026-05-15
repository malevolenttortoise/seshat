"""
Goodreads author-id reverse-lookup (v2.13.0).

Closes the long-deferred v2.11.0 wiring gap: when a Seshat author has
no stored `goodreads_id` (so `_try_source` can't short-circuit and
the `search_author` policy lock makes Goodreads inert for that
author's source-scans), this module resolves the author's
goodreads_id from one of their books.

Strategy (cheapest-first):

  1. Pick a book for this author with the strongest available
     identifier:

       a. `books.goodreads_id` already stored on the book
          → derive directly, zero resolver hops
       b. `books.isbn` populated
          → resolver chain (auto_complete / hardcover book_mappings /
            openlibrary) returns the goodreads_book_id
       c. `books.asin` populated
          → same resolver chain

  2. With a goodreads_book_id in hand, fetch `/book/show/{id}` via
     the v2.13.0 `goodreads_session` (curl_cffi Chrome120 bypass).

  3. Parse the response's JSON-LD `author[].url` (or `sameAs`) for
     the `/author/show/{id}` pattern. That's the author's
     goodreads_id.

  4. Persist to `authors.goodreads_id`. Future source-scans pick it
     up via the existing `_try_source` short-circuit and fan out
     `/author/list/{id}` + per-book detail fetches.

  5. Return the resolved id, or `None` on any failure (no book with
     resolvable identifier, resolver chain dry, /book/show 4xx, no
     parseable author URL, etc.).

Used by:

  - `_try_source` in `app/discovery/lookup.py` — fallback when
    Goodreads's stored author_id is missing, BEFORE letting
    `search_author` no-op.
  - The async backfill task that runs after Calibre sync (sweeps
    every author missing a goodreads_id, populates whatever it can).

Both callers share the same code path so the rate-limit + soft-block
detection + caching come along for free.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from app.discovery.database import get_db
from app.metadata import goodreads_session
from app.metadata.goodreads_id_resolver import (
    ResolveQuery, resolve_goodreads_id,
)

_log = logging.getLogger("seshat.discovery.goodreads_author_backfill")

# /author/show/{id} OR /author/show/{id}.Slug — extract the digits.
_AUTHOR_URL_RX = re.compile(r"/author/show/(\d+)")


async def _pick_seed_book(author_id: int) -> Optional[dict]:
    """Pick the book by this author with the strongest available
    identifier for reverse-lookup. Order of preference:

      1. Owned + has goodreads_id (instant — no resolver needed)
      2. Owned + has isbn (resolver one hop)
      3. Owned + has asin
      4. Any + has goodreads_id
      5. Any + has isbn
      6. Any + has asin

    Returns a dict with the book's id + identifiers, or None if no
    suitable book exists.
    """
    db = await get_db()
    try:
        # Ranking SQL: each CASE branch encodes a tier. ORDER BY the
        # tier rank, then prefer the lowest book id for determinism.
        cur = await db.execute(
            """
            SELECT id, title, goodreads_id, isbn, asin, amazon_id, owned,
                CASE
                    WHEN owned = 1 AND goodreads_id IS NOT NULL AND goodreads_id != '' THEN 1
                    WHEN owned = 1 AND isbn        IS NOT NULL AND isbn        != '' THEN 2
                    WHEN owned = 1 AND asin        IS NOT NULL AND asin        != '' THEN 3
                    WHEN owned = 1 AND amazon_id   IS NOT NULL AND amazon_id   != '' THEN 3
                    WHEN              goodreads_id IS NOT NULL AND goodreads_id != '' THEN 4
                    WHEN              isbn        IS NOT NULL AND isbn        != '' THEN 5
                    WHEN              asin        IS NOT NULL AND asin        != '' THEN 6
                    WHEN              amazon_id   IS NOT NULL AND amazon_id   != '' THEN 6
                    ELSE 99
                END AS tier
            FROM books
            WHERE author_id = ? AND hidden = 0
            ORDER BY tier, id
            LIMIT 1
            """,
            (author_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        record = dict(zip(cols, row))
        if record["tier"] == 99:
            return None
        return record
    finally:
        await db.close()


def _parse_author_id_from_html(html: str) -> Optional[str]:
    """Extract the author's goodreads id from a /book/show/{id} page.

    Tries JSON-LD `author[].url` / `sameAs` first (most stable);
    falls back to scanning anchor hrefs for /author/show/{id}.
    Returns the digit-only id ('38550') without the slug, or None
    if nothing parses.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")

    # JSON-LD first.
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        authors_ld = data.get("author")
        if not authors_ld:
            continue
        candidates: list[dict] = (
            authors_ld if isinstance(authors_ld, list) else [authors_ld]
        )
        for a in candidates:
            if not isinstance(a, dict):
                continue
            for key in ("url", "sameAs", "@id"):
                url = a.get(key)
                if not url:
                    continue
                m = _AUTHOR_URL_RX.search(str(url))
                if m:
                    return m.group(1)

    # HTML anchor fallback. Goodreads's right-side author byline
    # contains <a href="/author/show/{id}.{slug}">.
    for a in soup.select("a[href*='/author/show/']"):
        m = _AUTHOR_URL_RX.search(a.get("href", "") or "")
        if m:
            return m.group(1)

    return None


async def _derive_goodreads_book_id(book: dict) -> Optional[str]:
    """Given a book row from `_pick_seed_book`, return the
    goodreads_book_id we should fetch /book/show for.

    Direct path: book.goodreads_id is already populated → use it.
    Resolver path: derive via the v2.13.0 resolver chain from ISBN /
    ASIN. The resolver itself caches outcomes (30-day TTL on hits)
    so a repeat call is free.
    """
    if book.get("goodreads_id"):
        return str(book["goodreads_id"])

    asin = book.get("asin") or book.get("amazon_id") or ""
    isbn = book.get("isbn") or ""
    if not isbn and not asin:
        return None

    result = await resolve_goodreads_id(ResolveQuery(isbn=isbn, asin=asin))
    if result and result.goodreads_book_id:
        return result.goodreads_book_id
    return None


async def _persist_author_goodreads_id(author_id: int, goodreads_id: str) -> None:
    """Write authors.goodreads_id. Idempotent — no-op if already set
    to the same value (cheap optimization for repeat backfill runs)."""
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT goodreads_id FROM authors WHERE id = ?", (author_id,),
        )
        row = await cur.fetchone()
        if row and row[0] == goodreads_id:
            return
        await db.execute(
            "UPDATE authors SET goodreads_id = ? WHERE id = ?",
            (goodreads_id, author_id),
        )
        await db.commit()
    finally:
        await db.close()


async def backfill_missing_author_ids(*, limit: Optional[int] = None) -> dict:
    """Sweep every author missing `goodreads_id` whose books have at
    least one resolvable identifier, and resolve via
    `resolve_author_goodreads_id`.

    Intended to run as a fire-and-forget background task after each
    Calibre sync completes — Calibre may have just freshly mined a
    pile of new identifiers that the previous backfill pass couldn't
    use.

    Rate-limit comes for free from `goodreads_session` (5s + 0–1s
    jitter per /book/show fetch). On a fresh install with ~200 authors
    to backfill, expect ~17 minutes wall time. Non-blocking — caller
    fires this via `asyncio.create_task`.

    Per the Phase-A bypass dispatcher gate: if any single backfill
    fetch returns a soft-block, the session module flips state to
    `soft_blocked` and the next iteration's `goodreads_session.get`
    call will... still fire (the dispatcher skip lives in the
    enricher/source-scan path, not at the session layer). To avoid
    pounding Cloudflare during a soft-block window, we short-circuit
    on `is_soft_blocked()` and abort the sweep early. Picks up on
    the next Calibre sync.

    `limit` caps the number of authors processed per call (None =
    no cap). Test hook + lever for cautious rollouts.

    Returns a stats dict suitable for logging:
      {"considered": int, "resolved": int, "missed": int,
       "skipped_soft_blocked": int}
    """
    stats = {
        "considered": 0, "resolved": 0,
        "missed": 0, "skipped_soft_blocked": 0,
    }

    db = await get_db()
    try:
        # Pick authors missing `goodreads_id` that have at least ONE
        # book with a resolvable identifier (direct goodreads_id or
        # ISBN/ASIN). Inner join eliminates "empty" authors with no
        # resolvable books — those would be wasted iterations.
        cur = await db.execute(
            """
            SELECT DISTINCT a.id, a.name
            FROM authors a
            JOIN books b ON b.author_id = a.id
            WHERE (a.goodreads_id IS NULL OR a.goodreads_id = '')
              AND b.hidden = 0
              AND (
                (b.goodreads_id IS NOT NULL AND b.goodreads_id != '')
                OR (b.isbn IS NOT NULL AND b.isbn != '')
                OR (b.asin IS NOT NULL AND b.asin != '')
                OR (b.amazon_id IS NOT NULL AND b.amazon_id != '')
              )
            ORDER BY a.id
            """
        )
        rows = await cur.fetchall()
    finally:
        await db.close()

    if not rows:
        _log.info("backfill: no authors need goodreads_id resolution")
        return stats

    candidates = [(int(r[0]), str(r[1])) for r in rows]
    if limit is not None:
        candidates = candidates[:limit]

    _log.info(
        "backfill: sweeping %d author(s) for missing goodreads_id "
        "(rate ~5s + jitter each → est. %d min wall time)",
        len(candidates), max(1, len(candidates) * 6 // 60),
    )

    for author_id, name in candidates:
        # Bail early if a previous iteration tripped Cloudflare.
        if goodreads_session.is_soft_blocked():
            stats["skipped_soft_blocked"] = len(candidates) - stats["considered"]
            _log.info(
                "backfill: aborting sweep — session state is "
                "soft_blocked. %d author(s) deferred to next Calibre "
                "sync (already resolved: %d, missed: %d).",
                stats["skipped_soft_blocked"],
                stats["resolved"], stats["missed"],
            )
            break
        stats["considered"] += 1
        try:
            resolved = await resolve_author_goodreads_id(author_id)
        except Exception:
            _log.exception(
                "backfill: unhandled error on author_id=%d %r (non-fatal)",
                author_id, name,
            )
            stats["missed"] += 1
            continue
        if resolved:
            stats["resolved"] += 1
        else:
            stats["missed"] += 1

    _log.info(
        "backfill: sweep complete. considered=%d resolved=%d missed=%d "
        "skipped_soft_blocked=%d",
        stats["considered"], stats["resolved"],
        stats["missed"], stats["skipped_soft_blocked"],
    )
    return stats


async def resolve_author_goodreads_id(author_id: int) -> Optional[str]:
    """Top-level helper. Resolves an author's goodreads_id from
    their books and persists it.

    Returns the goodreads_id string on success, None on any failure.
    Never raises — author-resolution failures are non-fatal everywhere
    this is called from.
    """
    try:
        book = await _pick_seed_book(author_id)
        if not book:
            _log.debug(
                "backfill: no seed book for author_id=%d (no books with "
                "goodreads_id / isbn / asin)", author_id,
            )
            return None

        book_id = await _derive_goodreads_book_id(book)
        if not book_id:
            _log.debug(
                "backfill: could not derive goodreads_book_id for author_id=%d "
                "from seed book id=%s (resolver chain dry)",
                author_id, book.get("id"),
            )
            return None

        session = await goodreads_session.get_session()
        url = f"https://www.goodreads.com/book/show/{book_id}"
        try:
            resp = await session.get(url)
        except Exception as e:
            _log.info(
                "backfill: HTTP error fetching %s for author_id=%d: %s",
                url, author_id, e,
            )
            return None

        if goodreads_session.is_cloudflare_soft_block(resp):
            _log.info(
                "backfill: soft-blocked fetching %s — abort, dispatcher "
                "skip will gate further attempts", url,
            )
            return None
        status = getattr(resp, "status_code", 0)
        if status >= 400:
            _log.debug(
                "backfill: %s returned HTTP %d for author_id=%d",
                url, status, author_id,
            )
            return None

        html = getattr(resp, "text", "") or (
            (getattr(resp, "content", b"") or b"").decode("utf-8", "ignore")
        )
        author_goodreads_id = _parse_author_id_from_html(html)
        if not author_goodreads_id:
            _log.info(
                "backfill: no author goodreads_id parsed from %s "
                "(JSON-LD + anchor fallback both empty) for author_id=%d",
                url, author_id,
            )
            return None

        await _persist_author_goodreads_id(author_id, author_goodreads_id)
        _log.info(
            "backfill: author_id=%d ← goodreads_id=%s "
            "(seed book id=%s, book_goodreads_id=%s)",
            author_id, author_goodreads_id, book.get("id"), book_id,
        )
        return author_goodreads_id
    except Exception:
        _log.exception(
            "backfill: unexpected error resolving author_id=%d (non-fatal)",
            author_id,
        )
        return None
