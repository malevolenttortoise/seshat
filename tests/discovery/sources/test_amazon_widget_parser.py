"""
Tests for the Amazon Author-Store widget parser
(v2.11.0 Stage 5++ commit 2/6).

Fixture: `tests/fixtures/amazon/sanderson_allbooks_page1.html` —
saved Firefox capture of Brandon Sanderson's
``/stores/author/B001IGFHW6/allbooks`` (1.49 MB). Validates that the
embedded `content` JSON parses faithfully, that mediaMatrix-driven
format variants are extracted, and that the CSRF + session tokens
needed for the follow-on /juvec POSTs round-trip cleanly.

The /juvec response shape is exercised via synthetic fixtures
mirroring the SSR widget's `content` schema. Commit 4 validates this
inference live and adjusts if Amazon's response framing differs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.discovery.sources.amazon_widget_parser import (
    AllBooksPageData,
    BINDING_TO_FILTER,
    DEFAULT_LANGUAGES,
    FILTER_TO_BINDING,
    FormatVariant,
    JuvecResponse,
    ParseError,
    Product,
    parse_allbooks_html,
    parse_juvec_response,
)


FIXTURE_DIR = Path(__file__).parent.parent.parent / "fixtures" / "amazon"
SANDERSON_HTML = FIXTURE_DIR / "sanderson_allbooks_page1.html"


@pytest.fixture(scope="module")
def sanderson_html() -> str:
    return SANDERSON_HTML.read_text()


@pytest.fixture(scope="module")
def sanderson_data(sanderson_html: str) -> AllBooksPageData:
    return parse_allbooks_html(sanderson_html)


class TestAllBooksHTMLBasicFields:
    """Top-level pageContext + token extraction from the SSR HTML."""

    def test_author_id_extracted(self, sanderson_data):
        assert sanderson_data.author_id == "B001IGFHW6"

    def test_store_id_extracted(self, sanderson_data):
        # Mark's capture: storeId is a UUID v4 generated server-side
        # per Amazon's author-store. Stable across captures of the
        # same author but different per author.
        assert sanderson_data.store_id == "264b6c18-49aa-3168-afdd-309475c0e555"

    def test_root_page_id_extracted(self, sanderson_data):
        assert sanderson_data.root_page_id == "e404fdad-587a-39a3-b453-d17a440bb311"

    def test_version_equals_store_id(self, sanderson_data):
        # In the captures we've seen, `version` mirrors `pageId` /
        # `storeId` — observed but document to surface if Amazon
        # later decouples them.
        assert sanderson_data.version == "264b6c18-49aa-3168-afdd-309475c0e555"

    def test_csrf_tokens_extracted(self, sanderson_data):
        # We don't pin the exact values — they're per-page-load and
        # rotate. But all three must be present and reasonably long.
        assert sanderson_data.slate_token
        assert len(sanderson_data.slate_token) > 100
        assert sanderson_data.fresh_cart_csrf_token
        assert len(sanderson_data.fresh_cart_csrf_token) > 40
        assert sanderson_data.amazon_api_csrf_token
        assert sanderson_data.amazon_api_csrf_token.startswith("1@")

    def test_visit_id_extracted(self, sanderson_data):
        assert sanderson_data.visit_id == "4340eb99-d3b7-4f71-a1eb-38a1909af350"

    def test_obfuscated_marketplace_id(self, sanderson_data):
        # ATVPDKIKX0DER = amazon.com US marketplace
        assert sanderson_data.obfuscated_marketplace_id == "ATVPDKIKX0DER"


class TestAllBooksHTMLProductGrid:
    """The ProductGrid widget's embedded `content` blob."""

    def test_total_result_count(self, sanderson_data):
        assert sanderson_data.total_result_count == 645

    def test_total_count_matches(self, sanderson_data):
        assert sanderson_data.total_count == 645

    def test_asin_list_full_page(self, sanderson_data):
        # Page-1 ASIN list has 112 entries (Amazon's default
        # pageSize=112 in the authorSearch block).
        assert len(sanderson_data.asin_list) == 112

    def test_asin_list_first_entry(self, sanderson_data):
        # B002GYI9C4 = Mistborn: The Final Empire, Kindle.
        assert sanderson_data.asin_list[0] == "B002GYI9C4"

    def test_populated_products_count(self, sanderson_data):
        # ~85 fully-populated products on page 1; the remaining ~27
        # ASINs are filled in by client-side /juvec batches as the
        # user scrolls.
        assert 70 < len(sanderson_data.products) <= 112

    def test_available_languages_includes_english(self, sanderson_data):
        assert "English" in sanderson_data.available_languages
        # Plus a healthy range of Sanderson translations.
        assert "Spanish" in sanderson_data.available_languages
        assert "ChineseSimplified" in sanderson_data.available_languages

    def test_sort_options_includes_popularity(self, sanderson_data):
        sort_values = {s.get("sortValue") for s in sanderson_data.sort_options}
        assert "author-sidecar-rank" in sort_values


class TestMistbornProduct:
    """Mistborn: The Final Empire is the canonical test case — it
    has all 6 format variants in its mediaMatrix and well-known
    series structure."""

    @pytest.fixture(scope="class")
    def mistborn(self, sanderson_data) -> Product:
        for p in sanderson_data.products:
            if p.asin == "B002GYI9C4":
                return p
        pytest.fail("Mistborn Kindle product not in parsed payload")

    def test_title(self, mistborn):
        assert mistborn.title == "Mistborn: The Final Empire"

    def test_contributors(self, mistborn):
        assert "Brandon Sanderson" in mistborn.contributors

    def test_binding_symbol(self, mistborn):
        # Server returns kindle_edition for the Kindle ASIN even
        # though the filter input value is just "kindle".
        assert mistborn.binding_symbol == "kindle_edition"
        assert mistborn.binding_display == "Kindle Edition"

    def test_series(self, mistborn):
        assert mistborn.series_title == "Mistborn"
        assert mistborn.series_position == 1
        assert mistborn.series_total == 7

    def test_detail_page_link(self, mistborn):
        # Relative URL, joined with https://www.amazon.com upstream.
        assert "/dp/B002GYI9C4" in mistborn.detail_page_link

    def test_cover_url_is_hires_media_amazon(self, mistborn):
        assert mistborn.cover_url is not None
        assert mistborn.cover_url.startswith("https://m.media-amazon.com/images/")

    def test_media_matrix_has_six_variants(self, mistborn):
        # Kindle + Audiobook + Hardcover + Paperback + Mass Market +
        # Preloaded Digital Audio Player = 6 sibling formats.
        symbols = {v.binding_symbol for v in mistborn.media_matrix}
        assert symbols == {
            "kindle_edition", "audio_download", "hardcover",
            "paperback", "mass_market", "preloaded_digital_audio_player",
        }

    def test_media_matrix_hardcover_asin(self, mistborn):
        hardcover = next(
            v for v in mistborn.media_matrix
            if v.binding_symbol == "hardcover"
        )
        # ISBN-10 of the Tor hardcover edition.
        assert hardcover.asin == "076531178X"

    def test_media_matrix_paperback_asin(self, mistborn):
        paperback = next(
            v for v in mistborn.media_matrix
            if v.binding_symbol == "paperback"
        )
        assert paperback.asin == "1250868289"


class TestProductFormatDistribution:
    """Across page 1, the populated products span every binding
    Amazon offers for this author. We don't pin exact counts — they
    drift as Amazon adds editions — but the distribution should
    cover the main 5 formats."""

    def test_binding_distribution(self, sanderson_data):
        from collections import Counter
        bindings = Counter(p.binding_symbol for p in sanderson_data.products)
        # All 5 mainstream formats present.
        for sym in ("kindle_edition", "hardcover", "paperback",
                    "mass_market", "audio_download"):
            assert bindings.get(sym, 0) > 0, (
                f"expected at least one {sym} product in page 1; "
                f"got distribution {dict(bindings)}"
            )

    def test_every_product_has_asin_and_title(self, sanderson_data):
        for p in sanderson_data.products:
            assert p.asin
            assert p.title
            assert p.binding_symbol


class TestParseErrors:
    """Defensive: malformed input should raise ParseError, not crash
    elsewhere."""

    def test_missing_product_grid_marker(self):
        html = "<html><body>no widgets here</body></html>"
        with pytest.raises(ParseError, match="ProductGrid marker"):
            parse_allbooks_html(html)

    def test_content_brace_unbalanced(self):
        html = '"widgetType":"ProductGrid","content":{"foo":"bar"'  # no closing }
        with pytest.raises(ParseError):
            parse_allbooks_html(html)

    def test_content_invalid_json(self):
        # Marker + opening brace, but garbage inside that's still
        # balanced brace-wise (so the scanner returns) yet not JSON.
        html = '"widgetType":"ProductGrid","content":{garbage but balanced}'
        with pytest.raises(ParseError):
            parse_allbooks_html(html)


class TestJuvecResponseParser:
    """Synthetic fixtures built from the SSR `content` schema —
    commit 4 validates against a live response and adjusts if
    Amazon's framing differs.
    """

    def test_filter_application_shape(self):
        """When the client POSTs with authorSearch + authorFilters
        (no ASINList), the server returns a fresh filtered ASINList
        + populated products + totalResultCount for that filter."""
        body = {
            "content": {
                "ASINList": ["B002GYI9C4", "B001QKBHG4"],
                "totalResultCount": 83,
                "products": [
                    {
                        "asin": "B002GYI9C4",
                        "title": {"displayString": "Mistborn: The Final Empire"},
                        "bindingInformation": {
                            "binding": {
                                "symbol": "kindle_edition",
                                "displayString": "Kindle Edition",
                            },
                        },
                        "byLine": {"contributors": [{"name": "Brandon Sanderson"}]},
                        "mediaMatrix": {"items": []},
                    },
                ],
            },
        }
        parsed = parse_juvec_response(body)
        assert parsed.asin_list == ("B002GYI9C4", "B001QKBHG4")
        assert parsed.total_result_count == 83
        assert len(parsed.products) == 1
        assert parsed.products[0].asin == "B002GYI9C4"

    def test_detail_fetch_shape(self):
        """When the client POSTs with ASINList (no authorSearch), the
        server returns just the populated products for those ASINs.
        totalResultCount may be absent — that's fine, returns None."""
        body = {
            "content": {
                "products": [
                    {
                        "asin": "B097KR667N",
                        "title": {"displayString": "Some Sanderson Book"},
                        "bindingInformation": {
                            "binding": {
                                "symbol": "kindle_edition",
                                "displayString": "Kindle Edition",
                            },
                        },
                        "byLine": {"contributors": [{"name": "Brandon Sanderson"}]},
                        "mediaMatrix": {"items": []},
                    },
                ],
            },
        }
        parsed = parse_juvec_response(body)
        assert parsed.asin_list == ()
        assert parsed.total_result_count is None
        assert len(parsed.products) == 1

    def test_unwrapped_content_fallback(self):
        """Defensive: if Amazon ever drops the `content` wrapper,
        parse the body as content directly. Stops a future framing
        change from breaking the scan flow silently."""
        body = {
            "products": [],
            "ASINList": [],
            "totalResultCount": 0,
        }
        parsed = parse_juvec_response(body)
        assert parsed.products == ()
        assert parsed.asin_list == ()

    def test_skips_malformed_products(self):
        """A product entry missing title, binding, or asin gets
        DEBUG-logged and skipped — the rest of the batch still
        comes through."""
        body = {
            "content": {
                "products": [
                    {"asin": "B001IGFHW6"},  # missing title + binding
                    {
                        "asin": "B002GYI9C4",
                        "title": {"displayString": "Mistborn"},
                        "bindingInformation": {
                            "binding": {
                                "symbol": "kindle_edition",
                                "displayString": "Kindle Edition",
                            },
                        },
                    },
                    {
                        # No asin
                        "title": {"displayString": "Ghost"},
                        "bindingInformation": {
                            "binding": {
                                "symbol": "kindle_edition",
                                "displayString": "Kindle Edition",
                            },
                        },
                    },
                ],
            },
        }
        parsed = parse_juvec_response(body)
        assert len(parsed.products) == 1
        assert parsed.products[0].asin == "B002GYI9C4"

    def test_no_content_raises(self):
        """If both top-level and `content`-wrapped extraction fail,
        raise ParseError so the caller can log + return [] instead
        of silently dropping a scan."""
        with pytest.raises(ParseError):
            parse_juvec_response({"unrelated": "shape"})  # type: ignore[arg-type]

    def test_top_level_content_must_be_dict(self):
        with pytest.raises(ParseError):
            parse_juvec_response({"content": "not a dict"})  # type: ignore[arg-type]


class TestFormatMappingTables:
    """The filter → binding mapping is the bridge between the
    Settings UI value (`"kindle"`) and the server's binding symbol
    (`"kindle_edition"`). Both directions must stay in sync."""

    def test_filter_to_binding_complete(self):
        # The four formats we expose in the Settings UI.
        assert FILTER_TO_BINDING == {
            "kindle": "kindle_edition",
            "paperback": "paperback",
            "hardcover": "hardcover",
            "mass_market": "mass_market",
        }

    def test_bidirectional_consistency(self):
        for filter_value, binding_symbol in FILTER_TO_BINDING.items():
            assert BINDING_TO_FILTER[binding_symbol] == filter_value

    def test_audio_bindings_not_in_filter(self):
        """Audible handles audiobooks — we should never offer the
        user `audio_download` / `audioCD` / etc. as Amazon discovery
        formats. The filter table is the gate that enforces it."""
        for filter_value in FILTER_TO_BINDING:
            assert "audio" not in filter_value
            assert filter_value != "audioCD"
            assert filter_value != "mp3_cd"

    def test_default_languages_includes_observed(self):
        # Should contain everything we've seen in real captures.
        for lang in ("English", "Spanish", "Japanese", "ChineseSimplified"):
            assert lang in DEFAULT_LANGUAGES
