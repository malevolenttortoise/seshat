"""
Tests for the bundled MAM enum fetcher.

The happy path uses the bundled categories.json fallback — no network
I/O. Verifies the flattener produces the expected shape, languages
return a non-empty list, and formats derive from the category tree.
"""
import pytest

from app.mam import enums as mam_enums


@pytest.fixture(autouse=True)
def clear_enum_cache():
    mam_enums._clear_cache()
    yield
    mam_enums._clear_cache()


class TestEnumFetcher:
    async def test_get_categories_from_bundle(self):
        cats = await mam_enums.get_categories(use_mam=False)
        assert len(cats) > 30  # bundled file has ~100+ categories

        ebooks_fantasy = [
            c for c in cats
            if c.main_name == "E-Books" and c.name == "Fantasy"
        ]
        assert len(ebooks_fantasy) == 1
        assert ebooks_fantasy[0].id == "63"
        assert "fantasy" in ebooks_fantasy[0].normalized

    async def test_get_categories_cached(self):
        first = await mam_enums.get_categories(use_mam=False)
        second = await mam_enums.get_categories(use_mam=False)
        assert first is second  # same object, cache hit

    async def test_get_languages(self):
        langs = mam_enums.get_languages()
        assert "english" in langs
        assert "german" in langs
        assert len(langs) > 20

    async def test_get_formats_from_bundle(self):
        formats = await mam_enums.get_formats()
        # Bundled JSON has AudioBooks, E-Books, Musicology, Radio.
        # Normalized forms will lowercase and collapse punctuation.
        assert any("ebook" in f or "e book" in f for f in formats)
        assert any("audiobook" in f for f in formats)

    async def test_refresh_without_token_falls_back_to_bundle(self):
        count = await mam_enums.refresh(token="")
        assert count > 30


class TestCatnameForId:
    def test_resolves_known_ebook_ids(self):
        # The three IDs Mark's prod audit surfaced as numeric strings
        # in grabs.category. Verifies the backfill mapping is correct.
        assert mam_enums.catname_for_id("63") == "Ebooks - Fantasy"
        assert mam_enums.catname_for_id("69") == "Ebooks - Science Fiction"
        # cat 61's bundled name happens to be "Comics/Graphic novels"
        assert mam_enums.catname_for_id("61") == "Ebooks - Comics/Graphic novels"

    def test_resolves_audiobook_id(self):
        # AudioBooks main_name doesn't get the "E-Books"→"Ebooks"
        # substitution — it's already in the live catname form.
        result = mam_enums.catname_for_id("39")
        assert result is not None and result.startswith("AudioBooks - ")

    def test_accepts_int_input(self):
        assert mam_enums.catname_for_id(63) == "Ebooks - Fantasy"

    def test_unknown_id_returns_none(self):
        assert mam_enums.catname_for_id("999999") is None

    def test_empty_input_returns_none(self):
        assert mam_enums.catname_for_id("") is None
        assert mam_enums.catname_for_id("   ") is None

    def test_substitutes_ebooks_form(self):
        # Bundled JSON has "E-Books"; live MAM catname uses "Ebooks".
        # Backfill must match live form so future grabs stay consistent.
        result = mam_enums.catname_for_id("63")
        assert result is not None
        assert "E-Books" not in result
        assert result.startswith("Ebooks ")
