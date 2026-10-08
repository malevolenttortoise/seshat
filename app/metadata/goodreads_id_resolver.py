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

    2. Hardcover GraphQL `book_mappings` — purpose-built for goodreads
       cross-references. One GraphQL roundtrip joins editions →
       book → book_mappings to pull the Goodreads ID without ever
       touching Goodreads itself. Requires a Hardcover API key.

    3. Open Library `?bibkeys=ISBN:{isbn}&jscmd=data&format=json`
       — returns `identifiers.goodreads` for some records. Free,
       no key required. Coverage is sparse (data-quality dependent)
       but it's a useful gap-filler for older / well-cataloged
       books.

    4. /book/auto_complete?format=json&q={title} (v2.13.2) — same
       endpoint as tier 1, but free-text title search. Goodreads'
       internal autocomplete returns up to 5 ranked book results
       with `bookId`, `workId`, and `author.id`. We post-filter
       results by the caller-supplied `author_goodreads_id` so an
       unrelated book that happens to share a title (boxed sets,
       parodies, same-name novels) cannot be picked. When the
       caller has no author_goodreads_id, this tier returns None
       — unconstrained title matches are too risky.

    5. /author/list/{author_goodreads_id} bibliography walk
       (v2.13.2) — page-by-page, schema.org/Book microdata extract,
       fuzzy title-match against the caller's title. Cached in the
       `author_bib` scope of `id_cache` (7-day TTL) so a Sanderson
       lookup pays the page walk once, not per-book. Implemented in
       `app.metadata.goodreads_bibliography`.

If all five tiers miss, return None — the caller (typically the
Goodreads source) skips and the enricher dispatcher moves to the
next source in the priority chain. We do NOT fall back to the
disallowed `/search` endpoint, even though kiwidude's Calibre
plugin does. Holding a higher standard is a deliberate choice.
"""
from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.metadata import source_gate

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
    """What we know about a book that needs a goodreads_book_id.

    `author_goodreads_id` (v2.13.2) anchors the title-search and
    bibliography tiers (T4, T5). When unset, those tiers no-op —
    we won't accept an unconstrained title match.
    """
    title: str = ""
    author: str = ""
    isbn: str = ""
    asin: str = ""
    author_goodreads_id: str = ""


@dataclass
class ResolveResult:
    """Outcome of a resolver chain attempt."""
    goodreads_book_id: Optional[str]
    # "auto_complete" (T1 ISBN/ASIN) | "hardcover" | "openlibrary"
    # | "auto_complete_title" (T4) | "bibliography" (T5) | None on miss
    tier: Optional[str]
    soft_blocked: bool = False  # True if a tier responded with a 202 / Cloudflare gate


async def resolve_goodreads_id(
    query: ResolveQuery,
    *,
    client: Optional[httpx.AsyncClient] = None,
    use_cache: bool = True,
) -> ResolveResult:
    """Run the tiered resolver chain. First hit wins.

    Every tier's request waits its source's turn in the source gate
    (Goodreads, Hardcover, OpenLibrary) and counts there, under the
    caller that ran the resolver (a scan, enrichment, the backfill), or
    `resolver` when none did.

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
    with source_gate.caller(source_gate.CALLER_RESOLVER):
        return await _resolve_goodreads_id(
            query, client=client, use_cache=use_cache,
        )


async def _resolve_goodreads_id(
    query: ResolveQuery,
    *,
    client: Optional[httpx.AsyncClient],
    use_cache: bool,
) -> ResolveResult:
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

        # ── Tier 2: Hardcover book_mappings ─────────────────────
        # Hardcover's GraphQL `book_mappings` table cross-references
        # each book to identifiers on other platforms (Goodreads,
        # Audible, Google Books, etc.). When we have an ISBN or ASIN
        # we can do a single editions→book→book_mappings join to get
        # the Goodreads ID without ever touching Goodreads.
        for ident_kind, ident_value in (("isbn_13", query.isbn), ("asin", query.asin)):
            if not ident_value:
                continue
            tier2 = await _tier2_hardcover_book_mappings(
                ident_kind, ident_value,
            )
            if tier2:
                _log.debug(
                    "resolver: tier2 (hardcover book_mappings) hit for %s=%s "
                    "→ goodreads_id=%s",
                    ident_kind, ident_value, tier2,
                )
                result = ResolveResult(tier2, "hardcover", soft_blocked)
                if use_cache:
                    id_cache.put_book_id(
                        isbn=query.isbn, asin=query.asin,
                        title=query.title, author=query.author,
                        book_id=tier2, tier="hardcover",
                    )
                return result

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

        # ── Tier 4: Goodreads auto_complete by title ───────────
        # (v2.13.2) Free-text title query through the same robots-
        # permitted auto_complete endpoint as T1. Filter by the
        # caller-supplied author_goodreads_id so a same-titled book
        # by a different author can never be picked. No author_id →
        # no T4 (unconstrained title matches are too risky).
        if query.title and query.author_goodreads_id:
            tier4 = await _tier4_auto_complete_title(
                client, query.title, query.author_goodreads_id,
            )
            if tier4 == "_soft_blocked":
                soft_blocked = True
            elif tier4:
                _log.debug(
                    "resolver: tier4 (auto_complete title) hit for title=%r "
                    "author_id=%s → goodreads_id=%s",
                    query.title, query.author_goodreads_id, tier4,
                )
                result = ResolveResult(tier4, "auto_complete_title", soft_blocked)
                if use_cache:
                    id_cache.put_book_id(
                        isbn=query.isbn, asin=query.asin,
                        title=query.title, author=query.author,
                        book_id=tier4, tier="auto_complete_title",
                    )
                return result

        # ── Tier 5: Author bibliography walk ───────────────────
        # (v2.13.2) When T4 missed but we have an author_goodreads_id,
        # walk /author/list/{id} pages and fuzzy-match titles. Cached
        # per-author so we only pay the page walk once.
        if query.title and query.author_goodreads_id:
            from app.metadata.goodreads_bibliography import find_book_in_bibliography
            tier5 = await find_book_in_bibliography(
                query.author_goodreads_id, query.title,
            )
            if tier5 == "_soft_blocked":
                soft_blocked = True
            elif tier5:
                _log.debug(
                    "resolver: tier5 (bibliography) hit for title=%r "
                    "author_id=%s → goodreads_id=%s",
                    query.title, query.author_goodreads_id, tier5,
                )
                result = ResolveResult(tier5, "bibliography", soft_blocked)
                if use_cache:
                    id_cache.put_book_id(
                        isbn=query.isbn, asin=query.asin,
                        title=query.title, author=query.author,
                        book_id=tier5, tier="bibliography",
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


async def _autocomplete(client: httpx.AsyncClient, url: str) -> Any:
    """One `auto_complete` request, in Goodreads' turn."""
    from app.metadata import goodreads_session  # avoid circular import at module load

    async with source_gate.turn(
        "goodreads", kind=source_gate.KIND_AUTOCOMPLETE,
    ) as turn:
        resp = await client.get(url)
        if goodreads_session.is_cloudflare_soft_block(resp):
            turn.block()
        else:
            turn.status(resp.status_code)
    return resp


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
        resp = await _autocomplete(client, _GOODREADS_AUTO_COMPLETE + identifier)
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


async def _tier4_auto_complete_title(
    client: httpx.AsyncClient,
    title: str,
    author_goodreads_id: str,
) -> Optional[str]:
    """Resolve a goodreads_book_id by title, anchored to a known author.

    (v2.13.2) Hits the same `/book/auto_complete?format=json&q=...`
    endpoint as T1 but with the URL-encoded title as the query.
    Goodreads' autocomplete returns up to 5 ranked results, each a
    dict with `bookId`, `workId`, `author.id`, `author.name`,
    `title`, `ratingsCount`, etc.

    The 5 results are ranked by Goodreads' relevance/popularity
    function, which empirically polluted with parodies and boxed
    sets when the query was `title + author`. Title-only is cleaner;
    we then post-filter by `author_goodreads_id` to reject results
    by other authors.

    Returns:
      - The Goodreads book ID string when at least one result has
        `author.id == author_goodreads_id`. When multiple match,
        pick the highest `ratingsCount` (most-popular edition).
      - The string `"_soft_blocked"` if the response looks like the
        Cloudflare 202 / empty-body interstitial.
      - None on any other miss / network error / parse error.
    """
    if not title or not author_goodreads_id:
        return None

    from app.metadata import goodreads_session  # avoid circular import

    encoded_title = urllib.parse.quote(title.strip())
    try:
        resp = await _autocomplete(client, _GOODREADS_AUTO_COMPLETE + encoded_title)
    except Exception as e:
        _log.debug("resolver: tier4 auto_complete network error: %s", e)
        return None

    if goodreads_session.is_cloudflare_soft_block(resp):
        goodreads_session.mark_soft_blocked(last_status=resp.status_code)
        _log.info(
            "resolver: tier4 auto_complete soft-blocked (status=%d, "
            "empty body) — Goodreads session state flipped to soft_blocked",
            resp.status_code,
        )
        return "_soft_blocked"

    if resp.status_code != 200:
        _log.debug(
            "resolver: tier4 auto_complete unexpected status %d",
            resp.status_code,
        )
        return None

    try:
        data = resp.json()
    except Exception:
        _log.debug("resolver: tier4 auto_complete non-JSON response")
        return None

    if not isinstance(data, list):
        return None

    # Filter to results matching the anchor author. Compare as strings
    # because Goodreads returns author.id as int but author_goodreads_id
    # is stored as a string in our DB.
    target = str(author_goodreads_id).strip()
    matches: list[dict] = []
    for hit in data:
        if not isinstance(hit, dict):
            continue
        author = hit.get("author") or {}
        author_id = author.get("id")
        if author_id is None:
            continue
        if str(author_id).strip() == target and hit.get("bookId"):
            matches.append(hit)

    if not matches:
        return None

    # When multiple matches (e.g. boxed-set + individual book by the
    # same author), prefer the most-rated one — that's the canonical
    # edition most callers want.
    matches.sort(
        key=lambda h: int(h.get("ratingsCount") or 0),
        reverse=True,
    )
    return str(matches[0]["bookId"])


_HARDCOVER_API = "https://api.hardcover.app/v1/graphql"

# Single-roundtrip GraphQL query: editions filtered by ISBN-13 or ASIN
# → book → book_mappings restricted to platform "Goodreads". Limits all
# of editions/book_mappings to 1 so Hardcover doesn't return a giant
# graph for popular titles with many editions.
_HARDCOVER_BOOK_MAPPINGS_QUERY = """
query GoodreadsMapping($ident_kind: editions_bool_exp!) {
  editions(where: $ident_kind, limit: 1) {
    book {
      book_mappings(
        where: {platform: {name: {_eq: "Goodreads"}}}
        limit: 1
      ) {
        external_id
      }
    }
  }
}
"""


async def _tier2_hardcover_book_mappings(
    ident_kind: str, ident_value: str,
) -> Optional[str]:
    """Resolve a Goodreads book ID via Hardcover's GraphQL
    `book_mappings` cross-reference.

    Hardcover's API ships a `book_mappings` table that joins each book
    to its identifiers on other platforms (Goodreads, Audible, Google
    Books, etc.). When we have an ISBN-13 or ASIN we can do one GraphQL
    roundtrip to get the Goodreads cross-ref without ever touching
    Goodreads itself.

    Args:
      ident_kind: "isbn_13" or "asin" — the editions-table column
                  we're filtering on.
      ident_value: The actual identifier value.

    Returns:
      The Goodreads book ID string on hit, None on miss / no Hardcover
      API key configured / any error. Errors are swallowed (returns
      None) — Tier 3 (OpenLibrary) is the next fall-through.
    """
    # Hardcover requires a Bearer token. Without one, this tier no-ops
    # (Tier 3 OL is free + no-key and covers the same cross-reference
    # need for many books).
    from app.config import load_settings
    from app.secrets import get_secret

    settings = load_settings()
    api_key = (await get_secret("hardcover_api_key")) or settings.get(
        "hardcover_api_key", ""
    )
    if not api_key:
        return None
    token = api_key.strip()
    if " " not in token:
        token = f"Bearer {token}"

    # Wrap the identifier into the editions_bool_exp shape Hardcover's
    # schema expects. Building it caller-side keeps the GraphQL query
    # a single static string.
    where_clause: dict = {ident_kind: {"_eq": ident_value}}

    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Seshat/2.13",
                "Authorization": token,
            },
        ) as client:
            async with source_gate.turn("hardcover") as turn:
                resp = await client.post(
                    _HARDCOVER_API,
                    json={
                        "query": _HARDCOVER_BOOK_MAPPINGS_QUERY,
                        "variables": {"ident_kind": where_clause},
                    },
                )
                turn.status(resp.status_code)
    except Exception as e:
        _log.debug("resolver: tier2 hardcover network error: %s", e)
        return None

    if resp.status_code != 200:
        _log.debug(
            "resolver: tier2 hardcover unexpected status %d", resp.status_code,
        )
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    if "errors" in data:
        _log.debug("resolver: tier2 hardcover graphql errors: %s", data["errors"])
        return None

    editions = (data.get("data") or {}).get("editions") or []
    if not editions:
        return None
    book = editions[0].get("book") or {}
    mappings = book.get("book_mappings") or []
    if not mappings:
        return None
    external_id = mappings[0].get("external_id")
    if not external_id:
        return None
    return str(external_id)


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
        resp = await source_gate.request("openlibrary", lambda: client.get(
            _OPENLIBRARY_BOOKS,
            params={
                "bibkeys": f"ISBN:{isbn}",
                "jscmd": "data",
                "format": "json",
            },
        ))
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
