"""
v2.21.0 Phase C — metadata cache reader.

`CachedSource` is the drop-in replacement for a live discovery source
when that source has its work mirrored into a per-source SQLite cache
(see `app/discovery/metadata_cache.py`).

## Why this exists

For Amazon, the live source class (`AmazonSource`) hits amazon.com on
every synchronous scan and trips Akamai IP-level penalty boxes when
density gets high. The cache architecture (v2.21.0) extracts the
scanning into a paced background worker (Phase D) and serves the
user-facing scan from a SQLite mirror.

The cache reader exposes the same `search_author` /
`get_author_books` shape the dispatcher already knows how to call,
so the swap is transparent. lookup.py keeps its same source-walking
loop; only the `amazon` singleton swaps from `AmazonSource` (live)
to `CachedSource(source_name="amazon")` (cache-backed).

## Behavior

- `search_author(name)` always returns None. Synchronous name→ID
  resolution is gone — the worker does it offline. lookup.py's
  stored-ID short-circuit (`authors.amazon_id` already populated)
  is what feeds `author_id` into `get_author_books`.

- `get_author_books(amazon_author_id, ...)`:
  * Cache HIT for (id, active library_slug): build an AuthorResult
    from the cached books, applying read-time filters for language
    + format + owned-only.
  * Cache MISS: idempotent INSERT OR IGNORE into the worker queue
    so the next worker iteration picks this author up. Return None.
    The synchronous scan moves on to the next source.

Filters apply at READ time so a settings change (English-only,
audiobook-only, etc.) takes effect immediately without re-scanning
Amazon. The cache stores everything Amazon returned.

## Phase C limits

- The worker (Phase D) doesn't exist yet, so the cache stays empty
  for now. Every Amazon lookup returns None + enqueues. End users
  observe: Amazon scans yield nothing, but no soft-block cascades.
- Once Phase D ships, the worker populates the cache and lookups
  start returning data.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from app.discovery import metadata_cache
from app.discovery.database import get_active_library
from app.discovery.sources.amazon_widget_parser import FILTER_TO_BINDING
from app.discovery.sources.base import AuthorResult, BookResult, SeriesResult


logger = logging.getLogger("seshat.discovery.metadata_cache_reader")


# Re-export so callers don't have to import both modules.
SOURCE_AMAZON = metadata_cache.SOURCE_AMAZON


# Heuristic copied from `app/discovery/sources/amazon.py`. A 10-char
# uppercase alphanumeric value is an Amazon Author Store ID; anything
# else is a legacy name-stored-as-id from pre-v2.11.0 installs.
import re as _re
_AUTHOR_ID_RE = _re.compile(r"^[A-Z0-9]{10}$")


def _is_amazon_author_id(value: str) -> bool:
    return bool(value) and bool(_AUTHOR_ID_RE.match(value))


def _norm(s: Optional[str]) -> str:
    """Lowercase + collapse whitespace for case-insensitive matching."""
    return (s or "").strip().lower()


def _language_matches(book_language: Optional[str], wanted: str) -> bool:
    """Read-time language filter.

    Permissive: empty/unknown book languages pass through (matches the
    `_lang_ok` behavior in lookup.py — we don't drop a book just
    because Amazon didn't tag a language). 'All Languages' disables
    the filter entirely.
    """
    if not wanted or _norm(wanted) in ("all", "all languages", ""):
        return True
    if not book_language:
        return True
    return _norm(wanted) in _norm(book_language) or _norm(book_language) in _norm(wanted)


def _format_matches(book_format: Optional[str], wanted: str) -> bool:
    """Read-time format filter.

    'allFormats' (Amazon's "any") disables. Empty book format passes.
    Otherwise normalize-equal both sides after translating `wanted`
    through FILTER_TO_BINDING — the live AmazonSource takes the
    filter-input shape (`kindle` / `audible_audiobook` / `paperback`
    / etc.) but the worker writes the binding-symbol shape
    (`kindle_edition` / `audio_download` / `paperback` / etc.) into
    the cache's `format` column. Without the translation, the
    default "kindle" filter would never match the cached
    "kindle_edition" rows.

    Falls back to a direct compare when `wanted` already looks like
    a binding symbol (tests + callers that pass the cache's stored
    shape directly).
    """
    if not wanted or _norm(wanted) in ("all", "allformats", ""):
        return True
    if not book_format:
        return True
    translated = FILTER_TO_BINDING.get(wanted, wanted)
    return _norm(book_format) == _norm(translated)


def _owned_filter(
    book_title: str, owned_titles: list[str], owned_only: bool,
) -> bool:
    """Read-time owned-only filter.

    Mirrors lookup.py's `_title_match` shape — case-insensitive
    sub-string-fuzzy match between the cached book title and the
    library's owned-title list. Permissive on edge cases; the merge
    layer downstream does the authoritative dedup.
    """
    if not owned_only:
        return True
    if not owned_titles:
        return False  # owned_only AND nothing owned → drop everything
    bt = _norm(book_title)
    for ot in owned_titles:
        n = _norm(ot)
        if not n:
            continue
        if n == bt or n in bt or bt in n:
            return True
    return False


def _row_to_book_result(row: Any, source_name: str) -> BookResult:
    """Build a BookResult from one cache row.

    Mirrors `AmazonSource._product_to_book` so downstream merge layer
    routes `external_id` to `books.amazon_id` via the existing
    `f"{source}_id"` UPDATE pattern.
    """
    # raw_json may store auxiliary fields (e.g. amazon_format_asins
    # serialization). Empty / missing is fine.
    extra: dict[str, Any] = {}
    raw = row["raw_json"]
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                extra = decoded
        except (TypeError, ValueError):
            extra = {}
    return BookResult(
        title=row["title"] or "",
        series_name=row["series_name"],
        series_index=row["series_pos"],
        isbn=row["isbn"],
        cover_url=row["cover_url"],
        pub_date=row["pub_date"],
        language=row["language"],
        external_id=row["book_asin"],
        source=source_name,
        source_url=extra.get("source_url") if isinstance(extra, dict) else None,
        amazon_format_asins=(
            extra.get("amazon_format_asins") if isinstance(extra, dict) else None
        ),
    )


def _assemble_author_result(
    *, author_name: str, author_id: str, book_rows: list[Any],
    source_name: str,
) -> AuthorResult:
    """Group cached books by series → AuthorResult. Mirrors
    `AmazonSource._build_author_result` so the merge-side shape is
    indistinguishable between live + cached reads."""
    series_map: dict[str, SeriesResult] = {}
    standalone: list[BookResult] = []
    for row in book_rows:
        book = _row_to_book_result(row, source_name)
        if book.series_name:
            series = series_map.get(book.series_name)
            if series is None:
                series = SeriesResult(name=book.series_name, books=[])
                series_map[book.series_name] = series
            series.books.append(book)
        else:
            standalone.append(book)
    return AuthorResult(
        name=author_name,
        external_id=author_id,
        books=standalone,
        series=list(series_map.values()),
    )


# ─── Queue helpers ──────────────────────────────────────────────


async def ensure_enqueued(
    *,
    source_name: str,
    author_id: str,
    library_slug: str,
    seshat_author_id: Optional[int] = None,
    priority: float = 100.0,
    enqueued_reason: str = "lookup_miss",
) -> bool:
    """Idempotent INSERT OR IGNORE into the source's worker queue.

    Returns True when a NEW row landed, False when one already existed.
    Used by `CachedSource.get_author_books` on cache miss so the worker
    picks the author up without the caller needing to know whether the
    Phase B backfill already covered them.
    """
    qt = metadata_cache.queue_table(source_name)
    db = await metadata_cache.get_db(source_name)
    try:
        before_cur = await db.execute(
            f"SELECT COUNT(*) FROM {qt} "
            f"WHERE author_id = ? AND library_slug = ?",
            (author_id, library_slug),
        )
        existed = (await before_cur.fetchone())[0] > 0
        if existed:
            return False
        await db.execute(
            f"INSERT OR IGNORE INTO {qt} "
            f"(author_id, library_slug, seshat_author_id, priority, "
            f"enqueued_reason, next_scan_due_at) "
            f"VALUES (?, ?, ?, ?, ?, ?)",
            (
                author_id, library_slug, seshat_author_id,
                priority, enqueued_reason, 0.0,
            ),
        )
        await db.commit()
        return True
    finally:
        await db.close()


# ─── Read API ──────────────────────────────────────────────────


async def read_cached_author(
    *,
    source_name: str,
    author_id: str,
    library_slug: str,
    language: str,
    format_filter: str,
    owned_titles: list[str],
    owned_only: bool,
) -> Optional[AuthorResult]:
    """Return an AuthorResult assembled from cached rows, or None on
    cache miss.

    Cache miss = no row in `metadata_cache_<source>_state` for this
    (author_id, library_slug). Cache hit but zero books surviving
    filters returns an empty AuthorResult so the caller can
    distinguish "no books matched filters" from "never scanned".
    """
    st = metadata_cache.state_table(source_name)
    bt = metadata_cache.books_table(source_name)
    db = await metadata_cache.get_db(source_name)
    try:
        state_cur = await db.execute(
            f"SELECT last_scanned_at, last_outcome, book_count "
            f"FROM {st} WHERE author_id = ? AND library_slug = ?",
            (author_id, library_slug),
        )
        state_row = await state_cur.fetchone()
        if state_row is None:
            return None  # cache miss
        books_cur = await db.execute(
            f"SELECT author_id, library_slug, book_asin, title, "
            f"series_name, series_pos, pub_date, format, language, "
            f"isbn, cover_url, raw_json, cached_at "
            f"FROM {bt} WHERE author_id = ? AND library_slug = ?",
            (author_id, library_slug),
        )
        book_rows = await books_cur.fetchall()
    finally:
        await db.close()

    # Apply read-time filters.
    filtered: list[Any] = []
    for row in book_rows:
        if not _language_matches(row["language"], language):
            continue
        if not _format_matches(row["format"], format_filter):
            continue
        if not _owned_filter(row["title"] or "", owned_titles, owned_only):
            continue
        filtered.append(row)

    return _assemble_author_result(
        author_name=author_id,  # placeholder; merge layer doesn't care
        author_id=author_id,
        book_rows=filtered,
        source_name=source_name,
    )


# ─── Drop-in source class ──────────────────────────────────────


class CachedSource:
    """Cache-backed discovery source. Shape-compatible with
    `BaseSource` subclasses; lookup.py + the dispatcher treat it
    identically to a live source.

    Construction does NOT pull settings — call `update_config(...)`
    after init, or pass the relevant kwargs at construction (mirrors
    AmazonSource's `format_filter` / `language` / etc).
    """

    EBOOK = "ebook"
    AUDIOBOOK = "audiobook"

    def __init__(
        self,
        *,
        source_name: str = SOURCE_AMAZON,
        format_filter: str = "kindle",
        audiobook_format_filter: str = "audible_audiobook",
        language: str = "English",
    ):
        self.source_name = source_name
        self.format_filter = format_filter
        self.audiobook_format_filter = audiobook_format_filter
        self.language = language
        # Attributes lookup.py / _try_source assigns directly on the
        # instance. Pre-create here so dataclass-style attribute access
        # never AttributeErrors.
        self._on_book = None
        self._on_new_candidate = None
        self._content_type = self.EBOOK
        self._linked_author_names: list[str] = []
        self._owned_titles: list[str] = []
        self._owned_series_names: list[str] = []
        # `_partial_state` is the Goodreads retry-resume hook. Cache
        # reads complete in one shot, so this stays None forever; the
        # retry loop's `partial = getattr(...)` skips us cleanly.
        self._partial_state = None

    # `name` is the source-key the merge layer + dispatcher key on.
    # Keep it bound to source_name so a future `CachedSource(source_name="goodreads")`
    # automatically becomes the goodreads source.
    @property
    def name(self) -> str:
        return self.source_name

    def _active_format_filter(self) -> str:
        """Mirror AmazonSource — pick ebook vs audiobook based on
        `_content_type` set by the dispatcher."""
        if self._content_type == self.AUDIOBOOK:
            return self.audiobook_format_filter
        return self.format_filter

    async def close(self) -> None:
        """No-op (we hold no HTTP client). Implemented for parity with
        BaseSource so dispatcher cleanup doesn't AttributeError."""
        return None

    async def search_author(
        self, author_name: str, **_kwargs: Any,
    ) -> Optional[AuthorResult]:
        """No synchronous name→ID resolution.

        Pre-v2.21.0 AmazonSource.search_author drove the resolver
        chain (vanity URL → search → DDG). All three hit amazon.com
        live; v2.21.0 explicitly moves that work into the background
        worker (Phase D). Returning None here means lookup.py's
        next-source loop proceeds — Amazon is silent until either:
          (a) the user manually populates `authors.amazon_id` and
              the stored-ID short-circuit (lookup.py:2790) picks it
              up, then `get_author_books` reads cache; OR
          (b) the worker resolves the ID + scans + caches it.
        """
        del author_name, _kwargs
        return None

    async def get_author_books(
        self,
        author_id: str,
        existing_titles: Optional[set] = None,
        owned_titles: Optional[list] = None,
        owned_only: bool = False,
        start_at: int = 0,
        **_extra: Any,
    ) -> Optional[AuthorResult]:
        """Serve from cache; enqueue + return None on miss.

        Args mirror BaseSource.get_author_books — see that for the
        general contract. `start_at` is accepted for signature parity
        with the Goodreads resume hook but never consulted (cache
        reads complete atomically).
        """
        del existing_titles, start_at, _extra  # not used for cache reads
        slug = get_active_library() or ""
        if not slug:
            logger.debug(
                "%s cache reader: no active library slug; skipping",
                self.source_name,
            )
            return None

        # Legacy state: authors.amazon_id occasionally stores a name
        # from the pre-v2.11.0 AmazonSource implementation. We can't
        # cache-lookup with a name, and we don't synchronously resolve
        # — the worker will pick this author up via the queue.
        if self.source_name == SOURCE_AMAZON and not _is_amazon_author_id(author_id):
            return None

        result = await read_cached_author(
            source_name=self.source_name,
            author_id=author_id,
            library_slug=slug,
            language=self.language,
            format_filter=self._active_format_filter(),
            owned_titles=owned_titles or [],
            owned_only=owned_only,
        )

        if result is None:
            # Cache miss — enqueue so the worker picks it up. We
            # don't have a clean seshat_author_id at this layer (we
            # could query authors.amazon_id reverse, but it's not
            # required for the worker — it can re-resolve when it
            # processes the row).
            new_row = await ensure_enqueued(
                source_name=self.source_name,
                author_id=author_id,
                library_slug=slug,
                priority=1000.0,  # user-bumped — popped before
                                  # the v2.21.0 backfill rows
                enqueued_reason="lookup_miss",
            )
            logger.info(
                "%s cache reader: MISS for %s in %s — %s for worker",
                self.source_name, author_id, slug,
                "enqueued" if new_row else "queue row already present",
            )
            return None

        n_books = len(result.books) + sum(len(s.books) for s in result.series)
        logger.debug(
            "%s cache reader: HIT for %s in %s — %d book(s) after filters",
            self.source_name, author_id, slug, n_books,
        )
        return result


# ─── Module-level convenience constructor ──────────────────────


def make_amazon_cached_source(
    *,
    format_filter: str = "kindle",
    audiobook_format_filter: str = "audible_audiobook",
    language: str = "English",
) -> CachedSource:
    """Build a CachedSource pre-configured for Amazon.

    Kept tiny so lookup.py can call this with the same shape it used
    to call `AmazonSource(...)`. The non-Amazon kwargs (rate_limit,
    burst_delay_s, use_ddg_fallback) are deliberately absent — the
    cache reader doesn't make HTTP requests, so they don't apply.
    """
    return CachedSource(
        source_name=SOURCE_AMAZON,
        format_filter=format_filter,
        audiobook_format_filter=audiobook_format_filter,
        language=language,
    )


# Suppress unused-import that helps IDE auto-imports for users
# building on this module.
_ = time
