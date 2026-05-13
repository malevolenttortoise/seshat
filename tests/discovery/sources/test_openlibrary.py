"""
Tests for the v2.10.6 OpenLibrarySource.

Mirrors the Hardcover test pattern: monkeypatch HTTP at the
`_get` level since `BaseSource._get_client` rebuilds the client
on every property access (which clobbers any MockTransport
injected on `self._client`).

Coverage:
  - Series-from-title extractor edge cases
  - `_resolve_author_keys` disambiguation (single, multi w/ name match,
    multi w/ work_count tiebreak, no match → fallback to top hit,
    variant-query recovery, cross-script aggregation)
  - `_fetch_all_author_works` pagination (single page, multi-page,
    partial-page stop)
  - End-to-end `search_author` (assembled AuthorResult, no-author miss,
    multi-record aggregation with dedup)
  - `get_author_books` is a no-op stub (mirrors HardcoverSource pattern)
"""
from __future__ import annotations

import json

import httpx

from app.discovery.sources.openlibrary import (
    OpenLibrarySource,
    _EDITION_REJECT_RX,
    _extract_language,
    _extract_series_from_title,
    _has_cjk,
    _query_variants,
)


def _patch_get(src: OpenLibrarySource, responses: dict, *, call_log: list = None):
    """Patch `_get` to map URL substrings to canned httpx.Responses.

    `responses` keys are URL substrings (e.g. "search/authors.json",
    "/authors/OL26320A/works.json"). Values are either:
      - a single dict (returned as JSON every time the URL matches)
      - a list of dicts (returned in order, one per call — for pagination tests)
    """
    state = {k: 0 for k in responses}

    async def fake_get(url: str, retries: int = 2, **kwargs):  # noqa: ARG001
        for url_frag, payload in responses.items():
            if url_frag in url:
                if call_log is not None:
                    call_log.append((url_frag, dict(kwargs.get("params") or {})))
                if isinstance(payload, list):
                    idx = state[url_frag]
                    state[url_frag] = idx + 1
                    body = payload[idx] if idx < len(payload) else {}
                else:
                    body = payload
                return httpx.Response(
                    200, content=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                )
        if call_log is not None:
            call_log.append(("unmatched", url))
        return httpx.Response(404, content=b"")

    src._get = fake_get  # type: ignore[method-assign]


def _make_source() -> OpenLibrarySource:
    return OpenLibrarySource(rate_limit=0)


# ── Pure helper: series extraction from title ─────────────────────


class TestSeriesExtraction:
    def test_simple_series_with_index(self):
        s, idx, cleaned = _extract_series_from_title("The Way of Kings (Stormlight Archive, #1)")
        assert s == "Stormlight Archive"
        assert idx == 1.0
        assert cleaned == "The Way of Kings"

    def test_series_with_decimal_index(self):
        s, idx, cleaned = _extract_series_from_title("Brief Cases (Dresden Files #15.6)")
        assert s == "Dresden Files"
        assert idx == 15.6
        assert cleaned == "Brief Cases"

    def test_series_without_index(self):
        s, idx, cleaned = _extract_series_from_title("Some Anthology (Stormlight Archive)")
        assert s == "Stormlight Archive"
        assert idx is None
        assert cleaned == "Some Anthology"

    def test_no_series_returns_unchanged(self):
        s, idx, cleaned = _extract_series_from_title("Just A Title")
        assert s is None
        assert idx is None
        assert cleaned == "Just A Title"

    def test_empty_title_safe(self):
        s, idx, cleaned = _extract_series_from_title("")
        assert s is None
        assert idx is None
        assert cleaned == ""


# ── Pure helpers: query variants + CJK detection ──────────────────


class TestQueryVariants:
    """v2.11.0 punct+whitespace strip tier — OL's full-text search
    is whitespace-sensitive on initials, so the resolver tries
    alternate forms when the verbatim query returns 0 hits."""

    def test_compact_initials_get_spaced_variant(self):
        # "K.D. Robertson" → also try "K. D. Robertson"
        out = _query_variants("K.D. Robertson")
        assert out[0] == "K.D. Robertson"
        assert "K. D. Robertson" in out

    def test_spaced_initials_get_compact_variant(self):
        # Inverse: "K. D. Robertson" → also try "K.D. Robertson"
        out = _query_variants("K. D. Robertson")
        assert out[0] == "K. D. Robertson"
        assert "K.D. Robertson" in out

    def test_full_name_unchanged_produces_single_variant(self):
        # No initial-pattern → only the verbatim form
        out = _query_variants("Brandon Sanderson")
        assert out == ["Brandon Sanderson"]

    def test_empty_input_safe(self):
        out = _query_variants("")
        assert out == [""]

    def test_three_consecutive_initials_handled(self):
        # "J.R.R. Tolkien" gets at least one variant form
        out = _query_variants("J.R.R. Tolkien")
        assert out[0] == "J.R.R. Tolkien"
        assert len(out) >= 2


class TestEditionRejectRegex:
    """v2.11.0 UAT exposed a precision gap: the original
    `_EDITION_REJECT_RX` only caught numbered editions ("2nd Edition")
    and missed the open-ended `"{X} Edition"` pattern. Hasekura's
    OL bibliography produced false-positive series names
    "Japanese Edition" and "Numbered Edition" because those slipped
    past the exact-list AND the numbered-only regex.

    Now matches any title ending in " edition" / " ed." / " printing"
    so the series extractor can reject them as decorations."""

    def test_numbered_edition_still_caught(self):
        # Regression: original behavior preserved
        assert _EDITION_REJECT_RX.match("2nd Edition")
        assert _EDITION_REJECT_RX.match("10th Edition")
        assert _EDITION_REJECT_RX.match("3rd Ed")
        assert _EDITION_REJECT_RX.match("1st printing")

    def test_language_edition_caught(self):
        # v2.11.0 UAT bugs — these are NOT series, they're decorations
        assert _EDITION_REJECT_RX.match("Japanese Edition")
        assert _EDITION_REJECT_RX.match("Spanish Edition")
        assert _EDITION_REJECT_RX.match("German Edition")
        assert _EDITION_REJECT_RX.match("French ed.")

    def test_other_decorator_edition_caught(self):
        assert _EDITION_REJECT_RX.match("Numbered Edition")
        assert _EDITION_REJECT_RX.match("Limited Edition")
        assert _EDITION_REJECT_RX.match("Author's Edition")

    def test_case_insensitive(self):
        assert _EDITION_REJECT_RX.match("japanese edition")
        assert _EDITION_REJECT_RX.match("JAPANESE EDITION")

    def test_non_edition_unaffected(self):
        # Real series names that happen to mention sub-words shouldn't trip
        assert not _EDITION_REJECT_RX.match("Stormlight Archive")
        assert not _EDITION_REJECT_RX.match("Spice & Wolf")
        assert not _EDITION_REJECT_RX.match("Edited Anthology")  # leading "edited" ≠ trailing "edition"


class TestExtractLanguage:
    """v2.11.0 — discovery-side language extraction. Pre-fix, every OL
    work landed with language=None, bypassing `_lang_ok` (which lets
    null through). Now we map MARC codes to ISO 639-1 so the filter
    actually fires for non-English entries."""

    def test_english_mapped(self):
        assert _extract_language({"languages": [{"key": "/languages/eng"}]}) == "en"

    def test_japanese_mapped(self):
        assert _extract_language({"languages": [{"key": "/languages/jpn"}]}) == "ja"

    def test_french_mapped_from_either_marc_variant(self):
        # MARC has both "fre" (deprecated) and "fra" (current). Both map.
        assert _extract_language({"languages": [{"key": "/languages/fre"}]}) == "fr"
        assert _extract_language({"languages": [{"key": "/languages/fra"}]}) == "fr"

    def test_bare_string_code(self):
        # Older OL responses sometimes have plain strings in the list
        assert _extract_language({"languages": ["jpn"]}) == "ja"

    def test_missing_languages_field_returns_none(self):
        assert _extract_language({"title": "anything"}) is None

    def test_empty_languages_list_returns_none(self):
        assert _extract_language({"languages": []}) is None

    def test_unknown_code_returns_none(self):
        # Don't fabricate mappings — let lookup.py treat unknown as
        # "let it through" the same as no-language path.
        assert _extract_language({"languages": [{"key": "/languages/xyz"}]}) is None

    def test_first_known_wins(self):
        # Multilingual works are rare but valid; take the first
        # mappable code so we don't get stuck on an unknown.
        assert _extract_language({
            "languages": [{"key": "/languages/xyz"}, {"key": "/languages/jpn"}],
        }) == "ja"


class TestHasCjk:
    """v2.11.0 cross-script aggregation gate."""

    def test_kanji_detected(self):
        assert _has_cjk("支倉凍砂") is True

    def test_hiragana_detected(self):
        assert _has_cjk("ひらがな") is True

    def test_katakana_detected(self):
        assert _has_cjk("カタカナ") is True

    def test_hangul_detected(self):
        assert _has_cjk("한글") is True

    def test_latin_not_detected(self):
        assert _has_cjk("Brandon Sanderson") is False

    def test_mixed_script_detected(self):
        # If ANY CJK char is present, returns True
        assert _has_cjk("Isuna 支倉") is True

    def test_empty_string_false(self):
        assert _has_cjk("") is False


# ── Phase 1: author-key resolution ────────────────────────────────


class TestResolveAuthorKeys:
    """v2.11.0: `_resolve_author_key` (single) → `_resolve_author_keys`
    (list). Most authors still resolve to one key; multi-key returns
    cover the cross-script + duplicate-record cases."""

    async def test_single_match_returned_as_singleton_list(self):
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [{"key": "/authors/OL38550A", "name": "Brandon Sanderson", "work_count": 142}],
            },
        })

        result = await src._resolve_author_keys("Brandon Sanderson")

        assert result == ["/authors/OL38550A"]
        await src.close()

    async def test_strict_name_match_wins_over_partial(self):
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [
                    {"key": "/authors/OL1A", "name": "Brandon Sanderson, Jr.", "work_count": 5},
                    {"key": "/authors/OL2A", "name": "Brandon Sanderson", "work_count": 142},
                    {"key": "/authors/OL3A", "name": "Brandon Sandersonish", "work_count": 100},
                ],
            },
        })

        result = await src._resolve_author_keys("Brandon Sanderson")

        # OL2A is the only strict-name match — partial matches don't
        # aggregate in absence of cross-script evidence.
        assert result == ["/authors/OL2A"]
        await src.close()

    async def test_multiple_strict_matches_aggregate_ordered_by_work_count(self):
        # Case variants of the same name all strictly normalize to the
        # same target — all should aggregate into the result list,
        # most-prolific first.
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [
                    {"key": "/authors/OL10A", "name": "ISUNA HASEKURA", "work_count": 3},
                    {"key": "/authors/OL20A", "name": "Isuna Hasekura", "work_count": 5},
                    {"key": "/authors/OL30A", "name": "Isuna HASEKURA", "work_count": 1},
                ],
            },
        })

        result = await src._resolve_author_keys("Isuna Hasekura")

        # All 3 case-variants of the same name aggregate, sorted by work_count
        assert result == ["/authors/OL20A", "/authors/OL10A", "/authors/OL30A"]
        await src.close()

    async def test_no_strict_match_falls_back_to_top_hit(self):
        # No name passes strict gate AND no substring match either.
        # Fall back to OL's top-ranked hit (single-element list).
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [
                    {"key": "/authors/OL99A", "name": "Wildly Different Person", "work_count": 50},
                    {"key": "/authors/OL98A", "name": "Also Different Author", "work_count": 1},
                ],
            },
        })

        result = await src._resolve_author_keys("Brandon Sanderson")

        assert result == ["/authors/OL99A"]
        await src.close()

    async def test_no_results_returns_empty_list(self):
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {"docs": []},
        })

        result = await src._resolve_author_keys("Nonexistent Author")

        assert result == []
        await src.close()

    async def test_period_normalization_still_matches(self):
        # "J.N. Chaney" (no space) should match "J. N. Chaney"
        # (space-separated) via the strict normalization rule.
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [{"key": "/authors/OL777A", "name": "J.N. Chaney", "work_count": 398}],
            },
        })

        result = await src._resolve_author_keys("J. N. Chaney")

        assert result == ["/authors/OL777A"]
        await src.close()

    async def test_variant_query_recovers_when_verbatim_misses(self):
        # The K.D. Robertson case: OL's full-text search returns 0
        # for the compact-initials form, 2 hits for spaced form.
        # The resolver tries variants when verbatim is empty.
        src = _make_source()
        call_log: list = []
        responses_in_order = [
            {"docs": []},  # first call: verbatim "K.D. Robertson"
            {"docs": [    # second call: variant "K. D. Robertson"
                {"key": "/authors/OL11162910A", "name": "K. D. Robertson", "work_count": 6},
            ]},
        ]
        _patch_get(src, {
            "search/authors.json": responses_in_order,
        }, call_log=call_log)

        result = await src._resolve_author_keys("K.D. Robertson")

        assert result == ["/authors/OL11162910A"]
        # Two HTTP calls: verbatim missed, variant recovered
        assert len(call_log) == 2
        await src.close()

    async def test_variant_query_no_retry_when_verbatim_hits(self):
        # If the verbatim query returns hits, NO variant retry happens.
        # Avoids burning RTTs for the common case.
        src = _make_source()
        call_log: list = []
        _patch_get(src, {
            "search/authors.json": {
                "docs": [{"key": "/authors/OL1A", "name": "Brandon Sanderson", "work_count": 142}],
            },
        }, call_log=call_log)

        await src._resolve_author_keys("Brandon Sanderson")

        assert len(call_log) == 1  # no variant retry triggered
        await src.close()

    async def test_cross_script_aggregation_includes_dominant_cjk(self):
        # The Hasekura case: OL returns several Latin-script "Isuna
        # Hasekura" records (5, 3, 3 works) plus a high-work-count
        # CJK record "支倉凍砂" (79 works). The CJK record dominates
        # work count → aggregate into the result.
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [
                    {"key": "/authors/OL6791851A", "name": "Isuna Hasekura", "work_count": 5},
                    {"key": "/authors/OL12608376A", "name": "ISUNA HASEKURA", "work_count": 3},
                    {"key": "/authors/OL15006681A", "name": "Isuna Hasekura", "work_count": 3},
                    {"key": "/authors/OL6811405A", "name": "支倉凍砂", "work_count": 79},
                    {"key": "/authors/OL12638968A", "name": "Hasekura Isuna", "work_count": 1},
                ],
            },
        })

        result = await src._resolve_author_keys("Isuna Hasekura")

        # All 3 strict matches + the CJK dominator
        assert "/authors/OL6791851A" in result  # 5 works, primary strict
        assert "/authors/OL12608376A" in result  # 3 works, strict
        assert "/authors/OL15006681A" in result  # 3 works, strict
        assert "/authors/OL6811405A" in result   # CJK dominator
        # The "Hasekura Isuna" reverse-order record does NOT strictly
        # normalize (h-i vs i-h), and it has low work_count, so excluded.
        assert "/authors/OL12638968A" not in result
        # Primary key (first in list) should be the most-prolific
        # STRICT match (5 works), not the CJK record (79). The CJK
        # record is appended after the strict aggregation pool.
        assert result[0] == "/authors/OL6791851A"
        await src.close()

    async def test_cross_script_excluded_when_work_count_lower(self):
        # A CJK record with FEWER works than strict matches should NOT
        # aggregate — it's likely a different person sharing a script.
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [
                    {"key": "/authors/OL1A", "name": "Some Author", "work_count": 50},
                    {"key": "/authors/OL2A", "name": "某作家", "work_count": 3},  # tiny CJK ghost
                ],
            },
        })

        result = await src._resolve_author_keys("Some Author")

        assert result == ["/authors/OL1A"]
        await src.close()

    async def test_latin_script_high_work_count_not_aggregated(self):
        # Cross-script aggregation is CJK-only. A high-work-count Latin
        # record that doesn't pass strict-name match must NOT be pulled
        # in — that's just "a different prolific author with overlap-y
        # name", a precision risk we explicitly reject.
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [
                    {"key": "/authors/OL1A", "name": "John Smith", "work_count": 5},
                    {"key": "/authors/OL99A", "name": "John Q. Robinson", "work_count": 500},
                ],
            },
        })

        result = await src._resolve_author_keys("John Smith")

        # Strict match only — the unrelated Robinson is excluded
        # even though work_count > strict-max.
        assert result == ["/authors/OL1A"]
        await src.close()


# ── Phase 2: paginated works walk ─────────────────────────────────


class TestFetchAllAuthorWorks:
    async def test_single_page_under_limit(self):
        src = _make_source()
        call_log: list = []
        _patch_get(src, {
            "/authors/OL38550A/works.json": {
                "entries": [{"key": f"/works/OL{i}W", "title": f"Book {i}"} for i in range(10)],
            },
        }, call_log=call_log)

        works = await src._fetch_all_author_works("/authors/OL38550A")

        assert len(works) == 10
        assert len(call_log) == 1  # no pagination
        assert works[0]["title"] == "Book 0"
        await src.close()

    async def test_paginates_through_full_pages(self):
        # 100 + 100 + 23 = 223 total. Caller should make 3 round-trips
        # and stop when the third page returns < limit rows.
        src = _make_source()
        responses = [
            {"entries": [{"key": f"/works/OL{i}W", "title": f"P1-{i}"} for i in range(100)]},
            {"entries": [{"key": f"/works/OL{100+i}W", "title": f"P2-{i}"} for i in range(100)]},
            {"entries": [{"key": f"/works/OL{200+i}W", "title": f"P3-{i}"} for i in range(23)]},
        ]
        call_log: list = []
        _patch_get(src, {
            "/authors/OL38550A/works.json": responses,
        }, call_log=call_log)

        works = await src._fetch_all_author_works("/authors/OL38550A")

        assert len(works) == 223
        assert len(call_log) == 3
        offsets = [params.get("offset") for _, params in call_log]
        assert offsets == [0, 100, 200]
        await src.close()

    async def test_bare_key_works_too(self):
        # `_fetch_all_author_works` should accept either "/authors/OL38550A"
        # or just "OL38550A" — it strips the prefix internally.
        src = _make_source()
        call_log: list = []
        _patch_get(src, {
            "/authors/OL38550A/works.json": {"entries": []},
        }, call_log=call_log)

        await src._fetch_all_author_works("OL38550A")

        assert len(call_log) == 1
        await src.close()

    async def test_no_entries_returns_empty(self):
        src = _make_source()
        _patch_get(src, {
            "/authors/OL38550A/works.json": {"entries": []},
        })

        works = await src._fetch_all_author_works("/authors/OL38550A")

        assert works == []
        await src.close()


# ── End-to-end search_author ──────────────────────────────────────


class TestSearchAuthorEndToEnd:
    async def test_assembles_books_and_series(self):
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [{"key": "/authors/OL38550A", "name": "Brandon Sanderson", "work_count": 142}],
            },
            "/authors/OL38550A/works.json": {
                "entries": [
                    {
                        "key": "/works/OL15161W",
                        "title": "The Way of Kings (The Stormlight Archive, #1)",
                        "covers": [12345],
                        "description": "Roshar epic.",
                        "first_publish_date": "2010-08-31",
                    },
                    {
                        "key": "/works/OL26321W",
                        "title": "Words of Radiance (The Stormlight Archive, #2)",
                        "covers": [67890],
                        "description": {"type": "/type/text", "value": "Continuation."},
                        "first_publish_date": "2014-03-04",
                    },
                    {
                        "key": "/works/OL68428W",
                        "title": "Elantris",
                        "covers": [],
                        "first_publish_date": "2005-04-21",
                    },
                ],
            },
        })

        result = await src.search_author("Brandon Sanderson")

        assert result is not None
        assert result.external_id == "/authors/OL38550A"
        assert len(result.books) == 1  # Elantris (standalone)
        assert len(result.series) == 1  # Stormlight Archive
        sa = result.series[0]
        assert sa.name == "The Stormlight Archive"
        assert len(sa.books) == 2
        assert sa.books[0].title == "The Way of Kings"
        assert sa.books[0].series_index == 1.0
        assert sa.books[0].cover_url == "https://covers.openlibrary.org/b/id/12345-L.jpg"
        assert sa.books[1].title == "Words of Radiance"
        assert sa.books[1].description == "Continuation."
        assert result.books[0].title == "Elantris"
        assert result.books[0].cover_url is None  # no covers
        await src.close()

    async def test_no_author_match_returns_none(self):
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {"docs": []},
        })

        result = await src.search_author("Nonexistent Author")

        assert result is None
        await src.close()

    async def test_author_found_but_zero_works_returns_minimal_result(self):
        # OL has the author record but their works list is empty.
        # Return a populated AuthorResult anyway so downstream knows
        # the author was resolved (just no books to merge).
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [{"key": "/authors/OLNEWA", "name": "New Author", "work_count": 0}],
            },
            "/authors/OLNEWA/works.json": {"entries": []},
        })

        result = await src.search_author("New Author")

        assert result is not None
        assert result.external_id == "/authors/OLNEWA"
        assert result.books == []
        assert result.series == []
        await src.close()

    async def test_get_author_books_is_no_op(self):
        # Mirrors HardcoverSource: search_author already returns
        # the full result, so get_author_books returns None and the
        # caller's two-phase flow collapses to phase 1.
        src = _make_source()
        result = await src.get_author_books("/authors/OL38550A")
        assert result is None
        await src.close()

    async def test_multi_record_aggregation_dedups_by_work_key(self):
        # End-to-end exercise of v2.11.0 cross-record aggregation:
        # OL returns multiple author records (e.g. case variants +
        # CJK dominant). Each has its own works list; the resolver
        # walks them all and dedups by work-key. Verifies primary
        # `external_id` is the first strict match (not the CJK key)
        # and that a duplicate work appearing under two records
        # surfaces only once in the final AuthorResult.
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [
                    {"key": "/authors/OLLatA", "name": "Isuna Hasekura", "work_count": 5},
                    {"key": "/authors/OLCjkA", "name": "支倉凍砂", "work_count": 79},
                ],
            },
            "/authors/OLLatA/works.json": {
                "entries": [
                    {"key": "/works/OLSharedW", "title": "Spice and Wolf 1"},
                    {"key": "/works/OLLatOnlyW", "title": "English Anthology"},
                ],
            },
            "/authors/OLCjkA/works.json": {
                "entries": [
                    # Same work appears under both records — dedup must kick in
                    {"key": "/works/OLSharedW", "title": "狼と香辛料 1"},
                    {"key": "/works/OLCjkOnlyW", "title": "狼と羊皮紙"},
                    {"key": "/works/OLCjkOnly2W", "title": "Wolf & Parchment 2"},
                ],
            },
        })

        result = await src.search_author("Isuna Hasekura")

        assert result is not None
        # Primary external_id is the FIRST strict-match record, not the
        # CJK record (which got appended via cross-script aggregation).
        assert result.external_id == "/authors/OLLatA"
        # 4 unique works total — the shared one dedups
        all_titles = [b.title for b in result.books] + [
            b.title for s in result.series for b in s.books
        ]
        # Whichever variant of the shared work landed first (Latin record
        # walked first), the other should NOT also appear
        assert len(all_titles) == 4, f"expected 4 unique works, got {all_titles}"
        # All 3 unique-to-CJK or unique-to-Latin works present
        assert any("Spice and Wolf 1" == t or "狼と香辛料 1" == t for t in all_titles)
        assert "English Anthology" in all_titles
        assert "狼と羊皮紙" in all_titles
        assert "Wolf & Parchment 2" in all_titles
        await src.close()

    async def test_language_extraction_lands_on_book_result(self):
        # v2.11.0 — OL works.json carries a `languages` field. Pre-fix
        # we ignored it, so every OL book arrived with language=None
        # which `_lang_ok` lets through. Now Japanese / French / etc.
        # works land with the right ISO 639-1 code so the user's
        # language filter actually fires.
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [{"key": "/authors/OL1A", "name": "Test Author", "work_count": 3}],
            },
            "/authors/OL1A/works.json": {
                "entries": [
                    {"key": "/works/OL1W", "title": "English Book",
                     "languages": [{"key": "/languages/eng"}]},
                    {"key": "/works/OL2W", "title": "Japanese Book",
                     "languages": [{"key": "/languages/jpn"}]},
                    {"key": "/works/OL3W", "title": "Language-Unknown Book"},
                ],
            },
        })

        result = await src.search_author("Test Author")
        assert result is not None
        books = result.books
        by_title = {b.title: b for b in books}

        assert by_title["English Book"].language == "en"
        assert by_title["Japanese Book"].language == "ja"
        # No `languages` field → None (lookup.py will treat as
        # "unknown, assume ok", same as pre-v2.11.0 behavior)
        assert by_title["Language-Unknown Book"].language is None
        await src.close()

    async def test_japanese_edition_decoration_not_treated_as_series(self):
        # v2.11.0 UAT — Hasekura's OL bibliography included works with
        # "(Japanese Edition)" parentheticals that the original
        # `_EDITION_REJECT_RX` missed. The series_map ended up with
        # garbage entries like a series literally named "Japanese
        # Edition" holding multiple unrelated books.
        #
        # Post-fix: not only is series rejected, but the decoration
        # parenthetical is STRIPPED from the title so the noise
        # doesn't pollute review-queue display or dedup.
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [{"key": "/authors/OL1A", "name": "Test Author", "work_count": 5}],
            },
            "/authors/OL1A/works.json": {
                "entries": [
                    {"key": "/works/OL1W", "title": "Some Book (Japanese Edition)"},
                    {"key": "/works/OL2W", "title": "Another Title (Numbered Edition)"},
                    {"key": "/works/OL3W", "title": "Third Book (Limited Edition)"},
                ],
            },
        })

        result = await src.search_author("Test Author")
        assert result is not None
        # All three should be standalone (no series entries)
        assert len(result.series) == 0, (
            f"Edition decorations leaked into series_map: "
            f"{[s.name for s in result.series]}"
        )
        assert len(result.books) == 3
        titles = sorted(b.title for b in result.books)
        # v2.11.0: decoration parenthetical stripped from title too.
        # Pre-fix titles ended in "(Japanese Edition)" etc.
        assert titles == ["Another Title", "Some Book", "Third Book"]
        for b in result.books:
            assert b.series_name is None

    async def test_format_specific_parenthetical_not_treated_as_series(self):
        # "(Annotated)", "(2nd Edition)", "(Illustrated)", "(Boxed Set)"
        # are common OL title decorations and must NOT become series_name.
        # Both the exact-name reject list AND the "Nth Edition" regex
        # have to fire correctly.
        #
        # v2.11.0: also assert that the decoration parenthetical is
        # stripped from the cleaned title so review queue / dedup
        # don't see the noise. Pre-v2.11.0 the title retained the
        # decoration; post-fix the title is the cleaned base.
        src = _make_source()
        _patch_get(src, {
            "search/authors.json": {
                "docs": [{"key": "/authors/OL1A", "name": "Some Author", "work_count": 5}],
            },
            "/authors/OL1A/works.json": {
                "entries": [
                    {"key": "/works/OL1W", "title": "Some Book (Annotated)"},
                    {"key": "/works/OL2W", "title": "Some Book (2nd Edition)"},
                    {"key": "/works/OL3W", "title": "Some Book (Illustrated)"},
                    {"key": "/works/OL4W", "title": "Some Book (10th Edition)"},
                    {"key": "/works/OL5W", "title": "Some Book (Boxed Set)"},
                ],
            },
        })

        result = await src.search_author("Some Author")

        assert result is not None
        assert len(result.series) == 0, (
            f"format/edition decorations leaked into series_map: "
            f"{[s.name for s in result.series]}"
        )
        assert len(result.books) == 5
        for b in result.books:
            assert b.series_name is None
            # Cleaned title — decoration parenthetical stripped
            assert b.title == "Some Book", (
                f"expected stripped 'Some Book', got {b.title!r}"
            )
        await src.close()
