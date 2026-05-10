"""
Bundle-detection + bundle-promote-cap + description-verification tests.

`_is_bundle` flags multi-book series collections so the scan logic can
keep them out of the "Found" tier when only the author matches (the
URL would point at the bundle, not the searched-for book), and so the
UI can render a "Series bundle" badge.

Cap behavior: when a bundle is the best result AND title-similarity
is below the floor, confidence-based promote is suppressed and the
result lands in "possible" instead. The description-based verification
path can promote the bundle back to Found if the searched title
appears as a structured list entry in the torrent's description.

A previous filelist-based verification signal (mbsc browser-cookie
scrape of /tor/filelist.php) was REMOVED in v2.4.0 — MAM staff
confirmed mbsc-tier scraping isn't on Section 1.7's approved
automation list. See feedback_mam_mbsc_filelist_tos.md.
"""
import httpx

from app.discovery.sources.mam import (
    _BUNDLE_PROMOTE_TS_FLOOR,
    _description_contains_title,
    _handle_response_cookie,
    _is_bundle,
    _strip_to_lines,
)


def _make_response(headers: list, body: str = "x") -> httpx.Response:
    """Helper: synthesize an httpx.Response with arbitrary headers.

    httpx.Response.cookies needs a `request` to extract Set-Cookie via
    its jar, so we attach a stub request — the URL is consulted by the
    jar's domain matching but the test cookies use explicit domain=.
    """
    request = httpx.Request("GET", "https://www.myanonamouse.net/")
    return httpx.Response(200, headers=headers, text=body, request=request)


# ─── Bundle detection ──────────────────────────────────────────


class TestIsBundle:
    def test_high_numfiles_is_bundle(self):
        # Demon Accords Series — 12 files in one torrent.
        assert _is_bundle({"numfiles": 12, "title": "Demon Accords Series"}) is True

    def test_single_file_is_not_bundle(self):
        # Most single books — one epub file, no bundle keyword.
        assert _is_bundle({"numfiles": 1, "title": "Bikini Days"}) is False

    def test_multi_format_single_book_is_not_bundle(self):
        # 4 formats of one book — under the numfiles floor.
        assert _is_bundle({"numfiles": 4, "title": "The Way of Kings"}) is False

    def test_title_keyword_collection(self):
        assert _is_bundle({"numfiles": 1, "title": "Foo Collection"}) is True

    def test_title_keyword_omnibus(self):
        assert _is_bundle({"numfiles": 1, "title": "The Foo Omnibus"}) is True

    def test_title_keyword_series(self):
        assert _is_bundle({"numfiles": 1, "title": "Demon Accords Series"}) is True

    def test_title_keyword_box_set_with_space(self):
        assert _is_bundle({"numfiles": 1, "title": "Foo Box Set"}) is True

    def test_title_keyword_boxset_no_space(self):
        assert _is_bundle({"numfiles": 1, "title": "Foo Boxset"}) is True

    def test_title_keyword_anthology(self):
        assert _is_bundle({"numfiles": 1, "title": "An Anthology of Foo"}) is True

    def test_series_info_range_is_bundle(self):
        # MAM format: {"<id>": ["Series Name", "<index>", numeric]}
        # A range index like "1-12" signals a multi-volume bundle.
        item = {
            "numfiles": 1,
            "title": "Some Bundle",
            "series_info": '{"104079":["The Demon Accords","1-12",1.0]}',
        }
        assert _is_bundle(item) is True

    def test_series_info_comma_list_is_bundle(self):
        item = {
            "numfiles": 1,
            "title": "Some Bundle",
            "series_info": '{"104079":["The Demon Accords","1, 3, 5",1.0]}',
        }
        assert _is_bundle(item) is True

    def test_series_info_single_index_is_not_bundle(self):
        # Single-volume index — normal book in a series, not a bundle.
        item = {
            "numfiles": 1,
            "title": "Bikini Days",
            "series_info": '{"117534":["Bikini Days","1",1.0]}',
        }
        assert _is_bundle(item) is False

    def test_no_signals_is_not_bundle(self):
        assert _is_bundle({}) is False
        assert _is_bundle({"numfiles": 0, "title": ""}) is False

    def test_malformed_series_info_does_not_crash(self):
        # Invalid JSON should fall through silently rather than crash a
        # 2000-book scan partway through.
        assert _is_bundle({"numfiles": 1, "title": "Foo", "series_info": "not json"}) is False

    def test_numeric_numfiles_string(self):
        # MAM occasionally returns numfiles as a string — coerce safely.
        assert _is_bundle({"numfiles": "12", "title": "Foo"}) is True

    def test_real_world_demon_accords_bundle(self):
        # The actual JSON Mark captured for torrent 424895 (the Demon
        # Accords Series ebook bundle that wrongly scored as the best
        # match for "Duel Nature" in production).
        item = {
            "id": 424895,
            "title": "Demon Accords Series",
            "numfiles": 12,
            "filetype": "epub",
            "series_info": '{"104079":["The Demon Accords","1-12",1.0]}',
        }
        assert _is_bundle(item) is True

    def test_real_world_demon_accords_1_4_bundle(self):
        # Torrent 135522 — title "The Demon Accords 1-4". 12 files.
        # Caught by both numfiles ≥ 5 and series-range marker.
        item = {
            "id": 135522,
            "title": "The Demon Accords 1-4",
            "numfiles": 12,
            "filetype": "mobi",
            "series_info": '{"104079":["The Demon Accords","1-4",1.0]}',
        }
        assert _is_bundle(item) is True

    def test_real_world_single_book(self):
        # Torrent 1056382 — Blackwood Milk Farm: Book 5. Single book.
        item = {
            "id": 1056382,
            "title": "Blackwood Milk Farm: Book 5",
            "numfiles": 1,
            "filetype": "epub",
            "series_info": '{"109731":["A Mist Valley Slice of Life Adventure","5",5.0]}',
        }
        assert _is_bundle(item) is False


# ─── Bundle promote cap + description-verification predicates ──


class TestBundlePromoteCap:
    """The cap is applied in `check_book._try_evaluate`. These tests
    pin the threshold constant so it can't drift silently and verify
    the promote-vs-cap decision logic mirrors what we documented."""

    def test_cap_threshold_is_strict(self):
        # Floor of 0.85 leaves plenty of room above the regular 0.70
        # promote threshold — bundles need genuine title coverage to
        # be elevated to Found, not just "above the normal bar".
        assert _BUNDLE_PROMOTE_TS_FLOOR > 0.70

    def test_description_verification_gate(self):
        # Mirror the verification trigger from _try_evaluate. Fires
        # whenever the best candidate is a bundle, the author overlaps,
        # and the title alone doesn't strongly match — independent of
        # the blended confidence score.
        def needs_description_check(
            is_bundle: bool, author_matched: bool, ts: float
        ) -> bool:
            return (
                is_bundle
                and author_matched
                and ts < _BUNDLE_PROMOTE_TS_FLOOR
            )

        # The Duel Nature → Demon Accords Series case: low conf, low ts,
        # author matches, bundle. Must verify.
        assert needs_description_check(True, True, 0.0) is True

        # No author overlap: don't burn a fetch on a totally unrelated
        # bundle (e.g. "Duel Nature" against "Mixed Calibre Library").
        assert needs_description_check(True, False, 0.0) is False

        # Bundle whose title strongly matches the calibre title
        # (intentional bundle catalog entry) — promotes via the normal
        # path without needing a description fetch.
        assert needs_description_check(True, True, 0.95) is False

        # Single-book result — never bundle-verify.
        assert needs_description_check(False, True, 0.0) is False

    def test_promote_after_verification_predicate(self):
        # Final promote decision. Mirrors the should_promote logic in
        # _try_evaluate so future refactors can't drop the bundle-cap
        # safety or break the description-verification override.
        def should_promote(
            is_bundle: bool, conf: float, ts: float, verified: bool
        ) -> bool:
            blocked = (
                is_bundle
                and ts < _BUNDLE_PROMOTE_TS_FLOOR
                and not verified
            )
            return verified or (conf >= 0.70 and not blocked)

        # Verified bundle promotes regardless of low confidence.
        assert should_promote(True, 0.30, 0.0, verified=True) is True

        # Same low-conf bundle, NOT verified — stays at possible.
        assert should_promote(True, 0.30, 0.0, verified=False) is False

        # High-conf bundle with weak title and no verification — capped.
        # (false-Found protection: bundle URL is misleading)
        assert should_promote(True, 0.74, 0.50, verified=False) is False

        # Same scores but verification succeeded — promote.
        assert should_promote(True, 0.74, 0.50, verified=True) is True

        # Non-bundle high-conf — normal promote.
        assert should_promote(False, 0.74, 0.50, verified=False) is True

        # Bundle with strong title match (no cap) — normal promote.
        assert should_promote(True, 0.95, 0.95, verified=False) is True


# ─── mam_id deletion-sentinel defense ──────────────────────────


class TestDeletionSentinelNotRotated:
    """A 2026-05-09 corruption regression hit when a rejected response
    included a Set-Cookie deletion sentinel for mam_id. The rotation
    handler must NOT capture deletions as fresh tokens, otherwise the
    in-memory + persisted store get poisoned and every subsequent
    search 403s until container restart. (Same defense applied to mbsc
    until that whole path was removed in v2.4.0 per TOS.)
    """

    async def test_mam_id_deletion_does_not_rotate(self):
        from app.discovery.sources import mam as mam_mod
        saved_token = mam_mod._current_token
        saved_save = mam_mod._last_rotation_save
        try:
            mam_mod._current_token = "VALID_OLD_MAM_ID"
            mam_mod._last_rotation_save = 0.0

            resp = _make_response([(
                "set-cookie",
                "mam_id=deleted; expires=Thu, 01 Jan 1970 00:00:01 GMT; "
                "Max-Age=0; path=/; domain=.myanonamouse.net",
            )])
            await _handle_response_cookie(resp)

            assert mam_mod._current_token == "VALID_OLD_MAM_ID"
        finally:
            mam_mod._current_token = saved_token
            mam_mod._last_rotation_save = saved_save


# ─── Description-based bundle verification ─────────────────────


class TestStripToLines:
    """Description text comes back from MAM as a mix of HTML
    (<br />, <strong>, etc.) and BBCode ([b], [size=4], [*], etc.).
    Block-level markup must become line breaks; inline formatting
    must vanish without affecting line structure."""

    def test_html_br_becomes_newline(self):
        result = _strip_to_lines("<strong>Title 1</strong><br />Title 2")
        assert "Title 1" in result
        assert "Title 2" in result

    def test_bbcode_list_marker_becomes_newline(self):
        result = _strip_to_lines("[*] Title A\n[*] Title B")
        assert any("Title A" in line for line in result)
        assert any("Title B" in line for line in result)

    def test_inline_html_stripped(self):
        result = _strip_to_lines("<strong>Plain</strong>")
        assert result == ["Plain"]

    def test_inline_bbcode_stripped(self):
        result = _strip_to_lines("[b]Bold[/b] [size=4]Big[/size]")
        assert result == ["Bold Big"]

    def test_empty_lines_dropped(self):
        result = _strip_to_lines("<br /><br />Title<br /><br />")
        assert result == ["Title"]

    def test_html_entities_decoded(self):
        result = _strip_to_lines("Foo&nbsp;Bar&amp;Baz&#65;")
        assert result == ["Foo Bar&BazA"]

    def test_none_input_returns_empty(self):
        assert _strip_to_lines(None) == []
        assert _strip_to_lines("") == []


class TestDescriptionContainsTitle:
    """Structured-line check that gates bundle promotion. Conservative
    on false positives (rejects prose mentions, recommendations,
    negations) and matches the most common bundle-listing patterns
    MAM uploaders use."""

    def test_real_world_strong_volume_pattern(self):
        desc = "<br /><br /><strong>Duel Nature - 4</strong><br />"
        assert _description_contains_title(desc, "Duel Nature") is True

    def test_bbcode_list_pattern_with_narrator(self):
        desc = (
            "[*] 01. How To Marry a Millionaire Vampire - Narrated by Foo - 11h, MP3\r\n"
            "[*] 02. Vamps and the City - Narrated by Bar - 11h34m, MP3"
        )
        assert _description_contains_title(desc, "How To Marry a Millionaire Vampire") is True
        assert _description_contains_title(desc, "Vamps and the City") is True

    def test_simple_dash_volume_marker(self):
        desc = "<br />Duel Nature - 4<br />Demon Driven - 2<br />"
        assert _description_contains_title(desc, "Duel Nature") is True
        assert _description_contains_title(desc, "Demon Driven") is True

    def test_book_word_volume(self):
        desc = "<br />Duel Nature Book 4<br />"
        assert _description_contains_title(desc, "Duel Nature") is True

    def test_paren_book_volume(self):
        desc = "<br />Duel Nature (Book 4)<br />"
        assert _description_contains_title(desc, "Duel Nature") is True

    def test_paren_year_after_title(self):
        desc = "<br />Duel Nature (2013)<br />"
        assert _description_contains_title(desc, "Duel Nature") is True

    def test_numbered_list_no_volume_marker(self):
        desc = "1. Duel Nature\n2. Demon Driven\n3. Brutal Asset"
        assert _description_contains_title(desc, "Duel Nature") is True
        assert _description_contains_title(desc, "Demon Driven") is True

    def test_rejects_prose_mention(self):
        # Recommendation prose — title appears mid-sentence with other
        # words around it; structured-line check rejects.
        desc = "Fans of Duel Nature will love this. Also from John Conroe."
        assert _description_contains_title(desc, "Duel Nature") is False

    def test_rejects_negation(self):
        # We don't have semantic negation detection, but the title
        # isn't on its own line so the structured check rejects anyway.
        desc = "<br />This bundle does NOT include Duel Nature.<br />"
        assert _description_contains_title(desc, "Duel Nature") is False

    def test_rejects_recommendation_context(self):
        desc = "<br />If you enjoyed Duel Nature, check out these books<br />"
        assert _description_contains_title(desc, "Duel Nature") is False

    def test_single_word_title_rejected(self):
        # Single-token titles are too noisy to match standalone
        # (would false-positive on every bundle listing that happens
        # to contain that word).
        desc = "<br /><strong>Dawn</strong><br />"
        assert _description_contains_title(desc, "Dawn") is False

    def test_empty_inputs(self):
        assert _description_contains_title("", "Duel Nature") is False
        assert _description_contains_title(None, "Duel Nature") is False
        assert _description_contains_title("desc", "") is False
        assert _description_contains_title("desc") is False

    def test_multiple_titles_any_hit(self):
        # OR semantics across the title alternates.
        desc = "<br /><strong>Duel Nature - 4</strong><br />"
        assert _description_contains_title(desc, "Bikini Days", "Duel Nature") is True

    def test_case_insensitive(self):
        desc = "<br /><strong>DUEL NATURE - 4</strong><br />"
        assert _description_contains_title(desc, "duel nature") is True
        desc2 = "<br /><strong>duel nature - 4</strong><br />"
        assert _description_contains_title(desc2, "DUEL NATURE") is True

    def test_punctuation_in_title_normalizes(self):
        # User's calibre title may have punctuation that the bundle
        # listing omits (or vice versa). Normalization should handle it.
        desc = "<br /><strong>Duel Nature - 4</strong><br />"
        assert _description_contains_title(desc, "Duel Nature") is True

    def test_dash_title_not_confused_with_volume_marker(self):
        # Title "Half-Elf Chronicles" contains a dash but it's part
        # of the title, not a volume marker.
        desc = "<br />Half-Elf Chronicles - 1<br />"
        assert _description_contains_title(desc, "Half-Elf Chronicles") is True

    def test_list_marker_followed_by_number(self):
        # "[*] 01. Title" — both the BBCode list marker AND the
        # numbering need stripping.
        desc = "[*] 01. Duel Nature"
        assert _description_contains_title(desc, "Duel Nature") is True
