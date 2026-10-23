"""
Tests pinning the v2.10.4 Goodreads search policy.

Pre-v2.10.4 this file tested `_pick_author_from_book_search`, the
parser that backed the `/search?search_type=books` author-id pivot.
Goodreads' robots.txt explicitly disallows `/search` for `*`
user-agents, so we dropped that whole code path. The author-id
resolution responsibility moves to v2.11.0 (reverse-lookup from a
known book's `/book/show/{id}` JSON-LD, or Hardcover/sitemap-mirror
cross-reference).

These tests pin the new policy so a future refactor can't
accidentally reintroduce the disallowed endpoint.
"""
from __future__ import annotations

import httpx

from app.discovery.sources.goodreads import (
    GoodreadsSource,
    _is_soft_block,
)


class TestSearchAuthorDisabled:
    """`search_author` no longer hits `/search`. Returns None and
    leaves it to the dispatcher to skip this source for authors with
    no stored `goodreads_id`."""

    async def test_search_author_returns_none_without_http(self):
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            return httpx.Response(200, content=b"<html></html>")

        src = GoodreadsSource()
        src._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=5.0,
        )

        result = await src.search_author("A.K. DuBoff")

        assert result is None
        # Critical regression check: NO outbound HTTP from search_author.
        # If a refactor reintroduces a /search call, this assertion fails.
        assert calls == []
        await src.close()

    async def test_search_author_never_hits_goodreads_search(self):
        """Belt-and-suspenders regression: even if call list is empty,
        explicitly verify no URL contains `/search`."""
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            return httpx.Response(200, content=b"<html></html>")

        src = GoodreadsSource()
        src._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=5.0,
        )

        for variant in ("A K Duboff", "A.K. DuBoff", "Brandon Sanderson"):
            await src.search_author(variant)

        for url in calls:
            assert "goodreads.com/search" not in url, (
                f"discovery source leaked a /search call: {url}"
            )
        await src.close()


class TestCloudflareSoftBlockDetection:
    """Same helper as the enricher side — distinguishes Cloudflare's
    202 / empty-body gate from genuine "no data" responses."""

    def test_202_status_is_soft_block(self):
        resp = httpx.Response(202, content=b"")
        assert _is_soft_block(resp) is True

    def test_200_with_empty_body_is_soft_block(self):
        resp = httpx.Response(200, content=b"")
        assert _is_soft_block(resp) is True

    def test_200_with_real_body_is_not_soft_block(self):
        resp = httpx.Response(200, content=b"<html>real content</html>")
        assert _is_soft_block(resp) is False

    def test_404_is_not_soft_block(self):
        resp = httpx.Response(404, content=b"not found")
        assert _is_soft_block(resp) is False

    def test_none_response_not_soft_block(self):
        assert _is_soft_block(None) is False
