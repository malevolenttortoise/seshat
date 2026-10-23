"""
Goodreads enricher source tests.

Drives `_merge_detail_page` directly with inline HTML fixtures so we
don't need real Goodreads. Also pins the v2.10.4 policy: the source
must no longer hit `/search` (robots-disallowed) and must detect
Cloudflare's 202 soft-block.

If Goodreads changes their `/book/show/{id}` markup, one of the
`_merge_detail_page` tests will fail and tell us which selector to
update — that endpoint remains robots-permitted and in scope.
"""
import httpx

from app.metadata.record import MetaRecord
from app.metadata.sources.goodreads import (
    GoodreadsSource,
    _merge_detail_page,
    _parse_series_string,
)


class TestSearchBookDisabled:
    """v2.10.4 — `/search` is robots-disallowed for `*` user-agents.
    Free-text title+author search no longer fires that endpoint; it
    returns None gracefully and lets the dispatcher move on."""

    async def test_search_book_returns_none_without_hitting_network(self):
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            return httpx.Response(200, content=b"<html></html>")

        src = GoodreadsSource()
        src.set_client(httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=5.0,
        ))

        result = await src.search_book("The Way of Kings", "Brandon Sanderson")

        assert result is None
        # The whole point: zero HTTP traffic from search_book.
        assert calls == []
        await src.close()

    async def test_search_book_empty_title_returns_none(self):
        src = GoodreadsSource()
        assert await src.search_book("", "any author") is None
        await src.close()


_LONG_DESC = (
    "On the world of Roshar, a sweeping epic of storms, assassins, "
    "and the last Knights Radiant. Kaladin, Shallan, and Dalinar's "
    "paths converge over the long course of this first volume."
)

_DETAIL_HTML = f"""
<html><head>
<meta property="og:description" content="A sweeping epic fantasy." />
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "Book",
  "name": "The Way of Kings",
  "author": [{{"@type": "Person", "name": "Brandon Sanderson"}}],
  "datePublished": "2010-08-31",
  "inLanguage": "en",
  "image": "https://i.gr-assets.com/books/detail.jpg",
  "numberOfPages": 1007,
  "isbn": "978-0-7653-2635-5",
  "description": "{_LONG_DESC}"
}}
</script>
</head><body>
<h3 class="Text__title3">
  <a href="/series/49075-the-stormlight-archive">The Stormlight Archive #1</a>
</h3>
</body></html>
"""


class TestMergeDetailPage:
    def test_jsonld_fields_populate(self):
        record = MetaRecord(title="The Way of Kings", source="goodreads")
        _merge_detail_page(record, _DETAIL_HTML)
        assert record.pub_date == "2010-08-31"
        assert record.language == "en"
        assert record.page_count == 1007
        assert record.isbn == "9780765326355"
        assert record.cover_url == "https://i.gr-assets.com/books/detail.jpg"
        assert record.authors == ["Brandon Sanderson"]
        # JSON-LD's full description wins over the shorter og:description
        # teaser — mirrors the longest-wins policy at the enricher merge
        # layer.
        assert record.description == _LONG_DESC
        assert record.series == "The Stormlight Archive"
        assert record.series_index == 1.0

    def test_falls_back_to_og_when_jsonld_description_missing(self):
        """No JSON-LD description → og:description is used."""
        html = """
        <html><head>
        <meta property="og:description" content="A sweeping epic fantasy." />
        </head><body></body></html>
        """
        record = MetaRecord(title="T", source="goodreads")
        _merge_detail_page(record, html)
        assert record.description == "A sweeping epic fantasy."

    def test_itemprop_description_is_picked_when_longest(self):
        """Schema.org microdata `[itemprop="description"]` is extracted
        alongside og:description and the longer one wins."""
        long_body = (
            "A far fuller description of the book, dozens of "
            "sentences long and covering plot, characters, and "
            "themes in far more detail than the og:description "
            "teaser would ever fit."
        )
        html = f"""
        <html><head>
        <meta property="og:description" content="Short teaser." />
        </head><body>
        <span itemprop="description">{long_body}</span>
        </body></html>
        """
        record = MetaRecord(title="T", source="goodreads")
        _merge_detail_page(record, html)
        assert record.description == long_body

    def test_goodreads_boilerplate_is_rejected(self):
        """v2.18.2: when the only description on the page is Goodreads'
        site-wide tagline (served as og:description for unreleased/stub
        book pages), reject it instead of storing it.

        Triggered by Spirit Blade (tid=1243620, unreleased) where the
        47-char tagline beat a missing-because-unfetched MAM description
        in `_pick_longer`. The fix at the source layer prevents the
        boilerplate from ever entering the merge.
        """
        html = """
        <html><head>
        <meta property="og:description"
              content="Discover and share books you love on Goodreads." />
        </head><body></body></html>
        """
        record = MetaRecord(title="Spirit Blade", source="goodreads")
        _merge_detail_page(record, html)
        assert record.description in (None, "")

    def test_real_description_still_wins_over_boilerplate(self):
        """Boilerplate-rejection only fires when boilerplate is the chosen
        candidate. A real long description on the same page must still
        be selected normally."""
        long_body = (
            "Aaron Conner lives a quiet life. He waits tables at a "
            "local restaurant and helps keep the apartment building "
            "he lives in maintained."
        )
        html = f"""
        <html><head>
        <meta property="og:description"
              content="Discover and share books you love on Goodreads." />
        </head><body>
        <span itemprop="description">{long_body}</span>
        </body></html>
        """
        record = MetaRecord(title="Spirit Blade", source="goodreads")
        _merge_detail_page(record, html)
        assert record.description == long_body

    def test_longest_description_wins_even_if_preexisting(self):
        """Tier 1 UAT context: per-source parsers should extract the
        best description from THEIR page and let the enricher's
        cross-source merge sort out priority. Under the new
        longest-wins policy, even a pre-populated `record.description`
        can be replaced if this page offers a longer one — the
        per-source contract is "return the best data from my
        source."""
        record = MetaRecord(
            title="The Way of Kings",
            description="Short pre-existing blurb.",  # 25 chars
            source="goodreads",
        )
        _merge_detail_page(record, _DETAIL_HTML)
        # JSON-LD description (>150 chars) beats the pre-existing one.
        assert record.description == _LONG_DESC


class TestParseSeriesString:
    def test_simple_split(self):
        name, idx = _parse_series_string("Stormlight Archive #1")
        assert name == "Stormlight Archive"
        assert idx == 1.0

    def test_decimal_index(self):
        name, idx = _parse_series_string("Mistborn #2.5")
        assert name == "Mistborn"
        assert idx == 2.5

    def test_no_index(self):
        name, idx = _parse_series_string("Series Name")
        assert name == "Series Name"
        assert idx is None


class TestABlockIsSkippedNotMissed:
    """G192 (2026-10-21): a block met by the book's own Goodreads lookup was
    filed 'no match', so G182 never retried it (review 2212). It is now
    `soft_blocked`, and the enricher's cleaned-title retry isn't sent."""

    @staticmethod
    def _install(monkeypatch, *, soft_blocked=False, page=None):
        from app.metadata import goodreads_id_resolver, goodreads_session
        from app.metadata.sources import goodreads as gr_src
        resolves: list[str] = []

        async def resolve(query):
            resolves.append(query.title)
            return goodreads_id_resolver.ResolveResult(
                None if soft_blocked else "123", "auto_complete", soft_blocked=soft_blocked,
            )

        class _Session:
            async def get(self, url, **_):
                if isinstance(page, Exception):
                    raise page
                return page

        async def get_session():
            return _Session()

        async def no_stored(book_id):
            return None

        monkeypatch.setattr(goodreads_id_resolver, "resolve_goodreads_id", resolve)
        monkeypatch.setattr(goodreads_session, "get_session", get_session)
        monkeypatch.setattr(gr_src, "_stored_record", no_stored)
        return resolves

    @staticmethod
    async def _status():
        from app.metadata.enricher import EnrichmentConfig, MetadataEnricher
        enricher = MetadataEnricher(EnrichmentConfig(enabled=True), sources=[GoodreadsSource()])
        await enricher.enrich(title="The Tale: Book 1", author="Author")
        return enricher.last_source_log[0]["status"]

    async def test_a_resolver_block(self, monkeypatch):
        resolves = self._install(monkeypatch, soft_blocked=True)
        assert await self._status() == "soft_blocked"
        assert resolves == ["The Tale: Book 1"]

    async def test_a_book_page_block(self, monkeypatch):
        resolves = self._install(monkeypatch, page=httpx.Response(202, content=b""))
        assert await self._status() == "soft_blocked"
        assert len(resolves) == 1

    async def test_the_backoff_refusing_the_page(self, monkeypatch):
        from app.metadata import goodreads_session
        self._install(monkeypatch, page=goodreads_session.GoodreadsBackingOff("book_page", 9e12))
        assert await self._status() == "soft_blocked"

    async def test_a_page_that_is_gone_stays_no_result(self, monkeypatch):
        resolves = self._install(monkeypatch, page=httpx.Response(404, content=b""))
        assert await self._status() == "no_result"
        assert resolves == ["The Tale: Book 1", "The Tale 1"]    # the cleaned title tried
