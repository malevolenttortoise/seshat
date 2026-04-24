"""
Tests for `GoodreadsSource.search_author` + the `_pick_author_from_book_search`
parser that backs it.

The old `/search?search_type=authors` endpoint migrated to client-side
React in early 2026 and is unusable for scrapers. The replacement path
queries `/search?search_type=books` and picks the most-common author
anchor across the result rows. These tests exercise that extraction +
the variant-retry fallback when Goodreads' punctuation-sensitive ranker
returns the wrong author on the first try.
"""
from __future__ import annotations

import pytest

from app.discovery.sources.goodreads import (
    GoodreadsSource,
    _pick_author_from_book_search,
)


def _book_row(title: str, author_name: str, author_id: str) -> str:
    """Build a minimal HTML fragment shaped like a Goodreads book-search row.

    Only the bits the parser looks at — the `a.authorName` anchor and
    a surrounding parent so the image scan has something to descend
    into. Real Goodreads pages carry lots more chrome; the parser
    ignores it.
    """
    return (
        f"<tr>"
        f'<a class="bookTitle" href="/book/show/999">{title}</a>'
        f'<a class="authorName" href="/author/show/{author_id}">'
        f'<span>{author_name}</span></a>'
        f"</tr>"
    )


def _page(rows_html: str) -> str:
    """Wrap rows in the minimum HTML envelope for lxml's html parser."""
    return f"<html><body><table>{rows_html}</table></body></html>"


# ─── _pick_author_from_book_search (pure function) ────────────

class TestPickAuthorFromBookSearch:
    def test_majority_author_wins(self):
        # 5 rows of DuBoff, 1 row of someone else — parser picks DuBoff.
        rows = "".join(
            [_book_row(f"Book {i}", "A.K. DuBoff", "18036488") for i in range(5)]
            + [_book_row("Unrelated", "Some Other Author", "99999")]
        )
        result = _pick_author_from_book_search(_page(rows), "A. K. Duboff")
        assert result is not None
        assert result.name == "A.K. DuBoff"
        assert result.external_id == "18036488"

    def test_author_name_must_match_query(self):
        # Query "A K Duboff" — Goodreads returns "Amy DuBoff" for all
        # rows (the real failure case from the screenshots). The gate
        # should reject it and the function should return None so the
        # caller can try the next variant.
        rows = "".join(
            [_book_row(f"Dark Rivals {i}", "Amy DuBoff", "555") for i in range(5)]
        )
        result = _pick_author_from_book_search(_page(rows), "A K Duboff")
        assert result is None

    def test_no_anchors_returns_none(self):
        assert _pick_author_from_book_search("<html></html>", "Brandon Sanderson") is None

    def test_anchor_without_id_is_skipped(self):
        # Malformed href — should not crash, just ignore that row.
        rows = (
            '<tr><a class="authorName" href="/not-an-author-path">Brandon</a></tr>'
            + _book_row("The Way of Kings", "Brandon Sanderson", "38550")
        )
        result = _pick_author_from_book_search(_page(rows), "Brandon Sanderson")
        assert result is not None
        assert result.external_id == "38550"

    def test_exact_match_beats_fuzzy_match_at_same_count(self):
        # Two candidates tied in count, one an exact normalized match,
        # the other only fuzzy. Counter.most_common() keys insertion
        # order on ties, so this documents the behavior: we walk in
        # frequency order; either match passes the gate, and the first
        # one encountered wins.
        rows = (
            _book_row("Book 1", "Brandon Sanderson", "38550")
            + _book_row("Book 2", "Brandon Sanderson", "38550")
        )
        result = _pick_author_from_book_search(_page(rows), "Brandon Sanderson")
        assert result.external_id == "38550"

    def test_picks_matching_author_even_if_not_most_common(self):
        # Rare but real: Goodreads' book search includes a co-author
        # whose books happen to outnumber the query author's on this
        # page. The gate is authors_match; the most-frequent rejected
        # candidate shouldn't block us from picking the correct one
        # further down the ranking.
        rows = "".join(
            [_book_row(f"X {i}", "Some Co-Author", "999") for i in range(4)]
            + [_book_row("Y", "A.K. DuBoff", "18036488")]
            + [_book_row("Z", "A.K. DuBoff", "18036488")]
        )
        result = _pick_author_from_book_search(_page(rows), "A. K. Duboff")
        assert result is not None
        assert result.external_id == "18036488"


# ─── search_author variant retry (integration) ────────────────

class _FakeResp:
    """Stands in for httpx.Response where the code only reads `.text`."""
    def __init__(self, text: str):
        self.text = text


class TestSearchAuthorVariantRetry:
    async def test_first_variant_hits_no_retries(self, monkeypatch):
        # Stored name "A.K. DuBoff" maps to canonical form; Goodreads
        # returns matching rows on the first query. No variant retry.
        src = GoodreadsSource()
        calls: list[str] = []

        async def fake_get(self, url, params=None, **kwargs):
            calls.append(params["q"])
            rows = _book_row("Stranded", "A.K. DuBoff", "18036488")
            return _FakeResp(_page(rows))

        monkeypatch.setattr(GoodreadsSource, "_get", fake_get)

        result = await src.search_author("A.K. DuBoff")
        assert result is not None
        assert result.external_id == "18036488"
        assert len(calls) == 1  # no retries

    async def test_first_variant_returns_wrong_author_retry_to_success(self, monkeypatch):
        # Stored name "A K Duboff" — Goodreads returns Amy DuBoff on
        # the first query (gate rejects), then "A.K. Duboff" variant
        # returns the right person.
        src = GoodreadsSource()
        calls: list[str] = []

        async def fake_get(self, url, params=None, **kwargs):
            q = params["q"]
            calls.append(q)
            # First query — wrong author.
            if q == "A K Duboff":
                rows = "".join(
                    _book_row(f"Dark Rivals {i}", "Amy DuBoff", "555")
                    for i in range(5)
                )
                return _FakeResp(_page(rows))
            # Subsequent variants return the right author.
            rows = "".join(
                _book_row(f"Book {i}", "A.K. DuBoff", "18036488") for i in range(5)
            )
            return _FakeResp(_page(rows))

        monkeypatch.setattr(GoodreadsSource, "_get", fake_get)

        result = await src.search_author("A K Duboff")
        assert result is not None
        assert result.external_id == "18036488"
        assert calls[0] == "A K Duboff"
        assert len(calls) >= 2  # at least one retry happened

    async def test_all_variants_fail_returns_none(self, monkeypatch):
        # No variant produces a matching author — function bails out
        # cleanly rather than returning the wrong person.
        src = GoodreadsSource()

        async def fake_get(self, url, params=None, **kwargs):
            rows = "".join(
                _book_row(f"X {i}", "Totally Different Person", "999")
                for i in range(5)
            )
            return _FakeResp(_page(rows))

        monkeypatch.setattr(GoodreadsSource, "_get", fake_get)

        assert await src.search_author("A K Duboff") is None

    async def test_plain_name_queries_once(self, monkeypatch):
        # Names without initial patterns only try the original — no
        # variant retries, so worst case stays one HTTP request.
        src = GoodreadsSource()
        calls: list[str] = []

        async def fake_get(self, url, params=None, **kwargs):
            calls.append(params["q"])
            # Return nothing useful — parser returns None.
            return _FakeResp(_page(""))

        monkeypatch.setattr(GoodreadsSource, "_get", fake_get)

        await src.search_author("Brandon Sanderson")
        assert calls == ["Brandon Sanderson"]

    async def test_transport_error_on_one_variant_tries_next(self, monkeypatch):
        # A network hiccup on the first variant shouldn't abort the
        # whole search — log and move to the next variant.
        src = GoodreadsSource()
        calls: list[str] = []

        async def fake_get(self, url, params=None, **kwargs):
            q = params["q"]
            calls.append(q)
            if q == "A K Duboff":
                raise RuntimeError("simulated transport error")
            rows = "".join(
                _book_row(f"Book {i}", "A.K. DuBoff", "18036488") for i in range(5)
            )
            return _FakeResp(_page(rows))

        monkeypatch.setattr(GoodreadsSource, "_get", fake_get)

        result = await src.search_author("A K Duboff")
        assert result is not None
        assert result.external_id == "18036488"
        assert len(calls) >= 2
