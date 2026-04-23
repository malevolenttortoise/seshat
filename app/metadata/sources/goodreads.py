"""
Goodreads metadata source.

Book-centric scraper. Two-pass flow:

  1. `/search?q={title author}&search_type=books` — get candidate book
     IDs + list-page titles + author names. Iterate in rank order,
     pick the first candidate whose title + author credibly match
     the search request. Matching here is a loose similarity check
     — the enricher will assign the real confidence score after we
     return.
  2. `/book/show/{book_id}` — visit the chosen book's detail page and
     parse JSON-LD (`datePublished`, `inLanguage`, `image`,
     `numberOfPages`, `publisher`) plus HTML fallbacks for series
     information and description.

No API key needed — Goodreads deprecated their public API in 2020
and the only option left is scraping HTML. A handful of CSS selectors
break every few years; when they do, the failing test case in
`tests/metadata/sources/test_goodreads.py` tells us exactly which
selector needs updating.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from app.metadata.record import MetaRecord
from app.metadata.scoring import score_match
from app.metadata.sources.base import MetaSource

_log = logging.getLogger("seshat.metadata.goodreads")

_BASE = "https://www.goodreads.com"

# Goodreads will serve the bot page if the User-Agent looks like a
# headless client, so we claim a normal Firefox UA.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
        "Gecko/20100101 Firefox/128.0"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# Search results include a per-row author link we can use to filter
# false positives before paying for a detail-page fetch.
_SEARCH_MIN_CONFIDENCE = 0.35


class GoodreadsSource(MetaSource):
    name = "goodreads"
    default_headers = _DEFAULT_HEADERS
    default_timeout = 45.0

    async def search_book(
        self, title: str, author: str
    ) -> Optional[MetaRecord]:
        if not title:
            return None
        query = f"{title} {author}".strip()

        try:
            resp = await self._get(
                f"{_BASE}/search",
                params={"q": query, "search_type": "books"},
            )
        except Exception:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        candidates = _parse_search_results(soup)
        if not candidates:
            return None

        # Filter + rank candidates by title/author similarity. The
        # enricher will compute the final confidence on the full
        # MetaRecord we return, but we do a cheap pre-filter here
        # so we don't spend a detail-page GET on obvious mismatches.
        best = None
        best_score = 0.0
        for cand in candidates:
            score = score_match(
                record_title=cand["title"],
                record_authors=[cand["author"]] if cand["author"] else [],
                search_title=title,
                search_authors=author,
            )
            if score > best_score:
                best = cand
                best_score = score

        if best is None or best_score < _SEARCH_MIN_CONFIDENCE:
            return None

        book_id = best["book_id"]
        record = MetaRecord(
            title=best["title"],
            authors=[best["author"]] if best["author"] else [],
            cover_url=best.get("cover_url"),
            source=self.name,
            external_id=book_id,
            source_url=f"{_BASE}/book/show/{book_id}",
        )

        try:
            detail_resp = await self._get(f"{_BASE}/book/show/{book_id}")
            _merge_detail_page(record, detail_resp.text)
        except Exception:
            _log.debug("goodreads: detail page fetch failed for id=%s", book_id)

        return record


# ─── HTML parsers (module-level so tests can exercise them directly) ──


def _parse_search_results(soup: BeautifulSoup) -> list[dict]:
    """Extract {book_id, title, author, cover_url} from a search page.

    The search results use the `tr[itemtype='http://schema.org/Book']`
    microdata rows. Each row has:
      - a `a.bookTitle` anchor with the book URL
      - a `a.authorName` anchor (possibly multiple for co-authors;
        we keep the first only — the record's full author list comes
        from the detail page)
      - a cover `img.bookCover` or `img[alt^='Cover']`
    """
    rows = soup.select("tr[itemtype='http://schema.org/Book']")
    out: list[dict] = []
    for row in rows:
        title_link = row.select_one("a.bookTitle")
        if not title_link:
            continue
        href = title_link.get("href", "")
        m = re.search(r"/book/show/(\d+)", href)
        if not m:
            continue
        title_el = title_link.select_one("span") or title_link
        title_text = _squeeze(title_el.get_text(" ", strip=True))
        # Goodreads search rows append "(Series Name, #N)" to the
        # title string. Leaving it in would pollute the Jaccard
        # similarity score and make "Book Prime" (a draft) outrank
        # "Book (Series, #1)" (the real book) — the tokens from the
        # series suffix dilute the intersection. Strip before scoring.
        title_text = _strip_parentheticals(title_text)

        author_el = row.select_one("a.authorName span") or row.select_one("a.authorName")
        author_text = _squeeze(author_el.get_text(" ", strip=True)) if author_el else ""

        img_el = row.select_one("img.bookCover, img[src*='books.google'], img[alt]")
        cover_url = img_el.get("src") if img_el else None
        if cover_url and "nophoto" in cover_url:
            cover_url = None
        elif cover_url:
            cover_url = _upgrade_cover_url(cover_url)

        out.append(
            {
                "book_id": m.group(1),
                "title": title_text,
                "author": author_text,
                "cover_url": cover_url,
            }
        )
    return out


def _merge_detail_page(record: MetaRecord, html: str) -> None:
    """Extract rich fields from a `/book/show/{id}` HTML page.

    Mutates `record` in place. Nothing here raises — missing fields
    are just left as-is on the record. JSON-LD is preferred over
    HTML scraping where both are available because it's stable
    across Goodreads redesigns.
    """
    soup = BeautifulSoup(html, "lxml")

    # Capture the caller-supplied description (if any) before any
    # selector writes to `record`. Longest-wins below needs this as
    # one of the candidates so a pre-populated field can survive
    # if it's genuinely longer than anything on this page.
    initial_desc = record.description
    jsonld_desc: Optional[str] = None

    # JSON-LD structured data block.
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue

        if data.get("name") and not record.title:
            record.title = str(data["name"])

        date = data.get("datePublished")
        if date and not record.pub_date:
            record.pub_date = str(date)[:10]

        pages = data.get("numberOfPages")
        if pages and not record.page_count:
            try:
                record.page_count = int(pages)
            except (ValueError, TypeError):
                pass

        lang = data.get("inLanguage")
        if lang and not record.language:
            record.language = str(lang)

        image = data.get("image")
        if image and not record.cover_url:
            record.cover_url = str(image)

        isbn = data.get("isbn")
        if isbn and not record.isbn:
            record.isbn = str(isbn).replace("-", "")

        # JSON-LD `description` is frequently the full back-of-book
        # text for Goodreads-mastered entries. When present it's
        # often LONGER than the og:description meta tag's teaser.
        # Collect here rather than writing directly to
        # `record.description` so the post-loop longest-wins step
        # can compare across all candidates.
        desc_ld = data.get("description")
        if desc_ld and not jsonld_desc:
            jsonld_desc = _squeeze(str(desc_ld))

        # Goodreads' author list in JSON-LD is a list of Person objects
        # with `name` fields. Fall back to HTML if missing.
        authors_ld = data.get("author")
        if authors_ld and len(record.authors) <= 1:
            names: list[str] = []
            if isinstance(authors_ld, list):
                for a in authors_ld:
                    if isinstance(a, dict) and a.get("name"):
                        names.append(str(a["name"]))
            elif isinstance(authors_ld, dict) and authors_ld.get("name"):
                names.append(str(authors_ld["name"]))
            if names:
                record.authors = names

    # Publication date fallback: Goodreads doesn't always put
    # `datePublished` in JSON-LD, but the HTML publication paragraph
    # ("First published August 31, 2010") is reliable.
    if not record.pub_date:
        pub_el = soup.find("p", {"data-testid": "publicationInfo"})
        if pub_el:
            parsed = _parse_pub_date_text(pub_el.get_text(" ", strip=True))
            if parsed:
                record.pub_date = parsed

    # Description: collect every candidate on the page, pick longest.
    # Goodreads serves the same description through multiple channels
    # depending on page age / A/B variant:
    #   - JSON-LD `description` (captured into `jsonld_desc` above)
    #   - Schema.org microdata `[itemprop="description"]`
    #   - React-era placeholder `[data-testid="description"]`
    #   - `meta[property="og:description"]` teaser
    # These can differ in length — og:description in particular is
    # often a truncated ~250-char teaser while JSON-LD or the
    # microdata span carries the full back-of-book text. Longest
    # wins so we never lock in the shortest variant. Mirrors the
    # longest-wins merge policy at the enricher level.
    desc_candidates: list[str] = []
    if initial_desc:
        desc_candidates.append(initial_desc)
    if jsonld_desc:
        desc_candidates.append(jsonld_desc)
    for selector in (
        '[itemprop="description"]',
        '[data-testid="description"]',
    ):
        for el in soup.select(selector):
            t = _squeeze(el.get_text(" ", strip=True))
            if t:
                desc_candidates.append(t)
    og_desc = soup.select_one("meta[property='og:description']")
    if og_desc and og_desc.get("content"):
        desc_candidates.append(_squeeze(og_desc["content"]))
    if desc_candidates:
        record.description = max(desc_candidates, key=len)

    # Series info: Goodreads uses `h3.Text__title3` with an anchor for
    # the series name. Shape: "Stormlight Archive #1".
    if not record.series:
        series_el = soup.select_one("h3.Text__title3 a") or soup.select_one(
            "a[href*='/series/']"
        )
        if series_el:
            series_text = _squeeze(series_el.get_text(" ", strip=True))
            name, index = _parse_series_string(series_text)
            if name:
                record.series = name
            if index is not None:
                record.series_index = index

    # Publisher: legacy `details` block.
    if not record.publisher:
        pub_el = soup.find(string=re.compile(r"^Publisher", re.I))
        if pub_el and pub_el.parent:
            next_el = pub_el.parent.find_next_sibling()
            if next_el:
                record.publisher = _squeeze(next_el.get_text(" ", strip=True))


# ─── Tiny text helpers ─────────────────────────────────────────────


_WS_RX = re.compile(r"\s+")
_PAREN_RX = re.compile(r"\s*\([^)]*\)\s*$")


def _squeeze(text: str) -> str:
    return _WS_RX.sub(" ", text or "").strip()


def _strip_parentheticals(text: str) -> str:
    """Drop a trailing `(...)` suffix from a title string.

    Goodreads wraps the series info in parens at the end:
    "The Way of Kings (The Stormlight Archive, #1)". We want the
    base title for similarity scoring; the series info is captured
    separately from the detail page.
    """
    if not text:
        return text
    return _PAREN_RX.sub("", text).strip()


# "First published January 1, 2020" → "2020-01-01"
# "Published August 31, 2010" → "2010-08-31"
# "Expected publication April 4, 2026" → "2026-04-04"
import datetime as _dt  # noqa: E402 — module-scoped by choice

_DATE_FORMATS = (
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%B %Y",
    "%b %Y",
    "%Y",
)

_PUB_TEXT_RX = re.compile(
    r"(?:first\s+published|published|expected\s+publication)\s+(.+)$",
    re.IGNORECASE,
)


def _parse_pub_date_text(text: str) -> Optional[str]:
    """Parse a Goodreads publicationInfo paragraph into ISO yyyy-mm-dd."""
    if not text:
        return None
    m = _PUB_TEXT_RX.search(text.strip())
    if not m:
        return None
    remainder = re.sub(r"(\d+)(?:st|nd|rd|th)", r"\1", m.group(1).strip())
    for fmt in _DATE_FORMATS:
        try:
            parsed = _dt.datetime.strptime(remainder, fmt)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _upgrade_cover_url(url: str) -> str:
    """Upgrade a thumbnail URL to the full-size cover.

    Goodreads serves size-suffixed images like `..._SY75_.jpg`
    (75px tall) in search and list pages. Stripping the suffix
    returns the original-size image — much better for the review
    queue UI. Safe to call on URLs that don't have the suffix:
    the regex only matches the exact pattern.
    """
    if not url:
        return url
    return re.sub(r"\._S[XY]\d+_\.", ".", url)


def _parse_series_string(text: str) -> tuple[Optional[str], Optional[float]]:
    """Split 'Stormlight Archive #1' into ('Stormlight Archive', 1.0)."""
    if not text:
        return None, None
    m = re.match(r"(.*?)\s*#\s*(\d+(?:\.\d+)?)", text)
    if m:
        try:
            return m.group(1).strip(), float(m.group(2))
        except ValueError:
            return m.group(1).strip(), None
    return text.strip(), None
