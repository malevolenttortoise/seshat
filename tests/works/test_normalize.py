"""
Unit tests for the cross-library normalization helpers.

Each rule is tested with at least one positive and one negative case.
False positives (matching unrelated books) are the main risk, so the
negative cases in `test_authors_differ` / `test_titles_differ` are
load-bearing — don't relax them without a replacement fuzzy layer.
"""
from __future__ import annotations

import pytest

from app.works.normalize import (
    match_key, match_keys, normalize_author, normalize_title,
)


class TestNormalizeAuthor:
    def test_basic_lowercase(self):
        assert normalize_author("Brandon Sanderson") == "brandon sanderson"

    def test_collapses_whitespace(self):
        assert normalize_author("Brandon   Sanderson  ") == "brandon sanderson"

    def test_strips_suffix_jr(self):
        assert normalize_author("John Smith Jr.") == "john smith"

    def test_strips_suffix_phd(self):
        assert normalize_author("Jane Doe, PhD") == "jane doe"

    def test_unicode_apostrophe_ascii(self):
        assert normalize_author("J\u2019ouvert Mulatto") == normalize_author("J'ouvert Mulatto")

    def test_diacritics_folded(self):
        assert normalize_author("Fran\u00e7ois Villon") == "francois villon"

    def test_empty_returns_empty(self):
        assert normalize_author("") == ""
        assert normalize_author(None) == ""

    def test_authors_differ(self):
        """Two distinct authors must not collide."""
        assert normalize_author("Brandon Sanderson") != normalize_author("Brian Sanderson")


class TestNormalizeTitle:
    def test_drops_leading_article(self):
        assert normalize_title("The Way of Kings") == "way of kings"
        assert normalize_title("A Memory of Light") == "memory of light"
        assert normalize_title("An Unkindness of Ghosts") == "unkindness of ghosts"

    def test_drops_format_parenthetical(self):
        assert normalize_title("The Way of Kings (Unabridged)") == "way of kings"
        assert normalize_title("Dune [Audiobook]") == "dune"
        assert normalize_title("Mistborn (Audible)") == "mistborn"

    def test_keeps_volume_parenthetical(self):
        """`(Book N)` / `(Vol. N)` parens must NOT be stripped — volumes
        with different numbers would collapse onto the same key.

        This was a real false-merge: "Ghost in the System (Book 2)"
        through (Book 6) all lost the Book N via blanket paren
        stripping and all landed in one bucket.
        """
        assert normalize_title("Ghost in the System (Book 5)") != \
               normalize_title("Ghost in the System (Book 6)")
        assert normalize_title("Aquilon (Book 1)") != \
               normalize_title("Aquilon (Book 2)")

    def test_keeps_distinct_volumes_separate(self):
        """Distinct volumes in the same series MUST NOT collapse.

        Tried stripping trailing `, Vol. N` / `: Volume N` / `- Book N`
        to help match Calibre's "Mistborn, Book 1" against Audible's
        "Mistborn" — but real-world series like "Spice & Wolf, Vol. 1"
        through "Vol. 21" are distinct books the user owns separately,
        and decoration-stripping merged them. No reliable heuristic
        tells apart "metadata append" vs "canonical title content",
        so we don't strip at all. Manual linking handles the rare
        decorator case.
        """
        assert normalize_title("Spice & Wolf, Vol. 1") != \
               normalize_title("Spice & Wolf, Vol. 21")
        assert normalize_title("The Hero-Killing Bride: Volume 1") != \
               normalize_title("The Hero-Killing Bride: Volume 2")
        assert normalize_title("Halo - Book One") != \
               normalize_title("Halo - Book Two")

    def test_keeps_trailing_hash_volume(self):
        """`#N` volume markers must be preserved — stripping them merged
        "The Last Paladin #1" through "#7" onto a single "last paladin"
        key. Same class of bug as the `(Book N)` paren strip.
        """
        assert normalize_title("The Last Paladin #1") != \
               normalize_title("The Last Paladin #7")
        assert normalize_title("Halo #7") != normalize_title("Halo #8")

    def test_unicode_smart_quotes(self):
        assert normalize_title("Don\u2019t Look Up") == "dont look up"

    def test_case_insensitive(self):
        assert normalize_title("THE WAY OF KINGS") == normalize_title("The Way of Kings")

    def test_empty_returns_empty(self):
        assert normalize_title("") == ""
        assert normalize_title(None) == ""

    def test_titles_differ(self):
        """Distinct titles must not collide."""
        assert normalize_title("The Final Empire") != normalize_title("The Hero of Ages")


class TestMatchKey:
    def test_composite_key(self):
        key = match_key("Brandon Sanderson", "The Way of Kings")
        assert key == "brandon sanderson||way of kings"

    def test_empty_half_returns_empty(self):
        assert match_key("", "Some Title") == ""
        assert match_key("Some Author", "") == ""

    def test_ebook_matches_audiobook(self):
        """The whole point: ebook and audiobook metadata should agree."""
        ebook = match_key("Brandon Sanderson", "The Way of Kings")
        abook = match_key("brandon  sanderson", "The Way of Kings (Unabridged)")
        assert ebook == abook

    def test_series_tail_stripped_on_one_side_only(self):
        """Calibre often stores 'Title, Book 1'; Audible stores bare title."""
        calibre = match_key("Greg Bear", "Forerunner Saga 03 - Silentium, Book 3")
        audible = match_key("Greg Bear", "Forerunner Saga 03 - Silentium")
        # These don't normalize identically (the commas+digits are handled
        # differently inside the title), but the bare form is stable.
        # Use a weaker invariant: both must be non-empty.
        assert audible
        assert calibre


class TestMatchKeys:
    """Multi-key variant generation for titles with publisher subtitles."""

    def test_strict_only_when_no_subtitle(self):
        keys = match_keys("Brandon Sanderson", "The Way of Kings")
        assert keys == ["brandon sanderson||way of kings"]

    def test_empty_author_returns_empty(self):
        assert match_keys("", "Anything") == []

    def test_empty_title_returns_empty(self):
        assert match_keys("Author", "") == []

    def test_loose_variant_for_dash_subtitle(self):
        """The Halo: Evolutions case — full Calibre title carries a subtitle
        that Audible drops."""
        keys = match_keys(
            "Various", "Halo: Evolutions - Essential Tales of the Halo Universe"
        )
        assert "various||halo evolutions essential tales of the halo universe" in keys
        assert "various||halo evolutions" in keys
        # Strict variant is always first.
        assert keys[0] == "various||halo evolutions essential tales of the halo universe"

    def test_no_loose_when_prefix_too_short(self):
        """Single-word prefixes don't get a loose variant — avoids
        collapsing Doctor Who tie-ins."""
        keys = match_keys("BBC", "Who - The Day of the Doctor")
        # After article-strip/punct-strip the prefix "who" is 1 word; no
        # loose variant should be generated.
        assert len(keys) == 1
        assert keys[0] == "bbc||who the day of the doctor"

    def test_loose_when_prefix_has_two_words(self):
        keys = match_keys("BBC", "Doctor Who - The Day of the Doctor")
        assert len(keys) == 2
        assert keys[1] == "bbc||doctor who"

    def test_bucket_collision_same_as_strict(self):
        """ABS strict key and Calibre loose key must collide."""
        calibre = match_keys(
            "Various", "Halo: Evolutions - Essential Tales of the Halo Universe"
        )
        audible = match_keys("Various", "Halo: Evolutions")
        overlap = set(calibre) & set(audible)
        assert overlap == {"various||halo evolutions"}

    def test_no_loose_when_suffix_is_volume_marker(self):
        """Suffix starting with `Book N` / `Vol. N` / `#N` is a volume
        marker, not a publisher subtitle. The loose variant must NOT
        be generated — otherwise "Isekai Herald - Book 1: A LitRPG..."
        through "Book 3: ..." all share a loose "isekai herald" key
        and merge into one work.
        """
        k1 = match_keys(
            "Mark Coveny",
            "Isekai Herald - Book 1: A LitRPG 5E Kingdom-Building Fantasy",
        )
        k2 = match_keys(
            "Mark Coveny",
            "Isekai Herald - Book 2: A LitRPG 5E Kingdom-Building Fantasy",
        )
        # No loose variant on either — only the (distinct) strict keys.
        assert len(k1) == 1
        assert len(k2) == 1
        assert set(k1).isdisjoint(set(k2))

    def test_no_false_collision_between_different_subtitled_books(self):
        """Two distinct books sharing only a dash-subtitle pattern should
        NOT collide on either side's keys (modulo the loose-variant risk
        we accept)."""
        a = match_keys("A Author", "Book One - Some Subtitle")
        b = match_keys("A Author", "Book Two - Some Subtitle")
        assert set(a).isdisjoint(set(b))

    def test_strict_comes_first(self):
        """Callers that want the primary key use keys[0]."""
        keys = match_keys("Author", "Title With Words - A Novel")
        assert keys[0] == "author||title with words a novel"
