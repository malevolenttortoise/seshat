"""
MetaRecord — the common shape every metadata source returns.

Intentionally richer than `BookMetadata` (which comes from the embedded
epub OPF block). Sources populate as many fields as they can; the
enricher merges across sources with first-win semantics on conflicts
and fills nulls from lower-priority sources.

Notes on field semantics:
  - `authors` is a list because multi-author books are common and
    losing co-authors matters for auto-train
  - `confidence` is the title+author similarity score the enricher
    assigned to THIS source's match; used to decide whether to
    accept, fall back to the next source, or tag for manual review
  - `source` is the source name (e.g. "goodreads") so the review UI
    can show which provider each field came from — especially
    important when we eventually let the user edit per-field
  - `cover_url` is the URL the source returned; the cover fetcher
    downloads it separately and stores a path in the review queue
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MetaRecord:
    """One source's answer to "what's this book?" """
    title: str = ""
    authors: list[str] = field(default_factory=list)
    series: Optional[str] = None
    series_index: Optional[float] = None
    description: Optional[str] = None
    isbn: Optional[str] = None
    publisher: Optional[str] = None
    pub_date: Optional[str] = None
    page_count: Optional[int] = None
    language: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    cover_url: Optional[str] = None
    source: str = ""
    source_url: Optional[str] = None
    external_id: Optional[str] = None
    confidence: float = 0.0
    # ── Audiobook-specific ──────────────────────────────────
    # Populated by Audible/Audnexus sources and left None by the
    # ebook-centric sources. Merged through `_merge_records` the
    # same as ebook fields — first-non-None wins.
    narrator: Optional[str] = None
    duration_sec: Optional[float] = None
    asin: Optional[str] = None
    abridged: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "title": self.title,
            "authors": list(self.authors),
            "series": self.series,
            "series_index": self.series_index,
            "description": self.description,
            "isbn": self.isbn,
            "publisher": self.publisher,
            "pub_date": self.pub_date,
            "page_count": self.page_count,
            "language": self.language,
            "tags": list(self.tags),
            "cover_url": self.cover_url,
            "source": self.source,
            "source_url": self.source_url,
            "external_id": self.external_id,
            "confidence": self.confidence,
            "narrator": self.narrator,
            "duration_sec": self.duration_sec,
            "asin": self.asin,
            "abridged": self.abridged,
        }
        # Per-source contribution log, attached by the enricher.
        if hasattr(self, "_source_log"):
            d["source_log"] = self._source_log  # type: ignore[attr-defined]
        # v2.29.0 — observability flag for the cache-first Amazon path.
        # When True, the source served this record from a local cache
        # row (skipping the live amazon.com/s search). Read by the
        # source_log + the enricher's cheap-source short-circuit gate.
        if getattr(self, "_from_cache", False):
            d["from_cache"] = True
        return d
