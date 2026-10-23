"""
Goodreads `/author/list/{author_id}` bibliography walker for T5
of the goodreads_id_resolver chain.

When the resolver's identifier-based tiers (T1-T3) and free-text
title tier (T4) all miss but we know the author's `goodreads_id`,
this module walks the author's bibliography pages and fuzzy-matches
titles to find the book. Page format is `?page=N` with 30 books per
page in `schema.org/Book` microdata — confirmed via probe on
Sanderson (14+ pages), James S.A. Corey (3), mid-list (1), debut
(1).

Robots-clean: `/author/list/` is NOT in goodreads.com's
`User-agent: *` Disallow list. The pages are served via AWS
CloudFront behind AWS WAF, which lets list pages through far more
readily than book pages; we route through `GoodreadsSession.get()`
for uniform pacing, block detection, the list-page backoff and
curl_cffi impersonation.

Cache strategy (`id_cache.author_bib` scope, 7-day TTL): walk pages
lazily and cache cumulatively. Early-stop on first title match. The
first entry in the cached list is a meta-header dict that tracks
`pages_walked` and `fully_indexed` so a subsequent lookup for a
different title by the same author can resume from where we left
off (or skip all walking entirely when the cache is fully indexed
and the title isn't there).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Optional

from bs4 import BeautifulSoup

from app.metadata import goodreads_session, id_cache
from app.metadata.scoring import title_similarity

_log = logging.getLogger("seshat.metadata.goodreads_bibliography")

_BASE = "https://www.goodreads.com"

# Pagination cap. Sanderson is ~14 pages; anything past 50 (1500
# entries) is almost certainly a Goodreads disambig-author-page
# pathology, not a real bibliography.
_MAX_PAGES = 50

# Title-similarity threshold for accepting a bibliography entry as
# the requested book. `title_similarity()` ranges 0.0-1.0 and
# already handles series-decoration noise, so 0.85 is conservative
# without being trigger-happy on partial matches like "Mistborn"
# vs "Mistborn Trilogy Boxed Set".
_TITLE_MATCH_THRESHOLD = 0.85


@dataclass
class BiblioEntry:
    """One row from an `/author/list/` page."""
    book_id: str
    work_id: str = ""
    title: str = ""
    pub_year: Optional[int] = None
    avg_rating: Optional[float] = None
    ratings_count: Optional[int] = None


# ─── Public API ──────────────────────────────────────────────────────


async def find_book_in_bibliography(
    author_goodreads_id: str,
    title: str,
) -> Optional[str]:
    """Walk an author's bibliography pages until we find the book.

    Returns:
      - The Goodreads `book_id` on a fuzzy-match hit.
      - The string `"_soft_blocked"` when a page fetch was blocked by
        Goodreads' bot protection, or list pages are backing off.
      - `None` on exhaustion (walked the whole bibliography without
        a match), missing inputs, or page-walk cap reached.
    """
    if not author_goodreads_id or not title:
        return None

    # Read prior cumulative state. Meta header sits at index 0 if
    # this author has been walked before.
    cached = id_cache.get_author_bib(author_goodreads_id) or []
    meta, entries = _split_cache(cached)
    pages_walked = int(meta.get("pages_walked", 0))
    fully_indexed = bool(meta.get("fully_indexed", False))

    # Search what we already have before paying any HTTP.
    hit = _find_match(entries, title)
    if hit is not None:
        return hit.book_id

    # If we've walked everything previously and the title still
    # isn't there, save the round-trip.
    if fully_indexed:
        return None

    # Walk additional pages. Resume from `pages_walked + 1` so we
    # don't re-fetch what's already cached.
    next_page = max(pages_walked + 1, 1)
    while next_page <= _MAX_PAGES:
        new_entries, has_more, soft_block = await _fetch_page(
            author_goodreads_id, next_page,
        )
        if soft_block:
            return "_soft_blocked"
        if new_entries:
            entries.extend(new_entries)
            hit = _find_match(new_entries, title)
        pages_walked = next_page
        fully_indexed = not has_more
        # Persist after every page so a partial walk isn't wasted
        # if a later page errors out.
        _write_cache(author_goodreads_id, entries, pages_walked, fully_indexed)
        if hit is not None:
            return hit.book_id
        if not has_more:
            return None
        next_page += 1

    _log.info(
        "bibliography: hit max_pages cap (%d) for author_id=%s "
        "without finding title=%r — bailing out",
        _MAX_PAGES, author_goodreads_id, title,
    )
    return None


# ─── Page fetcher + parser ───────────────────────────────────────────


async def _fetch_page(
    author_goodreads_id: str, page: int,
) -> tuple[list[BiblioEntry], bool, bool]:
    """Fetch one bibliography page and parse it into BiblioEntry rows.

    Returns `(entries, has_more, soft_blocked)` where:
      - `entries` is the parsed rows (empty list on parse failure).
      - `has_more` is True when a `?page=N+1` link is present.
      - `soft_blocked` is True when Goodreads' bot protection turned the
        page away, or list pages are backing off — caller bails the
        whole walk rather than falsely caching a partial result.
    """
    url = f"{_BASE}/author/list/{author_goodreads_id}?page={page}"
    session = await goodreads_session.get_session()
    try:
        resp = await session.get(url)
    except goodreads_session.GoodreadsBackingOff as e:
        # Not "no more pages": that would cache the walk as complete.
        _log.debug("bibliography: page %d skipped — %s", page, e)
        return [], False, True
    except Exception as e:
        _log.debug(
            "bibliography: network error fetching page %d for author_id=%s: %s",
            page, author_goodreads_id, e,
        )
        return [], False, False

    if goodreads_session.is_soft_block(resp):
        _log.info(
            "bibliography: soft-blocked on page %d for author_id=%s "
            "(status=%s) — abandoning walk",
            page, author_goodreads_id,
            getattr(resp, "status_code", None),
        )
        return [], False, True

    status = getattr(resp, "status_code", None)
    if status != 200:
        _log.debug(
            "bibliography: unexpected status %s on page %d for author_id=%s",
            status, page, author_goodreads_id,
        )
        return [], False, False

    body = getattr(resp, "text", None) or ""
    return _parse_page(body)


def _parse_page(html: str) -> tuple[list[BiblioEntry], bool, bool]:
    """Extract `BiblioEntry` rows + `has_more` from a list-page body."""
    soup = BeautifulSoup(html, "lxml")
    entries: list[BiblioEntry] = []

    rows = soup.select('tr[itemtype="http://schema.org/Book"]')
    for row in rows:
        title_a = row.select_one("a.bookTitle")
        if not title_a:
            continue
        href = title_a.get("href") or ""
        # /book/show/{book_id}.{slug}  OR  /book/show/{book_id}-{slug}
        book_id = _extract_book_id_from_href(href)
        if not book_id:
            continue
        title_text = ""
        name_span = title_a.select_one("span[itemprop='name']")
        if name_span:
            title_text = name_span.get_text(" ", strip=True)
        else:
            title_text = title_a.get_text(" ", strip=True)

        # Editions link exposes the work_id (robots-permitted target).
        work_id = ""
        ed_a = row.select_one("a[href*='/work/editions/']")
        if ed_a:
            work_id = _extract_work_id_from_href(ed_a.get("href") or "")

        # Mini rating block: "4.49 avg rating — 1,027,854 ratings — published 2006 — ..."
        pub_year: Optional[int] = None
        avg_rating: Optional[float] = None
        ratings_count: Optional[int] = None
        mini = row.select_one(".minirating")
        greybox = row.select_one(".greyText.smallText.uitext")
        if mini:
            avg_rating, ratings_count = _parse_mini_rating(
                mini.get_text(" ", strip=True),
            )
        if greybox:
            pub_year = _parse_pub_year(greybox.get_text(" ", strip=True))

        entries.append(BiblioEntry(
            book_id=book_id,
            work_id=work_id,
            title=title_text,
            pub_year=pub_year,
            avg_rating=avg_rating,
            ratings_count=ratings_count,
        ))

    # `has_more`: the pagination block contains `?page=N` links. If
    # the highest visible page > current, there's more to walk.
    # The current page won't have its own pagination link in the
    # paginated row — Goodreads renders forward links + a few
    # backward links + ellipsis.
    has_more = bool(soup.select(".pagination a[href*='page='], a[rel='next']"))

    return entries, has_more, False


# ─── Cache helpers ───────────────────────────────────────────────────


def _split_cache(
    cached: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[BiblioEntry]]:
    """Split the raw cached list into (meta_header, entries)."""
    meta: dict[str, Any] = {}
    entries: list[BiblioEntry] = []
    for item in cached:
        if not isinstance(item, dict):
            continue
        if item.get("_meta") is True:
            meta = item
            continue
        book_id = item.get("book_id")
        if not book_id:
            continue
        entries.append(BiblioEntry(
            book_id=str(book_id),
            work_id=str(item.get("work_id") or ""),
            title=str(item.get("title") or ""),
            pub_year=item.get("pub_year"),
            avg_rating=item.get("avg_rating"),
            ratings_count=item.get("ratings_count"),
        ))
    return meta, entries


def _write_cache(
    author_id: str,
    entries: list[BiblioEntry],
    pages_walked: int,
    fully_indexed: bool,
) -> None:
    """Persist the cumulative walk state. Meta header at index 0."""
    payload: list[dict[str, Any]] = [{
        "_meta": True,
        "pages_walked": pages_walked,
        "fully_indexed": fully_indexed,
    }]
    payload.extend(asdict(e) for e in entries)
    id_cache.put_author_bib(author_id, payload)


def _find_match(
    entries: list[BiblioEntry], title: str,
) -> Optional[BiblioEntry]:
    """Return the best fuzzy-matched entry above the threshold."""
    best: Optional[BiblioEntry] = None
    best_score = 0.0
    for entry in entries:
        if not entry.title:
            continue
        score = title_similarity(title, entry.title)
        if score > best_score:
            best_score = score
            best = entry
    if best is not None and best_score >= _TITLE_MATCH_THRESHOLD:
        return best
    return None


# ─── Tiny parsers ────────────────────────────────────────────────────


def _extract_book_id_from_href(href: str) -> str:
    """`/book/show/68428.Mistborn` or `/book/show/2767793-the-hero-of-ages`
    → `68428` or `2767793`."""
    if not href:
        return ""
    tail = href.rsplit("/", 1)[-1]
    # Stop at the first non-digit, after stripping any leading slash.
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    return digits


def _extract_work_id_from_href(href: str) -> str:
    """`/work/editions/66322-mistborn-the-final-empire` → `66322`."""
    if not href:
        return ""
    tail = href.rsplit("/", 1)[-1]
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    return digits


def _parse_mini_rating(text: str) -> tuple[Optional[float], Optional[int]]:
    """Parse `'4.49 avg rating — 1,027,854 ratings'` → (4.49, 1027854)."""
    import re
    avg: Optional[float] = None
    count: Optional[int] = None
    avg_m = re.search(r"(\d+\.\d+)\s+avg\s+rating", text, re.I)
    if avg_m:
        try:
            avg = float(avg_m.group(1))
        except ValueError:
            pass
    count_m = re.search(r"([\d,]+)\s+ratings", text, re.I)
    if count_m:
        try:
            count = int(count_m.group(1).replace(",", ""))
        except ValueError:
            pass
    return avg, count


def _parse_pub_year(text: str) -> Optional[int]:
    """Extract a 4-digit year from `'... published 2006 — ...'`."""
    import re
    m = re.search(r"published\s+(\d{4})", text, re.I)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None
