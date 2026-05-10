"""Cohort C rescue mechanism tests (B3 of the Part C arc).

Two paths address books where text scoring underperforms (Possible
band) AND cover-pHash can't help (because the right MAM upload uses
visually-different cover art — the Cohort C definition):

  - **B3a description rescue** — `_description_mentions_title_loose`
    + integration in `_try_evaluate`. Fetches the torrent description
    via the documented Search API (TOS-allowed) and word-boundary
    matches the searched title. Catches cases like Incarceron / MMM
    where the publisher description always names the book.

  - **B3b volume disambiguation** — extended `_extract_volume` (now
    handles trailing Roman numerals II-XX and bare arabic) plus a
    post-evaluation penalty in `_try_evaluate` that uses the ORIGINAL
    calibre title (not the per-pass search title — variant passes
    deliberately strip volume markers). For Raw Bk1, this prefers
    plain "Raw" over series-sibling "Raw V/VI/VII".
"""
import pytest

from app.discovery.sources.mam import _description_mentions_title_loose
from app.metadata.scoring import _extract_volume


# ─── B3b: extended _extract_volume ──────────────────────────────


class TestExtractVolumeRoman:
    @pytest.mark.parametrize("title,expected", [
        ("Raw V", 5),
        ("Raw VI", 6),
        ("Raw VII", 7),
        ("Raw VIII", 8),
        ("Final Fantasy VII", 7),
        ("Foo XIV", 14),
        ("Foo XX", 20),
    ])
    def test_roman_trailing(self, title, expected):
        assert _extract_volume(title) == expected

    def test_bare_i_is_intentionally_skipped(self):
        # "Star Wars: Episode I" — bare trailing "I" is too noisy to
        # match (would false-positive on "I, Robot" / "I am Legend" /
        # any title ending in the pronoun). Documented exclusion.
        assert _extract_volume("Star Wars: Episode I") is None

    @pytest.mark.parametrize("title", [
        # Bare "I" alone is too noisy ("I, Robot", "I am Legend")
        "I, Robot",
        "I am Legend",
        # Roman characters as prefix or middle don't match
        "X-Men",
        "II of III: A Story",
        "V for Vendetta",
        # Roman immediately after non-space
        "TitleV",
    ])
    def test_roman_no_false_match(self, title):
        assert _extract_volume(title) is None


class TestExtractVolumeTrailingArabic:
    @pytest.mark.parametrize("title,expected", [
        ("Right of Retribution 2", 2),
        ("Right of Retribution 02", 2),
        ("Domestic Decay 2", 2),
        ("Past Life Hero 2", 2),
        ("Foo 12", 12),
    ])
    def test_trailing_arabic(self, title, expected):
        assert _extract_volume(title) == expected

    @pytest.mark.parametrize("title", [
        # Mid-title numbers don't match (anchored to end)
        "Apollo 11 Mission Report",
        # Hyphenated number doesn't match (no leading space)
        "Catch-22",
        # 4-digit year-likes excluded by \d{1,2} bound
        "Stories 1990",
        # No trailing digit
        "Foundation",
    ])
    def test_trailing_arabic_no_false_match(self, title):
        assert _extract_volume(title) is None


class TestExtractVolumePrecedence:
    """Keyword > Roman > trailing arabic. Pin the priority order."""

    def test_keyword_wins_over_trailing(self):
        # "Foo Book 5 7" — keyword "Book 5" wins over trailing "7".
        # Realistic? Not really, but pin the precedence.
        assert _extract_volume("Foo Book 5 extra 7") == 5

    def test_roman_when_no_keyword(self):
        assert _extract_volume("Raw V") == 5


# ─── B3a: description-based loose match ─────────────────────────


class TestDescriptionMentionsTitleLoose:
    def test_multi_word_title_in_paragraph(self):
        desc = "<p>Monsters Mayhem & Misfits is the first book in...</p>"
        assert _description_mentions_title_loose(desc, "Monsters Mayhem & Misfits") is True

    def test_long_single_word_title(self):
        # 5+ char single-word titles are accepted (Incarceron is the
        # canonical case — 10 chars, distinctive).
        desc = "Incarceron is the place where..."
        assert _description_mentions_title_loose(desc, "Incarceron") is True

    def test_short_single_word_rejected(self):
        # "Raw" (3 chars, single token) is too noisy — rejected to
        # avoid false positives on "raw materials" / "raw emotion"
        # in unrelated descriptions.
        desc = "Raw is a story about..."
        assert _description_mentions_title_loose(desc, "Raw") is False

    def test_subtitle_stripped_for_match(self):
        # Title carries subtitle; the subtitle isn't required to
        # appear — match strips after first colon.
        desc = "<p>Monsters Mayhem & Misfits is hilarious!</p>"
        assert _description_mentions_title_loose(
            desc, "Monsters Mayhem & Misfits: A Comedy"
        ) is True

    def test_word_boundary_anchored(self):
        # Title appearing as substring inside another word doesn't
        # match — \b prevents "Veil" from matching "unveiled".
        desc = "The truth was unveiled at midnight..."
        assert _description_mentions_title_loose(desc, "Veil") is False

    def test_html_bbcode_stripped(self):
        # Block-level markup becomes whitespace; inline markup vanishes.
        # Title still findable across markup.
        desc = "<br /><strong>Incarceron</strong><br />by Catherine Fisher"
        assert _description_mentions_title_loose(desc, "Incarceron") is True

    def test_empty_inputs_return_false(self):
        assert _description_mentions_title_loose(None, "Incarceron") is False
        assert _description_mentions_title_loose("", "Incarceron") is False
        assert _description_mentions_title_loose("Incarceron", None) is False
        assert _description_mentions_title_loose("Incarceron", "") is False

    def test_case_insensitive(self):
        desc = "INCARCERON IS THE PLACE..."
        assert _description_mentions_title_loose(desc, "Incarceron") is True

    def test_negation_context_still_matches(self):
        # Loose match doesn't try to detect prose negations like "if you
        # liked X" / "not Incarceron" — it just confirms the title
        # appears. The author-matched + Possible-band gates upstream
        # are the false-positive defense. Pin this so a future
        # "smart" rewrite that adds context detection also accounts
        # for it explicitly.
        desc = "Fans of Incarceron will enjoy..."
        assert _description_mentions_title_loose(desc, "Incarceron") is True
