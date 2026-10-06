"""
Delayed-torrents folder rotation.

When the snatch queue is full and a new grab arrives, we don't want
to drop the new grab on the floor — the queue is FIFO, so newer
announces are often the ones the user cares about most right now.
Instead, we rotate:

    1. Pop the OLDEST queued grab
    2. Move its saved .torrent bytes (`torrent_store`) into
       `delayed_torrents_path/` — never re-fetched from MAM
       (snatch safety, ADR-0022)
    3. Remove the popped grab's queue/state entries
    4. Return its id so the caller can re-check queue capacity and
       enqueue the new grab

The delayed folder is a "dead-drop" the user's future UI will scan
and offer a "push back to queue" action for. We deliberately don't
track delayed files in the database (user decision #4) — the
filesystem IS the queue. No migrations, no orphan cleanup.

Filename convention: `<grab_id>_<mam_torrent_id>.torrent`. The
grab id comes first so ls sort order matches insertion order, and
the torrent id is included so the user can locate the MAM page
without crossing back to the DB.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import aiosqlite

from app.orchestrator import torrent_store
from app.rate_limit import queue as queue_mod
from app.storage import grabs as grabs_storage

_log = logging.getLogger("seshat.orchestrator.delayed")


async def rotate_oldest_to_delayed(
    db: aiosqlite.Connection,
    *,
    delayed_path: str,
) -> Optional[int]:
    """Pop the oldest queued grab and park it in the delayed folder.

    Returns the evicted grab_id once its queue slot is free, or None if:
      - the queue is empty
      - the delayed_path isn't configured
      - the disk write failed (the grab stays queued, untouched)

    A queued grab whose saved bytes are missing can't be parked without
    a second MAM download, so it is failed loudly instead — that still
    frees the slot, so its id is returned.
    """
    if not delayed_path:
        _log.debug("rotate_oldest_to_delayed: delayed_path not configured")
        return None

    # Pop ORDERS by priority desc, then queued_at asc, which is NOT
    # pure FIFO if any grab has non-zero priority. For rotation we
    # want the LOWEST-priority, OLDEST grab — the one the user is
    # least likely to miss. Query explicitly.
    cursor = await db.execute(
        """
        SELECT grab_id FROM pending_queue
        ORDER BY priority ASC, queued_at ASC
        LIMIT 1
        """
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    evict_id = int(row["grab_id"])

    grab = await grabs_storage.get_grab(db, evict_id)
    if grab is None:
        # Orphan — just drop the queue row.
        await queue_mod.remove(db, evict_id)
        return None

    torrent_bytes = torrent_store.load(grab.torrent_file_path)
    if torrent_bytes is None:
        await queue_mod.remove(db, evict_id)
        await torrent_store.fail_missing_file(db, grab)
        return evict_id

    try:
        folder = Path(delayed_path)
        folder.mkdir(parents=True, exist_ok=True)
        # Keep the name filesystem-safe — MAM torrent names can
        # contain slashes, quotes, etc. We only use the ID + grab_id
        # to avoid any sanitization bugs.
        dest = folder / f"{evict_id}_{grab.mam_torrent_id}.torrent"
        dest.write_bytes(torrent_bytes)
    except Exception:
        _log.exception(
            "rotate_oldest_to_delayed: write failed grab_id=%d", evict_id
        )
        return None

    # Remove from the pending_queue and mark the grab as failed-unknown
    # with a clear reason. We don't want to leave it in pending_queue
    # state (the budget watcher would try to drain it). A dedicated
    # STATE_DELAYED could be added later if the UI needs the distinction.
    await queue_mod.remove(db, evict_id)
    await grabs_storage.set_state(
        db, evict_id, grabs_storage.STATE_FAILED_UNKNOWN,
        failed_reason="rotated to delayed folder",
    )
    torrent_store.discard(grab.torrent_file_path)

    _log.info(
        "rotate_oldest_to_delayed: parked grab_id=%d → %s",
        evict_id, dest,
    )
    return evict_id
