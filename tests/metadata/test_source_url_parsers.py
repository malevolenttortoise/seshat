"""
Unit tests for v2.20.0 Phase 3 source-ID parsers + canonical URL builders.
"""
from __future__ import annotations

import pytest

from app.metadata.source_url_parsers import (
    canonical_author_url,
    known_sources,
    parse_amazon,
    parse_audible,
    parse_audiobookshelf,
    parse_google_books,
    parse_goodreads,
    parse_hardcover,
    parse_ibdb,
    parse_kobo,
    parse_openlibrary,
    parse_source_id,
)


class TestParseAmazon:
    @pytest.mark.parametrize("value, expected", [
        ("B001IGFHW6", "B001IGFHW6"),
        ("b001igfhw6", "B001IGFHW6"),
        (" B001IGFHW6 ", "B001IGFHW6"),
        ("https://www.amazon.com/stores/Brandon-Sanderson/author/B001IGFHW6", "B001IGFHW6"),
        ("https://www.amazon.com/stores/author/B001IGFHW6/allbooks", "B001IGFHW6"),
        ("https://amazon.com/-/e/B001IGFHW6", "B001IGFHW6"),
        ("https://amazon.com/Brandon-Sanderson/e/B001IGFHW6", "B001IGFHW6"),
        ("/dp/B001IGFHW6", "B001IGFHW6"),
        ("/marketplaces/ATVPDKIKX0DER/contributors/authors/B001IGFHW6", "B001IGFHW6"),
        ("https://amazon.com/author/B001IGFHW6", "B001IGFHW6"),
    ])
    def test_recognized(self, value, expected):
        assert parse_amazon(value) == expected

    @pytest.mark.parametrize("value", [
        "", "   ", "not-an-asin", "B00", "12345", None,
        "https://example.com/foo/bar",
    ])
    def test_rejected(self, value):
        assert parse_amazon(value) is None


class TestParseGoodreads:
    @pytest.mark.parametrize("value, expected", [
        ("38550", "38550"),
        (" 38550 ", "38550"),
        ("https://www.goodreads.com/author/show/38550", "38550"),
        ("https://www.goodreads.com/author/show/38550.Brandon_Sanderson", "38550"),
        ("goodreads.com/author/show/38550", "38550"),
    ])
    def test_recognized(self, value, expected):
        assert parse_goodreads(value) == expected

    @pytest.mark.parametrize("value", [
        "", "abc", "B001IGFHW6", None,
        "https://goodreads.com/book/show/38550",
    ])
    def test_rejected(self, value):
        assert parse_goodreads(value) is None


class TestParseOpenlibrary:
    @pytest.mark.parametrize("value, expected", [
        ("OL26320A", "OL26320A"),
        ("ol26320a", "OL26320A"),
        ("https://openlibrary.org/authors/OL26320A", "OL26320A"),
        ("https://openlibrary.org/authors/OL26320A/Brandon_Sanderson", "OL26320A"),
    ])
    def test_recognized(self, value, expected):
        assert parse_openlibrary(value) == expected

    @pytest.mark.parametrize("value", [
        "", "OL", "OL26320W", "12345", None,
    ])
    def test_rejected(self, value):
        assert parse_openlibrary(value) is None


class TestParseHardcover:
    def test_numeric_id_passes(self):
        assert parse_hardcover("204214") == "204214"

    def test_url_returns_slug(self):
        assert parse_hardcover(
            "https://hardcover.app/authors/brandon-sanderson"
        ) == "brandon-sanderson"

    def test_garbage_returns_none(self):
        assert parse_hardcover("https://example.com/foo") is None
        assert parse_hardcover("") is None


class TestParseAudible:
    @pytest.mark.parametrize("value, expected", [
        ("B001IGFHW6", "B001IGFHW6"),
        ("https://www.audible.com/author/Brandon-Sanderson/B001IGFHW6", "B001IGFHW6"),
    ])
    def test_recognized(self, value, expected):
        assert parse_audible(value) == expected


class TestParseLooseSources:
    def test_kobo_accepts_anything(self):
        assert parse_kobo("brandon-sanderson") == "brandon-sanderson"
        assert parse_kobo("  spaces  ") == "spaces"
        assert parse_kobo("") is None

    def test_ibdb_numeric_only(self):
        assert parse_ibdb("12345") == "12345"
        assert parse_ibdb("https://www.iblist.com/author.php?id=12345") == "12345"
        assert parse_ibdb("garbage") is None

    def test_google_books_passthrough(self):
        assert parse_google_books("Brandon Sanderson") == "Brandon Sanderson"
        assert parse_google_books("") is None

    def test_audiobookshelf_uuid_only(self):
        assert parse_audiobookshelf(
            "6604ea08-7f4e-49dd-b7ef-f5617682428e"
        ) == "6604ea08-7f4e-49dd-b7ef-f5617682428e"
        assert parse_audiobookshelf("not-a-uuid") is None


class TestCanonicalAuthorUrl:
    def test_amazon(self):
        assert canonical_author_url("amazon", "B001IGFHW6") == (
            "https://www.amazon.com/stores/author/B001IGFHW6/allbooks"
        )

    def test_goodreads(self):
        assert canonical_author_url("goodreads", "38550") == (
            "https://www.goodreads.com/author/show/38550"
        )

    def test_openlibrary(self):
        assert canonical_author_url("openlibrary", "OL26320A") == (
            "https://openlibrary.org/authors/OL26320A"
        )

    def test_hardcover_slug(self):
        assert canonical_author_url("hardcover", "brandon-sanderson") == (
            "https://hardcover.app/authors/brandon-sanderson"
        )

    def test_hardcover_numeric_returns_none(self):
        # Numeric ID has no canonical URL (Hardcover routes by slug).
        assert canonical_author_url("hardcover", "204214") is None

    def test_audible(self):
        assert canonical_author_url("audible", "B001IGFHW6") == (
            "https://www.audible.com/author/-/B001IGFHW6"
        )

    @pytest.mark.parametrize("source", ["kobo", "ibdb", "google_books"])
    def test_loose_sources_have_no_url(self, source):
        assert canonical_author_url(source, "anything") is None

    def test_unknown_source_returns_none(self):
        assert canonical_author_url("myspace", "whatever") is None


class TestDispatch:
    def test_parse_source_id_routes_correctly(self):
        assert parse_source_id("amazon", "B001IGFHW6") == "B001IGFHW6"
        assert parse_source_id("goodreads", "38550") == "38550"

    def test_unknown_source_raises(self):
        with pytest.raises(ValueError, match="unknown source"):
            parse_source_id("myspace", "anything")

    def test_known_sources_includes_all_columns(self):
        ks = set(known_sources())
        for src in ("amazon", "goodreads", "openlibrary", "hardcover",
                    "audible", "kobo", "ibdb", "google_books",
                    "audiobookshelf"):
            assert src in ks
