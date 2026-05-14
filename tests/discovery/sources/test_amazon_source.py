"""
End-to-end tests for the AmazonSource orchestrator
(v2.11.0 Stage 5++ commit 5/6).

Drives `AmazonSource.search_author` and `AmazonSource.get_author_books`
with a mock curl_cffi session, asserting the right HTTP calls happen
in the right order and the resulting AuthorResult carries the
expected BookResult / SeriesResult tree.

The supporting modules (parser, juvec client, author-id resolver) are
covered by their own focused test files; this file exercises the
orchestration glue.

Fixtures: the Sanderson allbooks HTML at
`tests/fixtures/amazon/sanderson_allbooks_page1.html` is the GET
response; synthetic /juvec responses (matching the parser-tested
shape) are the POST responses.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.discovery.sources.amazon import (
    AmazonSource,
    _is_amazon_author_id,
)
from app.discovery.sources.base import AuthorResult


FIXTURE_DIR = Path(__file__).parent.parent.parent / "fixtures" / "amazon"
SANDERSON_HTML = (FIXTURE_DIR / "sanderson_allbooks_page1.html").read_text()


# ─── Mock curl_cffi-style session ────────────────────────────────


class MockResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class MockSession:
    """Routes GETs by URL substring, POSTs by index. Records every
    call so tests can assert on ordering + bodies."""

    def __init__(
        self,
        get_routes: dict[str, MockResponse] | None = None,
        post_responses: list[MockResponse] | None = None,
    ):
        self.get_routes = get_routes or {}
        self.post_responses = list(post_responses or [])
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.closed = False

    async def get(self, url: str, timeout: float = 30.0):
        self.get_calls.append(url)
        for substring, resp in self.get_routes.items():
            if substring in url:
                return resp
        return MockResponse(status_code=404, text="")

    async def post(self, url: str, json: dict, timeout: float = 30.0):
        self.post_calls.append((url, json))
        if not self.post_responses:
            return MockResponse(status_code=503, text="")
        return self.post_responses.pop(0)

    async def close(self):
        self.closed = True


# ─── Synthetic /juvec response builder ──────────────────────────


def _juvec_response_json(
    *,
    asin_list: list[str] | None = None,
    total: int | None = None,
    products: list[dict] | None = None,
) -> str:
    """Live response shape (validated 2026-05-13): products + ASINList
    + totalResultCount at top level, `isSuccess: True`, plus a request-
    echo `content` field that the parser explicitly ignores."""
    body: dict = {
        "products": products or [],
        "isSuccess": True,
        "content": {"includeOutOfStock": True},  # request echo
    }
    if asin_list is not None:
        body["ASINList"] = asin_list
    if total is not None:
        body["totalResultCount"] = total
        body["totalCount"] = total
    raw = json.dumps(body)
    return raw + " " * max(0, 1100 - len(raw))  # bypass thin-body guard


def _product_dict(
    asin: str,
    title: str,
    binding: str = "kindle_edition",
    *,
    series_title: str | None = None,
    series_position: int | None = None,
    series_total: int | None = None,
    contributors: list[str] | None = None,
    media_matrix: list[tuple[str, str]] | None = None,
) -> dict:
    """Build the minimum product dict shape the parser accepts."""
    p = {
        "asin": asin,
        "title": {"displayString": title},
        "bindingInformation": {"binding": {
            "symbol": binding,
            "displayString": binding.replace("_", " ").title(),
        }},
        "byLine": {"contributors": [
            {"name": n} for n in (contributors or ["Brandon Sanderson"])
        ]},
        "mediaMatrix": {"items": [
            {
                "product": f"/marketplaces/ATVPDKIKX0DER/products/{vasin}",
                "binding": {"symbol": vsym, "displayString": vsym.replace("_", " ").title()},
            }
            for vsym, vasin in (media_matrix or [])
        ]},
        "detailPageLinkURL": f"/Book/dp/{asin}",
    }
    if series_title:
        p["bookSeriesInfo"] = {
            "seriesTitle": series_title,
            "position": series_position,
            "total": series_total,
        }
    return p


# ─── _is_amazon_author_id heuristic ──────────────────────────────


class TestIsAmazonAuthorId:
    def test_canonical_id_accepted(self):
        assert _is_amazon_author_id("B001IGFHW6") is True

    def test_name_rejected(self):
        assert _is_amazon_author_id("Brandon Sanderson") is False

    def test_lowercase_rejected(self):
        assert _is_amazon_author_id("b001igfhw6") is False

    def test_wrong_length_rejected(self):
        assert _is_amazon_author_id("B001") is False
        assert _is_amazon_author_id("B001IGFHW6XYZ") is False

    def test_empty_rejected(self):
        assert _is_amazon_author_id("") is False


# ─── AmazonSource.search_author ─────────────────────────────────


class TestSearchAuthor:
    async def test_returns_author_result_with_resolved_id(self):
        """search_author should call the resolver, set external_id
        to the resolved ID, and return a minimal AuthorResult."""
        # Use the Sanderson fixture as the /dp/{asin} response since
        # it contains the contributor path the tier-1 resolver wants.
        # (The fixture happens to be allbooks not /dp, but the
        # `/marketplaces/.../authors/B001IGFHW6` substring is the same.)
        session = MockSession(get_routes={
            # Tier-2 search fallback (since we don't pass known_book_asin)
            "/s?": MockResponse(200, _search_html("Brandon-Sanderson", "B001IGFHW6")),
        })
        source = AmazonSource()
        source._session = session
        source._session_init_attempted = True  # skip the curl_cffi factory

        result = await source.search_author("Brandon Sanderson")
        assert result is not None
        assert result.name == "Brandon Sanderson"
        assert result.external_id == "B001IGFHW6"
        assert result.books == []
        assert result.series == []

    async def test_returns_none_when_resolver_fails(self):
        session = MockSession(get_routes={
            "/s?": MockResponse(200, "<html>" + ("x" * 100_000) + "</html>"),
        })
        source = AmazonSource()
        source._session = session
        source._session_init_attempted = True

        result = await source.search_author("Nobody Knows")
        assert result is None

    async def test_returns_none_when_curl_cffi_missing(self):
        """No session available → graceful None, no HTTP fired."""
        source = AmazonSource()
        source._session = None
        source._session_init_attempted = True  # don't try to build one
        result = await source.search_author("Brandon Sanderson")
        assert result is None


def _search_html(slug: str, author_id: str) -> str:
    """Build a search-results HTML payload tier-2 will parse cleanly."""
    body = (
        f'<html><body>'
        f'<a href="/{slug}/e/{author_id}/ref=sr_aut">{slug.replace("-", " ")}</a>'
        f'<a href="/-/e/{author_id}">.</a>'
        f'</body></html>'
    )
    pad = "<!-- " + ("x" * (80_000 - len(body))) + " -->"
    return body + pad


# ─── AmazonSource.get_author_books ──────────────────────────────


class TestGetAuthorBooks:
    async def test_full_scan_with_fixture_no_extra_pages(self):
        """When called with a real Author Store ID and the Sanderson
        fixture as the allbooks response, we expect:
          - GET allbooks fired once
          - POST /juvec filter-application fired (Kindle + English
            differ from page defaults of allFormats / All Languages)
          - Result wraps the populated products into AuthorResult
            with series grouping intact
        """
        # First /juvec call: filter-application returns a small
        # filtered set. Subsequent detail-fetch is small enough that
        # we don't need additional pagination.
        filter_resp = _juvec_response_json(
            asin_list=["B002GYI9C4"],
            total=1,
            products=[
                _product_dict(
                    "B002GYI9C4", "Mistborn: The Final Empire",
                    series_title="Mistborn", series_position=1, series_total=7,
                    media_matrix=[("hardcover", "076531178X")],
                ),
            ],
        )

        session = MockSession(
            get_routes={
                "/stores/author/B001IGFHW6/allbooks": MockResponse(
                    200, SANDERSON_HTML,
                ),
            },
            post_responses=[MockResponse(200, filter_resp)],
        )
        source = AmazonSource(burst_delay_s=0.0)
        source._session = session
        source._session_init_attempted = True

        result = await source.get_author_books("B001IGFHW6")
        assert isinstance(result, AuthorResult)
        assert result.external_id == "B001IGFHW6"
        # Author name should be lifted from product contributors.
        assert result.name == "Brandon Sanderson"
        # One Mistborn book (Kindle, in series).
        assert len(result.series) == 1
        assert result.series[0].name == "Mistborn"
        assert len(result.series[0].books) == 1
        assert result.series[0].books[0].title == "Mistborn: The Final Empire"
        assert result.series[0].books[0].series_index == 1.0
        assert result.series[0].books[0].external_id == "B002GYI9C4"
        assert result.series[0].books[0].source == "amazon"
        assert result.series[0].books[0].source_url is not None
        assert "amazon.com" in result.series[0].books[0].source_url

    async def test_get_calls_resolver_when_name_passed(self):
        """Legacy state where authors.amazon_id holds a name (not an
        ID) — the source resolves it on the fly."""
        session = MockSession(get_routes={
            # tier-2 resolver hit
            "/s?": MockResponse(200, _search_html("Brandon-Sanderson", "B001IGFHW6")),
            # then allbooks
            "/stores/author/B001IGFHW6/allbooks": MockResponse(200, SANDERSON_HTML),
        }, post_responses=[
            MockResponse(200, _juvec_response_json(
                asin_list=[], total=0, products=[],
            )),
        ])
        source = AmazonSource(burst_delay_s=0.0)
        source._session = session
        source._session_init_attempted = True

        result = await source.get_author_books("Brandon Sanderson")
        assert result is not None
        assert result.external_id == "B001IGFHW6"
        # Resolver hit /s before allbooks.
        assert any("/s?" in c for c in session.get_calls)
        assert any("/allbooks" in c for c in session.get_calls)

    async def test_unresolvable_name_returns_none(self):
        session = MockSession(get_routes={
            # tier-2 returns no anchors → resolver returns None
            "/s?": MockResponse(200, "<html>" + ("y" * 80_000) + "</html>"),
        })
        source = AmazonSource(burst_delay_s=0.0)
        source._session = session
        source._session_init_attempted = True

        result = await source.get_author_books("Nobody In Amazon")
        assert result is None
        # No allbooks GET fired.
        assert not any("/allbooks" in c for c in session.get_calls)

    async def test_thin_body_allbooks_returns_none(self):
        """Akamai-thin allbooks GET = soft-block. Return None, log;
        don't try to parse a block-page."""
        session = MockSession(get_routes={
            "/stores/author/B001IGFHW6/allbooks": MockResponse(
                200, "<html>thin body</html>",
            ),
        })
        source = AmazonSource(burst_delay_s=0.0)
        source._session = session
        source._session_init_attempted = True

        result = await source.get_author_books("B001IGFHW6")
        assert result is None

    async def test_juvec_failure_falls_back_to_ssr_products(self):
        """If the filter-application POST fails, we still return
        whatever the SSR page already populated (85ish products).
        Better than nothing."""
        session = MockSession(
            get_routes={
                "/stores/author/B001IGFHW6/allbooks": MockResponse(
                    200, SANDERSON_HTML,
                ),
            },
            post_responses=[
                MockResponse(500, ""),
                MockResponse(500, ""),  # retry too
            ],
        )
        source = AmazonSource(burst_delay_s=0.0)
        source._session = session
        source._session_init_attempted = True

        result = await source.get_author_books("B001IGFHW6")
        assert result is not None
        # SSR had Mistborn Kindle Edition in the fixture; the source
        # filters to kindle_edition by default so this should land
        # in the result.
        all_books = list(result.books)
        for s in result.series:
            all_books.extend(s.books)
        assert any(b.title.startswith("Mistborn") for b in all_books), (
            f"expected at least one Mistborn book in SSR-fallback result; "
            f"got titles: {[b.title for b in all_books]}"
        )

    async def test_paginates_until_total_exhausted(self):
        """Filter response says total=200, page 1 returns 100; we
        should POST page 2."""
        page1 = _juvec_response_json(
            asin_list=[f"B{i:09d}" for i in range(100)],
            total=200,
            products=[
                _product_dict(f"B{i:09d}", f"Book {i}", contributors=["Brandon Sanderson"])
                for i in range(100)
            ],
        )
        page2 = _juvec_response_json(
            asin_list=[f"B{i:09d}" for i in range(100, 200)],
            total=200,
            products=[
                _product_dict(f"B{i:09d}", f"Book {i}", contributors=["Brandon Sanderson"])
                for i in range(100, 200)
            ],
        )
        session = MockSession(
            get_routes={
                "/stores/author/B001IGFHW6/allbooks": MockResponse(
                    200, SANDERSON_HTML,
                ),
            },
            post_responses=[
                MockResponse(200, page1),
                MockResponse(200, page2),
            ],
        )
        source = AmazonSource(burst_delay_s=0.0)
        source._session = session
        source._session_init_attempted = True

        result = await source.get_author_books("B001IGFHW6")
        assert result is not None
        # Should have fired two filter-application POSTs (page=1, page=2).
        filter_calls = [
            body for _url, body in session.post_calls
            if "authorSearch" in body
        ]
        pages_requested = [b["authorSearch"]["page"] for b in filter_calls]
        assert pages_requested == [1, 2], (
            f"expected POSTs for pages [1, 2]; got {pages_requested}"
        )

    async def test_default_filter_skips_filter_application_post(self):
        """When format='allFormats' + language='All Languages' (the
        page defaults), the source uses the SSR-populated products
        directly without a filter-application POST. Only detail-fetch
        POSTs may fire for any unpopulated tail.
        """
        # The Sanderson fixture's totalResultCount is 645 with 85
        # populated products. So a detail-fetch tail of 27 ASINs is
        # expected (112 page-1 ASINs - 85 populated). That's 2
        # detail-fetch batches at MAX_BATCH_SIZE=16. Plus pagination
        # up to MAX_PAGES if SSR's totalResultCount survives the
        # default-filter path.
        session = MockSession(
            get_routes={
                "/stores/author/B001IGFHW6/allbooks": MockResponse(
                    200, SANDERSON_HTML,
                ),
            },
            # Many empty responses for batch + pagination
            post_responses=[
                MockResponse(200, _juvec_response_json(
                    asin_list=[], total=None, products=[],
                ))
                for _ in range(20)
            ],
        )
        source = AmazonSource(
            burst_delay_s=0.0,
            format_filter="allFormats",
            language="All Languages",
        )
        source._session = session
        source._session_init_attempted = True

        await source.get_author_books("B001IGFHW6")
        # No page=1 filter-application POST — SSR data is reused.
        # Pages 2+ still fire as filter-application requests (each
        # carries authorSearch.page=N and no ASINList).
        page1_filter_calls = [
            body for _url, body in session.post_calls
            if "authorSearch" in body
            and "ASINList" not in body
            and body["authorSearch"]["page"] == 1
        ]
        assert page1_filter_calls == [], (
            "default filter should NOT re-fetch page 1 via /juvec when "
            "the SSR data is already in hand"
        )


# ─── Callbacks ──────────────────────────────────────────────────


class TestCallbacks:
    async def test_on_book_fires_per_book_with_title_string(self):
        """`_on_book(title: str)` is parameterized with a TITLE
        string, NOT the BookResult instance — see the def in
        `app/discovery/lookup.py` (writes
        `state._lookup_progress["current_book"]` which is serialized
        into the live-scan SSE feed; passing a dataclass crashes the
        frontend with React error #31)."""
        seen_titles: list[str] = []

        session = MockSession(
            get_routes={
                "/stores/author/B001IGFHW6/allbooks": MockResponse(
                    200, SANDERSON_HTML,
                ),
            },
            post_responses=[MockResponse(200, _juvec_response_json(
                asin_list=["B002GYI9C4", "B003"],
                total=2,
                products=[
                    _product_dict("B002GYI9C4", "Mistborn"),
                    _product_dict("B003", "The Way of Kings"),
                ],
            ))],
        )
        source = AmazonSource(burst_delay_s=0.0)
        source._session = session
        source._session_init_attempted = True
        source._on_book = lambda title: seen_titles.append(title)

        await source.get_author_books("B001IGFHW6")
        # Strings, not BookResult dataclasses.
        for t in seen_titles:
            assert isinstance(t, str), (
                f"on_book must receive a str title; got {type(t).__name__}"
            )
        assert "Mistborn" in seen_titles
        assert "The Way of Kings" in seen_titles

    async def test_on_new_candidate_fires_parameterless(self):
        """`_on_new_candidate()` is a parameterless tick counter
        — calling it with an arg raises TypeError, which gets
        DEBUG-logged + swallowed. Confirm we call it with NO args."""
        tick_count = [0]

        def on_new_candidate():
            tick_count[0] += 1

        session = MockSession(
            get_routes={
                "/stores/author/B001IGFHW6/allbooks": MockResponse(
                    200, SANDERSON_HTML,
                ),
            },
            post_responses=[MockResponse(200, _juvec_response_json(
                asin_list=["B002GYI9C4", "B003"],
                total=2,
                products=[
                    _product_dict("B002GYI9C4", "Mistborn"),
                    _product_dict("B003", "The Way of Kings"),
                ],
            ))],
        )
        source = AmazonSource(burst_delay_s=0.0)
        source._session = session
        source._session_init_attempted = True
        source._on_new_candidate = on_new_candidate

        await source.get_author_books("B001IGFHW6")
        assert tick_count[0] >= 2, (
            f"expected at least 2 ticks for 2 books; got {tick_count[0]}"
        )

    async def test_callback_exception_does_not_kill_scan(self):
        """A buggy _on_book callback that raises should be logged
        and swallowed; the scan continues."""
        session = MockSession(
            get_routes={
                "/stores/author/B001IGFHW6/allbooks": MockResponse(
                    200, SANDERSON_HTML,
                ),
            },
            post_responses=[MockResponse(200, _juvec_response_json(
                asin_list=["B002GYI9C4"], total=1,
                products=[_product_dict("B002GYI9C4", "Mistborn")],
            ))],
        )
        source = AmazonSource(burst_delay_s=0.0)
        source._session = session
        source._session_init_attempted = True
        source._on_book = lambda t: (_ for _ in ()).throw(RuntimeError("buggy"))

        result = await source.get_author_books("B001IGFHW6")
        assert result is not None


# ─── close() ─────────────────────────────────────────────────────


class TestClose:
    async def test_close_closes_session(self):
        session = MockSession()
        source = AmazonSource()
        source._session = session
        source._session_init_attempted = True
        await source.close()
        assert session.closed is True


class TestAmazonFormatAsins:
    """v2.11.1: each BookResult emitted by AmazonSource carries the
    mediaMatrix cross-reference map JSON-encoded in
    `amazon_format_asins`. The merge layer persists it to
    `books.amazon_format_asins`."""

    async def test_book_carries_mediamatrix_json(self):
        """Mistborn has Kindle / Hardcover / Paperback / Mass Market /
        Audible / Preloaded Digital Audio variants in the SSR JSON.
        The emitted BookResult should carry a JSON map matching."""
        import json as _json
        session = MockSession(
            get_routes={
                "/stores/author/B001IGFHW6/allbooks": MockResponse(
                    200, SANDERSON_HTML,
                ),
            },
            post_responses=[MockResponse(200, _juvec_response_json(
                asin_list=["B002GYI9C4"],
                total=1,
                products=[
                    _product_dict(
                        "B002GYI9C4", "Mistborn",
                        series_title="Mistborn", series_position=1,
                        media_matrix=[
                            ("kindle_edition", "B002GYI9C4"),
                            ("hardcover", "076531178X"),
                            ("paperback", "1250868289"),
                            ("audio_download", "B001QKBHG4"),
                        ],
                    ),
                ],
            ))],
        )
        source = AmazonSource(burst_delay_s=0.0)
        source._session = session
        source._session_init_attempted = True

        result = await source.get_author_books("B001IGFHW6")
        assert result is not None
        all_books = list(result.books)
        for s in result.series:
            all_books.extend(s.books)
        mistborn = next(b for b in all_books if b.title == "Mistborn")
        assert mistborn.amazon_format_asins is not None
        parsed = _json.loads(mistborn.amazon_format_asins)
        assert parsed == {
            "kindle_edition": "B002GYI9C4",
            "hardcover": "076531178X",
            "paperback": "1250868289",
            "audio_download": "B001QKBHG4",
        }

    async def test_book_with_no_mediamatrix_emits_none(self):
        """A product entry with an empty mediaMatrix.items array
        should produce amazon_format_asins=None on the BookResult."""
        session = MockSession(
            get_routes={
                "/stores/author/B001IGFHW6/allbooks": MockResponse(
                    200, SANDERSON_HTML,
                ),
            },
            post_responses=[MockResponse(200, _juvec_response_json(
                asin_list=["B003"],
                total=1,
                products=[
                    _product_dict(
                        "B003", "Solo Title",
                        series_title=None,
                        media_matrix=[],
                    ),
                ],
            ))],
        )
        source = AmazonSource(burst_delay_s=0.0)
        source._session = session
        source._session_init_attempted = True

        result = await source.get_author_books("B001IGFHW6")
        assert result is not None
        all_books = list(result.books)
        for s in result.series:
            all_books.extend(s.books)
        solo = next(b for b in all_books if b.title == "Solo Title")
        assert solo.amazon_format_asins is None

    async def test_json_is_stable_across_reruns(self):
        """sort_keys=True in the JSON dump means a re-scan that
        returns the same variants produces a byte-identical string.
        Lets the merge layer's deferred change-detection logic
        (if any) skip writes for unchanged records."""
        import json as _json
        session = MockSession(
            get_routes={
                "/stores/author/B001IGFHW6/allbooks": MockResponse(
                    200, SANDERSON_HTML,
                ),
            },
            post_responses=[MockResponse(200, _juvec_response_json(
                asin_list=["B002GYI9C4"],
                total=1,
                products=[
                    _product_dict(
                        "B002GYI9C4", "Mistborn",
                        media_matrix=[
                            ("hardcover", "076531178X"),
                            ("kindle_edition", "B002GYI9C4"),
                            ("paperback", "1250868289"),
                        ],
                    ),
                ],
            ))],
        )
        source = AmazonSource(burst_delay_s=0.0)
        source._session = session
        source._session_init_attempted = True

        result = await source.get_author_books("B001IGFHW6")
        all_books = list(result.books)
        for s in result.series:
            all_books.extend(s.books)
        mistborn = next(b for b in all_books if b.title == "Mistborn")
        # Keys should be alphabetically sorted regardless of source
        # ordering — hardcover < kindle_edition < paperback.
        assert mistborn.amazon_format_asins is not None
        decoded = _json.loads(mistborn.amazon_format_asins)
        assert list(decoded.keys()) == sorted(decoded.keys())
