"""
Manual Grab HTTP endpoints — the "Grab from MAM" page.

    POST /api/v1/manual-grab/preview        one row's preview
    POST /api/v1/manual-grab/grab           start a Grab all job (≤30 rows)
    GET  /api/v1/manual-grab/grab/{job_id}  the job's per-row status
    GET  /api/v1/manual-grab/cover/{tid}    a cached preview thumbnail

The page calls `/preview` once per row, in sequence, so rows fill in as
they arrive; the pacer behind it spaces the MAM calls whoever calls.
The batch cap is enforced here, not just in the UI. The logic lives in
`app/orchestrator/manual_grab.py`; see ADR-0023 for the rules.

Session auth (auth_secret cookie) is enforced by the global middleware.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import state
from app.mam.torrent_id import extract_torrent_id
from app.orchestrator import manual_grab

router = APIRouter(prefix="/api/v1/manual-grab", tags=["manual-grab"])


class PreviewRequest(BaseModel):
    kind: Literal["link"]
    value: str = Field(..., min_length=1, max_length=2000)


class GrabItemIn(BaseModel):
    kind: Literal["link"]
    value: str = Field(..., min_length=1, max_length=2000)
    buy_personal_fl: bool = False
    # Set only by the row's "Download again" confirm (D13).
    override_mam_snatched: bool = False


class GrabRequest(BaseModel):
    items: list[GrabItemIn] = Field(..., min_length=1, max_length=manual_grab.MAX_BATCH)


def _deps():
    if state.dispatcher is None:
        raise HTTPException(503, "dispatcher not initialized yet")
    return state.dispatcher


@router.post("/preview")
async def preview(body: PreviewRequest) -> dict:
    row = await manual_grab.preview_link(_deps(), body.value)
    return row.to_dict()


@router.post("/grab")
async def grab(body: GrabRequest) -> dict:
    deps = _deps()
    items: list[manual_grab.GrabRequestItem] = []
    seen: set[str] = set()
    duplicates: list[int] = []
    for i, it in enumerate(body.items):
        tid = extract_torrent_id(it.value)
        if tid is not None and tid in seen:
            duplicates.append(i)
            continue
        if tid is not None:
            seen.add(tid)
        items.append(manual_grab.GrabRequestItem(
            kind=it.kind,
            value=it.value,
            buy_personal_fl=it.buy_personal_fl,
            override_mam_snatched=it.override_mam_snatched,
        ))
    if duplicates:
        raise HTTPException(
            422,
            "The same torrent is in this batch more than once "
            f"(rows {', '.join(str(i + 1) for i in duplicates)}).",
        )
    job = manual_grab.start_job(deps, items)
    return job.to_dict()


@router.get("/grab/{job_id}")
async def grab_status(job_id: str) -> dict:
    job = manual_grab.get_job(job_id)
    if job is None:
        raise HTTPException(
            404,
            "No such batch. Seshat may have restarted; anything it had "
            "already grabbed is in the grab history.",
        )
    return job.to_dict()


@router.get("/cover/{torrent_id}")
async def cover(torrent_id: str):
    if not torrent_id.isdigit():
        raise HTTPException(400, "bad torrent id")
    path = manual_grab.cached_cover_path(torrent_id)
    if path is None:
        raise HTTPException(404, "no cover cached")
    return FileResponse(path)
