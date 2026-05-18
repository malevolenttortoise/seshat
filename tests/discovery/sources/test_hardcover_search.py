"""
Tests for the v2.10.5 HardcoverSource rewrite.

Pre-v2.10.5 the source did "search books matching the author name,
then filter by contributor" — which only surfaced books with the
author's name in the TITLE, missing whole catalogs (Jim Butcher
returned 10 graphic novels instead of his 146-book bibliography).

The new implementation does direct two-phase lookup:
  1. SEARCH_AUTHOR_QUERY by name → disambiguate to one author_id
     (`_resolve_author_id`)
  2. AUTHOR_BOOKS_QUERY paginated walks the `contributions` relation
     (`_fetch_all_author_books`)

These tests pin both phases by monkeypatching `_query` directly,
which bypasses the discovery `BaseSource` client-rebuild lifecycle
(`_get_client` rebuilds on every `.client` access, which would
clobber any MockTransport injected on `self._client`).
"""
from __future__ import annotations

from app.discovery.sources.hardcover import HardcoverSource


def _patch_query(src: HardcoverSource, responses: dict, *, call_log: list = None):
    """Monkeypatch `src._query` to return canned per-operation responses.

    `responses` maps operation-name fragments ("SearchAuthor",
    "AuthorsMeta", "AuthorBooks") to either:
      - a single dict (returned every time the operation matches)
      - a list of dicts (returned in order, one per call — for
        testing pagination)
    `call_log` (optional) accumulates the matched op-names so tests
    can assert call counts/ordering.
    """
    state = {k: 0 for k in responses}

    async def fake_query(query: str, variables: dict = None) -> dict:
        for op_name, payload in responses.items():
            if op_name in query:
                if call_log is not None:
                    call_log.append((op_name, dict(variables or {})))
                if isinstance(payload, list):
                    idx = state[op_name]
                    state[op_name] = idx + 1
                    return payload[idx] if idx < len(payload) else {}
                return payload
        if call_log is not None:
            call_log.append(("unmatched", dict(variables or {})))
        return {}

    src._query = fake_query  # type: ignore[method-assign]


def _make_source() -> HardcoverSource:
    return HardcoverSource(api_key="test-key")


class TestResolveAuthorId:
    async def test_single_candidate_returned_directly(self):
        # Hardcover returned exactly 1 author id — no disambiguation
        # needed, no follow-up AUTHORS_META call.
        src = _make_source()
        call_log: list = []
        _patch_query(src, {
            "SearchAuthor": {"search": {"ids": [109593], "results": ""}},
        }, call_log=call_log)

        result = await src._resolve_author_id("Jim Butcher")

        assert result == 109593
        assert [c[0] for c in call_log] == ["SearchAuthor"]
        await src.close()

    async def test_multiple_candidates_disambiguated_by_name_match(self):
        # 4 candidates returned. Only one has a name that exactly
        # matches "Jim Butcher"; that one wins regardless of books_count.
        src = _make_source()
        _patch_query(src, {
            "SearchAuthor": {"search": {
                "ids": [109593, 1428184, 1383790, 1379351], "results": "",
            }},
            "AuthorsMeta": {"authors": [
                {"id": 109593, "name": "Jim Butcher", "books_count": 146},
                {"id": 1428184, "name": "James Butcher", "books_count": 200},
                {"id": 1383790, "name": "Jim Butcheron", "books_count": 5},
                {"id": 1379351, "name": "Some Other Person", "books_count": 99},
            ]},
        })

        result = await src._resolve_author_id("Jim Butcher")

        assert result == 109593  # exact name match wins, books_count irrelevant
        await src.close()

    async def test_books_count_breaks_tie_on_name_match(self):
        # Two namesakes both pass the strict name gate. Higher
        # books_count wins (the more prolific one is more likely to
        # be the user's target).
        src = _make_source()
        _patch_query(src, {
            "SearchAuthor": {"search": {"ids": [111, 222], "results": ""}},
            "AuthorsMeta": {"authors": [
                {"id": 111, "name": "John Smith", "books_count": 5},
                {"id": 222, "name": "John Smith", "books_count": 200},
            ]},
        })

        result = await src._resolve_author_id("John Smith")

        assert result == 222  # more prolific Smith wins
        await src.close()

    async def test_no_search_results_returns_none(self):
        src = _make_source()
        _patch_query(src, {
            "SearchAuthor": {"search": {"ids": [], "results": ""}},
        })

        result = await src._resolve_author_id("Nonexistent Author")

        assert result is None
        await src.close()

    async def test_no_meta_match_falls_back_to_first_id(self):
        # Multiple candidates returned, but NONE pass the strict
        # name-match gate (e.g., all are partial matches). Fall back
        # to Hardcover's top-ranked id rather than returning None.
        src = _make_source()
        _patch_query(src, {
            "SearchAuthor": {"search": {"ids": [555, 666], "results": ""}},
            "AuthorsMeta": {"authors": [
                {"id": 555, "name": "Wildly Different Name", "books_count": 50},
                {"id": 666, "name": "Also Different", "books_count": 1},
            ]},
        })

        result = await src._resolve_author_id("Brandon Sanderson")

        assert result == 555  # first id from Hardcover's ranker
        await src.close()


class TestFetchAllAuthorBooks:
    async def test_single_page_under_limit(self):
        # 10 books returned, less than the 100 page size — single
        # round-trip, no further pagination.
        src = _make_source()
        call_log: list = []
        _patch_query(src, {
            "AuthorBooks": {"authors": [{"contributions": [
                {"book": {"id": i, "title": f"Book {i}"}} for i in range(10)
            ]}]},
        }, call_log=call_log)

        books = await src._fetch_all_author_books(109593, content_type=None)

        assert len(books) == 10
        assert len(call_log) == 1  # no pagination needed
        assert books[0]["title"] == "Book 0"
        await src.close()

    async def test_paginates_through_full_page_until_partial(self):
        # Mock returns 100 books on each of the first two pages,
        # then 23 on page 3 → total 223. Caller should make exactly
        # 3 round-trips and accumulate all 223.
        src = _make_source()
        responses = [
            {"authors": [{"contributions": [
                {"book": {"id": i, "title": f"P1-{i}"}} for i in range(100)
            ]}]},
            {"authors": [{"contributions": [
                {"book": {"id": 100 + i, "title": f"P2-{i}"}} for i in range(100)
            ]}]},
            {"authors": [{"contributions": [
                {"book": {"id": 200 + i, "title": f"P3-{i}"}} for i in range(23)
            ]}]},
        ]
        call_log: list = []
        _patch_query(src, {"AuthorBooks": responses}, call_log=call_log)

        books = await src._fetch_all_author_books(109593, content_type=None)

        assert len(call_log) == 3
        offsets = [v.get("offset") for _, v in call_log]
        assert offsets == [0, 100, 200]
        assert len(books) == 223
        await src.close()

    async def test_audiobook_content_type_uses_format_id_2(self):
        # The format-id list passed to AUTHOR_BOOKS_QUERY varies by
        # content_type. Verify the audiobook path routes through
        # `[2]` (audiobook reading_format_id) not `[1, 4]` (print + ebook).
        src = _make_source()
        call_log: list = []
        _patch_query(src, {
            "AuthorBooks": {"authors": [{"contributions": []}]},
        }, call_log=call_log)

        await src._fetch_all_author_books(109593, content_type="audiobook")

        assert call_log[0][1]["format_ids"] == [2]
        await src.close()

    async def test_no_authors_returned_yields_empty(self):
        src = _make_source()
        _patch_query(src, {"AuthorBooks": {"authors": []}})

        books = await src._fetch_all_author_books(999999, content_type=None)

        assert books == []
        await src.close()


class TestSearchAuthorEndToEnd:
    """End-to-end: SEARCH_AUTHOR → AUTHOR_BOOKS → assembled AuthorResult."""

    async def test_jim_butcher_regression(self):
        """Regression test for the v2.10.5 fix.

        Pre-fix: the old book-search-by-name approach returned 10
        graphic novels for Jim Butcher because they had "Jim
        Butcher's" in the title, missing all the actual novels.
        Post-fix: the contributions-relation walk surfaces every
        book attributed to author_id=109593.
        """
        src = _make_source()
        _patch_query(src, {
            "SearchAuthor": {"search": {"ids": [109593], "results": ""}},
            "AuthorBooks": {"authors": [{
                "id": 109593, "name": "Jim Butcher", "books_count": 146,
                "contributions": [
                    {"book": {
                        "id": 1001, "title": "Storm Front", "slug": "storm-front",
                        "contributions": [{"author": {"id": 109593, "name": "Jim Butcher"}}],
                        "book_series": [{
                            "position": 1,
                            "series": {"id": 5, "name": "The Dresden Files"},
                        }],
                        "editions": [{
                            "id": 9001, "isbn_13": "9780451457813",
                            "release_date": "2000-04-01",
                            "language": {"code3": "eng"},
                        }],
                    }},
                    {"book": {
                        "id": 1002, "title": "Furies of Calderon", "slug": "furies",
                        "contributions": [{"author": {"id": 109593, "name": "Jim Butcher"}}],
                        "book_series": [{
                            "position": 1,
                            "series": {"id": 7, "name": "Codex Alera"},
                        }],
                        "editions": [{
                            "id": 9002, "release_date": "2004-10-05",
                            "language": {"code3": "eng"},
                        }],
                    }},
                ],
            }]},
        })

        result = await src.search_author("Jim Butcher")

        assert result is not None
        assert result.external_id == "109593"
        total = len(result.books) + sum(
            len(s.books) for s in (result.series or [])
        )
        assert total == 2
        series_names = {s.name for s in result.series}
        assert series_names == {"The Dresden Files", "Codex Alera"}
        await src.close()

    async def test_empty_title_book_skipped(self):
        """v2.11.0 — Hardcover occasionally returns book records with
        null/empty `title` (data-quality misses in their catalog).
        Pre-fix, these landed as empty-title rows in the review queue.
        Caught during 2026-05-13 Hasekura UAT — `hardcover_id=2532938`
        had no title yet created a phantom row.
        Post-fix: skip the BookResult emission entirely."""
        src = _make_source()
        _patch_query(src, {
            "SearchAuthor": {"search": {"ids": [123], "results": ""}},
            "AuthorBooks": {"authors": [{
                "id": 123, "name": "Test Author", "books_count": 3,
                "contributions": [
                    {"book": {
                        "id": 1001, "title": "Real Book", "slug": "real-book",
                        "contributions": [{"author": {"id": 123, "name": "Test Author"}}],
                        "editions": [{"language": {"code3": "eng"}}],
                    }},
                    {"book": {
                        "id": 1002, "title": None, "slug": "phantom",
                        "contributions": [{"author": {"id": 123, "name": "Test Author"}}],
                        "editions": [{"isbn_13": "9999999999999"}],
                    }},
                    {"book": {
                        "id": 1003, "title": "  ", "slug": "whitespace-only",
                        "contributions": [{"author": {"id": 123, "name": "Test Author"}}],
                        "editions": [{}],
                    }},
                ],
            }]},
        })

        result = await src.search_author("Test Author")

        assert result is not None
        all_titles = [b.title for b in result.books] + [
            b.title for s in (result.series or []) for b in s.books
        ]
        assert all_titles == ["Real Book"]
        await src.close()

    async def test_no_api_key_returns_none(self):
        src = HardcoverSource(api_key="")
        result = await src.search_author("Anyone")
        assert result is None
        await src.close()

    async def test_resolver_miss_returns_none(self):
        src = _make_source()
        _patch_query(src, {
            "SearchAuthor": {"search": {"ids": [], "results": ""}},
        })

        result = await src.search_author("Nonexistent Author")

        assert result is None
        await src.close()

    async def test_book_mappings_populate_cross_source_ids(self):
        """v2.16.0 Gap 1 — Hardcover's `book_mappings` table carries
        Goodreads / OpenLibrary / Google Books external IDs for many
        books. Verify the per-book GraphQL fetch surfaces them on the
        BookResult so the merge layer's COALESCE-fill can seed
        `goodreads_id`/`openlibrary_id`/`google_books_id` on the books
        row — the unblocker for Phase-2 author-goodreads_id backfill
        on audiobook-only / Hardcover-only authors.
        """
        src = _make_source()
        _patch_query(src, {
            "SearchAuthor": {"search": {"ids": [42], "results": ""}},
            "AuthorBooks": {"authors": [{
                "id": 42, "name": "Test Author", "books_count": 3,
                "contributions": [
                    # All three mappings present + OL value in the
                    # `/books/OLxxxM` path form (Hardcover sometimes
                    # stores the prefix; we strip to the bare key).
                    {"book": {
                        "id": 1001, "title": "SAO Novel 01", "slug": "sao",
                        "contributions": [{"author": {"id": 42, "name": "Test Author"}}],
                        "editions": [{"language": {"code3": "eng"}}],
                        "book_mappings": [
                            {"external_id": "36607207",
                             "platform": {"name": "Goodreads"}},
                            {"external_id": "/books/OL47349783M",
                             "platform": {"name": "OpenLibrary"}},
                            {"external_id": "5UFcAQAACAAJ",
                             "platform": {"name": "Google"}},
                        ],
                    }},
                    # No mappings at all — three xid fields stay None.
                    {"book": {
                        "id": 1002, "title": "Mapping-less Book",
                        "contributions": [{"author": {"id": 42, "name": "Test Author"}}],
                        "editions": [{"language": {"code3": "eng"}}],
                    }},
                    # Goodreads only + OL in bare-key form (no path
                    # prefix) — must round-trip unchanged.
                    {"book": {
                        "id": 1003, "title": "Partial Mappings",
                        "contributions": [{"author": {"id": 42, "name": "Test Author"}}],
                        "editions": [{"language": {"code3": "eng"}}],
                        "book_mappings": [
                            {"external_id": "12345",
                             "platform": {"name": "Goodreads"}},
                            {"external_id": "OL99999W",
                             "platform": {"name": "OpenLibrary"}},
                        ],
                    }},
                ],
            }]},
        })

        result = await src.search_author("Test Author")
        assert result is not None
        by_title = {
            b.title: b
            for b in result.books
            + [bk for s in result.series for bk in s.books]
        }

        b1 = by_title["SAO Novel 01"]
        assert b1.goodreads_id == "36607207"
        assert b1.openlibrary_id == "OL47349783M"  # /books/ stripped
        assert b1.google_books_id == "5UFcAQAACAAJ"

        b2 = by_title["Mapping-less Book"]
        assert b2.goodreads_id is None
        assert b2.openlibrary_id is None
        assert b2.google_books_id is None

        b3 = by_title["Partial Mappings"]
        assert b3.goodreads_id == "12345"
        assert b3.openlibrary_id == "OL99999W"  # bare key passes through
        assert b3.google_books_id is None
        await src.close()

    async def test_punctuation_mismatch_does_not_drop_books(self):
        """v2.10.5 regression — pre-fix, an author searched as 'J. N.
        Chaney' (with spaces) but stored on Hardcover as 'J.N. Chaney'
        (no space) would resolve correctly but then have all books
        rejected by the per-book authorship-name gate.

        The new code drops that gate entirely (books are by definition
        by this author since they came via the author's contributions
        relation), so this scenario must surface every book.
        """
        src = _make_source()
        _patch_query(src, {
            "SearchAuthor": {"search": {"ids": [241293], "results": ""}},
            "AuthorBooks": {"authors": [{
                "id": 241293, "name": "J.N. Chaney", "books_count": 398,
                "contributions": [
                    {"book": {
                        "id": 5001, "title": "Renegade Star",
                        "slug": "renegade-star",
                        # Note: contributor is "J.N. Chaney" (no space) but
                        # we searched as "J. N. Chaney" — pre-fix this
                        # would fail the per-book name gate and skip.
                        "contributions": [{"author": {"id": 241293, "name": "J.N. Chaney"}}],
                        "editions": [],
                    }},
                ],
            }]},
        })

        result = await src.search_author("J. N. Chaney")  # space-separated

        assert result is not None
        total = len(result.books) + sum(
            len(s.books) for s in (result.series or [])
        )
        assert total == 1, (
            "punctuation mismatch between caller and Hardcover canonical "
            "name must not drop books"
        )
        await src.close()


class TestNoSearchQueryRegression:
    """Pin the v2.10.5 policy: NEVER use the deprecated SEARCH_QUERY
    (`query_type: "Book"`) book-search-by-name path. Future refactors
    that re-introduce it will fail this test."""

    async def test_only_search_author_query_type_is_used(self):
        used_query_types: list[str] = []

        async def fake_query(query: str, variables: dict = None) -> dict:
            qstripped = query.replace(" ", "").replace("\n", "")
            if 'query_type:"Author"' in qstripped:
                used_query_types.append("Author")
            elif 'query_type:"Book"' in qstripped:
                used_query_types.append("Book")
            return {
                "search": {"ids": [109593], "results": ""},
                "authors": [{"contributions": []}],
            }

        src = _make_source()
        src._query = fake_query  # type: ignore[method-assign]
        await src.search_author("Jim Butcher")

        assert "Author" in used_query_types, (
            "search_author must call SEARCH_AUTHOR (query_type: \"Author\")"
        )
        assert "Book" not in used_query_types, (
            "v2.10.5 policy: never call SEARCH_QUERY (query_type: \"Book\") "
            "— that path returns books by title-text match and missed real "
            "bibliographies (the Jim Butcher 0-books bug)"
        )
        await src.close()


class TestBookMappingsPlatformCase:
    """v2.16.2 regression — the platform-name filter in the BookData
    fragment MUST be lowercase. Hardcover's `book_mappings.platform.name`
    is `goodreads` / `openlibrary` / `google` (lowercase). GraphQL
    `_in` is case-sensitive, so the TitleCase form shipped in v2.16.0
    / v2.16.1 returned zero rows against the live API — UAT
    2026-05-17 caught 5300 candidate books with zero stamps."""

    def test_fragment_uses_lowercase_platform_names(self):
        from app.discovery.sources.hardcover import FRAGMENTS

        assert '"goodreads"' in FRAGMENTS
        assert '"openlibrary"' in FRAGMENTS
        assert '"google"' in FRAGMENTS
        assert '"Goodreads"' not in FRAGMENTS
        assert '"OpenLibrary"' not in FRAGMENTS
        # Title-case "Google" alone is unambiguous-enough that a
        # presence check would mis-fire on doc comments. Just check
        # the filter clause shape:
        assert '_in: ["goodreads", "openlibrary", "google"]' in FRAGMENTS
