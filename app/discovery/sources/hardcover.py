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
import httpx, logging, json
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

SEARCH_QUERY = """
query Search($query: String!) {
  search(query: $query, query_type: "Book", per_page: 50) {
    ids
    results
  }
}
"""

FIND_BOOKS_BY_IDS = FRAGMENTS + """
query FindBooksByIds($ids: [Int!], $languages: [String!]) {
  books(where: {id: {_in: $ids}}, order_by: {users_read_count: desc_nulls_last}) {
    ...BookData
    editions(
      where: {reading_format_id: {_in: [1, 4]}, language: {_or: [{code3: {_in: $languages}}, {code3: {_is_null: true}}]}}
      order_by: {users_count: desc_nulls_last}
    ) { ...EditionData }
  }
}
"""

# Direct author query (may work with some API keys)
AUTHOR_BOOKS_QUERY = FRAGMENTS + """
query AuthorBooks($id: Int!, $languages: [String!]) {
  authors(where: {id: {_eq: $id}}) {
    id name bio image { url }
    book_authors(order_by: {book: {release_date: asc}}) {
      book {
        ...BookData
        editions(
          where: {reading_format_id: {_in: [1, 4]}, language: {_or: [{code3: {_in: $languages}}, {code3: {_is_null: true}}]}}
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
        "User-Agent": "Seshat/1.0 (https://github.com/mnbaker117/seshat)",
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

    async def search_author(self, author_name: str, owned_titles: list = None,
                            owned_series_names: list = None) -> Optional[AuthorResult]:
        """Find an author by searching for their books, then merging by same author.

        Two important behaviors:

        **Query expansion** — fires one SEARCH_QUERY for the bare
        author name PLUS one for each entry in `owned_titles[:25]`,
        and accumulates the result IDs across ALL queries (no early
        break). This is the only way prolific authors return more
        than ~10 books on Hardcover; without expansion, a Sanderson
        scan once returned 10 IDs of which only 1 actually matched
        the user's library, while Goodreads matched 14 of 16 owned
        books in the same scan.

        **owned_series_names** — passed through to the per-book
        series picker so Hardcover candidates that match the user's
        Calibre series names beat deeper sub-series like "The
        Mistborn Saga: The Original Trilogy" when Calibre says
        "The Mistborn Saga". See `_pick_best_series` above.
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

            # Build the query list. Order matters for tie-breaking but
            # since we accumulate from ALL queries it mostly affects the
            # "which books got searched in priority order" debug story.
            search_queries = [author_name]
            if owned_titles:
                # `[:25]` is sized to cover the ~95th-percentile of
                # owned books for prolific authors. A smaller slice
                # silently misses books at positions 11-16 in the SQL
                # return order — verified by a Sanderson scan that
                # missed Rhythm of War, Tress, etc. with `[:10]`.
                # Cost is bounded: 26 searches × ~250ms = ~6.5s, and
                # only on the first Hardcover scan of a large author.
                for t in owned_titles[:25]:
                    search_queries.append(f"{t} {author_name}")

            all_book_ids = set()
            for sq in search_queries:
                data = await self._query(SEARCH_QUERY, {"query": sq})
                # Defensive: Hardcover can return `search: null` or
                # `search: {ids: null}` for queries that match nothing
                # or partially fail server-side. Dict-default `{}`
                # only protects against MISSING keys, not null VALUES,
                # so coerce both null cases via `or` chains.
                search = data.get("search") or {}
                ids = search.get("ids") or []
                for bid in ids[:10]:
                    try:
                        all_book_ids.add(int(bid))
                    except (ValueError, TypeError):
                        pass
                # No early-break: every search contributes to the union.

            if not all_book_ids:
                logger.info(f"  Hardcover: no search results for '{author_name}'")
                return None

            # Cap at 100 IDs. With 25 owned-title queries we
            # routinely accumulate 60-90 unique IDs for prolific
            # authors, and Hardcover's books-by-id query handles
            # batches this size comfortably (~270ms for ~40 IDs).
            book_ids = list(all_book_ids)[:100]
            logger.info(
                f"  Hardcover: search yielded {len(all_book_ids)} unique IDs "
                f"across {len(search_queries)} queries → fetching {len(book_ids)}"
            )
            
            # Step 2: Fetch books by IDs
            books_data = await self._query(FIND_BOOKS_BY_IDS, {
                "ids": book_ids, "languages": ["eng", "en"]
            })
            books = books_data.get("books", [])
            if not books:
                return None
            
            # Pre-normalize owned titles for the disambiguation tiebreaker.
            # Same shape lookup.py uses (lowercased, punctuation stripped).
            owned_title_norms = set()
            for ot in (owned_titles or []):
                if not ot:
                    continue
                tn = re.sub(r'[^\w\s]', '', ot.lower()).strip()
                tn = re.sub(r'\s+', ' ', tn)
                if tn:
                    owned_title_norms.add(tn)

            # Extract author info from cached_contributors
            author_id = None

            def _check_contributor(c, target_name):
                """Check if a contributor matches our author name."""
                target = target_name.lower().strip()
                target_parts = set(target.replace(".", "").split())
                if isinstance(c, dict):
                    # Try multiple name fields
                    cname = ""
                    if c.get("name"):
                        cname = c["name"]
                    elif isinstance(c.get("author"), dict) and c["author"].get("name"):
                        cname = c["author"]["name"]
                    elif c.get("author_name"):
                        cname = c["author_name"]
                    
                    cn = cname.lower().strip()
                    # Exact match
                    if cn == target:
                        return True, cname, c.get("id") or c.get("author_id")
                    # Match ignoring periods/dots (J. K. Rowling vs JK Rowling)
                    if cn.replace(".", "") == target.replace(".", ""):
                        return True, cname, c.get("id") or c.get("author_id")
                    # All name parts present (handles "James S A Corey" vs "James S. A. Corey")
                    cn_parts = set(cn.replace(".", "").split())
                    if target_parts and cn_parts and target_parts == cn_parts:
                        return True, cname, c.get("id") or c.get("author_id")
                    return False, cname, c.get("id") or c.get("author_id")
                elif isinstance(c, str):
                    cn = c.lower().strip()
                    if cn == target or cn.replace(".", "") == target.replace(".", ""):
                        return True, c, None
                    return False, c, None
                return False, str(c), None
            
            def _parse_contributors(raw):
                """Parse contributors from various formats."""
                if isinstance(raw, list):
                    return raw
                if isinstance(raw, str):
                    try:
                        import json as jn
                        return jn.loads(raw)
                    except (ValueError, TypeError):
                        return [{"name": raw}]
                return []
            
            # ── Namesake disambiguation ────────────────────────────
            # Hardcover often has multiple authors with the same name
            # — "David Burke" returns three distinct people: a LitRPG
            # writer with 32 books, a music journalist with 7, and a
            # Cold War espionage writer with 3. Both the bare-name
            # query and the owned-title queries above are name-based,
            # so the accumulated book set can mix all three. Without
            # filtering, the merge would silently glue another
            # David Burke's catalog onto the user's LitRPG author.
            #
            # Strategy: tally each `book.contributions[].author.id`
            # whose name matches our target, vote-rank by owned-title
            # overlap (book count is the tiebreaker), and keep only
            # the winning ID's books. With one candidate ID — the
            # common case — no filtering happens.
            id_to_books = {}  # author_id (str) → [book dict, ...]
            id_to_name = {}   # author_id (str) → display name
            for book in books:
                for contrib in book.get("contributions", []):
                    author_obj = contrib.get("author", {})
                    if not isinstance(author_obj, dict):
                        continue
                    aname = author_obj.get("name", "")
                    matched, _, _ = _check_contributor({"name": aname}, author_name)
                    if not matched:
                        continue
                    aid_raw = author_obj.get("id")
                    if aid_raw is None:
                        # Skip ID-less matches for the vote — they'd
                        # collapse all unknown authors into one bucket
                        # and wreck the tally.
                        continue
                    aid = str(aid_raw)
                    id_to_books.setdefault(aid, []).append(book)
                    id_to_name.setdefault(aid, aname)
                    break  # one vote per book per ID

            winning_id = None
            if len(id_to_books) >= 2:
                def _vote_score(item):
                    aid, blist = item
                    base = len(blist)
                    # Tiebreaker: how many of this ID's books overlap
                    # the user's owned titles? Normalized comparison
                    # mirrors lookup.py's _normalize style.
                    overlap = 0
                    if owned_title_norms:
                        for b in blist:
                            t = b.get("title", "") or ""
                            tn = re.sub(r'[^\w\s]', '', t.lower()).strip()
                            tn = re.sub(r'\s+', ' ', tn)
                            if tn and tn in owned_title_norms:
                                overlap += 1
                    # Score: book count × 10 + overlap × 100. Owned
                    # overlap dominates because a single confirmed
                    # match is much stronger evidence than +5 random
                    # books — but book count still breaks ties when
                    # neither candidate has any owned overlap.
                    return overlap * 100 + base * 10

                ranked = sorted(id_to_books.items(), key=_vote_score, reverse=True)
                winning_id = ranked[0][0]
                summary = ", ".join(
                    f"{aid} ({id_to_name.get(aid,'?')}): {len(blist)} books"
                    for aid, blist in ranked
                )
                logger.info(
                    f"  Hardcover: namesake disambiguation for '{author_name}' — "
                    f"{len(id_to_books)} candidate IDs [{summary}] → picked {winning_id}"
                )
                # Filter the working set to only the winner's books
                winner_book_ids = {id(b) for b in id_to_books[winning_id]}
                books = [b for b in books if id(b) in winner_book_ids]
                author_id = winning_id
            elif len(id_to_books) == 1:
                winning_id = next(iter(id_to_books))
                author_id = winning_id
                logger.debug(
                    f"  Hardcover: disambiguation pass: 1 candidate ID for "
                    f"'{author_name}' (id={winning_id}, "
                    f"{len(id_to_books[winning_id])} books) — no filter needed"
                )
            # else: no contributions[] IDs found at all — fall through
            # to the legacy edition-contributor path below, which
            # accepts books by name match without an ID.

            # Build result from found books
            series_map = {}
            standalone = []
            
            for book in books:
                is_by_author = False
                
                # Check 1: Book-level contributions (most reliable)
                for contrib in book.get("contributions", []):
                    author_obj = contrib.get("author", {})
                    if isinstance(author_obj, dict):
                        aname = author_obj.get("name", "")
                        matched, _, _ = _check_contributor({"name": aname}, author_name)
                        if matched:
                            is_by_author = True
                            if not author_id:
                                author_id = str(author_obj.get("id", "matched"))
                            logger.debug(f"  Hardcover: '{book.get('title')}' matched via contributions → '{aname}'")
                            break
                
                # Check 2: Edition-level cached_contributors
                if not is_by_author:
                    for edition in book.get("editions", []):
                        contribs = _parse_contributors(edition.get("contributors"))
                        for c in contribs:
                            matched, cname, _ = _check_contributor(c, author_name)
                            if matched:
                                is_by_author = True
                                logger.debug(f"  Hardcover: '{book.get('title')}' matched via edition contributor → '{cname}'")
                                break
                        if is_by_author:
                            break
                
                if not is_by_author:
                    # Log what we found for diagnosis
                    book_authors = [c.get("author", {}).get("name", "?") for c in book.get("contributions", [])]
                    ed_contribs = []
                    for ed in book.get("editions", []):
                        for c in _parse_contributors(ed.get("contributors")):
                            _, cn, _ = _check_contributor(c, author_name)
                            if cn: ed_contribs.append(cn)
                    all_names = book_authors + ed_contribs
                    logger.info(f"  Hardcover: skipping '{book.get('title')}' — contributors: {all_names[:5] if all_names else '(none)'}")
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
