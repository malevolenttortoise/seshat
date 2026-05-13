"""
Universal URL-paste import (v2.11.0 Stage 4.5).

Compensating UX for the discovery gaps that emerge when sources go
into soft-block or transient-503 mode (Amazon CAPTCHA mid-batch,
Google Books backend hiccups, etc.). User pastes a book URL from
any supported source — Seshat extracts the ID, fetches metadata
directly from that source's by-id endpoint, and produces a unified
record the import flow can consume.

Module split (matches v2.11.0 plan):
  - `parse_url(url) -> (source_name, external_id)` — pure pattern-matching,
    no I/O. Returns None on unrecognized URL.
  - `fetch_by_url(url) -> dict` — dispatcher; takes a URL, parses it,
    routes to the appropriate per-source fetcher. Returns the same
    flat dict shape the existing `_fetch_goodreads_book` /
    `_fetch_hardcover_book` helpers in `import_export.py` return,
    so the existing router code can stay unchanged.

Per-source fetchers live in `import_export.py` (existing GR/HC) or
this module (new for Amazon / OL / GB / Kobo / IBDB).

Soft-block / 503 handling: each fetcher raises HTTPException with
a clear message pointing the user at a different source. The
dispatcher passes those through unchanged.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx
from fastapi import HTTPException

logger = logging.getLogger("seshat.discovery.url_import")


# ─── URL pattern → (source, id-extractor) registry ──────────────────

# Order matters: earlier patterns win on overlap. List goes from
# most-specific URL shape to least so a Goodreads link doesn't get
# misread as a vague catch-all.
_URL_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Goodreads — only `/book/show/{id}` (existing v2.10.4 endpoint).
    # `/search` is robots-disallowed and not supported here.
    ("goodreads", re.compile(r"goodreads\.com/book/show/(\d+)")),

    # Hardcover — `/books/{slug}` (existing v2.10.x endpoint).
    ("hardcover", re.compile(r"hardcover\.app/books/([a-zA-Z0-9_-]+)")),

    # Amazon — `/dp/{ASIN}` or `/gp/product/{ASIN}`. ASINs are
    # 10-char alphanumeric, ISBN-10 is a subset. Handle all locale
    # TLDs (.com / .co.uk / .co.jp / .ca / .de / .fr / .it / .es).
    ("amazon", re.compile(
        r"amazon\.(?:com|co\.uk|co\.jp|ca|de|fr|it|es|com\.au|com\.mx|com\.br|in|nl|pl|sg|ae|sa|se|eg|tr)"
        r"/(?:.*?/)?(?:dp|gp/product)/([A-Z0-9]{10})"
    )),

    # Open Library — three shapes: works (canonical work), books
    # (specific edition), ISBN (lookup-by-ISBN). All routed to OL
    # but with different fetchers per shape.
    ("openlibrary_work", re.compile(r"openlibrary\.org/works/(OL\d+W)")),
    ("openlibrary_book", re.compile(r"openlibrary\.org/books/(OL\d+M)")),
    ("openlibrary_isbn", re.compile(r"openlibrary\.org/isbn/([0-9X-]+)")),

    # Google Books — `/books?id={vol_id}` or `/books/about/{slug}?id={vol_id}`.
    # The vol_id is what GoogleBooks API actually keys on; the slug
    # is decorative. Capture vol_id regardless of slug position.
    ("google_books", re.compile(
        r"books\.google\.(?:com|[a-z]{2,3}(?:\.[a-z]{2,3})?)/books"
        r"\?(?:.*?&)?id=([A-Za-z0-9_-]+)"
    )),

    # Kobo — `/{lang-region}/ebook/{slug}` or `/{lang-region}/audiobook/{slug}`.
    # Locale is variable (us/en, ca/en, de/de, etc.). Capture the slug.
    ("kobo", re.compile(
        r"kobo\.com/[a-z-]+/[a-z]+/(?:ebook|audiobook)/([a-zA-Z0-9_-]+)"
    )),

    # IBDB — `/book/{uuid}` on ibdb.dev. UUID v4 shape.
    ("ibdb", re.compile(
        r"ibdb\.dev/book/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    )),
]


def parse_url(url: str) -> Optional[tuple[str, str]]:
    """Parse a book URL into (source_name, external_id).

    Returns None if the URL doesn't match any known pattern. The
    source_name is one of: goodreads, hardcover, amazon,
    openlibrary_work, openlibrary_book, openlibrary_isbn,
    google_books, kobo, ibdb.

    Pure function — no I/O, no exceptions. Callers handle the
    None-return case as "unrecognized URL".
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    for source, pattern in _URL_PATTERNS:
        m = pattern.search(url)
        if m:
            return (source, m.group(1))
    return None


# ─── Per-source by-id fetchers (return the flat-dict shape that the
#     existing import_export.py routers consume) ──────────────────────


_FIREFOX_UA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
)


async def fetch_openlibrary_isbn(isbn: str) -> dict:
    """Fetch an Open Library book by ISBN via `/api/books?bibkeys=`.

    OL's bibkeys endpoint is the most precise lookup — ISBN is an
    exact key. Returns title, authors, publisher, cover, description
    (when available), and the source_url pointing at the canonical
    OL book page.
    """
    normalized = isbn.replace("-", "").strip()
    if not normalized:
        raise HTTPException(400, "ISBN required")
    params = {
        "bibkeys": f"ISBN:{normalized}",
        "jscmd": "data",
        "format": "json",
    }
    headers = {"Accept": "application/json", "User-Agent": _FIREFOX_UA}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        r = await client.get("https://openlibrary.org/api/books", params=params)
        r.raise_for_status()
    data = r.json()
    payload = data.get(f"ISBN:{normalized}")
    if not payload:
        raise HTTPException(404, f"No Open Library record for ISBN {normalized}")

    title = (payload.get("title") or "").strip()
    authors_raw = payload.get("authors") or []
    author_name = ""
    for a in authors_raw:
        if isinstance(a, dict) and a.get("name"):
            author_name = a["name"]
            break

    # Cover — `cover` is a dict of size→url; prefer "large".
    cover_url: Optional[str] = None
    covers = payload.get("cover") or {}
    if isinstance(covers, dict):
        cover_url = covers.get("large") or covers.get("medium") or covers.get("small")

    publishers_raw = payload.get("publishers") or []
    publisher: Optional[str] = None
    for p in publishers_raw:
        if isinstance(p, dict) and p.get("name"):
            publisher = p["name"]
            break

    pub_date = payload.get("publish_date") or None
    page_count = payload.get("number_of_pages")
    if not isinstance(page_count, int) or page_count <= 0:
        page_count = None

    description: Optional[str] = None
    excerpts = payload.get("excerpts") or []
    for ex in excerpts:
        if isinstance(ex, dict) and isinstance(ex.get("text"), str):
            description = ex["text"].strip() or None
            if description:
                break

    canonical_url = payload.get("url") or f"https://openlibrary.org/isbn/{normalized}"
    return {
        "source": "openlibrary",
        "source_url": json.dumps({"openlibrary": canonical_url}),
        "title": title,
        "author_name": author_name,
        "description": (description or "")[:1000],
        "isbn": normalized,
        "pub_date": pub_date,
        "cover_url": cover_url,
        "publisher": publisher,
        "page_count": page_count,
        "series_name": None,
        "series_index": None,
        # external_id stored on the OL column when row is created
        "openlibrary_id": None,  # bibkeys path doesn't give us a work-key
    }


async def fetch_openlibrary_work(work_key: str) -> dict:
    """Fetch an Open Library work by work-key (e.g. OL15161W).

    Two-step:
      1. `/works/{key}.json` for the canonical title + description
         + work-level cover.
      2. `/works/{key}/editions.json?limit=1` to get a first-edition
         ISBN + publisher + pub_date.
    """
    work_key = work_key.strip()
    if not work_key:
        raise HTTPException(400, "Work key required")
    headers = {"Accept": "application/json", "User-Agent": _FIREFOX_UA}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        work_resp = await client.get(f"https://openlibrary.org/works/{work_key}.json")
        if work_resp.status_code == 404:
            raise HTTPException(404, f"No Open Library work {work_key}")
        work_resp.raise_for_status()
        work = work_resp.json()

        editions_resp = await client.get(
            f"https://openlibrary.org/works/{work_key}/editions.json",
            params={"limit": 1},
        )
    title = (work.get("title") or "").strip()

    # Description can be a string or {value: str} dict
    description: Optional[str] = None
    desc = work.get("description")
    if isinstance(desc, dict):
        description = desc.get("value")
    elif isinstance(desc, str):
        description = desc

    # Cover from work-level `covers` array
    cover_url: Optional[str] = None
    covers = work.get("covers")
    if isinstance(covers, list) and covers:
        first = covers[0]
        if isinstance(first, int) and first > 0:
            cover_url = f"https://covers.openlibrary.org/b/id/{first}-L.jpg"

    # Authors — fetched separately via author key references
    author_name = ""
    for a in (work.get("authors") or []):
        if isinstance(a, dict):
            akey = (a.get("author") or {}).get("key", "")
            if akey:
                try:
                    async with httpx.AsyncClient(timeout=10, headers=headers) as ac:
                        ar = await ac.get(f"https://openlibrary.org{akey}.json")
                        if ar.status_code == 200:
                            author_name = (ar.json().get("name") or "").strip()
                            if author_name:
                                break
                except Exception:
                    pass

    # Pull ISBN + publisher + pub_date from the first edition if available
    isbn = None
    publisher = None
    pub_date = None
    page_count = None
    try:
        editions_resp.raise_for_status()
        editions = editions_resp.json().get("entries") or []
        if editions:
            ed = editions[0]
            isbns = (ed.get("isbn_13") or []) + (ed.get("isbn_10") or [])
            isbn = isbns[0] if isbns else None
            pubs = ed.get("publishers") or []
            publisher = pubs[0] if pubs else None
            pub_date = ed.get("publish_date") or None
            pc = ed.get("number_of_pages")
            if isinstance(pc, int) and pc > 0:
                page_count = pc
    except Exception:
        pass

    return {
        "source": "openlibrary",
        "source_url": json.dumps({
            "openlibrary": f"https://openlibrary.org/works/{work_key}",
        }),
        "title": title,
        "author_name": author_name,
        "description": (description or "")[:1000],
        "isbn": isbn,
        "pub_date": pub_date,
        "cover_url": cover_url,
        "publisher": publisher,
        "page_count": page_count,
        "series_name": None,
        "series_index": None,
        "openlibrary_id": work_key,
    }


async def fetch_google_books_volume(volume_id: str) -> dict:
    """Fetch a Google Books volume by ID (e.g. ?id=foo from a books.google URL).

    Direct lookup — no search/score. The Google Books volumes endpoint
    `https://www.googleapis.com/books/v1/volumes/{id}` returns the
    full volumeInfo for that record.
    """
    volume_id = volume_id.strip()
    if not volume_id:
        raise HTTPException(400, "Volume ID required")

    from app.config import load_settings
    api_key = (load_settings().get("google_books_api_key") or "").strip()

    params = {}
    if api_key:
        params["key"] = api_key
    headers = {"Accept": "application/json", "User-Agent": _FIREFOX_UA}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        r = await client.get(
            f"https://www.googleapis.com/books/v1/volumes/{volume_id}",
            params=params,
        )
        if r.status_code == 404:
            raise HTTPException(404, f"No Google Books volume {volume_id}")
        if r.status_code == 503:
            raise HTTPException(
                503,
                "Google Books is temporarily unavailable (503). "
                "Try again in a few minutes, or paste a different source URL.",
            )
        r.raise_for_status()
    vi = (r.json().get("volumeInfo") or {})

    # ISBN: prefer ISBN_13
    isbn = None
    for ident in vi.get("industryIdentifiers", []):
        if ident.get("type") == "ISBN_13":
            isbn = ident["identifier"]
            break
        if ident.get("type") == "ISBN_10" and not isbn:
            isbn = ident["identifier"]

    # Cover — use Google Books image links; upgrade to zoom=0 for max quality
    cover_url: Optional[str] = None
    images = vi.get("imageLinks", {})
    for key in ("large", "medium", "small", "thumbnail", "smallThumbnail"):
        if images.get(key):
            cover_url = re.sub(r"zoom=\d", "zoom=0", images[key])
            cover_url = re.sub(r"&edge=curl", "", cover_url)
            cover_url = cover_url.replace("http://", "https://")
            break

    # Description: strip HTML tags
    description = re.sub(r"<[^>]+>", "", vi.get("description") or "").strip()

    authors = vi.get("authors") or []
    author_name = authors[0] if authors else ""

    return {
        "source": "google_books",
        "source_url": json.dumps({
            "google_books": vi.get("infoLink")
                or vi.get("canonicalVolumeLink")
                or f"https://books.google.com/books?id={volume_id}",
        }),
        "title": vi.get("title", "").strip(),
        "author_name": author_name,
        "description": description[:1000],
        "isbn": isbn,
        "pub_date": vi.get("publishedDate"),
        "cover_url": cover_url,
        "publisher": vi.get("publisher"),
        "page_count": vi.get("pageCount"),
        "series_name": None,
        "series_index": None,
        "google_books_id": volume_id,
    }


async def fetch_ibdb_book(uuid: str) -> dict:
    """Fetch an IBDB book by UUID.

    IBDB's `/api/book/{uuid}` returns the single book record directly.
    """
    uuid = uuid.strip()
    if not uuid:
        raise HTTPException(400, "Book UUID required")
    headers = {"Accept": "application/json", "User-Agent": _FIREFOX_UA}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        r = await client.get(f"https://ibdb.dev/api/book/{uuid}")
        if r.status_code == 404:
            raise HTTPException(404, f"No IBDB book {uuid}")
        r.raise_for_status()
        data = r.json()

    # IBDB wraps the response — handle both direct and nested shapes
    book = data.get("book") or data
    if not isinstance(book, dict):
        raise HTTPException(502, "Unexpected IBDB response shape")

    authors_raw = book.get("authors") or []
    author_name = ""
    if isinstance(authors_raw, list):
        for a in authors_raw:
            if isinstance(a, dict) and a.get("name"):
                author_name = a["name"]
                break
            if isinstance(a, str):
                author_name = a
                break

    cover_raw = book.get("image") or book.get("cover")
    cover_url: Optional[str] = None
    if isinstance(cover_raw, dict):
        cover_url = cover_raw.get("url")
    elif isinstance(cover_raw, str):
        cover_url = cover_raw

    return {
        "source": "ibdb",
        "source_url": json.dumps({"ibdb": f"https://ibdb.dev/book/{uuid}"}),
        "title": (book.get("title") or book.get("name") or "").strip(),
        "author_name": author_name,
        "description": (book.get("synopsis") or book.get("description") or "")[:1000],
        "isbn": book.get("isbn13") or book.get("isbn_13") or book.get("isbn"),
        "pub_date": book.get("publicationDate") or book.get("publish_date"),
        "cover_url": cover_url,
        "publisher": None,
        "page_count": book.get("pageCount") or book.get("pages"),
        "series_name": None,
        "series_index": None,
        "ibdb_id": uuid,
    }


async def fetch_amazon_book(asin: str) -> dict:
    """Fetch an Amazon book by ASIN via the product detail page.

    Scrapes `amazon.com/dp/{asin}`. Amazon's bot-detection means
    sustained sequential calls trigger CAPTCHA / Robot Check;
    per-book volume (one-shot URL paste) is generally fine because
    the request density stays low.

    Surfaces clear errors when the response is a CAPTCHA / sub-50KB
    thin body (the v2.10.8 soft-block indicators).
    """
    asin = asin.strip().upper()
    if not asin or len(asin) != 10:
        raise HTTPException(400, f"Invalid ASIN: {asin}")

    headers = {
        "User-Agent": _FIREFOX_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    async with httpx.AsyncClient(
        timeout=30, headers=headers, follow_redirects=True,
    ) as client:
        r = await client.get(f"https://www.amazon.com/dp/{asin}")
        if r.status_code == 503:
            raise HTTPException(
                503,
                "Amazon is rate-limiting us (HTTP 503). Try again in "
                "a few minutes, or paste a different source URL.",
            )
        r.raise_for_status()
        html_text = r.text

    # Soft-block detection: real product pages are 100KB+; CAPTCHA
    # / Robot Check pages are sub-50KB and contain specific strings.
    if len(html_text) < 50_000 or (
        "Enter the characters you see below" in html_text
        or "/errors/validateCaptcha" in html_text
        or "<title>Robot Check</title>" in html_text
    ):
        raise HTTPException(
            503,
            "Amazon presented a CAPTCHA / robot-check page. Try again "
            "in a few minutes, or paste a Hardcover / Open Library URL "
            "for this book instead.",
        )

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise HTTPException(500, "BeautifulSoup not available for Amazon parse")

    soup = BeautifulSoup(html_text, "lxml")

    # Title — productTitle is the canonical span
    title_el = soup.select_one("#productTitle") or soup.select_one("h1")
    title = title_el.get_text(strip=True) if title_el else ""

    # Author — first byline contributor with role "Author"
    author_name = ""
    byline = soup.select_one("#bylineInfo, .author") or soup
    for a in byline.select("a.a-link-normal"):
        text = a.get_text(strip=True)
        if text and "Author" in (a.parent.get_text() if a.parent else ""):
            author_name = text
            break
    if not author_name:
        # Fallback: first author-styled anchor anywhere on page
        first_author = soup.select_one(".author a, .contributorNameID")
        if first_author:
            author_name = first_author.get_text(strip=True)

    # Cover — landingImage data-old-hires has the full-res URL
    cover_url = None
    img = soup.select_one("#landingImage, #imgBlkFront")
    if img:
        cover_url = img.get("data-old-hires") or img.get("src")

    # Description — bookDescription_feature_div / iframe noscript
    description = ""
    desc_el = soup.select_one("#bookDescription_feature_div .a-expander-content, #productDescription")
    if desc_el:
        description = desc_el.get_text(separator=" ", strip=True)[:1000]

    # ISBN / publisher / pub_date from carousel attribute cards
    isbn = None
    publisher = None
    pub_date = None
    pages = None
    for li in soup.select(".rpi-carousel-attribute-card, #detailBullets_feature_div li"):
        label_el = li.select_one(".rpi-attribute-label, .a-text-bold")
        value_el = li.select_one(".rpi-attribute-value, span:not(.a-text-bold)")
        if not (label_el and value_el):
            continue
        label = label_el.get_text(strip=True).lower().rstrip(":")
        value = value_el.get_text(strip=True)
        if "isbn-13" in label or "isbn-10" in label:
            isbn = value.replace("-", "")
        elif "publisher" in label:
            publisher = value.split(";")[0].strip()
        elif "publication date" in label or "publication" in label:
            pub_date = value
        elif "print length" in label or "paperback" in label or "hardcover" in label:
            m = re.search(r"(\d+)\s*pages", value, re.IGNORECASE)
            if m:
                try:
                    pages = int(m.group(1))
                except ValueError:
                    pass

    return {
        "source": "amazon",
        "source_url": json.dumps({"amazon": f"https://www.amazon.com/dp/{asin}"}),
        "title": title,
        "author_name": author_name,
        "description": description,
        "isbn": isbn,
        "pub_date": pub_date,
        "cover_url": cover_url,
        "publisher": publisher,
        "page_count": pages,
        "series_name": None,
        "series_index": None,
        "amazon_id": asin,
    }


async def fetch_kobo_book(slug: str) -> dict:
    """Fetch a Kobo book by storefront slug.

    Reuses `KoboSource._get_book_details()` which already knows how
    to drive cloudscraper + parse Kobo's detail-page selectors.
    """
    slug = slug.strip()
    if not slug:
        raise HTTPException(400, "Kobo slug required")

    from app.discovery.sources.kobo import KoboSource

    src = KoboSource(rate_limit=0)  # No need to throttle a one-shot lookup
    try:
        url = f"https://www.kobo.com/us/en/ebook/{slug}"
        details = await src._get_book_details(url)
        if not details.get("title"):
            raise HTTPException(
                404,
                f"No Kobo book at slug {slug!r} (or Cloudflare blocked "
                "the fetch). Try a different source URL.",
            )
        return {
            "source": "kobo",
            "source_url": json.dumps({"kobo": url}),
            "title": details.get("title") or "",
            "author_name": "",  # Kobo detail page doesn't reliably expose author in the dict
            "description": (details.get("description") or "")[:1000],
            "isbn": details.get("isbn"),
            "pub_date": details.get("pub_date"),
            "cover_url": details.get("cover_url"),
            "publisher": details.get("publisher"),
            "page_count": details.get("page_count"),
            "series_name": details.get("series_name"),
            "series_index": details.get("series_index"),
            "kobo_id": slug,
        }
    finally:
        await src.close()


# ─── Dispatcher ────────────────────────────────────────────────────


async def fetch_by_url(url: str) -> dict:
    """Parse a URL and dispatch to the matching source's by-id fetcher.

    Returns the flat-dict shape that the existing `_fetch_goodreads_book`
    / `_fetch_hardcover_book` helpers in `import_export.py` return.

    Raises HTTPException(400) on unrecognized URL, and propagates any
    source-side HTTPExceptions (404 not-found, 503 soft-block, etc.).

    The Goodreads + Hardcover branches delegate to the existing helpers
    in `import_export.py` so we don't break the v2.10.4 contract; the
    new branches (amazon, openlibrary*, google_books, kobo, ibdb) use
    the fetchers defined in this module.
    """
    parsed = parse_url(url)
    if parsed is None:
        raise HTTPException(
            400,
            "Unrecognized book URL. Supported sources: Goodreads "
            "(/book/show/{id}), Hardcover (/books/{slug}), Amazon "
            "(/dp/{ASIN}), Open Library (/works /books /isbn), "
            "Google Books (/books?id=...), Kobo (/ebook/{slug}), "
            "IBDB (/book/{uuid}).",
        )
    source, ext_id = parsed
    logger.debug("url_import: routing url=%r → source=%r id=%r", url, source, ext_id)

    if source == "goodreads":
        from app.discovery.routers.import_export import _fetch_goodreads_book
        return await _fetch_goodreads_book(ext_id)
    if source == "hardcover":
        from app.discovery.routers.import_export import _fetch_hardcover_book
        return await _fetch_hardcover_book(ext_id)
    if source == "amazon":
        return await fetch_amazon_book(ext_id)
    if source == "openlibrary_work":
        return await fetch_openlibrary_work(ext_id)
    if source == "openlibrary_book":
        # `/books/{OLnM}` — edition page. Resolve to work via redirect.
        # For now, treat it the same as openlibrary_work but with the
        # edition key. Edition pages on OL render the same metadata.
        # (Future refinement: separate edition vs work code path.)
        return await fetch_openlibrary_isbn(ext_id) if ext_id.isdigit() else await _fetch_openlibrary_edition(ext_id)
    if source == "openlibrary_isbn":
        return await fetch_openlibrary_isbn(ext_id)
    if source == "google_books":
        return await fetch_google_books_volume(ext_id)
    if source == "kobo":
        return await fetch_kobo_book(ext_id)
    if source == "ibdb":
        return await fetch_ibdb_book(ext_id)

    # Defensive — shouldn't reach here if the registry stays in sync
    raise HTTPException(500, f"Source {source!r} parsed but no fetcher wired")


async def _fetch_openlibrary_edition(edition_key: str) -> dict:
    """Fetch an OL edition by edition-key (e.g. OL24230520M).

    Edition pages have the same data shape as the bibkeys endpoint
    when keyed by OLID. Internal helper, not exported.
    """
    headers = {"Accept": "application/json", "User-Agent": _FIREFOX_UA}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        r = await client.get(
            "https://openlibrary.org/api/books",
            params={
                "bibkeys": f"OLID:{edition_key}",
                "jscmd": "data",
                "format": "json",
            },
        )
        r.raise_for_status()
    data = r.json()
    payload = data.get(f"OLID:{edition_key}")
    if not payload:
        raise HTTPException(404, f"No Open Library edition {edition_key}")

    # Reuse the bibkeys shape parsing by faking the ISBN slot
    title = (payload.get("title") or "").strip()
    authors_raw = payload.get("authors") or []
    author_name = ""
    for a in authors_raw:
        if isinstance(a, dict) and a.get("name"):
            author_name = a["name"]
            break

    cover_url = None
    covers = payload.get("cover") or {}
    if isinstance(covers, dict):
        cover_url = covers.get("large") or covers.get("medium") or covers.get("small")

    publishers_raw = payload.get("publishers") or []
    publisher = None
    for p in publishers_raw:
        if isinstance(p, dict) and p.get("name"):
            publisher = p["name"]
            break

    isbns_raw = (payload.get("identifiers") or {}).get("isbn_13") or \
                (payload.get("identifiers") or {}).get("isbn_10") or []
    isbn = isbns_raw[0] if isbns_raw else None

    return {
        "source": "openlibrary",
        "source_url": json.dumps({
            "openlibrary": payload.get("url")
                or f"https://openlibrary.org/books/{edition_key}",
        }),
        "title": title,
        "author_name": author_name,
        "description": "",
        "isbn": isbn,
        "pub_date": payload.get("publish_date"),
        "cover_url": cover_url,
        "publisher": publisher,
        "page_count": payload.get("number_of_pages") or None,
        "series_name": None,
        "series_index": None,
        "openlibrary_id": edition_key,
    }
