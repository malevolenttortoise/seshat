"""
Kobo source — third in the priority chain after Goodreads and Hardcover.

Kobo has no public API, so this module scrapes the storefront pages.
Cloudflare sits in front of kobo.com; requests go through
`app.metadata.kobo_session` (curl_cffi with Chrome's TLS profile, shared
with the enrichment source). A Cloudflare challenge is never solved
(ADR-0025): it is counted as a block and Kobo backs off. Until the
2026-10 audit this used `cloudscraper`, a challenge solver.

Two passes per author, mirroring the goodreads pattern:

  1. Author search page (`?fcsearchfield=Author&fcmedia=Book`) for the
     full set of book URLs, titles, and thumbnail covers. Paginates
     through `&pagenumber=N` up to `MAX_PAGES`.
  2. Per-book detail page for the rich metadata Kobo only exposes on
     book pages: series name + index, ISO publication date, language,
     ISBN-13, page count, description, publisher, full-resolution
     cover.

Optimizations that keep scan times bounded:
  - Books already in the DB get a minimal BookResult for URL backfill,
    no detail fetch.
  - In `owned_only` (Library-only) mode, books that don't match
    `owned_titles` are skipped entirely — saves ~3s per skipped book
    at the rate limit, which dominates total scan time.
  - `&fcmedia=Book` filters audiobooks out of the search before they
    can pollute the per-book pass.
  - kobo_id and ISBN-13 dedupe passes catch the two distinct ways
    Kobo's catalog produces duplicates.

Per-book progress hook: `self._on_book(title)` is called for DETAIL
fetches and URL-backfill emits, but NOT for filter-noise skips.
"""
import logging, asyncio, re
from datetime import datetime
from typing import Optional
from lxml import html
from app.discovery.sources.base import BaseSource, AuthorResult, BookResult, SeriesResult
from app.metadata import kobo_session

logger = logging.getLogger("seshat.discovery.kobo")
BASE = "https://www.kobo.com"


def _parse_kobo_date(text: str) -> Optional[str]:
    """Parse a Kobo 'Release Date:' value to ISO YYYY-MM-DD.

    Kobo's eBook Details panel renders dates as 'June 15, 2011' or, for
    pre-orders/old titles where only month or year is known, 'June 2011'
    or '2011'. We try each from most-specific to least and fall back to
    None on a miss so the merge layer treats the field as unknown.
    """
    if not text:
        return None
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y", "%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _strip_punct_ws(s: str) -> str:
    """Lowercase + strip everything except [a-z0-9]. Used for the
    most-aggressive author-name comparison tier — collapses
    "J. N. Chaney" and "J.N. Chaney" both to "jnchaney"."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _kobo_author_matches(
    card_author: str, queried: str, linked: list[str],
) -> bool:
    """Decide whether `card_author` (Kobo's per-result author byline)
    matches the queried author or one of their pen-name aliases.

    Four match tiers, tried in order. Lowest-friction first:

      1. Exact lowercased equality
      2. Period-strip equality ("J. K. Rowling" / "JK Rowling")
      3. Punctuation + whitespace strip equality (v2.10.6 fix —
         "J. N. Chaney" vs "J.N. Chaney" both → "jnchaney"; the
         period-strip alone leaves "j n chaney" vs "jn chaney" which
         don't compare equal). Same root cause we fixed in
         HardcoverSource at v2.10.5.
      4. Parts-set equality for word-order shuffles ("Rowling J K")

    Extracted to module level so the matcher is unit-testable
    independently of the surrounding `get_author_books` plumbing.
    """
    if not card_author:
        return False
    cn = card_author.lower().strip()
    accepted = {queried.lower().strip()}
    for ln in linked or []:
        if ln:
            accepted.add(ln.lower().strip())

    if cn in accepted:
        return True
    cn_no_dot = cn.replace(".", "")
    if cn_no_dot in {a.replace(".", "") for a in accepted}:
        return True
    if _strip_punct_ws(cn) in {_strip_punct_ws(a) for a in accepted}:
        return True
    queried_parts = set(queried.lower().replace(".", "").split())
    cn_parts = set(cn_no_dot.split())
    if cn_parts and cn_parts == queried_parts:
        return True
    return False


class KoboSource(BaseSource):
    """Kobo fetches through `kobo_session` (curl_cffi) instead of the base
    class's httpx client. It still inherits from BaseSource for interface
    consistency and the shared logger/rate_limit."""
    name = "kobo"

    def __init__(self, rate_limit: float = 3.0, concurrency: int = 4):
        super().__init__(rate_limit=rate_limit)
        # v2.11.0: per-book detail fetches run with bounded concurrency
        # via an asyncio.Semaphore. Since the 2026-10 audit every request
        # waits Kobo's turn in the source gate, so concurrency only
        # overlaps parsing; the request rate is one per Rate seconds (G72).
        self.concurrency = concurrency

    async def _fetch(self, url: str) -> Optional[str]:
        # 30s read timeout: Kobo detail pages can be slow mid-scan on
        # prolific authors. Pacing is the source gate's.
        return await kobo_session.fetch(url, timeout=30.0)

    async def _get_book_details(self, kobo_url: str) -> dict:
        """Fetch a Kobo book detail page and extract structured metadata.

        Kobo's search results only carry title + thumbnail + URL —
        everything else (series, language, ISBN, page count,
        description, full-res cover) lives only on the detail page,
        and the detail page renders it all in clean static HTML so
        we don't need a JS engine.

        Selectors used:
          h1.title.product-field                  → title
          span.series.product-field a             → series name
          span.sequenced-name-prefix              → "Book N -" index
          div.book-stats strong (next to "Pages") → page_count
          div.bookitem-secondary-metadata li      → release date, ISBN,
                                                    language, publisher
          div[data-full-synopsis]                 → description
          img.cover-image                         → high-res cover

        Every field defaults to None so callers can build a BookResult
        with COALESCE-friendly nulls.
        """
        details = {
            "title": None, "series_name": None, "series_index": None,
            "pub_date": None, "language": None, "isbn": None,
            "page_count": None, "description": None, "publisher": None,
            "cover_url": None,
        }
        page_html = await self._fetch(kobo_url)
        if not page_html:
            return details
        try:
            page = html.fromstring(page_html)
        except Exception as e:
            logger.debug(f"  Kobo: detail-page parse error for {kobo_url}: {e}")
            return details

        # Title (canonical, used to validate the page actually loaded)
        title_el = page.xpath("//h1[contains(@class,'title') and contains(@class,'product-field')]/text()")
        if title_el:
            details["title"] = title_el[0].strip()

        # High-res cover (Kobo serves a 353x569 version on detail pages
        # vs the 80x120 thumbnail on search results)
        cover_el = page.xpath("//img[contains(@class,'cover-image')]/@src")
        if cover_el:
            c = cover_el[0]
            details["cover_url"] = ("https:" + c) if c.startswith("//") else c

        # Series name (anchor inside span.series.product-field)
        series_el = page.xpath("//span[contains(@class,'series') and contains(@class,'product-field')]//a/text()")
        if series_el:
            details["series_name"] = series_el[0].strip()

        # Series index — Kobo renders this as "Book 1 - " in a separate
        # span before the series link. Pull the first number; supports
        # decimals like "Book 2.5 - " for novellas.
        seq_el = page.xpath("//span[@class='sequenced-name-prefix']/text()")
        if seq_el:
            m = re.search(r'(\d+(?:\.\d+)?)', seq_el[0])
            if m:
                try:
                    details["series_index"] = float(m.group(1))
                except ValueError:
                    pass

        # Page count: <strong>592</strong> followed by <span>Pages</span>
        # inside book-stats. We pick the strong whose sibling span text is
        # exactly "Pages" so we don't confuse it with "hours" or "words".
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

        # eBook Details panel (Release Date, ISBN, Language, Publisher).
        # Each <li> is a labeled field except the very first which is just
        # the publisher name (no "Publisher:" prefix on Kobo).
        detail_lis = page.xpath("//div[contains(@class,'bookitem-secondary-metadata')]//li")
        known_prefixes = ("Release Date:", "Book ID:", "Language:", "Imprint:",
                          "Download options:", "File size:", "ISBN:")
        for li in detail_lis:
            text = li.text_content().strip()
            if text.startswith("Release Date:"):
                details["pub_date"] = _parse_kobo_date(text.split(":", 1)[1])
            elif text.startswith("Book ID:") or text.startswith("ISBN:"):
                # Kobo labels their identifier "Book ID" but it's the EAN/ISBN-13
                isbn = text.split(":", 1)[1].strip()
                # Validate it looks like an ISBN-13 (13 digits) before accepting
                if re.fullmatch(r'\d{10}|\d{13}', isbn):
                    details["isbn"] = isbn
            elif text.startswith("Language:"):
                details["language"] = text.split(":", 1)[1].strip()
            elif not any(text.startswith(p) for p in known_prefixes):
                # Unlabeled <li> = publisher name (always the first li in
                # the panel). Only set once so we don't overwrite with
                # later unlabeled noise.
                if not details["publisher"]:
                    details["publisher"] = text

        # Description: Kobo hides the full synopsis in <div data-full-synopsis>
        # which is display:none until the user clicks "Read more". The text
        # is present in the static HTML so we don't need JS execution.
        # We strip the trailing series listing (Kobo often appends "The
        # Expanse: Leviathan Wakes / Caliban's War / ..." after the synopsis)
        # by capping at 2000 chars — long enough for any real synopsis,
        # short enough to drop the bibliography.
        desc_el = page.xpath("//div[@data-full-synopsis]")
        if desc_el:
            desc_text = desc_el[0].text_content().strip()
            desc_text = re.sub(r'\s+', ' ', desc_text)
            if desc_text:
                details["description"] = desc_text[:2000]

        return details

    async def search_author(self, author_name: str) -> Optional[AuthorResult]:
        try:
            search_url = f"{BASE}/us/en/search?query={author_name.replace(' ', '%20')}&fcsearchfield=Author"
            page_html = await self._fetch(search_url)
            if not page_html:
                return None

            page = html.fromstring(page_html)

            # Check for author match in results
            # New Kobo: data-testid='search-result-widget'
            # Old Kobo: h2.title.product-field
            result_titles_new = page.xpath("//a[@data-testid='title']")
            result_titles_old = page.xpath("//h2[@class='title product-field']/a")
            result_titles = result_titles_new or result_titles_old

            # external_id stores the ORIGINAL author_name (not a URL
            # slug), because Kobo has no stable public author-id and
            # get_author_books needs to re-query by name. Storing the
            # raw name round-trips cleanly through Unicode and
            # punctuation; storing a slug and then reconstructing via
            # `replace("-", " ").title()` is lossy on apostrophes,
            # accents, and intentional hyphenation ("O'Brien" →
            # "O'brien", "Jean-Luc" → "Jean Luc").
            if result_titles:
                which = "new layout" if result_titles_new else "old layout"
                logger.debug(f"  Kobo: matched {len(result_titles)} results via {which} selectors for '{author_name}'")
                return AuthorResult(name=author_name, external_id=author_name)

            # Neither selector matched. Two possibilities: Kobo genuinely
            # has no results (expected for obscure authors), or Kobo changed
            # their DOM again and our selectors are stale. Distinguish the
            # two via explicit markers before the pre-Phase-3a code's
            # "assume the author exists" fallback — that fallback was
            # silently masking DOM changes that would show up as empty
            # AuthorResult objects downstream.
            if "No results found" in page_html:
                logger.debug(f"  Kobo: 'No results found' marker present for '{author_name}'")
                return None
            if len(page_html) < 5000:
                logger.debug(f"  Kobo: short response ({len(page_html)} bytes) for '{author_name}' — likely empty/error page")
                return None

            # Page is >5000 bytes and has no "No results" marker but
            # NONE of our selectors matched. Almost certainly a Kobo
            # DOM change. Warn loudly so the user notices and can
            # report the layout change, and return None rather than
            # constructing a fake AuthorResult — a fake one would only
            # trigger a pointless follow-up fetch downstream.
            logger.warning(
                f"  Kobo: {len(page_html)} bytes returned for '{author_name}' "
                f"but no result selectors matched — Kobo may have changed their DOM"
            )
            return None

        except Exception as e:
            logger.error(f"Kobo search error '{author_name}': {e}")
            return None

    async def get_author_books(self, author_name: str, existing_titles: set = None, owned_only: bool = False, owned_titles: list = None, **kw) -> Optional[AuthorResult]:
        """Search Kobo for an author, then enrich each book via its detail page.

        `author_name` arrives from `search_author`'s external_id —
        which IS the original user-provided name, not a URL slug, so
        no lossy normalization is needed.

        Two optimizations layered on top of the basic two-pass scrape:
          - Books already in the DB get a URL-backfill BookResult
            with no detail fetch (Kobo's search results only carry
            title + cover anyway).
          - In `owned_only` mode, books that don't match `owned_titles`
            never reach the detail fetch — they'd be dropped by the
            merge layer downstream regardless. This trims minutes off
            scans for prolific authors with large unowned catalogs.
        """
        if existing_titles is None:
            existing_titles = set()
        try:
            # `&fcmedia=Book` filters out audiobooks at the search
            # layer. The naming is counterintuitive — "Book" is Kobo's
            # internal label for the eBook category, with Audiobook as
            # its own peer category. Without this filter, prolific
            # audiobook-only authors like J.N. Chaney return ~280
            # audiobook results that fuzzy-match against owned ebook
            # rows in the URL-backfill pass, polluting source_url with
            # wrong-format URLs. The filter is harmless for normal
            # authors and surgically protective for the edge case.
            base_search_url = (
                f"{BASE}/us/en/search?query={author_name.replace(' ', '%20')}"
                f"&fcsearchfield=Author&fcmedia=Book&numrecords=60"
            )
            page_html = await self._fetch(base_search_url)
            if not page_html:
                return None

            page = html.fromstring(page_html)
            books = []
            series_map = {}

            def _extract_items(p):
                # New search page format
                its = p.xpath("//a[@data-testid='title']")
                if not its:
                    # Old format
                    its = p.xpath("//h2[@class='title product-field']/a")
                return its

            items = _extract_items(page)
            if not items:
                logger.debug(f"  Kobo: no book items matched on get_author_books page for '{author_name}' ({len(page_html)} bytes)")

            # ── Pagination ─────────────────────────────────────────
            # Kobo's author search caps at 60 results per page
            # (`&numrecords=60`). Prolific authors silently truncate
            # at the first page without this loop — Sanderson has 60+
            # ebook results, J.N. Chaney has ~280 across all formats.
            #
            # Pagination is JavaScript-driven `<button>` elements
            # rather than href links, but Kobo's canonical URL still
            # exposes the state as `&pagenumber=N` (page 1 has no
            # param). The total page count lives in plain text inside
            # `<button data-testid="pagination-item-last-page"><span>N</span></button>`,
            # which we parse to know when to stop.
            #
            # MAX_PAGES caps the worst case at 600 entries (60 × 10),
            # comfortably above any plausible single-author catalog.
            MAX_PAGES = 10
            last_page = 1
            last_page_btns = page.xpath(
                "//button[@data-testid='pagination-item-last-page']//span/text()"
            )
            if last_page_btns:
                try:
                    last_page = int(last_page_btns[0].strip())
                except (ValueError, TypeError):
                    last_page = 1

            if last_page > 1:
                target_pages = min(last_page, MAX_PAGES)
                logger.info(
                    f"  Kobo: '{author_name}' has {last_page} result pages — "
                    f"fetching {target_pages}{' (capped)' if last_page > MAX_PAGES else ''}"
                )
                for pn in range(2, target_pages + 1):
                    page_url = f"{base_search_url}&pagenumber={pn}"
                    extra_html = await self._fetch(page_url)
                    if not extra_html:
                        logger.debug(f"  Kobo: page {pn} fetch failed — stopping pagination")
                        break
                    extra_page = html.fromstring(extra_html)
                    extra_items = _extract_items(extra_page)
                    if not extra_items:
                        logger.debug(f"  Kobo: page {pn} returned 0 items — stopping pagination")
                        break
                    items = items + extra_items
                    logger.debug(f"  Kobo: page {pn} added {len(extra_items)} raw items (running total {len(items)})")

            # Author validation: Kobo's `&fcsearchfield=Author` query
            # treats the queried name as a substring against any
            # contributor field — translator, foreword, illustrator,
            # "various authors" anthologies — so we get false positives
            # under the queried author. Each result card carries a
            # `data-testid="authors"` element with the actual primary
            # author(s) of that book, so we walk up to the result-card
            # ancestor and pull the displayed author for per-book filter
            # before queuing the slow detail fetch.
            #
            # Linked author names (pen names + co-authors set up by the
            # user) are accepted too — set on the source instance via
            # `_linked_author_names` before the call.
            linked = list(getattr(self, "_linked_author_names", None) or [])

            def _author_matches(card_author: str) -> bool:
                return _kobo_author_matches(card_author, author_name, linked)

            # Pass 1: collect raw search results (title, href, cover
            # thumbnail). Dedupe by kobo_id because the XPath
            # `//a[@data-testid='title']` matches BOTH the cover-image
            # anchor AND the title-text anchor for each result —
            # without this dedupe every book would be processed twice.
            raw_books = []
            seen_ids = set()
            skipped_bad_author = 0
            for item in items:
                title = item.text_content().strip()
                href = item.get("href", "")
                if not title:
                    continue

                # Extract Kobo book ID from URL
                kobo_id = href.rstrip("/").split("/")[-1] if href else None

                # Dedupe: skip if we've already seen this kobo_id. Falls
                # back to (title, href) for items missing a kobo_id so
                # we still dedupe correctly on edge cases.
                dedupe_key = kobo_id or (title, href)
                if dedupe_key in seen_ids:
                    continue
                seen_ids.add(dedupe_key)

                # Author validation — pair the title with its result
                # card's `data-testid="authors"` block. Each card may
                # list multiple authors (anthologies, co-authors); we
                # accept the result if ANY listed author matches.
                card = item.xpath(
                    "ancestor::*[descendant::*[@data-testid='authors']][1]"
                )
                card_authors: list[str] = []
                if card:
                    card_authors = [
                        a.strip()
                        for a in card[0].xpath(
                            ".//*[@data-testid='authors']//a/text()"
                        )
                        if a.strip()
                    ]
                if card_authors and not any(_author_matches(ca) for ca in card_authors):
                    skipped_bad_author += 1
                    logger.info(
                        f"  Kobo: skipping '{title}' — authors "
                        f"{card_authors} don't match ['{author_name}']"
                    )
                    continue

                # Try to get cover image (thumbnail from search page; will
                # be replaced with the full-res version from the detail
                # page if the per-book fetch runs)
                cover = None
                parent = item.xpath("ancestor::div[contains(@class,'item-detail') or contains(@class,'result-item')]")
                if parent:
                    img = parent[0].xpath(".//img/@src")
                    if img:
                        cover = img[0]
                        if cover.startswith("//"):
                            cover = "https:" + cover

                # Build full Kobo URL
                kobo_url = None
                if href:
                    kobo_url = href if href.startswith("http") else f"https://www.kobo.com{href}"

                raw_books.append({
                    "title": title, "kobo_id": kobo_id,
                    "cover": cover, "kobo_url": kobo_url,
                })

            # Pass 2: per-book detail enrichment. Skip-known mirrors
            # the goodreads.py logic — for titles already in the DB
            # we only need to backfill the source_url.
            def _norm(t):
                t = re.sub(r'[^\w\s]', '', t.lower()).strip()
                return re.sub(r'\s+', ' ', t)

            existing_norm = {_norm(t) for t in existing_titles}
            skipped_known = 0
            skipped_unowned = 0
            enriched = 0
            # Secondary ISBN dedupe — Kobo sometimes has multiple
            # distinct `kobo_id` store listings for what is actually
            # the same edition (regional storefronts, admin
            # duplicates). The kobo_id-keyed dedupe in Pass 1 can't
            # catch these because the store IDs are genuinely
            # different; only the ISBN-13 reveals them as the same
            # book. We can't avoid the wasted detail fetch (we need
            # the page to learn the ISBN), but we can keep the
            # duplicate out of the merge pass and the final count.
            seen_isbns = set()
            dupe_isbns = 0

            # v2.11.0: classify-then-parallel-fetch refactor. The old
            # sequential loop ran ~3s/book minimum (rate limit dominated)
            # and topped out at 80-180s for prolific authors. The new
            # pattern partitions raw_books into three buckets:
            #   1. URL-backfill (known title) — no detail fetch needed
            #   2. SKIP-UNOWNED (library-only mode + not in owned_titles)
            #   3. DETAIL fetch — the slow path, now run in parallel via
            #      asyncio.Semaphore-gated workers
            # Phase 3 (ISBN dedup + BookResult assembly) stays sequential
            # because dedup state must be ordered.

            url_backfill_rbs: list[dict] = []
            detail_rbs: list[dict] = []
            for rb in raw_books:
                norm = _norm(rb["title"])
                is_known = bool(existing_norm) and any(
                    norm == et or norm in et or et in norm for et in existing_norm
                )
                if is_known or not rb["kobo_url"]:
                    url_backfill_rbs.append(rb)
                    continue
                if owned_only:
                    is_owned = False
                    if owned_titles:
                        owned_norm = [_norm(ot) for ot in owned_titles]
                        is_owned = any(
                            norm == on or norm in on or on in norm
                            for on in owned_norm
                        )
                    if not is_owned:
                        skipped_unowned += 1
                        logger.debug(f"    SKIP-UNOWNED (library-only): '{rb['title']}'")
                        continue
                detail_rbs.append(rb)

            # Pass 2a — URL-backfill emit (fast, sequential).
            for rb in url_backfill_rbs:
                skipped_known += 1
                logger.debug(f"    SKIP-KNOWN (URL backfill): '{rb['title']}'")
                on_book = getattr(self, '_on_book', None)
                if on_book:
                    on_book(rb["title"])
                books.append(BookResult(
                    title=rb["title"], cover_url=rb["cover"],
                    external_id=rb["kobo_id"], source="kobo",
                    source_url=rb["kobo_url"],
                ))

            # Pass 2b — bump new-candidate counter once per detail RB
            # so the UI ticker climbs through the parallel work too.
            on_new_candidate = getattr(self, '_on_new_candidate', None)
            if on_new_candidate:
                for _ in detail_rbs:
                    on_new_candidate()

            # Pass 2c — parallel detail fetches. Bounded by
            # asyncio.Semaphore so we don't fire more than
            # `self.concurrency` simultaneous fetches.
            # Every fetch waits Kobo's turn in the source gate, so the
            # request rate is one per Metadata Sources rate whatever the
            # concurrency (G72: ~0.33 req/s at 3s; it used to be
            # ~concurrency/rate_limit, ~1.33 req/s).
            if detail_rbs:
                sem = asyncio.Semaphore(self.concurrency)
                on_book = getattr(self, '_on_book', None)
                progress_counter = {"n": 0}
                total = len(detail_rbs)

                async def _fetch_one(rb_local: dict) -> tuple[dict, dict]:
                    async with sem:
                        details = await self._get_book_details(rb_local["kobo_url"])
                    # Emit per-book progress on completion (not on submit)
                    # so the UI ticker reflects actual finished work.
                    progress_counter["n"] += 1
                    n = progress_counter["n"]
                    if on_book:
                        on_book(rb_local["title"])
                    if n == 1 or n % 5 == 0 or n == total:
                        logger.info(
                            f"  Kobo: detail {n}/{total} done "
                            f"(concurrency={self.concurrency}, rate_limit={self.rate_limit}s)"
                        )
                    return rb_local, details

                # gather preserves submission order so the subsequent
                # ISBN-dedup pass sees results in raw_books order.
                fetch_results = await asyncio.gather(
                    *(_fetch_one(rb) for rb in detail_rbs)
                )
            else:
                fetch_results = []

            # Pass 3 — sequential ISBN-dedup + BookResult assembly.
            # MUST be sequential because seen_isbns is order-dependent
            # (first wins on collision).
            for rb, details in fetch_results:
                enriched += 1
                isbn = details.get("isbn")
                if isbn and isbn in seen_isbns:
                    dupe_isbns += 1
                    logger.debug(
                        f"    DUPE-ISBN: '{rb['title']}' (isbn={isbn}) — "
                        f"already seen, skipping BookResult"
                    )
                    continue
                if isbn:
                    seen_isbns.add(isbn)

                logger.debug(
                    f"    DETAIL: '{rb['title']}' → series={details.get('series_name')}"
                    f"#{details.get('series_index')}, date={details.get('pub_date')},"
                    f" lang={details.get('language')}, isbn={details.get('isbn')},"
                    f" pages={details.get('page_count')}"
                )

                br = BookResult(
                    title=rb["title"],
                    series_name=details.get("series_name"),
                    series_index=details.get("series_index"),
                    isbn=details.get("isbn"),
                    cover_url=details.get("cover_url") or rb["cover"],
                    pub_date=details.get("pub_date"),
                    description=details.get("description"),
                    page_count=details.get("page_count"),
                    external_id=rb["kobo_id"],
                    language=details.get("language"),
                    source="kobo",
                    source_url=rb["kobo_url"],
                )

                if details.get("series_name"):
                    sname = details["series_name"]
                    if sname not in series_map:
                        series_map[sname] = SeriesResult(name=sname, books=[])
                    series_map[sname].books.append(br)
                else:
                    books.append(br)

            logger.info(
                f"  Kobo: found {len(books) + sum(len(s.books) for s in series_map.values())} "
                f"books for '{author_name}' ({enriched} enriched, {skipped_known} URL-backfill"
                f"{f', {skipped_unowned} skipped (library-only)' if skipped_unowned else ''}"
                f"{f', {skipped_bad_author} skipped (wrong author)' if skipped_bad_author else ''}"
                f"{f', {dupe_isbns} ISBN-dupes dropped' if dupe_isbns else ''})"
            )

            return AuthorResult(
                name=author_name, external_id=author_name,
                books=books, series=list(series_map.values()),
            )
        except Exception as e:
            logger.error(f"Kobo author books error '{author_name}': {e}")
            return None

    async def close(self):
        # The curl_cffi session is shared (`kobo_session`); nothing of its
        # own to close.
        await super().close()
