"""
Unit tests for `app.metadata.author_names` — normalization, matching,
and variant expansion.
"""
from __future__ import annotations

import pytest

from app.metadata.author_names import (
    author_name_variants,
    authors_match,
    normalize_author_name,
    pick_canonical_display_name,
)


# ─── normalize_author_name ────────────────────────────────────

class TestNormalizeAuthorName:
    @pytest.mark.parametrize("raw, normalized", [
        # The driving case — four variants of A. K. Duboff collapse
        # to a single canonical form.
        ("A K Duboff",     "ak duboff"),
        ("A. K. Duboff",   "ak duboff"),
        ("A.K. Duboff",    "ak duboff"),
        ("A.K. DuBoff",    "ak duboff"),
        # Longer initial runs
        ("J.R.R. Tolkien", "jrr tolkien"),
        ("J R R Tolkien",  "jrr tolkien"),
        # No initials — pass through unchanged aside from case
        ("Brandon Sanderson", "brandon sanderson"),
        ("Pierce Brown",      "pierce brown"),
        # Diacritics stripped
        ("Noël Carré",    "noel carre"),
        # Extra whitespace collapsed
        ("A.  K.   Duboff", "ak duboff"),
        ("  Brandon  Sanderson  ", "brandon sanderson"),
    ])
    def test_canonical_forms(self, raw, normalized):
        assert normalize_author_name(raw) == normalized

    def test_empty_returns_empty(self):
        assert normalize_author_name("") == ""
        assert normalize_author_name(None) == ""  # type: ignore[arg-type]

    def test_only_whitespace_returns_empty(self):
        assert normalize_author_name("   ") == ""

    def test_single_letter_token_kept(self):
        # "A" alone is an initial, not a word. No merge target → stays as-is.
        assert normalize_author_name("A") == "a"

    def test_dupe_internal_whitespace_normalized_to_single_space(self):
        assert normalize_author_name("A. K.     Duboff") == "ak duboff"


# ─── authors_match ────────────────────────────────────────────

class TestAuthorsMatch:
    @pytest.mark.parametrize("a, b", [
        ("A K Duboff",   "A.K. Duboff"),
        ("A. K. Duboff", "A.K. DuBoff"),
        ("A.K. Duboff",  "AK Duboff"),
        ("J.R.R. Tolkien", "J R R Tolkien"),
        ("Brandon Sanderson", "brandon sanderson"),
        ("Noël Carré", "Noel Carre"),
    ])
    def test_equivalent_names_match(self, a, b):
        assert authors_match(a, b)

    @pytest.mark.parametrize("a, b", [
        # Completely different authors.
        ("A.K. Duboff",       "Amy DuBoff"),
        ("Brandon Sanderson", "Pierce Brown"),
        # Shared surname, different firsts — common false-positive risk.
        ("J.R.R. Tolkien",    "Christopher Tolkien"),
        ("Brandon Sanderson", "Brian Sanderson"),
    ])
    def test_different_authors_do_not_match(self, a, b):
        assert not authors_match(a, b)

    def test_one_character_typo_still_matches(self):
        # Dropped letter on a longer name should still clear 0.92.
        assert authors_match("Brandon Sanderson", "Brandon Sandersn")

    def test_empty_inputs_do_not_match(self):
        assert not authors_match("", "Brandon Sanderson")
        assert not authors_match("Brandon Sanderson", "")
        assert not authors_match("", "")


# ─── author_name_variants ─────────────────────────────────────

class TestAuthorNameVariants:
    def test_spaced_initials_generates_four_variants(self):
        # Driving case for the Goodreads fix: Mark's Calibre row is
        # "A K Duboff" but Goodreads responds to "A.K. Duboff".
        out = author_name_variants("A K Duboff")
        assert out[0] == "A K Duboff"      # original tried first
        assert "A. K. Duboff" in out
        assert "A.K. Duboff" in out
        assert "AK Duboff" in out
        assert len(out) == 4

    def test_periodized_compact_generates_four_variants(self):
        out = author_name_variants("A.K. Duboff")
        assert out[0] == "A.K. Duboff"
        assert "A. K. Duboff" in out
        assert "A K Duboff" in out
        assert "AK Duboff" in out
        assert len(out) == 4

    def test_periodized_spaced_generates_four_variants(self):
        out = author_name_variants("A. K. Duboff")
        assert out[0] == "A. K. Duboff"
        assert "A.K. Duboff" in out
        assert "A K Duboff" in out
        assert "AK Duboff" in out
        assert len(out) == 4

    def test_triple_initials(self):
        out = author_name_variants("J.R.R. Tolkien")
        assert out[0] == "J.R.R. Tolkien"
        assert "J. R. R. Tolkien" in out
        assert "J R R Tolkien" in out
        assert "JRR Tolkien" in out

    def test_no_initials_returns_only_original(self):
        # No expansion when the name has no initial-like tokens.
        assert author_name_variants("Brandon Sanderson") == ["Brandon Sanderson"]
        assert author_name_variants("Pierce Brown") == ["Pierce Brown"]

    def test_capped_at_four_variants(self):
        assert len(author_name_variants("A K Duboff")) <= 4

    def test_empty_input_returns_empty_list(self):
        assert author_name_variants("") == []
        assert author_name_variants("   ") == []

    def test_original_always_first(self):
        # Even when the original matches one of the shapes we render
        # later, it should remain position 0 so first-try uses the
        # caller's stored spelling.
        assert author_name_variants("AK Duboff")[0] == "AK Duboff"

    def test_deduplicated(self):
        # When rendering produces a duplicate of an earlier entry
        # (e.g., single-letter + word name where compact and spaces
        # render identically), the list stays deduped.
        out = author_name_variants("A Smith")
        assert len(out) == len(set(out))


# ─── pick_canonical_display_name ──────────────────────────────

class TestPickCanonicalDisplayName:
    def test_more_periods_wins(self):
        # Option 4a heuristic — the more-punctuated form is canonical.
        assert (
            pick_canonical_display_name("A K DuBoff", "A. K. DuBoff")
            == "A. K. DuBoff"
        )

    def test_more_periods_wins_regardless_of_order(self):
        # Reverse argument order — existing wins if it has more periods.
        assert (
            pick_canonical_display_name("A. K. DuBoff", "A K DuBoff")
            == "A. K. DuBoff"
        )

    def test_tie_keeps_existing(self):
        # Equal period count — keep the existing stored name so we
        # don't thrash the row on every sync.
        assert (
            pick_canonical_display_name("A K DuBoff", "A K DuBoff")
            == "A K DuBoff"
        )
        assert (
            pick_canonical_display_name("A.K. DuBoff", "A. K. DuBoff")
            == "A.K. DuBoff"
        )

    def test_empty_existing_returns_incoming(self):
        assert pick_canonical_display_name("", "A. K. DuBoff") == "A. K. DuBoff"

    def test_empty_incoming_returns_existing(self):
        assert pick_canonical_display_name("A. K. DuBoff", "") == "A. K. DuBoff"

    def test_plain_names_tie_keeps_existing(self):
        # No periods in either — tie — existing wins.
        assert (
            pick_canonical_display_name("Brandon Sanderson", "brandon sanderson")
            == "Brandon Sanderson"
        )
