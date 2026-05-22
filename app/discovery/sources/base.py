"""
Base classes and result types for discovery source plugins.

Any new source plugin should:
1. Subclass BaseSource
2. Set name = "yoursource" as a class attribute
3. Set default_headers, default_timeout, follow_redirects as needed
4. Implement search_author() and get_author_books()
5. Use self._get(url) for rate-limited GETs with retry logic
6. Override _get_client() if custom auth/header logic is needed
7. Call await self.close() in cleanup

See goodreads.py for a reference implementation using the standard GET flow,
hardcover.py for a GraphQL POST override, and mam.py for a completely custom
HTTP implementation.
"""
from dataclasses import dataclass, field
from typing import Optional
import asyncio
import logging
import re
import httpx


_SENSITIVE_QUERY_PARAMS = ("key", "apikey", "api_key", "token", "password", "secret")
_REDACT_RX = re.compile(
    r"([?&](?:" + "|".join(_SENSITIVE_QUERY_PARAMS) + r"))=[^&\s'\"]*",
    re.IGNORECASE,
)


def _redact_sensitive(s: object) -> str:
    """Strip sensitive query-string params (api keys, tokens) before
    they hit logs. httpx exception messages include the full request
    URL with params, which leaks Google Books / future-keyed-source
    secrets into operator-visible logs."""
    return _REDACT_RX.sub(r"\1=REDACTED", str(s))


# ─── Result Types ────────────────────────────────────────────

@dataclass
class BookResult:
    title: str
    series_name: Optional[str] = None
    series_index: Optional[float] = None
    isbn: Optional[str] = None
    cover_url: Optional[str] = None
    pub_date: Optional[str] = None
    expected_date: Optional[str] = None
    is_unreleased: bool = False
    description: Optional[str] = None
    page_count: Optional[int] = None
    external_id: Optional[str] = None
    language: Optional[str] = None
    source: str = ""
    source_url: Optional[str] = None
    # v2.11.1: Amazon-specific. JSON-encoded `{binding_symbol: asin}`
    # map from the Author-Store `mediaMatrix` cross-references. Lets
    # us record the Kindle/Hardcover/Paperback/Audible ASIN siblings
    # of a canonical work in one column so the UI can offer "switch
    # format" later. Only AmazonSource populates this today; other
    # sources leave it None and the merge layer no-ops.
    amazon_format_asins: Optional[str] = None
    # v2.21.0 schema-v2: the source's binding shape for this book
    # (e.g. `kindle_edition`, `audio_download`, `paperback`,
    # `hardcover` for Amazon; left None for sources that don't
    # expose a binding). The metadata cache stores this verbatim so
    # the cache reader can filter per-content-type at read time
    # WITHOUT relying on the scan-time format filter that the
    # worker used. Required for the v2 per-author-scan workflow,
    # where a single scan returns mixed bindings and each book row
    # needs to identify its own.
    format: Optional[str] = None
    # v2.16.0 Gap 1 — cross-source identifiers a source happens to
    # know for the book it's emitting. Hardcover surfaces these via
    # its `book_mappings` table (goodreads/openlibrary/google
    # platforms). The merge layer COALESCE-fills any column whose
    # current value is NULL, so a Hardcover scan can seed the
    # `goodreads_id` that Phase-2 author backfill later reverse-
    # looks-up to enrich `authors.goodreads_id`. None for sources
    # that don't expose cross-source IDs.
    goodreads_id: Optional[str] = None
    openlibrary_id: Optional[str] = None
    google_books_id: Optional[str] = None


@dataclass
class SeriesResult:
    name: str
    total_books: Optional[int] = None
    description: Optional[str] = None
    external_id: Optional[str] = None
    books: list[BookResult] = field(default_factory=list)


@dataclass
class AuthorResult:
    name: str
    bio: Optional[str] = None
    image_url: Optional[str] = None
    external_id: Optional[str] = None
    books: list[BookResult] = field(default_factory=list)
    series: list[SeriesResult] = field(default_factory=list)


# ─── Base Source Class ───────────────────────────────────────

class BaseSource:
    """Base class for all metadata source plugins.

    Subclasses typically set class attributes (name, default_headers, etc.)
    and implement search_author() / get_author_books(). Shared concerns like
    HTTP client management, rate limiting, and retry logic live here.

    Class attributes to override:
        name: Short identifier used for logging (e.g., "goodreads", "hardcover")
        default_headers: Dict of HTTP headers used for all requests
        default_timeout: httpx timeout in seconds
        follow_redirects: Whether httpx should auto-follow 3xx redirects
    """
    # ── Override these class attributes in subclasses ──
    name: str = "base"
    default_headers: dict = {}
    default_timeout: float = 30.0
    follow_redirects: bool = True

    def __init__(self, rate_limit: float = 2.0):
        """Initialize the source.

        Args:
            rate_limit: Seconds to wait before each request (throttles requests
                       to avoid getting rate-limited by the source API).
        """
        self.rate_limit = rate_limit
        self.logger = logging.getLogger(f"seshat.discovery.{self.name}")
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-create the HTTP client.

        The default implementation uses the class attributes for headers,
        timeout, and redirect following. Override in subclasses that need
        custom authentication logic (e.g., Hardcover's Bearer token, MAM's
        Cookie header) or request-time client recreation.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.default_timeout,
                headers=self.default_headers,
                follow_redirects=self.follow_redirects,
            )
        return self._client

    @property
    def client(self) -> httpx.AsyncClient:
        """Convenience accessor for the HTTP client (lazy-initialized on first use)."""
        return self._get_client()

    async def _get(self, url: str, retries: int = 2, **kwargs) -> httpx.Response:
        """HTTP GET with per-source rate limiting and retry/backoff.

        Waits `rate_limit` seconds before each attempt. On failure,
        retries up to `retries` times with exponentially increasing
        backoff: 3s → 6s → 12s, capped at 12s. The exponential
        schedule clears Goodreads' rate-limit window (~6s recovery)
        which a fixed shorter backoff would miss, causing real
        503-affected books to silently get skipped.

        Subclasses with custom request patterns (POST, GraphQL, etc.) can
        either call this with their own URL construction or implement their
        own request method — see hardcover.py's _query() for an example.

        Args:
            url: Full URL to fetch
            retries: Number of retry attempts after the initial request
                     (default 2 → 3 total attempts)
            **kwargs: Additional arguments passed to httpx.AsyncClient.get()
                     (e.g., params, headers for per-request overrides)

        Returns:
            The httpx.Response object on success

        Raises:
            Whatever httpx raises on the final failed attempt — and emits
            a logger.warning so silent skips become visible in container
            logs without needing verbose mode.
        """
        for attempt in range(retries + 1):
            try:
                await asyncio.sleep(self.rate_limit)
                resp = await self.client.get(url, **kwargs)
                resp.raise_for_status()
                return resp
            except Exception as e:
                if attempt < retries:
                    backoff = min(3 * (2 ** attempt), 12)  # 3, 6, 12, 12, ...
                    self.logger.debug(
                        f"  {self.name}: attempt {attempt+1}/{retries+1} failed "
                        f"for {url}: {_redact_sensitive(e)} — retrying in {backoff}s"
                    )
                    await asyncio.sleep(backoff)
                    continue
                # Surface terminal failure as a WARNING instead of letting
                # it disappear silently. Caller still gets the exception.
                self.logger.warning(
                    f"  {self.name}: GIVING UP on {url} after {retries+1} "
                    f"attempts: {_redact_sensitive(e)}"
                )
                raise

    async def close(self):
        """Clean up the HTTP client. Safe to call multiple times.

        Should be called when the source is no longer needed (e.g., during
        app shutdown or when reloading sources after settings changes).
        """
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # ── Abstract methods (override in subclasses) ──

    async def search_author(self, author_name: str) -> Optional[AuthorResult]:
        """Search for an author by name and return their info + books.

        Override in subclasses. The default implementation raises
        NotImplementedError.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement search_author()"
        )

    async def get_author_books(self, author_id: str) -> Optional[AuthorResult]:
        """Fetch an author's books given an external source ID.

        Override in subclasses. The default implementation raises
        NotImplementedError.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement get_author_books()"
        )
