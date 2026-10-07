"""
Delayed-torrents manager endpoints.

    GET  /api/v1/delayed             — scan the delayed folder and list files
    POST /api/v1/delayed/{filename}/reinject
                                     — submit the file's own bytes (budget
                                       permitting, else queue them), then
                                       delete the file
    DELETE /api/v1/delayed/{filename}
                                     — just delete the file (user gave up)

The delayed folder is a flat directory of .torrent files named
`<grab_id>_<mam_torrent_id>.torrent`. There's no DB tracking — the
filesystem IS the queue (user decision #4). The GET endpoint scans
and parses the filenames to build a list.

Reinject never fetches from MAM (snatch safety, ADR-0022): the delayed
file IS the .torrent, so its bytes go through the dispatcher's bytes-in
path (`submit_torrent_bytes`) on the grab row named in the filename.
Before that, one search-API call confirms the torrent is still on MAM —
a removed torrent would sit at 0% in qBit forever.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import state
from app.config import load_settings
from app.database import get_db
from app.orchestrator import torrent_store
from app.orchestrator.dispatch import (
    grab_claim_lock,
    release_write_lock,
    submit_torrent_bytes,
)
from app.storage import grabs as grabs_storage

_log = logging.getLogger("seshat.routers.delayed")

router = APIRouter(prefix="/api/v1/delayed", tags=["delayed"])

_FILENAME_RX = re.compile(r"^(\d+)_(\d+)\.torrent$")


class DelayedItem(BaseModel):
    filename: str
    grab_id: int
    mam_torrent_id: str
    size_bytes: int


class DelayedListResponse(BaseModel):
    path: str
    items: list[DelayedItem]


class ReinjectResponse(BaseModel):
    ok: bool
    grab_id: Optional[int] = None
    error: Optional[str] = None


class SimpleOk(BaseModel):
    ok: bool


def _get_delayed_path() -> Path:
    settings = load_settings()
    p = settings.get("delayed_torrents_path", "") or ""
    if not p:
        raise HTTPException(
            404, "delayed_torrents_path not configured in settings"
        )
    return Path(p)


def _scan(folder: Path) -> list[DelayedItem]:
    if not folder.exists():
        return []
    items: list[DelayedItem] = []
    for f in sorted(folder.iterdir()):
        if not f.is_file():
            continue
        m = _FILENAME_RX.match(f.name)
        if not m:
            continue
        items.append(
            DelayedItem(
                filename=f.name,
                grab_id=int(m.group(1)),
                mam_torrent_id=m.group(2),
                size_bytes=f.stat().st_size,
            )
        )
    return items


@router.get("", response_model=DelayedListResponse)
async def list_delayed() -> DelayedListResponse:
    folder = _get_delayed_path()
    return DelayedListResponse(path=str(folder), items=_scan(folder))


def _validate_filename(filename: str) -> re.Match[str]:
    """Reject any filename that isn't a single safe component matching
    the expected `<grab_id>_<mam_id>.torrent` shape. Runs before any
    filesystem call so a malformed value can't escape the delayed
    folder via traversal segments."""
    if "/" in filename or "\\" in filename or "\x00" in filename:
        raise HTTPException(400, "invalid filename")
    m = _FILENAME_RX.match(filename)
    if not m:
        raise HTTPException(400, f"filename {filename} doesn't match expected pattern")
    return m


@router.post("/{filename}/reinject", response_model=ReinjectResponse)
async def reinject(filename: str) -> ReinjectResponse:
    if state.dispatcher is None:
        raise HTTPException(503, "dispatcher not initialized")
    deps = state.dispatcher

    m = _validate_filename(filename)
    folder = _get_delayed_path()
    fpath = folder / filename

    if not fpath.exists():
        raise HTTPException(404, f"{filename} not found in delayed folder")

    grab_id = int(m.group(1))
    mam_id = m.group(2)
    try:
        torrent_bytes = fpath.read_bytes()
    except OSError as e:
        raise HTTPException(500, f"could not read {filename}: {e}")

    liveness = await torrent_store.check_still_on_mam(
        mam_id, deps.live_mam_token(),
    )
    if liveness == "unreachable":
        return ReinjectResponse(
            ok=False, grab_id=grab_id,
            error=(
                "Couldn't confirm the torrent is still on MAM (MAM "
                "unreachable, or the cookie needs attention). Nothing was "
                "submitted; try again later."
            ),
        )

    db = await get_db()
    try:
        if liveness == "removed":
            detail = f"torrent {mam_id} was removed from MAM; delayed file deleted"
            if await grabs_storage.get_grab(db, grab_id) is not None:
                await grabs_storage.set_state(
                    db, grab_id, grabs_storage.STATE_FAILED_TORRENT_GONE,
                    failed_reason=detail,
                )
            _unlink(fpath)
            return ReinjectResponse(ok=False, grab_id=grab_id, error=detail)

        # Claim the grab row so a double-click, or an inject of the same
        # torrent ID racing this one, can't place it twice.
        await release_write_lock(db)
        async with grab_claim_lock():
            grab = await grabs_storage.get_grab(db, grab_id)
            if grab is None:
                return ReinjectResponse(
                    ok=False, grab_id=grab_id,
                    error=f"grab #{grab_id} not found; the file was left in place",
                )
            if grab.state in grabs_storage.BLOCKING_STATES:
                return ReinjectResponse(
                    ok=False, grab_id=grab_id,
                    error=f"grab #{grab_id} is already {grab.state}",
                )
            other = await grabs_storage.find_blocking_grab(
                db, mam_id, exclude_grab_id=grab_id,
            )
            if other is not None:
                return ReinjectResponse(
                    ok=False, grab_id=other.id,
                    error=(
                        f"torrent {mam_id} was grabbed again as grab "
                        f"#{other.id} ({other.state}); nothing was submitted"
                    ),
                )
            prior_state, prior_reason = grab.state, grab.failed_reason
            await grabs_storage.set_state(
                db, grab_id, grabs_storage.STATE_FETCHED,
            )

        result = await submit_torrent_bytes(
            deps, grab_id=grab_id, torrent_bytes=torrent_bytes,
        )
        placed = result.action in ("submit", "queue")
        if not placed:
            # Nothing happened (dry run, budget+queue full, bad file):
            # release the claim so the user can try again.
            await grabs_storage.set_state(
                db, grab_id, prior_state, failed_reason=prior_reason or "",
            )
    finally:
        await db.close()

    pipeline_ok = placed and result.error is None
    if pipeline_ok:
        _unlink(fpath)

    return ReinjectResponse(
        ok=pipeline_ok,
        grab_id=result.grab_id,
        error=result.error,
    )


def _unlink(fpath: Path) -> None:
    try:
        fpath.unlink()
    except OSError:
        _log.exception("failed to delete delayed file %s", fpath)


@router.delete("/{filename}", response_model=SimpleOk)
async def delete_delayed(filename: str) -> SimpleOk:
    _validate_filename(filename)
    folder = _get_delayed_path()
    fpath = folder / filename
    if not fpath.exists():
        raise HTTPException(404, f"{filename} not found")
    try:
        fpath.unlink()
        return SimpleOk(ok=True)
    except OSError as e:
        raise HTTPException(500, f"Failed to delete: {e}")
