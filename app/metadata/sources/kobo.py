"""
Kobo metadata source — web scraping.

Kobo has no public API, so we scrape the storefront pages. Cloudflare
sits in front of kobo.com, so the HTTP layer uses `cloudscraper`
(synchronous) wrapped in `asyncio.to_thread`.

Two-pass flow for single-book lookup:
  1. Search page: `kobo.com/us/en/search?query={title+author}&fcmedia=Book`
     Pick the best-matching result by title/author similarity.
  2. Detail page: rich metadata from clean static HTML — series name
     + index, ISBN-13, publication date, language, page count,
     publisher, description, high-res cover (353x569).

Selectors handle both old and new Kobo layouts via dual-selector
fallback (data-testid for new, class-based for old).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Optional

from lxml import html

from app.metadata import source_gate
from app.metadata.record import MetaRecord
from app.metadata.sources.base import MetaSource
from app.metadata.text_clean import description_to_plain_text

_log = logging.getLogger("seshat.metadata.kobo")

_BASE = "https://www.kobo.com"


def _parse_kobo_date(text: str) -> Optional[str]:
    if not text:
        return None
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y", "%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _create_scraper():
    try:
        import cloudscraper
        return cloudscraper.create_scraper(
            browser={
                "custom": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) "
                          "Gecko/20100101 Firefox/132.0"
            },
        )
    except ImportError:
        _log.warning("cloudscraper not installed — Kobo source disabled")
        return None


class KoboSource(MetaSource):
    """Kobo uses cloudscraper (sync) instead of httpx.

    Overrides search_book to do sync HTTP wrapped in to_thread.
    """

    name = "kobo"
    default_timeout = 30.0

    def __init__(self, *, rate_limit: float = 3.0):
        super().__init__(rate_limit=rate_limit)
        self._session = None

    def _get_session(self):
        if self._session is None:
            self._session = _create_scraper()
        return self._session

    def _fetch_sync(self, url: str) -> tuple[Optional[int], Optional[str]]:
        """(status, body on a 200); (None, None) on a transport error."""
        session = self._get_session()
        if not session:
            return None, None
        try:
            r = session.get(url, timeout=self.default_timeout)
            if r.status_code == 200:
                return r.status_code, r.text
            return r.status_code, None
        except Exception as e:
            _log.debug("kobo fetch error: %s", e)
            return None, None

    async def _fetch(self, url: str) -> Optional[str]:
        # In Kobo's turn (the source gate's Metadata Sources rate).
        async with source_gate.turn(self.name) as turn:
            status, text = await asyncio.to_thread(self._fetch_sync, url)
            turn.status(status)
        return text

    async def search_book(
        self, title: str, author: str, **_,
    ) -> Optional[MetaRecord]:
        if not title:
            return None

        query = f"{title} {author}".strip().replace(" ", "%20")
        search_url = f"{_BASE}/us/en/search?query={query}&fcmedia=Book"

        page_html = await self._fetch(search_url)
        if not page_html:
            return None

        try:
            page = html.fromstring(page_html)
        except Exception:
            return None

        # Extract search result links — handle both old and new layouts.
        results_new = page.xpath("//a[@data-testid='title']")
        results_old = page.xpath("//h2[contains(@class,'title') and contains(@class,'product-field')]/a")
        result_links = results_new or results_old

        if not result_links:
            return None

        # Score each result by title similarity and pick the best.
        from app.metadata.scoring import score_match
        best_url = None
        best_score = 0.0
        best_title = ""
        for link in result_links[:10]:
            link_title = link.text_content().strip()
            href = link.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = _BASE + href

            sc = score_match(
                record_title=link_title,
                record_authors=[],  # search results don't carry author
                search_title=title,
                search_authors=author,
            )
            # Weight title-only since search results don't have author.
            if sc > best_score:
                best_url = href
                best_score = sc
                best_title = link_title

        if not best_url or best_score < 0.25:
            return None

        # Pass 2: fetch the detail page for rich metadata.
        details = await self._get_book_details(best_url)
        if not details:
            return None

        return MetaRecord(
            title=details.get("title") or best_title,
            authors=[],  # Kobo search doesn't reliably surface author
            series=details.get("series_name"),
            series_index=details.get("series_index"),
            description=description_to_plain_text(details.get("description")),
            isbn=details.get("isbn"),
            publisher=details.get("publisher"),
            pub_date=details.get("pub_date"),
            page_count=details.get("page_count"),
            language=details.get("language"),
            cover_url=details.get("cover_url"),
            source="kobo",
            source_url=best_url,
        )

    async def _get_book_details(self, kobo_url: str) -> Optional[dict]:
        """Fetch a Kobo detail page and extract structured metadata."""
        details: dict = {}
        page_html = await self._fetch(kobo_url)
        if not page_html:
            return None
        try:
            page = html.fromstring(page_html)
        except Exception:
            return None

        # Title.
        title_el = page.xpath(
            "//h1[contains(@class,'title') and contains(@class,'product-field')]/text()"
        )
        if title_el:
            details["title"] = title_el[0].strip()

        # High-res cover (353x569 vs thumbnail 80x120).
        cover_el = page.xpath("//img[contains(@class,'cover-image')]/@src")
        if cover_el:
            c = cover_el[0]
            details["cover_url"] = ("https:" + c) if c.startswith("//") else c

        # Series name.
        series_el = page.xpath(
            "//span[contains(@class,'series') and contains(@class,'product-field')]//a/text()"
        )
        if series_el:
            details["series_name"] = series_el[0].strip()

        # Series index: "Book N -" prefix.
        seq_el = page.xpath("//span[@class='sequenced-name-prefix']/text()")
        if seq_el:
            m = re.search(r"(\d+(?:\.\d+)?)", seq_el[0])
            if m:
                try:
                    details["series_index"] = float(m.group(1))
                except ValueError:
                    pass

        # Page count.
        pages_el = page.xpath(
            "//div[contains(@class,'book-stats')]"
            "//div[@class='column'][.//span[normalize-space()='Pages']]"
            "//strong/text()"
        )
        if pages_el:
            try:
                details["page_count"] = int(pages_el[0].strip())
            except ValueError:
                pass

        # eBook Details panel.
        detail_lis = page.xpath(
            "//div[contains(@class,'bookitem-secondary-metadata')]//li"
        )
        known_prefixes = (
            "Release Date:", "Book ID:", "Language:", "Imprint:",
            "Download options:", "File size:", "ISBN:",
        )
        for li in detail_lis:
            text = li.text_content().strip()
            if text.startswith("Release Date:"):
                details["pub_date"] = _parse_kobo_date(text.split(":", 1)[1])
            elif text.startswith(("Book ID:", "ISBN:")):
                isbn = text.split(":", 1)[1].strip()
                if re.fullmatch(r"\d{10}|\d{13}", isbn):
                    details["isbn"] = isbn
            elif text.startswith("Language:"):
                details["language"] = text.split(":", 1)[1].strip()
            elif not any(text.startswith(p) for p in known_prefixes):
                if "publisher" not in details:
                    details["publisher"] = text

        # Description: hidden div with full synopsis text.
        desc_el = page.xpath("//div[@data-full-synopsis]")
        if desc_el:
            desc_text = desc_el[0].text_content().strip()
            desc_text = re.sub(r"\s+", " ", desc_text)
            if desc_text:
                details["description"] = desc_text[:2000]

        return details

    async def close(self) -> None:
        self._session = None
        await super().close()
