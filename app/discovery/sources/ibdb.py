"""
IBDB (Internet Book Database) metadata source — REST API.

Simple JSON API at ibdb.dev with no auth required. Returns book metadata
including title, authors, ISBN, cover URL, description, publisher, and
publication date. Positioned as a supplementary source for ISBN and
publisher backfill when primary sources miss.
"""
import logging
from typing import Optional

from app.discovery.sources.base import BaseSource, AuthorResult, SeriesResult, BookResult
from app.metadata.scoring import score_match

logger = logging.getLogger("seshat.discovery.ibdb")

_SEARCH_URL = "https://ibdb.dev/api/search"


class IbdbSource(BaseSource):
    name = "ibdb"
    default_headers = {
        "Accept": "application/json",
    }
    default_timeout = 15.0

    def __init__(self, rate_limit: float = 1.0):
        super().__init__(rate_limit=rate_limit)

    async def search_author(self, author_name: str) -> Optional[AuthorResult]:
        """Search IBDB for an author. Returns minimal AuthorResult."""
        try:
            resp = await self._get(_SEARCH_URL, params={"q": author_name})
            data = resp.json()
        except Exception:
            return None

        results = _extract_results(data)
        if not results:
            return None

        return AuthorResult(name=author_name, external_id=author_name)

    async def get_author_books(
        self, author_id: str,
        existing_titles: set = None,
        owned_titles: list = None,
        owned_only: bool = False,
    ) -> Optional[AuthorResult]:
        """Search IBDB for books by this author.

        IBDB doesn't have a dedicated author endpoint, so we search
        by author name and filter results by match quality.
        """
        author_name = author_id
        existing_titles = existing_titles or set()
        owned_titles = owned_titles or []

        try:
            resp = await self._get(_SEARCH_URL, params={"q": author_name})
            data = resp.json()
        except Exception:
            return None

        results = _extract_results(data)
        if not results:
            return None

        books = []
        series_map = {}

        for item in results[:20]:
            if not isinstance(item, dict):
                continue

            title = item.get("title") or item.get("name") or ""
            if not title:
                continue

            item_authors = _extract_authors(item)
            sc = score_match(
                record_title=title, record_authors=item_authors,
                search_title=title, search_authors=author_name,
            )
            # Only include results with reasonable author match
            if sc < 0.3:
                continue

            # ibdb.dev's actual response shape (verified live 2026-04-15):
            #   isbn13         (string, no underscore)
            #   synopsis       (string)
            #   publicationDate (string, e.g. "2022-03" or "2022-11-30")
            #   image          (DICT with .url/.width/.height — never a bare URL)
            #   id             (uuid string)
            # No native series field; series info is sometimes embedded in the
            # title ("A Touch of Light: Book 1 in The Ashes of Avarin") but
            # not exposed as structured data, so we don't try to parse it
            # and let the consensus pass align with goodreads/hardcover.
            # The pre-2026 mappings (isbn_13 / publication_date / etc.) are
            # kept as fallbacks in case the API drifts back, but in practice
            # only the camelCase keys ever match.
            isbn = item.get("isbn13") or item.get("isbn_13") or item.get("isbn") or item.get("isbn_10")
            cover_raw = item.get("image") or item.get("cover") or item.get("thumbnail")
            # `image` is always a dict with a .url field. Older fallback keys
            # could in theory be a bare string URL — handle both shapes so a
            # future API revision doesn't crash sqlite binding (the v1.1.9
            # bug: dict went straight into cover_url and crashed on UPDATE
            # books with "type 'dict' is not supported").
            if isinstance(cover_raw, dict):
                cover = cover_raw.get("url")
            elif isinstance(cover_raw, str):
                cover = cover_raw
            else:
                cover = None
            pub_date = item.get("publicationDate") or item.get("publication_date") or item.get("publish_date")
            pages = item.get("pageCount") or item.get("pages") or item.get("page_count")
            description = item.get("synopsis") or item.get("description") or item.get("summary")
            series_name = item.get("series") or item.get("series_name")
            series_index = None
            if item.get("series_number") or item.get("series_index"):
                try:
                    series_index = float(item.get("series_number") or item.get("series_index"))
                except (ValueError, TypeError):
                    pass

            bk = BookResult(
                title=title,
                series_name=series_name if isinstance(series_name, str) else None,
                series_index=series_index,
                isbn=str(isbn).replace("-", "") if isbn else None,
                cover_url=cover,
                pub_date=str(pub_date)[:10] if pub_date else None,
                description=description if isinstance(description, str) else None,
                page_count=int(pages) if isinstance(pages, (int, str)) and str(pages).isdigit() else None,
                language=item.get("language") if isinstance(item.get("language"), str) else None,
                external_id=str(item.get("id", "")),
                source="ibdb",
            )

            if series_name and isinstance(series_name, str):
                key = series_name.lower().strip()
                if key not in series_map:
                    series_map[key] = SeriesResult(name=series_name)
                series_map[key].books.append(bk)
            else:
                books.append(bk)

        return AuthorResult(
            name=author_name,
            external_id=author_name,
            books=books,
            series=list(series_map.values()),
        )


def _extract_results(data) -> list:
    """Handle IBDB's variable response shapes."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        results = data.get("results", data.get("books", data.get("data", [])))
        return results if isinstance(results, list) else []
    return []


def _extract_authors(item: dict) -> list[str]:
    """Extract author names from various IBDB response shapes."""
    authors = item.get("authors") or item.get("author") or []
    if isinstance(authors, str):
        return [authors]
    if isinstance(authors, list):
        out = []
        for a in authors:
            if isinstance(a, str):
                out.append(a)
            elif isinstance(a, dict):
                out.append(a.get("name", str(a)))
        return out
    return []
