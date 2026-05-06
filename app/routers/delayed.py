"""
Delayed-torrents manager endpoints.

    GET  /api/v1/delayed             — scan the delayed folder and list files
    POST /api/v1/delayed/{filename}/reinject
                                     — parse the grab_id + mam_id from the
                                       filename and re-inject through the
                                       dispatcher, then delete the file
    DELETE /api/v1/delayed/{filename}
                                     — just delete the file (user gave up)

The delayed folder is a flat directory of .torrent files named
`<grab_id>_<mam_torrent_id>.torrent`. There's no DB tracking — the
filesystem IS the queue (user decision #4). The GET endpoint scans
and parses the filenames to build a list; the reinject endpoint uses
the mam_torrent_id to call `inject_grab` which fetches a fresh
.torrent from MAM and routes through the normal pipeline.
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
from app.orchestrator.dispatch import inject_grab

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

    m = _validate_filename(filename)
    folder = _get_delayed_path()
    fpath = folder / filename

    if not fpath.exists():
        raise HTTPException(404, f"{filename} not found in delayed folder")

    mam_id = m.group(2)
    result = await inject_grab(
        state.dispatcher,
        torrent_id=mam_id,
        raw_line=f"delayed_reinject:{filename}",
    )

    pipeline_ok = result.action in ("submit", "queue") and result.error is None
    if pipeline_ok:
        try:
            fpath.unlink()
        except OSError:
            _log.exception("failed to delete delayed file after reinject")

    return ReinjectResponse(
        ok=pipeline_ok,
        grab_id=result.grab_id,
        error=result.error,
    )


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
