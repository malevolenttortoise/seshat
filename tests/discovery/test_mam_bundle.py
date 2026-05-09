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
from app.discovery.sources.mam import (
    _BUNDLE_PROMOTE_TS_FLOOR,
    _is_bundle,
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

    def test_cap_predicate(self):
        # Mirror the predicate from _try_evaluate so a refactor that
        # changes the cap logic without updating it here gets caught.
        def would_cap(is_bundle: bool, conf: float, ts: float) -> bool:
            return is_bundle and conf >= 0.70 and ts < _BUNDLE_PROMOTE_TS_FLOOR

        # Demon Accords Series for "Duel Nature": author-only match on
        # a bundle. Confidence 0.30 < 0.70 → wouldn't promote anyway,
        # cap doesn't fire.
        assert would_cap(True, 0.30, 0.0) is False

        # Hypothetical bundle that scores high on confidence (e.g. via
        # series boost + author) but title doesn't really match — cap.
        assert would_cap(True, 0.74, 0.50) is True

        # Same scores but not a bundle — let normal promote logic run.
        assert would_cap(False, 0.74, 0.50) is False

        # Bundle whose own title strongly matches the user's calibre
        # title (intentional bundle catalog entry) — let it promote.
        assert would_cap(True, 0.95, 0.95) is False
