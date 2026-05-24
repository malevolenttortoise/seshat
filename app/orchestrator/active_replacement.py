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
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
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


def is_auto_enact_allowed(
    library_slug: str,
    settings: dict,
    libraries: Optional[list[dict]] = None,
) -> bool:
    """Compound gate for post-detection auto-enact (5b).

    Auto-enact requires ALL of:
      1. The master gate (`is_replacement_allowed`) passes.
      2. `active_replacement_auto_enact_by_slug[<slug>]` is truthy.

    Both default off; the operator must explicitly opt in per-library.
    OVERLAP safety classification hard-disables via the master gate.

    When this returns False, a freshly-detected opportunity stays in
    'detected' status until a user clicks Enact in the UI. When True,
    the post-detection auto-enact step performs the file swap
    immediately.
    """
    if not is_replacement_allowed(library_slug, settings, libraries=libraries):
        return False
    auto_map = settings.get("active_replacement_auto_enact_by_slug") or {}
    return bool(auto_map.get(library_slug, False))


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
            "enabled": bool,                # master opt-in
            "effective": bool,              # master gate result
            "auto_enact": bool,             # secondary opt-in (5b)
            "auto_enact_effective": bool,   # auto-enact gate result
        }

    `effective` is what the manual-enact path checks. `auto_enact_effective`
    is what the post-detection auto-enact step checks (compound gate:
    master + safety + auto_enact bool). The UI surfaces both so the user
    sees when their auto-enact toggle is overridden by the master gate
    or the safety classification.
    """
    safety = compute_library_safety(library, settings)
    slug = library.get("slug") or ""
    enabled_map = settings.get("active_replacement_enabled_by_slug") or {}
    enabled = bool(enabled_map.get(slug, False))
    effective = enabled and safety != LibrarySafety.OVERLAP
    auto_map = settings.get("active_replacement_auto_enact_by_slug") or {}
    auto_enact = bool(auto_map.get(slug, False))
    auto_enact_effective = effective and auto_enact
    return {
        "slug": slug,
        "name": library.get("name") or slug,
        "content_type": library.get("content_type") or "",
        "library_path": library.get("library_path") or "",
        "safety": safety.value,
        "enabled": enabled,
        "effective": effective,
        "auto_enact": auto_enact,
        "auto_enact_effective": auto_enact_effective,
    }


# ─── Phase 5b — enactment orchestrator ───────────────────────
#
# `enact_opportunity` performs the destructive file swap for one
# detected opportunity:
#
#   1. Re-validate the opportunity (status still 'detected', master
#      gate still True, library still present).
#   2. Resolve the owned book's on-disk directory (Calibre via
#      metadata.db; ABS via the API).
#   3. Move the directory to `<library_path>/.seshat-replaced/<ts>/`.
#   4. Insert a `replacement_enactments` audit row.
#   5. Call the appropriate sink's `remove()` (CalibreSink full-image,
#      CWASink slim-image fallback, ABSSink for audiobooks).
#   6a. On success: flip opportunity status → 'enacted'.
#   6b. On sink failure: move the directory back from .seshat-replaced/,
#       stamp `failed_at` + `failed_reason` on the audit row, return
#       the result without bumping the opportunity status.
#
# `restore_enactment` is the inverse: move the directory back, call
# sink.deliver to re-register with the library app, flip the
# opportunity status back to 'detected', stamp `restored_at`.


@dataclass(frozen=True)
class EnactmentResult:
    """Outcome of an `enact_opportunity` / `restore_enactment` call.

    `status` is one of:
      * "enacted"     — file swap completed; opportunity → 'enacted'.
      * "restored"    — soft-delete reversed; opportunity → 'detected'.
      * "blocked"     — gates rejected the call (master gate off,
                        opportunity not in expected status, etc.).
                        No file or DB state changed.
      * "not_found"   — opportunity / enactment id doesn't exist, or
                        the owned book row can't be located.
      * "failed"      — sink call returned an error AFTER the
                        soft-delete; the soft-delete was rolled back
                        and `failed_at` audit-stamped. Opportunity
                        status is unchanged.
      * "no_sink"     — no destination sink could be constructed for
                        this library (missing calibredb AND CWA creds,
                        or ABS not configured). No state changed.

    `detail` is human-readable text used by the UI toast. `error`
    carries the raw underlying error string (sink stderr, exception
    message) for the audit log and debug tracing.
    """
    status: str
    opportunity_id: int
    enactment_id: Optional[int]
    detail: str
    error: Optional[str] = None


# ─── Sink selection ──────────────────────────────────────────


def _select_sink_for_library(
    library: dict,
    settings: dict,
) -> tuple[str, object, Optional[str]]:
    """Pick the appropriate sink for one library.

    Returns (kind, sink_instance, error). `error` is non-None when no
    sink could be constructed; in that case the caller surfaces a
    `no_sink` EnactmentResult and exits without touching state.

    Calibre routing mirrors `routers/metadata.book_push`: prefer
    full-image `calibredb` (always available when present), fall back
    to CWA admin-form delete when slim users have configured CWA.
    Audiobookshelf is single-sink (filesystem delete + ABS rescan).
    """
    from app.sinks.audiobookshelf import AudiobookshelfSink
    from app.sinks.calibre import CalibreSink, CALIBREDB_CMD
    from app.sinks.cwa import CWASink

    app_type = library.get("app_type", "")
    library_path = library.get("library_path") or ""
    if not library_path:
        return ("", None, "library has no library_path configured")

    if app_type == "calibre":
        # `shutil.which` returns None when the binary isn't on PATH,
        # which on the slim image is the expected state. We use that
        # as the gate to fall through to CWA.
        calibredb_present = shutil.which(CALIBREDB_CMD) is not None
        if calibredb_present:
            return ("calibre", CalibreSink(library_path), None)

        # Slim-image path. CWASink ignores its constructor's
        # `ingest_path` for the remove flow (admin API is called
        # directly with library creds from settings); pass an empty
        # string so we don't accidentally couple to the delivery path.
        cwa_url = settings.get("cwa_base_url") or ""
        cwa_user = settings.get("cwa_username") or ""
        if cwa_url and cwa_user:
            return ("cwa", CWASink(""), None)
        return (
            "", None,
            "no Calibre sink available: calibredb not on PATH and "
            "CWA admin not configured (cwa_base_url + cwa_username)",
        )

    if app_type == "audiobookshelf":
        return (
            "abs",
            AudiobookshelfSink(
                library_path,
                abs_base_url=settings.get("abs_url", "") or "",
                # api key + library id resolved at call time via
                # _maybe_trigger_scan reading the encrypted store.
                # For the v2.27.0 path the sink just needs the
                # base_url + library_id for the rescan trigger after
                # remove; we wire those into the sink up front.
                abs_api_key="",  # filled below
                abs_library_id=settings.get("abs_sink_library_id", "") or "",
            ),
            None,
        )

    return ("", None, f"unsupported app_type: {app_type!r}")


# ─── Owned-book path resolution ──────────────────────────────


async def _resolve_owned_book_dir(
    library: dict,
    owned_book_id: int,
    settings: dict,
) -> tuple[Optional[str], dict, Optional[str]]:
    """Resolve a Seshat owned_book_id to the book's on-disk directory.

    Returns (book_dir, owned_row, error). `owned_row` carries the
    per-library books-table row fields the enact path needs
    downstream (calibre_id, mam_torrent_id, audiobookshelf_id,
    formats). `error` is non-None when resolution failed.

    Calibre lookup: read the per-library `books.calibre_id`, then
    open Calibre's `metadata.db` read-only and join `books.path`
    onto the library root. This works even when calibredb isn't on
    PATH (slim image) because we're hitting Calibre's DB directly.

    ABS lookup: read `books.audiobookshelf_id`, call ABS's
    `/api/items/{id}?expanded=1`, and take `dirname(audioFiles[0].metadata.path)`.
    """
    # The discovery DB is keyed per-library; the public API name is
    # `get_db` (active library set via `set_active_library`), and the
    # rest of the codebase imports it under the alias `get_library_db`.
    # Mirror that convention here.
    from app.discovery.database import get_db as get_library_db

    library_slug = library.get("slug") or ""
    library_path = library.get("library_path") or ""
    app_type = library.get("app_type") or ""
    if not (library_slug and library_path):
        return None, {}, "library missing slug or library_path"

    try:
        # `get_db(slug)` accepts a per-library slug directly — no
        # global-state mutation needed. Avoids leaking active-library
        # state across concurrent requests.
        lib_db = await get_library_db(library_slug)
    except Exception as e:
        return None, {}, f"could not open library DB: {type(e).__name__}: {e}"

    try:
        cursor = await lib_db.execute(
            "SELECT id, title, calibre_id, audiobookshelf_id, "
            "       mam_torrent_id, formats "
            "FROM books WHERE id = ?",
            (owned_book_id,),
        )
        row = await cursor.fetchone()
    finally:
        await lib_db.close()

    if row is None:
        return None, {}, f"owned book_id {owned_book_id} not found in library {library_slug!r}"

    cols = ["id", "title", "calibre_id", "audiobookshelf_id",
            "mam_torrent_id", "formats"]
    owned_row = dict(zip(cols, row))

    if app_type == "calibre":
        cal_id = owned_row.get("calibre_id")
        if not cal_id:
            return None, owned_row, (
                f"owned book {owned_book_id} has no calibre_id; cannot "
                f"resolve on-disk path (pre-Seshat books need a "
                f"calibre_sync backfill first)"
            )
        # Calibre's metadata.db lives at <library_path>/metadata.db.
        # The `books.path` column is the relative directory inside the
        # library — joining yields the absolute book directory.
        metadata_db = Path(library_path) / "metadata.db"
        if not metadata_db.exists():
            return None, owned_row, f"Calibre metadata.db not found at {metadata_db}"
        try:
            con = sqlite3.connect(
                f"file:{os.fspath(metadata_db)}?mode=ro", uri=True,
            )
            try:
                cal_rows = con.execute(
                    "SELECT path FROM books WHERE id = ?", (int(cal_id),),
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error as e:
            return None, owned_row, f"sqlite read failed: {type(e).__name__}: {e}"
        if not cal_rows:
            return None, owned_row, (
                f"calibre_id {cal_id} not in Calibre metadata.db "
                f"(library {library_slug} may be out of sync)"
            )
        rel_path = cal_rows[0][0]
        book_dir = str(Path(library_path) / rel_path)
        return book_dir, owned_row, None

    if app_type == "audiobookshelf":
        abs_id = owned_row.get("audiobookshelf_id")
        if not abs_id:
            return None, owned_row, (
                f"owned book {owned_book_id} has no audiobookshelf_id; "
                f"cannot resolve on-disk path"
            )
        from app.library_apps.audiobookshelf import (
            AudiobookshelfClient,
            _get_abs_api_key,
        )
        base_url = (settings.get("abs_url") or "").rstrip("/")
        api_key = await _get_abs_api_key()
        if not (base_url and api_key):
            return None, owned_row, "ABS not configured (abs_url + abs_api_key)"
        try:
            client = AudiobookshelfClient(base_url, api_key)
            item = await client.get_item(abs_id)
        except Exception as e:
            return None, owned_row, f"ABS API call failed: {type(e).__name__}: {e}"
        # `item.media.audioFiles[].metadata.path` carries the absolute
        # file path on disk. Audiobook folders contain N audio files
        # sharing a parent dir; we want that parent.
        media = item.get("media") or {}
        audio_files = media.get("audioFiles") or []
        path = None
        for af in audio_files:
            md = af.get("metadata") or {}
            p = md.get("path") or af.get("path")
            if p:
                path = p
                break
        if not path:
            # Single-file books may store the path on the libraryItem
            # itself; some ABS versions also expose `path` at the
            # top of the response.
            path = item.get("path")
        if not path:
            return None, owned_row, "ABS API returned no file path for this item"
        book_dir = str(Path(path).parent) if "." in Path(path).name else path
        return book_dir, owned_row, None

    return None, owned_row, f"unsupported app_type: {app_type!r}"


# ─── Soft-delete helpers ─────────────────────────────────────


def _soft_delete_dir_for(library_path: str) -> Path:
    """Return the `.seshat-replaced/<YYYYMMDD-HHMMSS>/` subdir under
    the library root for a fresh enact.

    Per design Decision 10, the soft-delete folder lives INSIDE the
    library root with a dot prefix so backup tools capture it but
    library-scanning tools ignore it. Each enact gets its own
    timestamp folder so multiple enacts on the same library are
    distinguishable + restorable independently.
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    return Path(library_path) / ".seshat-replaced" / ts


def _move_dir(src: str, dst: str) -> tuple[bool, Optional[str]]:
    """Move `src` directory to `dst`. Returns (ok, error).

    Creates the destination's parent directory chain when missing.
    """
    src_p = Path(src)
    if not src_p.exists():
        return False, f"source path does not exist: {src}"
    try:
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dst)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return True, None


def _directory_size(path: str) -> Optional[int]:
    """Sum total bytes under a directory. Returns None on errors.

    Best-effort: any unreadable entry is silently skipped (audit-row
    size is a metric, not a correctness gate)."""
    p = Path(path)
    if not p.exists():
        return None
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        return None
    return total


# ─── Enact / restore ─────────────────────────────────────────


async def enact_opportunity(
    db,
    opportunity_id: int,
    *,
    acted_by: Optional[str] = None,
    settings: Optional[dict] = None,
    libraries: Optional[list[dict]] = None,
) -> EnactmentResult:
    """Perform the destructive file swap for one detected opportunity.

    See module docstring for the full flow. `db` is the main Seshat
    DB connection (where `replacement_opportunities` +
    `replacement_enactments` live). `settings` and `libraries`
    default to the live snapshots when omitted — tests inject their
    own.

    Caller is responsible for committing `db` after a successful
    return (so the routing layer can batch enact + audit + status
    update into one COMMIT).
    """
    from app.config import load_settings
    from app.quality import enactments, opportunities

    if settings is None:
        settings = load_settings()
    libs = libraries if libraries is not None else list(state._discovered_libraries)

    # ── Step 1: load + validate opportunity ──────────────────
    opp = await opportunities.get_opportunity(db, opportunity_id)
    if opp is None:
        return EnactmentResult(
            status="not_found",
            opportunity_id=opportunity_id,
            enactment_id=None,
            detail=f"opportunity {opportunity_id} does not exist",
        )
    if opp.get("status") != "detected":
        return EnactmentResult(
            status="blocked",
            opportunity_id=opportunity_id,
            enactment_id=None,
            detail=(
                f"opportunity status is {opp.get('status')!r}; "
                f"only 'detected' rows are enactable"
            ),
        )

    library_slug = opp.get("owned_library_slug") or ""
    library = next((l for l in libs if l.get("slug") == library_slug), None)
    if library is None:
        return EnactmentResult(
            status="blocked",
            opportunity_id=opportunity_id,
            enactment_id=None,
            detail=f"library {library_slug!r} is not currently discovered",
        )

    # ── Step 2: re-check master gate ─────────────────────────
    # Library safety classification + per-library opt-in. The
    # detection-time check happened in `replacement_detector.py`, but
    # gates may have flipped since (operator toggled off, library
    # path changed and now overlaps qBit, etc.). Re-check at enact
    # time so we never act on a stale gate.
    if not is_replacement_allowed(library_slug, settings, libraries=libs):
        return EnactmentResult(
            status="blocked",
            opportunity_id=opportunity_id,
            enactment_id=None,
            detail=(
                f"active replacement gate failed for library "
                f"{library_slug!r} (safety / opt-in re-checked)"
            ),
        )

    # ── Step 3: pick destination sink ────────────────────────
    sink_kind, sink, sink_err = _select_sink_for_library(library, settings)
    if sink is None:
        return EnactmentResult(
            status="no_sink",
            opportunity_id=opportunity_id,
            enactment_id=None,
            detail=sink_err or "no sink available for this library",
            error=sink_err,
        )

    # ── Step 4: resolve owned book's on-disk directory ───────
    owned_book_id = int(opp.get("owned_book_id") or 0)
    book_dir, owned_row, resolve_err = await _resolve_owned_book_dir(
        library, owned_book_id, settings,
    )
    if book_dir is None:
        return EnactmentResult(
            status="not_found",
            opportunity_id=opportunity_id,
            enactment_id=None,
            detail=resolve_err or "could not resolve owned book path",
            error=resolve_err,
        )
    if not Path(book_dir).exists():
        # Already gone — treat as success-but-no-op so the operator
        # can re-detect / dismiss without us crashing. The opportunity
        # row stays 'detected' so the UI surfaces the orphan state.
        return EnactmentResult(
            status="not_found",
            opportunity_id=opportunity_id,
            enactment_id=None,
            detail=f"owned book directory does not exist: {book_dir}",
        )

    # ── Step 5: soft-delete (move out of the library tree) ───
    library_path = library.get("library_path") or ""
    soft_root = _soft_delete_dir_for(library_path)
    soft_dst = soft_root / Path(book_dir).name
    moved_ok, move_err = _move_dir(book_dir, str(soft_dst))
    if not moved_ok:
        return EnactmentResult(
            status="failed",
            opportunity_id=opportunity_id,
            enactment_id=None,
            detail=f"soft-delete move failed: {move_err}",
            error=move_err,
        )

    owned_size = _directory_size(str(soft_dst))

    # ── Step 6: record initial audit row (before sink call) ──
    enactment_id = await enactments.record_enactment(
        db,
        opportunity_id=opportunity_id,
        acted_by=acted_by,
        library_slug=library_slug,
        owned_book_id_before=owned_book_id,
        owned_path_before=book_dir,
        owned_path_after=str(soft_dst),
        owned_size_bytes=owned_size,
        candidate_path=None,
        candidate_size_bytes=None,
        sink_result=None,
    )

    # ── Step 7: call the sink's remove ───────────────────────
    if sink_kind in ("calibre", "cwa"):
        cal_id = owned_row.get("calibre_id")
        if sink_kind == "calibre":
            sink_result = await sink.remove(
                calibre_book_id=int(cal_id) if cal_id else None,
                # Fall back to mam_torrent_id when calibre_id is
                # unavailable (shouldn't happen on Mark's prod, but
                # keep the path covered).
                mam_torrent_id=(
                    int(owned_row["mam_torrent_id"])
                    if owned_row.get("mam_torrent_id")
                    and not cal_id
                    else None
                ),
            )
        else:  # cwa
            if not cal_id:
                # CWA's admin form delete needs the Calibre book id.
                # Roll back the soft-delete and surface a clear error.
                rb_ok, rb_err = _move_dir(str(soft_dst), book_dir)
                rollback_note = "" if rb_ok else f" (rollback also failed: {rb_err})"
                await enactments.mark_enactment_failed(
                    db, enactment_id,
                    reason=f"CWA delete requires calibre_id, but owned book {owned_book_id} has none{rollback_note}",
                )
                return EnactmentResult(
                    status="failed" if rb_ok else "rolled_back",
                    opportunity_id=opportunity_id,
                    enactment_id=enactment_id,
                    detail=f"CWA delete needs calibre_id (owned book {owned_book_id} has none)",
                    error="missing calibre_id for CWA path",
                )
            sink_result = await sink.remove(calibre_book_id=int(cal_id))
    elif sink_kind == "abs":
        # ABSSink.remove takes the original on-disk path. We already
        # moved the dir out, so passing the original path triggers
        # the idempotent "path doesn't exist → success" branch and
        # fires the scan trigger — which is the actual reconciliation
        # ABS needs.
        sink_result = await sink.remove(path=book_dir)
    else:
        # Defensive — selection logic should have rejected this
        # earlier, but if a future sink kind slips through we want
        # the soft-delete rolled back rather than orphaned.
        rb_ok, _ = _move_dir(str(soft_dst), book_dir)
        await enactments.mark_enactment_failed(
            db, enactment_id, reason=f"unhandled sink_kind={sink_kind!r}",
        )
        return EnactmentResult(
            status="failed",
            opportunity_id=opportunity_id,
            enactment_id=enactment_id,
            detail=f"unhandled sink kind: {sink_kind!r}",
            error="unhandled sink_kind",
        )

    if not sink_result.success:
        # ── Rollback path ────────────────────────────────────
        # Sink call failed AFTER the soft-delete landed. Move the
        # file back to its original location and audit-stamp the
        # failure so the operator can retry once the sink is
        # reachable again. Opportunity status STAYS 'detected'.
        rb_ok, rb_err = _move_dir(str(soft_dst), book_dir)
        rollback_note = "" if rb_ok else f" (rollback ALSO failed: {rb_err})"
        await enactments.mark_enactment_failed(
            db, enactment_id,
            reason=f"sink {sink_kind} remove failed: {sink_result.error}{rollback_note}",
        )
        return EnactmentResult(
            status="failed" if rb_ok else "rolled_back",
            opportunity_id=opportunity_id,
            enactment_id=enactment_id,
            detail=(
                f"sink {sink_kind} remove failed; soft-delete "
                f"{'rolled back' if rb_ok else 'NOT rolled back'}"
            ),
            error=sink_result.error or "sink returned success=False",
        )

    # ── Step 8: persist sink_result text + flip opportunity ──
    await db.execute(
        "UPDATE replacement_enactments SET sink_result = ? WHERE id = ?",
        (sink_result.detail or "ok", enactment_id),
    )
    await opportunities.update_status(
        db, opportunity_id,
        status="enacted",
        acted_by=acted_by,
    )
    _log.info(
        "active replacement: enacted opportunity %s in library %s "
        "(sink=%s, book moved to %s)",
        opportunity_id, library_slug, sink_kind, soft_dst,
    )
    return EnactmentResult(
        status="enacted",
        opportunity_id=opportunity_id,
        enactment_id=enactment_id,
        detail=f"enacted via {sink_kind} sink; soft-delete at {soft_dst}",
    )


async def restore_enactment(
    db,
    enactment_id: int,
    *,
    restored_by: Optional[str] = None,
    settings: Optional[dict] = None,
    libraries: Optional[list[dict]] = None,
) -> EnactmentResult:
    """Reverse a successful enactment: move the soft-deleted file back
    to its original location, re-register with the library app, flip
    the opportunity back to 'detected', stamp `restored_at`.

    Gated by:
      * Enactment row exists + has `failed_at IS NULL AND
        restored_at IS NULL` (only "active" enactments are restorable).
      * The soft-delete file still exists at `owned_path_after`
        (retention sweeper may have purged it — Phase 6 work).

    Caller is responsible for committing `db` after a successful return.
    """
    from app.config import load_settings
    from app.metadata.extract import BookMetadata
    from app.quality import enactments, opportunities

    if settings is None:
        settings = load_settings()
    libs = libraries if libraries is not None else list(state._discovered_libraries)

    enactment = await enactments.get_enactment(db, enactment_id)
    if enactment is None:
        return EnactmentResult(
            status="not_found",
            opportunity_id=0,
            enactment_id=enactment_id,
            detail=f"enactment {enactment_id} does not exist",
        )

    opportunity_id = int(enactment.get("opportunity_id") or 0)
    if enactment.get("failed_at") is not None:
        return EnactmentResult(
            status="blocked",
            opportunity_id=opportunity_id,
            enactment_id=enactment_id,
            detail="cannot restore a failed enactment (soft-delete already rolled back)",
        )
    if enactment.get("restored_at") is not None:
        return EnactmentResult(
            status="blocked",
            opportunity_id=opportunity_id,
            enactment_id=enactment_id,
            detail="enactment already restored",
        )

    library_slug = enactment.get("library_slug") or ""
    library = next((l for l in libs if l.get("slug") == library_slug), None)
    if library is None:
        return EnactmentResult(
            status="blocked",
            opportunity_id=opportunity_id,
            enactment_id=enactment_id,
            detail=f"library {library_slug!r} is not currently discovered",
        )

    soft_path = enactment.get("owned_path_after") or ""
    original_path = enactment.get("owned_path_before") or ""
    if not (soft_path and original_path):
        return EnactmentResult(
            status="failed",
            opportunity_id=opportunity_id,
            enactment_id=enactment_id,
            detail="enactment audit row is missing path fields",
            error="missing soft_path or original_path",
        )
    if not Path(soft_path).exists():
        return EnactmentResult(
            status="not_found",
            opportunity_id=opportunity_id,
            enactment_id=enactment_id,
            detail=(
                f"soft-delete file at {soft_path} is gone "
                f"(retention sweeper may have purged it)"
            ),
        )

    # Refuse if the original location is already occupied — restoring
    # would overwrite the wrong file. The operator can manually
    # reconcile before calling restore again.
    if Path(original_path).exists():
        return EnactmentResult(
            status="blocked",
            opportunity_id=opportunity_id,
            enactment_id=enactment_id,
            detail=(
                f"original path {original_path} is occupied; refusing "
                f"to overwrite. Manually inspect before retrying."
            ),
        )

    # Move file back to original location.
    moved_ok, move_err = _move_dir(soft_path, original_path)
    if not moved_ok:
        return EnactmentResult(
            status="failed",
            opportunity_id=opportunity_id,
            enactment_id=enactment_id,
            detail=f"restore move failed: {move_err}",
            error=move_err,
        )

    # Re-register with the library app. For Calibre + CWA: pick a
    # representative file and call `sink.deliver()`. For ABS: just
    # trigger a library scan via the existing scan endpoint (filesystem
    # is authoritative, scan picks the restored folder up).
    sink_kind, sink, sink_err = _select_sink_for_library(library, settings)
    re_register_detail: Optional[str] = None
    if sink is None:
        # Couldn't construct a sink. The file IS already physically
        # back; flag the audit with a non-fatal warning so the user
        # knows to manually re-add to the library if needed.
        re_register_detail = f"no sink to re-register: {sink_err}"
    else:
        if sink_kind == "abs":
            # ABS scan trigger only — no file copy. Call the private
            # helper directly; AudiobookshelfSink only exposes scan
            # via deliver/remove, both of which expect a path. Use
            # `remove(path=non_existent_path)` to hit the idempotent
            # "no path, but trigger scan" branch.
            scan_result = await sink.remove(path="")  # type: ignore[arg-type]
            re_register_detail = scan_result.detail or "abs rescan triggered"
            if not scan_result.success:
                # Path required by ABSSink.remove — we use the original
                # restored path as the trigger anchor (file is back, so
                # the idempotent-success branch fires).
                scan_result = await sink.remove(path=original_path)
                re_register_detail = scan_result.detail or "abs rescan triggered"
        else:
            # Calibre or CWA — pick the first file inside the restored
            # directory and call deliver. The sink handles dedup;
            # calibredb add returns "already in library" gracefully
            # when the row never got dropped (UAT-edge: enact removed
            # the row, restore needs to re-add).
            picked: Optional[Path] = None
            for p in Path(original_path).rglob("*"):
                if p.is_file():
                    picked = p
                    break
            if picked is not None:
                meta = BookMetadata()  # minimal — sink uses defaults
                deliver_result = await sink.deliver(str(picked), meta)
                re_register_detail = (
                    deliver_result.detail or deliver_result.error
                    or f"{sink_kind} re-deliver returned no detail"
                )
            else:
                re_register_detail = (
                    f"restored directory {original_path} has no files; "
                    f"library re-registration skipped"
                )

    # Stamp restored_at + flip opportunity back to 'detected'.
    await enactments.mark_enactment_restored(
        db, enactment_id, restored_by=restored_by,
    )
    await opportunities.update_status(
        db, opportunity_id,
        status="detected",
        acted_by=restored_by,
    )

    _log.info(
        "active replacement: restored enactment %s for opportunity %s "
        "in library %s (sink=%s)",
        enactment_id, opportunity_id, library_slug, sink_kind or "<none>",
    )
    return EnactmentResult(
        status="restored",
        opportunity_id=opportunity_id,
        enactment_id=enactment_id,
        detail=(
            f"restored {original_path} from {soft_path}; "
            f"re-registration: {re_register_detail}"
        ),
    )


# ─── Phase 5b Phase 6 — retention sweeper ────────────────────


def purge_expired_soft_deletes(
    settings: Optional[dict] = None,
    libraries: Optional[list[dict]] = None,
) -> dict:
    """Walk every library's `.seshat-replaced/` folder and purge
    timestamp subdirs older than the configured retention window.

    The folder layout (`_soft_delete_dir_for`) is:
        <library_path>/.seshat-replaced/<YYYYMMDD-HHMMSS>/<book_dir>/...

    Each immediate child of `.seshat-replaced/` is a timestamp. We
    parse the name; if the parse fails OR if the age (now - timestamp)
    is older than `active_replacement_soft_delete_retention_days * 86400`
    seconds, the whole subtree is `shutil.rmtree`'d.

    Returns a stats dict:
        {
            "purged":     N total subtrees deleted across libraries
            "kept":       M total subtrees within retention window
            "malformed":  K dirs whose name didn't parse as a timestamp
            "errors":     E rmtree failures (logged but don't raise)
            "per_library": [{slug, library_path, purged, kept, malformed, errors}, ...]
        }

    Pure-Python + filesystem only. No DB writes — the audit
    `replacement_enactments` rows survive purge; their
    `owned_path_after` becomes a dangling pointer. The restore
    endpoint already handles "soft-delete gone" with a 404 + the
    "retention sweeper may have purged it" hint.

    Idempotent: re-running is a no-op once steady state is reached.
    """
    from app.config import load_settings

    if settings is None:
        settings = load_settings()
    libs = libraries if libraries is not None else list(state._discovered_libraries)

    retention_days = int(
        settings.get("active_replacement_soft_delete_retention_days") or 30
    )
    if retention_days < 1:
        # Defensive: never accept zero/negative retention; the
        # settings UI clamps to ≥1 but a hand-edited settings.json
        # could land here.
        retention_days = 30
    cutoff = time.time() - retention_days * 86400

    totals = {"purged": 0, "kept": 0, "malformed": 0, "errors": 0}
    per_library: list[dict] = []

    for lib in libs:
        library_path = lib.get("library_path") or ""
        slug = lib.get("slug") or ""
        if not library_path:
            continue
        soft_root = Path(library_path) / ".seshat-replaced"
        lib_stats = {
            "slug": slug, "library_path": library_path,
            "purged": 0, "kept": 0, "malformed": 0, "errors": 0,
        }
        if not soft_root.exists() or not soft_root.is_dir():
            per_library.append(lib_stats)
            continue

        for entry in soft_root.iterdir():
            if not entry.is_dir():
                # Stray files don't belong here; skip them so the
                # sweep doesn't blast an operator's manual artifact.
                continue
            ts = _parse_soft_delete_timestamp(entry.name)
            if ts is None:
                lib_stats["malformed"] += 1
                continue
            if ts >= cutoff:
                lib_stats["kept"] += 1
                continue
            # Older than retention → purge.
            try:
                shutil.rmtree(entry)
                lib_stats["purged"] += 1
                _log.info(
                    "soft-delete retention: purged %s "
                    "(library=%s, age=%.1f days)",
                    entry, slug,
                    (time.time() - ts) / 86400,
                )
            except OSError as e:
                lib_stats["errors"] += 1
                _log.warning(
                    "soft-delete retention: rmtree failed for %s: "
                    "%s: %s",
                    entry, type(e).__name__, e,
                )

        for k in ("purged", "kept", "malformed", "errors"):
            totals[k] += lib_stats[k]
        per_library.append(lib_stats)

    return {**totals, "per_library": per_library}


def _parse_soft_delete_timestamp(name: str) -> Optional[float]:
    """Parse a `YYYYMMDD-HHMMSS` directory name to a unix timestamp.

    Returns None on any parse failure so the caller can flag the dir
    as malformed without crashing the sweep. The format must match
    exactly — `time.strftime("%Y%m%d-%H%M%S")` is what
    `_soft_delete_dir_for` writes, so anything else here is either
    an operator-created folder we shouldn't touch OR a future-version
    name we don't recognize.
    """
    try:
        struct = time.strptime(name, "%Y%m%d-%H%M%S")
    except (ValueError, TypeError):
        return None
    try:
        # `mktime` interprets the struct as local time, which matches
        # how _soft_delete_dir_for stamped it via strftime. UTC drift
        # is up to ±1 day across local-time epoch boundaries, which
        # is negligible at the retention granularity we care about
        # (default 30 days; nobody tunes this below 1 day).
        return time.mktime(struct)
    except (ValueError, OverflowError):
        return None
