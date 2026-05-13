"""
Tests for the v2.11.0 Stage 4.5 universal URL-paste importer.

Coverage:
  - `parse_url(url)` — pure pattern-matching for each supported source
  - Per-source fetchers — mocked HTTP, asserts canonical dict shape
  - `fetch_by_url(url)` dispatcher — unknown URL raises 400
"""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

from app.discovery.url_import import (
    fetch_amazon_book,
    fetch_by_url,
    fetch_google_books_volume,
    fetch_ibdb_book,
    fetch_openlibrary_isbn,
    fetch_openlibrary_work,
    parse_url,
)


# ─── Pure parser tests ────────────────────────────────────────────


class TestParseUrl:
    def test_goodreads_book_show(self):
        assert parse_url(
            "https://www.goodreads.com/book/show/12345.Some_Title"
        ) == ("goodreads", "12345")

    def test_hardcover_book_slug(self):
        assert parse_url(
            "https://hardcover.app/books/way-of-kings-2010"
        ) == ("hardcover", "way-of-kings-2010")

    def test_amazon_dp(self):
        assert parse_url(
            "https://www.amazon.com/dp/B0CJDP9MNL"
        ) == ("amazon", "B0CJDP9MNL")

    def test_amazon_gp_product(self):
        assert parse_url(
            "https://www.amazon.com/gp/product/0765326353/"
        ) == ("amazon", "0765326353")

    def test_amazon_with_slug_segment(self):
        # Real-world URL has product name segment between domain + /dp/
        assert parse_url(
            "https://www.amazon.com/Way-Kings-Stormlight-Archive-Book/dp/0765326353"
        ) == ("amazon", "0765326353")

    def test_amazon_co_uk(self):
        assert parse_url(
            "https://www.amazon.co.uk/dp/0765326353"
        ) == ("amazon", "0765326353")

    def test_openlibrary_work(self):
        assert parse_url(
            "https://openlibrary.org/works/OL15161W/The_Way_of_Kings"
        ) == ("openlibrary_work", "OL15161W")

    def test_openlibrary_book_edition(self):
        assert parse_url(
            "https://openlibrary.org/books/OL24230520M/The_Way_of_Kings"
        ) == ("openlibrary_book", "OL24230520M")

    def test_openlibrary_isbn(self):
        assert parse_url(
            "https://openlibrary.org/isbn/9780765326355"
        ) == ("openlibrary_isbn", "9780765326355")

    def test_google_books(self):
        assert parse_url(
            "https://books.google.com/books?id=AbCdEfGhIjK"
        ) == ("google_books", "AbCdEfGhIjK")

    def test_google_books_with_extra_params(self):
        assert parse_url(
            "https://books.google.com/books?hl=en&id=AbCdEfGhIjK&dq=foo"
        ) == ("google_books", "AbCdEfGhIjK")

    def test_kobo_ebook(self):
        assert parse_url(
            "https://www.kobo.com/us/en/ebook/spice-and-wolf-vol-1-light-novel"
        ) == ("kobo", "spice-and-wolf-vol-1-light-novel")

    def test_kobo_audiobook(self):
        assert parse_url(
            "https://www.kobo.com/us/en/audiobook/way-of-kings"
        ) == ("kobo", "way-of-kings")

    def test_ibdb_uuid(self):
        assert parse_url(
            "https://ibdb.dev/book/12345678-aaaa-bbbb-cccc-1234567890ab"
        ) == ("ibdb", "12345678-aaaa-bbbb-cccc-1234567890ab")

    def test_unknown_url_returns_none(self):
        assert parse_url("https://random-website.com/book/12345") is None

    def test_empty_string_returns_none(self):
        assert parse_url("") is None

    def test_none_returns_none(self):
        assert parse_url(None) is None  # type: ignore[arg-type]

    def test_whitespace_stripped(self):
        assert parse_url(
            "  https://www.goodreads.com/book/show/12345  "
        ) == ("goodreads", "12345")


# ─── Per-source fetcher tests ─────────────────────────────────────


def _httpx_response(json_data, status=200):
    return httpx.Response(
        status, content=json.dumps(json_data).encode(),
        headers={"Content-Type": "application/json"},
    )


class TestFetchOpenLibraryIsbn:
    async def test_returns_canonical_dict(self):
        payload = {
            "ISBN:9780765326355": {
                "title": "The Way of Kings",
                "authors": [{"name": "Brandon Sanderson", "url": "..."}],
                "publishers": [{"name": "Tor"}],
                "publish_date": "August 31, 2010",
                "number_of_pages": 1007,
                "cover": {
                    "large": "https://covers.openlibrary.org/b/id/12345-L.jpg",
                },
                "url": "https://openlibrary.org/books/OL24230520M/The_Way_of_Kings",
            }
        }

        def handler(req):
            return _httpx_response(payload)

        with patch("app.discovery.url_import.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = _async_returns(
                httpx.Response(200, content=json.dumps(payload).encode())
            )
            result = await fetch_openlibrary_isbn("978-0-7653-2635-5")

        assert result["source"] == "openlibrary"
        assert result["title"] == "The Way of Kings"
        assert result["author_name"] == "Brandon Sanderson"
        assert result["publisher"] == "Tor"
        assert result["isbn"] == "9780765326355"  # normalized
        assert result["page_count"] == 1007
        assert "openlibrary" in json.loads(result["source_url"])

    async def test_no_payload_raises_404(self):
        with patch("app.discovery.url_import.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = _async_returns(
                httpx.Response(200, content=b"{}")
            )
            with pytest.raises(HTTPException) as exc:
                await fetch_openlibrary_isbn("0000000000000")
        assert exc.value.status_code == 404

    async def test_empty_isbn_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            await fetch_openlibrary_isbn("")
        assert exc.value.status_code == 400


class TestFetchOpenLibraryWork:
    async def test_returns_canonical_dict(self):
        # Two-step fetch: /works/{key}.json then /works/{key}/editions.json
        work_payload = {
            "title": "The Way of Kings",
            "description": "An epic of storms and assassins.",
            "covers": [12345],
            "authors": [{"author": {"key": "/authors/OL38550A"}}],
        }
        editions_payload = {
            "entries": [{
                "isbn_13": ["9780765326355"],
                "publishers": ["Tor"],
                "publish_date": "August 31, 2010",
                "number_of_pages": 1007,
            }]
        }
        author_payload = {"name": "Brandon Sanderson"}

        calls = {"work": 0, "editions": 0, "author": 0}

        async def routed_get(url=None, *args, **kwargs):
            # Route by URL path so the two-step + author lookup all resolve
            if "/works/OL15161W.json" in str(url):
                calls["work"] += 1
                resp = httpx.Response(200, content=json.dumps(work_payload).encode())
            elif "/works/OL15161W/editions.json" in str(url):
                calls["editions"] += 1
                resp = httpx.Response(200, content=json.dumps(editions_payload).encode())
            elif "/authors/OL38550A.json" in str(url):
                calls["author"] += 1
                resp = httpx.Response(200, content=json.dumps(author_payload).encode())
            else:
                resp = httpx.Response(404)
            resp._request = httpx.Request("GET", url)
            return resp

        with patch("app.discovery.url_import.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = routed_get
            result = await fetch_openlibrary_work("OL15161W")

        assert calls["work"] == 1
        assert calls["editions"] == 1
        assert calls["author"] == 1
        assert result["source"] == "openlibrary"
        assert result["title"] == "The Way of Kings"
        assert result["author_name"] == "Brandon Sanderson"
        assert result["isbn"] == "9780765326355"
        assert result["publisher"] == "Tor"
        assert result["page_count"] == 1007
        assert result["openlibrary_id"] == "OL15161W"
        assert result["cover_url"].startswith("https://covers.openlibrary.org/b/id/12345")


class TestFetchGoogleBooksVolume:
    async def test_returns_canonical_dict(self):
        payload = {
            "volumeInfo": {
                "title": "The Way of Kings",
                "authors": ["Brandon Sanderson"],
                "publisher": "Tor",
                "publishedDate": "2010-08-31",
                "description": "An epic.",
                "pageCount": 1007,
                "industryIdentifiers": [
                    {"type": "ISBN_13", "identifier": "9780765326355"},
                ],
                "imageLinks": {"thumbnail": "http://example.com/cover.jpg?zoom=1"},
                "infoLink": "https://books.google.com/books?id=ABC",
            }
        }
        with patch("app.discovery.url_import.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = _async_returns(
                httpx.Response(200, content=json.dumps(payload).encode())
            )
            with patch("app.config.load_settings", return_value={"google_books_api_key": ""}):
                result = await fetch_google_books_volume("ABC")

        assert result["source"] == "google_books"
        assert result["title"] == "The Way of Kings"
        assert result["author_name"] == "Brandon Sanderson"
        assert result["isbn"] == "9780765326355"
        assert result["page_count"] == 1007
        assert result["google_books_id"] == "ABC"
        # Cover URL should be upgraded
        assert "zoom=0" in result["cover_url"]
        assert result["cover_url"].startswith("https://")

    async def test_503_raises_503(self):
        with patch("app.discovery.url_import.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = _async_returns(
                httpx.Response(503)
            )
            with patch("app.config.load_settings", return_value={}):
                with pytest.raises(HTTPException) as exc:
                    await fetch_google_books_volume("ANY")
        assert exc.value.status_code == 503

    async def test_404_raises_404(self):
        with patch("app.discovery.url_import.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = _async_returns(
                httpx.Response(404)
            )
            with patch("app.config.load_settings", return_value={}):
                with pytest.raises(HTTPException) as exc:
                    await fetch_google_books_volume("UNKNOWN")
        assert exc.value.status_code == 404


class TestFetchIbdbBook:
    async def test_returns_canonical_dict(self):
        payload = {
            "title": "Spice and Wolf, Vol. 1",
            "authors": [{"name": "Isuna Hasekura"}],
            "isbn13": "9780759530355",
            "synopsis": "An itinerant merchant meets a wolf-goddess.",
            "publicationDate": "2010-12-21",
            "image": {"url": "https://covers.ibdb.dev/abc.jpg"},
            "pageCount": 226,
        }
        with patch("app.discovery.url_import.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = _async_returns(
                httpx.Response(200, content=json.dumps(payload).encode())
            )
            result = await fetch_ibdb_book("12345678-aaaa-bbbb-cccc-1234567890ab")

        assert result["source"] == "ibdb"
        assert result["title"] == "Spice and Wolf, Vol. 1"
        assert result["author_name"] == "Isuna Hasekura"
        assert result["isbn"] == "9780759530355"
        assert result["page_count"] == 226
        assert result["ibdb_id"] == "12345678-aaaa-bbbb-cccc-1234567890ab"

    async def test_404_raises_404(self):
        with patch("app.discovery.url_import.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = _async_returns(
                httpx.Response(404)
            )
            with pytest.raises(HTTPException) as exc:
                await fetch_ibdb_book("00000000-0000-0000-0000-000000000000")
        assert exc.value.status_code == 404


class TestFetchAmazonBook:
    async def test_invalid_asin_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            await fetch_amazon_book("SHORT")
        assert exc.value.status_code == 400

    async def test_captcha_page_detected_as_soft_block(self):
        # Sub-50KB body with CAPTCHA marker → 503 soft-block error
        captcha_html = (
            "<html><head><title>Robot Check</title></head>"
            "<body>Enter the characters you see below</body></html>"
        )
        with patch("app.discovery.url_import.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = _async_returns(
                httpx.Response(200, content=captcha_html.encode())
            )
            with pytest.raises(HTTPException) as exc:
                await fetch_amazon_book("B0CJDP9MNL")
        assert exc.value.status_code == 503
        assert "CAPTCHA" in exc.value.detail

    async def test_503_response_raises_503(self):
        with patch("app.discovery.url_import.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = _async_returns(
                httpx.Response(503, content=b"")
            )
            with pytest.raises(HTTPException) as exc:
                await fetch_amazon_book("B0CJDP9MNL")
        assert exc.value.status_code == 503


# ─── Dispatcher tests ──────────────────────────────────────────────


class TestFetchByUrl:
    async def test_unknown_url_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            await fetch_by_url("https://random-site.com/book/123")
        assert exc.value.status_code == 400
        assert "Unrecognized" in exc.value.detail

    async def test_empty_url_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            await fetch_by_url("")
        assert exc.value.status_code == 400


# ─── Test helpers ──────────────────────────────────────────────────


def _async_returns(response: httpx.Response):
    """Build an async mock that returns the same response on every call.

    Attaches a synthetic request on each call so `raise_for_status()`
    works without needing the caller to track the URL.
    """
    async def fake_get(url=None, *args, **kwargs):
        # Re-attach a fresh request on each call so the response can
        # be raised correctly. httpx requires `request` for
        # raise_for_status to be callable.
        response._request = httpx.Request("GET", url or "https://example.test/")
        return response
    return fake_get
