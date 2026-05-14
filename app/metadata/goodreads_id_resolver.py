"""
Ethical goodreads_book_id resolver chain.

Goodreads' `/search` endpoint is explicitly disallowed for `*`
user-agents per https://www.goodreads.com/robots.txt. We don't hit
it. When the enricher (or any caller) needs a goodreads_book_id for
a book it doesn't already have one for, this module runs a chain of
robots-clean lookups in priority order:

    1. /book/auto_complete?format=json&q={isbn_or_asin}
       — undocumented JSON endpoint, NOT in the Disallow list.
       Identifier-based, not free-text. Handles most ebook imports
       since almost every epub/azw3 carries ISBN in file metadata.

    2. Hardcover GraphQL `book_mappings` (DEFERRED to v2.11.0)
       — purpose-built for goodreads cross-references. Requires the
       Hardcover discovery-source client which doesn't exist yet at
       v2.10.4. Returns None for now; the chain falls through to
       tier 3.

    3. Open Library `?bibkeys=ISBN:{isbn}&jscmd=data&format=json`
       — returns `identifiers.goodreads` for some records. Free,
       no key required. Coverage is sparse (data-quality dependent)
       but it's a useful gap-filler for older / well-cataloged
       books.

If all three tiers miss, return None — the caller (typically the
Goodreads source) skips and the enricher dispatcher moves to the
next source in the priority chain. We do NOT fall back to the
disallowed `/search` endpoint, even though kiwidude's Calibre
plugin does. Holding a higher standard is a deliberate choice.

The full strategy is documented in
`memory/project_seshat_metadata_overhaul.md` Phase 1.5.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

_log = logging.getLogger("seshat.metadata.goodreads_id_resolver")

_GOODREADS_AUTO_COMPLETE = (
    "https://www.goodreads.com/book/auto_complete?format=json&q="
)
_OPENLIBRARY_BOOKS = "https://openlibrary.org/api/books"

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
        "Gecko/20100101 Firefox/128.0"
    ),
    "Accept": "application/json",
}


@dataclass
class ResolveQuery:
    """What we know about a book that needs a goodreads_book_id."""
    title: str = ""
    author: str = ""
    isbn: str = ""
    asin: str = ""


@dataclass
class ResolveResult:
    """Outcome of a resolver chain attempt."""
    goodreads_book_id: Optional[str]
    tier: Optional[str]  # "auto_complete", "hardcover", "openlibrary", or None on miss
    soft_blocked: bool = False  # True if a tier responded with a 202 / Cloudflare gate


async def resolve_goodreads_id(
    query: ResolveQuery,
    *,
    client: Optional[httpx.AsyncClient] = None,
    use_cache: bool = True,
) -> ResolveResult:
    """Run the tiered resolver chain. First hit wins.

    `client` is optional — tests inject an `httpx.AsyncClient` with a
    `MockTransport` to drive scenarios. Production callers can pass
    a shared client to amortize connection pooling across calls.

    v2.13.0: cache lookup against `app.metadata.id_cache` happens
    BEFORE any HTTP. Hits (30-day TTL) skip the network entirely.
    Misses are ALSO cached (1-day TTL) so a dead-end ISBN doesn't
    pay another auto_complete round-trip every scan. `use_cache=False`
    bypasses both read and write — useful for the canary which
    explicitly wants a live probe of the resolver chain.
    """
    from app.metadata import id_cache

    if use_cache:
        cached = id_cache.get_book_id(
            isbn=query.isbn, asin=query.asin,
            title=query.title, author=query.author,
        )
        if cached is not None:
            book_id, tier = cached
            return ResolveResult(book_id, tier or None, soft_blocked=False)

    owned_client = client is None
    if owned_client:
        client = httpx.AsyncClient(timeout=15.0, headers=_DEFAULT_HEADERS)

    soft_blocked = False
    try:
        # ── Tier 1: Goodreads auto_complete by ISBN/ASIN ───────
        for ident in (query.isbn, query.asin):
            if not ident:
                continue
            tier1 = await _tier1_auto_complete(client, ident)
            if tier1 == "_soft_blocked":
                soft_blocked = True
                continue
            if tier1:
                _log.debug(
                    "resolver: tier1 (auto_complete) hit for %s → goodreads_id=%s",
                    ident, tier1,
                )
                result = ResolveResult(tier1, "auto_complete", soft_blocked)
                if use_cache:
                    id_cache.put_book_id(
                        isbn=query.isbn, asin=query.asin,
                        title=query.title, author=query.author,
                        book_id=tier1, tier="auto_complete",
                    )
                return result

        # ── Tier 2: Hardcover book_mappings (deferred) ─────────
        # Implemented in v2.11.0 once the Hardcover discovery client
        # has a `book_mappings` GraphQL query method. Until then we
        # log a stub so future ops can see this tier was skipped.
        _log.debug(
            "resolver: tier2 (hardcover book_mappings) deferred to v2.11.0"
        )

        # ── Tier 3: Open Library identifiers.goodreads ─────────
        if query.isbn:
            tier3 = await _tier3_openlibrary(client, query.isbn)
            if tier3:
                _log.debug(
                    "resolver: tier3 (openlibrary) hit for isbn=%s → goodreads_id=%s",
                    query.isbn, tier3,
                )
                result = ResolveResult(tier3, "openlibrary", soft_blocked)
                if use_cache:
                    id_cache.put_book_id(
                        isbn=query.isbn, asin=query.asin,
                        title=query.title, author=query.author,
                        book_id=tier3, tier="openlibrary",
                    )
                return result

        # Full miss across all tiers — cache the negative so the next
        # scan with the same identifier doesn't re-probe Goodreads.
        # Skip cache-write on soft-block so a transient Cloudflare gate
        # doesn't poison the cache for a day.
        if use_cache and not soft_blocked:
            id_cache.put_book_id(
                isbn=query.isbn, asin=query.asin,
                title=query.title, author=query.author,
                book_id=None, tier=None,
            )
        return ResolveResult(None, None, soft_blocked)
    finally:
        if owned_client:
            try:
                await client.aclose()
            except Exception:
                pass


async def _tier1_auto_complete(
    client: httpx.AsyncClient, identifier: str
) -> Optional[str]:
    """Hit Goodreads' undocumented auto_complete JSON endpoint.

    Returns the goodreads_book_id, the string `"_soft_blocked"` if the
    response looks like a Cloudflare 202 gate, or None on any other
    miss/error.

    The endpoint is NOT in robots.txt's `*` Disallow list. Identifier-
    based (not free-text), so it doesn't conflict with the `/search`
    rule we're avoiding.

    v2.13.0: still uses httpx (auto_complete is a single-shot JSON
    probe, not the heavy HTML burst surface that needs curl_cffi
    chrome120 impersonation). Detection + runtime-state flag write
    routed through `app.metadata.goodreads_session` so the dispatcher
    skip + Settings status card see the same signal whether the 202
    came from this tier or from the heavy HTML fetchers.
    """
    from app.metadata import goodreads_session  # avoid circular import at module load

    try:
        resp = await client.get(_GOODREADS_AUTO_COMPLETE + identifier)
    except Exception as e:
        _log.debug("resolver: tier1 auto_complete network error: %s", e)
        return None

    # Cloudflare soft-block: 202 with empty body. Surface as a distinct
    # signal so callers can distinguish "Goodreads doesn't know this
    # book" from "Goodreads is blocking us at the network layer."
    if goodreads_session.is_cloudflare_soft_block(resp):
        goodreads_session.mark_soft_blocked(last_status=resp.status_code)
        _log.info(
            "resolver: tier1 auto_complete soft-blocked (status=%d, "
            "empty body) — Goodreads session state flipped to soft_blocked",
            resp.status_code,
        )
        return "_soft_blocked"

    if resp.status_code != 200:
        _log.debug(
            "resolver: tier1 auto_complete unexpected status %d",
            resp.status_code,
        )
        return None

    try:
        data = resp.json()
    except Exception:
        _log.debug("resolver: tier1 auto_complete non-JSON response")
        return None

    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            book_id = first.get("bookId")
            if book_id:
                return str(book_id)
    return None


async def _tier3_openlibrary(
    client: httpx.AsyncClient, isbn: str
) -> Optional[str]:
    """Query Open Library's books API for `identifiers.goodreads`.

    OL's coverage of the goodreads cross-reference is sparse but
    populated for a meaningful fraction of older / well-cataloged
    books (Charlotte's Web returns it; recent indie self-pub
    typically doesn't). When present, return the first goodreads_id.
    """
    try:
        resp = await client.get(
            _OPENLIBRARY_BOOKS,
            params={
                "bibkeys": f"ISBN:{isbn}",
                "jscmd": "data",
                "format": "json",
            },
        )
    except Exception as e:
        _log.debug("resolver: tier3 openlibrary network error: %s", e)
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except Exception:
        return None

    # Response shape: {"ISBN:1234567890": {"identifiers": {"goodreads": ["id"]}}}
    book_entry = data.get(f"ISBN:{isbn}")
    if not isinstance(book_entry, dict):
        return None
    idents = book_entry.get("identifiers")
    if not isinstance(idents, dict):
        return None
    gr_ids = idents.get("goodreads")
    if isinstance(gr_ids, list) and gr_ids:
        return str(gr_ids[0])
    return None
