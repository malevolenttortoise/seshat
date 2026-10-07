"""
Manual Grab HTTP endpoints — the "Grab from MAM" page.

    POST /api/v1/manual-grab/preview        one row's preview (link or .torrent)
    POST /api/v1/manual-grab/grab           start a Grab all job (≤30 rows)
    GET  /api/v1/manual-grab/grab/{job_id}  the job's per-row status
    GET  /api/v1/manual-grab/wedges         wedges a batch may spend
    GET  /api/v1/manual-grab/cover/{tid}    a cached preview thumbnail

The page calls `/preview` once per row, in sequence, so rows fill in as
they arrive; the pacer behind it spaces the MAM calls whoever calls.
The batch cap is enforced here, not just in the UI. The logic lives in
`app/orchestrator/manual_grab.py`; see ADR-0023 for the rules.

Session auth (auth_secret cookie) is enforced by the global middleware.
"""
from __future__ import annotations

import base64
import binascii
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from app import state
from app.mam.torrent_id import extract_torrent_id
from app.mam.torrent_meta import BencodeError, read_mam_comment
from app.orchestrator import manual_grab

# base64 of a 1 MB .torrent, plus slack. Uploads travel as base64 in
# JSON (D18): no multipart dependency, and .torrent files are tiny.
_MAX_B64 = (manual_grab.MAX_TORRENT_BYTES * 4) // 3 + 8

router = APIRouter(prefix="/api/v1/manual-grab", tags=["manual-grab"])


class _ItemIn(BaseModel):
    """A pasted link (`value`) or an uploaded .torrent (`name` + `data_b64`)."""

    kind: Literal["link", "file"]
    value: str = Field("", max_length=2000)
    name: str = Field("", max_length=500)
    data_b64: str = Field("", max_length=_MAX_B64)

    @model_validator(mode="after")
    def _shape(self):
        if self.kind == "link" and not self.value.strip():
            raise ValueError("a link item needs `value`")
        if self.kind == "file" and not self.data_b64:
            raise ValueError("a file item needs `data_b64`")
        return self

    def file_bytes(self) -> bytes:
        try:
            return base64.b64decode(self.data_b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, f"{self.name or 'file'}: not valid base64")


class PreviewRequest(_ItemIn):
    pass


class GrabItemIn(_ItemIn):
    buy_personal_fl: bool = False
    # Set only by the row's "Download again" confirm (D13).
    override_mam_snatched: bool = False
    # The batch "Use wedges" toggle, applied by the page to each
    # eligible row (D7). Links only.
    use_wedge: bool = False

    @model_validator(mode="after")
    def _wedge_needs_a_link(self):
        if self.use_wedge and self.kind != "link":
            raise ValueError(
                "an uploaded .torrent can't take a wedge (it rides on the MAM download)"
            )
        return self


class GrabRequest(BaseModel):
    items: list[GrabItemIn] = Field(..., min_length=1, max_length=manual_grab.MAX_BATCH)


def _deps():
    if state.dispatcher is None:
        raise HTTPException(503, "dispatcher not initialized yet")
    return state.dispatcher


@router.post("/preview")
async def preview(body: PreviewRequest) -> dict:
    deps = _deps()
    if body.kind == "file":
        row = await manual_grab.preview_file(deps, body.name, body.file_bytes())
    else:
        row = await manual_grab.preview_link(deps, body.value)
    return row.to_dict()


def _torrent_id_of(item: GrabItemIn, data: Optional[bytes]) -> Optional[str]:
    if item.kind == "link":
        return extract_torrent_id(item.value)
    try:
        stamp = read_mam_comment(data or b"")
    except BencodeError:
        return None
    return stamp.torrent_id if stamp else None


@router.post("/grab")
async def grab(body: GrabRequest) -> dict:
    deps = _deps()
    items: list[manual_grab.GrabRequestItem] = []
    seen: set[str] = set()
    duplicates: list[int] = []
    for i, it in enumerate(body.items):
        data = it.file_bytes() if it.kind == "file" else None
        tid = _torrent_id_of(it, data)
        if tid is not None and tid in seen:
            duplicates.append(i)
            continue
        if tid is not None:
            seen.add(tid)
        items.append(manual_grab.GrabRequestItem(
            kind=it.kind,
            value=it.value if it.kind == "link" else (it.name or "upload.torrent"),
            buy_personal_fl=it.buy_personal_fl,
            override_mam_snatched=it.override_mam_snatched,
            data=data,
            use_wedge=it.use_wedge,
        ))
    if duplicates:
        raise HTTPException(
            422,
            "The same torrent is in this batch more than once "
            f"(rows {', '.join(str(i + 1) for i in duplicates)}).",
        )
    # D9: never part-spend. Checked against a fresh read of the account
    # (the page's count can be minutes old) before anything starts.
    wanted = sum(1 for it in items if it.use_wedge)
    if wanted:
        budget = await manual_grab.wedge_budget(deps, fresh=True)
        if budget is None:
            raise HTTPException(
                409, "Can't read your wedge count from MAM right now; "
                "turn wedges off or try again.",
            )
        if wanted > budget.spendable:
            raise HTTPException(409, manual_grab.wedge_shortfall_message(wanted, budget))
    job = manual_grab.start_job(deps, items)
    return job.to_dict()


@router.get("/wedges")
async def wedges() -> dict:
    """Wedges, the policy's reserve, and how many a batch may spend (D9)."""
    budget = await manual_grab.wedge_budget(_deps())
    if budget is None:
        raise HTTPException(409, "Can't read your wedge count from MAM right now.")
    return budget.to_dict()


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
