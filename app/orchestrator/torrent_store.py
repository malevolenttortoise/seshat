"""
On-disk store for the .torrent bytes of queued grabs.

Snatch safety (ADR-0022): MAM sees one download per torrent, ever. A
grab that can't go to qBit yet (budget full, client unreachable) used
to drop its bytes and re-fetch them from MAM when the budget watcher
popped it, so every queued grab cost two MAM downloads. The bytes now
live here, keyed by grab id, from the moment the grab is queued until
it reaches qBit or a terminal state.

    <DATA_DIR>/queued-torrents/<grab_id>.torrent

The path is also stamped on `grabs.torrent_file_path`. Files carry the
user's passkey (it is in the announce URL), the same exposure as the
delayed-torrents folder, so they are written owner-only.

Also home to `check_still_on_mam`, the liveness check run before saved
bytes go to qBit: a torrent removed from MAM while it sat in the queue
would otherwise land in qBit and sit at 0% forever.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal, Optional

import aiosqlite

from app import config
from app.mam.torrent_info import (
    TorrentInfoError,
    TorrentNotFoundError,
    get_torrent_info,
)
from app.storage import grabs as grabs_storage

_log = logging.getLogger("seshat.orchestrator.torrent_store")

_DIRNAME = "queued-torrents"


def store_dir() -> Path:
    # Resolved per call (not bound at import) so the test suite's
    # DATA_DIR redirect applies.
    return Path(config.DATA_DIR) / _DIRNAME


def save(grab_id: int, torrent_bytes: bytes) -> Path:
    """Write a queued grab's bytes; returns the path. Raises OSError.

    Written to a temp name and renamed, so a crash mid-write never
    leaves a truncated file that a later pop would hand to qBit.
    """
    folder = store_dir()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{grab_id}.torrent"
    tmp = folder / f".{grab_id}.torrent.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, torrent_bytes)
    finally:
        os.close(fd)
    os.replace(tmp, dest)
    return dest


def load(path: Optional[str]) -> Optional[bytes]:
    """The saved bytes, or None when there's no path or no file."""
    if not path:
        return None
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def discard(path: Optional[str]) -> None:
    """Delete a saved file; a missing file is not an error."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        _log.warning("could not delete saved .torrent %s", path, exc_info=True)


async def sweep(db: aiosqlite.Connection) -> int:
    """Delete saved files whose grab is no longer queued.

    A file only exists for a grab sitting in `pending_queue`; anything
    else is left over from a crash between submit and delete, or from a
    queued grab that was removed some other way. Run at startup.
    Returns the number of files deleted.
    """
    folder = store_dir()
    if not folder.is_dir():
        return 0
    cursor = await db.execute("SELECT grab_id FROM pending_queue")
    queued = {int(r[0]) for r in await cursor.fetchall()}
    removed = 0
    for f in folder.iterdir():
        stem = f.name.removesuffix(".torrent")
        keep = (
            f.is_file()
            and f.name.endswith(".torrent")
            and stem.isdigit()
            and int(stem) in queued
        )
        if keep:
            continue
        try:
            f.unlink()
            removed += 1
        except OSError:
            _log.warning("sweep: could not delete %s", f, exc_info=True)
    if removed:
        _log.info("torrent store sweep: deleted %d stale file(s)", removed)
    return removed


async def notify_grab_failed(grab_id: int, detail: str) -> None:
    """`grab.failed` notification; never raises."""
    try:
        from app.notifications import bus, events
        await bus.emit(
            events.GRAB_FAILED,
            title="Grab needs attention",
            message=f"Grab #{grab_id}: {detail}",
        )
    except Exception:
        _log.exception("grab.failed bus emit failed (non-fatal)")


async def fail_missing_file(
    db: aiosqlite.Connection, grab: grabs_storage.GrabRow,
) -> None:
    """Fail a queued grab whose saved bytes are gone — loudly, and
    without re-fetching (ADR-0022). Mark's call at Phase 0 kickoff:
    a volume problem or a manual delete must surface, not be papered
    over with a second MAM download. The caller has already taken the
    grab out of `pending_queue`.
    """
    where = grab.torrent_file_path or "no saved file recorded"
    detail = (
        f"queued .torrent missing from disk ({where}); not re-fetched "
        "from MAM, since that would be a second download"
    )
    _log.error("grab %d (tid=%s): %s", grab.id, grab.mam_torrent_id, detail)
    await grabs_storage.set_state(
        db, grab.id, grabs_storage.STATE_FAILED_UNKNOWN, failed_reason=detail,
    )
    await notify_grab_failed(grab.id, detail)


Liveness = Literal["ok", "removed", "unreachable"]


async def check_still_on_mam(torrent_id: str, token: str) -> Liveness:
    """Is this torrent still on MAM? One fresh search-API call by ID —
    never a .torrent download.

    `removed` is the permanent not-found (ADR-0006). `unreachable`
    covers everything else (network, 5xx, an expired cookie's empty
    response, no cookie at all): the caller holds rather than guessing.
    """
    if not torrent_id:
        return "ok"
    if not token:
        return "unreachable"
    try:
        await get_torrent_info(torrent_id, token=token, ttl=0)
    except TorrentNotFoundError:
        return "removed"
    except TorrentInfoError as e:
        _log.info("liveness check for tid=%s unreachable: %s", torrent_id, e)
        return "unreachable"
    return "ok"
