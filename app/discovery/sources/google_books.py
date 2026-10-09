"""
Google Books metadata source — public REST API.

Clean JSON responses with title, authors, publisher, date, description,
ISBN, pageCount, categories, language, and image links. Without an API
key the call bills to Google's shared anonymous project, which is out of
quota (429s); the stored key is sent on every request.

**Queries (2026-10 audit, G55).** Since about 2026-09-26 Google answers a
query made only of field operators (`inauthor:X`, `intitle:X`) with 200
and no results. An author scan now searches the quoted name as free text
(`"Travis Dean"`) and keeps the volumes whose authors match, in one
search per author: `search_author` does it and returns the books inline.
Pages hold at most 20 items whatever `maxResults` says, so paging
advances by the items actually returned, and stops at the end of the
results, at a page with no matching volume, or after `_MAX_PAGES`.

Positioned as a supplementary source for ISBN, publisher, and
description backfill. Match quality is poor for niche/indie titles
and there's no native series field — we parse series from the title
parenthetical pattern when possible.
"""
import asyncio
import logging
import re
import time
from typing import Optional

import httpx

from app.discovery.sources.base import BaseSource, AuthorResult, SeriesResult, BookResult, Contributor, _redact_sensitive
from app.metadata import source_gate
from app.metadata.scoring import author_overlap
from app.metadata.sources.google_books import retry_delay

logger = logging.getLogger("seshat.discovery.google_books")

_API = "https://www.googleapis.com/books/v1/volumes"

# Circuit-breaker threshold: after this many consecutive 429 responses,
# auto-disable the source by switching off its toggles in
# `metadata_sources` (see `_trip_circuit_breaker`). The anonymous Google Books quota can run out for days on
# a modest library scan, and without this every subsequent scan wastes
# a full per-book budget slot on a source that's guaranteed to 429.
# 5 is tight enough to catch sustained exhaustion quickly without
# tripping on a transient blip (one retry-after a rate-limit window).
# Reset to 0 on any successful response — so a day-later scan after
# the quota resets naturally clears the counter on its first hit.
_CIRCUIT_BREAKER_THRESHOLD = 5

# Pages of results one author scan reads at most (≤ 20 volumes each).
_MAX_PAGES = 5


class GoogleBooksSource(BaseSource):
    name = "google_books"
    default_headers = {
        "Accept": "application/json",
    }
    default_timeout = 15.0

    def __init__(self, rate_limit: float = 1.5, api_key: str = ""):
        super().__init__(rate_limit=rate_limit)
        # Consecutive 429 counter for the circuit breaker. Instance-level
        # so `reload_sources()` (fired on every settings save) resets it
        # when the user re-enables Google Books in Settings — without that
        # reset, re-enabling after a trip would immediately re-trip on
        # the first request because the counter would still be past the
        # threshold.
        self._consecutive_429s = 0
        # v2.10.7 — Google Books API key. The keyed endpoint has a
        # much more generous quota than the no-key public endpoint
        # (which keeps hitting 429 on modest scans). When set, every
        # request gets `?key=…` appended via _request_params(). Empty
        # string preserves the legacy no-key behavior so the source
        # still works on installs without a configured key.
        self.api_key = (api_key or "").strip()

    def _request_params(self, base: dict) -> dict:
        """Merge the API key into a request params dict if configured.
        Returns a new dict so the caller's `base` isn't mutated."""
        out = dict(base)
        if self.api_key:
            out["key"] = self.api_key
        return out

    def update_api_key(self, key: str) -> None:
        """Force the api_key to update (mirrors HardcoverSource).

        lookup.py injects the freshest key from the encrypted secrets
        store as a per-source pre-flight on every scan, so a key
        rotation in the Settings UI takes effect on the next scan
        without restarting the process.
        """
        self.api_key = (key or "").strip()

    async def _get(self, url: str, retries: int = 0, **kwargs):
        """GET with Google Books' retry rules, each attempt in its turn.

        - **429 (quota exhausted)**: fail fast, don't retry. Retrying on
          429 just burns the quota faster — better to surface the failure
          immediately and let the enricher fall through to the next source.
          Counts toward the circuit breaker.
        - **5xx (transient server error)**: retry up to 2 times, 3s then
          6s plus jitter (`retry_delay`). Google's `backendFailed` 503s
          usually clear on the first retry.
        - **Other / network errors**: same retry path; httpx surfaces
          connect errors as exceptions without an HTTP code, which
          generally indicates a transient issue worth retrying.

        One warning when the request finally fails. Until the 2026-10
        audit every 503 also logged the base class's "GIVING UP … after
        1 attempts" before the retry that usually succeeded.

        Also implements the auto-disable circuit breaker: tracks
        consecutive 429 responses and switches Google Books off in
        `metadata_sources` when the threshold trips, so the very next
        author skips it instead of burning 60s on a guaranteed-to-fail
        request.
        """
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                async with source_gate.turn(self.name) as turn:
                    resp = await self.client.get(url, **kwargs)
                    turn.status(resp.status_code)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response is not None else 0
                if status == 429:
                    # Quota exhausted — never retry, count toward circuit breaker
                    self._consecutive_429s += 1
                    if self._consecutive_429s >= _CIRCUIT_BREAKER_THRESHOLD:
                        self._trip_circuit_breaker()
                    logger.warning(f"  google_books: HTTP 429 (quota) — {_redact_sensitive(e)}")
                    raise
                if status >= 500 and attempt < max_attempts - 1:
                    backoff = retry_delay(attempt)
                    logger.debug(
                        f"  google_books: {status} on attempt {attempt+1}/{max_attempts} "
                        f"— retrying in {backoff:.1f}s ({_redact_sensitive(e)})"
                    )
                    await asyncio.sleep(backoff)
                    continue
                logger.warning(
                    f"  google_books: GIVING UP after {attempt+1} attempt(s): "
                    f"{_redact_sensitive(e)}"
                )
                raise
            except Exception as e:
                # Network / timeout / other transient — retry the same as 5xx
                if attempt < max_attempts - 1:
                    backoff = retry_delay(attempt)
                    logger.debug(
                        f"  google_books: network error on attempt "
                        f"{attempt+1}/{max_attempts} — retrying in "
                        f"{backoff:.1f}s ({_redact_sensitive(e)})"
                    )
                    await asyncio.sleep(backoff)
                    continue
                logger.warning(
                    f"  google_books: GIVING UP after {attempt+1} attempt(s): "
                    f"{_redact_sensitive(e)}"
                )
                raise
            # Success path: any 2xx clears the 429 counter
            self._consecutive_429s = 0
            return resp

    async def _search_pages(self, author_name: str) -> list[dict]:
        """Every volume the free-text search returns for the quoted name,
        up to `_MAX_PAGES` pages. A failed first page returns nothing; a
        failed later page keeps what came before."""
        phrase = '"' + author_name.replace('"', " ").strip() + '"'
        items_all: list[dict] = []
        start = 0
        for page in range(_MAX_PAGES):
            try:
                resp = await self._get(
                    _API,
                    params=self._request_params({
                        "q": phrase,
                        "maxResults": "40",
                        "printType": "books",
                        "startIndex": str(start),
                    }),
                )
                data = resp.json()
            except Exception:
                break
            items = data.get("items") or []
            if not items:
                break
            items_all.extend(items)
            start += len(items)
            matching = sum(
                1 for it in items
                if author_overlap((it.get("volumeInfo") or {}).get("authors", []), author_name) >= 0.5
            )
            if matching == 0 or start >= int(data.get("totalItems") or 0):
                break
        return items_all

    def _trip_circuit_breaker(self) -> None:
        """Auto-disable Google Books in settings after repeated 429s.

        Flips the Google Books scan+enrich toggles to False inside the
        Phase-7 `metadata_sources` dict (lookup.py reads from there),
        logs a WARNING, and timestamps the trip in
        `google_books_auto_disabled_at`. Safe to call multiple times —
        if both toggles are already off (a prior trip on this process),
        this is a no-op. The user re-enables via the Metadata Sources
        panel when quota resets; the PATCH triggers `reload_sources()`
        which builds a fresh source instance with counter=0.
        """
        from app.config import load_settings, save_settings
        s = load_settings()
        sources = s.get("metadata_sources") or {}
        entry = dict(sources.get("google_books") or {})
        already_off = not (entry.get("ebook_scan") or entry.get("ebook_enrich")
                           or entry.get("audiobook_scan") or entry.get("audiobook_enrich"))
        if already_off:
            return  # already off — another caller tripped first
        entry["ebook_scan"] = False
        entry["ebook_enrich"] = False
        entry["audiobook_scan"] = False
        entry["audiobook_enrich"] = False
        sources["google_books"] = entry
        s["metadata_sources"] = sources
        s["google_books_auto_disabled_at"] = time.time()
        save_settings(s)
        logger.warning(
            f"Google Books auto-disabled after {self._consecutive_429s} "
            f"consecutive 429 responses (API quota likely exhausted). "
            f"Re-enable in Settings when quota resets."
        )

    async def search_author(self, author_name: str) -> Optional[AuthorResult]:
        """Search Google Books for an author's books: the one search per
        author, returned inline (lookup merges them without asking
        `get_author_books`). None when nothing by them came back. Until the
        2026-10 audit this made its own 5-result query and then
        `get_author_books` searched again."""
        found = await self.get_author_books(author_name)
        if found is None or not (found.books or found.series):
            # Results, but none by this author: no author. An empty result
            # would send lookup to `get_author_books` for the same search
            # again (seen live, 2026-10-09).
            return None
        return found

    async def get_author_books(
        self, author_id: str,
        existing_titles: set = None,
        owned_titles: list = None,
        owned_only: bool = False,
    ) -> Optional[AuthorResult]:
        """Fetch an author's books: the quoted name as free text, paged by
        the items actually returned (G55). `author_id` is the name."""
        author_name = author_id
        existing_titles = existing_titles or set()
        owned_titles = owned_titles or []

        all_items = await self._search_pages(author_name)
        if not all_items:
            return None

        # Deduplicate by Google volume ID
        seen_ids = set()
        unique_items = []
        for item in all_items:
            vid = item.get("id", "")
            if vid and vid not in seen_ids:
                seen_ids.add(vid)
                unique_items.append(item)

        books = []
        series_map = {}

        for item in unique_items:
            vi = item.get("volumeInfo", {})
            title = vi.get("title", "")
            if not title:
                continue

            item_authors = vi.get("authors", [])
            # v2.12.0 namesake gate. The old `score_match` call here
            # compared `record_title=title, search_title=title` —
            # trivially 1.0 for title similarity, so the composite
            # score never dropped below the 0.3 threshold regardless
            # of author mismatch. Result: indie author searches
            # surfaced unrelated books by other authors sharing part
            # of the queried name (UAT 2026-05-14: "Colin Graves"
            # got 19 google_books "new books" most of which were by
            # a different Colin Graves; Hardcover + Kobo correctly
            # rejected the same namesake via the lookup-layer
            # `_validate_author` gate).
            #
            # `author_overlap` returns the fraction of the queried
            # author's name parts that appear in the record's authors
            # list, after `normalize_author`. We require ≥0.5 — gives
            # a little tolerance for typographic variants without
            # admitting unrelated namesakes.
            if author_overlap(item_authors, author_name) < 0.5:
                continue

            # ISBN: prefer ISBN_13, fall back to ISBN_10
            isbn = None
            for ident in vi.get("industryIdentifiers", []):
                if ident.get("type") == "ISBN_13":
                    isbn = ident["identifier"]
                    break
                if ident.get("type") == "ISBN_10" and not isbn:
                    isbn = ident["identifier"]

            # Cover: upgrade thumbnail URL for best quality
            cover_url = None
            images = vi.get("imageLinks", {})
            for key in ("large", "medium", "small", "thumbnail", "smallThumbnail"):
                if images.get(key):
                    cover_url = _upgrade_cover(images[key])
                    break

            # Series: parse from title parenthetical
            series_name, series_index = _parse_series_from_title(title)

            # Language: Google uses ISO 639-1 codes
            lang = vi.get("language")

            # Strip HTML from description
            desc = vi.get("description")
            if desc:
                desc = re.sub(r"<[^>]+>", "", desc).strip()[:2000]

            bk = BookResult(
                title=title,
                series_name=series_name,
                series_index=series_index,
                isbn=isbn,
                cover_url=cover_url,
                pub_date=vi.get("publishedDate"),
                description=desc,
                page_count=vi.get("pageCount"),
                language=lang,
                external_id=item.get("id", ""),
                source="google_books",
                source_url=vi.get("infoLink") or vi.get("canonicalVolumeLink"),
                # v3.0.0 Phase 3.5 — `volumeInfo.authors` is a flat name
                # list with no role signal, so each entry is treated as the
                # plain author role (role=None). Google Books is LINK-ONLY
                # (not in TRUSTED_CREATE_SOURCES): `_link_discovered_contributors`
                # resolves these against EXISTING author rows and never mints,
                # which is the safeguard against its untyped lists pulling in
                # non-authors. No per-author IDs exposed by the API.
                contributors=[Contributor(name=a) for a in item_authors if a],
            )

            if series_name:
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


def _upgrade_cover(url: str) -> str:
    """Upgrade Google Books thumbnail URL for maximum quality."""
    url = re.sub(r"zoom=\d", "zoom=0", url)
    url = re.sub(r"&edge=curl", "", url)
    url = url.replace("http://", "https://")
    return url


def _parse_series_from_title(title: str) -> tuple:
    """Extract series info from title parenthetical.

    Google Books encodes series as:
      "The Way of Kings (The Stormlight Archive, #1)"
      "Mistborn: The Final Empire"

    Returns (series_name, index) or (None, None).
    """
    m = re.search(r"\((.+?),?\s*#(\d+(?:\.\d+)?)\)\s*$", title)
    if m:
        try:
            return m.group(1).strip(), float(m.group(2))
        except ValueError:
            return m.group(1).strip(), None
    return None, None
