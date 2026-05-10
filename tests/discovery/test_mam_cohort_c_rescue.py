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

from app.discovery.sources.mam import (
    _description_mentions_title_loose,
    _pick_best_result,
)
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


class TestExtractVolumeSubtitleStrip:
    """The 2026-05-09 fix: when the full title doesn't match any
    volume pattern, also try the strip-subtitle form. UAT canary:
    "Delivering Justice 2: A Men's Superhero Adventure" — trailing
    arabic regex requires the digit at title END, but here the
    digit is mid-title (followed by ": A Men's..."). Stripping the
    subtitle yields "Delivering Justice 2" → vol 2.
    """

    def test_arabic_after_subtitle(self):
        assert _extract_volume("Delivering Justice 2: A Men's Superhero Adventure") == 2

    def test_roman_after_subtitle(self):
        # Raw siblings — subtitle "A Primeval Harem" makes Roman
        # mid-title without the strip.
        assert _extract_volume("Raw V: A Primeval Harem") == 5
        assert _extract_volume("Raw VIII: A Primeval Harem") == 8

    def test_keyword_after_subtitle(self):
        # "Foo Vol. 3 - Subtitle" → strip → "Foo Vol. 3" → keyword 3
        assert _extract_volume("Foo Vol. 3 - Subtitle") == 3

    def test_short_form_already_matched_no_strip_needed(self):
        # If full title already matches, return without checking short.
        # Pin the precedence so a future refactor doesn't accidentally
        # shadow the result.
        assert _extract_volume("Foo: Book 5") == 5  # full keyword wins

    def test_dash_subtitle_delimiter(self):
        assert _extract_volume("Title 7 - The Saga Continues") == 7

    def test_subtitle_strip_doesnt_affect_negatives(self):
        # Plain titles with no volume in either full or short form.
        assert _extract_volume("Foundation: A Sci-Fi Classic") is None


class TestExtractVolumeRangeGate:
    """Range markers should NOT yield a single int — that misled the
    per-candidate volume disambiguation in _try_evaluate. UAT canary:
    bundle "Series request, Domestic Decay 2 - 5" was extracting "5"
    via trailing arabic and falsely volume-mismatching against a
    vol-2 search.
    """

    def test_range_returns_none(self):
        assert _extract_volume("Series request, Domestic Decay 2 - 5") is None

    def test_keyworded_range_returns_none(self):
        assert _extract_volume("The Demon Accords Books 1-4") is None

    def test_bare_trailing_range_returns_none(self):
        assert _extract_volume("Demon Accords 1-4") is None

    def test_paren_range_returns_none(self):
        assert _extract_volume("Foo (1-3)") is None

    def test_single_volume_still_works_after_range_gate(self):
        # The range gate only fires when _extract_volume_range matches.
        # Single-volume titles must still extract normally.
        assert _extract_volume("Demon Accords: Book 7") == 7
        assert _extract_volume("Foo Bar 5") == 5


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


# ─── _pick_best_result confidence tiebreak ──────────────────────


class TestPickBestResultConfidenceTiebreak:
    """When match_pct ties across siblings (e.g. all 5 Marcus Sloss
    "Monsters Mayhem & Misfits N" books at 96% match_pct), the
    candidate with NO volume marker has higher post-B3b confidence
    (no -0.20 penalty) and SHOULD win over its siblings.

    Pre-fix the sort key was (fmt_rank, -match_pct, -fmt_count,
    -seeders) — fmt_count would win the tiebreak and whichever
    sibling happens to have multiple formats uploaded would be
    silently picked. Post-fix `confidence` slots in between
    match_pct and fmt_count.
    """

    def _candidate(self, tid, *, match_pct, confidence, formats=None, seeders=5):
        if formats is None:
            formats = ["epub"]
        return {
            "torrent_id": tid,
            "match_pct": match_pct,
            "confidence": confidence,
            "format_str": ",".join(formats),
            "formats": formats,
            "seeders": seeders,
        }

    def test_higher_conf_wins_when_match_pct_tied(self):
        # MMM canary — Bk1 (no vol marker, conf=0.96) vs siblings
        # (vol marker → -0.20 penalty → conf=0.77). Match_pct tied.
        # MMM6 has 2 formats (would have won pre-fix via fmt_count
        # tiebreak); now confidence ranks above fmt_count so MMM1
        # wins despite having only 1 format.
        candidates = [
            self._candidate("MMM3", match_pct=96, confidence=0.77),
            self._candidate("MMM2", match_pct=96, confidence=0.77),
            self._candidate("MMM1", match_pct=96, confidence=0.96),
            self._candidate("MMM6", match_pct=96, confidence=0.77, formats=["azw3", "epub"]),
            self._candidate("MMM4", match_pct=96, confidence=0.77),
        ]
        winner = _pick_best_result(candidates, format_priority=["epub"])
        assert winner["torrent_id"] == "MMM1"

    def test_match_pct_still_dominates_over_conf(self):
        # When match_pct differs, it wins over confidence — the
        # tiebreak only fires within an equal match_pct cohort.
        candidates = [
            self._candidate("low_match", match_pct=80, confidence=0.95),
            self._candidate("high_match", match_pct=95, confidence=0.50),
        ]
        winner = _pick_best_result(candidates, format_priority=["epub"])
        assert winner["torrent_id"] == "high_match"

    def test_fmt_count_still_breaks_tie_when_conf_tied(self):
        # When match_pct AND confidence are both tied, fmt_count
        # falls back as the next-tier tiebreak (legacy behavior).
        candidates = [
            self._candidate("one_fmt", match_pct=96, confidence=0.96, formats=["epub"]),
            self._candidate("two_fmts", match_pct=96, confidence=0.96, formats=["epub", "azw3"]),
        ]
        winner = _pick_best_result(candidates, format_priority=["epub", "azw3"])
        assert winner["torrent_id"] == "two_fmts"
