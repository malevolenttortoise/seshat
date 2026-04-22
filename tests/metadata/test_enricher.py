"""
Enricher tests.

Uses fake MetaSource instances (subclasses with canned search_book
results) to drive the enricher through the short-circuit, merge,
timeout, and failure paths without making any HTTP calls.
"""
import asyncio
from typing import Optional

import pytest

from app.metadata.enricher import (
    EnrichmentConfig,
    MetadataEnricher,
    _clean_audiobook_title,
)
from app.metadata.record import MetaRecord
from app.metadata.sources.base import MetaSource


class _FakeSource(MetaSource):
    def __init__(self, *, name: str, result: Optional[MetaRecord] = None):
        super().__init__(rate_limit=0)
        self.__class__.name = name  # one-off; fine for test subclasses
        self.name = name
        self._result = result
        self.call_count = 0

    async def search_book(self, title, author):
        self.call_count += 1
        return self._result


class _SlowSource(MetaSource):
    name = "slow"

    def __init__(self):
        super().__init__(rate_limit=0)

    async def search_book(self, title, author):
        await asyncio.sleep(10)  # intentionally longer than timeout
        return None


class _ExplodingSource(MetaSource):
    name = "broken"

    def __init__(self):
        super().__init__(rate_limit=0)

    async def search_book(self, title, author):
        raise RuntimeError("simulated scraper failure")


@pytest.fixture
def accept_low_cfg():
    return EnrichmentConfig(enabled=True, accept_confidence=0.6)


class TestEnricher:
    async def test_disabled_returns_none(self):
        cfg = EnrichmentConfig(enabled=False)
        enricher = MetadataEnricher(cfg, sources=[])
        assert await enricher.enrich(title="T", author="A") is None

    async def test_high_confidence_short_circuits(self, accept_low_cfg):
        good = MetaRecord(
            title="The Way of Kings",
            authors=["Brandon Sanderson"],
            description="High confidence",
            source="goodreads",
        )
        second = _FakeSource(
            name="second",
            result=MetaRecord(title="Other", authors=["X"], source="second"),
        )
        first = _FakeSource(name="first", result=good)
        enricher = MetadataEnricher(accept_low_cfg, sources=[first, second])

        result = await enricher.enrich(
            title="The Way of Kings", author="Brandon Sanderson"
        )
        assert result is not None
        assert result.description == "High confidence"
        assert first.call_count == 1
        assert second.call_count == 0  # short-circuited

    async def test_merges_nulls_from_later_sources(self):
        # accept_confidence 1.01 ensures neither fake record short-circuits
        # the loop, so the enricher visits every source and merges their
        # outputs. (Real sources would score <1 and merge naturally.)
        cfg = EnrichmentConfig(enabled=True, accept_confidence=1.01)
        first_rec = MetaRecord(
            title="Book",
            authors=["Author"],
            cover_url="first-cover.jpg",
            source="first",
        )
        second_rec = MetaRecord(
            title="Book",
            authors=["Author"],
            description="Back-of-book blurb",
            page_count=500,
            isbn="9781234567890",
            source="second",
        )
        first = _FakeSource(name="first", result=first_rec)
        second = _FakeSource(name="second", result=second_rec)
        enricher = MetadataEnricher(cfg, sources=[first, second])

        result = await enricher.enrich(title="Book", author="Author")
        assert result is not None
        assert result.description == "Back-of-book blurb"
        assert result.page_count == 500
        assert result.isbn == "9781234567890"
        assert result.cover_url == "first-cover.jpg"  # first wins

    async def test_timeout_falls_through(self, accept_low_cfg):
        cfg = EnrichmentConfig(
            enabled=True, accept_confidence=0.6, per_source_timeout=0.05
        )
        fallback_rec = MetaRecord(
            title="The Way of Kings",
            authors=["Brandon Sanderson"],
            source="fallback",
        )
        slow = _SlowSource()
        fallback = _FakeSource(name="fallback", result=fallback_rec)
        enricher = MetadataEnricher(cfg, sources=[slow, fallback])

        result = await enricher.enrich(
            title="The Way of Kings", author="Brandon Sanderson"
        )
        assert result is not None
        assert result.source == "fallback"

    async def test_source_exception_is_swallowed(self):
        cfg = EnrichmentConfig(enabled=True, accept_confidence=0.6)
        rec = MetaRecord(
            title="Book",
            authors=["Author"],
            source="good",
        )
        broken = _ExplodingSource()
        good = _FakeSource(name="good", result=rec)
        enricher = MetadataEnricher(cfg, sources=[broken, good])

        result = await enricher.enrich(title="Book", author="Author")
        assert result is not None
        assert result.source == "good"

    async def test_all_none_returns_none(self):
        cfg = EnrichmentConfig(enabled=True)
        a = _FakeSource(name="a", result=None)
        b = _FakeSource(name="b", result=None)
        enricher = MetadataEnricher(cfg, sources=[a, b])
        assert await enricher.enrich(title="X", author="Y") is None
        assert a.call_count == 1
        assert b.call_count == 1

    async def test_audiobook_flag_swaps_source_list(self):
        """Phase 6: `audiobook=True` routes to `audiobook_sources` so the
        audiobook-specific priority (Audible leads; Audnexus is
        hydrated internally by AudibleSource) actually runs, and the
        ebook sources aren't called."""
        cfg = EnrichmentConfig(enabled=True, accept_confidence=0.6)
        ebook_rec = MetaRecord(
            title="Book", authors=["A"], source="goodreads",
        )
        audio_rec = MetaRecord(
            title="Book", authors=["A"], source="audible",
            narrator="Michael Kramer", duration_sec=36000, asin="B00XYZ",
        )
        ebook_src = _FakeSource(name="goodreads", result=ebook_rec)
        audio_src = _FakeSource(name="audible", result=audio_rec)
        enricher = MetadataEnricher(
            cfg, sources=[ebook_src], audiobook_sources=[audio_src],
        )

        ebook_result = await enricher.enrich(
            title="Book", author="A", audiobook=False,
        )
        assert ebook_result is not None
        assert ebook_result.source == "goodreads"
        assert ebook_src.call_count == 1
        assert audio_src.call_count == 0

        audio_result = await enricher.enrich(
            title="Book", author="A", audiobook=True,
        )
        assert audio_result is not None
        assert audio_result.source == "audible"
        assert audio_result.narrator == "Michael Kramer"
        assert audio_result.asin == "B00XYZ"
        # Ebook source wasn't consulted on the audiobook call.
        assert ebook_src.call_count == 1


class TestCleanAudiobookTitle:
    """MAM filenames carry publisher decorations that Audible's catalog
    doesn't. The cleaner strips them so title searches hit."""

    def test_strips_trailing_bracket(self):
        assert _clean_audiobook_title("Halo: Empty Throne [Halo 36]") == "Halo: Empty Throne"

    def test_strips_format_paren(self):
        assert _clean_audiobook_title("The Way of Kings (Unabridged)") == "The Way of Kings"
        assert _clean_audiobook_title("Dune (Audiobook)") == "Dune"
        assert _clean_audiobook_title("Mistborn (Abridged)") == "Mistborn"

    def test_strips_volume_tail(self):
        assert _clean_audiobook_title("Mistborn, Book 1") == "Mistborn"
        assert _clean_audiobook_title("The Stormlight Archive: Book 3") == "The Stormlight Archive"
        assert _clean_audiobook_title("Some Series, Vol. 2") == "Some Series"

    def test_strips_stacked_decorations(self):
        """'Halo: Empty Throne [Halo 36] (Unabridged)' collapses fully."""
        result = _clean_audiobook_title(
            "Halo: Empty Throne [Halo 36] (Unabridged)"
        )
        assert result == "Halo: Empty Throne"

    def test_empty_and_none_safe(self):
        assert _clean_audiobook_title("") == ""
        assert _clean_audiobook_title(None) is None

    def test_preserves_clean_title(self):
        """No-op on titles that don't carry decorations."""
        assert _clean_audiobook_title("Project Hail Mary") == "Project Hail Mary"

    def test_does_not_strip_midsentence_brackets(self):
        """Only TRAILING brackets get stripped — internal ones are
        part of the title. "2001: A Space Odyssey" with an inner
        volume marker shouldn't lose its body."""
        # This one has no trailing bracket so it's untouched.
        assert _clean_audiobook_title(
            "A Novel [Remastered Edition] with Extras"
        ) == "A Novel [Remastered Edition] with Extras"
