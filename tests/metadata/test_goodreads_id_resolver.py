"""
Tests for the ethical goodreads_book_id resolver chain.

The resolver replaces Goodreads' robots-disallowed `/search` endpoint
with a tiered chain of robots-clean lookups:
  Tier 1 — /book/auto_complete?q={isbn_or_asin}
  Tier 2 — Hardcover book_mappings (deferred to v2.11.0; stubbed here)
  Tier 3 — Open Library identifiers.goodreads
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from app.metadata.goodreads_id_resolver import (
    ResolveQuery,
    resolve_goodreads_id,
)


@pytest.fixture(autouse=True)
def _isolated_id_cache(tmp_path, monkeypatch):
    """v2.13.0: the resolver now caches outcomes in
    `app.metadata.id_cache` AND writes the goodreads_session_state
    runtime flag via app.config.save_settings on soft-block. Both
    side-effects must land in tmp_path, not the dev DATA_DIR.

    SETTINGS_PATH is computed at import time from DATA_DIR, so
    patching DATA_DIR alone leaks writes to the real settings.json.
    We also patch SETTINGS_PATH so any save_settings call writes here.
    """
    from app import config
    from app.metadata import id_cache

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(
        id_cache, "_db_path", lambda: tmp_path / "id_cache.db",
    )
    yield


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, timeout=5.0)


class TestResolverTier1AutoComplete:
    async def test_isbn_hit_returns_book_id(self):
        def handler(req: httpx.Request) -> httpx.Response:
            assert "auto_complete" in str(req.url)
            assert "q=9780765376671" in str(req.url)
            return httpx.Response(200, json=[
                {"bookId": "8134945", "title": "Mistborn"},
            ])

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="9780765376671", title="Mistborn", author="Brandon Sanderson"),
                client=client,
            )

        assert result.goodreads_book_id == "8134945"
        assert result.tier == "auto_complete"
        assert result.soft_blocked is False

    async def test_asin_hit_returns_book_id(self):
        def handler(req: httpx.Request) -> httpx.Response:
            assert "q=B07HRHN73T" in str(req.url)
            return httpx.Response(200, json=[{"bookId": "42"}])

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(asin="B07HRHN73T"), client=client,
            )

        assert result.goodreads_book_id == "42"
        assert result.tier == "auto_complete"

    async def test_isbn_then_asin_short_circuits_on_isbn_hit(self):
        # Both identifiers present — ISBN should be tried first and win.
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            return httpx.Response(200, json=[{"bookId": "111"}])

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="9780000000000", asin="B0000000"),
                client=client,
            )

        assert result.goodreads_book_id == "111"
        # Should have hit ISBN, NOT ASIN (one call total).
        assert len(calls) == 1
        assert "9780000000000" in calls[0]

    async def test_no_isbn_no_asin_falls_through(self):
        # Empty query identifiers — Tier 1 is skipped entirely.
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            return httpx.Response(200, json=[{"bookId": "should-not-be-called"}])

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(title="Mistborn", author="Brandon Sanderson"),
                client=client,
            )

        assert result.goodreads_book_id is None
        assert result.tier is None
        # No auto_complete call, no openlibrary call (no ISBN).
        assert calls == []

    async def test_202_response_flags_soft_block_and_falls_through(self):
        # Cloudflare gate — 202 with empty body. Caller needs to know
        # this is "Goodreads is blocking us" not "Goodreads doesn't
        # know this book."
        def handler(req: httpx.Request) -> httpx.Response:
            if "auto_complete" in str(req.url):
                return httpx.Response(202, content=b"")
            # Tier 3 OpenLibrary call also fires after Tier 1 soft-blocks.
            return httpx.Response(200, json={
                "ISBN:9780000000000": {
                    "identifiers": {"goodreads": ["999"]}
                }
            })

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="9780000000000"), client=client,
            )

        # Tier 3 picked up the slack — but the soft_blocked flag still
        # surfaces that Goodreads is gated.
        assert result.goodreads_book_id == "999"
        assert result.tier == "openlibrary"
        assert result.soft_blocked is True

    async def test_200_with_empty_body_also_treated_as_soft_block(self):
        # Cloudflare sometimes returns 200 with an empty body too.
        def handler(req: httpx.Request) -> httpx.Response:
            if "auto_complete" in str(req.url):
                return httpx.Response(200, content=b"")
            return httpx.Response(404)

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="9781111111111"), client=client,
            )

        assert result.goodreads_book_id is None
        assert result.soft_blocked is True

    async def test_malformed_json_returns_none_gracefully(self):
        def handler(req: httpx.Request) -> httpx.Response:
            if "auto_complete" in str(req.url):
                return httpx.Response(200, content=b"<html>not json</html>")
            return httpx.Response(404)

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="9781111111111"), client=client,
            )

        assert result.goodreads_book_id is None

    async def test_empty_json_array_falls_through(self):
        def handler(req: httpx.Request) -> httpx.Response:
            if "auto_complete" in str(req.url):
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="9781111111111"), client=client,
            )

        assert result.goodreads_book_id is None
        assert result.soft_blocked is False


class TestResolverTier3OpenLibrary:
    async def test_isbn_with_goodreads_cross_ref_returns_id(self):
        def handler(req: httpx.Request) -> httpx.Response:
            if "openlibrary" in str(req.url):
                assert "bibkeys=ISBN%3A0140328726" in str(req.url) or \
                       "bibkeys=ISBN:0140328726" in str(req.url)
                return httpx.Response(200, json={
                    "ISBN:0140328726": {
                        "identifiers": {"goodreads": ["24178"]}
                    }
                })
            # Tier 1 misses (no goodreads bookId in auto_complete).
            return httpx.Response(200, json=[])

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="0140328726"), client=client,
            )

        assert result.goodreads_book_id == "24178"
        assert result.tier == "openlibrary"

    async def test_no_identifiers_returns_none(self):
        # Recent indie self-pub: OpenLibrary entry exists but has no
        # goodreads cross-reference. Coverage gap that future tiers
        # (Hardcover book_mappings) will close in v2.11.0.
        def handler(req: httpx.Request) -> httpx.Response:
            if "openlibrary" in str(req.url):
                return httpx.Response(200, json={
                    "ISBN:9999999999": {
                        "identifiers": {"isbn_13": ["9999999999"]}
                    }
                })
            return httpx.Response(200, json=[])

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="9999999999"), client=client,
            )

        assert result.goodreads_book_id is None

    async def test_isbn_not_in_openlibrary_returns_none(self):
        # OL returns an empty dict for unknown ISBNs.
        def handler(req: httpx.Request) -> httpx.Response:
            if "openlibrary" in str(req.url):
                return httpx.Response(200, json={})
            return httpx.Response(200, json=[])

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="0000000000"), client=client,
            )

        assert result.goodreads_book_id is None
        assert result.tier is None


class TestResolverNoSearchRegression:
    async def test_resolver_never_hits_goodreads_search(self):
        # Regression-proof the policy: this whole module exists to
        # AVOID `/search` for `*` user-agents (robots-disallowed).
        # Even when every tier misses, no `/search` URL should ever
        # appear in the call list.
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            return httpx.Response(404)

        async with _make_client(handler) as client:
            await resolve_goodreads_id(
                ResolveQuery(
                    isbn="9781111111111",
                    asin="B0000000",
                    title="Some Book",
                    author="Some Author",
                ),
                client=client,
            )

        for url in calls:
            assert "goodreads.com/search" not in url, (
                f"resolver leaked a /search call: {url}"
            )
