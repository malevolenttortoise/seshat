"""
Audible discovery source — finds an author's audiobooks via Audible's
public catalog endpoint, hydrated through Audnexus for full metadata.

Wraps the same `api.audible{tld}/1.0/catalog/products` + `api.audnex.us`
call chain used by the pipeline-side `AudibleSource` (at
`app/metadata/sources/audible.py`), but fits the `BaseSource` contract
expected by `app/discovery/lookup.py`: `search_author(name)` returns
an `AuthorResult` with every audiobook the author has released.

Why a separate source file from `app/metadata/sources/audible.py`?
Two different orchestrators, two different result shapes. The
pipeline asks "which book is THIS file?" — one answer. Discovery
asks "what has this author ever released?" — a list.

Used only for libraries whose `content_type == "audiobook"`. Ebook
libraries skip this source entirely (see `_sources_for_content_type`
in lookup.py).
"""
from __future__ import annotations

import logging
from typing import Optional

from app.discovery.sources.base import BaseSource, AuthorResult, BookResult, Contributor

logger = logging.getLogger("seshat.discovery.audible")

# Mirror the pipeline-side region / TLD map. Kept as a local dict so
# changes on one side don't silently regress the other. If we add a
# new region (Brazil etc.), update both places.
REGION_TLDS = {
    "us": ".com", "ca": ".ca", "uk": ".co.uk", "au": ".com.au",
    "fr": ".fr", "de": ".de", "jp": ".co.jp", "it": ".it",
    "in": ".in", "es": ".es",
}

# Per-author catalog cap. Audible's catalog endpoint supports up to
# 50 per page, and most authors have well under 200 audiobooks. We
# page until `total_results` is exhausted or `_MAX_AUTHOR_BOOKS` is
# hit — whichever comes first.
_PAGE_SIZE = 50
_MAX_AUTHOR_BOOKS = 400


class AudibleDiscoverySource(BaseSource):
    """Discovery-side Audible source — implements `search_author()`."""

    name = "audible"
    default_timeout = 30.0

    def __init__(self, *, region: str = "us", rate_limit: float = 0.5):
        super().__init__(rate_limit=rate_limit)
        from app.metadata.sources.audnexus import VALID_REGIONS
        self.region = region if region in VALID_REGIONS else "us"

    # ── URL helpers ───────────────────────────────────────────

    def _catalog_url(self) -> str:
        tld = REGION_TLDS.get(self.region, ".com")
        return f"https://api.audible{tld}/1.0/catalog/products"

    # ── Main entry point ──────────────────────────────────────

    async def search_author(self, author_name: str) -> Optional[AuthorResult]:
        """Return every audiobook Audible lists under `author_name`.

        Flow:
          1. Paginate Audible catalog with `author={name}` until we've
             either pulled every hit or hit the cap.
          2. Hydrate each ASIN via Audnexus `/books/{asin}` to get
             narrator / series / duration / cover / summary.

        Returns None when no hits come back (new-author case) — mirrors
        every other discovery source's convention. Errors propagate as
        None too; `lookup._try_source` logs them and moves on.
        """
        if not author_name:
            return None

        asins = await self._fetch_catalog_asins(author_name)
        if not asins:
            logger.info(
                "audible: no catalog hits for author %r (region=%s)",
                author_name, self.region,
            )
            return None

        # Hydrate through Audnexus. Imports here (not top-level) to
        # avoid circular risk between discovery ↔ metadata packages.
        from app.metadata.sources.audnexus import AudnexusSource
        audnexus = AudnexusSource(
            region=self.region, rate_limit=self.rate_limit,
        )
        books: list[BookResult] = []
        try:
            for asin in asins:
                try:
                    record = await audnexus.fetch_by_asin(asin)
                except Exception as e:
                    logger.debug("audible: audnexus fetch failed for %s: %s", asin, e)
                    record = None
                if record is None:
                    continue
                books.append(_record_to_book_result(record))
        finally:
            await audnexus.close()

        if not books:
            return None

        return AuthorResult(
            name=author_name,
            external_id=None,  # Audible doesn't expose author-level IDs here
            books=books,
            series=[],  # deduced from book entries at merge time, not upfront
        )

    async def _fetch_catalog_asins(self, author_name: str) -> list[str]:
        """Page the Audible catalog endpoint and return the ASIN list.

        Audible's `/catalog/products` is 0-indexed for pagination.
        Sending `page=1` with `num_results=50` when total_results is
        46 returns an empty list (it's asking for entries 51-100) and
        we'd falsely conclude the author has zero audiobooks.
        """
        asins: list[str] = []
        url = self._catalog_url()
        page = 0
        while True:
            params = {
                "author": author_name,
                "num_results": str(_PAGE_SIZE),
                "page": str(page),
                "products_sort_by": "Relevance",
            }
            try:
                resp = await self._get(url, params=params)
            except Exception as e:
                logger.debug(
                    "audible: catalog page %d failed for %r: %s",
                    page, author_name, e,
                )
                break
            data = resp.json() or {}
            products = data.get("products") or []
            if not products:
                break
            for p in products:
                asin = p.get("asin")
                if asin and asin not in asins:
                    asins.append(asin)
                if len(asins) >= _MAX_AUTHOR_BOOKS:
                    return asins
            total = int(data.get("total_results") or 0)
            if len(asins) >= total:
                break
            page += 1
            # Rate limit respected by `_get()`; no extra sleep here.
        return asins


def _record_to_book_result(rec) -> BookResult:
    """Project a `MetaRecord` (Audnexus-hydrated) into a `BookResult`.

    Notes on field mapping:
      - `external_id` carries the ASIN so the merge layer can match
        across multiple source scans without re-hitting Audible for
        already-known books.
      - `language` is capitalized by `_item_to_record` already; leave
        it as-is.
      - `description`/`cover_url` ride through unchanged; the merge
        layer decides whether Audible's cover should beat Calibre's.
    """
    series_name = rec.series
    series_index = rec.series_index
    # v3.0.0 Phase 3.5 — Audnexus keeps authors and narrators in
    # separate fields (`rec.authors` vs `rec.narrator`), so `rec.authors`
    # is already a clean author list with no role ambiguity. Every entry
    # is the plain author role (role=None). Audible is trusted-create.
    # (The per-author Audible ASIN is dropped upstream in
    # `_item_to_record`, so it's not captured here — a deferred
    # capture-now opportunity for the author-ID enrichment arc.)
    contributors = [Contributor(name=n) for n in (rec.authors or []) if n]
    return BookResult(
        title=rec.title or "",
        series_name=series_name,
        series_index=series_index,
        isbn=rec.isbn,
        cover_url=rec.cover_url,
        pub_date=rec.pub_date,
        expected_date=None,
        is_unreleased=False,
        description=rec.description,
        page_count=None,
        external_id=rec.asin or rec.external_id,
        language=rec.language,
        source="audible",
        source_url=rec.source_url,
        contributors=contributors,
    )
