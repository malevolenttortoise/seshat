"""
Import / Export endpoints.

Two flows live here:

  - **Export**: dumps the active library to a JSON snapshot the user
    can download from the Settings page. Includes books, authors,
    series, suggestions — everything except the on-disk cover files
    (which are owned by Calibre, not us).
  - **Import**: lets the user paste a Goodreads or Hardcover URL,
    pre-fetches the metadata for review (preview), and on confirmation
    upserts the result as a new book in the active library. The
    fetch helpers in this file talk to the same source modules that
    the regular author scans use, so manual additions get the same
    series/language detection.

Endpoints:
  GET  /api/export
  POST /api/books/search-url
  POST /api/books/import-preview
  POST /api/books/import-add
"""
import asyncio
import json
import logging
import re
from datetime import datetime
from difflib import SequenceMatcher
import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import Response

from app.config import load_settings
from app.discovery.database import get_db, HF

logger = logging.getLogger("seshat.discovery")

router = APIRouter(prefix="/api/discovery", tags=["import_export"])


@router.get("/export")
async def export_books(filter: str = Query("missing"), format: str = Query("csv")):
    """Export books as CSV or text. filter: all|library|missing. format: csv|text."""
    db = await get_db()
    try:
        c = [HF]; p = []
        if filter == "library": c.append("b.owned=1")
        elif filter == "missing": c.append("b.owned=0")
        w = " AND ".join(c)
        rows = await (await db.execute(
            f"SELECT b.title, a.name as author_name, b.pub_date, b.expected_date, b.source, b.source_url, b.is_unreleased, b.mam_status, b.mam_url, b.mam_formats "
            f"FROM books b JOIN authors a ON b.author_id=a.id WHERE {w} ORDER BY a.sort_name, b.title", p
        )).fetchall()

        # Priority order for "best" URL
        url_priority = ["goodreads", "hardcover", "kobo", "amazon", "ibdb", "google_books"]

        def _best_url(source_url_json):
            """Extract the best URL and its source name from JSON."""
            if not source_url_json:
                return "", ""
            try:
                urls = json.loads(source_url_json)
                if not isinstance(urls, dict):
                    return "", ""
                for src in url_priority:
                    if src in urls:
                        return src, urls[src]
                # Return first available if none match priority
                for src, url in urls.items():
                    return src, url
            except (ValueError, TypeError):
                pass
            return "", ""

        if format == "csv":
            import csv, io
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["Title", "Author", "Release Date", "Source", "Source URL", "MAM Status", "MAM URL", "MAM Formats"])
            for r in rows:
                src_name, src_url = _best_url(r["source_url"])
                date = r["pub_date"] or r["expected_date"] or ""
                if r["is_unreleased"] and r["expected_date"]:
                    date = f"{r['expected_date']} (upcoming)"
                mam_status = r["mam_status"] or ""
                mam_url = r["mam_url"] or ""
                mam_formats = r["mam_formats"] or ""
                writer.writerow([r["title"], r["author_name"], date, src_name or r["source"] or "", src_url, mam_status, mam_url, mam_formats])
            content = buf.getvalue()
            return Response(content=content, media_type="text/csv",
                          headers={"Content-Disposition": f"attachment; filename=books_{filter}.csv"})
        else:
            lines = ["Title, Author, Release Date, Source, Source URL, MAM Status, MAM URL, MAM Formats"]
            for r in rows:
                src_name, src_url = _best_url(r["source_url"])
                date = r["pub_date"] or r["expected_date"] or ""
                if r["is_unreleased"] and r["expected_date"]:
                    date = f"{r['expected_date']} (upcoming)"
                # Escape commas in titles/authors
                title = r["title"].replace(",", ";")
                author = r["author_name"].replace(",", ";")
                mam_status = r["mam_status"] or ""
                mam_url = r["mam_url"] or ""
                mam_formats = (r["mam_formats"] or "").replace(",", "/")
                lines.append(f"{title}, {author}, {date}, {src_name or r['source'] or ''}, {src_url}, {mam_status}, {mam_url}, {mam_formats}")
            content = "\n".join(lines)
            return Response(content=content, media_type="text/plain",
                          headers={"Content-Disposition": f"attachment; filename=books_{filter}.txt"})
    finally:
        await db.close()


# ─── Fetch helpers (used by import routes) ──────────────────
async def _fetch_goodreads_book(book_id: str) -> dict:
    """Fetch book details from Goodreads by book ID.

    The `/book/show/{id}` endpoint is robots-permitted (it's the
    `/search` endpoint that's disallowed for `*` user-agents) so
    this user-initiated paste-URL import path is policy-clean.

    Detects Cloudflare's 202-with-empty-body soft-block and surfaces
    a 503 with a clear message pointing the user at Hardcover —
    avoids the silent "no metadata extracted" failure mode where
    the page parsing chugs through empty markup and returns an
    empty record.
    """
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"}
    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
        r = await client.get(f"https://www.goodreads.com/book/show/{book_id}")
        if r.status_code == 202 or (200 <= r.status_code < 300 and not (r.content or b"")):
            logger.info(
                "Goodreads paste-URL import: soft-blocked at network layer "
                "(status=%d, empty=%s) for book_id=%s",
                r.status_code, not bool(r.content), book_id,
            )
            raise HTTPException(
                503,
                "Goodreads is currently soft-blocking this server's IP "
                "(Cloudflare gate). Try again in a few minutes, or paste "
                "a Hardcover URL instead.",
            )
        r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    result = {"goodreads_id": book_id, "source": "goodreads", "source_url": json.dumps({"goodreads": f"https://www.goodreads.com/book/show/{book_id}"})}
    title_el = soup.find("h1", {"data-testid": "bookTitle"}) or soup.find("h1")
    result["title"] = title_el.get_text(strip=True) if title_el else ""
    author_el = soup.find("span", {"data-testid": "name"}) or soup.select_one("a.ContributorLink span")
    result["author_name"] = author_el.get_text(strip=True) if author_el else ""
    for script in soup.select("script[type='application/ld+json']"):
        try:
            ld = json.loads(script.string)
            if ld.get("image"): result["cover_url"] = ld["image"]
            if ld.get("datePublished"): result["pub_date"] = ld["datePublished"][:10]
            if ld.get("isbn"): result["isbn"] = ld["isbn"]
            if ld.get("numberOfPages"): result["page_count"] = int(ld["numberOfPages"])
        except (ValueError, TypeError, AttributeError): pass
    desc_el = soup.find("div", {"data-testid": "description"})
    if desc_el:
        spans = desc_el.find_all("span", class_=re.compile("Formatted"))
        result["description"] = (spans[-1] if spans else desc_el).get_text(strip=True)[:1000]
    series_el = soup.find("div", {"data-testid": "seriesTitle"})
    if series_el:
        for link in series_el.find_all("a"):
            sm = re.match(r'(.+?)\s*(?:\(|#)([\d.]+)\)?', link.get_text(strip=True))
            if sm:
                result["series_name"] = sm.group(1).strip()
                try: result["series_index"] = float(sm.group(2))
                except (ValueError, TypeError): pass
                break
    pub_el = soup.find("p", {"data-testid": "publicationInfo"})
    if pub_el:
        pt = pub_el.get_text(strip=True)
        em = re.search(r'[Ee]xpected\s+(?:publication\s+)?(.+?)$', pt)
        if em:
            result["is_unreleased"] = True
            for fmt in ["%B %d, %Y", "%B %Y", "%Y"]:
                try:
                    result["expected_date"] = datetime.strptime(re.sub(r'(\d+)(?:st|nd|rd|th)', r'\1', em.group(1).strip()), fmt).strftime("%Y-%m-%d")
                    break
                except ValueError: pass
    return result


async def _fetch_hardcover_book(slug: str) -> dict:
    """Fetch book details from Hardcover by slug using search API."""
    settings = load_settings()
    api_key = settings.get("hardcover_api_key", "")
    if not api_key:
        raise Exception("Hardcover API key not configured")
    headers = {"Content-Type": "application/json", "Authorization": api_key if api_key.startswith("Bearer") else f"Bearer {api_key}"}

    # Convert slug to search query: "honor-among-thieves-2014" → "honor among thieves"
    search_term = re.sub(r'-\d{4}$', '', slug).replace('-', ' ')
    logger.debug(f"  Hardcover import: slug='{slug}' → search='{search_term}'")

    # Step 1: Search for candidate book IDs
    search_query = """query($q: String!) {
        search(query: $q, query_type: "Book", per_page: 10, page: 1) { ids }
    }"""
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        r = await client.post("https://api.hardcover.app/v1/graphql",
            json={"query": search_query, "variables": {"q": search_term}})
        r.raise_for_status()

    data = r.json()
    ids_list = data.get("data", {}).get("search", {}).get("ids", [])
    logger.debug(f"  Hardcover import: search returned {len(ids_list)} IDs: {ids_list[:10]}")

    if not ids_list:
        raise Exception(f"No results on Hardcover for: {search_term}")

    # Step 2: Fetch all candidates and match by slug
    detail_query = """query($ids: [Int!]) { books(where: {id: {_in: $ids}}) {
        id title slug description
        series: cached_featured_series
        book_series { position series { name id } }
        contributions { author { name id } }
        editions(order_by: {users_count: desc_nulls_last}, limit: 1) {
            isbn_13 release_date
            image: cached_image
        }
    }}"""
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        r = await client.post("https://api.hardcover.app/v1/graphql",
            json={"query": detail_query, "variables": {"ids": [int(i) for i in ids_list[:10]]}})
        r.raise_for_status()

    bdata = r.json()
    candidates = bdata.get("data", {}).get("books", [])
    logger.debug(f"  Hardcover import: fetched {len(candidates)} candidates")

    # Match by exact slug first
    book = None
    for c in candidates:
        c_slug = c.get("slug", "")
        logger.debug(f"  Hardcover import: candidate slug='{c_slug}' title='{c.get('title')}'")
        if c_slug == slug:
            book = c
            logger.debug(f"  Hardcover import: MATCHED by slug → '{c.get('title')}'")
            break

    # Fallback: match by title similarity
    if not book:
        for c in candidates:
            if search_term.lower() in c.get("title", "").lower():
                book = c
                logger.debug(f"  Hardcover import: MATCHED by title → '{c.get('title')}'")
                break

    # Last fallback: first result
    if not book and candidates:
        book = candidates[0]
        logger.debug(f"  Hardcover import: FALLBACK to first → '{book.get('title')}'")

    if not book:
        raise Exception(f"Book not found on Hardcover for: {search_term}")

    # Parse the matched book data (already fetched with full details)
    real_slug = book.get("slug", slug)
    edition = book.get("editions", [{}])[0] if book.get("editions") else {}
    cover = None
    img = edition.get("image")
    if isinstance(img, dict): cover = img.get("url")
    elif isinstance(img, str): cover = img
    author_name = ""
    for c in book.get("contributions", []):
        a = c.get("author", {})
        if isinstance(a, dict) and a.get("name"):
            author_name = a["name"]; break
    series_name = None; series_index = None; series_options = []
    # Collect all series from book_series relation
    bs = book.get("book_series")
    if bs and isinstance(bs, list):
        for bse in bs:
            if isinstance(bse, dict):
                sr_obj = bse.get("series", {})
                if isinstance(sr_obj, dict) and sr_obj.get("name"):
                    series_options.append({"name": sr_obj["name"], "position": bse.get("position")})
    # Also check cached_featured_series
    series_data = book.get("series")
    if series_data and isinstance(series_data, list):
        for s in series_data:
            if isinstance(s, dict) and s.get("name"):
                if not any(so["name"] == s["name"] for so in series_options):
                    series_options.append({"name": s["name"], "position": s.get("position")})
    # Pick best series using same heuristic as scan
    if series_options:
        def _score(c):
            s = 0
            name = c["name"]
            if ":" in name: s += 10
            if c["position"] is not None: s += 5
            if "(" in name: s -= 3
            s += min(len(name.split()), 5)
            return s
        series_options.sort(key=_score, reverse=True)
        series_name = series_options[0]["name"]
        series_index = series_options[0]["position"]
        logger.debug(f"  Hardcover import: {len(series_options)} series found: {[s['name'] for s in series_options]} → default '{series_name}'")
    return {
        "hardcover_id": str(book.get("id")), "source": "hardcover",
        "source_url": json.dumps({"hardcover": f"https://hardcover.app/books/{real_slug}"}),
        "title": book.get("title", ""), "author_name": author_name,
        "description": (book.get("description") or "")[:1000],
        "isbn": edition.get("isbn_13"), "pub_date": edition.get("release_date"),
        "cover_url": cover, "series_name": series_name, "series_index": series_index,
        "series_options": series_options if len(series_options) > 1 else None,
    }


@router.post("/books/search-url")
async def search_by_url(data: dict = Body(...)):
    """Fetch book details from any supported source URL.

    v2.11.0 Stage 4.5: extended from Goodreads/Hardcover-only to
    cover Amazon, Open Library (works/books/isbn), Google Books,
    Kobo, IBDB. Dispatch lives in `app/discovery/url_import.py`;
    this router just unwraps the request body and surfaces errors.
    """
    from app.discovery.url_import import fetch_by_url
    url = data.get("url", "").strip()
    if not url:
        raise HTTPException(400, "URL is required")
    try:
        return await fetch_by_url(url)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Failed to fetch: {e}")
    except Exception as e:
        raise HTTPException(500, f"Error: {e}")


@router.post("/books/import-preview")
async def import_preview(data: dict = Body(...)):
    """Parse multiple URLs, fetch each, and check against DB."""

    def _norm(s):
        """Normalize name for comparison: collapse spaces, strip punctuation."""
        s = re.sub(r'\s+', ' ', s.lower().strip())
        s = re.sub(r'\.\s*', '. ', s)  # "S.A." → "S. A."
        return re.sub(r'\s+', ' ', s).strip()

    def _fuzzy(a, b):
        return SequenceMatcher(None, _norm(a), _norm(b)).ratio() > 0.85

    urls = data.get("urls", [])
    if not urls:
        raise HTTPException(400, "No URLs provided")

    results = []
    db = await get_db()
    try:
        # Pre-load all books for fuzzy matching
        all_books = await (await db.execute(
            f"SELECT b.id, b.title, b.owned, b.source_url, b.author_id, a.name as author_name "
            f"FROM books b JOIN authors a ON b.author_id=a.id WHERE {HF}"
        )).fetchall()

        # v2.11.0 Stage 4.5: dispatch via the universal URL parser.
        # Covers Goodreads, Hardcover, Amazon, Open Library, Google
        # Books, Kobo, IBDB. Unknown URLs surface "Unrecognized URL".
        from app.discovery.url_import import fetch_by_url, parse_url

        for url in urls[:50]:
            url = url.strip()
            if not url: continue
            entry = {"url": url, "status": "error", "error": None, "book": None}
            try:
                if parse_url(url) is None:
                    entry["error"] = "Unrecognized URL format"
                    results.append(entry); continue
                book = await fetch_by_url(url)

                entry["book"] = book
                title = book.get("title", "")
                author = book.get("author_name", "")

                if title and author:
                    # Fuzzy match against all books in DB
                    matched = None
                    for r in all_books:
                        if _fuzzy(r["title"], title) and _fuzzy(r["author_name"], author):
                            matched = r
                            break
                    if matched:
                        entry["status"] = "owned" if matched["owned"] else "tracked"
                        entry["existing_id"] = matched["id"]
                        entry["has_url"] = bool(matched["source_url"] and matched["source_url"] != "{}")
                    else:
                        entry["status"] = "new"
                else:
                    entry["status"] = "new"
            except Exception as e:
                entry["error"] = str(e)[:200]
            results.append(entry)
            await asyncio.sleep(0.5)
        return {"results": results}
    finally:
        await db.close()


@router.post("/books/import-add")
async def import_add_books(data: dict = Body(...)):
    """Add books from import preview. Expects {books: [{...book data...}]}."""

    def _norm(s):
        s = re.sub(r'\s+', ' ', s.lower().strip())
        s = re.sub(r'\.\s*', '. ', s)
        return re.sub(r'\s+', ' ', s).strip()

    def _fuzzy(a, b):
        return SequenceMatcher(None, _norm(a), _norm(b)).ratio() > 0.85

    books = data.get("books", [])
    if not books:
        raise HTTPException(400, "No books to import")
    added = 0; updated = 0
    for book_data in books:
        try:
            title = book_data.get("title", "").strip()
            author_name = book_data.get("author_name", "").strip()
            if not title or not author_name: continue

            db = await get_db()
            try:
                # Fuzzy-match author
                all_authors = await (await db.execute("SELECT id, name FROM authors")).fetchall()
                aid = None
                for a in all_authors:
                    if _fuzzy(a["name"], author_name):
                        aid = a["id"]; break
                if not aid:
                    cur = await db.execute("INSERT INTO authors (name, sort_name) VALUES (?, ?)", (author_name, author_name))
                    aid = cur.lastrowid

                # Fuzzy-match existing book
                existing_books = await (await db.execute("SELECT id, title, source_url FROM books WHERE author_id=?", (aid,))).fetchall()
                existing = None
                for eb in existing_books:
                    if _fuzzy(eb["title"], title):
                        existing = eb; break

                if existing:
                    if book_data.get("source_url"):
                        try:
                            new_urls = json.loads(book_data["source_url"])
                            old_urls = json.loads(existing["source_url"] or "{}")
                            old_urls.update(new_urls)
                            await db.execute("UPDATE books SET source_url=? WHERE id=?", (json.dumps(old_urls), existing["id"]))
                            updated += 1
                        except (ValueError, TypeError): pass
                else:
                    # Find/create series
                    sid = None
                    if book_data.get("series_name"):
                        srow = await (await db.execute("SELECT id FROM series WHERE LOWER(name)=LOWER(?) AND author_id=?", (book_data["series_name"], aid))).fetchone()
                        if srow: sid = srow["id"]
                        else:
                            cur = await db.execute("INSERT INTO series (name, author_id) VALUES (?, ?)", (book_data["series_name"], aid))
                            sid = cur.lastrowid

                    is_unreleased = 1 if book_data.get("is_unreleased") else 0
                    src = book_data.get("source", "import")
                    await db.execute(
                        "INSERT INTO books (title, author_id, series_id, series_index, pub_date, expected_date, is_unreleased, description, isbn, cover_url, source, source_url, owned, is_new) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,1)",
                        (title, aid, sid, book_data.get("series_index"), book_data.get("pub_date"),
                         book_data.get("expected_date"), is_unreleased, book_data.get("description"),
                         book_data.get("isbn"), book_data.get("cover_url"), src,
                         book_data.get("source_url", "{}"))
                    )
                    added += 1
                await db.commit()
            finally:
                await db.close()
        except Exception as e:
            logger.error(f"Import error for '{book_data.get('title')}': {e}")
    return {"status": "ok", "added": added, "updated": updated}
