"""v3.10.0 — `app.discovery.language` normalization tests.

Eight sources write `books.language` in three different shapes, so the
same language lands as "en", "eng" or "English" depending on who found
the book. Everything that compares languages goes through here.

The load-bearing property is the three-way split between English,
foreign, and UNKNOWN: 99.3% of unowned rows on the reference install
have no language at all, and an unparseable value must never be treated
as foreign.
"""
from __future__ import annotations

import pytest

from app.discovery.language import (
    is_english, is_foreign, normalize_language,
)


# ─── The shapes actually present in the DB ────────────────────


@pytest.mark.parametrize("raw", ["en", "eng", "English", "ENGLISH", " en "])
def test_english_shapes_all_normalize(raw):
    assert normalize_language(raw) == "en"
    assert is_english(raw)
    assert not is_foreign(raw)


@pytest.mark.parametrize("raw,code", [
    ("de", "de"), ("ger", "de"), ("deu", "de"), ("German", "de"),
    ("Deutsch", "de"),
    ("fre", "fr"), ("fra", "fr"), ("French", "fr"),
    ("jpn", "ja"), ("Japanese", "ja"),
    ("spa", "es"), ("Spanish", "es"), ("español", "es"),
    ("heb", "he"), ("Hebrew", "he"),
    ("zho", "zh"), ("chi", "zh"), ("Chinese", "zh"),
])
def test_foreign_shapes_normalize(raw, code):
    assert normalize_language(raw) == code
    assert is_foreign(raw)
    assert not is_english(raw)


def test_hardcover_raw_code_path():
    """Hardcover emits 'English' for eng/en but the RAW ISO 639-2 code
    for everything else — so 'fre' and 'English' must both work."""
    assert normalize_language("English") == "en"
    assert normalize_language("fre") == "fr"


# ─── Locale tags + qualifiers (scraped Kobo values) ───────────


@pytest.mark.parametrize("raw,code", [
    ("en-US", "en"), ("en_GB", "en"), ("pt-BR", "pt"), ("zh-Hans", "zh"),
    ("English (US)", "en"), ("Chinese [Simplified]", "zh"),
])
def test_locale_tags_and_qualifiers(raw, code):
    assert normalize_language(raw) == code


# ─── Fail-open: unknown is NOT foreign ────────────────────────


@pytest.mark.parametrize("raw", [
    None, "", "   ", "Klingon", "xx", "qqq", "und", "mul", "unknown",
    "zxx", "n/a", "(", "()",
])
def test_unknown_values_are_neither_english_nor_foreign(raw):
    assert normalize_language(raw) is None
    assert not is_english(raw)
    assert not is_foreign(raw), (
        "an unparseable language must never be flagged as foreign — "
        "99.3% of unowned rows have no language at all"
    )


def test_is_foreign_is_narrower_than_not_is_english():
    """The distinction the sweep depends on."""
    assert not is_english(None) and not is_foreign(None)
    assert not is_english("Klingon") and not is_foreign("Klingon")
    assert not is_english("de") and is_foreign("de")


# ─── Consistency with the existing OL map ─────────────────────


def test_superset_of_the_openlibrary_map():
    """`sources/openlibrary.py::_OL_LANGUAGE_MAP` only had to cover what
    OL emits; this module has to cover every source, so it must agree
    with that map everywhere the map has an opinion."""
    from app.discovery.sources.openlibrary import _OL_LANGUAGE_MAP
    for three, two in _OL_LANGUAGE_MAP.items():
        assert normalize_language(three) == two, three


def test_round_trips_its_own_output():
    """Normalizing an already-normalized value is a no-op — the backfill
    writes normalized codes and must be safely re-runnable."""
    for raw in ["English", "ger", "jpn", "pt-BR", "es"]:
        once = normalize_language(raw)
        assert normalize_language(once) == once
