"""
Tests for Goodreads's `_series_from_title_paren` title-fallback
helper.

Goodreads' detail pages increasingly embed series info in the page
title itself — "Right of Retribution 3 (Right of Retribution #3)".
When the structured seriesTitle div is missing or unparseable
(which happened on Mark's UAT run), this fallback pulls the same
info out of the title so the book still lands in the correct
series at the correct index.

Without this fallback, Goodreads reported the book as standalone,
the merge layer URL-backfilled it onto a different book (a fuzzy
match on the title stem), and the library ended up with a junk
row where the new entry should have been.
"""
from __future__ import annotations

import pytest

from app.discovery.sources.goodreads import _series_from_title_paren


class TestSeriesFromTitleParen:
    def test_right_of_retribution_3(self):
        name, idx = _series_from_title_paren(
            "Right of Retribution 3 (Right of Retribution #3)"
        )
        assert name == "Right of Retribution"
        assert idx == 3.0

    def test_hash_with_space_before(self):
        # Some Goodreads pages have whitespace variants.
        name, idx = _series_from_title_paren(
            "Book (Series Name #1)"
        )
        assert name == "Series Name"
        assert idx == 1.0

    def test_comma_separator(self):
        # Occasional variant: "Series Name, #3" inside parens.
        name, idx = _series_from_title_paren(
            "Book (Series Name, #3)"
        )
        assert name == "Series Name"
        assert idx == 3.0

    def test_comma_without_hash(self):
        # "Some Series, 2" — bare number, no hash.
        name, idx = _series_from_title_paren("Book (Some Series, 2)")
        assert name == "Some Series"
        assert idx == 2.0

    def test_decimal_series_index(self):
        # Novellas at fractional positions.
        name, idx = _series_from_title_paren(
            "Novella (Series #2.5)"
        )
        assert name == "Series"
        assert idx == 2.5

    def test_no_trailing_paren_returns_none(self):
        # Just a subtitle, not a series.
        name, idx = _series_from_title_paren(
            "Otherlife Dreams: The Selfless Hero Trilogy"
        )
        assert name is None
        assert idx is None

    def test_paren_without_index_returns_none(self):
        # Parenthetical that isn't a series+number — don't claim a series.
        name, idx = _series_from_title_paren(
            "The Book (Special Edition)"
        )
        assert name is None
        assert idx is None

    def test_empty_title(self):
        assert _series_from_title_paren("") == (None, None)
        assert _series_from_title_paren(None) == (None, None)  # type: ignore[arg-type]

    def test_intermediate_paren_not_at_end_rejected(self):
        # "Series #3" buried mid-title shouldn't extract — avoids
        # false positives on weird formatting.
        name, idx = _series_from_title_paren(
            "Book (Series #3) with extra text"
        )
        assert name is None
        assert idx is None

    def test_leading_whitespace_tolerated(self):
        name, idx = _series_from_title_paren(
            "Book (  Series Name  #2  )"
        )
        assert name == "Series Name"
        assert idx == 2.0
