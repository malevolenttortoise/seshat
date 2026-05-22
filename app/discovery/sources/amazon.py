"""
Amazon Author-Store discovery source (v2.11.0 Stage 5++).

Replaces the legacy sequential search-and-detail-fetch strategy with
a single Author-Store HTML fetch + batched POST /juvec pagination.
The new flow:

  1. Resolve the author's Amazon Author Store ID via
     `amazon_author_id_resolver` (caches on `authors.amazon_id`).
  2. GET `/stores/author/{author_id}/allbooks` with curl_cffi Chrome
     120 TLS impersonation.
  3. Parse the embedded widget JSON (`amazon_widget_parser`) — yields
     85ish populated products with full mediaMatrix cross-references.
  4. POST `/juvec` to filter to the configured format + language.
  5. POST `/juvec` detail-fetch batches (≤16 ASINs each) until every
     filtered ASIN has populated data.
  6. POST `/juvec` next-page until `totalResultCount` is exhausted
     (or the safety cap is hit).
  7. Convert products into `BookResult` rows, grouping by
     `bookSeriesInfo.seriesTitle` into `SeriesResult`. Carry the
     mediaMatrix format-variant ASIN map on each result so
     downstream code can pick alternate-format versions without
     re-scanning.

Per-book enricher use (which lives in `app/metadata/sources/amazon.py`)
is unaffected by this file — it operates on already-known ASINs via
isolated detail-page fetches.

Akamai bypass: curl_cffi Chrome 120 (shipped Stage 5+). Sustained
density is the bot-detection trigger; this flow makes ~7 requests
per author scan (1 GET + 1 filter POST + ~3 detail-fetch POSTs +
~2 next-page POSTs) instead of the legacy 45 detail GETs.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any, Optional

from app.discovery.amazon_author_id_resolver import (
    _AMAZON_SOFT_BLOCK_THIN_BODY_BYTES,
    amazon_block_remaining_s,
    is_amazon_blocked,
    record_amazon_soft_block,
    resolve_amazon_author_id,
    parse_retry_after,
)
from app.discovery.sources.base import (
    AuthorResult,
    BaseSource,
    BookResult,
    SeriesResult,
)
from app.discovery.sources.amazon_juvec_client import (
    JuvecClient,
    JuvecError,
)
from app.discovery.sources.amazon_widget_parser import (
    AllBooksPageData,
    FILTER_TO_BINDING,
    ParseError,
    Product,
    SoftBlockSuspectedError,
    parse_allbooks_html,
)


logger = logging.getLogger("seshat.discovery.amazon")


_ALLBOOKS_URL_TEMPLATE = "https://www.amazon.com/stores/author/{author_id}/allbooks"
_AMAZON_BASE = "https://www.amazon.com"

# Safety caps so a misbehaving response can't trigger a runaway scan.
_MAX_PAGES = 6              # 6 × 112 = 672 product slots > Sanderson's 645 max
_MAX_TOTAL_PRODUCTS = 800   # hard cap; truncate beyond this
_MAX_BATCH_SIZE = 16        # /juvec detail-fetch batch limit (Amazon's own cap)

# An Amazon Author Store ID is 10 chars, uppercase alphanumeric.
# Heuristic guard used to distinguish "this caller already has the
# resolved ID" from "this caller passed an author name and needs
# resolution" — the latter happens when authors.amazon_id was
# populated by the legacy AmazonSource (which stored the name).
_AUTHOR_ID_RE = __import__("re").compile(r"^[A-Z0-9]{10}$")


def _create_impersonating_session():
    """Build a curl_cffi AsyncSession with Chrome 120 TLS impersonation.

    Akamai Bot Manager scores requests against TLS handshake fingerprint
    (JA3). Python's stdlib TLS fingerprint is on every bot-detection
    blocklist; curl_cffi drives libcurl-impersonate to replicate
    Chrome's handshake exactly.

    Returns None on ImportError — callers degrade gracefully (the
    scan returns empty rather than crashing).
    """
    try:
        from curl_cffi.requests import AsyncSession
        return AsyncSession(impersonate="chrome120", timeout=30.0)
    except ImportError:
        logger.warning(
            "amazon: curl_cffi not installed — author-store scans will "
            "be Akamai-blocked. Install via `pip install curl_cffi`."
        )
        return None


def _is_amazon_author_id(value: str) -> bool:
    """Heuristic: 10 chars, uppercase alphanumeric. Used to decide
    whether to call the resolver."""
    return bool(value) and bool(_AUTHOR_ID_RE.match(value))


class AmazonSource(BaseSource):
    """Author-Store discovery source. See module docstring."""

    name = "amazon"

    def __init__(
        self,
        rate_limit: float = 30.0,
        *,
        format_filter: str = "kindle",
        audiobook_format_filter: str = "audible_audiobook",
        language: str = "English",
        burst_delay_s: float = 0.8,
        use_ddg_fallback: bool = False,
    ):
        """
        Args:
            rate_limit: Inter-author sleep, in seconds. The
                Author-Store flow makes ~7 requests per author; this
                governs how quickly we move between authors. 30s is
                the conservative ship-default; can be lowered after
                live UAT validates the lower request count is safe.
            format_filter: One of the EBOOK_FILTERS keys (kindle,
                paperback, hardcover, mass_market). Used during
                ebook-content-type scans. Drives Amazon's server-side
                `authorFilters.format` on /juvec.
            audiobook_format_filter: One of the AUDIOBOOK_FILTERS
                keys (audible_audiobook, audio_cd, mp3_cd,
                preloaded_digital_audio). Used during audiobook-
                content-type scans. v2.11.1 default
                `audible_audiobook` matches the Audible-distributed
                digital audiobook format Amazon sells (dominant).
            language: Capitalized language name from Amazon's
                `content.languageFilter` list (e.g. "English").
            burst_delay_s: Inter-/juvec-POST sleep within one author
                scan. Default 0.8s.
            use_ddg_fallback: v2.19.0 — when True, the resolver falls
                back to a DuckDuckGo site-restricted search after the
                amazon.com tiers fail. Opt-in via the
                `amazon_use_ddg_fallback` source-config setting.
                Off by default.
        """
        super().__init__(rate_limit=rate_limit)
        self.format_filter = format_filter
        self.audiobook_format_filter = audiobook_format_filter
        self.language = language
        self.burst_delay_s = burst_delay_s
        self.use_ddg_fallback = use_ddg_fallback
        self._session: Any | None = None
        self._session_init_attempted = False
        # Set externally by lookup.py before each scan; tells the
        # source whether to use `format_filter` (ebook) or
        # `audiobook_format_filter` (audiobook). Default "ebook" so
        # standalone use of the source / tests don't need to set it.
        self._content_type: str = "ebook"

    def _active_format_filter(self) -> str:
        """Pick the right format filter for the current scan based
        on `_content_type` (set by lookup.py before the call)."""
        if self._content_type == "audiobook":
            return self.audiobook_format_filter
        return self.format_filter

    # ─── Session management ──────────────────────────────────────

    def _get_session(self):
        """Lazy curl_cffi session. None if curl_cffi isn't installed."""
        if self._session is not None:
            return self._session
        if self._session_init_attempted:
            return None
        self._session_init_attempted = True
        self._session = _create_impersonating_session()
        return self._session

    async def close(self):
        """Close the curl_cffi session if open."""
        if self._session is not None and hasattr(self._session, "close"):
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        # Also close any httpx client the base class may have built
        await super().close()

    # ─── Public discovery API ────────────────────────────────────

    async def search_author(
        self, author_name: str,
    ) -> Optional[AuthorResult]:
        """Resolve the author's Amazon Author Store ID and return a
        minimal AuthorResult.

        `external_id` is the 10-char Author Store ID. lookup.py
        persists this to `authors.amazon_id` via its dynamic
        `UPDATE authors SET {source}_id` pattern, so subsequent
        scans can short-circuit the resolver.
        """
        # v2.19.0 — penalty box: if amazon.com is in soft-block
        # cooldown, every downstream HTTP call would just walk into
        # the same wall. Bail early; the cascade stops here.
        if is_amazon_blocked():
            logger.info(
                "amazon: search_author %r SKIPPED — soft-block cooldown "
                "(%.0fs remaining)",
                author_name, amazon_block_remaining_s(),
            )
            return None

        session = self._get_session()
        if session is None:
            return None

        author_id = await resolve_amazon_author_id(
            author_name, session=session,
            use_ddg_fallback=self.use_ddg_fallback,
        )
        if not author_id:
            logger.info(
                "amazon: search_author %r → no Author Store ID resolved",
                author_name,
            )
            return None

        return AuthorResult(
            name=author_name,
            external_id=author_id,
            books=[],
            series=[],
        )

    async def get_author_books(
        self,
        author_id: str,
        existing_titles: set | None = None,
        owned_titles: list | None = None,
        owned_only: bool = False,
    ) -> Optional[AuthorResult]:
        """Run the Author-Store scan workflow.

        Args:
            author_id: Either the 10-char Amazon Author Store ID
                (preferred — comes from `authors.amazon_id` after
                Stage 5++) OR a fallback author-name string (for
                upgraded installs where the legacy AmazonSource left
                a name in that column).
            existing_titles: Set of titles Seshat already has rows
                for. We don't currently use this for filtering (the
                merge layer dedupes downstream) but accept it for
                signature compatibility with other sources.
            owned_titles: List of titles the user owns. Same note.
            owned_only: When True, only return owned-title rows. We
                ignore this here — Amazon discovery is "new-book
                surfacing", not owned-only enumeration.

        Returns AuthorResult on success, None when:
          - curl_cffi isn't installed
          - Author Store ID can't be resolved (for name fallback)
          - The allbooks GET fails / soft-blocked
          - All /juvec POSTs fail

        Empty-but-non-None AuthorResult is a valid scan result
        (Amazon may genuinely have no Kindle+English books for a
        niche author).
        """
        # existing_titles / owned_titles / owned_only kept for signature
        # parity with other sources (the lookup.py dispatcher passes
        # them to all). Amazon's flow doesn't need them — the merge
        # layer dedupes downstream.
        del existing_titles, owned_titles, owned_only

        # v2.19.0 — penalty box: bail early if amazon.com is in
        # soft-block cooldown.
        if is_amazon_blocked():
            logger.info(
                "amazon: get_author_books %r SKIPPED — soft-block cooldown "
                "(%.0fs remaining)",
                author_id, amazon_block_remaining_s(),
            )
            return None

        session = self._get_session()
        if session is None:
            return None

        # Resolve to a real Author Store ID if needed.
        if _is_amazon_author_id(author_id):
            amazon_author_id = author_id
            author_name = author_id  # placeholder; products carry the real name
        else:
            # Legacy state — value is a name string. Resolve.
            author_name = author_id
            amazon_author_id = await resolve_amazon_author_id(
                author_name, session=session,
                use_ddg_fallback=self.use_ddg_fallback,
            )
            if not amazon_author_id:
                logger.info(
                    "amazon: get_author_books %r → no Author Store ID resolved",
                    author_name,
                )
                return None

        # Stage 1: GET allbooks
        try:
            page_data = await self._fetch_allbooks(amazon_author_id, session)
        except _AllBooksFetchError as exc:
            logger.warning(
                "amazon: allbooks fetch failed for %s (%s): %s",
                amazon_author_id, author_name, exc,
            )
            return None

        # Surface the author's real name if products carry it.
        if page_data.products:
            contributors = page_data.products[0].contributors
            if contributors:
                author_name = contributors[0]

        # Stage 2: collect products via /juvec
        client = JuvecClient(
            page_data, session, burst_delay_s=self.burst_delay_s,
        )
        try:
            products = await self._collect_products(client, page_data)
        except JuvecError as exc:
            logger.warning(
                "amazon: /juvec collection failed for %s: %s",
                amazon_author_id, exc,
            )
            # v2.19.0 — if the failure was a soft-block signal (429 or
            # thin-body), record the IP-level penalty box so the next
            # author scan short-circuits instead of cascading.
            if exc.is_soft_block_signal:
                record_amazon_soft_block(
                    f"/juvec for {amazon_author_id}: {exc}",
                    retry_after_s=exc.retry_after_s,
                )
            # Fall back to whatever the SSR populated
            products = list(page_data.products)

        if not products:
            return AuthorResult(
                name=author_name,
                external_id=amazon_author_id,
                books=[],
                series=[],
            )

        # Stage 3: filter to the configured format (defensive — the
        # server filter should have done this, but mediaMatrix
        # cross-format alternates may sneak in on edge cases).
        # v2.11.1: `_active_format_filter()` picks ebook vs audiobook
        # filter based on `self._content_type` set by lookup.py.
        # v2.21.0 schema-v2: when the caller passes `allFormats` (the
        # metadata cache worker does this so a single Amazon round-
        # trip covers every library this author lives in), the Stage
        # 3 filter has to be a passthrough — `FILTER_TO_BINDING` has
        # no `allFormats` entry, so the comparison would otherwise
        # zero out every product. Downstream consumers receive books
        # with their actual `binding_symbol` and partition per-
        # library themselves.
        active_filter = self._active_format_filter()
        if active_filter == "allFormats":
            filtered = list(products)
        else:
            target_binding = FILTER_TO_BINDING.get(active_filter, active_filter)
            filtered = [
                p for p in products if p.binding_symbol == target_binding
            ]
            if len(filtered) < len(products):
                logger.debug(
                    "amazon: filtered %d products to %d matching binding=%r",
                    len(products), len(filtered), target_binding,
                )

        # Stage 4: convert to BookResults grouped by series
        return self._build_author_result(
            author_name, amazon_author_id, filtered,
        )

    # ─── Internal: workflow stages ──────────────────────────────

    async def _fetch_allbooks(
        self, author_id: str, session: Any,
    ) -> AllBooksPageData:
        url = _ALLBOOKS_URL_TEMPLATE.format(author_id=author_id)
        try:
            resp = await session.get(url, timeout=30.0)
        except Exception as exc:
            raise _AllBooksFetchError(f"transport error: {exc}") from exc

        status = getattr(resp, "status_code", None)
        body = getattr(resp, "text", None) or ""
        if status != 200:
            # v2.19.0 — explicit 429 trips the IP-level penalty box.
            # v2.20.3 — HTTP 202 is Akamai's sensor-challenge response;
            # treat it the same as 429 (penalty box) so the cascade
            # short-circuits subsequent authors instead of grinding
            # through a pile of identical 202s.
            # Other non-200s stay as one-off failures (the caller logs +
            # returns empty for this author).
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
                    f"allbooks GET {url} returned HTTP {status} with "
                    f"{len(body)}-byte body"
                    + (" (Akamai sensor challenge)" if status == 202 else ""),
                    retry_after_s=parse_retry_after(raw_ra),
                )
            raise _AllBooksFetchError(
                f"HTTP {status} (body {len(body)} bytes)"
            )
        if len(body) < _AMAZON_SOFT_BLOCK_THIN_BODY_BYTES:
            # v2.19.0 — thin body at 200 OK is the CAPTCHA-interstitial
            # signature; record IP-level block so the next scan doesn't
            # walk into the same wall.
            record_amazon_soft_block(
                f"allbooks GET {url} returned 200 with {len(body)}-byte body"
            )
            raise _AllBooksFetchError(
                f"thin body ({len(body)} bytes) — likely Akamai soft-block"
            )
        try:
            return parse_allbooks_html(body)
        except SoftBlockSuspectedError as exc:
            # v2.20.2 — 200 OK + non-thin body + no ProductGrid marker
            # almost always means Amazon served a CAPTCHA / soft-block
            # page that snuck past the size-based thin-body check at
            # line 420. The 50KB threshold catches the small bare-page
            # variant; this catches the full-chrome variant. Trip the
            # IP-level cooldown so subsequent scans short-circuit
            # instead of cascading into the same wall.
            record_amazon_soft_block(
                f"allbooks GET {url} returned 200 with "
                f"{len(body)}-byte body but no ProductGrid marker "
                f"(suspected CAPTCHA / soft-block)"
            )
            raise _AllBooksFetchError(
                f"soft-block suspected: {exc}"
            ) from exc
        except ParseError as exc:
            raise _AllBooksFetchError(f"parse error: {exc}") from exc

    async def _collect_products(
        self,
        client: JuvecClient,
        page_data: AllBooksPageData,
    ) -> list[Product]:
        """Drive the JuvecClient through filter-application +
        detail-fetch + pagination until we've collected every product
        for the configured filter (or hit a safety cap).

        v2.11.1: `active_filter` resolved once via
        `_active_format_filter()` so ebook vs audiobook scans use the
        right `format_filter` setting throughout the workflow.
        """
        active_filter = self._active_format_filter()
        # v2.19.0 — SSR-bypass shortcut.
        #
        # The /stores/author/{id}/allbooks SSR page already carries the
        # complete catalog when `page_data.total_result_count <=
        # len(page_data.products)`: every ASIN the author has under the
        # default-filter view is populated inline, and Stage 3 below will
        # Python-filter by binding_symbol to surface only the configured
        # format. Skipping /juvec entirely in that case cuts a typical
        # scan from 3 requests (allbooks + filter POST + detail batch)
        # down to 1, which is the biggest single reduction in
        # amazon.com request density we can ship.
        #
        # The previous default_filter branch (allFormats / All Languages)
        # already short-circuited /juvec for the rare configuration
        # where Seshat asked for the SSR's exact default view. The
        # broader condition below covers the same case plus every other
        # author whose total catalog fits in the SSR page's product
        # array — that's the vast majority of non-prolific authors.
        if (
            page_data.total_result_count > 0
            and len(page_data.products) >= page_data.total_result_count
        ):
            logger.debug(
                "amazon: SSR-bypass — %d SSR products >= "
                "total_result_count=%d; skipping /juvec entirely",
                len(page_data.products), page_data.total_result_count,
            )
            return list(page_data.products)

        # Legacy default-filter shortcut — kept for the explicit
        # allFormats / All Languages configuration where the SSR page's
        # native default-view matches what we want to scan.
        default_filter = (
            active_filter == "allFormats"
            and self.language == "All Languages"
        )
        if default_filter:
            collected: list[Product] = list(page_data.products)
            asin_list = list(page_data.asin_list)
            total_count = page_data.total_result_count
        else:
            first = await client.fetch_filtered_page(
                page=1,
                format_filter=active_filter,
                language=self.language,
            )
            collected = list(first.products)
            asin_list = list(first.asin_list)
            total_count = first.total_result_count or 0
            logger.debug(
                "amazon: filter-application returned %d products / %d asins / "
                "totalResultCount=%d (filter=%r)",
                len(collected), len(asin_list), total_count, active_filter,
            )

        # Detail-fetch any ASINs in the list that aren't already
        # populated.
        populated_asins = {p.asin for p in collected}
        unpopulated = [a for a in asin_list if a not in populated_asins]
        for batch_start in range(0, len(unpopulated), _MAX_BATCH_SIZE):
            batch = unpopulated[batch_start:batch_start + _MAX_BATCH_SIZE]
            if not batch:
                break
            resp = await client.fetch_asin_batch(
                batch,
                format_filter=active_filter,
                language=self.language,
            )
            collected.extend(resp.products)
            if len(collected) >= _MAX_TOTAL_PRODUCTS:
                logger.warning(
                    "amazon: hit max_total_products=%d cap during detail-fetch",
                    _MAX_TOTAL_PRODUCTS,
                )
                return collected[:_MAX_TOTAL_PRODUCTS]

        # Pagination: keep fetching pages until totalResultCount is
        # exhausted or we hit the max-pages cap.
        page = 2
        while (
            total_count > 0
            and len(collected) < total_count
            and page <= _MAX_PAGES
            and len(collected) < _MAX_TOTAL_PRODUCTS
        ):
            resp = await client.fetch_filtered_page(
                page=page,
                format_filter=active_filter,
                language=self.language,
            )
            if not resp.products and not resp.asin_list:
                logger.debug("amazon: page %d empty; pagination ends", page)
                break
            collected.extend(resp.products)
            # Detail-fetch this page's unpopulated tail too.
            seen = {p.asin for p in collected}
            tail_unpop = [a for a in resp.asin_list if a not in seen]
            for batch_start in range(0, len(tail_unpop), _MAX_BATCH_SIZE):
                batch = tail_unpop[batch_start:batch_start + _MAX_BATCH_SIZE]
                if not batch:
                    break
                detail = await client.fetch_asin_batch(
                    batch,
                    format_filter=active_filter,
                    language=self.language,
                )
                collected.extend(detail.products)
                if len(collected) >= _MAX_TOTAL_PRODUCTS:
                    break
            page += 1

        return collected[:_MAX_TOTAL_PRODUCTS]

    # ─── Internal: result construction ──────────────────────────

    def _build_author_result(
        self,
        author_name: str,
        amazon_author_id: str,
        products: list[Product],
    ) -> AuthorResult:
        """Convert a flat product list into AuthorResult with books
        grouped by series. Fires _on_book / _on_new_candidate
        callbacks for each book if the caller set them."""
        on_book = getattr(self, "_on_book", None)
        on_new_candidate = getattr(self, "_on_new_candidate", None)

        # Group by series_title. None / empty → standalone.
        series_map: dict[str, SeriesResult] = {}
        standalone: list[BookResult] = []
        for p in products:
            book = self._product_to_book(p)
            # lookup.py's `_on_book(title: str)` writes to
            # `state._lookup_progress["current_book"]`, which is
            # serialized into the live-scan SSE feed. Passing the
            # BookResult instead of a string caused React error #31
            # in v2.11.0 Stage 5++ UAT (frontend tried to render the
            # dataclass as a child node). Pass the title only.
            if on_book is not None:
                try:
                    on_book(book.title)
                except Exception as exc:  # callback bug shouldn't kill scan
                    logger.debug("amazon: _on_book callback raised: %s", exc)
            # `_on_new_candidate()` is a parameterless tick counter
            # — see `_on_new_candidate` def in app/discovery/lookup.py.
            if on_new_candidate is not None:
                try:
                    on_new_candidate()
                except Exception as exc:
                    logger.debug(
                        "amazon: _on_new_candidate callback raised: %s", exc,
                    )

            if p.series_title:
                series = series_map.get(p.series_title)
                if series is None:
                    series = SeriesResult(
                        name=p.series_title,
                        total_books=p.series_total,
                        books=[],
                    )
                    series_map[p.series_title] = series
                series.books.append(book)
            else:
                standalone.append(book)

        return AuthorResult(
            name=author_name,
            external_id=amazon_author_id,
            books=standalone,
            series=list(series_map.values()),
        )

    def _product_to_book(self, p: Product) -> BookResult:
        """Convert one parsed Product into a BookResult.

        We store the canonical ASIN in `external_id` and rely on the
        merge layer's `f"{source}_id"` UPDATE pattern (lookup.py:759)
        to land it in `books.amazon_id`. The mediaMatrix variants
        are JSON-serialized into `amazon_format_asins` so the merge
        layer can persist them to the column added in v2.11.0 Stage
        5++ commit 1.
        """
        source_url = (
            urllib.parse.urljoin(_AMAZON_BASE, p.detail_page_link)
            if p.detail_page_link else None
        )
        # JSON-encode the mediaMatrix variants as `{binding_symbol: asin}`.
        # Stable key order via sorted() so a re-scan that returns the
        # same variants generates an identical JSON string (lets the
        # merge layer skip the UPDATE when nothing actually changed).
        format_asins: dict[str, str] = {}
        for v in p.media_matrix:
            if v.binding_symbol and v.asin:
                format_asins[v.binding_symbol] = v.asin
        amazon_format_asins = (
            json.dumps(format_asins, sort_keys=True)
            if format_asins else None
        )
        return BookResult(
            title=p.title,
            series_name=p.series_title,
            series_index=(
                float(p.series_position)
                if p.series_position is not None else None
            ),
            cover_url=p.cover_url,
            external_id=p.asin,
            source="amazon",
            source_url=source_url,
            amazon_format_asins=amazon_format_asins,
            # v2.21.0 schema-v2: the metadata cache stores this so the
            # cache reader can partition mixed-binding scan results by
            # content_type at read time. `binding_symbol` is the
            # canonical Amazon shape (`kindle_edition`,
            # `audio_download`, `paperback`, `hardcover`, …).
            format=p.binding_symbol or None,
        )


# ─── Internal exception ──────────────────────────────────────────


class _AllBooksFetchError(Exception):
    """The initial allbooks GET failed in a recoverable way (logged,
    return None from the public scan method)."""
