"""
Tests for the Phase 4 path-overlap safety layer.

Coverage:
  - LibrarySafety enum string-encoding (UI consumption)
  - _normalize / _paths_overlap edge cases
  - compute_library_safety across overlapping / disjoint / missing inputs
  - is_replacement_allowed permission gate (opt-in + safety interaction)
  - library_replacement_status reporting shape (Phase 6 UI consumption)

Phase 5 execution-path tests will live in a separate file
(`test_active_replacement_execution.py`) so the safety layer can be
refactored independently of replacement-execution logic.
"""
from __future__ import annotations

import pytest

from app.orchestrator.active_replacement import (
    LibrarySafety,
    _normalize,
    _paths_overlap,
    compute_library_safety,
    is_auto_enact_allowed,
    is_replacement_allowed,
    library_replacement_status,
)


# ─── Fixtures ────────────────────────────────────────────────


def _calibre_library(
    *, slug="my-calibre", path="/calibre-library",
    name="Calibre", content_type="ebook",
) -> dict:
    return {
        "slug": slug,
        "name": name,
        "app_type": "calibre",
        "content_type": content_type,
        "library_path": path,
        "source_db_path": f"{path}/metadata.db",
    }


def _abs_library(
    *, slug="abs-audiobooks", path="/audiobooks",
    name="Audiobookshelf", content_type="audiobook",
) -> dict:
    return {
        "slug": slug,
        "name": name,
        "app_type": "audiobookshelf",
        "content_type": content_type,
        "library_path": path,
        "abs_library_id": "abc-123",
        "abs_base_url": "http://abs.local:13378",
    }


def _settings(
    *, qbit_path="/downloads",
    enabled_by_slug: dict | None = None,
    auto_enact_by_slug: dict | None = None,
) -> dict:
    return {
        "local_path_prefix": qbit_path,
        "active_replacement_enabled_by_slug": enabled_by_slug or {},
        "active_replacement_auto_enact_by_slug": auto_enact_by_slug or {},
    }


# ─── _normalize ──────────────────────────────────────────────


class TestNormalize:
    def test_strips_trailing_slash(self):
        assert _normalize("/foo/") == "/foo"
        assert _normalize("/foo") == "/foo"

    def test_collapses_dots(self):
        assert _normalize("/foo/./bar") == "/foo/bar"
        assert _normalize("/foo/bar/..") == "/foo"

    def test_empty_returns_none(self):
        assert _normalize("") is None
        assert _normalize("   ") is None
        assert _normalize(None) is None  # type: ignore[arg-type]

    def test_root_keeps_separator(self):
        assert _normalize("/") == "/"


# ─── _paths_overlap ──────────────────────────────────────────


class TestPathsOverlap:
    def test_equal_paths_overlap(self):
        assert _paths_overlap("/foo", "/foo") is True

    def test_subpath_overlaps(self):
        assert _paths_overlap("/foo/bar", "/foo") is True
        assert _paths_overlap("/foo", "/foo/bar") is True

    def test_separator_guard_prevents_false_positive(self):
        """/foobar must NOT overlap /foo — they're sibling directories
        with a shared prefix."""
        assert _paths_overlap("/foobar", "/foo") is False
        assert _paths_overlap("/foo", "/foobar") is False

    def test_disjoint_paths(self):
        assert _paths_overlap("/foo", "/bar") is False
        assert _paths_overlap("/calibre-library", "/downloads") is False


# ─── compute_library_safety ──────────────────────────────────


class TestComputeLibrarySafety:
    def test_disjoint_calibre_path_is_safe(self):
        lib = _calibre_library(path="/calibre-library")
        s = _settings(qbit_path="/downloads")
        assert compute_library_safety(lib, s) == LibrarySafety.SAFE

    def test_calibre_under_qbit_downloads_overlaps(self):
        """Mark's classic foot-gun — Calibre library inside qBit downloads."""
        lib = _calibre_library(path="/downloads/calibre-books")
        s = _settings(qbit_path="/downloads")
        assert compute_library_safety(lib, s) == LibrarySafety.OVERLAP

    def test_qbit_downloads_under_calibre_overlaps(self):
        """Reverse direction also unsafe — replacement could still
        clobber a torrent file from a subdirectory."""
        lib = _calibre_library(path="/data")
        s = _settings(qbit_path="/data/downloads")
        assert compute_library_safety(lib, s) == LibrarySafety.OVERLAP

    def test_equal_paths_overlap(self):
        lib = _calibre_library(path="/data")
        s = _settings(qbit_path="/data")
        assert compute_library_safety(lib, s) == LibrarySafety.OVERLAP

    def test_missing_library_path_is_unknown(self):
        lib = _calibre_library(path="")
        s = _settings(qbit_path="/downloads")
        assert compute_library_safety(lib, s) == LibrarySafety.UNKNOWN

    def test_missing_qbit_prefix_is_unknown(self):
        lib = _calibre_library(path="/calibre-library")
        s = _settings(qbit_path="")
        assert compute_library_safety(lib, s) == LibrarySafety.UNKNOWN

    def test_separator_guard_keeps_sibling_safe(self):
        lib = _calibre_library(path="/downloadsbackup")
        s = _settings(qbit_path="/downloads")
        assert compute_library_safety(lib, s) == LibrarySafety.SAFE

    def test_abs_library_safe_when_disjoint(self):
        lib = _abs_library(path="/audiobooks")
        s = _settings(qbit_path="/downloads")
        assert compute_library_safety(lib, s) == LibrarySafety.SAFE

    def test_abs_library_overlap_when_inside_downloads(self):
        lib = _abs_library(path="/downloads/audiobooks")
        s = _settings(qbit_path="/downloads")
        assert compute_library_safety(lib, s) == LibrarySafety.OVERLAP


# ─── is_replacement_allowed ──────────────────────────────────


class TestIsReplacementAllowed:
    def test_safe_and_enabled_returns_true(self):
        lib = _calibre_library(slug="my-calibre", path="/calibre-library")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"my-calibre": True},
        )
        assert is_replacement_allowed("my-calibre", s, libraries=[lib]) is True

    def test_safe_but_not_enabled_returns_false(self):
        """Default off — opt-in is required."""
        lib = _calibre_library(slug="my-calibre", path="/calibre-library")
        s = _settings(qbit_path="/downloads")
        assert is_replacement_allowed("my-calibre", s, libraries=[lib]) is False

    def test_overlap_hard_disables_even_when_enabled(self):
        """Even with the per-library bool flipped on, OVERLAP wins."""
        lib = _calibre_library(slug="risky", path="/downloads/calibre")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"risky": True},
        )
        assert is_replacement_allowed("risky", s, libraries=[lib]) is False

    def test_unknown_safety_respects_opt_in(self):
        """UNKNOWN means the safety check couldn't decide — the user's
        explicit opt-in is taken as attestation that they've verified
        their setup."""
        lib = _calibre_library(slug="my-calibre", path="")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"my-calibre": True},
        )
        assert is_replacement_allowed("my-calibre", s, libraries=[lib]) is True

    def test_unknown_safety_without_opt_in_is_off(self):
        lib = _calibre_library(slug="my-calibre", path="")
        s = _settings(qbit_path="/downloads")
        assert is_replacement_allowed("my-calibre", s, libraries=[lib]) is False

    def test_unknown_slug_returns_false(self):
        s = _settings(enabled_by_slug={"ghost": True})
        assert is_replacement_allowed("ghost", s, libraries=[]) is False

    def test_empty_slug_returns_false(self):
        assert is_replacement_allowed("", _settings(), libraries=[]) is False


# ─── library_replacement_status ──────────────────────────────


class TestLibraryReplacementStatus:
    def test_status_includes_all_fields(self):
        lib = _calibre_library(slug="cal", path="/calibre-library")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"cal": True},
        )
        status = library_replacement_status(lib, s)
        assert status == {
            "slug": "cal",
            "name": "Calibre",
            "content_type": "ebook",
            "library_path": "/calibre-library",
            "safety": "safe",
            "enabled": True,
            "effective": True,
            "auto_enact": False,
            "auto_enact_effective": False,
        }

    def test_overlap_makes_effective_false_even_when_enabled(self):
        lib = _calibre_library(slug="cal", path="/downloads/calibre")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"cal": True},
        )
        status = library_replacement_status(lib, s)
        assert status["safety"] == "overlap"
        assert status["enabled"] is True
        assert status["effective"] is False

    def test_unknown_with_opt_in_yields_effective_true(self):
        """UNKNOWN + opt-in → user has attested → effective True
        (warning is the UI's job, not the resolver's)."""
        lib = _calibre_library(slug="cal", path="")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"cal": True},
        )
        status = library_replacement_status(lib, s)
        assert status["safety"] == "unknown"
        assert status["effective"] is True

    def test_missing_enabled_map_treats_as_off(self):
        lib = _calibre_library(slug="cal", path="/calibre-library")
        s = {"local_path_prefix": "/downloads"}
        status = library_replacement_status(lib, s)
        assert status["enabled"] is False
        assert status["effective"] is False
        assert status["auto_enact"] is False
        assert status["auto_enact_effective"] is False

    def test_auto_enact_field_reflects_setting(self):
        lib = _calibre_library(slug="cal", path="/calibre-library")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"cal": True},
            auto_enact_by_slug={"cal": True},
        )
        status = library_replacement_status(lib, s)
        assert status["auto_enact"] is True
        assert status["auto_enact_effective"] is True

    def test_auto_enact_effective_false_when_master_off(self):
        # Auto-enact toggled on but master gate off → auto path inert.
        lib = _calibre_library(slug="cal", path="/calibre-library")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"cal": False},
            auto_enact_by_slug={"cal": True},
        )
        status = library_replacement_status(lib, s)
        assert status["enabled"] is False
        assert status["auto_enact"] is True
        assert status["auto_enact_effective"] is False

    def test_auto_enact_effective_false_when_overlap(self):
        # OVERLAP hard-disables both manual and auto regardless of toggles.
        lib = _calibre_library(slug="cal", path="/downloads/calibre")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"cal": True},
            auto_enact_by_slug={"cal": True},
        )
        status = library_replacement_status(lib, s)
        assert status["safety"] == "overlap"
        assert status["effective"] is False
        assert status["auto_enact_effective"] is False


# ─── is_auto_enact_allowed ───────────────────────────────────


class TestIsAutoEnactAllowed:
    def test_all_gates_open_returns_true(self):
        lib = _calibre_library(slug="cal", path="/calibre-library")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"cal": True},
            auto_enact_by_slug={"cal": True},
        )
        assert is_auto_enact_allowed("cal", s, libraries=[lib]) is True

    def test_master_off_blocks_auto(self):
        lib = _calibre_library(slug="cal", path="/calibre-library")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"cal": False},
            auto_enact_by_slug={"cal": True},
        )
        assert is_auto_enact_allowed("cal", s, libraries=[lib]) is False

    def test_auto_off_blocks_auto(self):
        lib = _calibre_library(slug="cal", path="/calibre-library")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"cal": True},
            auto_enact_by_slug={"cal": False},
        )
        assert is_auto_enact_allowed("cal", s, libraries=[lib]) is False

    def test_overlap_blocks_auto_even_when_both_on(self):
        lib = _calibre_library(slug="cal", path="/downloads/calibre")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"cal": True},
            auto_enact_by_slug={"cal": True},
        )
        assert is_auto_enact_allowed("cal", s, libraries=[lib]) is False

    def test_missing_maps_default_off(self):
        lib = _calibre_library(slug="cal", path="/calibre-library")
        s = {"local_path_prefix": "/downloads"}
        assert is_auto_enact_allowed("cal", s, libraries=[lib]) is False

    def test_unknown_library_slug_returns_false(self):
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"cal": True},
            auto_enact_by_slug={"cal": True},
        )
        assert is_auto_enact_allowed("ghost", s, libraries=[]) is False

    def test_unknown_safety_with_both_toggles_on_allowed(self):
        # UNKNOWN safety lets the user attest. Master + auto both on → auto fires.
        lib = _calibre_library(slug="cal", path="")
        s = _settings(
            qbit_path="/downloads",
            enabled_by_slug={"cal": True},
            auto_enact_by_slug={"cal": True},
        )
        assert is_auto_enact_allowed("cal", s, libraries=[lib]) is True
