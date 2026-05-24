"""
Quality scoring (v2.26.0 — Bundle A.1).

Generalizes the v2.9.0 format-priority gate (see app/orchestrator/
format_dedup.py) from a 1-axis format model into a multi-axis tiered
quality model that maps cleanly to *arr-style "quality profiles".

The profile per media type is an ordered list of axes; the score of a
candidate is a tuple of tier indices, one per axis. Comparison is
lexicographic: better candidates produce lower tuples. The format axis
is always primary so existing v2.9.0 behavior is preserved when no
numeric-axis data is available.

  audiobook profile (default):
      format → audio_bitrate_kbps → audio_channels
  ebook profile (default):
      format    (no numeric axes — ebook QualitySnapshot rows are
                 correctly empty per the v2.25.0 extraction layer)

Settings shape (per app/config.py DEFAULT_SETTINGS):

  format_priority   (legacy, kept for back-compat) — drives the format
                    axis and the enabled/disabled flag the dedup gate
                    uses to decide allow-vs-hold.
  quality_axes      (new in v2.26.0) — per-media-type ordered axis list
                    used for tiebreaking among same-format candidates.
                    A media type with no entry here gets a format-only
                    profile (preserves v2.9.0 dedup behavior verbatim).

`resolve_profile_from_settings` composes both into a QualityProfile;
that's the single seam every caller goes through (dedup gate in Phase 2,
active replacement in Phase 5, per-library override resolver in Phase 3).

Scoring semantics:
  - Each tier in an axis has an integer rank: 0 = best, len-1 = worst.
  - A candidate's per-axis rank is the index of the tier whose
    `min_value` is the largest value <= the candidate's measurement.
  - Unknown/missing data on an axis ranks one step worse than the
    worst defined tier (len). That way a known-but-low value still
    beats a fully-unknown one, but a missing axis never silently wins
    against a measured-low sibling.
  - Format axis is the same: 0 = highest-priority tier. Unknown
    formats rank as len (after the last enumerated format).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.quality.extract import QualitySnapshot

_log = logging.getLogger("seshat.quality.scoring")


# ─── Profile data model ───────────────────────────────────────


@dataclass(frozen=True)
class FormatTier:
    """One format tier in a profile's format axis.

    `enabled` mirrors the v2.9.0 `format_priority` semantics: an
    enabled format always grabs (when nothing higher is racing);
    a disabled format holds for `format_dedup_hold_seconds` so a
    higher-priority sibling has a chance to arrive.
    """
    fmt: str
    enabled: bool = True


@dataclass(frozen=True)
class NumericTier:
    """One tier in a numeric axis.

    `min_value` is the inclusive lower bound: a candidate falls into
    this tier if its measured value is >= min_value AND less than the
    min_value of the next-higher tier. Defining tiers strictly by
    `min_value` keeps the data plain JSON; comparison is implicit in
    list order.
    """
    label: str
    min_value: int


@dataclass(frozen=True)
class NumericAxis:
    """A numeric axis with ordered tiers, highest quality first.

    `axis_name` must match a field name on QualitySnapshot (e.g.
    'audio_bitrate_kbps', 'audio_channels').
    """
    axis_name: str
    tiers: tuple[NumericTier, ...]


@dataclass(frozen=True)
class QualityProfile:
    """Composed profile for one media type.

    Built by `resolve_profile_from_settings`. The format axis is always
    the primary scoring axis; numeric axes are tiebreakers in declared
    order. An empty `numeric_axes` tuple yields format-only scoring
    (the v2.9.0 behavior).
    """
    media_type: str
    format_tiers: tuple[FormatTier, ...]
    numeric_axes: tuple[NumericAxis, ...] = field(default_factory=tuple)


# ─── Defaults ─────────────────────────────────────────────────


# v2.26.0 default numeric-axis tiers. Format tiers stay in the legacy
# `format_priority` setting (no change to that data shape). Mark can
# override per-library via `quality_axes_overrides_by_slug` (Phase 3).
#
# Audiobook tiers chosen to align with common MAM rip qualities — most
# modern rips land at 128 (Audible AAC), older rips at 64-96, lossless
# rips at 320+. Channels axis is binary in practice (stereo vs mono),
# but expressed as min_value tiers so the same code path handles both.
#
# Ebook has no numeric axes by default — file count and size aren't
# reliable quality signals (large PDFs can be scans; small EPUBs can
# be high quality). If we ever expose ebook-specific axes (page count,
# OCR'd vs retail), this is where they'd land.
DEFAULT_QUALITY_AXES: dict[str, list[dict]] = {
    "audiobook": [
        {
            "axis": "audio_bitrate_kbps",
            "tiers": [
                {"label": "320+ kbps", "min_value": 320},
                {"label": "192+ kbps", "min_value": 192},
                {"label": "128+ kbps", "min_value": 128},
                {"label": "64+ kbps",  "min_value": 64},
                {"label": "<64 kbps",  "min_value": 0},
            ],
        },
        {
            "axis": "audio_channels",
            "tiers": [
                {"label": "Stereo+", "min_value": 2},
                {"label": "Mono",    "min_value": 1},
            ],
        },
    ],
    "ebook": [],
}


# ─── Resolver ─────────────────────────────────────────────────


def resolve_profile_for_library(
    media_type: str,
    library_slug: Optional[str],
    settings: dict,
) -> Optional[QualityProfile]:
    """Per-library variant of `resolve_profile_from_settings` (A.3).

    Layers `quality_profile_overrides_by_slug[<slug>]` over the global
    `format_priority` + `quality_axes` settings. Override semantics are
    whole-list replacement per (media_type, key): if a per-library
    override defines `format_priority.audiobook`, the entire global
    audiobook list is replaced; numeric axes for that media type still
    fall through to global unless ALSO overridden.

    library_slug=None or a slug with no override entry yields the same
    profile as `resolve_profile_from_settings(media_type, settings)`.

    v2.26.0 scoping: per-library overrides apply during Phase 5 active
    replacement (where the owned book's library is known) and the
    Settings UI surface. The announce-time dedup gate keeps using the
    global profile because the destination library isn't decided until
    after the grab.
    """
    if not library_slug:
        return resolve_profile_from_settings(media_type, settings)

    overrides_root = settings.get("quality_profile_overrides_by_slug") or {}
    override = overrides_root.get(library_slug) or {}
    if not override:
        return resolve_profile_from_settings(media_type, settings)

    merged = {
        "format_priority": dict(settings.get("format_priority") or {}),
        "quality_axes":    dict(settings.get("quality_axes") or {}),
    }

    fp_override = (override.get("format_priority") or {}).get(media_type)
    if fp_override is not None:
        merged["format_priority"][media_type] = fp_override

    qa_override = (override.get("quality_axes") or {}).get(media_type)
    if qa_override is not None:
        merged["quality_axes"][media_type] = qa_override

    return resolve_profile_from_settings(media_type, merged)


def resolve_profile_from_settings(
    media_type: str,
    settings: dict,
) -> Optional[QualityProfile]:
    """Compose a QualityProfile for `media_type` from a settings dict.

    Reads two keys:
      - `format_priority`  → format axis (legacy v2.9.0 shape preserved)
      - `quality_axes`     → numeric axes (new in v2.26.0)

    Returns None if `format_priority` has no entry for this media type —
    the caller treats None as "fall through to allow" the same way
    `evaluate_format_dedup` does today.
    """
    fmt_lists = settings.get("format_priority") or {}
    raw_formats = fmt_lists.get(media_type)
    if raw_formats is None:
        return None

    format_tiers = tuple(
        FormatTier(
            fmt=str(entry.get("fmt") or "").strip().lower(),
            enabled=bool(entry.get("enabled")),
        )
        for entry in raw_formats
        if entry.get("fmt")
    )

    axes_cfg = settings.get("quality_axes") or DEFAULT_QUALITY_AXES
    raw_axes = axes_cfg.get(media_type) or []
    numeric_axes = tuple(
        NumericAxis(
            axis_name=str(ax.get("axis") or "").strip(),
            tiers=tuple(
                NumericTier(
                    label=str(t.get("label") or "").strip(),
                    min_value=int(t.get("min_value") or 0),
                )
                for t in (ax.get("tiers") or [])
            ),
        )
        for ax in raw_axes
        if ax.get("axis") and ax.get("tiers")
    )

    return QualityProfile(
        media_type=media_type,
        format_tiers=format_tiers,
        numeric_axes=numeric_axes,
    )


# ─── Format-axis helpers (used by dedup gate) ────────────────


def format_rank(profile: QualityProfile, fmt: str) -> int:
    """Rank `fmt` against the profile's format tier list.

    Returns 0 for the highest-priority tier, len-1 for the lowest,
    and len for "format not in the profile" (unknown formats sort
    worst but don't crash the comparator).
    """
    if not fmt:
        return len(profile.format_tiers)
    target = fmt.strip().lower()
    for i, tier in enumerate(profile.format_tiers):
        if tier.fmt == target:
            return i
    return len(profile.format_tiers)


def format_is_enabled(profile: QualityProfile, fmt: str) -> bool:
    """True iff `fmt` is in the profile's format tiers AND marked enabled.

    Unknown formats return False — the dedup gate's existing
    `format_dedup_unknown_fmt` fall-through handles those before this
    helper is consulted, but the False return keeps the helper safe
    for any future caller.
    """
    if not fmt:
        return False
    target = fmt.strip().lower()
    for tier in profile.format_tiers:
        if tier.fmt == target:
            return tier.enabled
    return False


# ─── Numeric-axis ranking ────────────────────────────────────


def _numeric_rank(axis: NumericAxis, value: Optional[int]) -> int:
    """Rank a candidate value against a numeric axis.

    Tier list is ordered highest-quality first; each tier's min_value
    is its inclusive lower bound. The candidate's rank is the index
    of the highest-quality tier whose min_value it meets.

    Missing/None values rank one step past the worst defined tier so
    a known-low value still beats a fully-unknown one.
    """
    if value is None or not isinstance(value, (int, float)):
        return len(axis.tiers)
    v = int(value)
    for i, tier in enumerate(axis.tiers):
        if v >= tier.min_value:
            return i
    return len(axis.tiers)


# ─── Top-level scorer ────────────────────────────────────────


def score_quality(
    *,
    profile: QualityProfile,
    fmt: str,
    snapshot: Optional[QualitySnapshot] = None,
) -> tuple[int, ...]:
    """Score a candidate against a profile. Lower tuple = higher quality.

    Tuple layout: (format_rank, *numeric_axis_ranks).

    `snapshot` is the QualitySnapshot for the candidate (Phase 5
    active-replacement decisions pass real data; Phase 2 dedup-time
    callers pass None when only the format is known from the IRC
    announce). When `snapshot` is None, every numeric axis ranks as
    "unknown" — comparison still works, it just falls back to pure
    format-priority ordering.

    The format-only fallback is by design: it preserves v2.9.0 dedup
    semantics on announces that don't have quality data yet. Phase 5
    explicitly pre-fetches mediainfo before scoring for replacement so
    numeric axes contribute there.
    """
    ranks: list[int] = [format_rank(profile, fmt)]
    for axis in profile.numeric_axes:
        value = getattr(snapshot, axis.axis_name, None) if snapshot else None
        ranks.append(_numeric_rank(axis, value))
    return tuple(ranks)
