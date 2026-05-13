"""
Tests for the v2.11.0 `_looks_foreign` keyword expansion.

Pre-v2.11.0 the foreign-language detection in `app/discovery/lookup.py`
relied on diacritics + non-Latin script + a Hungarian/Polish/Russian
keyword list. UAT 2026-05-13 (Hasekura) exposed French / German /
Italian / Polish edition markers slipping through:

  - "Coffret Spice and Wolf tomes 9" (French boxed set)
  - "Some Book Sonderausgabe" (German special edition)

v2.11.0 adds four unambiguous-non-English edition markers to the
keyword list: coffret (FR), ausgabe (DE), edizione (IT), wydanie (PL).

This test pins the new behavior without re-asserting the existing
diacritic/script detection paths (those have implicit coverage via
the existing merge-pipeline tests).
"""
from __future__ import annotations

from app.discovery.lookup import _looks_foreign


class TestLooksForeignNewKeywords:
    """v2.11.0 — unambiguously-non-English edition markers."""

    def test_french_coffret(self):
        assert _looks_foreign("Coffret Spice and Wolf tomes 9") is True

    def test_german_ausgabe(self):
        assert _looks_foreign("Spice and Wolf Sonderausgabe") is True

    def test_italian_edizione(self):
        assert _looks_foreign("Spice and Wolf Edizione Limitata") is True

    def test_polish_wydanie(self):
        assert _looks_foreign("Spice and Wolf Wydanie Specjalne") is True


class TestLooksForeignRegression:
    """Existing keyword + script detection unaffected by v2.11.0 expansion."""

    def test_hungarian_kapuja(self):
        assert _looks_foreign("Halál kapuja") is True

    def test_polish_przebudzenie(self):
        assert _looks_foreign("Babilon: Przebudzenie") is True

    def test_russian_cyrillic(self):
        assert _looks_foreign("Пробуждение Левиафана") is True

    def test_japanese_kanji(self):
        assert _looks_foreign("狼と香辛料") is True

    def test_french_diacritic(self):
        # Already caught by _RX_FOREIGN_ACCENTS — verify the
        # path still works alongside the new keyword list.
        assert _looks_foreign("Édition spéciale") is True


class TestLooksForeignNotEnglishFalsePositives:
    """English titles that contain non-English-looking words but
    aren't actually foreign. Guards against over-eager filtering."""

    def test_english_with_tome_word_passes(self):
        # "tome" exists in English ("The Forbidden Tomes"). The
        # v2.11.0 keyword list intentionally excludes "tomes" /
        # "tome" because they overlap with English.
        assert _looks_foreign("The Forbidden Tomes of Power") is False

    def test_english_standard_title(self):
        assert _looks_foreign("The Way of Kings") is False

    def test_english_with_punctuation(self):
        assert _looks_foreign("Spice and Wolf, Vol. 7") is False

    def test_english_with_apostrophe(self):
        assert _looks_foreign("Wolf and Parchment: New Theory") is False

    def test_empty_string(self):
        # _looks_foreign should not crash on empty input. Either
        # True or False is acceptable; empty title is filtered
        # earlier in the pipeline anyway.
        try:
            _looks_foreign("")
        except Exception as e:
            raise AssertionError(f"_looks_foreign crashed on empty: {e}")
