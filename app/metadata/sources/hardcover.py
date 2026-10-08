"""
Hardcover.app metadata source — GraphQL API.

Book-centric MetaSource. Two queries:
  1. Search by "{title} {author}" → get book IDs
  2. FindBooksByIds → full metadata with English edition preference

Auth: requires a Hardcover API Bearer token (user provides in
Seshat settings). Without a token, returns None silently.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.metadata import source_gate
from app.metadata.record import MetaRecord
from app.metadata.sources.base import MetaSource
from app.metadata.text_clean import description_to_plain_text

_log = logging.getLogger("seshat.metadata.hardcover")

_API = "https://api.hardcover.app/v1/graphql"

_FRAGMENTS = """
fragment BookData on books {
  id title slug rating description
  series: cached_featured_series
  book_series { position series { name id } }
  tags: cached_tags
  contributions { author { name id } }
}
fragment EditionData on editions {
  title id isbn_13 asin
  image: cached_image
  release_date pages
  language { code3 }
}
"""

_SEARCH = """
query Search($query: String!) {
  search(query: $query, query_type: "Book", per_page: 25) {
    ids
  }
}
"""

_FIND_BOOKS = _FRAGMENTS + """
query FindBooksByIds($ids: [Int!], $languages: [String!], $format_ids: [Int!]) {
  books(where: {id: {_in: $ids}}, order_by: {users_read_count: desc_nulls_last}) {
    ...BookData
    editions(
      where: {reading_format_id: {_in: $format_ids},
              language: {_or: [{code3: {_in: $languages}},
                               {code3: {_is_null: true}}]}}
      order_by: {users_count: desc_nulls_last}
      limit: 1
    ) { ...EditionData }
  }
}
"""


# Hardcover's `reading_format_id` enum: 1=Physical, 2=Audiobook, 4=E-Book.
# Mirror the discovery-side filter split — audiobook enrichment pulls
# audiobook editions, everything else pulls print/ebook.
def _edition_format_ids(audiobook: bool) -> list[int]:
    return [2] if audiobook else [1, 4]


class HardcoverSource(MetaSource):
    name = "hardcover"
    default_timeout = 30.0

    def __init__(self, *, api_key: str = "", rate_limit: float = 1.0):
        super().__init__(rate_limit=rate_limit)
        self._api_key = api_key.strip()

    def _build_client(self) -> httpx.AsyncClient:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Seshat/1.0",
        }
        if self._api_key:
            token = self._api_key
            if " " not in token:
                token = f"Bearer {token}"
            headers["Authorization"] = token
        return httpx.AsyncClient(
            timeout=self.default_timeout,
            headers=headers,
        )

    async def _query(self, query: str, variables: dict) -> dict:
        import json
        async with source_gate.turn(self.name) as t:
            resp = await self.client.post(
                _API,
                content=json.dumps({"query": query, "variables": variables}),
            )
            t.status(resp.status_code)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            _log.warning("hardcover graphql errors: %s", data["errors"])
        return data.get("data", {})

    async def search_book(
        self, title: str, author: str, **_,
    ) -> Optional[MetaRecord]:
        if not self._api_key:
            return None
        if not title:
            return None

        query = f"{title} {author}".strip()
        try:
            search_data = await self._query(_SEARCH, {"query": query})
        except Exception:
            _log.exception("hardcover: search failed")
            return None

        ids = search_data.get("search", {}).get("ids", [])
        if not ids:
            return None

        # Fetch top 10 results with full metadata. `_audiobook_hint` is
        # set by the enricher before the call when enriching audiobook
        # grabs, so we pick the right `reading_format_id` bucket.
        audiobook = bool(getattr(self, "_audiobook_hint", False))
        try:
            books_data = await self._query(
                _FIND_BOOKS,
                {
                    "ids": ids[:10],
                    "languages": ["eng", "en"],
                    "format_ids": _edition_format_ids(audiobook),
                },
            )
        except Exception:
            _log.exception("hardcover: fetch books failed")
            return None

        books = books_data.get("books", [])
        if not books:
            return None

        # Score and pick the best match.
        from app.metadata.scoring import score_match
        best = None
        best_score = 0.0
        for book in books:
            book_title = book.get("title", "")
            book_authors = [
                c.get("author", {}).get("name", "")
                for c in book.get("contributions", [])
                if c.get("author", {}).get("name")
            ]
            score = score_match(
                record_title=book_title,
                record_authors=book_authors,
                search_title=title,
                search_authors=author,
            )
            if score > best_score:
                best = book
                best_score = score

        if best is None or best_score < 0.3:
            return None

        return _book_to_record(best)


def _book_to_record(book: dict) -> MetaRecord:
    """Convert a Hardcover book object to a MetaRecord."""
    authors = [
        c.get("author", {}).get("name", "")
        for c in book.get("contributions", [])
        if c.get("author", {}).get("name")
    ]

    # Series from book_series.
    series_name = None
    series_index = None
    for bs in book.get("book_series", []):
        s = bs.get("series", {})
        if s.get("name"):
            series_name = s["name"]
            pos = bs.get("position")
            if pos is not None:
                try:
                    series_index = float(pos)
                except (ValueError, TypeError):
                    pass
            break

    # Best edition.
    editions = book.get("editions", [])
    ed = editions[0] if editions else {}

    isbn = ed.get("isbn_13") or ""
    cover_url = ed.get("image") or ""
    pub_date = ed.get("release_date") or ""
    pages = ed.get("pages")
    lang = (ed.get("language") or {}).get("code3", "")

    # Tags.
    tags = []
    raw_tags = book.get("tags")
    if isinstance(raw_tags, list):
        tags = [str(t.get("tag", t) if isinstance(t, dict) else t) for t in raw_tags[:20]]
    elif isinstance(raw_tags, str):
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

    slug = book.get("slug", "")
    book_id = book.get("id", "")

    return MetaRecord(
        title=book.get("title", ""),
        authors=authors,
        series=series_name,
        series_index=series_index,
        description=description_to_plain_text(book.get("description")),
        isbn=str(isbn).replace("-", "") if isbn else None,
        pub_date=str(pub_date)[:10] if pub_date else None,
        page_count=int(pages) if pages else None,
        language=lang or None,
        tags=tags,
        cover_url=cover_url or None,
        source="hardcover",
        source_url=f"https://hardcover.app/books/{slug}" if slug else None,
        external_id=str(book_id),
    )
