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
