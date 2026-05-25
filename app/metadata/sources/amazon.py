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

    async def search_book(
        self, title: str, author: str, **_,
    ) -> Optional[MetaRecord]:
        if not title:
            return None

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

    async def close(self) -> None:
        self._session = None
        await super().close()


def _extract_asin(url: str) -> Optional[str]:
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    return m.group(1) if m else None


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
