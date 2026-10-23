"""
Goodreads author-id reverse-lookup (v2.13.0).

Closes the long-deferred v2.11.0 wiring gap: when a Seshat author has
no stored `goodreads_id` (so `_try_source` can't short-circuit and
the `search_author` policy lock makes Goodreads inert for that
author's source-scans), this module resolves the author's
goodreads_id from one of their books.

Strategy (Phase 1; autocomplete first since the 2026-10 audit, G90):

  1. Pick a book for this author with the strongest available
     identifier (its Goodreads book ID, ISBN or ASIN).

  2. One Goodreads autocomplete for it — by ISBN / ASIN, or by title
     matched on the book's Goodreads ID — and, when that finds nothing,
     one more with the author's name added. A hit names the book's
     author: when it's our author (normalised name), that's the ID, with
     no book page at all.

  3. Only when the hit names someone else first (a co-authored book),
     fetch `/book/show/{id}` and look for our author among every
     JSON-LD author on the page.

  4. Persist to `authors.goodreads_id` (mirrored to the same person's
     author in the other libraries).

Book pages are the kind of Goodreads request AWS WAF blocks; until the
2026-10 audit every author cost one.

Used by:

  - Hygiene's author-ID job (`hygiene.job_author_id_backfill`).
  - The weekly job (`weekly_author_id_backfill`, G84 / G104): first an ID
    copied from the same person's author in another library, then Phase
    1, then Phase 2 for authors with books Phase 1 couldn't resolve.

Scans no longer call it (G84 / call 5): a new author gets its Goodreads
ID from the weekly job, then its list page from the cache worker.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Optional

from bs4 import BeautifulSoup

from app import state
from app.config import CALIBRE_DB_PATH
from app.discovery.database import get_db
from app.metadata import goodreads_session
from app.metadata import source_gate
from app.metadata.author_names import normalize_author_name

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

        if goodreads_session.is_soft_block(resp):
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


# Authors this process has already tried to resolve, per phase and
# library (ADR-0005): an author whose books resolve to nothing is not
# retried by the same phase until the next restart (Phase 2's Calibre
# lookup still gets its own try). Until the 2026-10 audit every library
# sync re-tried every unresolvable author.
_attempted: set[tuple[str, str, int]] = set()


def _book_pages_backing_off() -> bool:
    return goodreads_session.is_backing_off(source_gate.KIND_BOOK_PAGE)


def _autocomplete_backing_off() -> bool:
    """Phase 1 is autocomplete first (G90): it stops on an autocomplete
    backoff; a book-page one only stops its co-author fallback."""
    return goodreads_session.is_backing_off(source_gate.KIND_AUTOCOMPLETE)


def _library_key() -> str:
    from app.discovery.database import get_active_library
    return str(get_active_library() or "")


def reset_attempted_for_tests() -> None:
    _attempted.clear()


@source_gate.as_caller(source_gate.CALLER_BACKFILL)
async def backfill_missing_author_ids(
    *, limit: Optional[int] = None,
    attempted: Optional[set] = None,
    phase2_with_books_only: bool = False,
) -> dict:
    """Sweep every author missing `goodreads_id` whose books have at
    least one resolvable identifier, and resolve via
    `resolve_author_goodreads_id`.

    Runs from Hygiene (`hygiene.job_author_id_backfill`). Until the
    2026-10 audit it also ran fire-and-forget after every Calibre and
    Audiobookshelf sync (every two hours on one install).

    Each request waits Goodreads' turn at the Metadata Sources rate (the
    source gate). Authors already tried in this process are skipped
    (`_attempted`, ADR-0005), and the sweep stops once book pages are
    backing off after an AWS WAF block; the next run picks up the rest.

    `limit` caps the number of authors processed per call (None =
    no cap). Test hook + lever for cautious rollouts.

    `attempted` is the set of authors already tried: Hygiene shares the
    process-wide one (an author is tried once until a restart, so a
    200-author Hygiene run moves on to the next batch); the weekly job
    passes a fresh set, so each run tries every unresolved author once
    (G91). `phase2_with_books_only` limits Phase 2 to authors with books
    that Phase 1 couldn't resolve (the weekly job, G104); bookless
    co-authors / pen names are left to Hygiene.

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

    tried = _attempted if attempted is None else attempted
    candidates = [(int(r[0]), str(r[1])) for r in rows]
    if not candidates:
        _log.info(
            "backfill: no Phase-1 candidates (no author needs a "
            "books-table reverse-lookup) — proceeding to Phase 2"
        )
    slug = _library_key()
    fresh = [(a, n) for a, n in candidates if ("p1", slug, a) not in tried]
    if candidates and not fresh:
        _log.info(
            "backfill: only previously-attempted authors remain (%d) — "
            "skipping Phase 1 until the next restart", len(candidates),
        )
    candidates = fresh
    if limit is not None:
        candidates = candidates[:limit]

    _log.info(
        "backfill: sweeping %d author(s) for missing goodreads_id "
        "(rate ~5s + jitter each → est. %d min wall time)",
        len(candidates), max(1, len(candidates) * 6 // 60),
    )

    for author_id, name in candidates:
        # Stop once autocomplete is backing off after a block.
        if _autocomplete_backing_off():
            stats["skipped_soft_blocked"] = len(candidates) - stats["considered"]
            _log.info(
                "backfill: stopping — Goodreads autocomplete is backing "
                "off. %d author(s) left for the next run (already "
                "resolved: %d, missed: %d).",
                stats["skipped_soft_blocked"],
                stats["resolved"], stats["missed"],
            )
            break
        tried.add(("p1", slug, author_id))
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
    if _book_pages_backing_off():
        _log.info(
            "backfill-phase2: skipping (Goodreads book pages are backing off)"
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
            """ + (
                "AND a.id IN (SELECT author_id FROM book_authors) "
                if phase2_with_books_only else ""
            ) + """
            ORDER BY a.id
            """
        )
        phase2_rows = await cur.fetchall()
    finally:
        await db.close()

    phase2_candidates = [
        (int(r[0]), str(r[1])) for r in phase2_rows
        if ("p2", slug, int(r[0])) not in tried
    ]
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
            if _book_pages_backing_off():
                phase2_stats["skipped_soft_blocked"] = (
                    len(phase2_candidates) - phase2_stats["considered"]
                )
                _log.info(
                    "backfill-phase2: stopping — Goodreads book pages are "
                    "backing off. %d left for the next run.",
                    phase2_stats["skipped_soft_blocked"],
                )
                break
            tried.add(("p2", slug, author_id))
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


_AUTOCOMPLETE = "https://www.goodreads.com/book/auto_complete?format=json&q="


async def _autocomplete_hits(query: str) -> tuple[str, list]:
    """('ok', hits) | ('blocked', []) | ('error', [])."""
    import urllib.parse
    session = await goodreads_session.get_session()
    try:
        resp = await session.get(_AUTOCOMPLETE + urllib.parse.quote(query))
    except goodreads_session.GoodreadsBackingOff:
        return "blocked", []
    except Exception as e:
        _log.debug("backfill: autocomplete error for %r: %s", query, e)
        return "error", []
    if goodreads_session.is_soft_block(resp):
        return "blocked", []
    if getattr(resp, "status_code", None) != 200:
        return "error", []
    try:
        data = resp.json()
    except Exception:
        return "error", []
    return "ok", [h for h in data if isinstance(h, dict)] if isinstance(data, list) else []


async def _author_via_autocomplete(
    book: dict, author_name: str, target_norm: str,
) -> tuple[str, Optional[str], Optional[str]]:
    """G90: the author's Goodreads ID from autocomplete. Returns
    ('match', author_id, book_id) when the hit's author is ours,
    ('other_author', None, book_id) when the book's hit names someone
    else first (a co-authored book: its page lists every author),
    ('blocked', None, None) or ('miss', None, None)."""
    title = str(book.get("title") or "")
    gr_book = str(book.get("goodreads_id") or "")
    ident = str(book.get("isbn") or book.get("asin") or book.get("amazon_id") or "")
    queries: list[tuple[str, Optional[str]]] = []
    if gr_book and title:
        queries.append((title, gr_book))
    elif ident:
        queries.append((ident, None))
    if title and author_name:
        queries.append((f"{title} {author_name}", gr_book or None))
    for i, (query, want) in enumerate(queries[:2]):
        status, hits = await _autocomplete_hits(query)
        if status == "blocked":
            return "blocked", None, None
        if status != "ok" or not hits:
            continue
        if want:
            hit = next((h for h in hits if str(h.get("bookId")) == want), None)
        elif i == 0:
            hit = hits[0]            # an ISBN / ASIN query: that edition
        else:
            hit = next((
                h for h in hits
                if normalize_author_name(str((h.get("author") or {}).get("name") or ""))
                == target_norm
            ), None)
        if hit is None:
            continue
        try:
            from app.discovery import goodreads_store
            await goodreads_store.save_hit(hit)
        except Exception:
            _log.debug("backfill: storing the hit failed", exc_info=True)
        author = hit.get("author") or {}
        if author.get("id") and normalize_author_name(str(author.get("name") or "")) == target_norm:
            return "match", str(author["id"]), str(hit.get("bookId") or "")
        return "other_author", None, str(hit.get("bookId") or "") or None
    return "miss", None, None


async def resolve_author_goodreads_id(author_id: int) -> Optional[str]:
    """Top-level helper. Resolves an author's goodreads_id from
    their books and persists it.

    Returns the goodreads_id string on success, None on any failure.
    Never raises — author-resolution failures are non-fatal everywhere
    this is called from.

    Autocomplete first (2026-10 audit, G90): one or two autocompletes
    for a seed book, and the book page only when the hit names a
    co-author first.

    v3.6.2 — name-verification guard. Before this fix, Phase 1 stamped
    whichever author appeared FIRST in the seed book's JSON-LD `author[]`
    block, with no check that the queried author was actually that author.
    Co-authored seed books silently misassigned the wrong GR ID, which
    Hygiene Job 9 (`consolidate_persons_by_source_id`) then merged into
    wrong persons — produced the Chohokiteki Kaeru → Roy Colt and
    Fehu Kazuno → Matt Waid wrong-merges observed live 2026-06-02.
    Mirrors Phase 2's existing match-by-normalized-name pattern at
    `_resolve_via_calibre_coauthor`. Autocomplete hits pass the same
    name check.
    """
    try:
        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT name FROM authors WHERE id = ?", (author_id,),
            )).fetchone()
        finally:
            await db.close()
        if not row or not row[0]:
            _log.debug(
                "backfill: author_id=%d not found or unnamed; skip",
                author_id,
            )
            return None
        author_name = str(row[0])
        target_norm = normalize_author_name(author_name)

        book = await _pick_seed_book(author_id)
        if not book:
            _log.debug(
                "backfill: no seed book for author_id=%d (no books with "
                "goodreads_id / isbn / asin)", author_id,
            )
            return None

        status, match, book_id = await _author_via_autocomplete(
            book, author_name, target_norm,
        )
        if status == "match" and match:
            await _persist_author_goodreads_id(author_id, match)
            _log.info(
                "backfill: author_id=%d %r ← goodreads_id=%s (autocomplete, "
                "seed book id=%s, goodreads book %s)",
                author_id, author_name, match, book.get("id"), book_id,
            )
            return match
        if status != "other_author" or not book_id:
            _log.info(
                "backfill: author_id=%d %r — autocomplete %s (seed book id=%s); "
                "tried again next run",
                author_id, author_name, status, book.get("id"),
            )
            return None
        return await _author_from_book_page(author_id, author_name, target_norm, book, book_id)
    except Exception:
        _log.exception(
            "backfill: unexpected error resolving author_id=%d (non-fatal)",
            author_id,
        )
        return None


async def _author_from_book_page(
    author_id: int, author_name: str, target_norm: str, book: dict, book_id: str,
) -> Optional[str]:
    """The co-authored case (G90): our author among every JSON-LD author on
    the book's page."""
    try:
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

        if goodreads_session.is_soft_block(resp):
            _log.info(
                "backfill: blocked by AWS WAF fetching %s — book pages "
                "backing off", url,
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

        # v3.6.2 — multi-author parse + match-by-name. Walk every
        # JSON-LD `author[]` entry on the page and stamp ONLY when
        # one of them normalizes to the queried author's name.
        # Without this guard, co-authored seed books mis-stamp the
        # wrong GR ID on the queried author and Hygiene Job 9
        # later merges them into the wrong person.
        all_authors = _parse_all_authors_from_html(html)
        if not all_authors:
            _log.info(
                "backfill: no author goodreads_id parsed from %s "
                "(JSON-LD authors empty) for author_id=%d",
                url, author_id,
            )
            return None

        match: Optional[str] = None
        for cand_name, cand_id in all_authors:
            if normalize_author_name(cand_name) == target_norm:
                match = cand_id
                break
        if not match:
            _log.info(
                "backfill: %d author(s) found at %s but none matched "
                "%r (normalized) — skipping stamp to avoid wrong-merge. "
                "JSON-LD authors: %r",
                len(all_authors), url, author_name,
                [n for n, _ in all_authors],
            )
            return None

        await _persist_author_goodreads_id(author_id, match)
        _log.info(
            "backfill: author_id=%d %r ← goodreads_id=%s "
            "(seed book id=%s, book_goodreads_id=%s; matched by "
            "normalized name out of %d JSON-LD authors)",
            author_id, author_name, match, book.get("id"), book_id,
            len(all_authors),
        )
        return match
    except Exception:
        _log.exception(
            "backfill: unexpected error resolving author_id=%d (non-fatal)",
            author_id,
        )
        return None



# ─── The weekly job (2026-10 audit wave 4b, G84 / G91 / G104) ──────────


async def copy_goodreads_ids_from_twins() -> int:
    """Give an author without a Goodreads ID the one the same person's
    author in another library has (G104; no request). Audiobook-library
    authors are mostly twins of ebook authors. Returns how many were
    filled."""
    from app.discovery.author_identity import linked_authors, person_id_for
    from app.discovery import metadata_cache
    copied = 0
    for lib in state._discovered_libraries:
        slug = lib.get("slug")
        if not slug:
            continue
        db = await get_db(slug=slug)
        try:
            missing = [int(r[0]) for r in await (await db.execute(
                "SELECT id FROM authors WHERE goodreads_id IS NULL OR goodreads_id = ''"
            )).fetchall()]
        finally:
            await db.close()
        for aid in missing:
            try:
                pid = await person_id_for(slug, aid)
                if pid is None:
                    continue
                found = None
                for other_slug, other_aid in await linked_authors(pid):
                    if other_slug == slug:
                        continue
                    odb = await get_db(slug=other_slug)
                    try:
                        r = await (await odb.execute(
                            "SELECT goodreads_id FROM authors WHERE id = ?", (other_aid,),
                        )).fetchone()
                    finally:
                        await odb.close()
                    if r and r[0]:
                        found = str(r[0])
                        break
                if not found or await metadata_cache.is_goodreads_id_known_unavailable(found):
                    continue
                db = await get_db(slug=slug)
                try:
                    cur = await db.execute(
                        "UPDATE authors SET goodreads_id = ? WHERE id = ? "
                        "AND (goodreads_id IS NULL OR goodreads_id = '')",
                        (found, aid),
                    )
                    await db.commit()
                    copied += cur.rowcount or 0
                finally:
                    await db.close()
            except Exception:
                _log.exception(
                    "backfill: copying a Goodreads ID to %s/%d failed (non-fatal)",
                    slug, aid,
                )
    if copied:
        _log.info("backfill: copied %d Goodreads author ID(s) from another library", copied)
    return copied


# The job switches the process-wide active library, so it waits while a
# scan, a library sync or Hygiene runs (they switch it too).
_WEEKLY_WAIT_STEP_S = 60.0
_WEEKLY_WAIT_MAX_S = 2 * 3600.0


@source_gate.as_caller(source_gate.CALLER_BACKFILL)
async def weekly_author_id_backfill() -> dict:
    """G84 / G104: copy IDs from twins, then Phase 1 and (for authors with
    books Phase 1 couldn't resolve) Phase 2 on each ebook library, with a
    fresh attempted set (G91)."""
    import asyncio
    from app.discovery import goodreads_candidates
    from app.discovery.database import get_active_library, set_active_library
    waited = 0.0
    while goodreads_candidates.merge_blocked_by() and waited < _WEEKLY_WAIT_MAX_S:
        await asyncio.sleep(_WEEKLY_WAIT_STEP_S)
        waited += _WEEKLY_WAIT_STEP_S
    blocker = goodreads_candidates.merge_blocked_by()
    if blocker:
        _log.info("backfill (weekly): skipped — %s still running after 2h", blocker)
        return {"skipped": blocker}
    totals = {"copied": await copy_goodreads_ids_from_twins(),
              "considered": 0, "resolved": 0, "missed": 0, "skipped_soft_blocked": 0}
    attempted: set = set()
    previous = get_active_library()
    try:
        for lib in state._discovered_libraries:
            slug = lib.get("slug")
            if not slug or (lib.get("content_type") or "ebook") != "ebook":
                continue
            set_active_library(slug)
            stats = await backfill_missing_author_ids(
                attempted=attempted, phase2_with_books_only=True,
            )
            for k in ("considered", "resolved", "missed", "skipped_soft_blocked"):
                totals[k] += int(stats.get(k, 0) or 0)
    finally:
        set_active_library(previous)
    _log.info(
        "backfill (weekly): copied=%d considered=%d resolved=%d missed=%d "
        "left_for_next_run=%d",
        totals["copied"], totals["considered"], totals["resolved"],
        totals["missed"], totals["skipped_soft_blocked"],
    )
    return totals
