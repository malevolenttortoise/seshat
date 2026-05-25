"""
Amazon metadata source — web scraping.

Scrapes Amazon's Kindle Store search + product detail pages. No API
key required — uses realistic browser headers to pass bot detection.

Two-pass flow:
  1. Search: amazon.com/s with explicit book-store parameters
  2. Detail: amazon.com/dp/{ASIN} for rich metadata

Based on analysis of CWA's proven Amazon scraper, this implementation:
  - Uses plain requests.Session (NOT cloudscraper — less fingerprint)
  - Includes Accept-Encoding header (critical for bot detection)
  - Uses explicit search params (unfiltered, sort, search-alias)
  - Extracts high-res covers from script JSON, not img elements
  - Filters pre-order pages
  - Uses data-feature-name selectors where possible (more stable)

Amazon aggressively blocks automated requests. If Amazon returns
CAPTCHAs or 503s consistently, this source degrades gracefully
(returns None) and the enricher falls through to the next provider.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Optional

import requests

from app.metadata.record import MetaRecord
from app.metadata.sources.base import MetaSource
from app.metadata.text_clean import description_to_plain_text

_log = logging.getLogger("seshat.metadata.amazon")

_SEARCH_URL = "https://www.amazon.com/s"
_PRODUCT_URL = "https://www.amazon.com/dp"

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) "
    "Gecko/20100101 Firefox/143.0"
)

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-US,en;q=0.9",
}

# High-res cover extraction from script JSON blocks.
_HIRES_RE = re.compile(r'"hiRes"\s*:\s*"([^"]+)"')

# Junk-listing pre-filter — third-party seller titles, bracketed
# format suffixes, and "By AUTHOR — Title" sham listings. These
# slip into Amazon search results and waste a detail-page fetch
# returning nothing useful. Examples it catches:
#   "[(Kingdom's Hope )] [Author: Chuck Black] [May-2006]"
#   "By BLACK CHUCK - SIR KENDRICK..."
#   "By Chuck Black - Kingdom's Edge (2006-05-16) [Paperback]"
_RX_JUNK_TITLE = re.compile(
    r'^\[?\(|'                    # starts with [( or (
    r'^By\s+[A-Z].*\s+-\s+|'      # "By AUTHOR - Title" seller format
    r'\[\s*(?:Paperback|Hardcover|Mass Market|Library Binding)\s*\]|'
    r'\)\s*(?:Paperback|Hardcover|Mass Market|Library Binding)\s*$|'
    r'by\s+\w+,\s+\w+\s+\(\d{4}\)\s+(?:Paperback|Hardcover)',
    re.IGNORECASE,
)

# Audiobook-format indicators found in RPI cards or page subtitle
# text. Seshat is an ebook pipeline — Audible / Audio CD results
# never produce a usable artifact, and they otherwise win against
# the actual ebook entry when their title matches more cleanly.
_AUDIO_FORMAT_KEYWORDS = {"audible", "audiobook", "audio cd", "listening length"}


class AmazonSource(MetaSource):
    name = "amazon"
    default_timeout = 15.0

    def __init__(self, *, rate_limit: float = 1.5):
        super().__init__(rate_limit=rate_limit)
        self._session: Optional[requests.Session] = None
        # v2.31.0 Tier 3 — lazy curl_cffi AsyncSession for the Author
        # Store fetch path. Kept separate from the requests.Session
        # because amazon.com/stores/author/* is Akamai-protected and
        # only the Chrome 120 impersonation handshake gets through.
        self._cffi_session = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(_HEADERS)
        return self._session

    def _fetch_sync(self, url: str, params: dict = None) -> Optional[str]:
        session = self._get_session()
        time.sleep(self.rate_limit)
        try:
            r = session.get(url, params=params, timeout=self.default_timeout)
            if r.status_code == 200:
                return r.text
            _log.info("amazon: HTTP %d for %s", r.status_code, url)
            return None
        except Exception as e:
            _log.debug("amazon fetch error: %s", e)
            return None

    async def _fetch(self, url: str, params: dict = None) -> Optional[str]:
        return await asyncio.to_thread(self._fetch_sync, url, params)

    def is_cheap_for(
        self,
        *,
        author_amazon_id: str = "",
        library_slug: str = "",
        **_,
    ) -> bool:
        """Return True iff this source can serve a hit for the given
        identifiers from the local Amazon metadata cache (no HTTP).

        Read by the enricher (F3) to decide whether to short-circuit
        through cheap sources after a good-enough fuzzy match. The
        probe is a single indexed SQLite SELECT — finishes in <1ms in
        practice, so doing it synchronously inside the enricher's loop
        doesn't meaningfully delay anything. Empty / non-ASIN-shaped
        author IDs short-circuit to False without touching the file.
        """
        if not _is_amazon_author_id_shape(author_amazon_id):
            return False
        return _sync_author_has_cached_books(
            author_id=author_amazon_id,
            library_slug=library_slug or "",
        )

    async def search_book(
        self,
        title: str,
        author: str,
        *,
        author_amazon_id: str = "",
        library_slug: str = "",
        **_,
    ) -> Optional[MetaRecord]:
        if not title:
            return None

        # v2.29.0 — cache-first phase. When the upstream pipeline
        # resolved an Amazon Author Store ID (10-char ASIN shape), we
        # can score the title against cached widget rows for that
        # author instead of hitting amazon.com/s. On hit, one
        # detail-page fetch suffices. On miss, enqueue a high-priority
        # worker rescan for the author and fall through to the live
        # /s search (with the F2 audiobook-retry loop intact).
        if _is_amazon_author_id_shape(author_amazon_id):
            cached = await self._cache_first_search(
                title=title, author=author,
                author_amazon_id=author_amazon_id,
                library_slug=library_slug,
            )
            if cached is not None:
                return cached

        live_hit = await self._live_search(title=title, author=author)
        if live_hit is not None:
            return live_hit

        # v2.31.0 — Tier 3 Author Store fallback. Both the cache phase
        # (Tier 1) and the live /s search (Tier 2) failed to surface a
        # usable record. When `author_amazon_id` is a verified Author
        # Store ID we can hit the storefront directly: the URL is keyed
        # on the verified author, so any title-scored hit is
        # author-identity-guaranteed by construction. Single SSR fetch
        # via curl_cffi Chrome 120 + parse the embedded widget JSON
        # (~85 candidate products) + one detail fetch for the best
        # binding-match. No /juvec — the F1 enqueue-on-miss above
        # already arranged a worker rescan for full coverage.
        if _is_amazon_author_id_shape(author_amazon_id):
            return await self._author_store_search(
                title=title, author=author,
                author_amazon_id=author_amazon_id,
            )

        return None

    async def _live_search(
        self, *, title: str, author: str,
    ) -> Optional[MetaRecord]:
        """v2.29.0 live /s search — F2 audiobook-retry loop.

        Hits amazon.com/s with explicit book-store parameters, ranks
        the top 3 hits by title score, walks the ranked list fetching
        detail pages until one parses to a usable record (rejecting
        audiobook + pre-order pages along the way).

        Returns None when the search response is empty, no product
        links surface, or every detail-page candidate is rejected.
        Tier 3 callers handle None.
        """
        query = f"{title} {author}".strip()
        search_html = await self._fetch(
            _SEARCH_URL,
            params={
                "field-keywords": query,
                "i": "digital-text",
                "search-alias": "stripbooks",
                "unfiltered": "1",
                "sort": "relevanceexprank",
            },
        )
        if not search_html:
            return None

        from bs4 import BeautifulSoup
        from app.metadata.scoring import score_match

        soup = BeautifulSoup(search_html, "lxml")

        # Extract product links from search results — deduplicate by URL.
        links: list[str] = []
        for container in soup.find_all(attrs={"data-component-type": "s-search-results"}):
            for a in container.find_all("a", href=True):
                href = a["href"]
                if "/dp/" not in href:
                    continue
                base = href.split("?")[0]
                if base not in links:
                    links.append(base)

        # Fallback: try data-asin attribute extraction.
        if not links:
            for r in soup.select("[data-asin]"):
                asin = r.get("data-asin", "").strip()
                if asin:
                    url = f"/dp/{asin}"
                    if url not in links:
                        links.append(url)

        if not links:
            return None

        # Score the first 3 search hits and rank them — v2.29.0 swaps
        # the old "pick best, single detail fetch" path for a ranked
        # loop so an audiobook page at rank 1 (which `_parse_detail_page`
        # rejects) falls through to the next-ranked ebook candidate
        # instead of returning None. Same total cap of 3 detail fetches.
        scored: list[tuple[str, str, float]] = []  # (asin, link, score)
        for link in links[:3]:
            asin = _extract_asin(link)
            if not asin:
                continue
            # Title text near this link tells us how well the hit
            # matches the query. Missing/empty title → score 0.3
            # placeholder (some search rows lack a visible title but
            # are still real product links worth trying).
            result_score: Optional[float] = None
            for a in soup.find_all("a", href=lambda h: h and asin in h):
                title_el = a.select_one("span")
                if title_el:
                    result_title = title_el.get_text(strip=True)
                    if _RX_JUNK_TITLE.search(result_title):
                        _log.debug("amazon: SKIP junk title: %r", result_title)
                        result_score = -1.0  # sentinel: drop entirely
                        break
                    result_score = score_match(
                        record_title=result_title,
                        record_authors=[],
                        search_title=title,
                        search_authors=author,
                    )
                    break
            if result_score is None:
                result_score = 0.3  # untitled fallback (matches pre-v2.29.0 behavior)
            if result_score < 0.0:
                continue
            scored.append((asin, link, result_score))

        if not scored:
            return None

        scored.sort(key=lambda x: x[2], reverse=True)

        # Walk the ranked list, fetching each detail page until one
        # parses to a usable record. `_parse_detail_page` returns None
        # for audiobook + pre-order pages — those don't count toward
        # the "match" and we move to the next candidate. Worst case is
        # 3 detail fetches × rate_limit (~1.5s each) = 4.5s, well under
        # the per-source 15s timeout.
        for asin, _link, score in scored:
            if score < 0.2:
                break
            detail_html = await self._fetch(f"{_PRODUCT_URL}/{asin}")
            if not detail_html:
                continue
            record = _parse_detail_page(detail_html, asin)
            if record is not None:
                return record
            _log.debug(
                "amazon: candidate %s rejected (audiobook/preorder); "
                "trying next", asin,
            )

        return None

    async def _cache_first_search(
        self,
        *,
        title: str,
        author: str,
        author_amazon_id: str,
        library_slug: str,
    ) -> Optional[MetaRecord]:
        """v2.29.0 cache-first phase.

        Returns a :class:`MetaRecord` when the local Amazon metadata
        cache surfaces a strong candidate for ``(title, author)`` under
        ``author_amazon_id`` — one detail-page fetch needed. Returns
        ``None`` on cache miss; in that case the caller falls through
        to the live ``/s`` path.

        Cache miss also enqueues a high-priority worker rescan for
        ``author_amazon_id`` so the cache catches up the next time
        the same book is enriched.

        Hint for ``book_format``: we prefer ``kindle_edition`` when
        the source is in its default ebook-pipeline configuration and
        ``audible_audiobook`` when the enricher is operating on an
        audiobook grab (the ``_audiobook_hint`` attribute set by
        :class:`MetadataEnricher`).
        """
        try:
            from app.discovery.metadata_cache_reader import (
                ensure_enqueued, read_books_by_author, SOURCE_AMAZON,
            )
            from app.metadata.scoring import score_match
        except Exception:
            return None

        book_format = (
            "audible_audiobook"
            if getattr(self, "_audiobook_hint", False)
            else "kindle_edition"
        )
        try:
            rows = await read_books_by_author(
                source_name=SOURCE_AMAZON,
                author_id=author_amazon_id,
                library_slug=library_slug or "",
                book_format=book_format,
                language="English",
            )
        except Exception as e:
            _log.debug("amazon cache lookup failed: %s", e)
            return None

        if not rows:
            # Cache miss for this author — high-priority rescan so the
            # next attempt hits. Best-effort; failures are non-fatal.
            try:
                await ensure_enqueued(
                    source_name=SOURCE_AMAZON,
                    author_id=author_amazon_id,
                    priority=1000.0,
                    enqueued_reason="enrich_miss",
                )
            except Exception as e:
                _log.debug("amazon enqueue-on-miss failed: %s", e)
            return None

        # Score each cached title against the search query plus a
        # volume-agreement tiebreaker.
        #
        # Why the tiebreaker: when an author has multiple books in a
        # series (e.g. "Idle Village Hero", "Idle Village Hero 2",
        # "Idle Village Hero 3", "Idle Village Hero 4"), the search
        # title for book 1 has no volume marker. Pure Jaccard scoring
        # tokenizes "2", "3", "4" as ordinary tokens that contribute
        # equally to the intersection/union pair, so all four siblings
        # tie at the same score. score_match's volume-mismatch guard
        # only fires when BOTH sides have volume markers — a
        # no-volume query against numbered candidates can't disambiguate.
        # Live /s sidesteps this because Amazon's search ranks book 1
        # first; the cache scores everything in the catalog and would
        # otherwise pick whichever sibling SQLite returned first.
        from app.metadata.scoring import _extract_volume
        search_volume = _extract_volume(title)

        scored: list[tuple[float, int, dict]] = []  # (score, vol_bonus, row)
        for row in rows:
            row_title = row.get("title") or ""
            if not row_title:
                continue
            sc = score_match(
                record_title=row_title,
                record_authors=[],
                search_title=title,
                search_authors=author,
            )
            # Volume-agreement tiebreaker:
            #   +1 when both sides agree (both have the same volume,
            #          or both have no volume marker)
            #    0 when one side has a volume and the other doesn't
            #   -1 when both sides have volumes that disagree
            row_volume = _extract_volume(row_title)
            if search_volume == row_volume:
                vol_bonus = 1
            elif search_volume is None or row_volume is None:
                vol_bonus = 0
            else:
                vol_bonus = -1
            scored.append((sc, vol_bonus, row))

        if not scored:
            return None

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        best_score, _best_bonus, best_row = scored[0]
        # Threshold for cache acceptance. The cache rows are
        # pre-filtered to the queried author_id, so title similarity
        # is the ONLY signal that matters here — a low title score
        # vs. another book in the same author's catalog is still
        # better-than-fuzzy because the author identity is verified.
        # 0.3 catches subtitle variations like "A Slice-of-Life
        # LitRPG" (MAM filename) vs. "A Town-Building LitRPG" (Amazon
        # detail page) for Idle Village Hero — observed live at 0.475.
        # Anything materially below 0.3 is a different book of the
        # same author.
        if best_score < 0.3:
            return None

        best_asin = best_row.get("book_asin") or ""
        if not _AMAZON_AUTHOR_ID_RE.match(best_asin or ""):
            # Book ASINs and author ASINs share the 10-char shape.
            # Bail out if the row is malformed.
            return None

        detail_html = await self._fetch(f"{_PRODUCT_URL}/{best_asin}")
        if not detail_html:
            return None
        record = _parse_detail_page(detail_html, best_asin)
        if record is None:
            # Detail page rejected (audiobook / pre-order). Fall back
            # to the live path so we don't return None just because
            # the cache pointed at a stale row.
            return None

        # Cache-row fields fill gaps the detail-page parse didn't cover
        # (cover_url, series, language, isbn).
        record = _merge_cache_into_record(record, best_row)
        # Cache-first verified the author identity via author_id — set
        # ``authors`` to the search author so the enricher's re-score
        # gets author-overlap credit. Pre-this-fix `_parse_detail_page`
        # always returned authors=[], dropping the score's author
        # contribution to 0 and stranding cache hits below the 0.8
        # accept_confidence threshold even though the book identity
        # was confirmed (Master Alvin came in at 0.77, status
        # below_threshold, so its cover never merged).
        if author and not record.authors:
            record.authors = [author]
        try:
            record._from_cache = True  # type: ignore[attr-defined]
        except Exception:
            pass
        return record

    async def _author_store_search(
        self,
        *,
        title: str,
        author: str,
        author_amazon_id: str,
    ) -> Optional[MetaRecord]:
        """v2.31.0 Tier 3 — Author Store direct fetch.

        Single GET of ``/stores/author/<asin>/allbooks`` via curl_cffi
        Chrome 120, parse the embedded SSR widget JSON, filter the
        returned products to the active binding (Kindle or Audible),
        score titles against the query, fetch one detail page for the
        best match. Same ``_from_cache=True`` treatment as
        :meth:`_cache_first_search` so the enricher's merge gate
        bypass applies — the URL is keyed on the verified author ID,
        so author identity is guaranteed by construction.

        **SSR-only coverage.** This phase reads only the products
        baked into the storefront's initial server-side render
        (typically the first 29-ish across all bindings, ~15-20
        Kindle-bound). Authors with larger catalogs hide later books
        behind ``/juvec`` pagination that the discovery worker walks
        but Tier 3 deliberately does not — Tier 3 is a best-effort
        first-pass while the F1 enqueue-on-miss arranges a worker
        rescan that populates the cache for the next attempt. If the
        right book isn't in the SSR widget Tier 3 returns ``None``
        (rather than guessing) and the eventual cache-backed Tier 1
        re-attempt picks it up.

        Returns ``None`` on:
          - curl_cffi unavailable (install missing)
          - Amazon soft-block cooldown active (penalty box)
          - allbooks GET non-200 / soft-block-suspected
          - widget parse failed
          - no binding-matched product scored above 0.4 (the
            author-only floor — see threshold comment below)
          - detail-page parse rejected (audiobook / pre-order)
        """
        html = await self._fetch_author_store_html(author_amazon_id)
        if not html:
            return None

        try:
            from app.discovery.sources.amazon_widget_parser import (
                FILTER_TO_BINDING, ParseError, parse_allbooks_html,
            )
        except Exception as e:
            _log.debug("amazon author-store: widget parser import failed: %s", e)
            return None

        try:
            page_data = parse_allbooks_html(html)
        except ParseError as e:
            # SoftBlockSuspectedError is a subclass; both surface here.
            # `_fetch_author_store_html` already recorded the soft-block
            # for the 202/429 + thin-body cases; bare parse failures on
            # a substantive body are one-off schema drift, treat as miss.
            _log.debug("amazon author-store: parse failed (%s)", e)
            return None
        except Exception as e:
            _log.debug("amazon author-store: unexpected parse error: %s", e)
            return None

        target_binding = (
            FILTER_TO_BINDING["audible_audiobook"]
            if getattr(self, "_audiobook_hint", False)
            else FILTER_TO_BINDING["kindle"]
        )
        candidates = [
            p for p in page_data.products
            if p.binding_symbol == target_binding
        ]
        if not candidates:
            _log.debug(
                "amazon author-store: no %s-binding products for %s "
                "(total products=%d)",
                target_binding, author_amazon_id, len(page_data.products),
            )
            return None

        from app.metadata.scoring import _extract_volume, score_match
        search_volume = _extract_volume(title)

        # Score each candidate + apply the same volume-agreement
        # tiebreaker the cache path uses (v2.29.0 273e28a). Storefront
        # listings for an author with a numbered series tie on pure
        # Jaccard the same way cache rows do.
        scored: list[tuple[float, int, object]] = []
        for product in candidates:
            if not product.title or not product.asin:
                continue
            sc = score_match(
                record_title=product.title,
                record_authors=list(product.contributors),
                search_title=title,
                search_authors=author,
            )
            row_volume = _extract_volume(product.title)
            if search_volume == row_volume:
                vol_bonus = 1
            elif search_volume is None or row_volume is None:
                vol_bonus = 0
            else:
                vol_bonus = -1
            scored.append((sc, vol_bonus, product))

        if not scored:
            return None

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        best_score, _best_bonus, best_product = scored[0]
        # The score_match formula is ``0.7 * title + 0.3 * author``;
        # a pure-author hit with zero title overlap floors at exactly
        # 0.300. The Author Store URL guarantees author identity for
        # every candidate on the page, so EVERY product matches on
        # author and the floor is meaningless — only meaningful title
        # similarity can disambiguate the right book from the rest of
        # the author's catalog. Threshold 0.40 requires at least ~0.14
        # title contribution over the author-only floor, which the
        # IVH→IVH-book-1 case clears at ~0.475 (observed) while
        # author-only mismatches at 0.300 are correctly rejected.
        if best_score < 0.4:
            _log.debug(
                "amazon author-store: best candidate %s scored %.3f < 0.4 "
                "(author-only floor; the right book is not in the SSR "
                "widget for this author)",
                getattr(best_product, "asin", "?"), best_score,
            )
            return None

        best_asin = best_product.asin
        if not _AMAZON_AUTHOR_ID_RE.match(best_asin or ""):
            return None

        detail_html = await self._fetch(f"{_PRODUCT_URL}/{best_asin}")
        if not detail_html:
            return None
        record = _parse_detail_page(detail_html, best_asin)
        if record is None:
            return None

        # Widget fields fill detail-page gaps (cover/series). The
        # storefront product never carries ISBN or language, so those
        # come from the detail page or stay None.
        widget_row = {
            "cover_url": best_product.cover_url,
            "series_name": best_product.series_title,
            "series_pos": best_product.series_position,
        }
        record = _merge_cache_into_record(record, widget_row)
        if author and not record.authors:
            record.authors = [author]
        try:
            record._from_cache = True  # type: ignore[attr-defined]
        except Exception:
            pass
        return record

    async def _fetch_author_store_html(
        self, author_id: str,
    ) -> Optional[str]:
        """One GET of ``/stores/author/<author_id>/allbooks`` via the
        curl_cffi Chrome 120 impersonation session.

        Returns ``None`` on:
          - curl_cffi missing (degrade gracefully)
          - Amazon soft-block cooldown active (penalty box)
          - transport error / non-200 response

        202 (Akamai sensor challenge) and 429 (rate limit) record an
        IP-level soft-block via the shared discovery-side penalty box
        so subsequent author-store calls + worker scans short-circuit.
        """
        try:
            from app.discovery.amazon_author_id_resolver import (
                is_amazon_blocked,
                parse_retry_after,
                record_amazon_soft_block,
            )
            from app.discovery.sources.amazon import (
                _ALLBOOKS_URL_TEMPLATE,
                _create_impersonating_session,
            )
        except Exception as e:
            _log.debug(
                "amazon author-store: discovery helpers unavailable: %s", e,
            )
            return None

        if is_amazon_blocked():
            _log.info(
                "amazon author-store: skipped — soft-block cooldown active",
            )
            return None

        session = self._cffi_session
        if session is None:
            session = _create_impersonating_session()
            if session is None:
                # curl_cffi missing — log once at debug; the discovery
                # source already logged the install warning at startup.
                return None
            self._cffi_session = session

        url = _ALLBOOKS_URL_TEMPLATE.format(author_id=author_id)
        try:
            resp = await session.get(url, timeout=30.0)
        except Exception as e:
            _log.debug("amazon author-store fetch error: %s", e)
            return None

        status = getattr(resp, "status_code", None)
        body = getattr(resp, "text", None) or ""
        if status in (429, 202):
            headers = getattr(resp, "headers", None)
            raw_ra = None
            if headers is not None:
                try:
                    raw_ra = (
                        headers.get("Retry-After")
                        or headers.get("retry-after")
                    )
                except Exception:
                    raw_ra = None
            record_amazon_soft_block(
                f"enricher author-store GET {url} returned HTTP {status} "
                f"with {len(body)}-byte body"
                + (" (Akamai sensor challenge)" if status == 202 else ""),
                retry_after_s=parse_retry_after(raw_ra),
            )
            return None
        if status != 200 or not body:
            _log.debug(
                "amazon author-store: HTTP %s, %d-byte body — miss",
                status, len(body),
            )
            return None
        return body

    async def close(self) -> None:
        self._session = None
        if self._cffi_session is not None:
            try:
                close_method = getattr(self._cffi_session, "close", None)
                if close_method is not None:
                    result = close_method()
                    if hasattr(result, "__await__"):
                        await result
            except Exception:
                pass
            self._cffi_session = None
        await super().close()


def _extract_asin(url: str) -> Optional[str]:
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    return m.group(1) if m else None


# 10-char uppercase alphanumeric = Amazon Author Store ID shape. Mirrors
# the discovery-side `_is_amazon_author_id` heuristic but kept local
# so this module doesn't take a hard dependency on the discovery cache
# package.
_AMAZON_AUTHOR_ID_RE = re.compile(r"^[A-Z0-9]{10}$")


def _is_amazon_author_id_shape(value: str) -> bool:
    return bool(value) and bool(_AMAZON_AUTHOR_ID_RE.match(value))


def _sync_author_has_cached_books(*, author_id: str, library_slug: str) -> bool:
    """Synchronous existence probe against the Amazon metadata cache.

    Returns True iff (a) the state table records ``last_outcome='ok'``
    for the author AND (b) at least one books-table row exists for the
    same author. ``library_slug`` is optional — empty means "any
    library that scanned this author counts".

    Used by ``AmazonSource.is_cheap_for``. Kept sync because the cache
    is a local SQLite file and the query is index-covered (~sub-ms).
    """
    import sqlite3
    try:
        from app.discovery.metadata_cache import get_db_path, SOURCE_AMAZON
    except Exception:
        return False
    try:
        db_path = get_db_path(SOURCE_AMAZON)
    except Exception:
        return False
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return False
    try:
        if library_slug:
            row = conn.execute(
                "SELECT 1 FROM metadata_cache_amazon_state "
                "WHERE author_id = ? AND library_slug = ? "
                "AND last_outcome = 'ok' LIMIT 1",
                (author_id, library_slug),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM metadata_cache_amazon_state "
                "WHERE author_id = ? AND last_outcome = 'ok' LIMIT 1",
                (author_id,),
            ).fetchone()
        if row is None:
            return False
        if library_slug:
            row = conn.execute(
                "SELECT 1 FROM metadata_cache_amazon_books "
                "WHERE author_id = ? AND library_slug = ? LIMIT 1",
                (author_id, library_slug),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM metadata_cache_amazon_books "
                "WHERE author_id = ? LIMIT 1",
                (author_id,),
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _parse_detail_page(html_text: str, asin: str) -> Optional[MetaRecord]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "lxml")

    # Pre-order check — reject if this is a pre-order page.
    if soup.find("input", attrs={"name": "submit.preorder"}):
        _log.debug("amazon: skipping pre-order page for %s", asin)
        return None

    # Title.
    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(strip=True) if title_el else ""
    if not title:
        return None

    # RPI carousel cards — structured metadata.
    rpi = {}
    for card in soup.select("[id^='rpi-attribute-']"):
        card_id = card.get("id", "")
        val_el = (
            card.select_one(".rpi-attribute-value a span")
            or card.select_one(".rpi-attribute-value span")
        )
        lab_el = card.select_one(".rpi-attribute-label span")
        rpi[card_id] = {
            "value": val_el.get_text(strip=True) if val_el else "",
            "label": lab_el.get_text(strip=True) if lab_el else "",
        }

    # Audiobook detection — Amazon's audiobook pages use "Listening
    # Length" instead of page count and surface "Audible Audiobook"
    # in the format / subtitle area. Seshat is an ebook pipeline,
    # so audiobook results never produce a usable artifact and would
    # otherwise win against the actual ebook entry when their title
    # matches more cleanly. Reject before any further processing.
    rpi_text = " ".join(
        f"{v.get('label', '')} {v.get('value', '')}" for v in rpi.values()
    ).lower()
    if any(kw in rpi_text for kw in _AUDIO_FORMAT_KEYWORDS):
        _log.debug("amazon: skipping audiobook page for %s (%s)", asin, title[:60])
        return None
    subtitle_el = soup.select_one("#productSubtitle")
    if subtitle_el:
        subtitle = subtitle_el.get_text(strip=True).lower()
        if any(kw in subtitle for kw in _AUDIO_FORMAT_KEYWORDS):
            _log.debug("amazon: skipping audiobook (subtitle) for %s", asin)
            return None

    # Series from RPI.
    series_name = None
    series_index = None
    series_card = rpi.get("rpi-attribute-book_details-series", {})
    if series_card.get("value"):
        series_name = series_card["value"]
        label = series_card.get("label", "")
        m = re.search(r"Book\s+(\d+(?:\.\d+)?)", label)
        if m:
            try:
                series_index = float(m.group(1))
            except ValueError:
                pass

    # Also try series from data-feature-name widget (CWA pattern).
    if not series_name:
        series_widget = soup.find(attrs={"data-feature-name": "seriesBulletWidget"})
        if series_widget:
            text = series_widget.get_text(" ", strip=True)
            m = re.search(r"Book\s+(\d+)(?:\s+of\s+\d+)?:\s*(.+)", text)
            if m:
                try:
                    series_index = float(m.group(1))
                except ValueError:
                    pass
                series_name = m.group(2).strip()

    # Strip series from title if present.
    if series_name and series_name in title:
        title = re.sub(
            r"\s*\(" + re.escape(series_name) + r"[^)]*\)\s*$", "", title
        ).strip()

    # Page count.
    pages = None
    pages_card = rpi.get("rpi-attribute-book_details-ebook_pages", {})
    if pages_card.get("value"):
        m = re.search(r"(\d+)", pages_card["value"])
        if m:
            pages = int(m.group(1))

    # Publication date.
    pub_date = None
    date_card = rpi.get("rpi-attribute-book_details-publication_date", {})
    if date_card.get("value"):
        pub_date = _parse_amazon_date(date_card["value"])

    # Language.
    language = None
    lang_card = rpi.get("rpi-attribute-language", {})
    if lang_card.get("value"):
        language = lang_card["value"]

    # ISBN-13 + fallback pub date from detail bullets.
    isbn = None
    for li in soup.select(
        "#detailBulletsWrapper_feature_div li, "
        "#detailBullets_feature_div li"
    ):
        spans = li.select("span.a-text-bold")
        for s in spans:
            label = s.get_text(strip=True).replace("\u200f", "").replace("\u200e", "")
            val_span = s.find_next_sibling("span")
            val = val_span.get_text(strip=True) if val_span else ""
            if "ISBN-13" in label and val:
                isbn = val.replace("-", "")
            if "Publication date" in label and val and not pub_date:
                pub_date = _parse_amazon_date(val)

    # Description — prefer data-feature-name selector (more stable).
    description = None
    desc_el = soup.find("div", attrs={"data-feature-name": "bookDescription"})
    if desc_el:
        # Drill into nested divs for the actual text.
        inner = desc_el.find("div")
        if inner:
            inner2 = inner.find("div")
            if inner2:
                description = inner2.get_text(strip=True)[:2000]
    if not description:
        desc_el = soup.select_one(
            "#bookDescription_feature_div .a-expander-content"
        )
        if desc_el:
            description = desc_el.get_text(strip=True)[:2000]

    # Cover image — prefer high-res from script JSON (CWA pattern).
    cover_url = None
    for script in soup.find_all("script"):
        text = script.string or ""
        m = _HIRES_RE.search(text)
        if m:
            cover_url = m.group(1)
            break
    # Fallback: img element with dynamic-image class.
    if not cover_url:
        img = soup.select_one("img.a-dynamic-image")
        if img:
            cover_url = img.get("src") or ""
    # Fallback: legacy element IDs.
    if not cover_url:
        for sel in ("#imgBlkFront", "#ebooksImgBlkFront", "#landingImage"):
            img = soup.select_one(sel)
            if img:
                cover_url = img.get("src") or ""
                if cover_url:
                    cover_url = re.sub(r"\._[A-Z][A-Z0-9_]+_\.", ".", cover_url)
                    break

    return MetaRecord(
        title=title,
        authors=[],
        series=series_name,
        series_index=series_index,
        description=description_to_plain_text(description),
        isbn=isbn,
        pub_date=pub_date,
        page_count=pages,
        language=language,
        cover_url=cover_url,
        source="amazon",
        source_url=f"https://www.amazon.com/dp/{asin}",
        external_id=asin,
    )


def _merge_cache_into_record(
    record: MetaRecord, row: dict,
) -> MetaRecord:
    """Fill record fields the detail-page parse missed using the
    cache row.

    Detail-page parses are authoritative when present (the live HTML
    is the freshest source), so this helper ONLY fills empty/None
    slots. Cover URL, series name + index, language, and ISBN are
    the typical fields a cache row carries that the detail parse
    sometimes misses.
    """
    if not record.cover_url and row.get("cover_url"):
        record.cover_url = row["cover_url"]
    if not record.series and row.get("series_name"):
        record.series = row["series_name"]
    if record.series_index is None and row.get("series_pos") is not None:
        try:
            record.series_index = float(row["series_pos"])
        except (TypeError, ValueError):
            pass
    if not record.language and row.get("language"):
        record.language = row["language"]
    if not record.isbn and row.get("isbn"):
        record.isbn = row["isbn"]
    return record


def _parse_amazon_date(text: str) -> Optional[str]:
    if not text:
        return None
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y", "%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
