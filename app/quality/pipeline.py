"""
Pipeline integration for quality-metadata extraction.

`extract_for_torrent(db, mam_torrent_id)` is the single entry point.
It checks for an existing extraction, fetches torrent info from MAM
(with `mediaInfo` opt-in), parses via `app.quality.extract`, and
upserts the result via `app.quality.storage`.

Used in two places:

  1. Inline on `book_grab_links` insert (acquisition_linkback.py) so
     new grabs auto-populate their quality data within seconds.

  2. The backfill worker (`app/quality/worker.py`) for catching up
     existing grabs that pre-date the feature.

Failures are logged but never raised — quality extraction is an
auxiliary signal; the calling path (grab linkage, sync sweep, etc.)
must not fail because MAM was momentarily unreachable.
"""
from __future__ import annotations

import logging
from typing import Optional

import aiosqlite

from app.mam.torrent_info import (
    TorrentInfoError,
    get_torrent_info,
)
from app.quality.extract import QualitySnapshot, extract_quality
from app.quality.storage import get_quality, upsert_quality

_log = logging.getLogger("seshat.quality.pipeline")


async def extract_for_torrent(
    db: aiosqlite.Connection,
    mam_torrent_id: str,
    *,
    force_refresh: bool = False,
) -> Optional[QualitySnapshot]:
    """Fetch + parse + persist quality data for one MAM torrent.

    Returns the persisted QualitySnapshot on success, or None if
    extraction was skipped (already cached + `force_refresh=False`)
    or failed.

    `force_refresh=True` skips the existence check and always re-calls
    MAM. Used by the backfill button when an operator explicitly
    wants to refresh stale rows (e.g., post-deploy after a parser fix).
    """
    if not mam_torrent_id:
        return None

    # Skip if already extracted — saves a MAM call on bundle dispatch
    # where N child grabs share one torrent ID. Backfill worker calls
    # this with force_refresh=False too; it iterates the
    # `list_missing_quality_torrent_ids` result, so re-checking here
    # is redundant but harmless and saves a race condition window
    # where two concurrent extractions could collide on the same row.
    if not force_refresh:
        existing = await get_quality(db, mam_torrent_id)
        if existing is not None:
            return None

    try:
        info = await get_torrent_info(mam_torrent_id, ttl=0)
    except TorrentInfoError as e:
        _log.warning(
            "quality extraction: torrent_info failed for %s: %s",
            mam_torrent_id, e,
        )
        return None

    snapshot = extract_quality(
        mam_torrent_id=mam_torrent_id,
        raw_mediainfo=info.mediainfo,
        description=info.description,
        tags=info.tags,
        raw_size=info.size,
        numfiles=info.numfiles,
        seeders=info.seeders,
        times_completed=info.times_completed,
        torrent_added_at=info.added,
    )

    try:
        await upsert_quality(db, snapshot)
        await db.commit()
    except Exception as e:
        _log.warning(
            "quality extraction: upsert failed for %s: %s",
            mam_torrent_id, e,
        )
        return None

    _log.info(
        "quality extracted for tid=%s: source=%s codec=%s bitrate=%s",
        mam_torrent_id,
        snapshot.source,
        snapshot.audio_format,
        snapshot.audio_bitrate_kbps,
    )
    return snapshot
