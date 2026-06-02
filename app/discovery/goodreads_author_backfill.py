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
import sqlite3
from typing import Optional

from bs4 import BeautifulSoup

from app.config import CALIBRE_DB_PATH
from app.discovery.database import get_db
from app.metadata import goodreads_session
from app.metadata.author_names import normalize_author_name
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
            WHERE id IN (SELECT book_id FROM book_authors WHERE author_id = ?)
              AND hidden = 0
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


def _parse_all_authors_from_html(html: str) -> list[tuple[str, str]]:
    """Extract every (name, goodreads_id) pair from a /book/show/{id}
    page's JSON-LD `author[]` block.

    Used by the Phase-2 cross-DB Calibre backfill: when a Seshat
    author has no books in Seshat's books table but appears as a
    co-author on a Calibre book with a goodreads identifier, we
    fetch the book detail page and need to pick the author that
    matches by name (not "the first one").

    Returns a list of (name, id) tuples in JSON-LD order. Empty
    list if no parseable authors.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

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
            name = a.get("name") or ""
            url_id: Optional[str] = None
            for key in ("url", "sameAs", "@id"):
                url = a.get(key)
                if not url:
                    continue
                m = _AUTHOR_URL_RX.search(str(url))
                if m:
                    url_id = m.group(1)
                    break
            if not name or not url_id:
                continue
            if url_id in seen:
                continue
            seen.add(url_id)
            out.append((str(name), url_id))
    return out


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
    to the same value (cheap optimization for repeat backfill runs).

    Guard: if the GR metadata cache has stamped `goodreads_id` as
    `unavailable_404` (the worker hit HTTP 404 from GR), skip the
    write. Otherwise backfill resolves the same dead ID from another
    book row and re-stamps it, restarting the worker retry loop the
    user manually broke. Mirrors ADR-0006's MAM stub semantics.
    """
    from app.discovery import metadata_cache
    if await metadata_cache.is_goodreads_id_known_unavailable(goodreads_id):
        _log.info(
            "backfill: skipping author_id=%s — goodreads_id=%s is marked "
            "unavailable_404 in the GR cache (author does not exist on "
            "Goodreads)",
            author_id, goodreads_id,
        )
        return
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

    # v2.20.0 — mirror to other libraries' linked author rows.
    try:
        from app.discovery.author_identity import mirror_source_id
        from app.discovery.database import get_active_library
        slug = get_active_library()
        if slug:
            await mirror_source_id(slug, author_id, "goodreads_id", goodreads_id)
    except Exception:
        # Best-effort; the per-library write above already landed.
        pass


def _resolve_calibre_db_path(override: Optional[str] = None) -> Optional[str]:
    """Resolve the path to Calibre's metadata.db.

    Multi-library installs (Calibre + ABS + others) keep per-library
    paths in `state._discovered_libraries`. The legacy
    `app.config.CALIBRE_DB_PATH` constant defaults to a path that
    only exists in single-library setups. Prefer the discovered
    per-library path when available; fall back to the config
    constant otherwise.
    """
    if override:
        return override
    try:
        from app import state
        for lib in state._discovered_libraries:
            if lib.get("app_type", "calibre") != "calibre":
                continue
            sdb = lib.get("source_db_path")
            if sdb:
                return str(sdb)
    except Exception:
        pass
    return str(CALIBRE_DB_PATH) if CALIBRE_DB_PATH else None


def _pick_calibre_book_with_goodreads_for(
    author_name: str, calibre_db_path: Optional[str] = None,
) -> Optional[str]:
    """Phase-2 helper: directly query Calibre's metadata.db for any
    book co-authored by `author_name` that carries a `goodreads`
    identifier. Returns the goodreads_book_id (digit string) or None.

    Used when Seshat's `books` table has NO row attributable to this
    author — typically a co-author or pen-name alias whose books
    Seshat attributed to the primary author only. Calibre's
    `books_authors_link` is N:N so it sees the contributor; Seshat's
    single `author_id` column on `books` does not.

    Read-only against Calibre's DB. No writes ever.
    """
    cal_path = _resolve_calibre_db_path(calibre_db_path)
    if not cal_path:
        return None
    try:
        conn = sqlite3.connect(cal_path)
    except sqlite3.OperationalError:
        return None
    try:
        # Calibre author name matching: try exact first, then case-
        # insensitive. We'd love a normalize-pass match but Calibre's
        # author names are user-entered and may differ in punctuation
        # from Seshat's normalized form. Two queries is cheap; the
        # alternative is dragging Seshat's normalizer into a SQL UDF.
        for sql in (
            "SELECT i.val FROM identifiers i "
            "JOIN books_authors_link bal ON bal.book = i.book "
            "JOIN authors a ON a.id = bal.author "
            "WHERE i.type = 'goodreads' AND a.name = ? LIMIT 1",
            "SELECT i.val FROM identifiers i "
            "JOIN books_authors_link bal ON bal.book = i.book "
            "JOIN authors a ON a.id = bal.author "
            "WHERE i.type = 'goodreads' AND LOWER(a.name) = LOWER(?) LIMIT 1",
        ):
            row = conn.execute(sql, (author_name,)).fetchone()
            if row and row[0]:
                return str(row[0]).strip()
        return None
    finally:
        conn.close()


async def resolve_author_via_calibre_coauthor(
    author_id: int, author_name: str,
) -> Optional[str]:
    """Phase-2 backfill path: resolve an author's goodreads_id via a
    Calibre book they appear on as ANY author (not just primary).

    Steps:
      1. Query Calibre's metadata.db for any book this author
         contributed to that carries a `goodreads` identifier.
      2. Fetch /book/show/{book_id} via the bypass.
      3. Parse ALL author[] entries from JSON-LD.
      4. Match by normalized name to find the right entry.
      5. Persist to authors.goodreads_id.

    Returns the resolved id or None. Never raises.
    """
    try:
        book_id = _pick_calibre_book_with_goodreads_for(author_name)
        if not book_id:
            return None

        session = await goodreads_session.get_session()
        url = f"https://www.goodreads.com/book/show/{book_id}"
        try:
            resp = await session.get(url)
        except Exception as e:
            _log.info(
                "backfill-phase2: HTTP error fetching %s for author_id=%d: %s",
                url, author_id, e,
            )
            return None

        if goodreads_session.is_cloudflare_soft_block(resp):
            _log.info(
                "backfill-phase2: soft-blocked fetching %s — abort.", url,
            )
            return None
        status = getattr(resp, "status_code", 0)
        if status >= 400:
            _log.debug(
                "backfill-phase2: %s returned HTTP %d for author_id=%d",
                url, status, author_id,
            )
            return None

        html = getattr(resp, "text", "") or (
            (getattr(resp, "content", b"") or b"").decode("utf-8", "ignore")
        )
        all_authors = _parse_all_authors_from_html(html)
        if not all_authors:
            _log.info(
                "backfill-phase2: no author entries in JSON-LD at %s "
                "for author_id=%d", url, author_id,
            )
            return None

        target_norm = normalize_author_name(author_name)
        match: Optional[str] = None
        for cand_name, cand_id in all_authors:
            if normalize_author_name(cand_name) == target_norm:
                match = cand_id
                break
        if not match:
            _log.info(
                "backfill-phase2: %d author(s) found at %s but none "
                "matched %r (normalized): %r",
                len(all_authors), url, author_name,
                [n for n, _ in all_authors],
            )
            return None

        await _persist_author_goodreads_id(author_id, match)
        _log.info(
            "backfill-phase2: author_id=%d %r ← goodreads_id=%s "
            "(via Calibre book %s; %d author entries on page)",
            author_id, author_name, match, book_id, len(all_authors),
        )
        return match
    except Exception:
        _log.exception(
            "backfill-phase2: unexpected error resolving author_id=%d "
            "%r (non-fatal)", author_id, author_name,
        )
        return None


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
            JOIN book_authors bpa ON bpa.author_id = a.id
            JOIN books b ON b.id = bpa.book_id
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

    candidates = [(int(r[0]), str(r[1])) for r in rows]
    if not candidates:
        _log.info(
            "backfill: no Phase-1 candidates (no author needs a "
            "books-table reverse-lookup) — proceeding to Phase 2"
        )
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

    # ── Phase 2: cross-DB Calibre-direct sweep ────────────────────
    # Catches the co-author / pen-name-alias gap where a Seshat author
    # exists but has zero books in Seshat's books table (Calibre's
    # books_authors_link is N:N but Seshat's books.author_id is 1:1, so
    # secondary contributors get an authors row and nothing else). We
    # query Calibre's metadata.db directly for any book they
    # contributed to that carries a `goodreads` identifier, fetch
    # /book/show, and match by normalized name.
    if goodreads_session.is_soft_blocked():
        _log.info(
            "backfill-phase2: skipping (session state is soft_blocked "
            "from Phase 1)"
        )
        return stats

    phase2_stats = {"considered": 0, "resolved": 0, "missed": 0,
                    "skipped_soft_blocked": 0}
    db = await get_db()
    try:
        cur = await db.execute(
            """
            SELECT a.id, a.name FROM authors a
            WHERE (a.goodreads_id IS NULL OR a.goodreads_id = '')
            ORDER BY a.id
            """
        )
        phase2_rows = await cur.fetchall()
    finally:
        await db.close()

    phase2_candidates = [(int(r[0]), str(r[1])) for r in phase2_rows]
    if limit is not None:
        # Honor the same limit across both phases combined (best-effort).
        remaining = max(0, limit - stats["considered"])
        phase2_candidates = phase2_candidates[:remaining]

    if phase2_candidates:
        _log.info(
            "backfill-phase2: sweeping %d remaining author(s) via "
            "Calibre cross-DB co-author lookup",
            len(phase2_candidates),
        )
        for author_id, name in phase2_candidates:
            if goodreads_session.is_soft_blocked():
                phase2_stats["skipped_soft_blocked"] = (
                    len(phase2_candidates) - phase2_stats["considered"]
                )
                _log.info(
                    "backfill-phase2: aborting — session state is "
                    "soft_blocked. %d deferred.",
                    phase2_stats["skipped_soft_blocked"],
                )
                break
            phase2_stats["considered"] += 1
            try:
                resolved = await resolve_author_via_calibre_coauthor(
                    author_id, name,
                )
            except Exception:
                _log.exception(
                    "backfill-phase2: unhandled error on author_id=%d %r "
                    "(non-fatal)", author_id, name,
                )
                phase2_stats["missed"] += 1
                continue
            if resolved:
                phase2_stats["resolved"] += 1
            else:
                phase2_stats["missed"] += 1
        _log.info(
            "backfill-phase2: sweep complete. considered=%d resolved=%d "
            "missed=%d skipped_soft_blocked=%d",
            phase2_stats["considered"], phase2_stats["resolved"],
            phase2_stats["missed"], phase2_stats["skipped_soft_blocked"],
        )

    # Roll Phase-2 stats into the overall return value.
    stats["considered"] += phase2_stats["considered"]
    stats["resolved"] += phase2_stats["resolved"]
    stats["missed"] += phase2_stats["missed"]
    stats["skipped_soft_blocked"] += phase2_stats["skipped_soft_blocked"]
    stats["phase2_resolved"] = phase2_stats["resolved"]
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
