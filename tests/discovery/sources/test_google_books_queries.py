"""Google Books queries (2026-10 audit wave 4 S6, G55). Since ~2026-09-26
a query made only of field operators (`inauthor:X`) returns 200 with no
results; discovery's unquoted `inauthor:First Last` half-worked by
accident and enrichment's `intitle:+inauthor:` (with no key) found
nothing."""
import httpx
import pytest

from app.discovery.sources.google_books import GoogleBooksSource


def _vol(i, author="Travis Dean", title=None):
    return {"id": f"v{i}", "volumeInfo": {
        "title": title or f"Book {i}", "authors": [author], "language": "en",
    }}


def _source(pages, *, total):
    """Pages of items answered in order; returns (source, requests)."""
    requests: list[httpx.Request] = []

    def handler(req):
        requests.append(req)
        start = int(req.url.params.get("startIndex", "0"))
        page = pages.get(start, [])
        return httpx.Response(200, json={"totalItems": total, "items": page})

    src = GoogleBooksSource(rate_limit=0, api_key="k-1")
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return src, requests


def _titles(result):
    return {b.title for b in result.books} | {b.title for s in result.series for b in s.books}


async def test_one_free_text_search_per_author():
    src, reqs = _source({0: [_vol(1), _vol(2)]}, total=2)
    found = await src.search_author("Travis Dean")
    assert len(reqs) == 1                        # no separate lookup first
    q = reqs[0].url.params
    assert q["q"] == '"Travis Dean"'             # quoted name, free text
    assert "inauthor" not in q["q"]
    assert q["key"] == "k-1" and q["startIndex"] == "0"
    assert _titles(found) == {"Book 1", "Book 2"}   # returned inline


async def test_pages_advance_by_the_items_returned():
    src, reqs = _source({
        0: [_vol(i) for i in range(20)],
        20: [_vol(i) for i in range(20, 25)],
    }, total=25)
    found = await src.search_author("Travis Dean")
    assert [r.url.params["startIndex"] for r in reqs] == ["0", "20"]
    assert len(_titles(found)) == 25


async def test_paging_stops_at_a_page_with_no_match():
    src, reqs = _source({
        0: [_vol(i) for i in range(20)],
        20: [_vol(i, author="Somebody Else") for i in range(20, 40)],
        40: [_vol(i) for i in range(40, 60)],
    }, total=300)
    found = await src.search_author("Travis Dean")
    assert [r.url.params["startIndex"] for r in reqs] == ["0", "20"]
    assert len(_titles(found)) == 20             # the other author's dropped


async def test_paging_is_capped():
    from app.discovery.sources import google_books as gb

    src, reqs = _source({i * 20: [_vol(i * 20 + j) for j in range(20)] for i in range(10)},
                        total=1000)
    await src.search_author("Travis Dean")
    assert len(reqs) == gb._MAX_PAGES


async def test_no_results_is_no_author():
    src, reqs = _source({}, total=0)
    assert await src.search_author("Nobody Here") is None
    assert len(reqs) == 1


async def test_a_failed_later_page_keeps_the_first():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if req.url.params.get("startIndex") == "20":
            return httpx.Response(400, json={})
        return httpx.Response(200, json={"totalItems": 50, "items": [_vol(i) for i in range(20)]})

    src = GoogleBooksSource(rate_limit=0)
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    found = await src.search_author("Travis Dean")
    assert len(_titles(found)) == 20


async def test_enrichment_searches_free_text_with_the_key(monkeypatch):
    from app.metadata.sources.google_books import GoogleBooksSource as MetaGB

    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"items": [_vol(1, title="Banished")]})

    src = MetaGB(api_key="k-2")
    src.set_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    rec = await src.search_book("Banished", "Travis Dean")
    q = seen[0].url.params
    assert q["q"] == "Banished Travis Dean"
    assert "intitle" not in q["q"] and "inauthor" not in q["q"]
    assert q["key"] == "k-2"
    assert rec is not None and rec.title == "Banished"


async def test_enrichment_does_not_retry_a_429(monkeypatch):
    from app.metadata.sources import google_books as meta_gb

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429, json={})

    async def no_sleep(_):
        return None
    monkeypatch.setattr(meta_gb.asyncio, "sleep", no_sleep)
    src = meta_gb.GoogleBooksSource(api_key="k")
    src.set_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await src.search_book("Banished", "Travis Dean") is None
    assert calls["n"] == 1
