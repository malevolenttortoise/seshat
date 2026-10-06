"""
Tests for the ethical goodreads_book_id resolver chain.

The resolver replaces Goodreads' robots-disallowed `/search` endpoint
with a tiered chain of robots-clean lookups:
  Tier 1 — /book/auto_complete?q={isbn_or_asin}
  Tier 2 — Hardcover book_mappings (deferred to v2.11.0; stubbed here)
  Tier 3 — Open Library identifiers.goodreads
  Tier 4 — /book/auto_complete?q={title}, post-filtered by author_goodreads_id (v2.13.2)
  Tier 5 — /author/list/{author_id} bibliography walk (v2.13.2; tests in test_goodreads_bibliography.py)
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


@pytest.fixture(autouse=True)
def goodreads_html(monkeypatch):
    """Tier 5 (the `/author/list/` walk) doesn't use the client a test
    passes in: it goes through the process-wide GoodreadsSession — real
    curl_cffi requests to goodreads.com, each behind a rate-limit sleep.
    Every test that reached Tier 5 did that; the no-/search regression
    test spent ~92s on it and its assertion never saw those URLs.

    This swaps the session for a fake that records each URL and answers
    through `box["handler"]` (default 404 — "no such page"), so no test
    here touches the network and a test can route Tier 5 through the
    same handler as its other tiers.
    """
    from app.metadata import goodreads_session

    box: dict = {"handler": lambda req: httpx.Response(404), "calls": []}

    class _FakeSession:
        async def get(self, url, **kwargs):
            box["calls"].append(url)
            return box["handler"](httpx.Request("GET", url))

    fake = _FakeSession()

    async def fake_get_session(rate_limit=None):
        return fake

    monkeypatch.setattr(goodreads_session, "get_session", fake_get_session)
    return box


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


class TestResolverTier2HardcoverBookMappings:
    """v2.13.0 — Hardcover GraphQL `book_mappings` resolver tier."""

    async def _setup_hardcover_key(self, monkeypatch, key: str = "test_key"):
        """Plant a hardcover_api_key in settings for the tier-2 fetch
        to find. Uses the tmp_path settings.json from the autouse
        fixture so it's isolated per test."""
        from app.config import load_settings, save_settings
        s = dict(load_settings())
        s["hardcover_api_key"] = key
        save_settings(s)

    async def test_isbn_hit_returns_goodreads_id(self, monkeypatch):
        from app.metadata.goodreads_id_resolver import (
            ResolveQuery, resolve_goodreads_id,
        )
        await self._setup_hardcover_key(monkeypatch)

        calls: list[str] = []

        def hardcover_handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            assert "hardcover.app" in str(req.url)
            return httpx.Response(200, json={
                "data": {
                    "editions": [{
                        "book": {
                            "book_mappings": [{"external_id": "8134945"}],
                        }
                    }]
                }
            })

        # Patch httpx.AsyncClient inside the resolver module so its
        # one-off Hardcover client gets our mock transport.
        import app.metadata.goodreads_id_resolver as gr_mod
        original_client = httpx.AsyncClient

        def fake_async_client(*args, **kwargs):
            kwargs.pop("timeout", None)
            kwargs.pop("headers", None)
            return original_client(
                transport=httpx.MockTransport(hardcover_handler),
                timeout=5.0,
            )
        monkeypatch.setattr(gr_mod.httpx, "AsyncClient", fake_async_client)

        # Tier 1 (auto_complete) misses, Tier 2 (Hardcover) hits.
        def goodreads_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])  # tier 1 miss

        # The injected `client` is for Goodreads + OL (NOT Hardcover —
        # tier 2 builds its own client which our patched AsyncClient
        # intercepts).
        transport = httpx.MockTransport(goodreads_handler)
        async with original_client(transport=transport, timeout=5.0) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="9780765376671"), client=client,
            )

        assert result.goodreads_book_id == "8134945"
        assert result.tier == "hardcover"
        # One Hardcover GraphQL call was made.
        assert len(calls) == 1

    async def test_no_api_key_skips_tier(self, monkeypatch):
        """Without a Hardcover API key the tier silently no-ops and
        Tier 3 picks up the slack."""
        from app.metadata.goodreads_id_resolver import (
            ResolveQuery, resolve_goodreads_id,
        )
        # No API key planted — autouse fixture's tmp_path settings.json
        # is fresh.

        hardcover_calls: list[str] = []
        original_client = httpx.AsyncClient

        def fake_async_client(*args, **kwargs):
            # If tier 2 attempts to build a Hardcover client, this
            # captures the call. We assert it's NEVER called below.
            hardcover_calls.append("attempted")
            return original_client(*args, **kwargs)
        import app.metadata.goodreads_id_resolver as gr_mod
        monkeypatch.setattr(gr_mod.httpx, "AsyncClient", fake_async_client)

        def goodreads_handler(req: httpx.Request) -> httpx.Response:
            if "auto_complete" in str(req.url):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={
                "ISBN:9780000000003": {
                    "identifiers": {"goodreads": ["111"]}
                }
            })

        transport = httpx.MockTransport(goodreads_handler)
        async with original_client(transport=transport, timeout=5.0) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="9780000000003"), client=client,
            )

        # Tier 3 (OpenLibrary) recovered the answer.
        assert result.goodreads_book_id == "111"
        assert result.tier == "openlibrary"
        # CRITICAL: tier 2 never attempted to build a Hardcover client.
        assert hardcover_calls == []

    async def test_book_mappings_empty_falls_through_to_tier3(self, monkeypatch):
        from app.metadata.goodreads_id_resolver import (
            ResolveQuery, resolve_goodreads_id,
        )
        await self._setup_hardcover_key(monkeypatch)

        def hardcover_handler(req: httpx.Request) -> httpx.Response:
            # Hardcover found the book but has no Goodreads mapping.
            return httpx.Response(200, json={
                "data": {"editions": [{"book": {"book_mappings": []}}]}
            })

        original_client = httpx.AsyncClient

        def fake_async_client(*args, **kwargs):
            kwargs.pop("timeout", None)
            kwargs.pop("headers", None)
            return original_client(
                transport=httpx.MockTransport(hardcover_handler),
                timeout=5.0,
            )
        import app.metadata.goodreads_id_resolver as gr_mod
        monkeypatch.setattr(gr_mod.httpx, "AsyncClient", fake_async_client)

        def fallthrough_handler(req: httpx.Request) -> httpx.Response:
            if "auto_complete" in str(req.url):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={
                "ISBN:9780000000004": {
                    "identifiers": {"goodreads": ["222"]}
                }
            })

        transport = httpx.MockTransport(fallthrough_handler)
        async with original_client(transport=transport, timeout=5.0) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="9780000000004"), client=client,
            )

        assert result.goodreads_book_id == "222"
        assert result.tier == "openlibrary"


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
    async def test_resolver_never_hits_goodreads_search(self, goodreads_html):
        # Regression-proof the policy: this whole module exists to
        # AVOID `/search` for `*` user-agents (robots-disallowed).
        # Even when every tier misses, no `/search` URL should ever
        # appear in the call list. T4 / T5 are also covered — passing
        # an author_goodreads_id activates them, and they must use
        # `auto_complete` / `/author/list/` rather than `/search`.
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            return httpx.Response(404)

        # Tier 5's HTML session answers through the same handler, so its
        # URLs are checked too.
        goodreads_html["handler"] = handler

        async with _make_client(handler) as client:
            await resolve_goodreads_id(
                ResolveQuery(
                    isbn="9781111111111",
                    asin="B0000000",
                    title="Some Book",
                    author="Some Author",
                    author_goodreads_id="38550",
                ),
                client=client,
            )

        assert any("/author/list/38550" in url for url in calls), (
            "Tier 5 never ran — the test no longer covers it"
        )
        for url in calls:
            assert "goodreads.com/search" not in url, (
                f"resolver leaked a /search call: {url}"
            )


class TestResolverTier4AutoCompleteTitle:
    """v2.13.2 — title-search tier with author_goodreads_id post-filter."""

    async def test_title_hits_with_author_filter(self):
        # The endpoint is the same `auto_complete` URL T1 uses; only
        # the query string differs (title vs identifier). Match by
        # author.id == author_goodreads_id.
        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "openlibrary" in url or "hardcover" in url:
                return httpx.Response(404)
            if "auto_complete" in url and "Mistborn" in url:
                return httpx.Response(200, json=[
                    {"bookId": "68428", "title": "Mistborn",
                     "author": {"id": 38550, "name": "Brandon Sanderson"},
                     "ratingsCount": 1000000},
                ])
            return httpx.Response(404)

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(
                    title="Mistborn",
                    author="Brandon Sanderson",
                    author_goodreads_id="38550",
                ),
                client=client,
            )

        assert result.goodreads_book_id == "68428"
        assert result.tier == "auto_complete_title"

    async def test_no_author_id_skips_t4(self):
        # T4 must no-op when author_goodreads_id is empty — we don't
        # accept unconstrained title matches.
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            return httpx.Response(404)

        async with _make_client(handler) as client:
            await resolve_goodreads_id(
                ResolveQuery(title="Mistborn", author="Brandon Sanderson"),
                client=client,
            )

        # No auto_complete title call without an author_id anchor.
        for url in calls:
            assert "auto_complete" not in url, (
                f"T4 leaked a call without author_goodreads_id: {url}"
            )

    async def test_wrong_author_results_rejected(self):
        # All 5 results have a different author.id — T4 must filter
        # them out and return None (chain falls through to T5).
        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "openlibrary" in url or "hardcover" in url:
                return httpx.Response(404)
            if "auto_complete" in url:
                # Parody by Sarandon Branderson (different author.id)
                return httpx.Response(200, json=[
                    {"bookId": "34821107", "title": "The Annoyomancer",
                     "author": {"id": 16688353, "name": "Sarandon Branderson"},
                     "ratingsCount": 5},
                ])
            # T5 also misses for this test's purposes.
            return httpx.Response(404)

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(
                    title="Mistborn",
                    author="Brandon Sanderson",
                    author_goodreads_id="38550",
                ),
                client=client,
            )

        assert result.goodreads_book_id is None

    async def test_multiple_matches_picks_highest_rated(self):
        # When several T4 results match the author, prefer the most-
        # rated one (canonical edition).
        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "openlibrary" in url or "hardcover" in url:
                return httpx.Response(404)
            if "auto_complete" in url:
                return httpx.Response(200, json=[
                    {"bookId": "boxed", "author": {"id": 38550},
                     "ratingsCount": 62000},
                    {"bookId": "canonical", "author": {"id": 38550},
                     "ratingsCount": 1027854},
                    {"bookId": "wellof", "author": {"id": 38550},
                     "ratingsCount": 691000},
                ])
            return httpx.Response(404)

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(
                    title="Mistborn",
                    author_goodreads_id="38550",
                ),
                client=client,
            )

        assert result.goodreads_book_id == "canonical"
        assert result.tier == "auto_complete_title"

    async def test_t4_403_flips_soft_block(self):
        # CloudFront 403 on the auto_complete endpoint should mark
        # the session soft_blocked (v2.13.2 detector expansion).
        from app.metadata import goodreads_session

        def handler(req: httpx.Request) -> httpx.Response:
            if "openlibrary" in str(req.url) or "hardcover" in str(req.url):
                return httpx.Response(404)
            return httpx.Response(403)

        async with _make_client(handler) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(
                    title="Mistborn",
                    author_goodreads_id="38550",
                ),
                client=client,
            )

        assert result.goodreads_book_id is None
        assert result.soft_blocked is True
        assert goodreads_session.is_soft_blocked() is True
