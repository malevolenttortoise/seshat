"""
Active library replacement (v2.26.0 — Bundle A.2).

Phase 4 in this module is the SAFETY LAYER ONLY:

  * `LibrarySafety` enum: classifies a library's overlap with qBit's
    download path.
  * `compute_library_safety(library, settings)`: pure path-overlap
    check given a library dict and the Seshat settings.
  * `is_replacement_allowed(library_slug, settings)`: top-level
    permission gate combining the per-library opt-in setting
    (`active_replacement_enabled_by_slug`) with the safety check.
    Returns False whenever safety == OVERLAP, regardless of the
    per-library bool.

Phase 5 (next) adds the actual replacement execution path that calls
into this gate before snatching/swapping.

Why path-overlap matters
────────────────────────
"Active replacement" means: when a higher-quality version of an owned
book becomes available, Seshat snatches it and swaps the file Calibre
/ ABS reads. The original file is left untouched in qBit's download
folder so the torrent keeps seeding.

The safety hinges on the assumption that the library app reads from a
folder DIFFERENT from qBit's download folder — otherwise "swap the
library file" also overwrites the seeding file, which would break the
seed and (worse) lie to the library app about which torrent backs the
file. We detect that overlap conservatively and default replacement
off when the configuration looks unsafe.

Conservative defaults
─────────────────────
- Per-library opt-in is required (`active_replacement_enabled_by_slug`).
- OVERLAP hard-disables the gate regardless of the bool.
- UNKNOWN (paths can't be resolved) lets the user opt in, but the
  Settings UI surfaces a warning. The user has explicitly attested
  their setup is safe by toggling on.
- SAFE permits the per-library bool to decide.
"""
from __future__ import annotations

import enum
import logging
import os
from typing import Optional

from app import state

_log = logging.getLogger("seshat.orchestrator.active_replacement")


# ─── Safety classification ────────────────────────────────────


class LibrarySafety(str, enum.Enum):
    """Outcome of `compute_library_safety` for one library.

    Stored as strings so JSON-encoding to the Settings UI surface is
    trivial — no enum-to-string adapter needed.
    """
    SAFE = "safe"        # library_path and qBit download path don't overlap
    OVERLAP = "overlap"  # one is a prefix of the other; replacement is unsafe
    UNKNOWN = "unknown"  # either path is empty / unconfigured / unresolvable


# ─── Path normalization ──────────────────────────────────────


def _normalize(path: str) -> Optional[str]:
    """Normalize a path for prefix comparison.

    Returns None for empty / non-string inputs so the caller can treat
    "no path" as unknown rather than mis-matching against another empty
    path. Strips trailing separators so `/foo/` and `/foo` compare
    equal, and collapses `..` and `.` segments via `os.path.normpath`.
    """
    if not path or not isinstance(path, str):
        return None
    s = path.strip()
    if not s:
        return None
    norm = os.path.normpath(s)
    # normpath on Linux leaves trailing slash off already; keep both
    # forms consistent for the prefix check.
    return norm.rstrip(os.sep) or os.sep


def _is_subpath_or_equal(a: str, b: str) -> bool:
    """True iff path `a` equals `b` or is a descendant of `b`.

    Both `a` and `b` are already normalized — the caller does the
    normalization. The implementation is a string-prefix check with a
    separator guard so `/foobar` doesn't false-positive against `/foo`.
    """
    if a == b:
        return True
    return a.startswith(b + os.sep)


def _paths_overlap(p1: str, p2: str) -> bool:
    """True iff either normalized path is a prefix of the other."""
    return _is_subpath_or_equal(p1, p2) or _is_subpath_or_equal(p2, p1)


# ─── Safety computation ──────────────────────────────────────


def compute_library_safety(library: dict, settings: dict) -> LibrarySafety:
    """Classify one library's overlap with qBit's download path.

    `library` is a discovered-library dict — same shape as entries in
    `state._discovered_libraries`. The function reads `library_path`
    (Calibre's library root or ABS's first folder fullPath) and
    compares against `local_path_prefix` from settings (Seshat's view
    of qBit's download path; see app/orchestrator/download_folders.py).

    Returns UNKNOWN when either side is missing — we don't guess.
    Returns OVERLAP when one is a subpath of the other (including equal
    paths). Returns SAFE only when both paths exist and neither
    contains the other.

    Out-of-scope (UNKNOWN):
      - Calibre/ABS containers that mount the SAME host directory at
        a DIFFERENT container path. Detecting that requires comparing
        host bind-mounts across containers, which Seshat can't see
        from inside its own container. The user has to attest to
        non-overlap by enabling the opt-in.
    """
    lib_path = _normalize(library.get("library_path") or "")
    qbit_path = _normalize(settings.get("local_path_prefix") or "")

    if not lib_path or not qbit_path:
        return LibrarySafety.UNKNOWN

    if _paths_overlap(lib_path, qbit_path):
        return LibrarySafety.OVERLAP

    return LibrarySafety.SAFE


# ─── Permission gate ─────────────────────────────────────────


def is_replacement_allowed(
    library_slug: str,
    settings: dict,
    libraries: Optional[list[dict]] = None,
) -> bool:
    """Top-level permission gate for active replacement on one library.

    Returns True iff ALL hold:
      1. The library is registered in `libraries` (or
         `state._discovered_libraries` when `libraries` is None).
      2. Safety == SAFE or UNKNOWN (OVERLAP hard-disables regardless
         of the per-library opt-in).
      3. `active_replacement_enabled_by_slug[<slug>]` is truthy
         (default False — opt-in required).

    For UNKNOWN safety, the per-library bool still rules; the UI is
    responsible for surfacing the warning so the user knows what
    they're attesting to.
    """
    if not library_slug:
        return False

    libs = libraries if libraries is not None else list(state._discovered_libraries)
    library = next((lib for lib in libs if lib.get("slug") == library_slug), None)
    if library is None:
        return False

    safety = compute_library_safety(library, settings)
    if safety == LibrarySafety.OVERLAP:
        return False

    enabled_map = settings.get("active_replacement_enabled_by_slug") or {}
    return bool(enabled_map.get(library_slug, False))


# ─── Reporting helper for the Settings UI surface (Phase 6) ──


def library_replacement_status(
    library: dict,
    settings: dict,
) -> dict:
    """Compose the per-library replacement status block for the UI.

    Returned dict layout:
        {
            "slug": str,
            "name": str,
            "content_type": str,
            "library_path": str,
            "safety": "safe" | "overlap" | "unknown",
            "enabled": bool,          # the opt-in setting itself
            "effective": bool,        # what the gate actually returns
        }

    `effective` is the value the Phase 5 replacement loop checks; the
    UI may want to display `enabled` separately so the user sees that
    their toggle was overridden by the safety gate.
    """
    safety = compute_library_safety(library, settings)
    slug = library.get("slug") or ""
    enabled_map = settings.get("active_replacement_enabled_by_slug") or {}
    enabled = bool(enabled_map.get(slug, False))
    effective = enabled and safety != LibrarySafety.OVERLAP
    return {
        "slug": slug,
        "name": library.get("name") or slug,
        "content_type": library.get("content_type") or "",
        "library_path": library.get("library_path") or "",
        "safety": safety.value,
        "enabled": enabled,
        "effective": effective,
    }
