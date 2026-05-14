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
    _strip_series_decorator,
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


class _TitleAwareSource(MetaSource):
    """Fake source that returns a different result depending on the
    title string it was called with. Used for title-variant fallback
    tests where the raw title fails but the cleaned variant succeeds.
    """

    def __init__(
        self,
        *,
        name: str,
        title_to_result: dict,
    ):
        super().__init__(rate_limit=0)
        self.__class__.name = name
        self.name = name
        self._map = title_to_result
        self.titles_seen: list[str] = []

    async def search_book(self, title, author):
        self.titles_seen.append(title)
        return self._map.get(title)


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
        # Exercise merge behavior across multiple sources. The first
        # source returns a pinned `confidence=1.0` so it's treated as
        # exact-ID (like MAM with a torrent_id). Exact-ID lookups
        # merge AND suppress the short-circuit break (`have_exact_id`
        # becomes True), so the second source also runs and gets a
        # chance to fill in nulls.
        cfg = EnrichmentConfig(enabled=True, accept_confidence=0.6)
        first_rec = MetaRecord(
            title="Book",
            authors=["Author"],
            cover_url="first-cover.jpg",
            source="first",
            confidence=1.0,  # exact-ID: merges without short-circuiting
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

    async def test_description_merge_prefers_longer(self):
        """When a later source returns a longer description, it
        replaces a shorter one from an earlier source. Tier 1 UAT
        bug: MAM returned a ~56-char preview that locked out
        Goodreads' full back-of-book text under first-non-empty.
        """
        cfg = EnrichmentConfig(enabled=True, accept_confidence=0.6)
        preview_text = "Short preview ending in…"  # ~24 chars
        full_text = (
            "A long back-of-book description that runs to "
            "several hundred characters describing the plot "
            "and characters in vivid detail across paragraphs."
        )
        preview_rec = MetaRecord(
            title="Book", authors=["Author"], source="mam",
            description=preview_text,
            confidence=1.0,  # exact-ID: bypasses threshold, doesn't short-circuit
        )
        full_rec = MetaRecord(
            title="Book", authors=["Author"], source="goodreads",
            description=full_text,
        )
        mam_src = _FakeSource(name="mam", result=preview_rec)
        gr_src = _FakeSource(name="goodreads", result=full_rec)
        enricher = MetadataEnricher(cfg, sources=[mam_src, gr_src])

        result = await enricher.enrich(title="Book", author="Author")
        assert result is not None
        # Compare against captured text values rather than the record
        # objects — `_merge_records` mutates `into` in place, so
        # `preview_rec.description` is the 141-char Goodreads text by
        # the time enrich() returns.
        assert result.description == full_text
        assert len(result.description) > len(preview_text)

    async def test_description_merge_keeps_longer_when_new_is_shorter(self):
        """Converse: if the earlier source already has the fuller
        description, a later source's shorter one does not overwrite."""
        cfg = EnrichmentConfig(enabled=True, accept_confidence=0.6)
        long_text = (
            "A long description with lots of detail about the "
            "plot and characters."
        )
        short_text = "Short."
        long_rec = MetaRecord(
            title="Book", authors=["Author"], source="mam",
            description=long_text,
            confidence=1.0,
        )
        short_rec = MetaRecord(
            title="Book", authors=["Author"], source="goodreads",
            description=short_text,
        )
        mam_src = _FakeSource(name="mam", result=long_rec)
        gr_src = _FakeSource(name="goodreads", result=short_rec)
        enricher = MetadataEnricher(cfg, sources=[mam_src, gr_src])

        result = await enricher.enrich(title="Book", author="Author")
        assert result is not None
        assert result.description == long_text

    async def test_title_variant_fallback_on_miss(self):
        """When a source returns no match for the raw title, the
        enricher retries with a series-decorator-stripped variant.
        Tier 1 UAT: MAM's "Monster's Mercy: Book 2" missed on
        Goodreads, but "Monster's Mercy 2" (same title minus the
        "Book" word, keeping the "2") matched cleanly.
        """
        cfg = EnrichmentConfig(enabled=True, accept_confidence=0.6)
        cleaned_match = MetaRecord(
            title="Monster's Mercy 2",
            authors=["Randi Darren"],
            source="goodreads",
        )
        source = _TitleAwareSource(
            name="goodreads",
            title_to_result={
                # Raw title → miss (None)
                "Monster's Mercy: Book 2": None,
                # Cleaned title → match
                "Monster's Mercy 2": cleaned_match,
            },
        )
        enricher = MetadataEnricher(cfg, sources=[source])

        result = await enricher.enrich(
            title="Monster's Mercy: Book 2", author="Randi Darren",
        )
        assert result is not None
        assert result.title == "Monster's Mercy 2"
        # Both titles were tried, in order.
        assert source.titles_seen == [
            "Monster's Mercy: Book 2",
            "Monster's Mercy 2",
        ]

    async def test_no_fallback_when_raw_title_matches(self):
        """Common case: raw title matches on the first try, no
        second HTTP call. Protects against the fallback doubling the
        scraping load on already-clean titles.
        """
        cfg = EnrichmentConfig(enabled=True, accept_confidence=0.6)
        hit = MetaRecord(
            title="Monster's Mercy: Book 2",
            authors=["Randi Darren"],
            source="goodreads",
        )
        source = _TitleAwareSource(
            name="goodreads",
            title_to_result={"Monster's Mercy: Book 2": hit},
        )
        enricher = MetadataEnricher(cfg, sources=[source])

        result = await enricher.enrich(
            title="Monster's Mercy: Book 2", author="Randi Darren",
        )
        assert result is not None
        assert source.titles_seen == ["Monster's Mercy: Book 2"]

    async def test_no_fallback_when_cleaned_equals_raw(self):
        """When the title has no series decorator to strip, the
        cleaned variant is identical to the raw — don't fire the
        fallback for zero benefit.
        """
        cfg = EnrichmentConfig(enabled=True, accept_confidence=0.6)
        source = _TitleAwareSource(
            name="goodreads",
            title_to_result={},  # nothing matches
        )
        enricher = MetadataEnricher(cfg, sources=[source])

        result = await enricher.enrich(
            title="Foundation", author="Isaac Asimov",
        )
        assert result is None
        # Raw title had no decorator; only one call should have
        # been made.
        assert source.titles_seen == ["Foundation"]

    async def test_below_threshold_merge_is_skipped(self):
        """A source that returns a wrong-book match (rescored below
        accept_confidence) must NOT contribute any fields to the
        accumulated record. Before this guard, Kobo was returning
        "Mercy Temple Chronicles: Collection 2" at confidence 0.44
        when Mark searched "Monster's Mercy 2", and its junk
        description leaked into the review card via the merge.
        """
        cfg = EnrichmentConfig(enabled=True, accept_confidence=0.8)
        # First source is exact-ID (like MAM with torrent_id) so the
        # loop doesn't short-circuit and the second source gets a turn.
        exact_rec = MetaRecord(
            title="Monster's Mercy 2",
            authors=["Randi Darren"],
            source="mam",
            confidence=1.0,
        )
        # Second source returns a completely different book at a
        # score_match confidence of ~0.44 (verified by hand:
        # tokens {mercy, 2} intersect with {monster's, mercy, 2};
        # no author overlap → 0.7 * 0.63 + 0.3 * 0 ≈ 0.44).
        wrong_rec = MetaRecord(
            title="Mercy Temple Chronicles Collection 2",
            authors=["Someone Else"],
            description="Junk description from the wrong book",
            source="kobo",
        )
        mam_src = _FakeSource(name="mam", result=exact_rec)
        kobo_src = _FakeSource(name="kobo", result=wrong_rec)
        enricher = MetadataEnricher(cfg, sources=[mam_src, kobo_src])

        result = await enricher.enrich(
            title="Monster's Mercy 2", author="Randi Darren",
        )
        assert result is not None
        # Kobo's junk description did NOT leak.
        assert result.description is None
        assert result.title == "Monster's Mercy 2"
        # source_log surfaces the skip so the UI can render it
        # distinctly from accepted contributions.
        source_log = getattr(result, "_source_log", [])
        kobo_entry = next(
            (e for e in source_log if e["source"] == "kobo"), None
        )
        assert kobo_entry is not None
        assert kobo_entry["status"] == "below_threshold"

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

    async def test_goodreads_skipped_when_soft_blocked(self, monkeypatch):
        """v2.13.0 Stage 6 — when `goodreads_session_state == "soft_blocked"`,
        the enricher must skip the goodreads source entirely (not call
        `search_book`). Without this gate every per-book lookup pays the
        full request → 202 → log → next-source roundtrip even though we
        already know Goodreads is gated.

        Patches `is_soft_blocked` directly rather than writing to the
        runtime-state file — keeps the test fully isolated from
        sibling tests that use fake sources named "goodreads".
        """
        from app.metadata import goodreads_session as gs

        monkeypatch.setattr(gs, "is_soft_blocked", lambda: True)

        cfg = EnrichmentConfig(enabled=True, accept_confidence=0.6)
        rec_from_fallback = MetaRecord(
            title="Book", authors=["A"], source="hardcover",
        )
        # Mirror the production source ordering: goodreads first, then a
        # fallback. The fallback should win because goodreads is skipped.
        gr_fake = _FakeSource(name="goodreads", result=None)
        fallback = _FakeSource(name="hardcover", result=rec_from_fallback)
        enricher = MetadataEnricher(cfg, sources=[gr_fake, fallback])

        result = await enricher.enrich(title="Book", author="A")
        assert result is not None
        assert result.source == "hardcover"
        # Critical: the goodreads source was NEVER called.
        assert gr_fake.call_count == 0
        # And the fallback did run exactly once.
        assert fallback.call_count == 1

    async def test_goodreads_runs_when_session_active(self, monkeypatch):
        """Mirror of the above — when not soft-blocked, the source DOES
        run. Guard against a regression where the new gate accidentally
        always-skips goodreads."""
        from app.metadata import goodreads_session as gs

        monkeypatch.setattr(gs, "is_soft_blocked", lambda: False)

        cfg = EnrichmentConfig(enabled=True, accept_confidence=0.6)
        gr_rec = MetaRecord(title="Book", authors=["A"], source="goodreads")
        gr_fake = _FakeSource(name="goodreads", result=gr_rec)
        enricher = MetadataEnricher(cfg, sources=[gr_fake])

        result = await enricher.enrich(title="Book", author="A")
        assert result is not None
        assert result.source == "goodreads"
        assert gr_fake.call_count == 1

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


class TestStripSeriesDecorator:
    """Unit tests for the `_strip_series_decorator` helper used by
    the enricher's title-variant fallback. Distinct from
    `_clean_title` in scoring.py: this keeps the NUMBER after the
    stripped decorator word, which matters for search-engine queries
    where the number is part of the canonical title."""

    def test_strips_colon_book_n(self):
        # Tier 1 UAT primary case.
        assert _strip_series_decorator(
            "Monster's Mercy: Book 2"
        ) == "Monster's Mercy 2"

    def test_strips_bare_book_n_suffix(self):
        assert _strip_series_decorator(
            "The Triangulum Fold Book 8"
        ) == "The Triangulum Fold 8"

    def test_strips_volume_with_period(self):
        assert _strip_series_decorator(
            "The Triangulum Fold Vol. 8"
        ) == "The Triangulum Fold 8"

    def test_strips_volume_word(self):
        assert _strip_series_decorator(
            "The Triangulum Fold Volume 8"
        ) == "The Triangulum Fold 8"

    def test_strips_part(self):
        assert _strip_series_decorator("Dune — Part 3") == "Dune 3"

    def test_strips_with_hash_prefix(self):
        assert _strip_series_decorator(
            "Monster's Mercy: Book #2"
        ) == "Monster's Mercy 2"

    def test_strips_decimal_index(self):
        assert _strip_series_decorator(
            "Mistborn: Book 3.5"
        ) == "Mistborn 3.5"

    def test_no_change_on_clean_title(self):
        assert _strip_series_decorator("Foundation") == "Foundation"

    def test_no_change_on_bare_number_format(self):
        # "#N" without the word "Book" doesn't match — the regex
        # only fires after a decorator keyword. That's intentional:
        # bare "Dune #2" on Goodreads is already the canonical form.
        assert _strip_series_decorator("Dune #2") == "Dune #2"

    def test_empty_and_none_safe(self):
        assert _strip_series_decorator("") == ""
        assert _strip_series_decorator(None) is None  # type: ignore[arg-type]

    def test_case_insensitive(self):
        assert _strip_series_decorator(
            "Monster's Mercy: BOOK 2"
        ) == "Monster's Mercy 2"
        assert _strip_series_decorator(
            "Monster's Mercy: book 2"
        ) == "Monster's Mercy 2"
