"""
Tests for app/quality/scoring.py — Bundle A.1 quality scoring.

Coverage:
  - resolve_profile_from_settings (legacy compat + new quality_axes)
  - format_rank / format_is_enabled (format-axis helpers)
  - _numeric_rank (tier-bucket math, missing-value semantics)
  - score_quality (tuple shape, lexicographic ordering, sparse data)

These tests pin both the v2.26.0 default profile and the back-compat
path that lets v2.9.0 `format_priority`-only configs keep working.
"""
from __future__ import annotations

from app.quality.extract import QualitySnapshot
from app.quality.scoring import (
    DEFAULT_QUALITY_AXES,
    FormatTier,
    NumericAxis,
    NumericTier,
    QualityProfile,
    _numeric_rank,
    format_is_enabled,
    format_rank,
    resolve_profile_for_library,
    resolve_profile_from_settings,
    score_quality,
)


# ─── Fixtures ────────────────────────────────────────────────


_FORMAT_PRIORITY_DEFAULTS = {
    "ebook": [
        {"fmt": "epub", "enabled": True},
        {"fmt": "azw3", "enabled": False},
        {"fmt": "mobi", "enabled": False},
        {"fmt": "pdf",  "enabled": False},
    ],
    "audiobook": [
        {"fmt": "m4b", "enabled": True},
        {"fmt": "mp3", "enabled": False},
    ],
}


def _settings(**overrides) -> dict:
    base = {
        "format_priority": _FORMAT_PRIORITY_DEFAULTS,
        "quality_axes": DEFAULT_QUALITY_AXES,
    }
    base.update(overrides)
    return base


def _audio_snapshot(
    *,
    bitrate: int | None = None,
    channels: int | None = None,
    fmt: str | None = "AAC",
) -> QualitySnapshot:
    return QualitySnapshot(
        mam_torrent_id="1",
        source="mediainfo",
        audio_format=fmt,
        audio_bitrate_kbps=bitrate,
        audio_channels=channels,
    )


# ─── resolve_profile_from_settings ───────────────────────────


def test_resolve_audiobook_profile_uses_defaults():
    profile = resolve_profile_from_settings("audiobook", _settings())
    assert profile is not None
    assert profile.media_type == "audiobook"
    assert profile.format_tiers == (
        FormatTier(fmt="m4b", enabled=True),
        FormatTier(fmt="mp3", enabled=False),
    )
    assert len(profile.numeric_axes) == 2
    assert profile.numeric_axes[0].axis_name == "audio_bitrate_kbps"
    assert profile.numeric_axes[1].axis_name == "audio_channels"


def test_resolve_ebook_profile_has_no_numeric_axes():
    profile = resolve_profile_from_settings("ebook", _settings())
    assert profile is not None
    assert profile.numeric_axes == ()
    assert len(profile.format_tiers) == 4
    assert profile.format_tiers[0] == FormatTier(fmt="epub", enabled=True)


def test_resolve_returns_none_for_unknown_media_type():
    assert resolve_profile_from_settings("comic", _settings()) is None


def test_resolve_back_compat_without_quality_axes():
    """Legacy v2.9.0 configs (no `quality_axes` key) still resolve.

    Missing `quality_axes` falls back to DEFAULT_QUALITY_AXES, so users
    who never edit the new setting still get the v2.26.0 default audio
    tiebreakers. Empty dict (explicitly opted out) would have to be
    written as `quality_axes: {audiobook: [], ebook: []}` — covered
    separately below.
    """
    profile = resolve_profile_from_settings(
        "audiobook",
        {"format_priority": _FORMAT_PRIORITY_DEFAULTS},
    )
    assert profile is not None
    assert len(profile.numeric_axes) == 2


def test_resolve_with_explicitly_empty_numeric_axes():
    profile = resolve_profile_from_settings(
        "audiobook",
        {
            "format_priority": _FORMAT_PRIORITY_DEFAULTS,
            "quality_axes": {"audiobook": [], "ebook": []},
        },
    )
    assert profile is not None
    assert profile.numeric_axes == ()


def test_resolve_skips_malformed_entries():
    """Entries missing required keys are silently dropped (don't crash)."""
    profile = resolve_profile_from_settings(
        "audiobook",
        {
            "format_priority": {
                "audiobook": [
                    {"fmt": "m4b", "enabled": True},
                    {"enabled": True},               # missing fmt — skip
                    {"fmt": "", "enabled": False},   # empty fmt — skip
                ],
            },
            "quality_axes": {
                "audiobook": [
                    {"axis": "audio_bitrate_kbps", "tiers": [
                        {"label": "high", "min_value": 192},
                    ]},
                    {"axis": "", "tiers": []},      # empty axis — skip
                    {"tiers": [{"label": "x", "min_value": 0}]},  # missing axis
                ],
            },
        },
    )
    assert profile is not None
    assert profile.format_tiers == (FormatTier(fmt="m4b", enabled=True),)
    assert len(profile.numeric_axes) == 1


def test_resolve_lowercases_format_strings():
    profile = resolve_profile_from_settings(
        "audiobook",
        {"format_priority": {"audiobook": [{"fmt": "M4B", "enabled": True}]}},
    )
    assert profile is not None
    assert profile.format_tiers[0].fmt == "m4b"


# ─── format_rank / format_is_enabled ─────────────────────────


def test_format_rank_orders_by_position():
    profile = resolve_profile_from_settings("ebook", _settings())
    assert format_rank(profile, "epub") == 0
    assert format_rank(profile, "azw3") == 1
    assert format_rank(profile, "mobi") == 2
    assert format_rank(profile, "pdf") == 3


def test_format_rank_unknown_format_sorts_last():
    profile = resolve_profile_from_settings("ebook", _settings())
    assert format_rank(profile, "djvu") == len(profile.format_tiers)
    assert format_rank(profile, "") == len(profile.format_tiers)


def test_format_rank_case_insensitive():
    profile = resolve_profile_from_settings("ebook", _settings())
    assert format_rank(profile, "EPUB") == 0
    assert format_rank(profile, "  Epub  ") == 0


def test_format_is_enabled_matches_legacy_flag():
    profile = resolve_profile_from_settings("ebook", _settings())
    assert format_is_enabled(profile, "epub") is True
    assert format_is_enabled(profile, "azw3") is False
    assert format_is_enabled(profile, "djvu") is False
    assert format_is_enabled(profile, "") is False


# ─── _numeric_rank ──────────────────────────────────────────


def _bitrate_axis() -> NumericAxis:
    return NumericAxis(
        axis_name="audio_bitrate_kbps",
        tiers=(
            NumericTier(label="320+", min_value=320),
            NumericTier(label="192+", min_value=192),
            NumericTier(label="128+", min_value=128),
            NumericTier(label="64+",  min_value=64),
            NumericTier(label="<64",  min_value=0),
        ),
    )


def test_numeric_rank_buckets_by_tier_min_values():
    axis = _bitrate_axis()
    assert _numeric_rank(axis, 320) == 0
    assert _numeric_rank(axis, 400) == 0
    assert _numeric_rank(axis, 256) == 1
    assert _numeric_rank(axis, 192) == 1
    assert _numeric_rank(axis, 128) == 2
    assert _numeric_rank(axis, 96) == 3
    assert _numeric_rank(axis, 32) == 4


def test_numeric_rank_missing_value_sorts_past_worst():
    axis = _bitrate_axis()
    assert _numeric_rank(axis, None) == len(axis.tiers)
    assert _numeric_rank(axis, "not a number") == len(axis.tiers)


def test_numeric_rank_zero_lands_in_lowest_tier():
    """The catch-all tier (min_value=0) is the floor for any non-negative read."""
    axis = _bitrate_axis()
    assert _numeric_rank(axis, 0) == 4


# ─── score_quality ──────────────────────────────────────────


def test_score_quality_tuple_shape():
    profile = resolve_profile_from_settings("audiobook", _settings())
    snap = _audio_snapshot(bitrate=192, channels=2)
    score = score_quality(profile=profile, fmt="m4b", snapshot=snap)
    assert score == (0, 1, 0)  # m4b / 192+ / Stereo+


def test_score_quality_lexicographic_format_dominates():
    """Format axis always dominates — a stereo 320kbps mp3 still loses to
    any m4b. Mirrors the v2.9.0 invariant that format priority wins."""
    profile = resolve_profile_from_settings("audiobook", _settings())
    great_mp3 = score_quality(
        profile=profile, fmt="mp3",
        snapshot=_audio_snapshot(bitrate=320, channels=2),
    )
    poor_m4b = score_quality(
        profile=profile, fmt="m4b",
        snapshot=_audio_snapshot(bitrate=64, channels=1),
    )
    assert poor_m4b < great_mp3


def test_score_quality_bitrate_breaks_tie_within_format():
    profile = resolve_profile_from_settings("audiobook", _settings())
    high = score_quality(
        profile=profile, fmt="m4b",
        snapshot=_audio_snapshot(bitrate=320, channels=2),
    )
    low = score_quality(
        profile=profile, fmt="m4b",
        snapshot=_audio_snapshot(bitrate=128, channels=2),
    )
    assert high < low


def test_score_quality_channels_breaks_tie_when_bitrate_equal():
    profile = resolve_profile_from_settings("audiobook", _settings())
    stereo = score_quality(
        profile=profile, fmt="m4b",
        snapshot=_audio_snapshot(bitrate=192, channels=2),
    )
    mono = score_quality(
        profile=profile, fmt="m4b",
        snapshot=_audio_snapshot(bitrate=192, channels=1),
    )
    assert stereo < mono


def test_score_quality_no_snapshot_yields_format_only():
    """Phase 2 dedup-gate path: no QualitySnapshot is available at announce
    time, so numeric axes all rank as 'unknown' (== past-worst). Comparison
    still works — falls back to pure format-priority ordering."""
    profile = resolve_profile_from_settings("audiobook", _settings())
    score = score_quality(profile=profile, fmt="m4b", snapshot=None)
    # m4b=0; 2 numeric axes both unknown (len of their tier lists).
    assert score == (0, 5, 2)


def test_score_quality_known_beats_unknown_within_format():
    profile = resolve_profile_from_settings("audiobook", _settings())
    measured_low = score_quality(
        profile=profile, fmt="m4b",
        snapshot=_audio_snapshot(bitrate=32, channels=1),
    )
    unknown = score_quality(profile=profile, fmt="m4b", snapshot=None)
    assert measured_low < unknown


def test_score_quality_ebook_format_only():
    """Ebooks have no numeric axes; tuple is length 1."""
    profile = resolve_profile_from_settings("ebook", _settings())
    score = score_quality(profile=profile, fmt="epub", snapshot=None)
    assert score == (0,)


def test_score_quality_unknown_format_sorts_last():
    profile = resolve_profile_from_settings("ebook", _settings())
    epub = score_quality(profile=profile, fmt="epub", snapshot=None)
    djvu = score_quality(profile=profile, fmt="djvu", snapshot=None)
    assert epub < djvu


# ─── resolve_profile_for_library (A.3 per-library overrides) ─


def _settings_with_overrides(overrides: dict) -> dict:
    return {
        "format_priority": _FORMAT_PRIORITY_DEFAULTS,
        "quality_axes": DEFAULT_QUALITY_AXES,
        "quality_profile_overrides_by_slug": overrides,
    }


def test_library_resolver_no_overrides_matches_global():
    settings = _settings()
    global_profile = resolve_profile_from_settings("audiobook", settings)
    library_profile = resolve_profile_for_library(
        "audiobook", "my-library", settings,
    )
    assert library_profile == global_profile


def test_library_resolver_none_slug_falls_through_to_global():
    settings = _settings_with_overrides({
        "my-library": {
            "format_priority": {"audiobook": [{"fmt": "mp3", "enabled": True}]},
        },
    })
    global_profile = resolve_profile_from_settings("audiobook", settings)
    no_slug = resolve_profile_for_library("audiobook", None, settings)
    assert no_slug == global_profile


def test_library_resolver_format_priority_override_replaces_whole_list():
    """Whole-list replacement: override defines mp3-first, global m4b-first
    is wholly displaced for this library's audiobook profile."""
    settings = _settings_with_overrides({
        "audiobooks-mobile": {
            "format_priority": {"audiobook": [
                {"fmt": "mp3", "enabled": True},
                {"fmt": "m4b", "enabled": False},
            ]},
        },
    })
    profile = resolve_profile_for_library(
        "audiobook", "audiobooks-mobile", settings,
    )
    assert profile is not None
    assert profile.format_tiers == (
        FormatTier(fmt="mp3", enabled=True),
        FormatTier(fmt="m4b", enabled=False),
    )
    # Numeric axes NOT overridden — inherit from global.
    assert len(profile.numeric_axes) == 2


def test_library_resolver_quality_axes_override_keeps_global_format_priority():
    settings = _settings_with_overrides({
        "audiobooks-home": {
            "quality_axes": {"audiobook": [
                {"axis": "audio_bitrate_kbps", "tiers": [
                    {"label": "any", "min_value": 0},
                ]},
            ]},
        },
    })
    profile = resolve_profile_for_library(
        "audiobook", "audiobooks-home", settings,
    )
    assert profile is not None
    # Format priority NOT overridden — global m4b > mp3 stays.
    assert profile.format_tiers[0].fmt == "m4b"
    # Numeric axes are the override (single-tier).
    assert len(profile.numeric_axes) == 1
    assert profile.numeric_axes[0].axis_name == "audio_bitrate_kbps"


def test_library_resolver_unknown_slug_uses_global():
    settings = _settings_with_overrides({
        "audiobooks-mobile": {
            "format_priority": {"audiobook": [{"fmt": "mp3", "enabled": True}]},
        },
    })
    profile = resolve_profile_for_library(
        "audiobook", "audiobooks-home", settings,
    )
    assert profile is not None
    assert profile.format_tiers[0].fmt == "m4b"  # global default


def test_library_resolver_other_media_type_unaffected():
    """An audiobook-only override doesn't touch ebook resolution."""
    settings = _settings_with_overrides({
        "audiobooks-mobile": {
            "format_priority": {"audiobook": [{"fmt": "mp3", "enabled": True}]},
        },
    })
    ebook_profile = resolve_profile_for_library(
        "ebook", "audiobooks-mobile", settings,
    )
    assert ebook_profile is not None
    assert ebook_profile.format_tiers[0].fmt == "epub"


def test_library_resolver_empty_override_falls_through_to_global():
    settings = _settings_with_overrides({
        "audiobooks-mobile": {},
    })
    profile = resolve_profile_for_library(
        "audiobook", "audiobooks-mobile", settings,
    )
    assert profile is not None
    assert profile.format_tiers[0].fmt == "m4b"


# ─── score_quality, continued ────────────────────────────────


def test_score_quality_total_ordering_across_profile():
    """Sanity check: a sorted-by-score list matches expected quality order."""
    profile = resolve_profile_from_settings("audiobook", _settings())
    candidates = [
        ("mp3-32-mono",  "mp3", _audio_snapshot(bitrate=32, channels=1)),
        ("m4b-192-stereo", "m4b", _audio_snapshot(bitrate=192, channels=2)),
        ("m4b-128-mono", "m4b", _audio_snapshot(bitrate=128, channels=1)),
        ("m4b-320-stereo", "m4b", _audio_snapshot(bitrate=320, channels=2)),
        ("mp3-192-stereo", "mp3", _audio_snapshot(bitrate=192, channels=2)),
    ]
    scored = sorted(
        candidates,
        key=lambda c: score_quality(profile=profile, fmt=c[1], snapshot=c[2]),
    )
    assert [c[0] for c in scored] == [
        "m4b-320-stereo",
        "m4b-192-stereo",
        "m4b-128-mono",
        "mp3-192-stereo",
        "mp3-32-mono",
    ]
