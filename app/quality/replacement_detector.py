"""
Replacement opportunity detector (v2.26.0 — Bundle A.2 Phase 5a).

Runs after a grab reaches STATE_COMPLETE in the pipeline. The grab's
quality snapshot has been extracted; this module scores it against
every owned book in every replacement-allowed library that shares the
candidate's dedup_key + media_type. Score wins (candidate < owned)
become rows in `replacement_opportunities` for user review.

DETECTION-ONLY in v2.26.0. No file modification happens here; Phase 5b
will add an opt-in "enact" path that consumes detected opportunities.

Hook integration: `pipeline.py` calls `detect_for_grab(grab_id, db)`
in a try/except after STATE_COMPLETE is set. Failures must not roll
back the grab — replacement detection is auxiliary signal.
"""
from __future__ import annotations

import logging
from typing import Optional

import aiosqlite

from app import state
from app.discovery.database import get_db as get_library_db
from app.orchestrator.active_replacement import is_replacement_allowed
from app.orchestrator.format_dedup import (
    media_type_from_category,
    normalize_dedup_key,
)
from app.quality.extract import QualitySnapshot
from app.quality.opportunities import record_opportunity
from app.quality.scoring import resolve_profile_for_library, score_quality
from app.quality.storage import get_quality, get_quality_for_book

_log = logging.getLogger("seshat.quality.replacement_detector")


# ─── Internal helpers ────────────────────────────────────────


def _snapshot_from_row(row: Optional[dict]) -> Optional[QualitySnapshot]:
    """Re-hydrate a `torrent_quality_metadata` row into a QualitySnapshot.

    Returns None when `row` is None or its `mam_torrent_id` is empty
    (the row never actually got extracted). The scoring helpers accept
    None for unknown snapshots; this just maintains type cleanliness.
    """
    if not row or not row.get("mam_torrent_id"):
        return None
    return QualitySnapshot(
        mam_torrent_id=row["mam_torrent_id"],
        source=row.get("source") or "none",
        audio_format=row.get("audio_format"),
        audio_bitrate_kbps=row.get("audio_bitrate_kbps"),
        audio_channels=row.get("audio_channels"),
        audio_bitrate_mode=row.get("audio_bitrate_mode"),
        audio_sample_rate=row.get("audio_sample_rate"),
        audio_compression=row.get("audio_compression"),
        audio_codec_id=row.get("audio_codec_id"),
        audio_duration_sec=row.get("audio_duration_sec"),
        audio_chapter_count=row.get("audio_chapter_count"),
        container_format=row.get("container_format"),
        num_files=row.get("num_files"),
        total_size_bytes=row.get("total_size_bytes"),
        seeders=row.get("seeders"),
        times_completed=row.get("times_completed"),
        torrent_added_at=row.get("torrent_added_at"),
        raw_mediainfo=row.get("raw_mediainfo"),
        raw_tags=row.get("raw_tags"),
    )


async def _owned_match_for_library(
    library_slug: str,
    dedup_key: str,
    media_type: str,
) -> list[dict]:
    """Find owned books matching the dedup key in one library's DB.

    Returns rows of `{book_id, formats, mam_torrent_id}` for each owned
    book whose normalized title+author key equals `dedup_key`.
    `mam_torrent_id` comes from the grabs join (NULL when the owned
    book wasn't acquired through Seshat — pre-existing libraries are
    common).

    Empty list on any DB error — detection is best-effort.
    """
    try:
        lib_db = await get_library_db(library_slug)
    except Exception as e:
        _log.debug(
            "replacement detector: library %s open failed: %s",
            library_slug, e,
        )
        return []
    try:
        # Pull owned books + the mam_torrent_id of the grab that brought
        # them in (via the global book_grab_links join). The per-library
        # DB doesn't have grabs; we'd need a cross-DB join. Since SQLite
        # doesn't span connections, do the second lookup in the app DB
        # below. For now, return book candidates from the library DB.
        cursor = await lib_db.execute(
            "SELECT b.id AS book_id, b.title, b.formats, "
            "       a.name AS author_name, b.mam_torrent_id "
            "FROM books b JOIN authors a ON a.id = b.author_id "
            "WHERE b.hidden = 0 AND b.owned = 1"
        )
        rows = await cursor.fetchall()
    finally:
        await lib_db.close()

    matches: list[dict] = []
    for r in rows:
        row_key = normalize_dedup_key(r["title"] or "", r["author_name"] or "")
        if row_key and row_key == dedup_key:
            formats_csv = (r["formats"] or "").lower()
            first_fmt = (formats_csv.split(",")[0] or "").strip()
            matches.append({
                "book_id": r["book_id"],
                "first_format": first_fmt,
                "mam_torrent_id": r["mam_torrent_id"],
            })
    return matches


# ─── Public entrypoint ───────────────────────────────────────


async def detect_for_grab(
    db: aiosqlite.Connection,
    *,
    grab_id: int,
    settings: dict,
    libraries: Optional[list[dict]] = None,
) -> int:
    """Detect replacement opportunities for one freshly-completed grab.

    Workflow:
      1. Load the grab row + compute dedup_key + media_type.
      2. Load the candidate's QualitySnapshot from
         torrent_quality_metadata (None falls back to format-only).
      3. For each library where active replacement is allowed (per
         `is_replacement_allowed`) and whose content_type matches the
         candidate's media_type:
            a. Resolve per-library QualityProfile.
            b. Score the candidate.
            c. Find owned books matching the dedup_key.
            d. For each owned book: score it, compare. If
               candidate_score < owned_score and they're not the same
               torrent: insert a row in replacement_opportunities.

    Returns the number of opportunities recorded (0 when nothing
    qualifies — the common case).

    Errors are caught + logged + the function returns 0 rather than
    raising — caller (pipeline) shouldn't rollback on detection issues.
    """
    if not grab_id:
        return 0

    try:
        cursor = await db.execute(
            "SELECT id, mam_torrent_id, torrent_name, author_blob, "
            "       category, book_format "
            "FROM grabs WHERE id = ?",
            (grab_id,),
        )
        grab_row = await cursor.fetchone()
        if grab_row is None:
            return 0

        cand_torrent_id = grab_row["mam_torrent_id"]
        if not cand_torrent_id:
            return 0

        cand_format = (grab_row["book_format"] or "").strip().lower()
        cand_category = grab_row["category"] or ""
        cand_title = grab_row["torrent_name"] or ""
        cand_author = grab_row["author_blob"] or ""

        media_type = media_type_from_category(cand_category)
        if not media_type:
            return 0

        dedup_key = normalize_dedup_key(cand_title, cand_author)
        if not dedup_key:
            return 0

        cand_snapshot = _snapshot_from_row(await get_quality(db, cand_torrent_id))

        libs = (
            libraries if libraries is not None
            else list(state._discovered_libraries)
        )

        recorded = 0
        for lib in libs:
            slug = lib.get("slug")
            if not slug:
                continue
            if (lib.get("content_type") or "") != media_type:
                continue
            if not is_replacement_allowed(slug, settings, libraries=libs):
                continue

            profile = resolve_profile_for_library(media_type, slug, settings)
            if profile is None:
                continue

            cand_score = score_quality(
                profile=profile, fmt=cand_format, snapshot=cand_snapshot,
            )

            matches = await _owned_match_for_library(slug, dedup_key, media_type)
            for owned in matches:
                owned_tid = owned.get("mam_torrent_id")
                # Self-comparison guard: the new grab might be the same
                # torrent that brought this owned row in (re-detection
                # after backfill, or duplicate insert). Skip silently.
                if owned_tid and owned_tid == cand_torrent_id:
                    continue

                owned_snapshot: Optional[QualitySnapshot] = None
                if owned_tid:
                    owned_snapshot = _snapshot_from_row(
                        await get_quality_for_book(db, slug, owned["book_id"]),
                    )

                owned_score = score_quality(
                    profile=profile,
                    fmt=owned["first_format"],
                    snapshot=owned_snapshot,
                )

                # Lower tuple = better quality. We only record when the
                # candidate strictly beats owned.
                if not (cand_score < owned_score):
                    continue

                inserted = await record_opportunity(
                    db,
                    candidate_grab_id=grab_id,
                    candidate_mam_torrent_id=cand_torrent_id,
                    candidate_format=cand_format or None,
                    candidate_score=cand_score,
                    owned_library_slug=slug,
                    owned_book_id=owned["book_id"],
                    owned_mam_torrent_id=owned_tid,
                    owned_format=owned["first_format"] or None,
                    owned_score=owned_score,
                    media_type=media_type,
                )
                if inserted:
                    recorded += 1

        if recorded:
            await db.commit()
            _log.info(
                "replacement detector: %d opportunity(ies) recorded for "
                "grab_id=%d (tid=%s, dedup_key=%s)",
                recorded, grab_id, cand_torrent_id, dedup_key,
            )
        return recorded
    except Exception:
        _log.exception(
            "replacement detector: unexpected failure for grab_id=%d "
            "(best-effort; not raising)",
            grab_id,
        )
        return 0
