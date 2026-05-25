"""Unit tests for app.metadata.sources.amazon — the live enricher
AmazonSource (distinct from the discovery-side cache-worker source).

Mocks the source's `_fetch` coroutine so no real HTTP fires.
"""
from __future__ import annotations

import pytest

from app.metadata.sources.amazon import AmazonSource


# ─── fixtures ────────────────────────────────────────────────


def _search_html(*asins_with_titles: tuple[str, str]) -> str:
    """Build a fake /s search-results page with N product links.

    Each tuple is (asin, title_text); the helper inserts them inside a
    div with `data-component-type="s-search-results"` so the parser's
    primary extraction path fires.
    """
    rows = []
    for asin, title in asins_with_titles:
        rows.append(
            f'<a href="/dp/{asin}/ref=sr_1_1"><span>{title}</span></a>'
        )
    return (
        '<html><body>'
        '<div data-component-type="s-search-results">'
        + "\n".join(rows)
        + "</div></body></html>"
    )


def _kindle_detail_html(asin: str, title: str = "A Real Book") -> str:
    """Detail page that `_parse_detail_page` accepts as a Kindle book."""
    return f"""
    <html><body>
      <h1 id="productTitle">{title}</h1>
      <div id="rpi-attribute-book_details-ebook_pages">
        <span class="rpi-attribute-label"><span>Print length</span></span>
        <span class="rpi-attribute-value"><span>320 pages</span></span>
      </div>
      <div id="rpi-attribute-book_details-publication_date">
        <span class="rpi-attribute-label"><span>Publication date</span></span>
        <span class="rpi-attribute-value"><span>May 1, 2024</span></span>
      </div>
    </body></html>
    """


def _audiobook_detail_html(asin: str, title: str = "Audio Edition") -> str:
    """Detail page that `_parse_detail_page` rejects (RPI mentions Audible)."""
    return f"""
    <html><body>
      <h1 id="productTitle">{title}</h1>
      <div id="rpi-attribute-audiobook_details-format">
        <span class="rpi-attribute-label"><span>Format</span></span>
        <span class="rpi-attribute-value"><span>Audible Audiobook</span></span>
      </div>
    </body></html>
    """


def _preorder_detail_html(asin: str) -> str:
    """Detail page that `_parse_detail_page` rejects (pre-order signal)."""
    return f"""
    <html><body>
      <h1 id="productTitle">Upcoming Release</h1>
      <input name="submit.preorder" type="submit" />
    </body></html>
    """


@pytest.fixture
def source(monkeypatch):
    """An AmazonSource with `_fetch` swapped for a stubbed coroutine."""
    src = AmazonSource(rate_limit=0)
    fetches: list[str] = []
    fetch_responses: dict[str, str] = {}

    async def fake_fetch(url, params=None):
        # Search URL goes through params; detail URLs include /dp/ASIN.
        if "/dp/" in url:
            fetches.append(url)
            return fetch_responses.get(url)
        # Search url + params encoded
        key = "SEARCH"
        fetches.append(key)
        return fetch_responses.get(key)

    monkeypatch.setattr(src, "_fetch", fake_fetch)
    return src, fetches, fetch_responses


# ─── F2 — audiobook retry ────────────────────────────────────


class TestF2AudiobookRetry:
    async def test_top_audiobook_falls_through_to_kindle_at_rank_2(
        self, source,
    ):
        src, fetches, responses = source
        # The audiobook listing has the cleanest title match ("The
        # Tale" verbatim) so it outranks the Kindle hit on score —
        # exactly the Master Alvin scenario from the F2 plan. Without
        # F2's retry, the audiobook detail fetch returns None at
        # `_parse_detail_page` and the whole call would return None.
        responses["SEARCH"] = _search_html(
            ("B000AUDIO0", "The Tale"),
            ("B000KINDLE", "The Tale: A Novel"),
            ("B000OTHER0", "Another Book"),
        )
        responses[
            "https://www.amazon.com/dp/B000AUDIO0"
        ] = _audiobook_detail_html("B000AUDIO0")
        responses[
            "https://www.amazon.com/dp/B000KINDLE"
        ] = _kindle_detail_html("B000KINDLE", title="The Tale: A Novel")
        responses[
            "https://www.amazon.com/dp/B000OTHER0"
        ] = _kindle_detail_html("B000OTHER0", title="Another Book")

        result = await src.search_book("The Tale", "Author Name")

        assert result is not None
        assert result.external_id == "B000KINDLE"
        # Two detail fetches: audiobook rejected, kindle accepted. The
        # rank-3 candidate must NOT have been fetched.
        detail_fetches = [u for u in fetches if "/dp/" in u]
        assert "https://www.amazon.com/dp/B000AUDIO0" in detail_fetches
        assert "https://www.amazon.com/dp/B000KINDLE" in detail_fetches
        assert "https://www.amazon.com/dp/B000OTHER0" not in detail_fetches

    async def test_all_three_audiobooks_returns_none(self, source):
        src, fetches, responses = source
        responses["SEARCH"] = _search_html(
            ("B000AUDIO1", "Series 1 (Audible Audiobook)"),
            ("B000AUDIO2", "Series 2 (Audible Audiobook)"),
            ("B000AUDIO3", "Series 3 (Audible Audiobook)"),
        )
        for asin in ("B000AUDIO1", "B000AUDIO2", "B000AUDIO3"):
            responses[
                f"https://www.amazon.com/dp/{asin}"
            ] = _audiobook_detail_html(asin)

        result = await src.search_book("Series", "Author")

        assert result is None
        # Cap at 3 detail fetches — no infinite loop, no extra requests.
        detail_fetches = [u for u in fetches if "/dp/" in u]
        assert len(detail_fetches) == 3

    async def test_preorder_falls_through_to_shipping(self, source):
        src, fetches, responses = source
        responses["SEARCH"] = _search_html(
            ("B000PREORD", "Upcoming Release"),
            ("B000SHIPPN", "Available Now: A Novel"),
        )
        responses[
            "https://www.amazon.com/dp/B000PREORD"
        ] = _preorder_detail_html("B000PREORD")
        responses[
            "https://www.amazon.com/dp/B000SHIPPN"
        ] = _kindle_detail_html(
            "B000SHIPPN", title="Available Now: A Novel",
        )

        result = await src.search_book("Available Now", "Author")
        assert result is not None
        assert result.external_id == "B000SHIPPN"

    async def test_junk_title_skipped_before_detail_fetch(self, source):
        """`_RX_JUNK_TITLE` should drop third-party seller titles
        before they consume a detail-page request."""
        src, fetches, responses = source
        responses["SEARCH"] = _search_html(
            ("B00JUNK001", "[(Kingdom's Hope )] [Author: Chuck Black]"),
            ("B0GOODKK01", "Kingdom's Hope"),
        )
        responses[
            "https://www.amazon.com/dp/B0GOODKK01"
        ] = _kindle_detail_html("B0GOODKK01", title="Kingdom's Hope")

        result = await src.search_book("Kingdom's Hope", "Chuck Black")
        assert result is not None
        assert result.external_id == "B0GOODKK01"
        # Junk row must NOT have been fetched.
        detail_fetches = [u for u in fetches if "/dp/" in u]
        assert "https://www.amazon.com/dp/B00JUNK001" not in detail_fetches

    async def test_empty_search_returns_none(self, source):
        src, fetches, responses = source
        responses["SEARCH"] = "<html><body></body></html>"
        result = await src.search_book("Anything", "Author")
        assert result is None
        # No detail fetches when there are no candidates.
        assert all("/dp/" not in u for u in fetches)


# ─── F1 — cache-first lookup ─────────────────────────────────


class TestF1CacheFirst:
    """When ``author_amazon_id`` is known and the cache holds rows for
    that author, the search short-circuits to a cache scoring pass
    plus a single detail fetch. On miss, the worker queue is bumped
    and the live ``/s`` flow runs."""

    async def test_cache_hit_uses_cache_then_one_detail_fetch(
        self, source, monkeypatch,
    ):
        src, fetches, responses = source

        async def fake_read(*, source_name, author_id, library_slug,
                            book_format, language):
            assert author_id == "B0C0AUTHOR"
            assert book_format == "kindle_edition"
            return [
                {
                    "title": "The Tale: A Novel",
                    "book_asin": "B000CACHEK",
                    "series_name": "Tale Series",
                    "series_pos": 1.0,
                    "cover_url": "https://amzn/cover.jpg",
                    "language": "English",
                    "isbn": None,
                },
            ]

        async def fake_enqueue(**kw):
            raise AssertionError("ensure_enqueued must NOT fire on hit")

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        monkeypatch.setattr(
            metadata_cache_reader, "ensure_enqueued", fake_enqueue,
        )
        responses[
            "https://www.amazon.com/dp/B000CACHEK"
        ] = _kindle_detail_html("B000CACHEK", title="The Tale: A Novel")

        result = await src.search_book(
            "The Tale", "Author",
            author_amazon_id="B0C0AUTHOR",
            library_slug="calibre-library",
        )

        assert result is not None
        assert result.external_id == "B000CACHEK"
        assert getattr(result, "_from_cache", False) is True
        # Cover URL filled from the cache row.
        assert result.cover_url == "https://amzn/cover.jpg"
        # Series filled from cache row.
        assert result.series == "Tale Series"
        # Authors populated with the search author so the enricher's
        # re-score sees author overlap (otherwise cache hits land
        # below the 0.8 accept_confidence threshold via title-only
        # scoring — observed live as Master Alvin at 0.77 → below_threshold).
        assert result.authors == ["Author"]
        # Exactly ONE detail fetch — no /s call, no second detail.
        detail_fetches = [u for u in fetches if "/dp/" in u]
        assert detail_fetches == ["https://www.amazon.com/dp/B000CACHEK"]
        # /s never queried.
        assert "SEARCH" not in fetches

    async def test_cache_miss_enqueues_and_falls_back_to_live(
        self, source, monkeypatch,
    ):
        src, fetches, responses = source
        enqueued: list[dict] = []

        async def fake_read(**_):
            return []  # no cached rows

        async def fake_enqueue(**kw):
            enqueued.append(kw)
            return True

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        monkeypatch.setattr(
            metadata_cache_reader, "ensure_enqueued", fake_enqueue,
        )
        # Live fallback: a clean Kindle hit.
        responses["SEARCH"] = _search_html(
            ("B00LIVEK01", "The Tale: A Novel"),
        )
        responses[
            "https://www.amazon.com/dp/B00LIVEK01"
        ] = _kindle_detail_html("B00LIVEK01", title="The Tale: A Novel")

        result = await src.search_book(
            "The Tale", "Author",
            author_amazon_id="B0C0AUTHOR",
            library_slug="calibre-library",
        )

        assert result is not None
        assert result.external_id == "B00LIVEK01"
        assert getattr(result, "_from_cache", False) is False
        # Enqueue fired with the right shape.
        assert len(enqueued) == 1
        assert enqueued[0]["author_id"] == "B0C0AUTHOR"
        assert enqueued[0]["priority"] == 1000.0
        assert enqueued[0]["enqueued_reason"] == "enrich_miss"

    async def test_empty_amazon_author_id_skips_cache_phase(
        self, source, monkeypatch,
    ):
        src, fetches, responses = source

        async def fake_read(**_):
            raise AssertionError(
                "read_books_by_author must NOT fire when "
                "author_amazon_id is missing"
            )

        async def fake_enqueue(**_):
            raise AssertionError(
                "ensure_enqueued must NOT fire on no-author-id call"
            )

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        monkeypatch.setattr(
            metadata_cache_reader, "ensure_enqueued", fake_enqueue,
        )
        responses["SEARCH"] = _search_html(
            ("B00LIVEK02", "The Tale: A Novel"),
        )
        responses[
            "https://www.amazon.com/dp/B00LIVEK02"
        ] = _kindle_detail_html("B00LIVEK02", title="The Tale: A Novel")

        result = await src.search_book("The Tale", "Author")
        assert result is not None
        assert result.external_id == "B00LIVEK02"

    async def test_non_asin_shaped_author_id_skips_cache_phase(
        self, source, monkeypatch,
    ):
        """Legacy pre-v2.11.0 installs stored author names in the
        ``amazon_id`` column. Anything not matching the 10-char ASIN
        shape skips the cache phase entirely."""
        src, fetches, responses = source

        async def fake_read(**_):
            raise AssertionError("cache phase must skip on legacy id shape")

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        responses["SEARCH"] = _search_html(
            ("B00LIVEK03", "Title"),
        )
        responses[
            "https://www.amazon.com/dp/B00LIVEK03"
        ] = _kindle_detail_html("B00LIVEK03", title="Title")

        result = await src.search_book(
            "Title", "Author",
            author_amazon_id="Orson Scott Card",  # legacy name-as-id
            library_slug="calibre-library",
        )
        assert result is not None

    async def test_volume_agreement_tiebreaker_picks_book_one(
        self, source, monkeypatch,
    ):
        """Regression test for the Idle Village Hero issue. Author has
        four books in a series; the search title for book 1 has no
        volume marker. Pure title scoring ties all four candidates at
        the same score; the volume-agreement tiebreaker must pick the
        no-volume candidate (book 1)."""
        src, fetches, responses = source

        async def fake_read(**_):
            return [
                {
                    "title": "Idle Village Hero 4: A Town-Building LitRPG Adventure",
                    "book_asin": "B0BOOK0004",
                    "series_name": "Idle Village Hero",
                    "series_pos": 4.0,
                    "cover_url": None, "language": None, "isbn": None,
                },
                {
                    "title": "Idle Village Hero 2: A Town-Building LitRPG Adventure",
                    "book_asin": "B0BOOK0002",
                    "series_name": "Idle Village Hero",
                    "series_pos": 2.0,
                    "cover_url": None, "language": None, "isbn": None,
                },
                {
                    "title": "Idle Village Hero: A Town-Building LitRPG Adventure",
                    "book_asin": "B0BOOK0001",
                    "series_name": "Idle Village Hero",
                    "series_pos": 1.0,
                    "cover_url": None, "language": None, "isbn": None,
                },
                {
                    "title": "Idle Village Hero 3: A Town-Building LitRPG Adventure",
                    "book_asin": "B0BOOK0003",
                    "series_name": "Idle Village Hero",
                    "series_pos": 3.0,
                    "cover_url": None, "language": None, "isbn": None,
                },
            ]

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        responses[
            "https://www.amazon.com/dp/B0BOOK0001"
        ] = _kindle_detail_html("B0BOOK0001", title="Idle Village Hero")

        result = await src.search_book(
            "Idle Village Hero: A Slice-of-Life LitRPG Adventure",
            "Leon West",
            author_amazon_id="B0CMJC56GQ",
            library_slug="calibre-library",
        )

        assert result is not None
        # Volume-agreement tiebreaker picks book 1 (no volume marker
        # matches the query's no-volume marker).
        assert result.external_id == "B0BOOK0001"

    async def test_volume_agreement_picks_matching_volume(
        self, source, monkeypatch,
    ):
        """When the query explicitly carries a volume marker, the
        tiebreaker prefers the same-volume cached row over siblings."""
        src, fetches, responses = source

        async def fake_read(**_):
            return [
                {
                    "title": "Idle Village Hero: A Town-Building LitRPG Adventure",
                    "book_asin": "B0BOOK0001",
                    "series_name": None, "series_pos": None,
                    "cover_url": None, "language": None, "isbn": None,
                },
                {
                    "title": "Idle Village Hero 3: A Town-Building LitRPG Adventure",
                    "book_asin": "B0BOOK0003",
                    "series_name": None, "series_pos": None,
                    "cover_url": None, "language": None, "isbn": None,
                },
                {
                    "title": "Idle Village Hero 2: A Town-Building LitRPG Adventure",
                    "book_asin": "B0BOOK0002",
                    "series_name": None, "series_pos": None,
                    "cover_url": None, "language": None, "isbn": None,
                },
            ]

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        responses[
            "https://www.amazon.com/dp/B0BOOK0003"
        ] = _kindle_detail_html("B0BOOK0003", title="Idle Village Hero 3")

        result = await src.search_book(
            "Idle Village Hero 3: A Town-Building LitRPG",
            "Leon West",
            author_amazon_id="B0CMJC56GQ",
            library_slug="calibre-library",
        )

        assert result is not None
        assert result.external_id == "B0BOOK0003"

    async def test_cache_hit_low_score_falls_back_to_live(
        self, source, monkeypatch,
    ):
        """Cached rows that don't score above 0.5 against the query
        should fall through to the live ``/s`` flow rather than picking
        an obviously wrong book."""
        src, fetches, responses = source

        async def fake_read(**_):
            return [
                {
                    "title": "Some Unrelated Book",
                    "book_asin": "B00IRRELEV",
                    "series_name": None,
                    "series_pos": None,
                    "cover_url": None,
                    "language": None,
                    "isbn": None,
                },
            ]

        async def fake_enqueue(**kw):
            # Score-too-low is NOT a cache miss — the cache HAS rows,
            # they're just irrelevant. Don't enqueue.
            raise AssertionError("enqueue must NOT fire on low-score hit")

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        monkeypatch.setattr(
            metadata_cache_reader, "ensure_enqueued", fake_enqueue,
        )
        responses["SEARCH"] = _search_html(
            ("B00LIVELO0", "The Tale"),
        )
        responses[
            "https://www.amazon.com/dp/B00LIVELO0"
        ] = _kindle_detail_html("B00LIVELO0", title="The Tale")

        result = await src.search_book(
            "The Tale", "Author",
            author_amazon_id="B0C0AUTHOR",
            library_slug="calibre-library",
        )
        assert result is not None
        assert result.external_id == "B00LIVELO0"
        # Live /s fired (low-score cache hit fell through).
        assert "SEARCH" in fetches


# ─── F4 — Author Store fallback (Tier 3) ────────────────────


def _make_product(
    *, asin: str, title: str,
    binding_symbol: str = "kindle_edition",
    contributors: tuple = (),
    series_title: str | None = None,
    series_position: int | None = None,
    cover_url: str | None = None,
):
    """Build a widget-parser Product object suitable for Tier 3 tests."""
    from app.discovery.sources.amazon_widget_parser import Product
    return Product(
        asin=asin,
        title=title,
        contributors=contributors,
        binding_symbol=binding_symbol,
        binding_display=binding_symbol.replace("_", " ").title(),
        series_title=series_title,
        series_position=series_position,
        series_total=None,
        detail_page_link=f"/dp/{asin}",
        cover_url=cover_url,
        media_matrix=(),
        genres=(),
    )


def _make_page_data(*products):
    """Wrap a tuple of Product objects in an AllBooksPageData."""
    from app.discovery.sources.amazon_widget_parser import AllBooksPageData
    return AllBooksPageData(
        author_id="B0C0AUTHOR",
        store_id="store-id",
        root_page_id="root-page-id",
        version="v1",
        slate_token="",
        fresh_cart_csrf_token="",
        amazon_api_csrf_token="",
        visit_id="",
        obfuscated_marketplace_id="",
        asin_list=tuple(p.asin for p in products),
        products=tuple(products),
        total_result_count=len(products),
        total_count=len(products),
        available_languages=(),
    )


class TestF4AuthorStoreFallback:
    """v2.31.0 Tier 3 — when cache (Tier 1) AND /s (Tier 2) both miss,
    and `author_amazon_id` is ASIN-shaped, fetch the Author Store
    storefront directly. URL is keyed on the verified author ID so
    title hits are author-identity-guaranteed (treated as
    `_from_cache=True` for merge-gate bypass)."""

    async def test_tier3_fires_when_cache_and_search_both_miss(
        self, source, monkeypatch,
    ):
        src, fetches, responses = source

        async def fake_read(**_):
            return []  # cache miss — Tier 1 returns None, /s runs

        async def fake_enqueue(**_):
            return True  # F1 enqueue-on-miss fires; doesn't matter here

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        monkeypatch.setattr(
            metadata_cache_reader, "ensure_enqueued", fake_enqueue,
        )

        # Tier 2 (/s) returns an empty result set — no candidates.
        responses["SEARCH"] = (
            "<html><body><div data-component-type='s-search-results'>"
            "</div></body></html>"
        )

        # Tier 3 storefront fetch — sentinel HTML routed past the
        # real widget parser via the mock below.
        async def fake_author_store_fetch(author_id):
            assert author_id == "B0C0AUTHOR"
            return "STOREFRONT_HTML"

        monkeypatch.setattr(
            src, "_fetch_author_store_html", fake_author_store_fetch,
        )

        from app.discovery.sources import amazon_widget_parser
        page_data = _make_page_data(
            _make_product(
                asin="B0STORE001", title="The Tale: A Novel",
                contributors=("Author",),
                cover_url="https://amzn/store-cover.jpg",
                series_title="Tale Series",
                series_position=1,
            ),
            _make_product(
                asin="B0STORE002", title="Some Other Work",
                binding_symbol="kindle_edition",
            ),
        )
        monkeypatch.setattr(
            amazon_widget_parser, "parse_allbooks_html",
            lambda html: page_data,
        )

        responses[
            "https://www.amazon.com/dp/B0STORE001"
        ] = _kindle_detail_html("B0STORE001", title="The Tale: A Novel")

        result = await src.search_book(
            "The Tale", "Author",
            author_amazon_id="B0C0AUTHOR",
            library_slug="calibre-library",
        )

        assert result is not None
        assert result.external_id == "B0STORE001"
        # Tier 3 hits get the cache-style treatment so the enricher's
        # merge gate bypass applies.
        assert getattr(result, "_from_cache", False) is True
        # Widget fields fill detail-page gaps.
        assert result.cover_url == "https://amzn/store-cover.jpg"
        assert result.series == "Tale Series"
        # Author populated from the search author (same reason as F1).
        assert result.authors == ["Author"]

    async def test_tier3_skipped_when_author_id_not_asin_shape(
        self, source, monkeypatch,
    ):
        """Author Store URL needs a real ASIN — legacy name-as-id rows
        from pre-v2.11.0 must not trigger a 404 fetch."""
        src, fetches, responses = source

        responses["SEARCH"] = (
            "<html><body><div data-component-type='s-search-results'>"
            "</div></body></html>"
        )

        async def must_not_fire(author_id):
            raise AssertionError(
                "Author Store fetch must NOT fire for non-ASIN id"
            )

        monkeypatch.setattr(
            src, "_fetch_author_store_html", must_not_fire,
        )

        result = await src.search_book(
            "The Tale", "Author",
            author_amazon_id="Orson Scott Card",  # legacy name-as-id
        )
        assert result is None

    async def test_tier3_skipped_when_no_author_id(
        self, source, monkeypatch,
    ):
        """No author_amazon_id at all — Tier 3 has no URL to query."""
        src, fetches, responses = source

        responses["SEARCH"] = (
            "<html><body><div data-component-type='s-search-results'>"
            "</div></body></html>"
        )

        async def must_not_fire(author_id):
            raise AssertionError(
                "Author Store fetch must NOT fire without author_amazon_id"
            )

        monkeypatch.setattr(
            src, "_fetch_author_store_html", must_not_fire,
        )

        result = await src.search_book("The Tale", "Author")
        assert result is None

    async def test_tier3_audiobook_hint_filters_by_audio_download_binding(
        self, source, monkeypatch,
    ):
        """When `_audiobook_hint` is True the storefront filter targets
        the Audible binding (`audio_download`), not Kindle."""
        src, fetches, responses = source
        src._audiobook_hint = True

        async def fake_read(**_):
            return []
        async def fake_enqueue(**_):
            return True

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        monkeypatch.setattr(
            metadata_cache_reader, "ensure_enqueued", fake_enqueue,
        )
        responses["SEARCH"] = (
            "<html><body><div data-component-type='s-search-results'>"
            "</div></body></html>"
        )

        async def fake_author_store_fetch(_id):
            return "STOREFRONT_HTML"
        monkeypatch.setattr(
            src, "_fetch_author_store_html", fake_author_store_fetch,
        )

        # Mix bindings — Kindle hit has the better title score but
        # should be ignored because we're in audiobook mode.
        from app.discovery.sources import amazon_widget_parser
        page_data = _make_page_data(
            _make_product(
                asin="B0KINDLE01", title="The Tale: A Novel",
                binding_symbol="kindle_edition",
            ),
            _make_product(
                asin="B0AUDIO001", title="The Tale: A Novel",
                binding_symbol="audio_download",
                cover_url="https://amzn/audio-cover.jpg",
            ),
        )
        monkeypatch.setattr(
            amazon_widget_parser, "parse_allbooks_html",
            lambda html: page_data,
        )
        responses[
            "https://www.amazon.com/dp/B0AUDIO001"
        ] = _kindle_detail_html("B0AUDIO001", title="The Tale: A Novel")

        result = await src.search_book(
            "The Tale", "Author",
            author_amazon_id="B0C0AUTHOR",
            library_slug="calibre-library",
        )
        assert result is not None
        assert result.external_id == "B0AUDIO001"

    async def test_tier3_returns_none_when_storefront_fetch_fails(
        self, source, monkeypatch,
    ):
        """Soft-block, transport error, or non-200 from the storefront
        fetch — Tier 3 must degrade to None (caller's overall result)."""
        src, fetches, responses = source

        async def fake_read(**_):
            return []
        async def fake_enqueue(**_):
            return True

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        monkeypatch.setattr(
            metadata_cache_reader, "ensure_enqueued", fake_enqueue,
        )
        responses["SEARCH"] = (
            "<html><body><div data-component-type='s-search-results'>"
            "</div></body></html>"
        )

        async def fetch_fails(_id):
            return None  # storefront fetch returned None (soft-block etc.)
        monkeypatch.setattr(
            src, "_fetch_author_store_html", fetch_fails,
        )

        result = await src.search_book(
            "The Tale", "Author",
            author_amazon_id="B0C0AUTHOR",
        )
        assert result is None

    async def test_tier3_parse_error_returns_none(
        self, source, monkeypatch,
    ):
        """Widget parse failed (ParseError / SoftBlockSuspected) —
        Tier 3 must degrade quietly without crashing the enrichment."""
        src, fetches, responses = source

        async def fake_read(**_):
            return []
        async def fake_enqueue(**_):
            return True

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        monkeypatch.setattr(
            metadata_cache_reader, "ensure_enqueued", fake_enqueue,
        )
        responses["SEARCH"] = (
            "<html><body><div data-component-type='s-search-results'>"
            "</div></body></html>"
        )

        async def fake_fetch(_id):
            return "BROKEN_HTML"
        monkeypatch.setattr(
            src, "_fetch_author_store_html", fake_fetch,
        )

        from app.discovery.sources import amazon_widget_parser

        def raise_parse(_):
            raise amazon_widget_parser.SoftBlockSuspectedError(
                "ProductGrid marker absent — soft-block suspected"
            )
        monkeypatch.setattr(
            amazon_widget_parser, "parse_allbooks_html", raise_parse,
        )

        result = await src.search_book(
            "The Tale", "Author",
            author_amazon_id="B0C0AUTHOR",
        )
        assert result is None

    async def test_tier3_author_only_floor_rejects_unrelated_books(
        self, source, monkeypatch,
    ):
        """Regression for the live UAT finding: the Author Store URL
        is keyed on the verified author, so EVERY product on the page
        matches on author and ``score_match`` floors at exactly 0.300
        (the ``0.3 * author`` term with zero title contribution). If
        the threshold isn't tight enough every candidate trivially
        passes and Tier 3 returns the first parseable book regardless
        of whether the right book is actually in the SSR widget. The
        threshold must be above 0.30 — empirically 0.40 lets the
        IVH-1 case clear at ~0.475 while blocking pure author hits."""
        src, fetches, responses = source

        async def fake_read(**_):
            return []
        async def fake_enqueue(**_):
            return True

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        monkeypatch.setattr(
            metadata_cache_reader, "ensure_enqueued", fake_enqueue,
        )
        responses["SEARCH"] = (
            "<html><body><div data-component-type='s-search-results'>"
            "</div></body></html>"
        )

        async def fake_fetch(_id):
            return "STOREFRONT_HTML"
        monkeypatch.setattr(
            src, "_fetch_author_store_html", fake_fetch,
        )

        # All products are by "Leon West" — author match floors them
        # at 0.300 — but NONE of the titles overlap with the search
        # term "Idle Village Hero". Pre-fix Tier 3 would have returned
        # the first parseable one; post-fix it must return None.
        from app.discovery.sources import amazon_widget_parser
        page_data = _make_page_data(
            _make_product(
                asin="B0WRONGRA1",
                title="Rise of the Class Smith: A Dungeon Building LitRPG Adventure",
                contributors=("Leon West",),
            ),
            _make_product(
                asin="B0WRONGAM2",
                title="Isle of the Amazonian Elves: A Fateforged Adventure",
                contributors=("Leon West",),
            ),
            _make_product(
                asin="B0WRONGCH3",
                title="Dungeon Champions Omnibus",
                contributors=("Leon West",),
            ),
        )
        monkeypatch.setattr(
            amazon_widget_parser, "parse_allbooks_html",
            lambda html: page_data,
        )

        # If Tier 3 ever fired a detail fetch we'd see it in `fetches`.
        # A `_kindle_detail_html` response is staged for B0WRONGRA1 in
        # case the threshold regressed — the assertion below would then
        # surface that as a returned record instead of a silent miss.
        responses[
            "https://www.amazon.com/dp/B0WRONGRA1"
        ] = _kindle_detail_html("B0WRONGRA1", title="Rise of the Class Smith")

        result = await src.search_book(
            "Idle Village Hero", "Leon West",
            author_amazon_id="B0CMJC56GQ",
        )

        assert result is None, (
            f"Tier 3 must reject author-only matches but returned "
            f"{getattr(result, 'external_id', None)!r}"
        )
        # No detail fetch fired — we bailed at the threshold gate.
        detail_fetches = [u for u in fetches if "/dp/B0WRONG" in u]
        assert detail_fetches == [], (
            f"detail fetch fired despite below-threshold score: {detail_fetches}"
        )

    async def test_tier3_volume_tiebreaker_picks_book_one(
        self, source, monkeypatch,
    ):
        """Same volume-disambiguation issue as F1 (Idle Village Hero
        scenario) applies to storefront listings: a no-volume query
        against numbered siblings ties on pure Jaccard. The
        volume-agreement tiebreaker must surface book 1 (no marker)."""
        src, fetches, responses = source

        async def fake_read(**_):
            return []
        async def fake_enqueue(**_):
            return True

        from app.discovery import metadata_cache_reader
        monkeypatch.setattr(
            metadata_cache_reader, "read_books_by_author", fake_read,
        )
        monkeypatch.setattr(
            metadata_cache_reader, "ensure_enqueued", fake_enqueue,
        )
        responses["SEARCH"] = (
            "<html><body><div data-component-type='s-search-results'>"
            "</div></body></html>"
        )

        async def fake_fetch(_id):
            return "STOREFRONT_HTML"
        monkeypatch.setattr(
            src, "_fetch_author_store_html", fake_fetch,
        )

        from app.discovery.sources import amazon_widget_parser
        page_data = _make_page_data(
            _make_product(
                asin="B0BOOK0004",
                title="Idle Village Hero 4: A Town-Building LitRPG",
            ),
            _make_product(
                asin="B0BOOK0001",
                title="Idle Village Hero: A Town-Building LitRPG",
            ),
            _make_product(
                asin="B0BOOK0002",
                title="Idle Village Hero 2: A Town-Building LitRPG",
            ),
        )
        monkeypatch.setattr(
            amazon_widget_parser, "parse_allbooks_html",
            lambda html: page_data,
        )

        responses[
            "https://www.amazon.com/dp/B0BOOK0001"
        ] = _kindle_detail_html("B0BOOK0001", title="Idle Village Hero")

        result = await src.search_book(
            "Idle Village Hero: A Slice-of-Life LitRPG",
            "Leon West",
            author_amazon_id="B0CMJC56GQ",
        )
        assert result is not None
        assert result.external_id == "B0BOOK0001"
