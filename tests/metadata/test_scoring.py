"""
Scoring + similarity tests.

The enricher's accept decision is downstream of these functions, so
making sure the boundary cases behave sanely is the easiest way to
prevent subtle match-quality regressions.
"""
import pytest

from app.metadata.scoring import (
    _extract_volume,
    _extract_volume_range,
    author_overlap,
    score_match,
    score_match_with_breakdown,
    title_similarity,
)


class TestTitleSimilarity:
    def test_exact_match_is_one(self):
        assert title_similarity("Foundation", "Foundation") == 1.0

    def test_word_order_invariant(self):
        assert title_similarity("Kings Way", "Way Kings") == 1.0

    def test_partial_overlap(self):
        # "Foundation" vs "Foundation and Empire" → one title is a
        # substring of the other. Since a09d063 the scoring weights
        # containment more heavily, producing ~0.71 (was <0.6 under
        # the old pure-token-overlap formula). The higher score is
        # correct behavior: a single-word title matching the first
        # word of a multi-word title IS a strong signal.
        score = title_similarity("Foundation", "Foundation and Empire")
        assert 0.6 < score < 0.8

    def test_disjoint_is_zero(self):
        assert title_similarity("Mistborn", "Dune") == 0.0

    def test_empty_inputs(self):
        assert title_similarity("", "Foundation") == 0.0
        assert title_similarity("Foundation", "") == 0.0

    def test_stopwords_dropped(self):
        # "The Way of Kings" vs "Way Kings" → all content tokens match.
        assert title_similarity("The Way of Kings", "Way Kings") == 1.0


class TestAuthorOverlap:
    def test_full_match_list(self):
        assert author_overlap(["Brandon Sanderson"], ["Brandon Sanderson"]) == 1.0

    def test_blob_vs_blob(self):
        assert author_overlap(
            "Brandon Sanderson, Janci Patterson",
            "Janci Patterson",
        ) == 1.0

    def test_no_overlap(self):
        assert author_overlap(["Isaac Asimov"], ["Frank Herbert"]) == 0.0

    def test_empty_target_is_zero(self):
        assert author_overlap(["Someone"], []) == 0.0

    def test_case_insensitive(self):
        assert author_overlap(
            ["brandon sanderson"], ["Brandon Sanderson"]
        ) == 1.0


class TestScoreMatch:
    def test_perfect_match_is_high(self):
        score = score_match(
            record_title="The Way of Kings",
            record_authors=["Brandon Sanderson"],
            search_title="The Way of Kings",
            search_authors="Brandon Sanderson",
        )
        assert score >= 0.95

    def test_title_only_match_is_lower(self):
        score = score_match(
            record_title="The Way of Kings",
            record_authors=["Someone Else"],
            search_title="The Way of Kings",
            search_authors="Brandon Sanderson",
        )
        # 0.7 from title, 0 from authors → 0.7
        assert 0.65 < score < 0.75

    def test_author_only_match_is_lowest(self):
        score = score_match(
            record_title="Mistborn",
            record_authors=["Brandon Sanderson"],
            search_title="The Way of Kings",
            search_authors="Brandon Sanderson",
        )
        # 0 from title, 0.3 from authors → 0.3
        assert 0.25 < score < 0.35


class TestExtractVolume:
    def test_book_n(self):
        assert _extract_volume("Foo: Book 5") == 5

    def test_volume_n(self):
        assert _extract_volume("Foo, Volume 12") == 12

    def test_vol_with_period(self):
        assert _extract_volume("Foo Vol. 3") == 3

    def test_no_volume(self):
        assert _extract_volume("Foundation") is None

    def test_bare_range_does_not_match(self):
        # "1-4" lacks the keyword prefix — bundle territory, handled by
        # Part B, not the volume-mismatch guard.
        assert _extract_volume("The Demon Accords 1-4") is None

    def test_empty(self):
        assert _extract_volume("") is None


class TestSeriesStripFallback:
    """Regression tests for the empty-residue fallback path.

    When the series-strip + clean removes everything that would
    distinguish this record from a sibling volume, the old code
    scored ts=0 and the result landed at 0.40 — a 'Possible' badge
    on a 100%-correct URL. The fix falls back to comparing original
    titles, with a volume-mismatch guard to keep Book 2 from being
    promoted as a match for Book 5.
    """

    def test_self_titled_series_first_book_promotes(self):
        # 1-800-Starship by J. N. Chaney — series == title == record.
        # Old behavior: confidence=0.40 → "Possible".
        b = score_match_with_breakdown(
            record_title="1-800-STARSHIP",
            record_authors=["J N Chaney"],
            search_title="1-800-Starship",
            search_authors="J. N. Chaney",
            known_series="1-800-Starship",
        )
        assert b["fallback_to_full_title"] is True
        assert b["confidence"] >= 0.95

    def test_series_name_prefix_with_calibre_subtitle_promotes(self):
        # Calibre adds a subtitle MAM doesn't have. Bikini Days case.
        b = score_match_with_breakdown(
            record_title="Bikini Days",
            record_authors=["Michael Dalton"],
            search_title="Bikini Days: An Unconventional Romance",
            search_authors="Michael Dalton",
            known_series="Bikini Days",
        )
        assert b["fallback_to_full_title"] is True
        assert b["confidence"] >= 0.95

    def test_book_n_residue_promotes_when_volumes_match(self):
        # Strip leaves "Book 5", which _clean_title eats via the
        # volume-noise pattern → empty residue. Volumes match → promote.
        b = score_match_with_breakdown(
            record_title="Blackwood Milk Farm: Book 5",
            record_authors=["Eden Redd"],
            search_title="Blackwood Milk Farm: Book 5",
            search_authors="Eden Redd",
            known_series="Blackwood Milk Farm",
        )
        assert b["fallback_to_full_title"] is True
        assert b["confidence"] >= 0.95

    def test_volume_mismatch_returns_zero(self):
        # Same series, different volumes — definitively wrong book.
        # Without this guard, the empty-residue fallback would score
        # Book 2 just as high as Book 5 (clean_title erases the
        # volume from both, ts=1.0).
        b = score_match_with_breakdown(
            record_title="Blackwood Milk Farm: Book 2",
            record_authors=["Eden Redd"],
            search_title="Blackwood Milk Farm: Book 5",
            search_authors="Eden Redd",
            known_series="Blackwood Milk Farm",
        )
        assert b["confidence"] == 0.0
        assert b.get("volume_mismatch") is True

    def test_bundle_range_residue_does_not_falsely_promote(self):
        # "The Demon Accords 1-4" — strip → "1-4" → all-numeric residue.
        # No volume extractable from "1-4" (no keyword), so the guard
        # doesn't fire. Falls back to full title comparison, which
        # against "Duel Nature" still scores ts=0 because the tokens
        # don't overlap. Confidence stays low — Part B handles bundles.
        b = score_match_with_breakdown(
            record_title="The Demon Accords 1-4",
            record_authors=["John Conroe"],
            search_title="Duel Nature",
            search_authors="John Conroe",
            known_series="The Demon Accords",
        )
        assert b["confidence"] < 0.5

    def test_normal_strip_still_works(self):
        # Strip leaves real tokens — current behavior unchanged.
        # "The Triangulum Fold: The Fold Series Book 8" with series
        # "The Fold" → strip → "The Triangulum Fold: Series Book 8"
        # which has plenty of non-numeric tokens.
        b = score_match_with_breakdown(
            record_title="The Triangulum Fold: The Fold Series Book 8",
            record_authors=["A Author"],
            search_title="The Triangulum Fold",
            search_authors="A Author",
            known_series="The Fold",
        )
        assert b["fallback_to_full_title"] is False
        assert b["series_stripped"] is True
        # Strong title overlap + author + series boost → high score.
        assert b["confidence"] >= 0.85

    def test_no_series_no_fallback(self):
        # When known_series is empty, none of this logic runs.
        b = score_match_with_breakdown(
            record_title="Foundation",
            record_authors=["Isaac Asimov"],
            search_title="Foundation",
            search_authors="Isaac Asimov",
        )
        assert b["fallback_to_full_title"] is False
        assert b["series_stripped"] is False
        assert b["confidence"] >= 0.95


class TestExtractVolumeRange:
    @pytest.mark.parametrize(
        "title,expected",
        [
            # Keyworded forms
            ("The Demon Accords Books 1-4", (1, 4)),
            ("Witcher Saga Volumes 1-7", (1, 7)),
            ("Foo Vol. 1-12", (1, 12)),
            ("Foo Vols 3-7", (3, 7)),
            ("Foo Books 1 - 4", (1, 4)),
            ("Foo Books #1-#4", (1, 4)),
            ("Foo Episodes 5-10", (5, 10)),
            ("Foo Parts 1-3", (1, 3)),
            # Bare trailing forms
            ("The Demon Accords 1-4", (1, 4)),
            ("Foo (1-3)", (1, 3)),
            ("Witcher Series 1-7", (1, 7)),
            # En-dash / em-dash
            ("Foo Books 1–4", (1, 4)),
            ("Foo Books 1—4", (1, 4)),
            ("Foo 1–4", (1, 4)),
        ],
    )
    def test_positive_patterns(self, title, expected):
        assert _extract_volume_range(title) == expected

    @pytest.mark.parametrize(
        "title",
        [
            # Single volumes (not ranges)
            "Foo Book 5",
            "Foo Volume 12",
            "Foo Vol. 3",
            # No range at all
            "Foundation",
            "The Way of Kings",
            # Year ranges (bare trailing form bound: end <= 50)
            "Stories 1990-2000",
            "Foo 1995-2005",
            # Reversed range (start >= end)
            "Foo Books 4-1",
            "Foo 5-3",
            # Decimals (single volume, not a range)
            "Foo Vol 1.5",
            # Empty / none
            "",
            "   ",
            # Mid-title hyphenation that would false-positive without trailing anchor
            "Stop-Loss: Foo",
            "1-800-Starship: Foo",
            # Bare range exceeding conservative bound (end > 50)
            "Foo 1-100",
            # Bare range with span > 30
            "Foo 1-40",
        ],
    )
    def test_negative_patterns(self, title):
        assert _extract_volume_range(title) is None

    def test_keyworded_form_lenient_on_large_ranges(self):
        # "Books 1-100" has explicit keyword → lenient bound (end <= 999,
        # span <= 99). 99 span exactly hits the upper limit.
        assert _extract_volume_range("Foo Books 1-100") == (1, 100)
        # 100 span exceeds it
        assert _extract_volume_range("Foo Books 1-101") is None

    def test_bare_form_at_start_does_not_match(self):
        # Anchored to title end — leading numeric ranges shouldn't match.
        assert _extract_volume_range("1-4 Foo") is None


class TestVolumeRangeMismatch:
    """Short-circuit guard for bundles whose range excludes the searched volume."""

    def test_volume_outside_range_returns_zero(self):
        # Demon Accords 1-4 vs searching for Book 7 — definitively wrong.
        b = score_match_with_breakdown(
            record_title="The Demon Accords Books 1-4",
            record_authors=["John Conroe"],
            search_title="The Demon Accords: Book 7",
            search_authors="John Conroe",
            known_series="The Demon Accords",
        )
        assert b["confidence"] == 0.0
        assert b.get("volume_range_mismatch") is True
        assert b["candidate_range"] == [1, 4]
        assert b["search_volume"] == 7

    def test_volume_inside_range_falls_through(self):
        # Book 2 IS in the 1-4 bundle — short-circuit must NOT fire.
        # Falls through to normal scoring; confidence may still be modest
        # because the bundle title doesn't strongly match the book title,
        # but the volume_range_mismatch flag is absent.
        b = score_match_with_breakdown(
            record_title="The Demon Accords Books 1-4",
            record_authors=["John Conroe"],
            search_title="The Demon Accords: Book 2",
            search_authors="John Conroe",
            known_series="The Demon Accords",
        )
        assert b.get("volume_range_mismatch") is None

    def test_volume_at_range_boundaries_falls_through(self):
        # Inclusive bounds: start and end values are IN the range.
        for vol in (1, 4):
            b = score_match_with_breakdown(
                record_title="Foo Books 1-4",
                record_authors=["X"],
                search_title=f"Foo: Book {vol}",
                search_authors="X",
            )
            assert b.get("volume_range_mismatch") is None

    def test_search_no_volume_falls_through(self):
        # Search has no volume marker → can't compare → no short-circuit.
        b = score_match_with_breakdown(
            record_title="Foo Books 1-4",
            record_authors=["X"],
            search_title="Foo",
            search_authors="X",
        )
        assert b.get("volume_range_mismatch") is None

    def test_record_no_range_falls_through(self):
        # Single-volume record vs single-volume search — uses the
        # existing volume_mismatch path, not the range path.
        b = score_match_with_breakdown(
            record_title="Foo: Book 2",
            record_authors=["X"],
            search_title="Foo: Book 5",
            search_authors="X",
            known_series="Foo",
        )
        assert b.get("volume_range_mismatch") is None

    def test_bare_trailing_range_works(self):
        # "Demon Accords 1-4" (no keyword) vs Book 7 — bare form catches it.
        b = score_match_with_breakdown(
            record_title="The Demon Accords 1-4",
            record_authors=["John Conroe"],
            search_title="The Demon Accords: Book 7",
            search_authors="John Conroe",
        )
        assert b["confidence"] == 0.0
        assert b.get("volume_range_mismatch") is True

    def test_mismatch_fires_before_series_strip(self):
        # Range mismatch is decisive even when series matches strongly —
        # short-circuit MUST fire before series-strip path runs.
        b = score_match_with_breakdown(
            record_title="The Demon Accords Books 1-4",
            record_authors=["John Conroe"],
            search_title="The Demon Accords: Book 9",
            search_authors="John Conroe",
            known_series="The Demon Accords",  # series matches
        )
        assert b["confidence"] == 0.0
        assert b["series_stripped"] is False  # short-circuit ran first
        assert b.get("volume_range_mismatch") is True
