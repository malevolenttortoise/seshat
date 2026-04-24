"""
Goodreads source — primary metadata provider.

Goodreads has the most complete catalog of the three sources but no
public API, so this module scrapes the public HTML pages. Two passes
per author:

  1. Author list page (`/author/list/{id}?per_page=100`) for every
     book ID, title, list-page series, cover, translator marker, and
     contributor marker. Paginates through `?page=N` until exhausted.
  2. Individual book pages (`/book/show/{id}`) for the full per-book
     details: authoritative language (from JSON-LD `inLanguage`), pub
     date, expected date, page count, series name + index, set/
     collection detection, translator confirmation, description, and
     a higher-quality cover.

The two-pass design exists because the list page doesn't carry enough
metadata to confidently filter foreign editions, sets, or translator-
only credits, but visiting every book page for every author is
expensive — so the list-page pre-filters cheap exclusions (`(translator)`,
`(contributor)`, obvious set titles) and the book-page pass only runs
for survivors.

Per-book progress hook: this module calls `self._on_book(title)` (set
by lookup.py) on every entry that does real work (a DETAIL fetch or a
URL-backfill emit) but NOT on filter-noise skips, so the unified scan
widget never flickers through "skipped translator", "skipped foreign",
etc.
"""
import asyncio, logging, re, json
from collections import Counter
from datetime import datetime
from typing import Optional
from bs4 import BeautifulSoup
from app.discovery.sources.base import BaseSource, AuthorResult, BookResult, SeriesResult
from app.metadata.author_names import author_name_variants, authors_match

logger = logging.getLogger("seshat.discovery.goodreads")
BASE = "https://www.goodreads.com"
HDR = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def _parse_date(text: str) -> Optional[str]:
    """Try to parse a date from various Goodreads formats."""
    if not text:
        return None
    text = re.sub(r'(\d+)(?:st|nd|rd|th)', r'\1', text.strip())
    for fmt in ["%B %d, %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y",
                "%b %d, %Y", "%b %d %Y", "%B %Y", "%b %Y", "%Y"]:
        try:
            dt = datetime.strptime(text.strip(), fmt)
            if dt.year < 100:
                dt = dt.replace(year=dt.year + 2000)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _is_future(d: str) -> bool:
    try:
        return datetime.strptime(d[:10], "%Y-%m-%d") > datetime.now()
    except (ValueError, TypeError):
        return False


def _is_set_from_series(series_text: str) -> bool:
    """Detect if series info indicates a box set/collection (e.g. '#1-6', '#1-7')."""
    return bool(re.search(r'#\d+\s*[-–]\s*\d+', series_text))


def _pick_author_from_book_search(
    html: str, query_name: str
) -> Optional[AuthorResult]:
    """Extract the most plausible author from a Goodreads book-search page.

    Counts `(author_name, author_id)` pairs across the `a.authorName`
    anchors on the page, ranks by frequency, and returns the top-ranked
    pair that passes `authors_match` against `query_name`. Returns
    `None` when there are no anchors or when no top-ranked pair is a
    plausible name match.

    Author-page image is best-effort: scan the anchor's row for an
    `img` with a non-`nophoto` src. If the page layout hides it, the
    result still returns with `image_url=None`.
    """
    soup = BeautifulSoup(html, "lxml")
    anchors = soup.select("a.authorName")
    if not anchors:
        return None

    counts: Counter[tuple[str, str]] = Counter()
    images: dict[tuple[str, str], str] = {}
    for a in anchors:
        nm = a.get_text(strip=True)
        href = a.get("href") or ""
        m = re.search(r"/author/show/(\d+)", href)
        if not m or not nm:
            continue
        key = (nm, m.group(1))
        counts[key] += 1
        if key not in images:
            parent = a.find_parent("tr") or a.find_parent("div")
            if parent:
                img_el = parent.select_one("img")
                if img_el:
                    src = img_el.get("src") or ""
                    if src and "nophoto" not in src:
                        images[key] = src

    if not counts:
        return None

    # Walk most-common first; accept the first that passes the
    # name-match gate. If none do, there's no confident result on this
    # page — return None so the caller can try the next variant.
    for (name, author_id), _count in counts.most_common():
        if authors_match(name, query_name):
            return AuthorResult(
                name=name,
                external_id=author_id,
                image_url=images.get((name, author_id)),
            )
    return None


class GoodreadsSource(BaseSource):
    name = "goodreads"
    default_headers = HDR
    default_timeout = 60.0
    # follow_redirects=True is the base default

    def __init__(self, rate_limit: float = 2.0):
        super().__init__(rate_limit=rate_limit)
        # Resume-from-position state for the v1.2 retry feature.
        # Populated after each book's detail page is merged into the
        # local `books`/`series_map`, then either cleared on normal
        # return (clean completion — nothing to resume) or preserved
        # across a CancelledError raised by the caller's wait_for
        # timeout. The caller (`lookup_author`) checks this after a
        # Goodreads timeout; if populated AND the scan budget has time
        # left, it kicks off a second call with start_at=index to
        # process the remainder. The second call inherits the prior
        # books/series_map snapshot so partial work from the first
        # call isn't lost even when the retry only finishes part of
        # the remainder. Per-source-instance (not global) so pen-name
        # linked pairs or concurrent scans don't cross-contaminate.
        self._partial_state: Optional[dict] = None
    # No custom _get — base provides it

    async def _get_book_details(self, book_id: str, title: str) -> dict:
        """Visit individual book page to get full details."""
        details = {
            "language": None, "pub_date": None, "expected_date": None,
            "is_unreleased": False, "is_set": False, "is_translation": False,
            "is_audiobook": False,
            "series_name": None, "series_index": None, "description": None,
            "page_count": None, "cover_url": None,
        }
        try:
            r = await self._get(f"{BASE}/book/show/{book_id}")
            soup = BeautifulSoup(r.text, "lxml")
            page_text = soup.get_text(" ", strip=True)

            # --- Language ---
            # Defer to JSON-LD `inLanguage` (authoritative, parsed in
            # the JSON-LD block below) and fall back to a strict
            # text-regex scan only if JSON-LD didn't yield a language.
            # The regex IS allowlisted to specific language names —
            # a loose `Language\s+(\w+)` once matched "and" out of body
            # text containing the phrase "language and", which silently
            # marked Leviathan Wakes as foreign and left an unrelated
            # anthology as the only Corey result.

            # --- Translator detection ---
            if "(translator)" in page_text.lower() or "translator" in page_text.lower()[:2000]:
                details["is_translation"] = True

            # --- Publication date ---
            # Try data-testid publicationInfo
            pub_el = soup.find("p", {"data-testid": "publicationInfo"})
            if pub_el:
                pt = pub_el.get_text(strip=True)
                # "First published January 1, 2020"
                dm = re.search(r'(?:published|Published)\s+(.+?)$', pt)
                if dm:
                    details["pub_date"] = _parse_date(dm.group(1))
                # "Expected publication April 4, 2026"
                em = re.search(r'[Ee]xpected\s+(?:publication\s+)?(.+?)$', pt)
                if em:
                    details["expected_date"] = _parse_date(em.group(1))
                    details["is_unreleased"] = True

            # Try JSON-LD structured data
            for script in soup.select("script[type='application/ld+json']"):
                try:
                    data = json.loads(script.string)
                    if not details["pub_date"] and data.get("datePublished"):
                        d = data["datePublished"]
                        details["pub_date"] = d[:10] if len(d) >= 10 else _parse_date(d)
                    if data.get("numberOfPages"):
                        try:
                            details["page_count"] = int(data["numberOfPages"])
                        except (ValueError, TypeError):
                            pass
                    if data.get("inLanguage") and not details["language"]:
                        # Goodreads sometimes encodes language as a code
                        # ("en", "en-US") and sometimes as a full name
                        # ("English"). lookup.py's _lang_ok() handles both.
                        details["language"] = data["inLanguage"]
                    if data.get("image"):
                        details["cover_url"] = data["image"]
                    # bookFormat: "Audiobook", "EBook", "Paperback", etc.
                    bf = (data.get("bookFormat") or "").lower()
                    if bf in ("audiobook", "audio", "audio cd", "audible audio"):
                        details["is_audiobook"] = True
                except (ValueError, TypeError, AttributeError):
                    pass

            # Allowlisted text-regex fallback for language. Only runs if
            # JSON-LD didn't supply one. Restricted to a known set of
            # language names to prevent false positives like the previous
            # `Language\s+(\w+)` matching "and" out of body text.
            if not details["language"]:
                lang_m = re.search(
                    r'Language\s+(English|Spanish|French|German|Italian|Portuguese|Dutch|'
                    r'Russian|Chinese|Japanese|Korean|Polish|Czech|Swedish|Norwegian|'
                    r'Danish|Finnish|Greek|Turkish|Arabic|Hebrew|Hindi|Thai|Vietnamese|'
                    r'Indonesian|Croatian|Serbian|Romanian|Hungarian|Bulgarian|Ukrainian|'
                    r'Catalan|Latin|Esperanto|Welsh|Irish|Gaelic|Slovak|Slovenian|'
                    r'Estonian|Latvian|Lithuanian|Icelandic|Albanian|Macedonian|Bosnian|'
                    r'Persian|Farsi|Urdu|Bengali|Tamil|Malay|Filipino|Tagalog|Swahili|'
                    r'Afrikaans)\b',
                    page_text,
                )
                if lang_m:
                    details["language"] = lang_m.group(1)

            # Fallback: check for "not yet published" text
            if "not yet published" in page_text.lower():
                details["is_unreleased"] = True

            # If pub_date is in the future, it's unreleased
            if details["pub_date"] and _is_future(details["pub_date"]):
                details["is_unreleased"] = True
                if not details["expected_date"]:
                    details["expected_date"] = details["pub_date"]
                details["pub_date"] = None

            # --- Series info from book page ---
            series_section = soup.find("div", {"data-testid": "seriesTitle"})
            series_text = ""
            if series_section:
                # Get only the text from series links, not page navigation
                series_links = series_section.find_all("a")
                if series_links:
                    parts = []
                    for sl in series_links:
                        parts.append(sl.get_text(strip=True))
                    series_text = ", ".join(parts)
                else:
                    series_text = series_section.get_text(strip=True)
                    # Truncate at any obvious page-chrome boundary
                    for boundary in ["|", "Goodreads", "Home", "My Books", "Browse"]:
                        idx = series_text.find(boundary)
                        if idx > 0:
                            series_text = series_text[:idx].strip()
                            break

            if series_text:
                # Check for set indicators: multiple series entries or range like #1-6
                if _is_set_from_series(series_text):
                    details["is_set"] = True

                # Count distinct series entries (sets are often in 2+ series)
                series_entries = [s.strip() for s in series_text.split(",") if s.strip()]
                real_series = [s for s in series_entries if not re.search(r'chronological|reading order|timeline', s, re.I)]
                if len(real_series) >= 2:
                    # In multiple real series = likely a set/omnibus
                    details["is_set"] = True

                # Extract primary series name and index (first non-chronological entry)
                for entry in series_entries:
                    if re.search(r'chronological|reading order|timeline', entry, re.I):
                        continue
                    sm = re.match(r'(.+?)\s*(?:\(|#)([\d.]+)\)?', entry)
                    if sm and not _is_set_from_series(entry):
                        details["series_name"] = sm.group(1).strip()
                        try:
                            details["series_index"] = float(sm.group(2))
                        except ValueError:
                            pass
                        break
                    elif not _is_set_from_series(entry):
                        # Series without index
                        sn = re.sub(r'\s*\(.*\)', '', entry).strip()
                        if sn:
                            details["series_name"] = sn
                        break

            # --- Description ---
            desc_el = soup.find("div", {"data-testid": "description"})
            if desc_el:
                # Get text from the expanded version if available
                spans = desc_el.find_all("span", class_=re.compile("Formatted"))
                if spans:
                    details["description"] = spans[-1].get_text(strip=True)[:500]
                else:
                    details["description"] = desc_el.get_text(strip=True)[:500]

        except Exception as e:
            logger.debug(f"  Goodreads: error getting details for book {book_id} '{title}': {e}")

        return details

    async def search_author(self, author_name: str) -> Optional[AuthorResult]:
        """Find an author's Goodreads ID via the book-search endpoint.

        Goodreads migrated `/search?search_type=authors` to client-side
        React rendering in early 2026. Scraping that page now yields an
        empty result shell (nav chrome + title tag only; author rows
        load via AJAX). We pivot to the book-search endpoint, which
        remains server-rendered:

          1. Query `/search?search_type=books&q=<name>`.
          2. Each result row carries an `a.authorName` anchor linking
             to `/author/show/{id}`. Count (name, id) pairs across the
             page; the author that wrote the books the query matched
             dominates the distribution.
          3. Gate the most-common pair through `authors_match` so we
             don't confidently return the wrong author when Goodreads'
             ranker returns adjacent-namespace results.

        If the first query returns no match (Goodreads' ranker is
        punctuation-sensitive — `"A K Duboff"` returns Amy DuBoff,
        `"A.K. Duboff"` returns A.K. DuBoff), retry with up to three
        punctuation variants from `author_name_variants`. Variant
        retries only fire when the first query already failed, so the
        common case stays one HTTP request.
        """
        for variant in author_name_variants(author_name):
            try:
                r = await self._get(
                    f"{BASE}/search",
                    params={"q": variant, "search_type": "books"},
                )
            except Exception as e:
                logger.error(f"Goodreads search error for variant '{variant}': {e}")
                continue

            result = _pick_author_from_book_search(r.text, author_name)
            if result is not None:
                if variant != author_name:
                    logger.info(
                        f"  Goodreads: matched '{author_name}' via variant '{variant}' "
                        f"→ '{result.name}' (id={result.external_id})"
                    )
                return result

        return None

    async def get_author_books(self, author_id: str, existing_titles: set = None, owned_titles: list = None, owned_only: bool = False, start_at: int = 0) -> Optional[AuthorResult]:
        """Scrape an author's full book list and visit per-book detail pages.

        Validates author identity from list-page titles BEFORE running
        any per-book fetches — if no owned/known title shows up on the
        author's list page at all, we're almost certainly looking at
        the wrong author and bail out cleanly.

        In `owned_only` mode (the "Library-only source scan" setting),
        books that don't match `existing_titles` get dropped by the
        merge layer downstream anyway, so we skip the per-book page
        visit for them up front. This saves ~2s per skipped book at
        the rate limit, which dominates total scan time for prolific
        authors.

        `start_at` is the v1.2 resume-from-position parameter. When
        the first call for an author times out mid-detail-loop, the
        caller saves `self._partial_state["index"]` and invokes the
        source a second time with `start_at=<index>`. The loop skips
        book entries with `i < start_at` and inherits the prior
        call's `books` / `series_map` snapshots so partial work
        isn't lost. `start_at=0` (the default) is a fresh call.
        """
        if existing_titles is None:
            existing_titles = set()
        if owned_titles is None:
            owned_titles = []
        # Resume from a prior partial state if the caller is retrying
        # this same author (matching author_id + start_at > 0). Prior
        # books/series are inherited so the returned AuthorResult from
        # the retry call covers 0..end, not just start_at..end.
        if start_at > 0 and self._partial_state and self._partial_state.get("author_id") == author_id:
            resume_books = list(self._partial_state["books"])
            resume_series_map = {
                sr.name: SeriesResult(name=sr.name, books=list(sr.books))
                for sr in self._partial_state["series"]
            }
            logger.info(
                f"  Goodreads: resuming author_id={author_id} from book "
                f"{start_at}/{self._partial_state.get('total', '?')} "
                f"with {len(resume_books) + sum(len(s.books) for s in resume_series_map.values())} prior books preserved"
            )
        else:
            resume_books = None
            resume_series_map = None
        try:
            r = await self._get(f"{BASE}/author/list/{author_id}", retries=2, params={"per_page": 100})
            soup = BeautifulSoup(r.text, "lxml")

            # Goodreads always 301-redirects /author/list/{id} →
            # /author/list/{id}.{Author_Slug}. httpx auto-follows so the
            # first request still works, but every pagination page
            # would also do its own 301→200 round-trip. Capturing the
            # resolved URL once and reusing its path for subsequent
            # pages cuts out one redirect per page — a 4-page Sanderson
            # list goes from 8 requests (4×301 + 4×200) to 5.
            list_path = str(r.url).split("?", 1)[0]

            nm_el = soup.select_one("a.authorName span")
            author_name = nm_el.get_text(strip=True) if nm_el else "Unknown"

            # Get author image
            author_img = None
            photo_el = soup.select_one("img.authorPhoto, img[alt*='author']")
            if photo_el:
                author_img = photo_el.get("src")
                if author_img and "nophoto" in author_img:
                    author_img = None

            # Pass 1: collect every book entry from the author list
            # pages (paginating through `?page=N` until exhausted).
            raw_books = []
            max_pages = 70  # Safety cap: 70 pages × 30 = ~2100 books max (Goodreads caps at 30/page)

            def _parse_book_rows(page_soup):
                """Parse book rows from a single author list page."""
                parsed = []
                page_rows = page_soup.select("tr[itemtype='http://schema.org/Book']")
                if not page_rows:
                    page_rows = page_soup.select("table.tableList tr")
                for row in page_rows:
                    title_el = row.select_one("a.bookTitle span") or row.select_one("a.bookTitle")
                    if not title_el:
                        continue
                    parsed.append(row)
                return parsed

            # Parse first page
            rows = _parse_book_rows(soup)

            # Check for additional pages and fetch them
            page_num = 1
            while page_num < max_pages:
                next_link = soup.select_one("a.next_page")
                if not next_link or not next_link.get("href"):
                    break
                page_num += 1
                logger.debug(f"  Goodreads: fetching author list page {page_num}...")
                try:
                    r = await self._get(list_path, retries=1,
                                        params={"per_page": 100, "page": page_num})
                    soup = BeautifulSoup(r.text, "lxml")
                    new_rows = _parse_book_rows(soup)
                    if not new_rows:
                        break
                    rows.extend(new_rows)
                except Exception as e:
                    logger.warning(f"  Goodreads: failed to fetch page {page_num}: {e}")
                    break

            if page_num > 1:
                logger.info(f"  Goodreads: fetched {page_num} pages of author books ({len(rows)} entries)")

            for row in rows:
                title_el = row.select_one("a.bookTitle span") or row.select_one("a.bookTitle")
                if not title_el:
                    continue
                full_title = title_el.get_text(strip=True)

                # Parse series from title
                sname = sidx = None
                sm = re.search(r'\(([^)]+),\s*#([\d.]+)\)', full_title)
                if sm:
                    sname = sm.group(1).strip()
                    try:
                        sidx = float(sm.group(2))
                    except ValueError:
                        pass
                    full_title = re.sub(r'\s*\([^)]+,\s*#[\d.]+\)', '', full_title).strip()

                # Get book ID
                title_link = row.select_one("a.bookTitle")
                book_id = None
                if title_link:
                    m = re.search(r"/book/show/(\d+)", title_link.get("href", ""))
                    if m:
                        book_id = m.group(1)

                # Get cover from list page (fallback)
                img = row.select_one("img.bookCover, img.bookSmallImg")
                cover = img.get("src") if img else None
                if cover:
                    if "_SX" in cover:
                        cover = re.sub(r'_SX\d+_', '_SX300_', cover)
                    elif "_SY" in cover:
                        cover = re.sub(r'_SY\d+_', '_SY400_', cover)

                # Quick check from list text for translator, contributor, or audiobook format
                row_text = row.get_text(" ", strip=True)
                row_text_lower = row_text.lower()
                has_translator = "(translator)" in row_text_lower
                is_contributor = "(contributor)" in row_text_lower
                is_audio_list = any(kw in row_text_lower for kw in [
                    "audible audio", "audio cd", "(narrator)", "audiobook",
                    "(read by)", "mp3 cd",
                ])

                raw_books.append({
                    "title": full_title, "book_id": book_id,
                    "list_series": sname, "list_series_idx": sidx,
                    "list_cover": cover, "has_translator": has_translator,
                    "is_contributor": is_contributor,
                    "is_audio_list": is_audio_list,
                })

            # Validate author using list page titles BEFORE visiting individual pages
            list_titles = [rb["title"] for rb in raw_books]

            def _title_match(a, b):
                """Quick fuzzy title match."""
                na = re.sub(r'[^\w\s]', '', a.lower()).strip()
                nb = re.sub(r'[^\w\s]', '', b.lower()).strip()
                return na == nb or na in nb or nb in na

            author_confirmed = False

            # Check 1: Do any owned book titles appear on the list page?
            if owned_titles:
                for ot in owned_titles:
                    if any(_title_match(ot, lt) for lt in list_titles):
                        author_confirmed = True
                        break

            # Check 2: Do any existing DB titles appear on the list page? (re-scan)
            if not author_confirmed and existing_titles:
                for lt in list_titles:
                    norm_lt = re.sub(r'[^\w\s]', '', lt.lower()).strip()
                    norm_lt = re.sub(r'\s+', ' ', norm_lt)
                    if any(norm_lt == et or norm_lt in et or et in norm_lt 
                           for et in existing_titles):
                        author_confirmed = True
                        break

            if not author_confirmed:
                if owned_titles:
                    logger.info(f"  Goodreads: author validation failed — none of {len(owned_titles)} owned titles match {len(list_titles)} list titles")
                    return None
                else:
                    # No owned titles to validate against (first sync before Calibre?) — proceed cautiously
                    logger.info(f"  Goodreads: no owned titles for validation, proceeding with {len(list_titles)} books")
                    author_confirmed = True

            logger.info(f"  Goodreads: author confirmed via title match")

            # Pass 2: visit each surviving book's detail page for the
            # full metadata Goodreads only exposes per book.
            total = len(raw_books)
            logger.info(f"  Goodreads: found {total} books on list page, fetching details...")

            # Inherit from a prior partial state on a resume call;
            # otherwise start fresh. See start_at docstring above.
            books = resume_books if resume_books is not None else []
            series_map = resume_series_map if resume_series_map is not None else {}
            skipped = {"foreign": 0, "set": 0, "translation": 0}

            for i, rb in enumerate(raw_books):
                if i < start_at:
                    continue
                if not rb["book_id"]:
                    continue

                # Quick skip: if list page already shows translator or contributor
                if rb["has_translator"]:
                    skipped["translation"] += 1
                    logger.debug(f"    SKIP (translator): '{rb['title']}'")
                    continue
                if rb.get("is_contributor"):
                    skipped.setdefault("contributor", 0)
                    skipped["contributor"] += 1
                    logger.debug(f"    SKIP (contributor): '{rb['title']}'")
                    continue
                if rb.get("is_audio_list"):
                    skipped.setdefault("audiobook", 0)
                    skipped["audiobook"] += 1
                    logger.debug(f"    SKIP (audiobook from list): '{rb['title']}'")
                    continue

                # Quick skip: title looks like a set/collection (no page visit needed)
                title_lower = rb["title"].lower()
                if any(kw in title_lower for kw in [
                    "box set", "boxed set", "boxset", "book set", "collection set",
                    "books collection", "hardcover collection", "paperback collection",
                    "complete series", "series set", "roleplaying game",
                ]) or re.search(r'series\s+#?\d+\s*[-–]\s*#?\d+', title_lower) or \
                   re.search(r'#\d+\s*[-–]\s*\d+', title_lower) or \
                   re.search(r'books?\s+\d+\s*[-–]\s*\d+', title_lower):
                    skipped["set"] += 1
                    logger.debug(f"    SKIP (set/collection title): '{rb['title']}'")
                    continue

                # Skip books already in DB (avoid unnecessary page visits)
                # But still emit a minimal result so the merge can backfill the URL
                if existing_titles:
                    norm_title = re.sub(r'[^\w\s]', '', rb["title"].lower()).strip()
                    norm_title = re.sub(r'\s+', ' ', norm_title)
                    if any(norm_title == et or norm_title in et or et in norm_title 
                           for et in existing_titles):
                        skipped.setdefault("known", 0)
                        skipped["known"] += 1
                        logger.debug(f"    SKIP (known, URL backfill): '{rb['title']}' → book/{rb['book_id']}")
                        # URL-backfill counts as "real work" for the
                        # per-book progress feed: we're emitting a
                        # BookResult that the merge layer consumes.
                        # Filter-noise skips ABOVE this point
                        # (translator / contributor / set) deliberately
                        # do NOT call _on_book — the user-visible feed
                        # only ticks through productive work.
                        on_book = getattr(self, '_on_book', None)
                        if on_book:
                            on_book(rb["title"])
                        # Emit minimal result for URL backfill (no page visit needed)
                        sname = rb["list_series"]
                        br = BookResult(
                            title=rb["title"],
                            series_name=sname,
                            series_index=rb["list_series_idx"],
                            external_id=rb["book_id"],
                            source="goodreads",
                            source_url=f"https://www.goodreads.com/book/show/{rb['book_id']}",
                        )
                        if sname:
                            if sname not in series_map:
                                series_map[sname] = SeriesResult(name=sname, books=[])
                            series_map[sname].books.append(br)
                        else:
                            books.append(br)
                        continue

                # owned_only optimization: skip detail fetches for
                # books that won't survive the merge layer in
                # library-only mode (see the docstring for context).
                #
                # IMPORTANT: in full_scan mode, lookup.py deliberately
                # passes `existing_titles=set()` so the URL-backfill
                # branch above doesn't fire and we revisit pages for
                # fresh metadata. We CANNOT trust existing_titles to
                # tell us which books are owned in that mode — we have
                # to consult `owned_titles` (passed separately) here.
                # Without this check, full_scan + owned_only would
                # skip ALL books including owned ones — once produced
                # 0 merged books on a real Sanderson scan while
                # Hardcover correctly merged 16.
                if owned_only:
                    if not (owned_titles and any(_title_match(ot, rb["title"]) for ot in owned_titles)):
                        skipped.setdefault("unowned", 0)
                        skipped["unowned"] += 1
                        logger.debug(f"    SKIP-UNOWNED (library-only): '{rb['title']}'")
                        continue

                # Log progress every 10 books
                if (i + 1) % 10 == 0 or i == 0:
                    logger.info(f"  Goodreads: checking book {i+1}/{total}...")

                # DETAIL fetch path — the slow one (HTTP + parse).
                # Emit per-book progress so the user sees the widget
                # tick through real work.
                on_book = getattr(self, '_on_book', None)
                if on_book:
                    on_book(rb["title"])
                # And bump the new-candidate counter so the
                # `new_books` count climbs in real time during the
                # rate-limited fetch instead of waiting for merge.
                # Only fired here on the DETAIL fetch path (NOT the
                # URL-backfill path above), so already-known books
                # don't inflate the count.
                on_new_candidate = getattr(self, '_on_new_candidate', None)
                if on_new_candidate:
                    on_new_candidate()

                details = await self._get_book_details(rb["book_id"], rb["title"])
                logger.debug(f"    PAGE: '{rb['title']}' → lang={details.get('language')}, set={details.get('is_set')}, trans={details.get('is_translation')}, audio={details.get('is_audiobook')}, series={details.get('series_name')}, date={details.get('pub_date') or details.get('expected_date')}")

                # Filter: language
                lang = (details.get("language") or "").lower()
                if lang and lang not in ("english", "en", "eng", ""):
                    skipped["foreign"] += 1
                    logger.debug(f"    SKIP (foreign language '{lang}'): '{rb['title']}'")
                    continue

                # Filter: translation (detected from book page)
                if details.get("is_translation") and lang and lang != "english":
                    skipped["translation"] += 1
                    logger.debug(f"    SKIP (translation): '{rb['title']}'")
                    continue

                # Filter: box set / collection
                if details.get("is_set"):
                    skipped["set"] += 1
                    logger.debug(f"    SKIP (set/collection from page): '{rb['title']}'")
                    continue

                # Filter: audiobook edition (from JSON-LD bookFormat)
                if details.get("is_audiobook"):
                    skipped.setdefault("audiobook", 0)
                    skipped["audiobook"] += 1
                    logger.debug(f"    SKIP (audiobook format): '{rb['title']}'")
                    continue

                # Build the BookResult
                sname = details.get("series_name") or rb["list_series"]
                sidx = details.get("series_index") or rb["list_series_idx"]
                cover = details.get("cover_url") or rb["list_cover"]

                br = BookResult(
                    title=rb["title"],
                    series_name=sname,
                    series_index=sidx,
                    cover_url=cover,
                    pub_date=details["pub_date"] if not details["is_unreleased"] else None,
                    expected_date=details.get("expected_date"),
                    is_unreleased=details.get("is_unreleased", False),
                    description=details.get("description"),
                    page_count=details.get("page_count"),
                    external_id=rb["book_id"],
                    source="goodreads",
                    source_url=f"https://www.goodreads.com/book/show/{rb['book_id']}",
                    language=details.get("language") or "English",
                )

                if sname:
                    if sname not in series_map:
                        series_map[sname] = SeriesResult(name=sname, books=[])
                    series_map[sname].books.append(br)
                    logger.debug(f"    INCLUDE: '{rb['title']}' → series '{sname}' #{sidx}")
                else:
                    books.append(br)
                    logger.debug(f"    INCLUDE: '{rb['title']}' → standalone")

                # Resume-point snapshot. Updated at the END of each
                # iteration so a CancelledError at the NEXT iteration's
                # first await preserves a consistent state: index =
                # next-book-to-process, books/series = what's been
                # committed through book i. Snapshot is a shallow-ish
                # copy of the lists + a rebuilt series list so mutating
                # the live series_map[sname].books in a later iteration
                # doesn't retroactively alter the saved snapshot.
                self._partial_state = {
                    "author_id": author_id,
                    "books": list(books),
                    "series": [
                        SeriesResult(name=s.name, books=list(s.books))
                        for s in series_map.values()
                    ],
                    "index": i + 1,
                    "total": total,
                }

            if any(skipped.values()):
                parts = []
                if skipped.get("known"): parts.append(f"{skipped['known']} already known")
                if skipped.get("foreign"): parts.append(f"{skipped['foreign']} foreign")
                if skipped.get("set"): parts.append(f"{skipped['set']} sets")
                if skipped.get("translation"): parts.append(f"{skipped['translation']} translations")
                if skipped.get("contributor"): parts.append(f"{skipped['contributor']} contributor-only")
                if skipped.get("unowned"): parts.append(f"{skipped['unowned']} unowned (library-only)")
                logger.info(f"  Goodreads: skipped {', '.join(parts)}")

            # Normal completion — clear any partial state from a prior
            # iteration or resume. Leaving it set would cause the NEXT
            # author's scan to accidentally think it's resuming if it
            # happened to pass a non-zero start_at.
            self._partial_state = None
            return AuthorResult(
                name=author_name, external_id=author_id, image_url=author_img,
                books=books, series=list(series_map.values()),
            )
        except asyncio.CancelledError:
            # Caller's wait_for timed out. Preserve `_partial_state` so
            # the caller can inspect it and optionally retry with
            # start_at=state["index"].
            raise
        except Exception as e:
            logger.error(f"Goodreads author error id={author_id}: {type(e).__name__}: {e}")
            # Non-cancellation failure — clear state so a later
            # unrelated scan doesn't resume from a dead context.
            self._partial_state = None
            return None
