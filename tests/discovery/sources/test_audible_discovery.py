"""
AudibleDiscoverySource tests.

Covers the author-level catalog search: paginated Audible catalog
walk + Audnexus hydration, result flattening into BookResult, and
the "no hits → None" contract every BaseSource must honor.
"""
from __future__ import annotations

import httpx


def _inject_transport(monkeypatch, handler):
    orig = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: orig(
            transport=httpx.MockTransport(handler),
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )


def _make_handler(catalog_pages: list[list[dict]], audnexus_items: dict[str, dict]):
    """Build a handler that serves paginated catalog + Audnexus hits.

    `catalog_pages[i]` is the `products` list for page i (Audible
    paginates 0-indexed). Empty list on a page ends pagination.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host.startswith("api.audible"):
            page = int(request.url.params.get("page", "0"))
            products = (
                catalog_pages[page] if 0 <= page < len(catalog_pages) else []
            )
            total = sum(len(p) for p in catalog_pages)
            return httpx.Response(200, json={
                "products": products, "total_results": total,
            })
        if host == "api.audnex.us":
            asin = request.url.path.rsplit("/", 1)[-1]
            item = audnexus_items.get(asin)
            if item is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=item)
        return httpx.Response(404)
    return handler


class TestSearchAuthor:
    async def test_single_page_results(self, monkeypatch):
        from app.discovery.sources.audible import AudibleDiscoverySource

        handler = _make_handler(
            catalog_pages=[[
                {"asin": "B0041KLD5I"}, {"asin": "B00HYCFLAG"},
            ]],
            audnexus_items={
                "B0041KLD5I": {
                    "asin": "B0041KLD5I", "title": "The Final Empire",
                    "authors": [{"name": "Brandon Sanderson"}],
                    "narrators": [{"name": "Michael Kramer"}],
                    "runtimeLengthMin": 1498,
                    "seriesPrimary": {"name": "Mistborn", "position": "1"},
                },
                "B00HYCFLAG": {
                    "asin": "B00HYCFLAG", "title": "The Way of Kings",
                    "authors": [{"name": "Brandon Sanderson"}],
                    "narrators": [{"name": "Michael Kramer"}],
                    "seriesPrimary": {"name": "The Stormlight Archive", "position": "1"},
                },
            },
        )
        _inject_transport(monkeypatch, handler)

        src = AudibleDiscoverySource(region="us", rate_limit=0)
        result = await src.search_author("Brandon Sanderson")
        assert result is not None
        assert result.name == "Brandon Sanderson"
        assert len(result.books) == 2
        titles = {b.title for b in result.books}
        assert titles == {"The Final Empire", "The Way of Kings"}
        # Per-book fields propagate from MetaRecord → BookResult.
        final = next(b for b in result.books if b.title == "The Final Empire")
        assert final.series_name == "Mistborn"
        assert final.series_index == 1.0
        assert final.external_id == "B0041KLD5I"
        assert final.source == "audible"

    async def test_paginates_through_multiple_pages(self, monkeypatch):
        from app.discovery.sources.audible import AudibleDiscoverySource

        # Two pages of 2 hits each = 4 unique ASINs. Audnexus's ASIN
        # normalizer requires B + 9 alphanumerics — anything shorter
        # short-circuits before the HTTP call even fires.
        def make_item(asin):
            return {
                "asin": asin, "title": f"Book-{asin}",
                "authors": [{"name": "Some Author"}],
            }

        page1 = [{"asin": "B00000001A"}, {"asin": "B00000002B"}]
        page2 = [{"asin": "B00000003C"}, {"asin": "B00000004D"}]
        handler = _make_handler(
            catalog_pages=[page1, page2],
            audnexus_items={a["asin"]: make_item(a["asin"])
                            for a in page1 + page2},
        )
        _inject_transport(monkeypatch, handler)

        src = AudibleDiscoverySource(rate_limit=0)
        result = await src.search_author("Some Author")
        assert result is not None
        assert len(result.books) == 4

    async def test_empty_catalog_returns_none(self, monkeypatch):
        from app.discovery.sources.audible import AudibleDiscoverySource

        _inject_transport(monkeypatch, _make_handler([[]], {}))
        src = AudibleDiscoverySource(rate_limit=0)
        assert await src.search_author("Obscure Author") is None

    async def test_empty_author_name_returns_none(self):
        from app.discovery.sources.audible import AudibleDiscoverySource
        src = AudibleDiscoverySource(rate_limit=0)
        assert await src.search_author("") is None

    async def test_audnexus_404_skipped_not_fatal(self, monkeypatch):
        """Some ASINs may not exist on Audnexus yet — skip, don't fail."""
        from app.discovery.sources.audible import AudibleDiscoverySource

        handler = _make_handler(
            catalog_pages=[[{"asin": "B00KNOWN01"}, {"asin": "B00MISSING"}]],
            audnexus_items={
                "B00KNOWN01": {
                    "asin": "B00KNOWN01", "title": "Known",
                    "authors": [{"name": "A"}],
                },
                # B00MISSING intentionally missing → Audnexus 404
            },
        )
        _inject_transport(monkeypatch, handler)

        src = AudibleDiscoverySource(rate_limit=0)
        result = await src.search_author("A")
        assert result is not None
        assert len(result.books) == 1
        assert result.books[0].title == "Known"

    def test_invalid_region_falls_back_to_us(self):
        from app.discovery.sources.audible import AudibleDiscoverySource
        src = AudibleDiscoverySource(region="zz")
        assert src.region == "us"
        assert src._catalog_url().endswith(".com/1.0/catalog/products")


class TestContributors:
    """v3.0.0 Phase 3.5 — Audnexus keeps authors + narrators in separate
    fields, so `BookResult.contributors` is populated from the clean
    `authors[]` list (role None, all authors); narrators never leak in.
    Audible is trusted-create."""

    async def test_co_authors_populated_narrator_excluded(self, monkeypatch):
        from app.discovery.sources.audible import AudibleDiscoverySource
        from app.discovery.sources.base import contributor_is_author

        handler = _make_handler(
            catalog_pages=[[{"asin": "B0COAUTH01"}]],
            audnexus_items={
                "B0COAUTH01": {
                    "asin": "B0COAUTH01", "title": "A Co-Authored Audiobook",
                    "authors": [{"name": "Jason Anspach"}, {"name": "Nick Cole"}],
                    "narrators": [{"name": "R.C. Bray"}],
                },
            },
        )
        _inject_transport(monkeypatch, handler)

        result = await AudibleDiscoverySource(rate_limit=0).search_author("Jason Anspach")
        bk = result.books[0]
        assert [(c.name, c.role) for c in bk.contributors] == [
            ("Jason Anspach", None),
            ("Nick Cole", None),
        ]
        # narrator stays out of contributors (it's in rec.narrator)
        assert "R.C. Bray" not in [c.name for c in bk.contributors]
        # both survive the role-filter
        kept = [c.name for c in bk.contributors if contributor_is_author(c.role)]
        assert kept == ["Jason Anspach", "Nick Cole"]

    async def test_single_author_one_contributor(self, monkeypatch):
        from app.discovery.sources.audible import AudibleDiscoverySource

        handler = _make_handler(
            catalog_pages=[[{"asin": "B0SOLO0001"}]],
            audnexus_items={
                "B0SOLO0001": {
                    "asin": "B0SOLO0001", "title": "Solo",
                    "authors": [{"name": "Brandon Sanderson"}],
                    "narrators": [{"name": "Michael Kramer"}],
                },
            },
        )
        _inject_transport(monkeypatch, handler)
        result = await AudibleDiscoverySource(rate_limit=0).search_author("Brandon Sanderson")
        assert [c.name for c in result.books[0].contributors] == ["Brandon Sanderson"]

    def test_region_tld_mapping(self):
        from app.discovery.sources.audible import AudibleDiscoverySource
        assert AudibleDiscoverySource(region="uk")._catalog_url().endswith(".co.uk/1.0/catalog/products")
        assert AudibleDiscoverySource(region="de")._catalog_url().endswith(".de/1.0/catalog/products")


class TestSourceRouting:
    """Content-type-aware source selection."""

    def test_ebook_gets_ebook_sources(self):
        from app.discovery.lookup import (
            _sources_for_content_type, SOURCES,
        )
        assert _sources_for_content_type("ebook") is SOURCES

    def test_audiobook_gets_audiobook_sources(self):
        from app.discovery.lookup import (
            _sources_for_content_type, AUDIOBOOK_SOURCES,
        )
        assert _sources_for_content_type("audiobook") is AUDIOBOOK_SOURCES

    def test_unknown_type_defaults_to_ebook(self):
        """Safer than silently skipping the scan."""
        from app.discovery.lookup import (
            _sources_for_content_type, SOURCES,
        )
        assert _sources_for_content_type("podcast") is SOURCES
        assert _sources_for_content_type("") is SOURCES

    def test_audiobook_sources_lead_with_audible(self):
        from app.discovery.lookup import AUDIOBOOK_SOURCES
        assert AUDIOBOOK_SOURCES[0].name == "audible"

    def test_ebook_sources_no_audible(self):
        """Audible should NOT run on ebook-library author scans."""
        from app.discovery.lookup import SOURCES
        assert "audible" not in [s.name for s in SOURCES]


class TestActiveLibraryContentType:
    def test_no_active_library_returns_ebook(self, monkeypatch):
        from app import state
        from app.discovery import database as disco_db
        monkeypatch.setattr(disco_db, "_active_library_slug", None)
        monkeypatch.setattr(state, "_discovered_libraries", [])
        assert state.get_active_library_content_type() == "ebook"

    def test_reads_content_type_from_discovered_libraries(self, monkeypatch):
        from app import state
        from app.discovery import database as disco_db
        monkeypatch.setattr(disco_db, "_active_library_slug", "abs-lib")
        monkeypatch.setattr(state, "_discovered_libraries", [
            {"slug": "abs-lib", "content_type": "audiobook"},
            {"slug": "calibre", "content_type": "ebook"},
        ])
        assert state.get_active_library_content_type() == "audiobook"

    def test_unknown_slug_defaults_to_ebook(self, monkeypatch):
        from app import state
        from app.discovery import database as disco_db
        monkeypatch.setattr(disco_db, "_active_library_slug", "ghost")
        monkeypatch.setattr(state, "_discovered_libraries", [
            {"slug": "real", "content_type": "audiobook"},
        ])
        assert state.get_active_library_content_type() == "ebook"
