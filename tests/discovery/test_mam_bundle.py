"""
Bundle-detection + bundle-promote-cap tests.

`_is_bundle` flags multi-book series collections so the scan logic can
keep them out of the "Found" tier when only the author matches (the
URL would point at the bundle, not the searched-for book), and so the
UI can render a "Series bundle" badge.

Cap behavior: when a bundle is the best result AND title-similarity
is below the floor, confidence-based promote is suppressed and the
result lands in "possible" instead. Verified here in isolation; Part
B2 will add a filelist-verification override that re-promotes a low-ts
bundle when the search title appears as a filename.
"""
import httpx
import pytest

from app.discovery.sources.mam import (
    _BUNDLE_PROMOTE_TS_FLOOR,
    _description_contains_title,
    _extract_mbsc,
    _fetch_filelist_response,
    _filelist_contains_title,
    _filelist_headers,
    _handle_response_cookie,
    _is_bundle,
    _normalize_for_filename_match,
    _parse_filelist_html,
    _strip_to_lines,
    get_current_mbsc_token,
    mark_mbsc_fresh,
    mbsc_is_stale,
    set_current_mbsc_token,
    set_mbsc_rotation_callback,
)


# Real captured response from /tor/filelist.php?torrentid=424895 — the
# Demon Accords Series ebook bundle. Filenames use two distinct naming
# styles in the same torrent (one author-first, one author-last), which
# is exactly the kind of variant the normalizer + substring matcher
# needs to handle.
DEMON_ACCORDS_FILELIST_HTML = (
    '<table class="tablesorter" id="fileListTable">'
    '<thead><th>Path</th><th>Filename</th><th>Size</th></tr></thead><tbody>'
    '<tr><td class="row1"></td><td class="row1">demon_accords_006_-_executable_-_john_conroe.epub</td><td class="row1">450.67 KiB</td></tr>'
    '<tr><td class="row2"></td><td class="row2">demon_accords_007_-_forced_ascent_-_john_conroe.epub</td><td class="row2">435.49 KiB</td></tr>'
    '<tr><td class="row1"></td><td class="row1">demon_accords_008_-_college_arcane_-_john_conroe.epub</td><td class="row1">455.80 KiB</td></tr>'
    '<tr><td class="row2"></td><td class="row2">John_Conroe_-_Demon_Accords_004_-_Duel_Nature.epub</td><td class="row2">325.82 KiB</td></tr>'
    '<tr><td class="row1"></td><td class="row1">John_Conroe_-_Demon_Accords_005_-_Fallen_Stars.epub</td><td class="row1">374.61 KiB</td></tr>'
    '</tbody></table>'
)


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


class TestBundlePromoteCap:
    """The cap is applied in `check_book._try_evaluate`. These tests
    pin the threshold constant so it can't drift silently and verify
    the promote-vs-cap decision logic is doing what we documented."""

    def test_cap_threshold_is_strict(self):
        # Floor of 0.85 leaves plenty of room above the regular 0.70
        # promote threshold — bundles need genuine title coverage to
        # be elevated to Found, not just "above the normal bar".
        assert _BUNDLE_PROMOTE_TS_FLOOR > 0.70

    def test_filelist_verification_gate(self):
        # Mirror the verification trigger from _try_evaluate. Fires
        # whenever the best candidate is a bundle, the author overlaps,
        # and the title alone doesn't strongly match — independent of
        # the blended confidence score. The latter point is the bugfix
        # in B2.1: B2's gate was tied to conf >= 0.70, which meant
        # author-only matches like Duel Nature → Demon Accords Series
        # (conf 0.30) never got verified.
        def needs_filelist_check(
            is_bundle: bool, author_matched: bool, ts: float
        ) -> bool:
            return (
                is_bundle
                and author_matched
                and ts < _BUNDLE_PROMOTE_TS_FLOOR
            )

        # The Duel Nature → Demon Accords Series case: low conf, low ts,
        # author matches, bundle. Must verify.
        assert needs_filelist_check(True, True, 0.0) is True

        # No author overlap: don't burn a fetch on a totally unrelated
        # bundle (e.g. "Duel Nature" against "Mixed Calibre Library").
        assert needs_filelist_check(True, False, 0.0) is False

        # Bundle whose title strongly matches the calibre title
        # (intentional bundle catalog entry) — promotes via the normal
        # path without needing a filelist fetch.
        assert needs_filelist_check(True, True, 0.95) is False

        # Single-book result — never bundle-verify.
        assert needs_filelist_check(False, True, 0.0) is False

    def test_promote_after_verification_predicate(self):
        # Final promote decision. Mirrors the should_promote logic in
        # _try_evaluate so future refactors can't drop the bundle-cap
        # safety or break the verification override.
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
        # This is THE Duel Nature path after B2.1.
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


class TestFilelistHeaders:
    """Pin the browser-shape headers MAM requires for /tor/filelist.php
    to return the bare <table> fragment instead of the full site
    wrapper. Production-confirmed via the debug-match endpoint:
    Seshat's working search-API headers (curl/8.0 UA, JSON content
    type) and a partial AJAX fix (Referer + jQuery Accept + XHR
    marker) BOTH still got the wrapper. Only switching to a Firefox
    UA + Sec-Fetch-* markers got the bare fragment back.
    """

    def test_referer_points_at_torrent_page(self):
        # The Referer is one of the AJAX-shape signals.
        h = _filelist_headers("mbsc-stub", "424895")
        assert h["Referer"] == "https://www.myanonamouse.net/t/424895"

    def test_jquery_signature_accept_header(self):
        # jQuery's $.ajax() sends Accept "text/html, */*; q=0.01" by
        # default. The q=0.01 is what jQuery uses.
        h = _filelist_headers("m", "1")
        assert h["Accept"] == "text/html, */*; q=0.01"

    def test_browser_user_agent(self):
        # curl/8.0 alone (the search-API UA) gets the wrapper response.
        # Browser UA is one of the signals MAM uses to decide whether
        # to render the page chrome vs. the bare AJAX fragment.
        h = _filelist_headers("m", "1")
        assert "Firefox" in h["User-Agent"]
        assert "Mozilla" in h["User-Agent"]

    def test_sec_fetch_xhr_markers(self):
        # Sec-Fetch-Dest:empty + Mode:cors + Site:same-origin is the
        # browser fingerprint of a fetch()/$.ajax() XHR call. MAM's
        # filelist endpoint appears to gate the bare-fragment
        # response on these.
        h = _filelist_headers("m", "1")
        assert h["Sec-Fetch-Dest"] == "empty"
        assert h["Sec-Fetch-Mode"] == "cors"
        assert h["Sec-Fetch-Site"] == "same-origin"

    def test_cookie_carries_only_mbsc(self):
        # The browser sends ONLY mbsc on filelist requests (verified
        # 2026-05-09 from Mark's DevTools — the only session-relevant
        # cookie present at MAM is mbsc; there is no mam_id cookie at
        # all). Sending mam_id alongside mbsc triggers MAM's
        # cross-session-defense logout (Set-Cookie deletions for uid +
        # mam_id + mbsc). Pin the exact wire shape so a future
        # refactor doesn't accidentally re-introduce mam_id.
        h = _filelist_headers("browser-xyz", "424895")
        assert h["Cookie"] == "mbsc=browser-xyz"

    def test_cookie_does_not_include_mam_id(self):
        # Belt + suspenders for the regression that bit production on
        # 2026-05-09: every filelist request was 302'd to login because
        # the cookie header carried both mam_id and mbsc.
        h = _filelist_headers("browser-xyz", "424895")
        assert "mam_id=" not in h["Cookie"]


class TestFilelistParser:
    def test_extracts_filenames_from_real_response(self):
        names = _parse_filelist_html(DEMON_ACCORDS_FILELIST_HTML)
        assert len(names) == 5
        assert "John_Conroe_-_Demon_Accords_004_-_Duel_Nature.epub" in names
        assert "demon_accords_006_-_executable_-_john_conroe.epub" in names

    def test_empty_html_returns_empty_list(self):
        assert _parse_filelist_html("") == []
        assert _parse_filelist_html(None) == []  # type: ignore[arg-type]

    def test_no_table_returns_empty(self):
        # MAM occasionally serves an error page or a redirect HTML when
        # the torrent is gone — must not crash.
        assert _parse_filelist_html("<html><body>not found</body></html>") == []

    def test_malformed_html_returns_empty(self):
        # Half-cut response from a truncated download.
        assert _parse_filelist_html("<table><tr><td>") == []


class TestNormalizeForFilenameMatch:
    def test_strips_extension(self):
        assert _normalize_for_filename_match("Duel_Nature.epub") == "duel nature"

    def test_collapses_separators(self):
        # Underscores, dashes, dots, multiple spaces all → single space.
        assert _normalize_for_filename_match(
            "John_Conroe_-_Demon_Accords_004_-_Duel_Nature.epub"
        ) == "john conroe demon accords 004 duel nature"

    def test_preserves_digits(self):
        # Volume numbers must survive normalization — they're useful
        # for distinguishing books in the same series.
        assert "004" in _normalize_for_filename_match("book_004.epub")

    def test_empty_input(self):
        assert _normalize_for_filename_match("") == ""
        assert _normalize_for_filename_match(None) == ""  # type: ignore[arg-type]


class TestFilelistContainsTitle:
    def setup_method(self):
        self.filenames = _parse_filelist_html(DEMON_ACCORDS_FILELIST_HTML)

    def test_title_in_filename_promotes_bundle(self):
        # The whole point: searching for "Duel Nature" finds it inside
        # the bundle's filelist even though the bundle's MAM title is
        # "Demon Accords Series".
        assert _filelist_contains_title(self.filenames, "Duel Nature") is True

    def test_title_not_in_filelist(self):
        # A book by the same author that isn't in this particular bundle.
        assert _filelist_contains_title(self.filenames, "Some Other Book") is False

    def test_alternate_naming_style(self):
        # The Demon Accords torrent has TWO naming conventions in the
        # same filelist; both should be matchable.
        assert _filelist_contains_title(self.filenames, "Forced Ascent") is True
        assert _filelist_contains_title(self.filenames, "College Arcane") is True

    def test_multiple_titles_any_hit(self):
        # When the cascade has both calibre_title and search_title (after
        # subtitle stripping), passing both gives the user-favourable
        # OR semantics. Ensures we don't miss a match because one variant
        # didn't normalize cleanly.
        assert _filelist_contains_title(
            self.filenames,
            "Bikini Days: An Unconventional Romance",
            "Duel Nature",
        ) is True

    def test_single_word_title_rejected_to_avoid_false_positives(self):
        # "Dawn" alone would substring-match "Bikini Dawn", "Dawn of Foo",
        # etc. — too noisy. Verifier requires ≥ 2 tokens.
        assert _filelist_contains_title(self.filenames, "Dawn") is False
        assert _filelist_contains_title(self.filenames, "Nature") is False

    def test_empty_filenames_returns_false(self):
        assert _filelist_contains_title([], "Duel Nature") is False

    def test_empty_titles_returns_false(self):
        assert _filelist_contains_title(self.filenames) is False
        assert _filelist_contains_title(self.filenames, "") is False
        assert _filelist_contains_title(self.filenames, "", "") is False

    def test_case_insensitive(self):
        # Filenames in real bundles are mixed case; calibre titles too.
        assert _filelist_contains_title(self.filenames, "DUEL NATURE") is True
        assert _filelist_contains_title(self.filenames, "duel nature") is True

    def test_punctuation_in_search_title_normalizes(self):
        # If user's calibre title has punctuation but filename doesn't.
        # "Duel: Nature" → "duel nature" → matches.
        assert _filelist_contains_title(self.filenames, "Duel: Nature") is True


# ─── mbsc browser-session cookie ────────────────────────────


def _make_response(headers: list, body: str = "x") -> httpx.Response:
    """Build a real httpx.Response with a bound request.

    httpx.Response.cookies lazily walks the request URL when picking
    which Set-Cookie headers apply, so a Response built without a
    request raises on `.cookies` access. Same helper shape as
    tests/mam/test_cookie.py.
    """
    request = httpx.Request(
        "GET", "https://www.myanonamouse.net/tor/filelist.php?torrentid=1"
    )
    return httpx.Response(
        200,
        headers=headers,
        content=body.encode("utf-8"),
        request=request,
    )


@pytest.fixture
def mbsc_state_isolated():
    """Save + restore mbsc module state around each test.

    Module globals leak across tests in the same process; this fixture
    snapshots `_current_mbsc_token`, the rotation callback, and the
    stale flag, lets the test mutate freely, then restores.
    """
    from app.discovery.sources import mam as mam_mod

    saved_token = mam_mod._current_mbsc_token
    saved_cb = mam_mod._mbsc_rotation_callback
    saved_stale = mam_mod._mbsc_stale
    try:
        yield
    finally:
        mam_mod._current_mbsc_token = saved_token
        mam_mod._mbsc_rotation_callback = saved_cb
        mam_mod._mbsc_stale = saved_stale


class TestExtractMbsc:
    """Set-Cookie parser for the mbsc cookie. Mirrors TestExtractMamId
    in tests/mam/test_cookie.py so a future jar-handling refactor
    can't silently break one cookie's rotation while leaving the
    other working."""

    def test_extracts_from_jar(self):
        resp = _make_response(
            [("set-cookie", "mbsc=NEW_BROWSER_TOKEN; Path=/; HttpOnly")]
        )
        assert _extract_mbsc(resp) == "NEW_BROWSER_TOKEN"

    def test_returns_none_when_no_cookie(self):
        resp = _make_response([("content-type", "text/html")])
        assert _extract_mbsc(resp) is None

    def test_unrelated_cookie_returns_none(self):
        # A mam_id rotation in the same response must not be
        # mis-extracted as an mbsc value.
        resp = _make_response(
            [("set-cookie", "mam_id=ROTATED_API_TOKEN; Path=/")]
        )
        assert _extract_mbsc(resp) is None

    def test_max_age_zero_deletion_returns_none(self):
        # MAM serves Set-Cookie: mbsc=deleted; Max-Age=0 to terminate
        # a session. Observed 2026-05-09 when a filelist fetch with a
        # mismatched mam_id+mbsc pair triggered MAM's cross-session
        # logout response; the prior regex fallback captured "deleted"
        # as a rotation and persisted it to the encrypted store. The
        # jar correctly drops Max-Age=0 cookies — extractor must
        # return None.
        resp = _make_response(
            [(
                "set-cookie",
                "mbsc=deleted; expires=Thu, 01 Jan 1970 00:00:01 GMT; "
                "Max-Age=0; path=/; domain=.myanonamouse.net",
            )]
        )
        assert _extract_mbsc(resp) is None


class TestDeletionSentinelNotRotated:
    """The 2026-05-09 corruption regression: a rejected filelist
    response includes Set-Cookie deletions for both mam_id and mbsc.
    The handler must NOT capture either as a rotation, otherwise the
    in-memory state and encrypted store get poisoned and every
    subsequent search 403s until container restart."""

    async def test_mbsc_deletion_does_not_rotate(self, mbsc_state_isolated):
        from app.discovery.sources import mam as mam_mod
        set_current_mbsc_token("VALID_OLD_VALUE")
        mam_mod._last_mbsc_rotation_save = 0.0
        fired: list[str] = []
        async def cb(new_token: str) -> None:
            fired.append(new_token)
        set_mbsc_rotation_callback(cb)

        resp = _make_response([(
            "set-cookie",
            "mbsc=deleted; expires=Thu, 01 Jan 1970 00:00:01 GMT; "
            "Max-Age=0; path=/; domain=.myanonamouse.net",
        )])
        await _handle_response_cookie(resp)

        # In-memory token preserved; callback never fired.
        assert get_current_mbsc_token() == "VALID_OLD_VALUE"
        assert fired == []

    async def test_mam_id_deletion_does_not_rotate(self, mbsc_state_isolated):
        # Same defense for the parallel mam_id slot in the discovery
        # module — the regression bit BOTH cookies because MAM sent
        # deletion sentinels for mam_id AND mbsc on the same response.
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


class TestMbscRotationHandler:
    """The handler is the only path that promotes a Set-Cookie value
    into the in-memory token + persistence callback. Drift here would
    silently kill rotation for the mbsc cookie."""

    async def test_rotation_updates_token_and_fires_callback(
        self, mbsc_state_isolated
    ):
        set_current_mbsc_token("OLD_VALUE")
        seen: list[str] = []

        async def cb(new_token: str) -> None:
            seen.append(new_token)

        set_mbsc_rotation_callback(cb)
        # Force the debounce window past the threshold so the test
        # doesn't depend on real wall-clock timing.
        from app.discovery.sources import mam as mam_mod
        mam_mod._last_mbsc_rotation_save = 0.0

        resp = _make_response([("set-cookie", "mbsc=FRESH_VALUE; Path=/")])
        await _handle_response_cookie(resp)

        assert get_current_mbsc_token() == "FRESH_VALUE"
        assert seen == ["FRESH_VALUE"]

    async def test_rotation_clears_stale_flag(self, mbsc_state_isolated):
        # A successful rotation means MAM accepted our cookie enough
        # to mint a new one — definitive evidence that whatever made
        # us mark stale earlier is no longer relevant.
        from app.discovery.sources import mam as mam_mod
        mam_mod._mbsc_stale = True
        set_current_mbsc_token("OLD")
        mam_mod._last_mbsc_rotation_save = 0.0

        resp = _make_response([("set-cookie", "mbsc=NEW; Path=/")])
        await _handle_response_cookie(resp)

        assert mbsc_is_stale() is False

    async def test_no_rotation_when_value_unchanged(
        self, mbsc_state_isolated
    ):
        # MAM occasionally echoes the same cookie back. Don't fire
        # the callback for a no-op; the persist budget is precious.
        set_current_mbsc_token("SAME")
        seen: list[str] = []

        async def cb(new_token: str) -> None:
            seen.append(new_token)

        set_mbsc_rotation_callback(cb)
        resp = _make_response([("set-cookie", "mbsc=SAME; Path=/")])
        await _handle_response_cookie(resp)

        assert seen == []


class TestMbscAutoDegrade:
    """When mbsc isn't configured, filelist verification must
    short-circuit BEFORE any network call — both to avoid the wasted
    ~2s round trip and to keep B1's bundle-cap-with-badge as the
    correct UX without mbsc."""

    async def test_fetch_returns_none_when_mbsc_unset(
        self, mbsc_state_isolated
    ):
        set_current_mbsc_token("")  # explicit "not configured"

        # No transport mocking needed — the auto-degrade short-circuit
        # fires before any HTTP call. If this test ever tries to reach
        # MAM, that's a regression and the test (running in CI without
        # network) will fail loudly.
        result = await _fetch_filelist_response("424895")
        assert result is None

    async def test_fetch_returns_none_when_torrent_id_empty(
        self, mbsc_state_isolated
    ):
        # Pre-existing guard, not an mbsc thing — pin it so a future
        # refactor doesn't drop it.
        set_current_mbsc_token("valid-mbsc")
        result = await _fetch_filelist_response("")
        assert result is None


class TestMbscStaleFlag:
    """Stale detection drives the Settings UI pill that tells Mark to
    paste a fresh mbsc when MAM starts rejecting the old one."""

    def test_starts_fresh(self, mbsc_state_isolated):
        from app.discovery.sources import mam as mam_mod
        mam_mod._mbsc_stale = False
        assert mbsc_is_stale() is False

    def test_mark_fresh_clears(self, mbsc_state_isolated):
        from app.discovery.sources import mam as mam_mod
        mam_mod._mbsc_stale = True
        mark_mbsc_fresh()
        assert mbsc_is_stale() is False

    async def test_login_page_response_marks_stale(
        self, mbsc_state_isolated, monkeypatch
    ):
        # Wire a mocked transport that returns the MAM login wrapper
        # (the response shape the original B2 work observed when
        # mam_id was sent without mbsc). The fetcher should detect
        # the marker and flip the stale flag.
        from app.discovery.sources import mam as mam_mod

        set_current_mbsc_token("expired-mbsc")
        mam_mod._mbsc_stale = False

        login_body = (
            "<html><head>"
            '<title>Login | My Anonamouse</title>'
            '</head><body data-uclass="0">login form</body></html>'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=login_body, request=request)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(mam_mod, "_get_client", lambda: mock_client)
        try:
            resp = await _fetch_filelist_response("424895")
        finally:
            await mock_client.aclose()

        assert resp is not None
        assert resp.status_code == 200
        assert mbsc_is_stale() is True

    async def test_real_filelist_response_does_not_mark_stale(
        self, mbsc_state_isolated, monkeypatch
    ):
        # The happy path: MAM returns the bare table fragment. No
        # login marker present → flag stays clear.
        from app.discovery.sources import mam as mam_mod

        set_current_mbsc_token("good-mbsc")
        mam_mod._mbsc_stale = False

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=DEMON_ACCORDS_FILELIST_HTML, request=request
            )

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(mam_mod, "_get_client", lambda: mock_client)
        try:
            await _fetch_filelist_response("424895")
        finally:
            await mock_client.aclose()

        assert mbsc_is_stale() is False


# ─── Description-based bundle verification ──────────────────


class TestStripToLines:
    """Description text comes back from MAM as a mix of HTML
    (<br />, <strong>, etc.) and BBCode ([b], [size=4], [*], etc.).
    Block-level markup must become line breaks; inline formatting
    must vanish without affecting line structure."""

    def test_html_br_becomes_newline(self):
        result = _strip_to_lines("<strong>Title 1</strong><br />Title 2")
        assert "Title 1" in result
        assert "Title 2" in result

    def test_strips_inline_html_tags(self):
        result = _strip_to_lines("<strong>bold</strong> <em>italic</em>")
        # Single line, formatting tags gone, content collapsed to one space
        assert result == ["bold italic"]

    def test_bbcode_list_marker_creates_lines(self):
        result = _strip_to_lines("[*]First[*]Second[*]Third")
        assert "First" in result
        assert "Second" in result
        assert "Third" in result

    def test_bbcode_formatting_stripped(self):
        result = _strip_to_lines("[b]bold[/b] [size=4]big[/size]")
        assert result == ["bold big"]

    def test_html_entities_decoded(self):
        result = _strip_to_lines("foo&nbsp;bar &amp; baz &quot;q&quot;")
        assert result == ['foo bar & baz "q"']

    def test_p_div_li_become_newlines(self):
        result = _strip_to_lines("<p>One</p><div>Two</div><li>Three</li>")
        assert "One" in result
        assert "Two" in result
        assert "Three" in result

    def test_real_world_demon_accords_pattern(self):
        # Pattern Mark called out: <br /><br /><strong>Title - N</strong><br />
        html = (
            "<br /><br /><strong>Duel Nature - 4</strong><br />"
            "<strong>Demon Driven - 2</strong><br />"
        )
        result = _strip_to_lines(html)
        assert "Duel Nature - 4" in result
        assert "Demon Driven - 2" in result

    def test_real_world_bbcode_audiobook_listing(self):
        # Pattern from the API docs example.
        body = (
            "[size=4][b]Love at Stake - Books 1-16[/b][/size]\r\n\r\n"
            "[*] 01. How To Marry a Millionaire Vampire - Narrated by Foo - 11h35m, MP3\r\n"
            "[*] 02. Vamps and the City - Narrated by Bar - 11h34m, MP3"
        )
        result = _strip_to_lines(body)
        assert any("How To Marry a Millionaire Vampire" in l for l in result)
        assert any("Vamps and the City" in l for l in result)

    def test_empty_inputs(self):
        assert _strip_to_lines("") == []
        assert _strip_to_lines(None) == []


class TestDescriptionContainsTitle:
    """The structured-line check that gates bundle promotion when the
    filelist signal isn't available. Conservative on false positives
    (rejects prose mentions, recommendations, negations) and matches
    the most common bundle-listing patterns MAM uploaders use."""

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
        # Same threshold as filelist matcher — single-token titles are
        # too noisy to match standalone (they'd false-positive on every
        # bundle listing that happens to contain that word).
        desc = "<br /><strong>Dawn</strong><br />"
        assert _description_contains_title(desc, "Dawn") is False

    def test_empty_inputs(self):
        assert _description_contains_title("", "Duel Nature") is False
        assert _description_contains_title(None, "Duel Nature") is False
        assert _description_contains_title("desc", "") is False
        assert _description_contains_title("desc") is False

    def test_multiple_titles_any_hit(self):
        # OR semantics across the title alternates — same shape as
        # _filelist_contains_title's variadic API.
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
        # Calibre title often has a colon for subtitle; bundle listing
        # may not. Both should still match the core title.
        assert _description_contains_title(desc, "Duel Nature") is True

    def test_dash_title_not_confused_with_volume_marker(self):
        # Title "Half-Elf Chronicles" contains a dash but it's part
        # of the title, not a volume marker. The matcher should handle
        # both forms (with and without volume suffix).
        desc = "<br />Half-Elf Chronicles - 1<br />"
        assert _description_contains_title(desc, "Half-Elf Chronicles") is True

    def test_list_marker_followed_by_number(self):
        # "[*] 01. Title" — both the BBCode list marker AND the
        # numbering need stripping.
        desc = "[*] 01. Duel Nature"
        assert _description_contains_title(desc, "Duel Nature") is True
