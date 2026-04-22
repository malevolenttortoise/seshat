"""
Metadata enricher — orchestrates scraper sources and merges results.

Called from the pipeline's prepare-book phase with the announce title
+ author blob. Walks the configured source priority list, calling
`search_book` on each until we either:
  - find a source whose result scores above the accept threshold
    (we stop there and return it), OR
  - exhaust the list and return whatever we gathered

Merge semantics across multiple sources:
  - First non-None value wins for each field (the highest-priority
    source that had data for a given field takes precedence)
  - Confidence becomes the MAX confidence seen across all sources
    (a strong match from any source is enough)
  - Cover URL is preferred from the highest-confidence source so we
    don't accidentally pick Goodreads' tiny thumbnail over Amazon's
    full-size cover

Per-source timeout + fail-safe: a stuck scraper never blocks the
pipeline. Each `search_book` is wrapped in `asyncio.wait_for()` with
the configured timeout (default 15s). Exceptions and timeouts are
logged and treated as "this source returned nothing" — the loop
advances to the next provider.

Feature flag: the pipeline only invokes the enricher when
`metadata_enrichment_enabled` is True in settings. Default is
False so existing deployments don't suddenly start making outbound
HTTP calls to every scraper on every book.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from app.metadata.record import MetaRecord
from app.metadata.scoring import score_match
from app.metadata.sources.base import MetaSource
from app.metadata.sources.goodreads import GoodreadsSource
from app.metadata.sources.amazon import AmazonSource
from app.metadata.sources.audible import AudibleSource
from app.metadata.sources.google_books import GoogleBooksSource
from app.metadata.sources.hardcover import HardcoverSource
from app.metadata.sources.ibdb import IbdbSource
from app.metadata.sources.kobo import KoboSource
from app.metadata.sources.mam_search import MamSearchSource

_log = logging.getLogger("seshat.metadata.enricher")

# Default provider priority. MAM runs first (free, authoritative,
# uses the cached torrent_info response). External scrapers follow
# in the user's spec order (#21) for fields MAM doesn't carry
# (covers, page count, pub date, ISBN).
#
# Audible lands after the ebook-centric sources because it
# contributes zero signal for ebook searches (no audiobook-side
# hit). For audiobook grabs the routing layer puts it first via
# `DEFAULT_AUDIOBOOK_PRIORITY` instead. Audible internally hydrates
# every hit through Audnexus (see app/metadata/sources/audible.py),
# so Audnexus is not listed here as its own standalone source —
# it has no title/author search endpoint and would always log
# "no match" if registered independently.
DEFAULT_PRIORITY: tuple[str, ...] = (
    "mam",
    "goodreads",
    "amazon",
    "hardcover",
    "kobo",
    "ibdb",
    "google_books",
    "audible",
)

# Audiobook-first priority. Used when the pipeline routes a grab
# based on MAM category `audiobooks …` or an audio file extension
# (.m4b/.mp3/.m4a). MAM still runs first because it's free and
# often has the category/tag metadata no external source carries.
# Audible owns the Audnexus chain internally.
DEFAULT_AUDIOBOOK_PRIORITY: tuple[str, ...] = (
    "mam",
    "audible",
    "goodreads",
    "hardcover",
    "google_books",
)

# Audiobook title sanitization. MAM filenames often carry
# `[Series N]` / `(Unabridged)` / `, Book 5` suffixes that the
# Audible catalog doesn't, which breaks fuzzy title matching.
# Stripped only for audiobook enrichment calls — the canonical
# title stored on the review queue is untouched.
_AB_BRACKET_TAIL_RX = re.compile(r"\s*\[[^\]]*\]\s*$")
_AB_FORMAT_PAREN_RX = re.compile(
    r"\s*\((unabridged|abridged|audio[\s-]?book|audible)\)\s*$",
    re.IGNORECASE,
)
_AB_VOLUME_TAIL_RX = re.compile(
    r"\s*[:,]\s*(book|vol\.?|volume|part)\s+\d+(?:\.\d+)?\s*$",
    re.IGNORECASE,
)


def _clean_audiobook_title(title: str) -> str:
    """Normalize an audiobook title for external catalog search.

    Runs when `enrich(audiobook=True)` is invoked — strips publisher
    decorations that the Audible catalog doesn't reproduce. Ordering
    matters: remove outer wrappers first, then the volume tail so
    "Halo: Empty Throne [Halo 36] (Unabridged)" collapses cleanly to
    "Halo: Empty Throne".

    The cleaned string is what we hand to each audiobook source's
    `search_book(title, author)` — the enricher never writes it back
    onto the MetaRecord or review-queue row.
    """
    if not title:
        return title
    s = title
    # Re-run each pass up to 3 times so stacked decorations unwind —
    # "Title [X] [Y]" collapses fully instead of leaving "Title [X]".
    for _ in range(3):
        s_new = _AB_BRACKET_TAIL_RX.sub("", s)
        s_new = _AB_FORMAT_PAREN_RX.sub("", s_new)
        s_new = _AB_VOLUME_TAIL_RX.sub("", s_new)
        if s_new == s:
            break
        s = s_new
    return s.strip()


# Accept threshold — records with confidence >= this are considered
# good enough to stop searching. Tuned so exact and near-exact
# matches short-circuit but lower matches fall through.
_ACCEPT_CONFIDENCE = 0.8

# Per-source timeout in seconds. Protects the pipeline from a single
# stuck scraper. Matches CWA's documented default.
_PER_SOURCE_TIMEOUT = 15.0

# Global wall-clock budget for a single enrich() call across all
# sources. Worst case under per-source defaults: 7 sources × 15s =
# 105s if every source individually times out. The 60s budget caps
# that at roughly half — enough headroom for normal scans while
# keeping the pipeline responsive on a stuck-source day. The budget
# is also used to clamp each individual per-source wait so a slow
# late-stage source can't single-handedly blow the cap.
_PER_BOOK_BUDGET = 60.0


@dataclass
class EnrichmentConfig:
    """Runtime knobs for the enricher.

    Built from settings.json in `main.py`. Kept distinct from the
    source instances themselves so tests can construct an enricher
    with a fixed config without reading settings.

    Two priority lists: `priority` is used for ebook grabs,
    `audiobook_priority` for audiobook grabs. The pipeline picks
    which list to use per-grab via the `audiobook=` kwarg on
    `MetadataEnricher.enrich()`.
    """

    enabled: bool = False
    priority: tuple[str, ...] = DEFAULT_PRIORITY
    audiobook_priority: tuple[str, ...] = DEFAULT_AUDIOBOOK_PRIORITY
    per_source_timeout: float = _PER_SOURCE_TIMEOUT
    per_book_budget: float = _PER_BOOK_BUDGET
    accept_confidence: float = _ACCEPT_CONFIDENCE
    disabled_sources: frozenset[str] = field(default_factory=frozenset)


_SOURCE_REGISTRY: dict[str, type[MetaSource]] = {
    MamSearchSource.name: MamSearchSource,
    GoodreadsSource.name: GoodreadsSource,
    AmazonSource.name: AmazonSource,
    HardcoverSource.name: HardcoverSource,
    KoboSource.name: KoboSource,
    IbdbSource.name: IbdbSource,
    GoogleBooksSource.name: GoogleBooksSource,
    AudibleSource.name: AudibleSource,
    # Audnexus is NOT registered — it has no standalone title/author
    # search. AudibleSource instantiates its own AudnexusSource
    # internally to hydrate catalog hits, and the pipeline calls
    # `AudnexusSource.fetch_by_asin()` directly for m4b ASIN lookups.
}


class MetadataEnricher:
    """Coordinates metadata lookup across the configured sources."""

    def __init__(
        self,
        config: EnrichmentConfig,
        *,
        sources: Optional[list[MetaSource]] = None,
        audiobook_sources: Optional[list[MetaSource]] = None,
        hardcover_api_key: str = "",
        audible_region: str = "us",
    ):
        self.config = config
        if sources is not None:
            # Test / custom override.
            self._sources = sources
        else:
            self._sources = _build_default_sources(
                config.priority, config,
                hardcover_api_key=hardcover_api_key,
                audible_region=audible_region,
            )
        if audiobook_sources is not None:
            self._audiobook_sources = audiobook_sources
        else:
            self._audiobook_sources = _build_default_sources(
                config.audiobook_priority, config,
                hardcover_api_key=hardcover_api_key,
                audible_region=audible_region,
            )

    async def enrich(
        self,
        *,
        title: str,
        author: str,
        mam_torrent_id: str = "",
        mam_token: str = "",
        audiobook: bool = False,
    ) -> Optional[MetaRecord]:
        """Run the priority list and return the best merged record.

        When `mam_torrent_id` and `mam_token` are provided, the MAM
        source gets an exact-ID lookup (confidence=1.0) for free —
        it reuses the cached torrent_info from the policy engine.
        External scrapers then fill any gaps (covers, page count, etc.)

        `audiobook=True` switches the priority list to
        `config.audiobook_priority` (Audible leads, hydrating its
        hits through Audnexus internally) so narrator / duration /
        ASIN come from the audiobook-aware sources first.

        Returns None when every source returned None or errored.
        """
        if not self.config.enabled:
            return None
        if not title and not author:
            return None

        # Audiobook title cleanup. Publisher decorations on MAM
        # filenames ("Halo: Empty Throne [Halo 36]", "(Unabridged)")
        # don't match Audible's catalog title. Strip them before the
        # loop so every source gets the clean form — the canonical
        # title in `metadata.title` is untouched.
        if audiobook:
            cleaned = _clean_audiobook_title(title)
            if cleaned != title:
                _log.debug(
                    "enricher: audiobook title cleaned %r → %r for search",
                    title, cleaned,
                )
                title = cleaned

        # Build the source list, injecting a MAM source with the
        # torrent ID if available. This is per-call because the
        # torrent ID changes for each book.
        base_sources = self._audiobook_sources if audiobook else self._sources
        sources = list(base_sources)
        if mam_torrent_id and mam_token:
            mam_src = MamSearchSource(
                mam_token=mam_token, torrent_id=mam_torrent_id
            )
            # Insert at the front so MAM runs first.
            sources = [mam_src] + [s for s in sources if s.name != "mam"]

        merged: Optional[MetaRecord] = None
        source_log: list[dict] = []  # per-source contributions
        have_exact_id = False  # MAM exact-ID gives us the match; keep querying for supplemental data
        known_series = ""  # populated by MAM exact-ID for series-aware scoring
        # Wall-clock start for the global per-book budget. Enforced
        # before each source so a stuck source can't blow the cap and
        # threaded into _safe_search so each per-source timeout is
        # clamped to the remaining budget.
        budget_started_at = asyncio.get_event_loop().time()

        for src in sources:
            elapsed = asyncio.get_event_loop().time() - budget_started_at
            remaining = self.config.per_book_budget - elapsed
            if remaining <= 0:
                _log.info(
                    "enricher: per-book budget (%.0fs) exceeded — skipping "
                    "remaining sources for %r: %s",
                    self.config.per_book_budget, title,
                    [s.name for s in sources[sources.index(src):]],
                )
                source_log.append({"source": src.name, "confidence": None, "status": "budget_exceeded"})
                break
            result = await self._safe_search(
                src, title=title, author=author, max_wait=remaining,
            )
            if result is None:
                # Emit at INFO so the log stream shows the full chain —
                # otherwise sources that fail to match for a given book
                # are invisible and the user can't tell whether they
                # were queried at all.
                _log.info(
                    "enricher: %s → no match (title=%r)", src.name, title,
                )
                source_log.append({"source": src.name, "confidence": None, "status": "no_result"})
                continue
            # Exact-ID lookups (like MAM with torrent_id) already set
            # confidence=1.0. Only re-score with Jaccard when the source
            # did a fuzzy text search (confidence not already pinned).
            is_exact = result.confidence >= 1.0
            if is_exact and result.series:
                known_series = result.series
            if not is_exact:
                result.confidence = score_match(
                    record_title=result.title or title,
                    record_authors=result.authors or [],
                    search_title=title,
                    search_authors=author,
                    known_series=known_series,
                )
            _log.info(
                "enricher: %s → confidence %.2f (title=%r)",
                src.name, result.confidence, result.title,
            )
            # Track what each source contributed.
            source_log.append({
                "source": src.name,
                "confidence": round(result.confidence, 2),
                "status": "matched",
                "cover_url": result.cover_url or None,
            })
            merged = _merge_records(merged, result)
            if is_exact:
                have_exact_id = True
                continue  # we have the match; keep querying for covers/pages/etc.
            if result.confidence >= self.config.accept_confidence and not have_exact_id:
                break  # good enough from a fuzzy source; stop here

        if merged is not None:
            merged._source_log = source_log  # type: ignore[attr-defined]
        return merged

    async def _safe_search(
        self, source: MetaSource, *, title: str, author: str,
        max_wait: Optional[float] = None,
    ) -> Optional[MetaRecord]:
        # Clamp per-source timeout to the remaining global budget so a
        # slow late-stage source can't single-handedly blow the per-book
        # cap. `max_wait=None` means "no global cap" (test code path).
        timeout = self.config.per_source_timeout
        if max_wait is not None:
            timeout = min(timeout, max(0.5, max_wait))
        try:
            return await asyncio.wait_for(
                source.search_book(title, author),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _log.warning(
                "enricher: %s timed out after %.0fs",
                source.name, timeout,
            )
            return None
        except Exception:
            _log.exception("enricher: %s raised", source.name)
            return None

    async def aclose(self) -> None:
        # Close both source lists. A source can appear in both the
        # ebook and audiobook priorities (audible, goodreads, etc.) —
        # dedupe by identity so `close()` only fires once per instance.
        seen = set()
        for src in list(self._sources) + list(self._audiobook_sources):
            if id(src) in seen:
                continue
            seen.add(id(src))
            try:
                await src.close()
            except Exception:
                pass


def _build_default_sources(
    priority: tuple[str, ...],
    config: EnrichmentConfig,
    *,
    hardcover_api_key: str = "",
    audible_region: str = "us",
) -> list[MetaSource]:
    """Instantiate the priority-ordered source list.

    `priority` is the order to walk; `config.disabled_sources` filters
    entries the user has explicitly switched off. The same enricher
    instance builds BOTH an ebook source list (from `config.priority`)
    and an audiobook source list (from `config.audiobook_priority`) —
    callers pass the appropriate tuple.

    `hardcover_api_key` is plumbed through from `_build_dispatcher`'s
    resolved_secrets — sourced from the encrypted store rather than
    `settings.json` (which is blanked after the Sprint 6 migration).
    A missing key leaves Hardcover registered but unauthenticated,
    in which case it returns None silently on every search.

    `audible_region` controls which Audible TLD the catalog search
    hits, and propagates into the Audnexus hydration call Audible
    makes internally. User-visible via the `audible_region` setting;
    defaults to "us".
    """
    if not hardcover_api_key:
        _log.debug(
            "enricher: no Hardcover API key provided; Hardcover source "
            "will return no results"
        )

    out: list[MetaSource] = []
    for name in priority:
        if name in config.disabled_sources:
            continue
        cls = _SOURCE_REGISTRY.get(name)
        if cls is None:
            _log.warning("enricher: unknown source %r in priority list", name)
            continue
        if name == "hardcover" and hardcover_api_key:
            out.append(cls(api_key=hardcover_api_key))
        elif name == "audible":
            out.append(cls(region=audible_region))
        else:
            out.append(cls())
    return out


def _merge_records(
    into: Optional[MetaRecord], new: MetaRecord
) -> MetaRecord:
    """First-non-None-wins merge.

    `into` is the accumulator (highest-priority so far); `new` is
    the next source's result. When a field is already populated on
    `into`, keep it. Confidence takes the max so we can stop once
    any source is above the threshold.
    """
    if into is None:
        return new

    def _pick(a, b):
        return a if a not in (None, "", []) else b

    into.title = _pick(into.title, new.title)
    if not into.authors:
        into.authors = list(new.authors)
    into.series = _pick(into.series, new.series)
    into.series_index = _pick(into.series_index, new.series_index)
    into.description = _pick(into.description, new.description)
    into.isbn = _pick(into.isbn, new.isbn)
    into.publisher = _pick(into.publisher, new.publisher)
    into.pub_date = _pick(into.pub_date, new.pub_date)
    into.page_count = _pick(into.page_count, new.page_count)
    into.language = _pick(into.language, new.language)
    if not into.tags:
        into.tags = list(new.tags)
    # Cover preference: stick with the current cover (higher-priority
    # source) unless it's empty. Highest-priority non-empty wins.
    into.cover_url = _pick(into.cover_url, new.cover_url)
    # Audiobook-specific fields: ebook sources leave these None, so
    # first-non-None wins pulls them through from whichever audiobook
    # source supplies them. `abridged` specifically requires the
    # None check because False is a valid, informative value.
    into.narrator = _pick(into.narrator, new.narrator)
    into.duration_sec = _pick(into.duration_sec, new.duration_sec)
    into.asin = _pick(into.asin, new.asin)
    if into.abridged is None:
        into.abridged = new.abridged
    # Confidence is a max over all sources — any strong match boosts
    # our belief that the merged record is correct.
    into.confidence = max(into.confidence, new.confidence)
    return into
