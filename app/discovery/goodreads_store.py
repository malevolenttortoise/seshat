"""
Goodreads detail store (2026-10 audit wave 4b, ADR-0026).

One row per Goodreads book ID in `metadata_cache_goodreads_books`, in two
parts that are filled separately:

- the **autocomplete part**: what a `/book/auto_complete` hit says about the
  book (work ID, title, author ID + name, page count, ratings, a description
  snippet, the Kindle ASIN), written by the candidate worker;
- the **page part**: a `/book/show/{id}` page parsed twice, once the way
  discovery reads it (`parse_book_page`: language, format, genres, series,
  contributors, …) and once the way enrichment reads it (a `MetaRecord`),
  written by whoever loaded the page: the candidate worker, phase 2 or a
  grab's enrichment (G93).

Enrichment reads a stored page before fetching one live, when it is less
than 90 days old and the book wasn't unreleased (G93). The page HTML itself
isn't kept (200–600 KB each).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from app.discovery import metadata_cache

_log = logging.getLogger("seshat.discovery.goodreads_store")

_SOURCE = metadata_cache.SOURCE_GOODREADS

# A stored page serves enrichment for this long (G93).
PAGE_MAX_AGE_S = 90 * 86400.0

_ASIN_RX = re.compile(r"[?&]asin=([A-Z0-9]{10})")
_TAG_RX = re.compile(r"<[^>]+>")


def hit_asin(hit: dict) -> Optional[str]:
    """The Kindle ASIN an autocomplete hit carries, in its
    `kcrPreviewUrl` query string (…/kp/embed?asin=B0…)."""
    m = _ASIN_RX.search(str(hit.get("kcrPreviewUrl") or ""))
    return m.group(1) if m else None


def hit_snippet(hit: dict) -> str:
    """The hit's description snippet as plain text ('' when none)."""
    import html as _html
    desc = hit.get("description")
    raw = desc.get("html") if isinstance(desc, dict) else desc
    text = _TAG_RX.sub(" ", _html.unescape(str(raw or "")))
    return re.sub(r"\s+", " ", text).strip()


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float_or_none(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def save_hit(hit: dict, *, now: Optional[float] = None) -> Optional[str]:
    """Store an autocomplete hit's fields under its book ID; returns the
    book ID (None when the hit has none). The page part is kept."""
    book_id = str(hit.get("bookId") or "").strip()
    if not book_id:
        return None
    author = hit.get("author") or {}
    row = (
        book_id,
        str(hit.get("workId") or "") or None,
        str(author.get("id") or "") or None,
        author.get("name") or None,
        hit.get("bookTitleBare") or hit.get("title") or None,
        _int_or_none(hit.get("numPages")),
        _int_or_none(hit.get("ratingsCount")),
        _float_or_none(hit.get("avgRating")),
        hit_snippet(hit) or None,
        hit_asin(hit),
        now if now is not None else time.time(),
    )
    bt = metadata_cache.books_table(_SOURCE)
    db = await metadata_cache.get_db(_SOURCE)
    try:
        await db.execute(
            f"INSERT INTO {bt} (book_id, work_id, author_gr_id, author_name, "
            f"ac_title, ac_num_pages, ratings_count, avg_rating, snippet, asin, "
            f"ac_fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            f"ON CONFLICT(book_id) DO UPDATE SET work_id=excluded.work_id, "
            f"author_gr_id=excluded.author_gr_id, author_name=excluded.author_name, "
            f"ac_title=excluded.ac_title, ac_num_pages=excluded.ac_num_pages, "
            f"ratings_count=excluded.ratings_count, avg_rating=excluded.avg_rating, "
            f"snippet=excluded.snippet, asin=excluded.asin, "
            f"ac_fetched_at=excluded.ac_fetched_at",
            row,
        )
        await db.commit()
    finally:
        await db.close()
    return book_id


def _page_details_json(details: dict) -> str:
    d = dict(details)
    d["contributors"] = [
        {"name": c.name, "role": c.role, "source_author_id": c.source_author_id}
        for c in (details.get("contributors") or [])
    ]
    return json.dumps(d)


def _record_json(html: str) -> Optional[str]:
    from app.metadata.record import MetaRecord
    from app.metadata.sources.goodreads import _merge_detail_page
    record = MetaRecord()
    try:
        _merge_detail_page(record, html)
    except Exception:
        _log.debug("goodreads store: enrichment parse failed", exc_info=True)
        return None
    return json.dumps(record.to_dict())


async def save_page(
    book_id: str, html: str, *, now: Optional[float] = None,
) -> dict:
    """Parse a loaded `/book/show/{book_id}` page both ways and store it;
    returns the discovery parse (`parse_book_page`)."""
    from app.discovery.sources.goodreads import parse_book_page
    details = parse_book_page(html, book_id=book_id)
    record = _record_json(html)
    bt = metadata_cache.books_table(_SOURCE)
    db = await metadata_cache.get_db(_SOURCE)
    try:
        await db.execute(
            f"INSERT INTO {bt} (book_id, page_fetched_at, page_json, record_json) "
            f"VALUES (?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET "
            f"page_fetched_at=excluded.page_fetched_at, "
            f"page_json=excluded.page_json, record_json=excluded.record_json",
            (str(book_id), now if now is not None else time.time(),
             _page_details_json(details), record),
        )
        await db.commit()
    finally:
        await db.close()
    return details


async def get_book(book_id: str) -> Optional[dict]:
    """The stored row for `book_id` (both parts), or None."""
    bt = metadata_cache.books_table(_SOURCE)
    db = await metadata_cache.get_db(_SOURCE)
    try:
        cur = await db.execute(
            f"SELECT * FROM {bt} WHERE book_id = ?", (str(book_id),),
        )
        row = await cur.fetchone()
    finally:
        await db.close()
    return dict(row) if row is not None else None


def page_details_from_row(row: Optional[dict]) -> Optional[dict]:
    """The stored discovery parse, contributors as `Contributor`s."""
    if not row or not row.get("page_json"):
        return None
    try:
        details = json.loads(row["page_json"])
    except (TypeError, ValueError):
        return None
    from app.discovery.sources.base import Contributor
    details["contributors"] = [
        Contributor(name=c.get("name") or "", role=c.get("role"),
                    source_author_id=c.get("source_author_id"))
        for c in (details.get("contributors") or [])
        if isinstance(c, dict) and c.get("name")
    ]
    return details


async def stored_record(
    book_id: str, *, max_age_s: float = PAGE_MAX_AGE_S,
    now: Optional[float] = None,
):
    """Enrichment's record from a stored page (G93): a `MetaRecord` when
    the page was stored less than `max_age_s` ago and the book wasn't
    unreleased then, else None (enrichment fetches the page live)."""
    row = await get_book(book_id)
    if not row or not row.get("record_json") or not row.get("page_fetched_at"):
        return None
    now = now if now is not None else time.time()
    if now - float(row["page_fetched_at"]) > max_age_s:
        return None
    details = page_details_from_row(row) or {}
    if details.get("is_unreleased"):
        return None
    try:
        data = json.loads(row["record_json"])
    except (TypeError, ValueError):
        return None
    from app.metadata.record import MetaRecord
    fields = {
        k: data.get(k) for k in (
            "title", "authors", "series", "series_index", "description",
            "isbn", "publisher", "pub_date", "page_count", "language",
            "tags", "cover_url",
        )
    }
    fields["authors"] = list(fields.get("authors") or [])
    fields["tags"] = list(fields.get("tags") or [])
    return MetaRecord(**fields)
