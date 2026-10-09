"""Amazon enrichment's live requests (2026-10 audit wave 4b, S5: A-C + G92):
curl_cffi only, a fresh session per book, the shared cooldown checked
before each request, blocks recorded, and at most two live requests per
book — across the enricher's title-variant retry too."""
from __future__ import annotations

import pytest

from app.discovery import amazon_author_id_resolver as resolver
from app.metadata import source_gate
from app.metadata.sources import amazon as amazon_mod
from app.metadata.sources.amazon import AmazonSource
from app.metadata.sources.base import enrichment_scope

SEARCH = "https://www.amazon.com/s"
_REAL_PAGE_PAD = resolver._AMAZON_SOFT_BLOCK_THIN_BODY_BYTES + 1000


def _search_html(*rows):
    links = "".join(f'<a href="/dp/{a}/ref=x"><span>{t}</span></a>' for a, t in rows)
    return ('<html><body><div data-component-type="s-search-results">'
            + links + "</div></body></html>")


def _kindle(title="The Tale"):
    pad = "x" * _REAL_PAGE_PAD   # real product pages are large (thin ones are blocks)
    return (f'<html><body><h1 id="productTitle">{title}</h1>'
            f'<div id="rpi-attribute-book_details-ebook_pages">'
            f'<span class="rpi-attribute-label"><span>Print length</span></span>'
            f'<span class="rpi-attribute-value"><span>320 pages</span></span></div>'
            f'<!-- {pad} --></body></html>')


def _audio(title="The Tale"):
    return (f'<html><body><h1 id="productTitle">{title}</h1>'
            f'<div id="rpi-attribute-audiobook_details-format">'
            f'<span class="rpi-attribute-label"><span>Format</span></span>'
            f'<span class="rpi-attribute-value"><span>Audible Audiobook</span></span></div>'
            f'<!-- {"x" * _REAL_PAGE_PAD} --></body></html>')


class Resp:
    def __init__(self, status, text, headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class FakeSession:
    instances: list["FakeSession"] = []

    def __init__(self, answers):
        self.answers, self.gets, self.closed = answers, [], False
        FakeSession.instances.append(self)

    async def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append(url)
        ans = self.answers.get(url) or self.answers.get(url.split("?")[0])
        return ans if isinstance(ans, Resp) else Resp(200, ans or "")

    async def close(self):
        self.closed = True


@pytest.fixture
def amazon(monkeypatch):
    answers: dict = {}
    FakeSession.instances = []
    from app.discovery.sources import amazon as disco_amazon
    monkeypatch.setattr(disco_amazon, "_create_impersonating_session",
                        lambda: FakeSession(answers))
    monkeypatch.setattr(resolver, "_persist_block_state", lambda **_: None)
    monkeypatch.setattr(resolver, "_blocked_until", 0.0)
    monkeypatch.setattr(resolver, "_block_reason", "")
    monkeypatch.setattr(resolver, "_block_count", 0)

    def cache(rows):
        from app.discovery import metadata_cache_reader as r

        async def read(**_):
            return rows

        async def enqueue(**_):
            return True
        monkeypatch.setattr(r, "read_books_by_author", read)
        monkeypatch.setattr(r, "ensure_enqueued", enqueue)

    return {"answers": answers, "cache": cache}


def _gets():
    return [u for s in FakeSession.instances for u in s.gets]


def test_requests_is_gone():
    assert not hasattr(amazon_mod, "requests")


async def test_without_an_author_id_a_search_and_one_product_page(amazon):
    amazon["answers"][SEARCH] = _search_html(("B000AUDIO0", "The Tale"), ("B000KINDLE", "The Tale: A Novel"))
    amazon["answers"]["https://www.amazon.com/dp/B000AUDIO0"] = _audio()
    amazon["answers"]["https://www.amazon.com/dp/B000KINDLE"] = _kindle()
    assert await AmazonSource().search_book("The Tale", "Author") is None
    assert _gets() == [SEARCH, "https://www.amazon.com/dp/B000AUDIO0"]
    assert not resolver.is_amazon_blocked()       # rejected as an audiobook


async def test_a_cache_hit_costs_one_product_page(amazon):
    amazon["cache"]([{"title": "The Tale", "book_asin": "B0CACHED01"}])
    amazon["answers"]["https://www.amazon.com/dp/B0CACHED01"] = _kindle()
    rec = await AmazonSource().search_book("The Tale", "Author", author_amazon_id="B0C0AUTHOR")
    assert rec is not None and rec.external_id == "B0CACHED01"
    assert _gets() == ["https://www.amazon.com/dp/B0CACHED01"]


async def test_a_cache_miss_costs_the_store_page_and_one_product_page(amazon, monkeypatch):
    amazon["cache"]([])
    from app.discovery.sources import amazon_widget_parser as wp

    class P:
        def __init__(self, asin, title):
            self.asin, self.title, self.binding_symbol = asin, title, wp.FILTER_TO_BINDING["kindle"]
            self.contributors, self.cover_url, self.series_title, self.series_position = ("Author",), None, None, None

    class Page:
        products = [P("B0STORE001", "The Tale")]

    monkeypatch.setattr(wp, "parse_allbooks_html", lambda html: Page())
    store = "https://www.amazon.com/stores/author/B0C0AUTHOR/allbooks"
    amazon["answers"][store] = "storefront" * 100
    amazon["answers"]["https://www.amazon.com/dp/B0STORE001"] = _kindle()
    async with enrichment_scope() as scope:
        rec = await AmazonSource().search_book("The Tale", "Author", author_amazon_id="B0C0AUTHOR")
        assert scope["amazon_live_left"] == 0      # the store page counted too
    assert rec is not None and rec.external_id == "B0STORE001"
    assert len(_gets()) == 2 and SEARCH not in _gets()


async def test_a_spent_product_page_means_no_store_page(amazon):
    amazon["cache"]([{"title": "The Tale", "book_asin": "B0CACHED01"}])
    amazon["answers"]["https://www.amazon.com/dp/B0CACHED01"] = _audio()   # rejected
    assert await AmazonSource().search_book("The Tale", "Author", author_amazon_id="B0C0AUTHOR") is None
    assert _gets() == ["https://www.amazon.com/dp/B0CACHED01"]
    assert not resolver.is_amazon_blocked()


async def test_the_cooldown_sends_nothing(amazon, monkeypatch):
    monkeypatch.setattr(resolver, "_blocked_until", 9e12)
    amazon["answers"][SEARCH] = _search_html(("B000KINDLE", "The Tale"))
    assert await AmazonSource().search_book("The Tale", "Author") is None
    assert _gets() == []


@pytest.mark.parametrize("resp", [
    Resp(200, "<html>Type the characters you see … /errors/validateCaptcha</html>"),
    Resp(503, "To discuss automated access to Amazon data please contact api-services-support@amazon.com"),
    Resp(202, ""),
    Resp(429, "", {"Retry-After": "120"}),
])
async def test_a_bot_page_is_recorded_as_a_block(amazon, resp):
    amazon["answers"][SEARCH] = resp
    assert await AmazonSource().search_book("The Tale", "Author") is None
    assert resolver.is_amazon_blocked()
    # The next book sends nothing while the cooldown runs.
    n = len(_gets())
    assert await AmazonSource().search_book("Other", "Author") is None
    assert len(_gets()) == n


async def test_a_thin_product_page_is_a_block(amazon):
    amazon["answers"][SEARCH] = _search_html(("B000KINDLE", "The Tale"))
    amazon["answers"]["https://www.amazon.com/dp/B000KINDLE"] = "<html>tiny</html>"
    assert await AmazonSource().search_book("The Tale", "Author") is None
    assert resolver.is_amazon_blocked()


async def test_a_blocked_request_counts_as_blocked(amazon):
    source_gate.reset()
    amazon["answers"][SEARCH] = Resp(202, "")
    with source_gate.caller("enrichment"):
        await AmazonSource().search_book("The Tale", "Author")
    counts = source_gate._pending.get((source_gate._today(), "amazon", "enrichment", "")) or {}
    assert counts.get("blocks") == 1 and counts.get("ok", 0) == 0


async def test_each_book_gets_a_fresh_session_closed_after(amazon):
    amazon["answers"][SEARCH] = _search_html()
    src = AmazonSource()
    await src.search_book("One", "Author")
    await src.search_book("Two", "Author")
    assert len(FakeSession.instances) == 2
    assert all(s.closed for s in FakeSession.instances)


async def test_the_budget_holds_across_the_enrichers_retry(amazon):
    """The enricher retries a no-match with a cleaned title; both calls
    are one book: one session, two live requests in all."""
    amazon["answers"][SEARCH] = _search_html(("B000AUDIO0", "The Tale"))
    amazon["answers"]["https://www.amazon.com/dp/B000AUDIO0"] = _audio()
    src = AmazonSource()
    async with enrichment_scope():
        assert await src.search_book("The Tale: Book 1", "Author") is None
        assert await src.search_book("The Tale", "Author") is None
    assert len(_gets()) == 2
    assert len(FakeSession.instances) == 1 and FakeSession.instances[0].closed
