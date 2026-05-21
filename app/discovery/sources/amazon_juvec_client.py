"""
Amazon /juvec POST client — session-replay for the Author-Store AJAX
endpoint (v2.11.0 Stage 5++).

After the initial GET of ``/stores/author/{id}/allbooks``, Amazon's
client-side JS hydrates remaining ASINs (and re-runs the query under
new filters) by POSTing to ``https://www.amazon.com/juvec``. We
mirror that behavior server-side, reusing the CSRF tokens + pageContext
UUIDs that came back from the initial GET.

Two request shapes — see `amazon_widget_parser` module docstring for
the full surface analysis:

  - **Filter-application** (`fetch_filtered_page`): body carries
    ``authorSearch`` (with `page`, `pageSize`, `sort`) + ``authorFilters``
    (format / language). Server returns a fresh filtered ASINList +
    populated products for that page.

  - **Detail-fetch** (`fetch_asin_batch`): body carries ``ASINList``
    (≤16 ASINs the client has already received in a prior page).
    Server returns just the populated products for those ASINs.

Both requests sit behind Akamai Bot Manager so the session must use
curl_cffi Chrome-120 impersonation. We inject the session externally
so tests can use a plain mock.

The two captured cURL bodies (in
``tests/fixtures/amazon/``) drive the body-construction tests in
``tests/discovery/sources/test_amazon_juvec_client.py``.

Author IP / customer ID:
    Mark's captures include ``customerId`` + ``customerIP`` tied to
    his logged-in session. Server-side anonymous scans don't have
    these. We send empty strings — Amazon's /juvec accepts them
    (validated via the probe script that ships alongside this
    module). If a future Amazon rev starts requiring real values,
    surface as a graceful skip + log rather than a crash.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

from app.discovery.sources.amazon_widget_parser import (
    AllBooksPageData,
    JuvecResponse,
    ParseError,
    parse_juvec_response,
)

logger = logging.getLogger("seshat.discovery.amazon.juvec")


_JUVEC_URL = "https://www.amazon.com/juvec"

# Maximum ASINs the client side packs into one detail-fetch batch
# (observed in Mark's captures). The server may accept more, but
# sticking to the observed cap avoids surprising Akamai.
_MAX_BATCH_SIZE = 16

# Page size used by Amazon's client (observed in every captured
# authorSearch block). We don't deviate.
_PAGE_SIZE = 112

# Sort token Amazon's UI defaults to.
_DEFAULT_SORT = "author-sidecar-rank"


class JuvecError(Exception):
    """A /juvec POST failed in a way the caller should treat as a
    scan-blocker for this author. Includes a brief reason; callers
    log + return empty results.

    v2.19.0 — carries optional structured fields so AmazonSource can
    distinguish "legitimate scan miss" from "Akamai jailed our IP"
    without string-matching the message. Both fields are None when
    the failure is something other than an HTTP non-200 (e.g. parse
    error)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_s: float | None = None,
        thin_body_bytes: int | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_s = retry_after_s
        self.thin_body_bytes = thin_body_bytes

    @property
    def is_soft_block_signal(self) -> bool:
        """True when the failure looks like an Akamai soft-block
        (HTTP 429 OR a 200-with-thin-body CAPTCHA interstitial)."""
        if self.status_code == 429:
            return True
        if self.thin_body_bytes is not None:
            return True
        return False


class JuvecClient:
    """Per-author /juvec session-replay client.

    Build one of these from the AllBooksPageData returned by the
    initial /stores/author/{id}/allbooks GET. The instance carries
    all the CSRF + pageContext state needed to make subsequent
    POSTs look like the same session.
    """

    def __init__(
        self,
        page_data: AllBooksPageData,
        session: Any,
        *,
        timeout: float = 30.0,
        max_retries: int = 1,
        burst_delay_s: float = 0.8,
    ):
        """
        Args:
            page_data: parsed result of `parse_allbooks_html`.
            session: curl_cffi-style async session
                (`.post(url, json=..., timeout=...)`).
            timeout: per-request timeout in seconds.
            max_retries: extra attempts on transient 5xx (one retry
                = two total attempts).
            burst_delay_s: minimum delay between successive POSTs
                within one author scan. Defaults to the
                amazon_author_store_juvec_burst_delay setting's
                ship-default. Caller may override.
        """
        self.page_data = page_data
        self.session = session
        self.timeout = timeout
        self.max_retries = max_retries
        self.burst_delay_s = burst_delay_s
        self._last_post_at: float | None = None

    # ─── Public methods ──────────────────────────────────────────

    async def fetch_filtered_page(
        self,
        page: int,
        format_filter: str = "kindle",
        language: str = "English",
    ) -> JuvecResponse:
        """POST /juvec for a filter-application request.

        Server returns a fresh filtered ASINList + populated products
        for ``page`` (1-indexed, pageSize=112). When totalResultCount
        from the response exceeds (page * 112), the caller should
        fetch the next page.

        Raises JuvecError on network failure / non-200 / malformed
        response that the parser can't make sense of.
        """
        body = self._build_filter_body(page, format_filter, language)
        return await self._post_and_parse(body, label=f"filter page {page}")

    async def fetch_asin_batch(
        self,
        asins: list[str],
        format_filter: str = "kindle",
        language: str = "English",
    ) -> JuvecResponse:
        """POST /juvec for a detail-fetch request.

        ``asins`` is the list of ASINs to populate; chunks larger
        than _MAX_BATCH_SIZE raise — caller's job to batch upstream.

        Returns populated products for those ASINs only.
        """
        if not asins:
            return JuvecResponse(
                products=(), asin_list=(), total_result_count=None,
            )
        if len(asins) > _MAX_BATCH_SIZE:
            raise ValueError(
                f"fetch_asin_batch called with {len(asins)} ASINs; "
                f"max batch size is {_MAX_BATCH_SIZE}"
            )
        body = self._build_detail_body(list(asins), format_filter, language)
        return await self._post_and_parse(
            body, label=f"detail batch n={len(asins)}",
        )

    # ─── Body construction ───────────────────────────────────────

    def _build_request_context(self) -> dict[str, Any]:
        """Common requestContext block emitted by every POST.

        Carries CSRF tokens + marketplace identifiers + the visit ID.
        Customer ID / IP are intentionally empty for anonymous server-
        side scans (validated by probe; Amazon's /juvec accepts an
        anonymous shape)."""
        marketplace = self.page_data.obfuscated_marketplace_id or "ATVPDKIKX0DER"
        return {
            "obfuscatedMarketplaceId": marketplace,
            "obfuscatedMerchantId": marketplace,
            "language": "en-US",
            "sessionId": "",          # session cookie carries the real id
            "customerId": "",         # anonymous
            "customerIP": "",         # anonymous
            "currency": "USD",
            "almThresholdsMap": {},
            "queryParameterMap": {},
            "weblabMap": {},
            "appendedParameters": {
                "ingress": "0",
                "visitId": self.page_data.visit_id,
            },
            "isPreviewCampaign": False,
            "deviceType": "desktop",
            "deviceMode": "Desktop",
            "appVersion": "",
            "osName": "",
            "ubId": "",
            "pageSubType": "Author",
            "previewWidgetGroup": None,
            "slateToken": self.page_data.slate_token,
            "freshCartCsrfToken": self.page_data.fresh_cart_csrf_token,
            "painterContentId": "",
            "internal": False,
            "profile": False,
            "debug": False,
            "mshop": False,
            "previewCampaignId": None,
            "inBlacklist": False,
            "amazonApiAjaxEndpoint": "data.amazon.com",
            "amazonApiCsrfToken": self.page_data.amazon_api_csrf_token,
        }

    def _build_page_context(self) -> dict[str, Any]:
        """Common pageContext block. Echoes the IDs/UUIDs we
        extracted from the initial allbooks GET."""
        author_id = self.page_data.author_id
        return {
            "template": "Marquee",
            "storeType": "AUTHOR",
            "brandName": "",
            "rootPagePath": f"/author/{author_id}",
            "isSearchEnabled": True,
            "authorId": author_id,
            "storeId": self.page_data.store_id,
            "rootPageId": self.page_data.root_page_id,
            "pagePath": f"/author/{author_id}/allbooks",
            "title": "books, biography, latest update",
            "afid": author_id,
            "version": self.page_data.version,
            "pageDescription": "",
            "theme": "author",
            "brandLogo": {
                "imageWidth": 1080, "imageHeight": 1080,
                "image": "", "shape": "circle", "imageOffsetTop": 0,
                "imageUrl": "", "hideBrandLogo": False,
            },
            "needVariationSupport": True,
        }

    def _build_filter_body(
        self, page: int, format_filter: str, language: str,
    ) -> dict[str, Any]:
        """Shape A — filter-application / pagination."""
        return {
            "requestContext": self._build_request_context(),
            "pageContext": self._build_page_context(),
            "widgetType": "ProductGrid",
            "sectionType": "AuthorAllBooksProductGrid",
            "productGridType": "ma",
            "authorSearch": {
                "includeOutOfStock": True,
                "pageSize": _PAGE_SIZE,
                "sort": _DEFAULT_SORT,
                "page": page,
                "keywords": "",
                "isSpellCorrectionEnabled": True,
            },
            "authorFilters": {
                "format": [format_filter],
                "language": [language],
            },
            "isManualGrid": True,
            "content": {"includeOutOfStock": True},
            "includeOutOfStock": True,
            "endpoint": "ajax-data",
        }

    def _build_detail_body(
        self, asins: list[str], format_filter: str, language: str,
    ) -> dict[str, Any]:
        """Shape B — detail-fetch for a specific ASIN batch."""
        return {
            "requestContext": self._build_request_context(),
            "pageContext": self._build_page_context(),
            "widgetType": "ProductGrid",
            "sectionType": "AuthorAllBooksProductGrid",
            "productGridType": "ma",
            "authorFilters": {
                "format": [format_filter],
                "language": [language],
            },
            "isManualGrid": True,
            "content": {"includeOutOfStock": True},
            "includeOutOfStock": True,
            "endpoint": "ajax-data",
            "ASINList": asins,
        }

    # ─── Transport ───────────────────────────────────────────────

    async def _post_and_parse(
        self, body: dict[str, Any], *, label: str,
    ) -> JuvecResponse:
        """POST one body with retries + burst-delay throttle, parse
        the response. Raises JuvecError on terminal failure."""
        await self._throttle()

        attempt = 0
        last_exc: Exception | None = None
        while attempt <= self.max_retries:
            attempt += 1
            try:
                resp = await self.session.post(
                    _JUVEC_URL,
                    json=body,
                    timeout=self.timeout,
                )
            except Exception as exc:
                last_exc = exc
                logger.debug(
                    "juvec %s attempt %d: transport error %s",
                    label, attempt, exc,
                )
                if attempt <= self.max_retries:
                    await asyncio.sleep(min(2.0 * attempt, 5.0))
                    continue
                raise JuvecError(
                    f"juvec {label}: transport error after {attempt} "
                    f"attempts: {exc}"
                ) from exc

            status = getattr(resp, "status_code", None)
            text = getattr(resp, "text", None)

            if status != 200:
                if 500 <= (status or 0) < 600 and attempt <= self.max_retries:
                    logger.debug(
                        "juvec %s attempt %d: HTTP %d, retrying",
                        label, attempt, status,
                    )
                    await asyncio.sleep(min(2.0 * attempt, 5.0))
                    continue
                # v2.19.0 — parse Retry-After when present so the
                # caller's penalty-box honors Amazon's stated cooldown.
                retry_after_s = None
                if status == 429:
                    from app.discovery.amazon_author_id_resolver import (
                        parse_retry_after,
                    )
                    headers = getattr(resp, "headers", None)
                    if headers is not None:
                        try:
                            raw_ra = headers.get("Retry-After") or headers.get("retry-after")
                        except Exception:
                            raw_ra = None
                        retry_after_s = parse_retry_after(raw_ra)
                raise JuvecError(
                    f"juvec {label}: HTTP {status} (body {len(text or '')} bytes)",
                    status_code=status,
                    retry_after_s=retry_after_s,
                )

            if not text:
                raise JuvecError(
                    f"juvec {label}: HTTP 200 with empty body — "
                    f"likely Akamai soft-block",
                    status_code=status,
                    thin_body_bytes=0,
                )

            # Akamai thin-body guard. Real /juvec responses are 50KB+
            # (Mark's captures: 53-135 KB transferred). Thin bodies
            # at 200 OK are the bot-block signature.
            if len(text) < 1_000:
                raise JuvecError(
                    f"juvec {label}: HTTP 200 with thin body ({len(text)} "
                    f"bytes) — likely Akamai soft-block",
                    status_code=status,
                    thin_body_bytes=len(text),
                )

            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise JuvecError(
                    f"juvec {label}: response body not JSON: {exc}"
                ) from exc

            try:
                return parse_juvec_response(payload)
            except ParseError as exc:
                raise JuvecError(f"juvec {label}: parse error: {exc}") from exc

        # Loop exited without success — should be unreachable given
        # the explicit raises above, but guard for safety.
        raise JuvecError(
            f"juvec {label}: exhausted retries (last={last_exc})"
        )

    async def _throttle(self) -> None:
        """Sleep at least burst_delay_s between successive POSTs to
        avoid pattern-matching Amazon's bot detector. Adds a small
        random jitter to widen the timing distribution."""
        now = asyncio.get_event_loop().time()
        if self._last_post_at is not None:
            elapsed = now - self._last_post_at
            jitter = random.uniform(0, self.burst_delay_s * 0.5)
            wait = (self.burst_delay_s + jitter) - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_post_at = asyncio.get_event_loop().time()
