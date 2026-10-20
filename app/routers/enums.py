"""
MAM enum endpoints for populating UI dropdowns.

    GET  /api/v1/enums                 — categories + languages + formats
    POST /api/v1/enums/refresh         — force a live MAM fetch

The GET endpoint is cheap: it serves from the process-wide in-memory
cache, which falls back to `app/mam/categories.json` if MAM has never
been reached. The refresh endpoint is the only path that triggers
network I/O — wire it up to a "refresh" button in the filter editor.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.mam import enums as mam_enums
from app.config import ui_error

router = APIRouter(prefix="/api/v1/enums", tags=["enums"])


class CategoryItem(BaseModel):
    id: str
    name: str
    main_id: str
    main_name: str
    normalized: str


class V2Enums(BaseModel):
    media_types: list[dict]
    main_categories: list[dict]
    content_tags: list[dict]
    languages: list[dict]


class EnumsResponse(BaseModel):
    categories: list[CategoryItem]
    languages: list[str]
    formats: list[str]
    v2: V2Enums


class RefreshResponse(BaseModel):
    ok: bool
    count: int
    error: Optional[str] = None


def _to_item(c: mam_enums.CategoryEntry) -> CategoryItem:
    return CategoryItem(
        id=c.id,
        name=c.name,
        main_id=c.main_id,
        main_name=c.main_name,
        normalized=c.normalized,
    )


@router.get("", response_model=EnumsResponse)
async def get_enums() -> EnumsResponse:
    cats = await mam_enums.get_categories()
    formats = await mam_enums.get_formats()
    v2_raw = mam_enums.get_v2_enums()
    return EnumsResponse(
        categories=[_to_item(c) for c in cats],
        languages=mam_enums.get_languages(),
        formats=formats,
        v2=V2Enums(**v2_raw),
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_enums() -> RefreshResponse:
    from app.mam.cookie import get_active_token
    token = await get_active_token()
    try:
        count = await mam_enums.refresh(token=token)
        return RefreshResponse(ok=True, count=count)
    except Exception as e:
        return RefreshResponse(ok=False, count=0, error=f"{type(e).__name__}: {ui_error(e)}")
