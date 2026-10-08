"""Every caller's requests to a metadata source wait their turn in the
source gate and are counted there (2026-10 audit wave 4 S1: G61, G71, G78)."""
import asyncio
import json

import httpx
import pytest

from app.metadata import source_gate
from tests.metadata.test_source_gate import FakeClock, _pending


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(source_gate, "_clock", c.mono)
    monkeypatch.setattr(source_gate, "_wall", c.walltime)
    monkeypatch.setattr(source_gate, "_sleep", c.sleep)
    monkeypatch.setattr(source_gate, "_jitter", lambda lo, hi: 0.0)
    return c


@pytest.fixture
def rates(monkeypatch):
    r = {"goodreads": 30.0, "hardcover": 3.0, "kobo": 3.0, "openlibrary": 3.0,
         "google_books": 3.0, "ibdb": 3.0}
    monkeypatch.setattr(
        source_gate, "load_settings",
        lambda: {"metadata_sources": {k: {"rate_limit": v} for k, v in r.items()}},
    )
    return r


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ─── Base `_get`s ────────────────────────────────────────────


async def test_discovery_sources_get_in_their_turn(clock, rates):
    from app.discovery.sources.openlibrary import OpenLibrarySource

    src = OpenLibrarySource(rate_limit=0)
    src._client = _client(lambda req: httpx.Response(200, json={}))
    with source_gate.caller("scan"):
        await src._get("https://openlibrary.org/search/authors.json")
        await src._get("https://openlibrary.org/search/authors.json")
    assert clock.slept == [3.0]       # the setting, not the constructor's 0
    assert _pending("openlibrary", "scan")["ok"] == 2


async def test_enrichment_sources_get_in_their_turn(clock, rates):
    from app.metadata.sources.ibdb import IbdbSource

    src = IbdbSource(rate_limit=0)
    src.set_client(_client(lambda req: httpx.Response(429)))
    with source_gate.caller("enrichment"):
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                await src._get("https://ibdb.dev/api/search", retries=0)
    row = _pending("ibdb", "enrichment")
    assert (row["requests"], row["blocks"]) == (2, 2)
    assert clock.slept == [3.0]


# ─── Hardcover (G61) ─────────────────────────────────────────


async def test_hardcovers_discovery_posts_are_paced(clock, rates):
    """They skipped the rate sleep entirely until the 2026-10 audit."""
    from app.discovery.sources.hardcover import HardcoverSource

    starts: list[float] = []

    def handler(req):
        starts.append(clock.now)
        return httpx.Response(200, json={"data": {"ok": 1}})

    src = HardcoverSource(api_key="k")
    client = _client(handler)
    src._get_client = lambda: client
    with source_gate.caller("scan"):
        await src._query("query { a }")
        await src._query("query { b }")
    assert starts == [1000.0, 1003.0]
    assert _pending("hardcover", "scan")["ok"] == 2


async def test_hardcovers_enrichment_posts_are_paced(clock, rates):
    from app.metadata.sources.hardcover import HardcoverSource

    src = HardcoverSource(api_key="k")
    src.set_client(_client(lambda req: httpx.Response(200, json={"data": {}})))
    with source_gate.caller("enrichment"):
        await src._query("query { a }", {})
        await src._query("query { b }", {})
    assert clock.slept == [3.0]


# ─── Goodreads ───────────────────────────────────────────────


class _FakeCurl:
    def __init__(self, status=200, body=b"<html>ok</html>"):
        self.status, self.body, self.urls = status, body, []

    async def get(self, url, **_):
        self.urls.append(url)

        class R:
            status_code = self.status
            content = self.body
            text = self.body.decode()
        return R()


async def test_the_goodreads_session_waits_goodreads_turn_and_counts_by_kind(clock, rates):
    """G50's guarantee, for every caller: the configured rate, read per
    request, whoever came first."""
    from app.metadata.goodreads_session import GoodreadsSession

    s = GoodreadsSession()
    s._curl = _FakeCurl()
    with source_gate.caller("worker"):
        await s.get("https://www.goodreads.com/author/list/1?page=1")
        await s.get("https://www.goodreads.com/author/list/1?page=2")
    assert clock.slept == [30.0]
    assert _pending("goodreads", "worker", "list_page")["ok"] == 2


async def test_a_goodreads_soft_block_counts_as_blocked(clock, rates, monkeypatch):
    from app.metadata import goodreads_session
    from app.metadata.goodreads_session import GoodreadsSession

    monkeypatch.setattr(goodreads_session, "mark_soft_blocked", lambda **_: None)
    s = GoodreadsSession()
    s._curl = _FakeCurl(status=202, body=b"")
    with source_gate.caller("scan"):
        await s.get("https://www.goodreads.com/book/show/1")
    row = _pending("goodreads", "scan", "book_page")
    assert (row["requests"], row["blocks"]) == (1, 1)


async def test_the_resolvers_autocomplete_waits_goodreads_turn(clock, rates, monkeypatch):
    """Tiers 1 / 4 had their own unpaced client."""
    from app.metadata import goodreads_id_resolver as resolver

    client = _client(lambda req: httpx.Response(200, json=[{"bookId": 42}]))
    with source_gate.caller("enrichment"):
        await resolver._tier1_auto_complete(client, "9780000000001")
        await resolver._tier1_auto_complete(client, "9780000000002")
    assert clock.slept == [30.0]
    assert _pending("goodreads", "enrichment", "autocomplete")["ok"] == 2


async def test_a_resolver_run_with_no_caller_counts_as_resolver(clock, rates, monkeypatch):
    from app.metadata import goodreads_id_resolver as resolver

    client = _client(lambda req: httpx.Response(200, json=[{"bookId": 42}]))
    out = await resolver.resolve_goodreads_id(
        resolver.ResolveQuery(isbn="9780000000001"), client=client, use_cache=False,
    )
    assert out.goodreads_book_id == "42"
    assert _pending("goodreads", "resolver", "autocomplete")["requests"] == 1


# ─── Enrichment (G71, G78) ───────────────────────────────────


def test_enrichment_sends_google_books_its_key():
    from app.main import _build_metadata_enricher

    enricher = _build_metadata_enricher({}, {"google_books_api_key": "k-123"})
    gb = [s for s in enricher._sources if s.name == "google_books"]
    assert gb and gb[0]._api_key == "k-123"


class _GatedSource:
    """An enrichment source whose search is one request in Kobo's turn."""

    def __init__(self, name="kobo", cheap=False):
        self.name = name
        self.calls = 0
        self.cheap = cheap

    def is_cheap_for(self, **_):
        return self.cheap     # a cheap source still runs after a good match

    async def search_book(self, title, author, **_):
        from app.metadata.record import MetaRecord
        self.calls += 1

        async def send():
            return httpx.Response(200)
        await source_gate.request("kobo", send)
        return MetaRecord(title=title, authors=[author], confidence=0.5)

    async def close(self):
        pass


async def test_waiting_for_a_turn_doesnt_time_enrichment_out(rates, monkeypatch):
    """G78: a source whose turn is further off than its timeout still runs,
    and the wait doesn't use up the per-book budget for the next source."""
    from app.metadata.enricher import EnrichmentConfig, MetadataEnricher

    monkeypatch.setattr(source_gate, "_sleep", asyncio.sleep)
    rates["kobo"] = 0.4
    async def send():
        return httpx.Response(200)
    with source_gate.caller("worker"):
        await source_gate.request("kobo", send)         # the worker just went

    first, second = _GatedSource(), _GatedSource(cheap=True)
    cfg = EnrichmentConfig(
        enabled=True, accept_confidence=0.9,
        per_source_timeout=0.15, per_book_budget=0.3,
    )
    enricher = MetadataEnricher(cfg, sources=[first, second])
    result = await enricher.enrich(title="Banished", author="Travis Dean")
    assert result is not None
    assert (first.calls, second.calls) == (1, 1)
    assert _pending("kobo", "enrichment")["requests"] == 2


# ─── Kobo (G72) ──────────────────────────────────────────────


def test_kobos_scan_cap_is_600s():
    from app.discovery import lookup

    assert {s.name: s.timeout_sec for s in lookup.SOURCES}["kobo"] == 600.0


# ─── The panel's endpoint ────────────────────────────────────


async def test_the_traffic_endpoint(clock, rates):
    from app.routers.metadata_sources import get_traffic

    async def send():
        return httpx.Response(200)
    with source_gate.caller("scan"):
        await source_gate.request("kobo", send)
        source_gate.count_merged("kobo", created=2)
    out = await get_traffic(days=8)
    kobo = out["sources"]["kobo"]
    assert (kobo["today"]["requests"], kobo["today"]["created"]) == (1, 2)
    assert len(kobo["daily"]) == 8
    json.dumps(out)
