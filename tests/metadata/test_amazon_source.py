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
