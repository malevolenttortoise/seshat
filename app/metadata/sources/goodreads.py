"""
Goodreads metadata source.

Book-centric, two-pass:

  1. **Resolve a `goodreads_book_id`** via the ethical resolver chain
     in `app.metadata.goodreads_id_resolver`. Five tiers in priority:

       T1  /book/auto_complete?q={isbn or asin}        (existing)
       T2  Hardcover book_mappings                      (existing)
       T3  OpenLibrary identifiers.goodreads            (existing)
       T4  /book/auto_complete?q={title} + author_id    (v2.13.2)
       T5  /author/list/{author_id} bibliography walk   (v2.13.2)

  2. **Fetch `/book/show/{book_id}`** through `GoodreadsSession`
     (curl_cffi chrome120 impersonation, Goodreads' turn in the source
     gate, and the book-page backoff after an AWS WAF block) and parse
     the HTML via `_merge_detail_page()` for the rich fields.

`/search` is **never** hit — robots.txt disallows it for `*`
user-agents. We hold a higher standard than the Calibre kiwidude
plugin (which scrapes `/search` with a rotated browser UA).

When the resolver chain returns no `goodreads_book_id` (no ISBN/ASIN
matched any of T1-T3, AND we have no stored author goodreads_id to
anchor T4/T5), `search_book` cleanly returns None and the enricher
dispatcher moves on to the next source.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from app.metadata.record import MetaRecord
from app.metadata.sources.base import MetaSource
from app.metadata.text_clean import description_to_plain_text

_log = logging.getLogger("seshat.metadata.goodreads")

_BASE = "https://www.goodreads.com"

# Legacy header constant — preserved as the class default for any
# direct httpx fallback path. The hot path now goes through
# `GoodreadsSession` which manages its own headers.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
        "Gecko/20100101 Firefox/128.0"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


# Known boilerplate strings Goodreads sometimes serves as og:description
# when a book page is a stub (e.g. unreleased title, metadata not yet
# populated). These pass any length-based filter but carry no book
# information — caught on Spirit Blade (tid=1243620, pre-release) where
# the 47-char site tagline beat MAM's missing-because-unfetched
# description. Extend this set when new placeholder strings appear.
_GOODREADS_BOILERPLATE = frozenset({
    "Discover and share books you love on Goodreads.",
})


class GoodreadsSource(MetaSource):
    name = "goodreads"
    default_headers = _DEFAULT_HEADERS
    default_timeout = 45.0

    async def search_book(
        self,
        title: str,
        author: str,
        *,
        isbn: str = "",
        asin: str = "",
        author_goodreads_id: str = "",
        **_,
    ) -> Optional[MetaRecord]:
        """Resolve via the ID-resolver chain, then fetch /book/show.

        See module docstring for the tier order. Returns None when:
          - title is empty (defensive)
          - the resolver returned no goodreads_book_id (insufficient
            identifiers — no ISBN/ASIN matched T1-T3 and no author
            goodreads_id was supplied to anchor T4/T5)
          - the resolver was blocked by Goodreads' bot protection (the
            session has already backed that request kind off)
          - the /book/show/{id} fetch failed
        """
        if not title:
            return None

        from app.metadata.goodreads_id_resolver import (
            ResolveQuery, resolve_goodreads_id,
        )

        query = ResolveQuery(
            title=title,
            author=author,
            isbn=isbn or "",
            asin=asin or "",
            author_goodreads_id=author_goodreads_id or "",
        )
        result = await resolve_goodreads_id(query)

        if result.soft_blocked:
            _log.info(
                "goodreads: resolver hit a soft-block while looking up "
                "title=%r — returning None (session state already flipped)",
                title,
            )
            return None

        if not result.goodreads_book_id:
            _log.info(
                "goodreads: resolver miss for title=%r author=%r "
                "(isbn=%s asin=%s author_id=%s) — no tier produced a book_id",
                title, author, isbn or "-", asin or "-",
                author_goodreads_id or "-",
            )
            return None

        record = await _fetch_and_parse_book(
            result.goodreads_book_id, title=title, author=author,
        )
        if record is None:
            return None

        # Tag the record so downstream merge + scoring see a Goodreads
        # contribution. The enricher does the confidence scoring; we
        # don't self-score.
        record.source = self.name
        record.source_url = f"{_BASE}/book/show/{result.goodreads_book_id}"
        record.external_id = str(result.goodreads_book_id)
        _log.info(
            "goodreads: resolved title=%r → book_id=%s via tier=%s",
            title, result.goodreads_book_id, result.tier or "?",
        )
        return record


async def _fetch_and_parse_book(
    book_id: str, *, title: str, author: str,
) -> Optional[MetaRecord]:
    """Fetch `/book/show/{book_id}` and merge fields into a MetaRecord.

    Routes through `GoodreadsSession` so curl_cffi impersonation,
    rate-limit jitter, and soft-block detection are uniform across
    every Goodreads HTML fetch in the app.
    """
    from app.metadata import goodreads_session

    record = MetaRecord(title=title, authors=[author] if author else [])

    url = f"{_BASE}/book/show/{book_id}"
    session = await goodreads_session.get_session()
    try:
        resp = await session.get(url)
    except Exception as e:
        _log.debug("goodreads: /book/show fetch error for %s: %s", book_id, e)
        return None

    if goodreads_session.is_soft_block(resp):
        # `session.get()` has already backed book pages off; this path
        # is an informational log only.
        _log.info(
            "goodreads: /book/show/%s blocked by AWS WAF — book pages "
            "backing off",
            book_id,
        )
        return None

    status = getattr(resp, "status_code", None)
    if status != 200:
        _log.debug(
            "goodreads: /book/show/%s unexpected status %s", book_id, status,
        )
        return None

    body = getattr(resp, "text", None) or ""
    if not body:
        return None

    try:
        _merge_detail_page(record, body)
    except Exception:
        _log.exception("goodreads: failed to parse /book/show/%s", book_id)
        return None

    return record


# ─── HTML parser for /book/show/{id} (robots-permitted) ────────


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
        # JSON-LD's `description` field is sometimes raw HTML
        # (`<p>...<br>`) rather than plain text — normalize before
        # storing so the review queue never surfaces the tags.
        chosen = description_to_plain_text(
            max(desc_candidates, key=len)
        )
        # Reject known stub-page boilerplate before it pollutes the
        # merge. Leaving record.description as None lets other
        # sources (MAM, Hardcover, etc.) supply real content.
        if chosen and chosen not in _GOODREADS_BOILERPLATE:
            record.description = chosen

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


def _squeeze(text: str) -> str:
    return _WS_RX.sub(" ", text or "").strip()


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
