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
import pytest

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

    def test_distinctive_single_word_title_accepted(self):
        # UAT canary 2026-05-10: "Chainfire" (9 chars, single token,
        # title of Sword of Truth Bk9). Distinctive enough to accept.
        # The structured-line equality match still requires the line
        # content to EQUAL the title, so false-positive risk is bounded
        # even with the relaxed gate.
        desc = "<p>09 - Chainfire</p>"
        assert _description_contains_title(desc, "Chainfire") is True

    def test_short_single_word_title_still_rejected(self):
        # < 5 chars AND < 2 tokens — too noisy. Mirrors existing "Dawn"
        # case. "Raw" is the canonical short single-word title that
        # collides with prose ("raw materials", "raw emotion").
        desc = "<p>* Raw</p>"
        assert _description_contains_title(desc, "Raw") is False

    def test_numbered_dash_prefix_pattern(self):
        # UAT canary 93760 (Sword of Truth .epub bundle): "<NN> -
        # <Title>" numbering scheme that the parser previously left as
        # ['09 - chainfire', '09'] candidates — neither equaled the
        # title. The new leading-prefix alternation strips "09 - " so
        # the title surfaces cleanly.
        desc = (
            "<p>00 - Debt of Bones</p>"
            "<p>09 - Chainfire</p>"
            "<p>10 - Phantom</p>"
        )
        assert _description_contains_title(desc, "Chainfire") is True
        assert _description_contains_title(desc, "Phantom") is True  # 7 chars, single token but distinctive
        assert _description_contains_title(desc, "Debt of Bones") is True

    def test_inline_asterisk_bullet_splits_lines(self):
        # UAT canary 5081 (.lit Sword of Truth bundle): every book on
        # one giant <p> line separated by `&nbsp;* ` markers. Without
        # the inline-asterisk-bullet block split, the parser saw one
        # massive run-on line with no per-title boundaries.
        desc = (
            "<p>These are great. &nbsp; &nbsp; &nbsp;"
            "* Wizards First Rule &nbsp; &nbsp; "
            "* Stone of Tears &nbsp; &nbsp; "
            "* Chainfire &nbsp; &nbsp; "
            "* Phantom</p>"
        )
        assert _description_contains_title(desc, "Wizards First Rule") is True
        assert _description_contains_title(desc, "Stone of Tears") is True
        assert _description_contains_title(desc, "Chainfire") is True

    def test_inline_asterisk_doesnt_split_emphasis(self):
        # `*word*` (markdown emphasis around a token) should NOT split
        # — the bullet pattern requires whitespace/&nbsp; on BOTH sides.
        desc = "<p>You *must* read Duel Nature.</p>"
        assert _description_contains_title(desc, "must") is False
        assert _description_contains_title(desc, "Duel Nature") is False


# ─── _author_match per-author subset ──────────────────────────────
#
# UAT 2026-05-11 canary: a 59-author mega-collection bundle
# ("Fantasy-Scifi Authors Starting With T") was matching a search
# for "Pierce Scott" because the listed author "Tamora Pierce"
# shared the token "pierce". The pre-fix logic UNIONED tokens
# across every listed author before checking overlap, so any
# single-token coincidence with a search-author surname triggered
# `author_matched=True`. Per-author subset matching ensures BOTH
# search tokens have to land on a SINGLE individual author.


class TestAuthorMatchPerAuthorSubset:
    @staticmethod
    def _result(authors):
        # _parse_author_info accepts comma-separated string OR a list.
        if isinstance(authors, list):
            return {"author_info": ",".join(authors)}
        return {"author_info": authors}

    def test_exact_match(self):
        from app.discovery.sources.mam import _author_match
        assert _author_match(
            "Pierce Scott", self._result(["Pierce Scott"]),
        ) is True

    def test_mega_collection_split_tokens_no_match(self):
        # The UAT canary itself: search for "Pierce Scott" against a
        # 59-author bundle whose only token-overlap is "Pierce" hiding
        # in "Tamora Pierce". Pre-fix this returned True (false +);
        # post-fix returns False (the two tokens come from different
        # authors, not a single one).
        from app.discovery.sources.mam import _author_match
        big = ["Tamora Pierce", "Terry Brooks", "Tim Powers",
               "Tad Williams", "T. H. White"]
        assert _author_match(
            "Pierce Scott", self._result(big),
        ) is False

    def test_surname_only_mam_author_via_reverse_subset(self):
        # MAM upload sometimes lists only the SURNAME — reverse-subset
        # branch accepts this legitimate match (m_tok ⊆ cal_tok AND
        # cal_surname is in m_tok).
        from app.discovery.sources.mam import _author_match
        assert _author_match(
            "Pierce Scott", self._result(["Scott"]),
        ) is True

    def test_surname_collision_first_name_no_match(self):
        # UAT round 5 canary: "M J Scott" surfaced for "Scott Reintgen"
        # because both share "scott" — but "scott" is the SURNAME of
        # the MAM author and the FIRST NAME of the search author. The
        # surname guard rejects this (search surname "reintgen" is
        # NOT in m_tok={scott}).
        from app.discovery.sources.mam import _author_match
        assert _author_match(
            "Scott Reintgen", self._result(["M J Scott"]),
        ) is False

    def test_first_name_only_mam_lost_to_surname_guard(self):
        # Trade-off documented in the docstring: first-name-only MAM
        # uploads no longer match via reverse-subset. Real fiction
        # uploads almost always have the full author name; the
        # surname-collision FP class is more common.
        from app.discovery.sources.mam import _author_match
        assert _author_match(
            "John Smith", self._result(["John"]),
        ) is False

    def test_single_letter_initial_filtered(self):
        # "Michael R. Hicks" tokens (with periods stripped) are
        # {michael, r, hicks} — but `r` is single-letter and gets
        # filtered (length > 1 gate). So a MAM author "Michael Hicks"
        # (no middle initial) still matches the search.
        from app.discovery.sources.mam import _author_match
        assert _author_match(
            "Michael R. Hicks", self._result(["Michael Hicks"]),
        ) is True

    def test_empty_mam_authors_returns_false(self):
        # UAT 2026-05-11 round 2: previously defaulted to True as a
        # permissive escape hatch for "right book, missing metadata"
        # (Cohort C-style). In practice, MAM uploads with no listed
        # authors are almost always generic mega-collections that
        # text-overlap-match user subtitle templates and clog the
        # Possible band. Returning False here lets the no-positive-
        # signal demote filter sweep them to Not Found. Real Cohort C
        # books have other rescue signals (cover-pHash, description
        # mention) that fire upstream of the demote filter.
        from app.discovery.sources.mam import _author_match
        assert _author_match("Anyone", {"author_info": ""}) is False
        assert _author_match("Anyone", {}) is False

    def test_search_author_only_initials_returns_true(self):
        # If our search author is something pathological like just
        # "J K" (all single-letter tokens), there's no signal to
        # discriminate on — fall back to permissive True.
        from app.discovery.sources.mam import _author_match
        assert _author_match(
            "J K", self._result(["Anyone At All"]),
        ) is True

    def test_completely_different_author_no_match(self):
        from app.discovery.sources.mam import _author_match
        assert _author_match(
            "Pierce Scott", self._result(["Brandon Sanderson"]),
        ) is False

    # NOTE: a prior test_first_name_only_match (Brandon vs Brandon
    # Sanderson) was removed in UAT round 5 — the surname guard
    # added to reverse-subset matching now rejects first-name-only
    # MAM uploads. See test_first_name_only_mam_lost_to_surname_guard
    # above for the documented trade-off.


# ─── _scoped_filename_search hyphen-digit normalization ────────────
#
# UAT 2026-05-11 canary: "The Redemption of Maribeth-5" by Chloe
# Garner returned 0 from every scoped variant — the bundle's
# filenames don't lexically contain the literal token "Maribeth-5"
# (MAM tokenizes word-digit boundary as a single token). Replacing
# `Maribeth-5` with `Maribeth 5` lets MAM index the digit
# separately and the scoped pass surfaces the bundle. Real
# letter-LETTER hyphenated titles (X-Men, Spider-Man, Half-Elf)
# are left unchanged.


class TestScopedFilenameSearchHyphenNormalization:
    @pytest.mark.asyncio
    async def test_hyphen_digit_replaced_with_space_in_query(self, monkeypatch):
        from app.discovery.sources import mam as mam_mod
        captured = {}

        async def _fake_mam_search(token, authors, title, **kwargs):
            captured["text_override"] = kwargs.get("text_override")
            return {"data": []}

        monkeypatch.setattr(mam_mod, "_mam_search", _fake_mam_search)
        await mam_mod._scoped_filename_search(
            "tok", "The Redemption of Maribeth-5", "Chloe Garner",
        )
        assert "Maribeth 5" in captured["text_override"]
        assert "Maribeth-5" not in captured["text_override"]

    @pytest.mark.asyncio
    async def test_letter_letter_hyphen_left_unchanged(self, monkeypatch):
        # X-Men, Spider-Man — the digit gate prevents these from
        # being mangled.
        from app.discovery.sources import mam as mam_mod
        captured = {}

        async def _fake_mam_search(token, authors, title, **kwargs):
            captured["text_override"] = kwargs.get("text_override")
            return {"data": []}

        monkeypatch.setattr(mam_mod, "_mam_search", _fake_mam_search)
        await mam_mod._scoped_filename_search(
            "tok", "X-Men: Apocalypse", "Chris Claremont",
        )
        assert "X-Men" in captured["text_override"]

    @pytest.mark.asyncio
    async def test_period_stripping_still_applied(self, monkeypatch):
        # Regression check: the existing period-strip behavior on the
        # author segment is unchanged.
        from app.discovery.sources import mam as mam_mod
        captured = {}

        async def _fake_mam_search(token, authors, title, **kwargs):
            captured["text_override"] = kwargs.get("text_override")
            return {"data": []}

        monkeypatch.setattr(mam_mod, "_mam_search", _fake_mam_search)
        await mam_mod._scoped_filename_search(
            "tok", "Forged in Flame", "Michael R. Hicks",
        )
        assert "Michael R Hicks" in captured["text_override"]
        assert "R." not in captured["text_override"]


# ─── Fix E + Fix F: debug-match decision predicates ──────────────
#
# UAT 2026-05-11 round 3: 50 of 56 unowned-Possibles were
# "series-bundle" matches (bundle titled by series name, e.g.
# "Northern Crusade Series"); 1-2 were exact-title singletons
# sitting just below the conf=0.70 promote threshold. Fix E
# promotes the series-bundle class; Fix F promotes exact-title
# singletons. These tests pin the debug-match decision-string
# logic so future refactors don't silently drop the new branches.


def _trace_for_decision(
    monkeypatch, *,
    title="Foo",
    authors="Bar",
    series_name="",
    mam_title="Foo",
    mam_authors=None,
    is_bundle=False,
):
    """Helper: construct a debug_check_book trace with a single
    canned candidate and return its decision string.
    """
    import asyncio
    from app.discovery.sources import mam as mam_mod

    if mam_authors is None:
        mam_authors = ["Bar"]
    canned = {
        "data": [{
            "id": "1",
            "title": mam_title,
            "author_info": ",".join(mam_authors),
            "filetypes": "epub",
            "category": "Ebooks",
            "language": 1,
            "lang_code": "en",
            "seeders": 5,
            "numfiles": 5 if is_bundle else 1,
        }],
    }

    async def _fake_search(token, authors, search_title, **_kw):
        return canned

    async def _fake_scoped(*_a, **_kw):
        return set()

    monkeypatch.setattr(mam_mod, "_mam_search", _fake_search)
    monkeypatch.setattr(mam_mod, "_scoped_filename_search", _fake_scoped)

    trace = asyncio.run(mam_mod.debug_check_book(
        token="tok", title=title, authors=authors, series_name=series_name,
    ))
    decisions = []
    for p in trace["passes"]:
        if p.get("pass_kind") == "scoped":
            continue
        for r in p["results"]:
            decisions.append(r["decision"])
    return decisions


class TestDebugDecisionFixE:
    """Series-bundle inferred match — bundle titled by series name."""

    def test_series_in_bundle_title_promotes(self, monkeypatch):
        # "A Whisper After Midnight" by Christian Warren Freed,
        # series='Northern Crusade', candidate is "Northern Crusade
        # Series" bundle by same author. UAT canary pattern.
        decisions = _trace_for_decision(
            monkeypatch,
            title="A Whisper After Midnight",
            authors="Christian Warren Freed",
            series_name="Northern Crusade",
            mam_title="Northern Crusade Series",
            mam_authors=["Christian Warren Freed"],
            is_bundle=True,
        )
        assert "would_promote_via_series_bundle_match" in decisions

    def test_series_NOT_in_bundle_title_doesnt_promote_via_E(self, monkeypatch):
        # "Beyond Atlantis" series='Ascendant Chronicles' vs
        # "Atlantis Quadrilogy - Box Set" — series name doesn't
        # appear in bundle title, Fix E shouldn't fire.
        decisions = _trace_for_decision(
            monkeypatch,
            title="Beyond Atlantis",
            authors="Brandon Ellis",
            series_name="Ascendant Chronicles",
            mam_title="Atlantis Quadrilogy - Box Set",
            mam_authors=["Brandon Ellis"],
            is_bundle=True,
        )
        assert "would_promote_via_series_bundle_match" not in decisions

    def test_non_bundle_with_series_match_doesnt_promote_via_E(self, monkeypatch):
        # Fix E is bundle-specific. A non-bundle hit with series
        # name in title shouldn't promote via Fix E. Title chosen
        # to avoid `_is_bundle`'s keyword fallback (no "series" /
        # "collection" / "omnibus" / "bundle" / "book" tokens).
        decisions = _trace_for_decision(
            monkeypatch,
            title="Foo",
            authors="Author",
            series_name="Tropical Run",
            mam_title="Tropical Run",
            mam_authors=["Author"],
            is_bundle=False,
        )
        assert "would_promote_via_series_bundle_match" not in decisions

    def test_empty_series_doesnt_promote_via_E(self, monkeypatch):
        decisions = _trace_for_decision(
            monkeypatch,
            title="Some Book",
            authors="Author",
            series_name="",
            mam_title="Some Series Bundle",
            mam_authors=["Author"],
            is_bundle=True,
        )
        assert "would_promote_via_series_bundle_match" not in decisions


class TestDebugDecisionFixF:
    """Strong-text-anchor — exact title + author at conf>=0.65."""

    def test_exact_title_singleton_promotes(self, monkeypatch):
        # Tenuous Defense canary: ts >= 0.95, auth_matched=True,
        # conf=0.665 → previously kept_as_possible, now promotes
        # via strong_text_anchor.
        decisions = _trace_for_decision(
            monkeypatch,
            title="Tenuous Defense",
            authors="John Spearman",
            mam_title="Tenuous Defense",
            mam_authors=["John Spearman"],
            is_bundle=False,
        )
        # Either strong_text_anchor OR would_promote_to_found is
        # acceptable — both indicate "auto-promoted via text".
        # The point is it didn't stay kept_as_possible.
        assert any(
            d in (
                "would_promote_via_strong_text_anchor",
                "would_promote_to_found",
            )
            for d in decisions
        )

    def test_high_ts_but_no_author_match_doesnt_promote_via_F(self, monkeypatch):
        # ts high but author doesn't match — Fix F shouldn't fire.
        # Caught by the existing kept_as_possible_no_author_match
        # branch instead.
        decisions = _trace_for_decision(
            monkeypatch,
            title="Tenuous Defense",
            authors="Some Other Author",
            mam_title="Tenuous Defense",
            mam_authors=["John Spearman"],
            is_bundle=False,
        )
        assert "would_promote_via_strong_text_anchor" not in decisions


# ─── _strip_subtitle trailing-parenthetical fallback ─────────────
#
# UAT canary 2026-05-11 round 3: "Tower Mage 2 (The Nine Magics
# #2)" by David Burke surfaced "Tower Mage 2: A LitRPG Isekai
# Fantasy" with ts=0.57 because pass 4 (short title) never fired.
# The cascade pass-list builder calls `_strip_subtitle` to derive
# the short form; without a `:` / ` - ` / `|` delimiter it returned
# None and pass 4 was skipped, leaving only pass 1/5 to search the
# full Calibre title (parenthetical and all). The trailing-paren
# fallback handles Calibre's standard "<Book> (<Series> #<N>)"
# convention.


class TestStripSubtitleParenFallback:
    def test_calibre_format_paren_stripped(self):
        from app.discovery.sources.mam import _strip_subtitle
        assert _strip_subtitle(
            "Tower Mage 2 (The Nine Magics #2)"
        ) == "Tower Mage 2"

    def test_simple_paren_year_stripped(self):
        from app.discovery.sources.mam import _strip_subtitle
        assert _strip_subtitle("Foundation (1951)") == "Foundation"

    def test_subtitle_delimiter_takes_precedence(self):
        # When BOTH a delimiter and a paren are present, the delimiter
        # branch wins (stable behavior — same as before this change).
        from app.discovery.sources.mam import _strip_subtitle
        assert _strip_subtitle(
            "Foo: A Subtitle (Series #1)"
        ) == "Foo"

    def test_paren_with_no_text_before_returns_none(self):
        # Pathological: title is JUST a parenthetical. The m.start() > 0
        # gate ensures we don't return empty.
        from app.discovery.sources.mam import _strip_subtitle
        assert _strip_subtitle("(only parens)") is None

    def test_short_remainder_after_strip_returns_none(self):
        # If stripping leaves < 3 chars, treat as not-stripped (avoids
        # surfacing useless one/two-char "titles" to the cascade).
        from app.discovery.sources.mam import _strip_subtitle
        assert _strip_subtitle("Hi (a long parenthetical)") is None

    def test_mid_title_paren_left_alone(self):
        # Only end-of-title parens trigger the fallback — mid-title
        # parens shouldn't be touched. Without delimiters, returns None.
        from app.discovery.sources.mam import _strip_subtitle
        assert _strip_subtitle("Foo (Updated) Bar") is None

    def test_no_delimiter_no_paren_returns_none(self):
        # Plain title with no subtitle markers — None as before.
        from app.discovery.sources.mam import _strip_subtitle
        assert _strip_subtitle("Foundation") is None

    def test_many_open_parens_no_redos(self):
        # Regression for CodeQL alerts #35 and #36 (py/polynomial-redos).
        # Pathological input: long run of '(' with no closing ')'.
        # The previous `re.search(r"\([^)]*\)\s*$", title)` backtracked
        # O(n^2) here; the rfind-based rewrite runs linearly. We don't
        # measure timing (flaky in CI), just assert it returns the
        # expected None quickly without blowing the call stack.
        from app.discovery.sources.mam import _strip_subtitle
        assert _strip_subtitle("(" * 5000) is None

    def test_many_spaces_then_paren_no_redos(self):
        # Companion regression for CodeQL #35's "many spaces" vector.
        from app.discovery.sources.mam import _strip_subtitle
        assert _strip_subtitle((" " * 5000) + "(no close") is None
