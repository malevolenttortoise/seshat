"""
Kobo metadata source — web scraping.

Kobo has no public API, so we scrape the storefront pages. Cloudflare
sits in front of kobo.com; requests go through `app.metadata.kobo_session`
(curl_cffi with Chrome's TLS profile, shared with the discovery source).
A challenge is never solved (ADR-0025); Kobo backs off instead.

Two-pass flow for single-book lookup:
  1. Search page: `kobo.com/us/en/search?query={title+author}&fcmedia=Book`
     Pick the best-matching result by title and author (each result
     card lists its authors), never another volume or edition of the
     title (G188).
  2. Detail page: rich metadata from clean static HTML — title with
     its subtitle, authors, series name + index, ISBN-13, publication
     date, language, page count, publisher, description, high-res
     cover (353x569).

Until G188 (2026-10-20) the record carried no authors, so the
enricher's score (title 70%, author 30%) topped out at 0.70 under its
0.80 bar: Kobo never contributed a field. Most of its near-misses were
other books ("My Dungeon Life … Volume 16" for volume 1; Claire Broad's
"Heaven Sent" for Jax A River's "Heaven Sent an Inquisitor…").

Selectors handle both old and new Kobo layouts via dual-selector
fallback (data-testid for new, class-based for old).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from lxml import html

from app.metadata import kobo_session
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


def _card_authors(title_link) -> list[str]:
    """The authors a search result's card lists beside its title link
    (`dd[data-testid=authors]`); [] on the old layout."""
    node = title_link
    for _ in range(6):
        node = node.getparent()
        if node is None:
            return []
        # Past the card (another book's title link): never borrow its authors.
        if len({a.get("href") for a in node.xpath(".//a[@data-testid='title']")}) > 1:
            return []
        dd = node.xpath(".//*[@data-testid='authors']")
        if dd:
            names = [a.text_content().strip() for a in dd[0].xpath(".//a")]
            if not names:
                names = [n.strip() for n in dd[0].text_content().split(",")]
            return [n for n in dict.fromkeys(names) if n]
    return []


class KoboSource(MetaSource):
    """Kobo fetches through `kobo_session` (curl_cffi) instead of httpx."""

    name = "kobo"
    default_timeout = 30.0

    def __init__(self, *, rate_limit: float = 3.0):
        super().__init__(rate_limit=rate_limit)

    def backing_off(self) -> bool:
        return kobo_session.backoff_until() > 0.0

    async def _fetch(self, url: str) -> Optional[str]:
        # In Kobo's turn (the source gate's Metadata Sources rate).
        return await kobo_session.fetch(url, timeout=self.default_timeout)

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

        # Score each result by title and author and pick the best,
        # leaving out another volume or edition of the title (G188).
        from app.metadata.scoring import score_match
        from app.works.title_identity import TitleRef, different_edition_or_volume
        best_url = None
        best_score = 0.0
        best_title = ""
        best_authors: list[str] = []
        for link in result_links[:10]:
            link_title = link.text_content().strip()
            href = link.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = _BASE + href
            if different_edition_or_volume(TitleRef(title), TitleRef(link_title)):
                continue
            card_authors = _card_authors(link)

            sc = score_match(
                record_title=link_title,
                record_authors=card_authors,
                search_title=title,
                search_authors=author,
            )
            if sc > best_score:
                best_url = href
                best_score = sc
                best_title = link_title
                best_authors = card_authors

        if not best_url or best_score < 0.25:
            return None

        # Pass 2: fetch the detail page for rich metadata.
        details = await self._get_book_details(best_url)
        if not details:
            return None

        return MetaRecord(
            title=details.get("title") or best_title,
            authors=details.get("authors") or best_authors,
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
            # "Heaven Sent" + "Soul Lessons from the Afterlife": the
            # subtitle tells two same-titled books apart.
            subtitle_el = page.xpath(
                "//span[contains(@class,'subtitle') and contains(@class,'product-field')]/text()"
            )
            subtitle = subtitle_el[0].strip() if subtitle_el else ""
            # Kobo also puts the series there ("My Dungeon Life, #16").
            if subtitle and not re.search(r"#\s*\d+(?:\.\d+)?\s*$", subtitle):
                details["title"] = f"{details['title']}: {subtitle}"

        authors = [
            a.strip() for a in page.xpath(
                "//span[contains(@class,'authors') and contains(@class,'product-field')]"
                "//a[contains(@class,'contributor-name')]/text()"
            ) if a.strip()
        ]
        if authors:
            details["authors"] = list(dict.fromkeys(authors))

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
        # The curl_cffi session is shared (`kobo_session`).
        await super().close()
