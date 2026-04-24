"""
Amazon metadata source — web scraping.

Scrapes Amazon's Kindle Store search + product detail pages for author
catalogs. Primary value: series data — Amazon is the most reliable
source for determining whether a book is standalone or part of a series.

Two-pass flow per author:
  1. Search: amazon.com/s for books by the author name
  2. Detail: amazon.com/dp/{ASIN} for series info, metadata

Author-centric (get_author_books), using httpx.AsyncClient, with results
grouped into SeriesResult/BookResult for the merge pipeline and
existing_titles/owned_titles optimization via _on_book/_on_new_candidate
progress hooks.

Key anti-bot measures:
  - Plain httpx with realistic Firefox headers (no cloudscraper)
  - Accept-Encoding: gzip, deflate, br, zstd (critical)
  - Explicit search params (unfiltered, stripbooks, digital-text)
  - Conservative rate limiting (2.0s default)
  - No retries (Amazon blocks aggressive retry patterns)
  - Graceful degradation to None on any failure
"""
import asyncio
import logging
import re
from datetime import datetime
from typing import Optional

from app.discovery.sources.base import BaseSource, AuthorResult, SeriesResult, BookResult

logger = logging.getLogger("seshat.discovery.amazon")

_SEARCH_URL = "https://www.amazon.com/s"
_PRODUCT_URL = "https://www.amazon.com/dp"

# High-res cover extraction from Amazon's inline script JSON.
_HIRES_RE = re.compile(r'"hiRes"\s*:\s*"([^"]+)"')

# Amazon cover URL size/quality suffix stripper — removes dimension
# constraints like "._SY346_." or "._SL500_." for full-res images.
_COVER_SUFFIX_RE = re.compile(r"\._[A-Z][A-Z0-9_]+_\.")

# Series extraction from RPI card labels: "Book 1", "Book 3.5"
_BOOK_NUM_RE = re.compile(r"Book\s+(\d+(?:\.\d+)?)")

# Series extraction from bullet widget: "Book 3 of 10: Series Name"
_SERIES_BULLET_RE = re.compile(r"Book\s+(\d+)(?:\s+of\s+\d+)?:\s*(.+)")

# Strip series parenthetical from title: "Title (Series Name, #3)"
_SERIES_PAREN_RE_TMPL = r"\s*\({series}[^)]*\)\s*$"

# Author attribution on search result cards
_AUTHOR_LINK_RE = re.compile(r"by\s+", re.IGNORECASE)

# Maximum pages of Amazon search results to fetch per author
_MAX_SEARCH_PAGES = 2
# Maximum detail page fetches per author scan
_MAX_DETAIL_FETCHES = 25

# Junk marketplace listing detection. Amazon search results include
# third-party seller listings with garbled titles like:
#   "[(Kingdom's Hope )] [Author: Chuck Black] [May-2006]"
#   "By BLACK CHUCK - SIR KENDRICK..."
#   "By Chuck Black - Kingdom's Edge (2006-05-16) [Paperback]"
_RX_JUNK_TITLE = re.compile(
    r'^\[?\(|'                    # starts with [( or (
    r'^By\s+[A-Z].*\s+-\s+|'     # "By AUTHOR - Title" seller format
    r'\[\s*(?:Paperback|Hardcover|Mass Market|Library Binding)\s*\]|'  # format in brackets
    r'\)\s*(?:Paperback|Hardcover|Mass Market|Library Binding)\s*$|'   # "...) Paperback" suffix
    r'by\s+\w+,\s+\w+\s+\(\d{4}\)\s+(?:Paperback|Hardcover)',        # "by Last, First (2006) Paperback"
    re.IGNORECASE,
)

# Audiobook format indicators found in RPI cards or page text.
# Amazon audiobook pages use "Listening Length" instead of page count
# and show "Audible Audiobook" in the format area.
_AUDIO_FORMAT_KEYWORDS = {"audible", "audiobook", "audio cd", "listening length"}


class AmazonSource(BaseSource):
    """Amazon Kindle Store metadata source.

    Author-centric: searches Amazon for an author name, extracts
    book ASINs from search results, visits detail pages for series
    info and metadata. Groups results into series and standalone.
    """

    name = "amazon"
    default_headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) "
            "Gecko/20100101 Firefox/143.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
    }
    default_timeout = 15.0

    def __init__(self, rate_limit: float = 2.0):
        super().__init__(rate_limit=rate_limit)

    async def _fetch(self, url: str, params: dict = None) -> Optional[str]:
        """HTTP GET with rate limiting, no retries.

        Amazon blocks aggressive retry patterns, so we degrade
        gracefully: one shot, return None on failure.
        """
        await asyncio.sleep(self.rate_limit)
        try:
            resp = await self.client.get(url, params=params)
            if resp.status_code == 200:
                return resp.text
            logger.info(f"  amazon: HTTP {resp.status_code} for {url}")
            return None
        except Exception as e:
            logger.debug(f"  amazon: fetch error for {url}: {e}")
            return None

    async def search_author(self, author_name: str) -> Optional[AuthorResult]:
        """Search Amazon for an author. Returns minimal AuthorResult.

        The external_id is the author name itself (Amazon doesn't expose
        stable author IDs in search results). get_author_books() will
        re-search by this name.
        """
        html = await self._fetch(
            _SEARCH_URL,
            params={
                "field-keywords": author_name,
                "i": "digital-text",
                "search-alias": "stripbooks",
                "unfiltered": "1",
                "sort": "relevanceexprank",
            },
        )
        if not html:
            return None

        # Quick check: does the search page have any results?
        if _count_result_asins(html) == 0:
            logger.info(f"  amazon: no search results for '{author_name}'")
            return None

        return AuthorResult(
            name=author_name,
            external_id=author_name,
        )

    async def get_author_books(
        self,
        author_id: str,
        existing_titles: set = None,
        owned_titles: list = None,
        owned_only: bool = False,
    ) -> Optional[AuthorResult]:
        """Fetch an author's catalog from Amazon search results.

        Paginates through search results, extracts ASINs, visits
        detail pages for series info and metadata. Groups books
        into series and standalone.
        """
        author_name = author_id  # external_id is the author name
        existing_titles = existing_titles or set()
        owned_titles = owned_titles or []

        # Normalized owned titles for quick matching
        owned_norm = {_quick_norm(t) for t in owned_titles}

        # Pen-name aliases — callers (lookup.py's _try_source wiring)
        # set `_linked_author_names` on the source instance before the
        # scan. Accept books bylined under any linked name so scanning
        # Randi Darren finds books attributed to William D. Arand too.
        linked_names = getattr(self, "_linked_author_names", []) or []
        accept_authors = [author_name] + list(linked_names)

        on_book = getattr(self, "_on_book", None)
        on_new_candidate = getattr(self, "_on_new_candidate", None)

        # Phase 1: Collect ASINs from search pages
        all_asins = []  # (asin, result_title) tuples
        seen_asins = set()

        for page in range(1, _MAX_SEARCH_PAGES + 1):
            params = {
                "field-keywords": author_name,
                "i": "digital-text",
                "search-alias": "stripbooks",
                "unfiltered": "1",
                "sort": "relevanceexprank",
            }
            if page > 1:
                params["page"] = str(page)

            html = await self._fetch(_SEARCH_URL, params=params)
            if not html:
                break

            page_asins = _extract_search_results(
                html, author_name, accept_authors=accept_authors,
            )
            if not page_asins:
                break

            new_on_page = 0
            for asin, title in page_asins:
                if asin not in seen_asins:
                    seen_asins.add(asin)
                    all_asins.append((asin, title))
                    new_on_page += 1

            # If this page had no new ASINs, stop paginating
            if new_on_page == 0:
                break

        if not all_asins:
            return None

        logger.info(
            f"  [amazon] Found {len(all_asins)} unique ASINs across "
            f"{min(page, _MAX_SEARCH_PAGES)} search page(s)"
        )

        # Phase 2: Visit detail pages and extract metadata
        series_map: dict[str, SeriesResult] = {}  # series_name → SeriesResult
        standalone_books: list[BookResult] = []
        detail_fetches = 0

        for asin, search_title in all_asins:
            # Skip junk marketplace listings before any processing
            if search_title and _RX_JUNK_TITLE.search(search_title):
                logger.debug(f"    SKIP (junk title): '{search_title}'")
                continue

            norm_title = _quick_norm(search_title) if search_title else ""

            # URL-backfill optimization: if the book is already known,
            # emit a minimal result without visiting the detail page.
            if norm_title and norm_title in existing_titles:
                bk = BookResult(
                    title=search_title,
                    external_id=asin,
                    source="amazon",
                    source_url=f"https://www.amazon.com/dp/{asin}",
                )
                standalone_books.append(bk)
                if on_book:
                    on_book(search_title)
                continue

            # owned_only optimization: skip detail fetch if the book
            # isn't one the user owns.
            if owned_only and norm_title and norm_title not in owned_norm:
                continue

            # Rate-limit detail page fetches
            if detail_fetches >= _MAX_DETAIL_FETCHES:
                logger.debug(
                    f"  amazon: hit detail fetch cap ({_MAX_DETAIL_FETCHES})"
                )
                break

            if on_book:
                on_book(search_title or asin)

            detail_html = await self._fetch(f"{_PRODUCT_URL}/{asin}")
            detail_fetches += 1

            if not detail_html:
                continue

            book = _parse_detail_page(
                detail_html, asin, expected_authors=accept_authors,
            )
            if not book:
                continue

            # Group into series or standalone
            if book.series_name:
                series_key = book.series_name.lower().strip()
                if series_key not in series_map:
                    series_map[series_key] = SeriesResult(
                        name=book.series_name,
                    )
                series_map[series_key].books.append(book)
            else:
                standalone_books.append(book)

            if on_new_candidate:
                on_new_candidate()

        logger.info(
            f"  [amazon] Parsed {detail_fetches} detail pages: "
            f"{len(series_map)} series, {len(standalone_books)} standalone"
        )

        return AuthorResult(
            name=author_name,
            external_id=author_name,
            books=standalone_books,
            series=list(series_map.values()),
        )


# ─── Helpers ────────────────────────────────────────────────


def _quick_norm(title: str) -> str:
    """Fast normalization for title matching — lowercase + strip punctuation."""
    t = re.sub(r"[^\w\s]", "", title.lower()).strip()
    return re.sub(r"\s+", " ", t)


def _count_result_asins(html: str) -> int:
    """Quick count of data-asin attributes without full HTML parse."""
    return len(re.findall(r'data-asin="[A-Z0-9]{10}"', html))


def _extract_card_authors(card) -> list[str]:
    """Extract author names from a search-result card.

    Amazon's result cards carry attribution in a couple of shapes:
      * `<a>` inside a ".a-color-secondary" wrapper following the
        "by" label (most common)
      * plain text "by Author Name" in the card's lower row

    Returns every candidate author name we can find — the caller
    then uses `authors_match` to decide whether any of them refer
    to the searched-for author. Robust against Amazon's HTML
    variations and returns an empty list when attribution isn't
    rendered at all (e.g. compact grid layouts).
    """
    authors: list[str] = []
    # Anchor-style links in the card's secondary-text area
    for a in card.select("a.a-link-normal"):
        href = a.get("href", "")
        # Contributor links have a stable URL pattern. Ignore other
        # `a.a-link-normal` links (which also cover the title itself,
        # price, rating, etc.).
        if "/e/" in href or "field-author=" in href:
            text = a.get_text(strip=True)
            if text:
                authors.append(text)
    if authors:
        return authors

    # Plain-text fallback: look for "by X[, Y]" pattern in the card's
    # combined text. Splits on commas + "and" to capture co-authors.
    text = card.get_text(" ", strip=True)
    m = re.search(
        r"\bby\s+([^|•·\n]+?)(?:\s+\||\s+•|\s+·|\s+\(|\s+\d\.\d|$)",
        text, re.IGNORECASE,
    )
    if m:
        byline = m.group(1)
        for part in re.split(r",\s*|\s+and\s+", byline):
            part = part.strip()
            if part and len(part) > 1:
                authors.append(part)
    return authors


def _extract_detail_authors(soup) -> list[str]:
    """Extract author names from an Amazon product detail page.

    Looks at the #bylineInfo block — the "by <author>" line under the
    title on the product page. Each contributor is an anchor with
    role text ("(Author)", "(Foreword)", "(Translator)", etc.) next
    to it; we keep only names whose role is empty or marked Author.
    Translators / forewords / editors are deliberately excluded —
    otherwise a book like "Kingdom Revival: Forward by Randy Clark"
    would match when the query is "Randy <anything>".
    """
    byline = soup.select_one("#bylineInfo")
    if not byline:
        return []
    authors: list[str] = []
    # Each contributor is wrapped in a span; role text follows in a
    # sibling span with class "contribution" or similar.
    for span in byline.select("span.author, div.author, span.contributor"):
        name_el = span.select_one("a") or span
        name = name_el.get_text(strip=True)
        if not name:
            continue
        role_el = span.select_one(".contribution") or span.select_one(
            ".a-color-secondary"
        )
        role = (role_el.get_text(" ", strip=True).lower() if role_el else "")
        # Accept only primary Author role or no explicit role. Excludes
        # "(Foreword)", "(Translator)", "(Editor)", "(Illustrator)",
        # "(Narrator)", etc.
        if role and "author" not in role:
            continue
        authors.append(name)
    if authors:
        return authors

    # Fallback: plain byline text (some layouts skip the span markup).
    text = byline.get_text(" ", strip=True)
    m = re.match(r"(?i)by\s+(.+?)(?:\s+\(|$)", text)
    if m:
        for part in re.split(r",\s*|\s+and\s+", m.group(1)):
            part = part.strip()
            if part:
                authors.append(part)
    return authors


def _extract_search_results(
    html: str,
    author_name: str = "",
    accept_authors: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Extract (asin, title) tuples from an Amazon search results page.

    Uses BeautifulSoup to parse the search result cards. Each card
    has a data-asin attribute and a nested title span.

    Author validation (when `author_name` is set):
      * Extract contributor anchors from the card via
        `_extract_card_authors`.
      * Accept iff any extracted author matches the query (or any
        name in `accept_authors`) under `authors_match` — the
        normalized + fuzzy comparator, so "William Arand" and
        "William D. Arand" and "W.D. Arand" all match.
      * If the card has NO discoverable author attribution (Amazon
        sometimes renders compact cards that omit the byline), the
        card is accepted for detail-page verification rather than
        rejected up front. The detail-page author gate downstream
        does the real filtering — the search filter's job is to
        cut obvious noise, not be the authority.

    `accept_authors` lets callers include pen-name aliases so a
    scan of "Randi Darren" accepts books bylined "William D. Arand"
    (the real author) and vice versa.
    """
    from bs4 import BeautifulSoup
    from app.metadata.author_names import authors_match

    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()

    # Build the acceptable-authors list once. None check for backwards
    # compat with the unit tests that call this without the kwarg.
    author_candidates: list[str] = []
    if author_name:
        author_candidates.append(author_name)
    for extra in accept_authors or []:
        if extra and extra not in author_candidates:
            author_candidates.append(extra)

    # Primary: data-asin cards in the main results container
    for card in soup.select("[data-asin]"):
        asin = card.get("data-asin", "").strip()
        if not asin or len(asin) != 10 or asin in seen:
            continue

        # Extract title from the card's heading area
        title = ""
        # Try h2 > a > span (most common layout)
        h2 = card.select_one("h2")
        if h2:
            span = h2.select_one("a span") or h2.select_one("span")
            if span:
                title = span.get_text(strip=True)

        if not title:
            continue

        # Author validation via extracted names + authors_match.
        if author_candidates:
            card_authors = _extract_card_authors(card)
            if card_authors:
                # Attribution found — accept iff any matches via
                # normalized + fuzzy comparison.
                matched = any(
                    authors_match(candidate, ca)
                    for candidate in author_candidates
                    for ca in card_authors
                )
                if not matched:
                    logger.debug(
                        f"    SKIP (wrong author): '{title}' — card "
                        f"authors {card_authors!r} don't match "
                        f"'{author_name}' (or pen-name aliases)"
                    )
                    continue
            # No attribution on the card → defer to detail-page gate.

        seen.add(asin)
        results.append((asin, title))

    # Fallback: extract from /dp/ links if data-asin didn't work
    if not results:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"/dp/([A-Z0-9]{10})", href)
            if not m:
                continue
            asin = m.group(1)
            if asin in seen:
                continue
            span = a.select_one("span")
            title = span.get_text(strip=True) if span else ""
            if title:
                seen.add(asin)
                results.append((asin, title))

    return results


def _parse_detail_page(
    html: str,
    asin: str,
    expected_authors: list[str] | None = None,
) -> Optional[BookResult]:
    """Parse an Amazon product detail page into a BookResult.

    Extracts title, series info, publication date, page count, ISBN,
    description, cover URL, and language from the product page HTML.

    When `expected_authors` is provided, the #bylineInfo block is
    parsed for contributor names and compared via `authors_match`.
    If none match, returns None — prevents Amazon from adding books
    whose ASIN survived the search filter but whose actual byline is
    a completely different author (the "Dr. William Li" /
    "Kingdom Revival" pollution from earlier UAT).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    # Pre-order check — skip unreleased products
    if soup.find("input", attrs={"name": "submit.preorder"}):
        return None

    # Title (required)
    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(strip=True) if title_el else ""
    if not title:
        return None

    # Author gate — reject early, before any other parsing work.
    if expected_authors:
        from app.metadata.author_names import authors_match

        detail_authors = _extract_detail_authors(soup)
        if not detail_authors:
            # No discoverable byline on the detail page — conservative
            # default is to reject. If Amazon couldn't attribute it,
            # we shouldn't claim it either.
            logger.debug(
                f"    SKIP (no byline): detail page for {asin} "
                f"('{title[:60]}') has no recognizable author info"
            )
            return None
        matched = any(
            authors_match(exp, da)
            for exp in expected_authors
            for da in detail_authors
        )
        if not matched:
            logger.debug(
                f"    SKIP (wrong author on detail): '{title[:60]}' "
                f"(asin={asin}) — detail authors {detail_authors!r} "
                f"don't match {expected_authors!r}"
            )
            return None

    # ── RPI carousel cards — structured metadata ──
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

    # ── Audiobook detection ──
    # Check RPI cards and page text for audiobook format indicators.
    # Amazon audiobook pages show "Audible Audiobook" in format areas
    # and use "Listening Length" instead of page count.
    rpi_text = " ".join(
        f"{v.get('label', '')} {v.get('value', '')}" for v in rpi.values()
    ).lower()
    if any(kw in rpi_text for kw in _AUDIO_FORMAT_KEYWORDS):
        logger.debug("amazon: skipping audiobook page for %s (%s)", asin, title[:60])
        return None

    # Also check the product subtitle / category breadcrumb area
    subtitle_el = soup.select_one("#productSubtitle")
    if subtitle_el:
        subtitle = subtitle_el.get_text(strip=True).lower()
        if any(kw in subtitle for kw in _AUDIO_FORMAT_KEYWORDS):
            logger.debug("amazon: skipping audiobook (subtitle) for %s", asin)
            return None

    # ── Series extraction (primary value of this source) ──
    series_name = None
    series_index = None

    # Try 1: RPI series card
    series_card = rpi.get("rpi-attribute-book_details-series", {})
    if series_card.get("value"):
        series_name = series_card["value"]
        m = _BOOK_NUM_RE.search(series_card.get("label", ""))
        if m:
            try:
                series_index = float(m.group(1))
            except ValueError:
                pass

    # Try 2: Series bullet widget (CWA pattern)
    if not series_name:
        widget = soup.find(attrs={"data-feature-name": "seriesBulletWidget"})
        if widget:
            text = widget.get_text(" ", strip=True)
            m = _SERIES_BULLET_RE.search(text)
            if m:
                try:
                    series_index = float(m.group(1))
                except ValueError:
                    pass
                series_name = m.group(2).strip()

    # Strip series name from title if present as parenthetical
    if series_name and series_name in title:
        title = re.sub(
            _SERIES_PAREN_RE_TMPL.format(series=re.escape(series_name)),
            "",
            title,
        ).strip()

    # ── Page count ──
    pages = None
    pages_card = rpi.get("rpi-attribute-book_details-ebook_pages", {})
    if pages_card.get("value"):
        m = re.search(r"(\d+)", pages_card["value"])
        if m:
            pages = int(m.group(1))

    # ── Publication date ──
    pub_date = None
    date_card = rpi.get("rpi-attribute-book_details-publication_date", {})
    if date_card.get("value"):
        pub_date = _parse_amazon_date(date_card["value"])

    # ── Language ──
    language = None
    lang_card = rpi.get("rpi-attribute-language", {})
    if lang_card.get("value"):
        language = lang_card["value"]

    # ── ISBN-13 + fallback pub date from detail bullets ──
    isbn = None
    for li in soup.select(
        "#detailBulletsWrapper_feature_div li, "
        "#detailBullets_feature_div li"
    ):
        for s in li.select("span.a-text-bold"):
            label = (
                s.get_text(strip=True)
                .replace("\u200f", "")
                .replace("\u200e", "")
            )
            val_span = s.find_next_sibling("span")
            val = val_span.get_text(strip=True) if val_span else ""
            if "ISBN-13" in label and val:
                isbn = val.replace("-", "")
            if "Publication date" in label and val and not pub_date:
                pub_date = _parse_amazon_date(val)

    # ── Description ──
    description = None
    desc_el = soup.find("div", attrs={"data-feature-name": "bookDescription"})
    if desc_el:
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

    # ── Cover URL — multi-tier fallback ──
    cover_url = None
    # Tier 1: High-res from script JSON blocks (best quality)
    for script in soup.find_all("script"):
        text = script.string or ""
        m = _HIRES_RE.search(text)
        if m:
            cover_url = m.group(1)
            break
    # Tier 2: Dynamic image element
    if not cover_url:
        img = soup.select_one("img.a-dynamic-image")
        if img:
            cover_url = img.get("src") or ""
    # Tier 3: Legacy element IDs with size suffix cleanup
    if not cover_url:
        for sel in ("#imgBlkFront", "#ebooksImgBlkFront", "#landingImage"):
            img = soup.select_one(sel)
            if img:
                cover_url = img.get("src") or ""
                if cover_url:
                    cover_url = _COVER_SUFFIX_RE.sub(".", cover_url)
                    break

    return BookResult(
        title=title,
        series_name=series_name,
        series_index=series_index,
        isbn=isbn,
        cover_url=cover_url or None,
        pub_date=pub_date,
        description=description,
        page_count=pages,
        language=language,
        external_id=asin,
        source="amazon",
        source_url=f"https://www.amazon.com/dp/{asin}",
    )


def _parse_amazon_date(text: str) -> Optional[str]:
    """Parse Amazon's various date formats into ISO YYYY-MM-DD."""
    if not text:
        return None
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y", "%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
