"""
Unit tests for the MAM torrent-info lookup (search API by ID).

Covers VIP/free/fl_vip parsing, caching, error handling, and the
boolean coercion logic for MAM's mixed string/int/bool representations.
All requests go through the FakeMAM fixture.
"""
import json

import pytest

from app.mam.torrent_info import (
    TorrentInfo,
    TorrentInfoError,
    TorrentNotFoundError,
    _classify_identifier,
    _to_bool,
    get_torrent_info,
    invalidate_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure each test starts with a clean torrent-info cache."""
    invalidate_cache()
    yield
    invalidate_cache()


def _make_search_response(overrides: dict | None = None) -> bytes:
    """Build a realistic search-by-id response with optional field overrides."""
    item = {
        "id": "965093",
        "language": "1",
        "main_cat": "14",
        "category": "63",
        "catname": "Ebooks - Fantasy",
        "size": "5242880",
        "numfiles": "1",
        "vip": "0",
        "free": "0",
        "fl_vip": "0",
        "personal_freeleech": "0",
        "title": "Test Book",
        "name": "Test Book",
        "seeders": "5",
        "leechers": "0",
        "times_completed": "42",
    }
    if overrides:
        item.update(overrides)
    return json.dumps({
        "perpage": 1,
        "start": 0,
        "found": 1,
        "data": [item],
    }).encode()


# ─── Happy path ─────────────────────────────────────────────


class TestGetTorrentInfoSuccess:
    async def test_parses_regular_torrent(self, fake_mam):
        fake_mam.search.body = _make_search_response()
        info = await get_torrent_info("965093", token="tok")
        assert isinstance(info, TorrentInfo)
        assert info.torrent_id == "965093"
        assert info.vip is False
        assert info.free is False
        assert info.fl_vip is False
        assert info.personal_freeleech is False
        assert info.category == "Ebooks - Fantasy"
        assert info.title == "Test Book"

    async def test_parses_vip_torrent(self, fake_mam):
        fake_mam.search.body = _make_search_response({
            "vip": "1", "fl_vip": "1",
        })
        info = await get_torrent_info("965093", token="tok")
        assert info.vip is True
        assert info.fl_vip is True
        assert info.free is False

    async def test_parses_freeleech_torrent(self, fake_mam):
        fake_mam.search.body = _make_search_response({
            "free": "1", "fl_vip": "1",
        })
        info = await get_torrent_info("965093", token="tok")
        assert info.vip is False
        assert info.free is True
        assert info.fl_vip is True

    async def test_parses_description_when_present(self, fake_mam):
        """v2.18.2: when MAM returns the opt-in `description` field,
        TorrentInfo preserves it verbatim. Plain-text normalization
        happens at the metadata-source layer (mam_search.py), not
        here — torrent_info stores raw HTML/BBCode as-is.
        """
        fake_mam.search.body = _make_search_response({
            "description": '<p style="margin:0">Long synopsis here.</p>',
        })
        info = await get_torrent_info("965093", token="tok")
        assert info.description == '<p style="margin:0">Long synopsis here.</p>'

    async def test_parses_personal_freeleech(self, fake_mam):
        fake_mam.search.body = _make_search_response({
            "personal_freeleech": "1",
        })
        info = await get_torrent_info("965093", token="tok")
        assert info.personal_freeleech is True

    async def test_parses_my_snatched(self, fake_mam):
        fake_mam.search.body = _make_search_response({"my_snatched": 1})
        info = await get_torrent_info("965093", token="tok")
        assert info.my_snatched is True

    async def test_my_snatched_absent_or_zero_is_false(self, fake_mam):
        fake_mam.search.body = _make_search_response()
        assert (await get_torrent_info("965093", token="tok")).my_snatched is False
        invalidate_cache()
        fake_mam.search.body = _make_search_response({"my_snatched": 0})
        assert (await get_torrent_info("965093", token="tok")).my_snatched is False

    async def test_request_hits_search_endpoint(self, fake_mam):
        fake_mam.search.body = _make_search_response()
        await get_torrent_info("965093", token="tok")
        assert any(
            "loadSearchJSONbasic.php" in str(req.url)
            for req in fake_mam.requests
        )

    async def test_payload_opts_into_isbn_and_description(self, fake_mam):
        """v2.18.2: payload must set both `isbn: true` and `description: true`.

        MAM's loadSearchJSONbasic.php omits these fields from the
        response by default. Probed v2.18.2: these are the only two
        real opt-in flags on the endpoint; both must be present or
        the enricher loses the uploader's full synopsis (which then
        lets Goodreads boilerplate win the longest-wins merge).
        """
        fake_mam.search.body = _make_search_response()
        await get_torrent_info("965093", token="tok")
        search_reqs = [
            r for r in fake_mam.requests
            if "loadSearchJSONbasic.php" in str(r.url)
        ]
        assert len(search_reqs) == 1
        body = json.loads(search_reqs[0].content)
        assert body.get("isbn") is True, "isbn opt-in flag missing"
        assert body.get("description") is True, "description opt-in flag missing"


# ─── Uploader (`ownership`) ─────────────────────────────────


class TestUploaderParsing:
    """The excluded-uploader guard keys on `uploader_name`. MAM's API
    reference documents `owner` / `owner_name`, but the live response
    (2026-10-07) has neither: the uploader arrives as
    `ownership`, a JSON-encoded string like `author_info`. An empty
    name makes the guard silently inert."""

    async def test_live_shape_json_string(self, fake_mam):
        fake_mam.search.body = _make_search_response({
            "ownership": '[12345,"SomeUploader"]',
        })
        info = await get_torrent_info("965093", token="tok")
        assert info.uploader_id == 12345
        assert info.uploader_name == "SomeUploader"

    async def test_decoded_list_still_accepted(self, fake_mam):
        fake_mam.search.body = _make_search_response({
            "ownership": [12345, "SomeUploader"],
        })
        info = await get_torrent_info("965093", token="tok")
        assert info.uploader_id == 12345
        assert info.uploader_name == "SomeUploader"

    @pytest.mark.parametrize("value", [None, "", "not json", "[]", "{}", '["x"]'])
    async def test_missing_or_malformed_is_empty(self, fake_mam, value):
        overrides = {} if value is None else {"ownership": value}
        fake_mam.search.body = _make_search_response(overrides)
        info = await get_torrent_info("965093", token="tok")
        assert info.uploader_id == 0
        assert info.uploader_name == ""


# ─── Caching ────────────────────────────────────────────────


class TestTorrentInfoCaching:
    async def test_second_call_uses_cache(self, fake_mam):
        fake_mam.search.body = _make_search_response()
        await get_torrent_info("965093", token="tok")
        await get_torrent_info("965093", token="tok")
        search_requests = [
            r for r in fake_mam.requests
            if "loadSearchJSONbasic.php" in str(r.url)
        ]
        assert len(search_requests) == 1

    async def test_different_ids_not_cached_together(self, fake_mam):
        fake_mam.search.body = _make_search_response()
        await get_torrent_info("111", token="tok")
        await get_torrent_info("222", token="tok")
        search_requests = [
            r for r in fake_mam.requests
            if "loadSearchJSONbasic.php" in str(r.url)
        ]
        assert len(search_requests) == 2

    async def test_ttl_zero_bypasses_cache(self, fake_mam):
        fake_mam.search.body = _make_search_response()
        await get_torrent_info("965093", token="tok")
        await get_torrent_info("965093", token="tok", ttl=0)
        search_requests = [
            r for r in fake_mam.requests
            if "loadSearchJSONbasic.php" in str(r.url)
        ]
        assert len(search_requests) == 2


# ─── Error handling ─────────────────────────────────────────


class TestTorrentInfoErrors:
    async def test_http_403_raises(self, fake_mam):
        fake_mam.search.status = 403
        fake_mam.search.body = b"forbidden"
        with pytest.raises(TorrentInfoError, match="HTTP 403"):
            await get_torrent_info("965093", token="bad")

    async def test_empty_body_raises(self, fake_mam):
        fake_mam.search.body = b""
        with pytest.raises(TorrentInfoError, match="empty response"):
            await get_torrent_info("965093", token="tok")

    async def test_no_results_raises(self, fake_mam):
        fake_mam.search.body = b'{"perpage":1,"start":0,"found":0,"data":[]}'
        with pytest.raises(TorrentInfoError, match="not found"):
            await get_torrent_info("999999", token="tok")

    async def test_no_results_is_the_permanent_not_found_subclass(self, fake_mam):
        # ADR-0006: empty data = removed from MAM. Transient failures
        # (403, empty body) must NOT be the not-found subclass.
        fake_mam.search.body = b'{"perpage":1,"start":0,"found":0,"data":[]}'
        with pytest.raises(TorrentNotFoundError):
            await get_torrent_info("999999", token="tok")
        fake_mam.search.body = b""
        with pytest.raises(TorrentInfoError) as exc:
            await get_torrent_info("999998", token="tok")
        assert not isinstance(exc.value, TorrentNotFoundError)


# ─── Boolean coercion ───────────────────────────────────────


class TestToBool:
    def test_string_zero(self):
        assert _to_bool("0") is False

    def test_string_one(self):
        assert _to_bool("1") is True

    def test_string_true(self):
        assert _to_bool("true") is True

    def test_int_zero(self):
        assert _to_bool(0) is False

    def test_int_one(self):
        assert _to_bool(1) is True

    def test_bool_true(self):
        assert _to_bool(True) is True

    def test_bool_false(self):
        assert _to_bool(False) is False

    def test_none(self):
        assert _to_bool(None) is False

    def test_empty_string(self):
        assert _to_bool("") is False


# ─── v2.13.2: ISBN/ASIN classifier ──────────────────────────


class TestClassifyIdentifier:
    """`_classify_identifier` splits MAM's free-text ISBN/ASIN field."""

    def test_none_returns_empty_pair(self):
        assert _classify_identifier(None) == ("", "")

    def test_empty_string_returns_empty_pair(self):
        assert _classify_identifier("") == ("", "")

    def test_whitespace_only_returns_empty_pair(self):
        assert _classify_identifier("   ") == ("", "")

    def test_unparseable_types_returns_empty_pair(self):
        # Defensive — dict / list / bool inputs aren't valid
        # identifiers; drop without crashing.
        assert _classify_identifier(["B0XXXXX"]) == ("", "")
        assert _classify_identifier({"isbn": "x"}) == ("", "")
        assert _classify_identifier(True) == ("", "")
        assert _classify_identifier(False) == ("", "")

    def test_int_isbn_from_mam_response(self):
        # CRITICAL — UAT 2026-05-16 surfaced this: MAM returns bare-
        # digit ISBNs as JSON integers (no quotes), so the classifier
        # MUST accept int input. ASINs always arrive as strings
        # because they contain letters.
        assert _classify_identifier(9798902092261) == ("9798902092261", "")
        assert _classify_identifier(9781234567890) == ("9781234567890", "")

    def test_bare_isbn_13(self):
        # Failure Frame Vol 13 — confirmed via probe 2026-05-16.
        assert _classify_identifier("9798902092261") == ("9798902092261", "")

    def test_bare_isbn_13_with_dashes(self):
        # Failure Frame Vol 1 ebook — MAM returned this exact form.
        assert _classify_identifier("979-8895615560") == ("9798895615560", "")

    def test_isbn_10_with_x_checksum(self):
        # The X checksum character on ISBN-10 must be preserved.
        assert _classify_identifier("043942089X") == ("043942089X", "")

    def test_asin_prefix_uppercase(self):
        # Per MAM upload-form convention.
        assert _classify_identifier("ASIN:B0H1XKSFHQ") == ("", "B0H1XKSFHQ")

    def test_asin_prefix_lowercase(self):
        # Defensive — uploaders may not match the documented case.
        assert _classify_identifier("asin:b0h1xksfhq") == ("", "B0H1XKSFHQ")

    def test_asin_prefix_mixed_with_whitespace(self):
        assert _classify_identifier("  Asin:  B0H1XKSFHQ  ") == ("", "B0H1XKSFHQ")

    def test_bare_asin_uploader_forgot_prefix(self):
        # MAM upload form says "For ASIN please prefix with 'ASIN:'"
        # but uploaders sometimes forget. Sniff B0+8 alphanumerics.
        assert _classify_identifier("B0H1XKSFHQ") == ("", "B0H1XKSFHQ")

    def test_bare_asin_lowercase_sniffed(self):
        assert _classify_identifier("b0h1xksfhq") == ("", "B0H1XKSFHQ")

    def test_isbn_prefix_defensive(self):
        # Upload form doesn't tell uploaders to use "ISBN:" but it's a
        # natural mistake — handle it gracefully.
        assert _classify_identifier("ISBN:9781234567890") == ("9781234567890", "")
        assert _classify_identifier("isbn:978-1-2345-6789-0") == ("9781234567890", "")

    def test_garbage_treated_as_isbn_digits_only(self):
        # If MAM ever returns something unrecognized, fall through to
        # the ISBN path so we don't drop the value silently.
        assert _classify_identifier("9781234abc567") == ("9781234567", "")

    def test_alphabetic_garbage_returns_empty(self):
        # Pure letters can't be either ISBN or ASIN — drop.
        assert _classify_identifier("hello world") == ("", "")
