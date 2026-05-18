"""
Hardcover.app source — GraphQL-backed metadata provider.

Hardcover is the only source with a real public API (GraphQL at
api.hardcover.app/v1/graphql), so this module skips HTML scraping
entirely and uses two queries per author scan:

  1. SEARCH_QUERY (`query_type: "Book"`) — returns a flat list of
     book IDs. Fired multiple times per author: once for the bare
     author name plus once per owned title in `owned_titles[:25]`.
     The result IDs are unioned and capped at 100.
  2. FIND_BOOKS_BY_IDS — pulls full metadata (book + first edition)
     for the entire ID set in a single round-trip, with English
     editions preferred via the `language.code3` filter.

Two non-obvious things in this module worth knowing about:

  - **Search expansion**: a bare-name query returns at most ~10
    results, dominated by samplers and edge-case omnibuses for
    prolific authors. To find a real Sanderson catalog we have to
    fan out queries against the user's owned titles too. The IDs
    are accumulated across ALL queries; do not early-break when
    one query returns hits.
  - **Namesake disambiguation**: Hardcover often has multiple
    authors who share a name (the "David Burke" problem — three
    distinct people, each with their own book list). The
    `search_author` function tallies each book's
    `contributions[].author.id`, vote-ranks them by owned-title
    overlap, and keeps only the winning ID's books before merging.
    Without this filter, the merge would silently glue another
    person's catalog onto the user's author.

Auth: the API key goes into the `Authorization` header. Users paste
the raw token from hardcover.app; we add the `Bearer ` prefix if it's
not already present.
"""
import asyncio
import re
import httpx, logging
from typing import Optional
from app.discovery.sources.base import BaseSource, AuthorResult, BookResult, SeriesResult

logger = logging.getLogger("seshat.discovery.hardcover")
API = "https://api.hardcover.app/v1/graphql"


# ─── Series-name normalization for owned-set matching ──────────────────
# Strip leading articles ("the", "a"), trailing series-suffix words
# ("saga", "series", "trilogy", "cycle", "chronicles", "novels"), all
# punctuation, and lowercase. Lets us match "The Mistborn Saga" against
# "Mistborn", "Mistborn Saga", "the mistborn saga", etc.
_RX_SERIES_LEAD = re.compile(r'^(the|a|an)\s+', re.IGNORECASE)
_RX_SERIES_TAIL = re.compile(
    r'\s+(saga|series|trilogy|cycle|chronicles|novels|books)\s*$',
    re.IGNORECASE,
)
_RX_SERIES_PUNCT = re.compile(r'[^\w\s]')


def _pick_hardcover_cover(book: dict, edition: dict) -> Optional[str]:
    """Pick the best cover URL from a Hardcover book + edition.

    Prefer the edition's `cached_image` (most specific to the exact
    printing we matched on), fall back to the book-level
    `cached_image` when the edition's is null. Hardcover populates
    `edition.image` inconsistently — older editions, print-only ones,
    and some imports lack it even though the book's canonical image
    is set. Without this fallback every such book stayed coverless
    until another source filled it in, even though Hardcover had
    the data the whole time.

    Each image field may be a `{"url": ...}` dict (current schema)
    or a bare string URL (older API revisions). Both shapes handled.
    """
    for img in (edition.get("image"), book.get("image")):
        if isinstance(img, dict):
            url = img.get("url")
            if url:
                return url
        elif isinstance(img, str) and img:
            return img
    return None


def _norm_series(name: str) -> str:
    if not name:
        return ""
    n = name.strip()
    n = _RX_SERIES_LEAD.sub('', n)
    # Apply tail-strip iteratively in case of "The Mistborn Saga Series"
    for _ in range(3):
        new_n = _RX_SERIES_TAIL.sub('', n)
        if new_n == n:
            break
        n = new_n
    n = _RX_SERIES_PUNCT.sub(' ', n).lower()
    return re.sub(r'\s+', ' ', n).strip()


def _pick_best_series(candidates: list, owned_norms: set) -> dict:
    """Pick the best series candidate for a Hardcover book.

    Hardcover frequently returns multiple `book_series` entries for
    the same book at different levels of taxonomy: Sanderson's
    Mistborn novels come back as `['The Mistborn Saga: The Original
    Trilogy', 'The Mistborn Saga', 'The Cosmere']`. Picking the most-
    nested or most-detailed name is wrong — the user's Calibre data
    uses "The Mistborn Saga", and the parent name is what matches.

    Selection logic, ordered by strength of signal:
      1. Exact normalized match against an owned series name (+1000).
      2. Substring/prefix overlap with an owned name (+300 to +500).
      3. Fallback colon / word-count / position heuristic for books
         whose owned-side series isn't known (new authors, missing
         Calibre tags).
    """
    if not candidates:
        return None

    def _score(c):
        s = 0
        name = c["name"]
        norm = _norm_series(name)

        # Tier 1: matches what the user already has in Calibre
        if owned_norms:
            if norm and norm in owned_norms:
                s += 1000  # exact normalized match — strongest signal
            else:
                # Prefix/substring match: e.g. owned "Mistborn" vs Hardcover
                # "Mistborn: Era 1", or owned "The Mistborn Saga" vs
                # Hardcover "Mistborn Saga". Both directions accepted.
                for own in owned_norms:
                    if not own or not norm:
                        continue
                    if norm.startswith(own + " ") or own.startswith(norm + " "):
                        s += 500
                        break
                    if own in norm or norm in own:
                        s += 300
                        break

        # Tier 2: original heuristics (tiebreakers / fallback for
        # books whose owned-side series isn't in our hint set)
        if ":" in name:
            s += 10  # sub-series like "Star Wars: Empire and Rebellion"
        if c["position"] is not None:
            s += 5
        if "(" in name:
            s -= 3  # penalize "(Chronological)", "(Publication Order)"
        s += min(len(name.split()), 5)
        return s

    candidates_sorted = sorted(candidates, key=_score, reverse=True)
    return candidates_sorted[0]

# Fragments matching the official plugin + book-level contributions
FRAGMENTS = """
fragment BookData on books {
  id title slug rating description
  series: cached_featured_series
  book_series { position series { name id } }
  tags: cached_tags
  canonical_id
  image: cached_image
  contributions { author { name id } }
  book_mappings(where: {platform: {name: {_in: ["goodreads", "openlibrary", "google"]}}}) {
    external_id
    platform { name }
  }
}
fragment EditionData on editions {
  title id isbn_13 asin
  contributors: cached_contributors
  image: cached_image
  reading_format_id
  release_date
  pages
  users_count
  language { code3 }
}
"""

# Batched author-meta lookup. Pulls just enough to disambiguate
# the SEARCH_AUTHOR results (name + books_count) without paying
# for the full bibliography of each candidate.
AUTHORS_META_QUERY = """
query AuthorsMeta($ids: [Int!]) {
  authors(where: {id: {_in: $ids}}) {
    id name books_count
  }
}
"""


# v2.10.5 — direct author search. Replaces the previous indirect
# "book-search by name → filter by contributor" approach, which
# only surfaced books where the author's name appeared in the
# TITLE (e.g., "Jim Butcher's The Dresden Files: Welcome to the
# Jungle" graphic novels) and missed every novel where the title
# didn't carry the name (Storm Front, Death Masks, the entire
# Codex Alera, Cinder Spires, etc.). Confirmed via probe 2026-05-13:
# Hardcover's `authors.books_count` for Jim Butcher = 146, while
# the old pipeline returned 10 graphic novels.
SEARCH_AUTHOR_QUERY = """
query SearchAuthor($query: String!) {
  search(query: $query, query_type: "Author", per_page: 20) {
    ids
    results
  }
}
"""


# Hardcover's `reading_format_id` enum (from their public GraphQL schema):
#   1 = Physical book, 2 = Audiobook, 4 = E-Book.
# Ebook scans want physical + ebook editions; audiobook scans want
# audiobook editions. Without this split, an audiobook-library scan
# returns print/ebook metadata for every Hardcover hit — silently
# useless.
def _edition_format_ids(content_type: Optional[str]) -> list[int]:
    if content_type == "audiobook":
        return [2]
    return [1, 4]


# Direct author-books query. Walks the `contributions` relation on
# the `authors` type — the correct relation name per schema
# introspection (the old `book_authors` field doesn't exist).
# Paginated via $limit + $offset; the caller loops until fewer
# than $limit rows return. Hardcover's default per-page cap is
# generous (~250+); we use 100 to keep round-trip sizes sane for
# very prolific authors (J.N. Chaney 493, Logan Jacobs 326).
#
# v2.12.0 — Phase 1.0 probe (Sanderson audiobook): without the
# `contributions(where: ...)` filter, audiobook scans returned 691
# books for 99 audiobook hits (592 print-only leaked through as
# books with empty `editions` arrays). With the filter, audiobook
# scans return 104 books for 104 audiobook hits — AND surface 5
# extra valid audiobook editions the unfiltered pagination
# previously starved out (the wasted print-only slots were filling
# pages before reaching the deeper audiobook editions).
AUTHOR_BOOKS_QUERY = FRAGMENTS + """
query AuthorBooks($id: Int!, $limit: Int!, $offset: Int!, $languages: [String!], $format_ids: [Int!]) {
  authors(where: {id: {_eq: $id}}) {
    id name bio books_count image { url }
    contributions(
      where: {book: {editions: {reading_format_id: {_in: $format_ids}}}}
      limit: $limit
      offset: $offset
      order_by: {book: {release_date: asc_nulls_last}}
    ) {
      book {
        ...BookData
        editions(
          where: {reading_format_id: {_in: $format_ids}, language: {_or: [{code3: {_in: $languages}}, {code3: {_is_null: true}}]}}
          order_by: {users_count: desc_nulls_last}
          limit: 1
        ) { ...EditionData }
      }
    }
  }
}
"""


class HardcoverSource(BaseSource):
    name = "hardcover"
    default_headers = {
        "Content-Type": "application/json",
        "User-Agent": "Seshat/1.0 (https://github.com/malevolenttortoise/seshat)",
    }
    default_timeout = 30.0

    def __init__(self, api_key: str = ""):
        super().__init__(rate_limit=1.0)
        self.api_key = api_key.strip()

    def _get_client(self) -> httpx.AsyncClient:
        """Override to inject the Bearer token header from self.api_key.

        Always creates a fresh client so that update_api_key() can force a
        reconnect with the new credentials.
        """
        headers = dict(self.default_headers)
        if self.api_key:
            token = self.api_key
            # Match plugin logic: add Bearer if not already present
            if " " not in token:
                token = f"Bearer {token}"
            headers["Authorization"] = token

        # Close any existing client before creating a new one
        if self._client is not None:
            try:
                # Schedule the close but don't block on it
                asyncio.create_task(self._client.aclose())
            except Exception:
                pass

        self._client = httpx.AsyncClient(
            timeout=self.default_timeout,
            headers=headers,
            follow_redirects=self.follow_redirects,
        )
        return self._client

    # client property inherited from BaseSource

    def update_api_key(self, key: str):
        """Force client recreation with new API key on next access."""
        self.api_key = key.strip()
        self._client = None  # Next client access will trigger _get_client()

    async def _query(self, query: str, variables: dict = None) -> dict:
        """POST a GraphQL query with retry on transient failures.

        Up to 3 attempts, 2s → 4s backoff. Retries fire on:
          - httpx.TransportError (network-layer / connection reset)
          - httpx.ReadTimeout / WriteTimeout / ConnectTimeout
          - HTTP 5xx (server-side errors)

        GraphQL-level errors (HTTP 200 + `errors` key) are NOT
        retried — those mean the query itself is bad and won't
        self-heal. Same for 4xx (auth / bad request) — retrying
        won't help and would waste the user's rate budget.
        """
        if not self.api_key:
            return {}
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        last_err = None
        for attempt in range(3):
            try:
                resp = await self.client.post(API, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if "errors" in data:
                    msgs = [e.get("message", "?") for e in data["errors"]]
                    logger.warning(f"Hardcover GraphQL errors: {msgs}")
                    return {}
                return data.get("data", {})
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                last_err = e
                if status >= 500 and attempt < 2:
                    delay = 2.0 * (attempt + 1)
                    logger.warning(f"Hardcover HTTP {status} — retry {attempt+1}/2 in {delay}s")
                    await asyncio.sleep(delay)
                    continue
                logger.error(f"Hardcover HTTP {status}: {e}")
                return {}
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_err = e
                if attempt < 2:
                    delay = 2.0 * (attempt + 1)
                    logger.warning(f"Hardcover transport error ({type(e).__name__}) — retry {attempt+1}/2 in {delay}s")
                    await asyncio.sleep(delay)
                    continue
                logger.error(f"Hardcover transport error after 3 attempts: {type(e).__name__}: {e}")
                return {}
        # Unreachable in practice (loop either returns or logs+returns), but
        # keep as a guard so the function always returns a dict.
        if last_err:
            logger.error(f"Hardcover query exhausted retries: {last_err}")
        return {}

    async def _resolve_author_id(
        self, author_name: str
    ) -> Optional[int]:
        """Resolve a name to a Hardcover author_id via SEARCH_AUTHOR_QUERY.

        Returns the integer id of the best-matching author record,
        or None on no match. Disambiguates among multiple namesakes
        (the "Jim Butcher" problem — Hardcover has 20 authors that
        match that query) by:

          1. Strict normalized-name gate (lower + period/space-strip)
             keeps only candidates whose name actually matches.
          2. books_count tiebreaker among survivors — the real,
             prolific author wins over the namesake with 1 book.
          3. If nothing passes the gate, fall back to the first id
             from Hardcover's own ranker (they put the most-likely
             match first in most cases).
        """
        search = await self._query(
            SEARCH_AUTHOR_QUERY, {"query": author_name}
        )
        ids_raw = ((search.get("search") or {}).get("ids") or [])
        if not ids_raw:
            logger.info(
                "  Hardcover: SEARCH_AUTHOR returned no IDs for %r",
                author_name,
            )
            return None

        candidate_ids: list[int] = []
        for x in ids_raw[:10]:  # top 10 should always include the right one
            try:
                candidate_ids.append(int(x))
            except (ValueError, TypeError):
                pass
        if not candidate_ids:
            return None
        if len(candidate_ids) == 1:
            return candidate_ids[0]

        # Pull name + books_count for the candidates so we can disambiguate.
        meta = await self._query(
            AUTHORS_META_QUERY, {"ids": candidate_ids}
        )
        records = meta.get("authors") or []
        if not records:
            # Couldn't disambiguate — trust Hardcover's ranker.
            return candidate_ids[0]

        target = author_name.lower().replace(".", "").replace(" ", "")
        scored: list[tuple[int, int, str]] = []  # (score, books_count, name)
        for rec in records:
            try:
                rid = int(rec.get("id"))
            except (ValueError, TypeError):
                continue
            name = rec.get("name") or ""
            count = rec.get("books_count") or 0
            normalized = name.lower().replace(".", "").replace(" ", "")
            score = 0
            if normalized == target:
                score = 100  # exact match
            elif target in normalized or normalized in target:
                score = 50   # substring
            if score == 0:
                continue
            scored.append((score, count, name))
            # Cache for log
            id_map = getattr(self, "_resolve_id_map_cache", None)
            if id_map is None:
                id_map = {}
                self._resolve_id_map_cache = id_map
            id_map[rid] = (name, count, score)

        if not scored:
            logger.info(
                "  Hardcover: no candidate name matched %r — falling back "
                "to top-ranked id %d", author_name, candidate_ids[0],
            )
            return candidate_ids[0]

        # Rank winners: higher score first, then higher books_count.
        # Re-derive the rid for the winner by matching name+count.
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        winner = scored[0]
        winning_id = None
        for rec in records:
            try:
                rid = int(rec.get("id"))
            except (ValueError, TypeError):
                continue
            if rec.get("name") == winner[2] and (rec.get("books_count") or 0) == winner[1]:
                winning_id = rid
                break
        if winning_id is None:
            winning_id = candidate_ids[0]

        if len(scored) > 1:
            losers = ", ".join(
                f"{n}({c})" for _, c, n in scored[1:5]
            )
            logger.info(
                "  Hardcover: disambiguated %r → id=%d (name=%r "
                "books_count=%d). Passed-over namesakes: %s",
                author_name, winning_id, winner[2], winner[1], losers,
            )
        return winning_id

    async def _fetch_all_author_books(
        self, author_id: int, content_type: Optional[str]
    ) -> list[dict]:
        """Walk every book under `authors.contributions` for this id.

        Paginated via `limit` + `offset`. Each page returns a list of
        contribution rows; we unwrap to book dicts and accumulate.
        Stops when a page returns fewer than `PAGE_SIZE` rows
        (Hasura's last-page signal).
        """
        PAGE_SIZE = 100  # Hardcover handles 100-row pages comfortably (~250ms)
        MAX_PAGES = 20   # 2000-book ceiling; prolific indie authors fit (Chaney 493, Jacobs 326)
        format_ids = _edition_format_ids(content_type)
        all_books: list[dict] = []
        offset = 0
        for _ in range(MAX_PAGES):
            page = await self._query(AUTHOR_BOOKS_QUERY, {
                "id": author_id,
                "limit": PAGE_SIZE,
                "offset": offset,
                "languages": ["eng", "en"],
                "format_ids": format_ids,
            })
            authors_arr = page.get("authors") or []
            if not authors_arr:
                break
            contribs = authors_arr[0].get("contributions") or []
            if not contribs:
                break
            for entry in contribs:
                book = entry.get("book") if isinstance(entry, dict) else None
                if isinstance(book, dict):
                    all_books.append(book)
            if len(contribs) < PAGE_SIZE:
                break  # last page
            offset += PAGE_SIZE
        return all_books

    async def search_author(self, author_name: str, owned_titles: list = None,  # noqa: ARG002 — owned_titles unused in v2.10.5 (was used by retired book-search expansion); kept for interface compat
                            owned_series_names: list = None) -> Optional[AuthorResult]:
        """Find an author and walk their full bibliography.

        v2.10.5 redesign: replaces the previous book-search-by-name
        approach with a direct two-phase author-id lookup. Solves the
        bug where Jim Butcher returned 10 graphic novels instead of
        his real 146-book catalog — the old SEARCH_QUERY only
        matched books with the author's name in the TITLE, missing
        every novel whose title is just "Storm Front" etc.

        Phase 1: SEARCH_AUTHOR_QUERY by name → disambiguate to a
        single Hardcover author_id (`_resolve_author_id`).
        Phase 2: AUTHOR_BOOKS_QUERY walks `authors.contributions`
        with pagination → full bibliography
        (`_fetch_all_author_books`).
        Phase 3: existing per-book processing loop extracts series,
        edition, cover, language, release date.

        `owned_titles` is no longer needed for query expansion (the
        new path doesn't paginate by guessing book titles) but stays
        in the signature for source-interface compatibility with the
        BaseSource contract and lookup.py's call sites.

        `owned_series_names` — passed through to the per-book series
        picker so Hardcover candidates that match the user's Calibre
        series names beat deeper sub-series like "The Mistborn Saga:
        The Original Trilogy" when Calibre says "The Mistborn Saga".
        See `_pick_best_series` above.
        """
        if not self.api_key:
            return None
        try:
            # Pre-compute normalized owned series names for the per-book
            # series picker. Empty set if the user has nothing tagged or
            # the caller didn't pass any — picker falls back to the
            # original colon/word-count heuristics in that case.
            owned_norms = {_norm_series(s) for s in (owned_series_names or []) if s}
            owned_norms.discard("")

            # Phase 1: resolve the Hardcover author_id.
            resolved_id = await self._resolve_author_id(author_name)
            if resolved_id is None:
                logger.info(
                    f"  Hardcover: could not resolve author_id for '{author_name}'"
                )
                return None
            author_id = str(resolved_id)

            # Phase 2: walk the full bibliography via paginated
            # contributions. Format filter varies by the active library's
            # content_type (set by lookup.py alongside `_linked_author_names`)
            # so audiobook scans pull audiobook editions while ebook scans
            # pull print/ebook.
            content_type = getattr(self, "_content_type", None)
            books = await self._fetch_all_author_books(resolved_id, content_type)
            if not books:
                logger.info(
                    f"  Hardcover: author_id={resolved_id} returned 0 books for '{author_name}'"
                )
                return None
            logger.info(
                f"  Hardcover: author_id={resolved_id} '{author_name}' → "
                f"{len(books)} books from contributions relation"
            )
            
            # Namesake disambiguation now happens in Phase 1
            # (`_resolve_author_id`) — by the time we get here, every
            # book in `books` was fetched via the resolved author's
            # contributions relation and is definitionally by them.
            # The per-book authorship check below remains as a safety
            # net for the rare Goodreads-vs-Hardcover name-spelling
            # edge case.

            # Build result from found books
            series_map = {}
            standalone = []
            
            for book in books:
                # Per-book authorship gate retired in v2.10.5: books
                # came in via `authors(id={resolved_id}).contributions`,
                # so they're already filtered to this author. The old
                # name-match gate was incorrectly rejecting books when
                # Goodreads ("J. N. Chaney" with spaces) and Hardcover
                # ("J.N. Chaney" no space) disagreed on punctuation,
                # killing whole catalogs that the relation correctly
                # surfaced.

                # v2.11.0: skip rows where Hardcover returned a
                # null/empty title. These are data-quality misses
                # in HC's catalog (book record exists with editions
                # + ISBN but no canonical title yet), and emitting
                # them creates phantom empty-title rows in the review
                # queue — UAT 2026-05-13 caught one for Hasekura.
                _raw_title = (book.get("title") or "").strip()
                if not _raw_title:
                    logger.debug(
                        "  hardcover: skipping book id=%s — null/empty title",
                        book.get("id"),
                    )
                    continue

                # Per-book progress hook. Hardcover has no per-book
                # HTTP fetch (everything comes from one GraphQL round-
                # trip), so this loop tears through fast — but the
                # widget feed stays consistent with Goodreads/Kobo,
                # which is useful when many large catalogs are being
                # merged in sequence.
                on_book = getattr(self, '_on_book', None)
                if on_book:
                    on_book(book.get("title", ""))
                # Bump the new-candidate counter alongside the
                # title display. Hardcover has no slow per-book fetch
                # phase so this fires in a tight burst at merge time
                # rather than smoothly during a network loop, but
                # the count still arrives at the correct value via
                # the on_progress(total) sync after the source
                # completes regardless.
                on_new_candidate = getattr(self, '_on_new_candidate', None)
                if on_new_candidate:
                    on_new_candidate()

                edition = book.get("editions", [{}])[0] if book.get("editions") else {}
                cover = _pick_hardcover_cover(book, edition)

                # Language: `code3` is an ISO 639-2 3-letter code
                # ("eng"). Map "eng"/"en" → "English" so downstream
                # `_lang_ok` and the UI display match the other
                # sources (which already emit the human-readable
                # form). Leave other codes raw so multi-language
                # users can still filter meaningfully.
                lang_obj = edition.get("language") or {}
                lang_code = (lang_obj.get("code3") if isinstance(lang_obj, dict) else "") or ""
                if lang_code.lower() in ("eng", "en"):
                    lang_name = "English"
                else:
                    lang_name = lang_code or None

                # Pages — Hardcover editions.pages is a nullable int. Coerce
                # defensively because GraphQL Int can come back as a string
                # for very large values in some client paths.
                pages_raw = edition.get("pages")
                page_count = None
                if pages_raw is not None:
                    try:
                        page_count = int(pages_raw)
                    except (ValueError, TypeError):
                        page_count = None

                # Unreleased detection: compare release_date to
                # today. If it's in the future, the book is upcoming
                # — move the date to `expected_date` and clear
                # `pub_date`. Mirror of the Goodreads pattern, and
                # what makes Upcoming-tab entries appear from
                # Hardcover.
                release_date = edition.get("release_date")
                pub_date = release_date
                expected_date = None
                is_unreleased = False
                if release_date:
                    try:
                        from datetime import datetime
                        if datetime.strptime(release_date[:10], "%Y-%m-%d") > datetime.now():
                            is_unreleased = True
                            expected_date = release_date
                            pub_date = None
                    except (ValueError, TypeError):
                        pass  # unparseable date — leave as-is, let lookup.py handle

                slug = book.get("slug", "")

                # v2.16.0 Gap 1 — Hardcover knows the Goodreads /
                # OpenLibrary / Google Books IDs for many books via
                # its `book_mappings` table. Surface them so the
                # merge layer can COALESCE-fill the per-source ID
                # columns, which in turn unblocks the Phase-2
                # author-goodreads_id backfill for audiobook-only
                # authors (and any author whose only books come
                # from Hardcover).
                gr_id = ol_id = gb_id = None
                for mapping in (book.get("book_mappings") or []):
                    if not isinstance(mapping, dict):
                        continue
                    ext_id = mapping.get("external_id")
                    if not ext_id:
                        continue
                    platform = (mapping.get("platform") or {}).get("name", "")
                    pkey = str(platform).strip().lower()
                    if pkey == "goodreads" and gr_id is None:
                        gr_id = str(ext_id).strip()
                    elif pkey == "openlibrary" and ol_id is None:
                        # Hardcover stores either bare OL keys or
                        # `/books/OLxxxM` / `/works/OLxxxW` paths.
                        # Strip the prefix so the column value
                        # matches what `openlibrary.py` itself
                        # writes (bare `OL...` form). BookSidebar
                        # suffix-routes M-keys to /books/ and
                        # W-keys to /works/, so either kind is
                        # link-resolvable.
                        raw = str(ext_id).strip()
                        ol_id = raw.rsplit("/", 1)[-1] if "/" in raw else raw
                    elif pkey == "google" and gb_id is None:
                        gb_id = str(ext_id).strip()

                br = BookResult(
                    title=book.get("title", ""),
                    isbn=edition.get("isbn_13"),
                    cover_url=cover,
                    pub_date=pub_date,
                    expected_date=expected_date,
                    is_unreleased=is_unreleased,
                    description=book.get("description"),
                    page_count=page_count,
                    language=lang_name,
                    external_id=str(book.get("id")),
                    source="hardcover",
                    source_url=f"https://hardcover.app/books/{slug}" if slug else None,
                    goodreads_id=gr_id,
                    openlibrary_id=ol_id,
                    google_books_id=gb_id,
                )
                
                # Check series info: try book_series relation first, then cached_featured_series
                sname = None; spos = None
                bs = book.get("book_series")
                if bs and isinstance(bs, list) and len(bs) > 0:
                    candidates = []
                    for bse in bs:
                        if isinstance(bse, dict):
                            sr_obj = bse.get("series", {})
                            if isinstance(sr_obj, dict) and sr_obj.get("name"):
                                candidates.append({
                                    "name": sr_obj["name"],
                                    "position": bse.get("position"),
                                    "id": sr_obj.get("id"),
                                })
                    if candidates:
                        # Owned-series-aware picker: prefer the
                        # candidate matching what Calibre already
                        # has, fall back to the colon/word-count
                        # heuristic when there's no owned-side hint.
                        best = _pick_best_series(candidates, owned_norms)
                        sname = best["name"]
                        spos = best["position"]
                        if len(candidates) > 1:
                            hint = " (owned-match)" if owned_norms and any(
                                _norm_series(sname) == on or _norm_series(sname) in on or on in _norm_series(sname)
                                for on in owned_norms
                            ) else ""
                            logger.debug(
                                f"  Hardcover: '{book.get('title')}' has "
                                f"{len(candidates)} series: "
                                f"{[c['name'] for c in candidates]} → picked "
                                f"'{sname}'{hint}"
                            )
                        else:
                            logger.debug(f"  Hardcover: '{book.get('title')}' series from book_series → '{sname}' #{spos}")
                if not sname:
                    series = book.get("series")
                    if series and isinstance(series, list) and len(series) > 0:
                        s = series[0]
                        if isinstance(s, dict) and s.get("name"):
                            sname = s["name"]
                            spos = s.get("position")
                            logger.debug(f"  Hardcover: '{book.get('title')}' series from cached → '{sname}' #{spos}")
                
                if sname:
                    br.series_name = sname
                    br.series_index = spos
                    if sname not in series_map:
                        series_map[sname] = SeriesResult(name=sname, books=[])
                    series_map[sname].books.append(br)
                    continue
                
                standalone.append(br)
            
            total = len(standalone) + sum(len(s.books) for s in series_map.values())
            logger.info(f"  Hardcover: found {total} books by '{author_name}' ({len(series_map)} series)")
            
            return AuthorResult(
                name=author_name,
                external_id=author_id or "search",
                books=standalone,
                series=list(series_map.values()),
            )
            
        except Exception as e:
            logger.error(f"Hardcover error for '{author_name}': {e}")
            return None

    async def get_author_books(self, author_id: str, **kw) -> Optional[AuthorResult]:
        """No-op override.

        Hardcover's `search_author` already returns a fully-populated
        AuthorResult (books + series + metadata) in one GraphQL
        round-trip, so lookup.py's two-phase
        search_author → get_author_books flow collapses into phase 1.
        `_try_source` takes the fast path whenever the search result
        already carries books or series, which is always true for
        Hardcover.

        This stub exists to satisfy the BaseSource contract.
        Returning None degrades gracefully on the unreachable slow
        path: the caller logs "No books returned" and moves on.
        `**kw` swallows lookup.py's extra kwargs so the TypeError-
        fallback in `_try_source` never fires for Hardcover.
        """
        return None
