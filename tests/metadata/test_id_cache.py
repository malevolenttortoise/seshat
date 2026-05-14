"""
Tests for `app.metadata.id_cache` — the SQLite-backed cross-reference
cache for ID-resolver outcomes.

Scope:
  - book_id scope round-trip (hit, miss, cached-miss)
  - author_bib scope round-trip
  - TTL expiry honored (cache reports miss when expires_at < now)
  - put_book_id with book_id=None caches a negative (short TTL)
  - normalize_book_id_key uses identifier-first ordering
  - clear_all and prune_expired
  - per-test DB isolation (sanity check the autouse fixture pattern)
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _isolated_cache_db(tmp_path, monkeypatch):
    """Point id_cache at a tmp_path file so each test starts fresh."""
    from app import config
    from app.metadata import id_cache

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        id_cache, "_db_path", lambda: tmp_path / "id_cache.db",
    )
    yield


class TestBookIdScope:
    def test_miss_returns_none(self):
        from app.metadata.id_cache import get_book_id
        assert get_book_id(isbn="9780000000000") is None

    def test_hit_round_trip(self):
        from app.metadata.id_cache import get_book_id, put_book_id
        put_book_id(
            isbn="9780765376671",
            book_id="8134945", tier="auto_complete",
        )
        result = get_book_id(isbn="9780765376671")
        assert result == ("8134945", "auto_complete")

    def test_cached_miss_returns_tuple_with_none(self):
        """A previously-tried resolve that returned None gets cached
        with a shorter TTL. The lookup must return (None, "miss") so
        callers can distinguish "never tried" from "tried, got nothing."
        """
        from app.metadata.id_cache import get_book_id, put_book_id
        put_book_id(isbn="9999999999", book_id=None, tier=None)
        result = get_book_id(isbn="9999999999")
        assert result == (None, "miss")

    def test_asin_keyed_independent_of_isbn(self):
        from app.metadata.id_cache import get_book_id, put_book_id
        put_book_id(asin="B07HRHN73T", book_id="42", tier="auto_complete")
        # ISBN lookup misses because keying is identifier-first.
        assert get_book_id(isbn="9780765376671") is None
        assert get_book_id(asin="B07HRHN73T") == ("42", "auto_complete")

    def test_title_author_fallback_key(self):
        from app.metadata.id_cache import get_book_id, put_book_id
        put_book_id(
            title="Mistborn", author="Brandon Sanderson",
            book_id="68428", tier="auto_complete",
        )
        # Different case + whitespace should normalize to the same key.
        assert get_book_id(
            title="  MISTBORN  ", author="  brandon sanderson",
        ) == ("68428", "auto_complete")

    def test_empty_query_does_nothing(self):
        from app.metadata.id_cache import get_book_id, put_book_id
        # No identifier at all — nothing to cache.
        put_book_id(book_id="42", tier="auto_complete")
        # ...and nothing to retrieve.
        assert get_book_id() is None

    def test_isbn_hyphens_normalized(self):
        from app.metadata.id_cache import get_book_id, put_book_id
        put_book_id(isbn="978-0-7653-7667-1", book_id="X", tier="t")
        assert get_book_id(isbn="9780765376671") == ("X", "t")


class TestAuthorBibScope:
    def test_miss_returns_none(self):
        from app.metadata.id_cache import get_author_bib
        assert get_author_bib("123") is None

    def test_hit_round_trip(self):
        from app.metadata.id_cache import get_author_bib, put_author_bib
        books = [
            {"book_id": "1", "title": "A"},
            {"book_id": "2", "title": "B"},
        ]
        put_author_bib("123", books)
        assert get_author_bib("123") == books

    def test_empty_author_id_does_nothing(self):
        from app.metadata.id_cache import get_author_bib, put_author_bib
        put_author_bib("", [{"book_id": "1"}])
        assert get_author_bib("") is None

    def test_cached_miss_returns_none(self):
        """A negative-cached author bib lookup returns None — same as a
        real miss. The cached miss is invisible to callers; its only
        effect is the cache hit (no HTTP re-probe) until TTL expires."""
        from app.metadata.id_cache import get_author_bib, put_author_bib
        put_author_bib("999", None)
        assert get_author_bib("999") is None  # cached, but caller can't tell


class TestTTLExpiry:
    def test_expired_book_id_treated_as_miss(self):
        """Manually backdate a cache row's expires_at to simulate TTL
        expiry. Lookup should return None (miss), not the stale value."""
        from app.metadata import id_cache
        id_cache.put_book_id(isbn="A", book_id="1", tier="t")
        # Backdate expires_at via direct SQL.
        with id_cache._conn() as c:
            c.execute(
                "UPDATE id_cache SET expires_at = ? WHERE scope = ? AND key = ?",
                (time.time() - 60, "book_id", "isbn:a"),
            )
        assert id_cache.get_book_id(isbn="A") is None

    def test_prune_expired_drops_only_expired_rows(self):
        from app.metadata import id_cache
        id_cache.put_book_id(isbn="LIVE", book_id="L", tier="t")
        id_cache.put_book_id(isbn="DEAD", book_id="D", tier="t")
        with id_cache._conn() as c:
            c.execute(
                "UPDATE id_cache SET expires_at = ? WHERE scope = ? AND key = ?",
                (time.time() - 60, "book_id", "isbn:dead"),
            )
        removed = id_cache.prune_expired()
        assert removed == 1
        # Live row still present.
        assert id_cache.get_book_id(isbn="LIVE") == ("L", "t")
        # Dead row gone.
        assert id_cache.get_book_id(isbn="DEAD") is None


class TestClearAll:
    def test_clear_all_drops_every_scope(self):
        from app.metadata import id_cache
        id_cache.put_book_id(isbn="X", book_id="1", tier="t")
        id_cache.put_author_bib("123", [{"book_id": "1"}])
        id_cache.clear_all()
        assert id_cache.get_book_id(isbn="X") is None
        assert id_cache.get_author_bib("123") is None


class TestResolverCacheIntegration:
    """Resolver chain end-to-end with cache:
      - First call hits HTTP and writes to cache
      - Second call with same query returns cached result without HTTP
    """

    async def test_second_call_short_circuits_via_cache(self):
        import httpx
        from app.metadata.goodreads_id_resolver import (
            ResolveQuery, resolve_goodreads_id,
        )

        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            return httpx.Response(200, json=[{"bookId": "8134945"}])

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
            first = await resolve_goodreads_id(
                ResolveQuery(isbn="9780765376671"), client=client,
            )
            second = await resolve_goodreads_id(
                ResolveQuery(isbn="9780765376671"), client=client,
            )

        assert first.goodreads_book_id == "8134945"
        assert second.goodreads_book_id == "8134945"
        # CRITICAL: only ONE HTTP call — the second resolve hit the cache.
        assert len(calls) == 1

    async def test_use_cache_false_bypasses_cache(self):
        """The canary will pass `use_cache=False` so it always probes
        the live HTTP path even when the cache has a stale answer."""
        import httpx
        from app.metadata.goodreads_id_resolver import (
            ResolveQuery, resolve_goodreads_id,
        )

        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            return httpx.Response(200, json=[{"bookId": "X"}])

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
            await resolve_goodreads_id(
                ResolveQuery(isbn="9780000000000"), client=client,
            )
            await resolve_goodreads_id(
                ResolveQuery(isbn="9780000000000"), client=client,
                use_cache=False,
            )

        # Two HTTP calls — the second resolved past the cache.
        assert len(calls) == 2

    async def test_miss_is_cached_so_repeat_misses_skip_http(self):
        """A dead-end ISBN that the resolver couldn't find shouldn't
        trigger another auto_complete call on the next scan within
        the miss-TTL window."""
        import httpx
        from app.metadata.goodreads_id_resolver import (
            ResolveQuery, resolve_goodreads_id,
        )

        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            # Tier 1 miss (empty array) + Tier 3 OL miss
            if "auto_complete" in str(req.url):
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
            first = await resolve_goodreads_id(
                ResolveQuery(isbn="9780000000001"), client=client,
            )
            calls_after_first = len(calls)
            second = await resolve_goodreads_id(
                ResolveQuery(isbn="9780000000001"), client=client,
            )

        assert first.goodreads_book_id is None
        assert second.goodreads_book_id is None
        # Second call must NOT add any HTTP — the miss is cached.
        assert len(calls) == calls_after_first

    async def test_soft_block_outcome_not_cached(self):
        """Don't cache a soft-block result. Cloudflare gates are
        transient — caching them would lock us out of Goodreads for
        the miss-TTL even after cookies refresh."""
        import httpx
        from app.metadata.goodreads_id_resolver import (
            ResolveQuery, resolve_goodreads_id,
        )

        def handler(req: httpx.Request) -> httpx.Response:
            if "auto_complete" in str(req.url):
                return httpx.Response(202, content=b"")  # soft-block
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
            result = await resolve_goodreads_id(
                ResolveQuery(isbn="9780000000002"), client=client,
            )

        assert result.soft_blocked is True
        # Cache must NOT have been written — verify by direct lookup.
        from app.metadata.id_cache import get_book_id
        assert get_book_id(isbn="9780000000002") is None
