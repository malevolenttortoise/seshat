"""
Author manager HTTP endpoints.

    GET    /api/v1/authors                 — counts + first page per list
    GET    /api/v1/authors/{list_name}     — paginated list
                                              (allowed | ignored | tentative_review)
    POST   /api/v1/authors/{list_name}     — add a single author or bulk add
    DELETE /api/v1/authors/{list_name}/{normalized}
                                          — remove a single author
    POST   /api/v1/authors/{list_name}/{normalized}/move
                                          — move to another list
                                              body: {"to": "allowed"|"ignored"}

The frontend uses the GET /api/v1/authors landing call to render
the tab counts in one shot, then drills into the per-list endpoint
when the user clicks a tab. Search + pagination are query params.

Bulk add accepts a `names` list of up to 500 authors per request so
the AuthenticatedTextarea-based "paste a list" UX works without
hammering the endpoint per row.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from app import state
from app.database import get_db
from app.filter.normalize import normalize_author
from app.storage import authors as authors_storage

_log = logging.getLogger("seshat.routers.authors")

router = APIRouter(prefix="/api/v1/authors", tags=["authors"])


_LIST_NAMES = ("allowed", "ignored", "tentative_review")


class AuthorRow(BaseModel):
    name: str
    normalized: str
    source: str
    added_at: str


class AuthorListResponse(BaseModel):
    list_name: str
    count: int
    items: list[AuthorRow]


class AuthorOverviewResponse(BaseModel):
    counts: dict[str, int]
    samples: dict[str, list[AuthorRow]]


class AddAuthorRequest(BaseModel):
    names: list[str] = Field(..., min_length=1, max_length=500)
    source: Optional[str] = None


class AddAuthorResponse(BaseModel):
    added: int
    skipped: int


class MoveAuthorRequest(BaseModel):
    to: str


class SimpleOk(BaseModel):
    ok: bool
    detail: Optional[str] = None


class BulkMoveResponse(BaseModel):
    moved: int


def _validate_list(list_name: str) -> None:
    if list_name not in _LIST_NAMES:
        raise HTTPException(404, f"Unknown list: {list_name}")


@router.get("", response_model=AuthorOverviewResponse)
async def overview() -> AuthorOverviewResponse:
    """Cheap landing call: counts for each list + the first 10 of each."""
    db = await get_db()
    try:
        counts = {
            "allowed": await authors_storage.count_allowed(db),
            "ignored": await authors_storage.count_ignored(db),
            "tentative_review": await authors_storage.count_tentative_review(db),
        }
        samples = {
            "allowed": await authors_storage.list_allowed(db, limit=10),
            "ignored": await authors_storage.list_ignored(db, limit=10),
            "tentative_review": await authors_storage.list_tentative_review(db),
        }
    finally:
        await db.close()
    return AuthorOverviewResponse(
        counts=counts,
        samples={
            k: [AuthorRow(**row) for row in rows] for k, rows in samples.items()
        },
    )


@router.get("/{list_name}", response_model=AuthorListResponse)
async def list_authors(
    list_name: str,
    search: str = Query("", max_length=200),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> AuthorListResponse:
    _validate_list(list_name)
    db = await get_db()
    try:
        if list_name == "allowed":
            rows = await authors_storage.list_allowed(
                db, search=search, limit=limit, offset=offset
            )
            count = await authors_storage.count_allowed(db)
        elif list_name == "ignored":
            rows = await authors_storage.list_ignored(
                db, search=search, limit=limit, offset=offset
            )
            count = await authors_storage.count_ignored(db)
        else:
            rows = await authors_storage.list_tentative_review(db)
            count = await authors_storage.count_tentative_review(db)
    finally:
        await db.close()
    return AuthorListResponse(
        list_name=list_name,
        count=count,
        items=[AuthorRow(**row) for row in rows],
    )


@router.post("/{list_name}", response_model=AddAuthorResponse)
async def add_authors(
    list_name: str, body: AddAuthorRequest
) -> AddAuthorResponse:
    _validate_list(list_name)
    if list_name == "tentative_review":
        # Manual adds to tentative_review don't make sense — that
        # list is auto-populated by reject actions on the tentative
        # torrent flow. Refuse to keep the data path clean.
        raise HTTPException(
            400, "tentative_review is auto-populated; add via reject flow"
        )

    source = body.source or "manual"
    added = 0
    skipped = 0
    db = await get_db()
    try:
        for raw in body.names:
            name = (raw or "").strip()
            if not name or not normalize_author(name):
                skipped += 1
                continue
            if list_name == "allowed":
                ok = await authors_storage.add_allowed(db, name, source=source)
            else:
                ok = await authors_storage.add_ignored(db, name, source=source)
            if ok:
                added += 1
            else:
                skipped += 1
    finally:
        await db.close()
    _log.info("authors: added %d / skipped %d to %s", added, skipped, list_name)
    if added > 0 and list_name in ("allowed", "ignored"):
        await state.refresh_filter_authors()
    return AddAuthorResponse(added=added, skipped=skipped)


@router.delete("/{list_name}/{normalized}", response_model=SimpleOk)
async def remove_author(list_name: str, normalized: str) -> SimpleOk:
    _validate_list(list_name)
    db = await get_db()
    try:
        if list_name == "allowed":
            n = await authors_storage.remove_allowed(db, normalized)
        elif list_name == "ignored":
            n = await authors_storage.remove_ignored(db, normalized)
        else:
            await authors_storage.remove_tentative_review(db, normalized)
            n = 1
    finally:
        await db.close()
    if n > 0 and list_name in ("allowed", "ignored"):
        await state.refresh_filter_authors()
    return SimpleOk(ok=n > 0, detail=None if n > 0 else "not found")


@router.post(
    "/tentative_review/bulk-move", response_model=BulkMoveResponse,
)
async def bulk_move_tentative(body: MoveAuthorRequest) -> BulkMoveResponse:
    """Move every author currently in tentative_review into allowed or ignored.

    Surfaced from the Authors page's Tentative review tab as
    "Ignore All" / "Allow All" one-button actions. Writes are
    done in a single transaction per row (same path as the
    single-item move) so a partial failure leaves consistent state.
    """
    target = body.to
    if target not in ("allowed", "ignored"):
        raise HTTPException(400, "to must be 'allowed' or 'ignored'")

    db = await get_db()
    try:
        rows = await authors_storage.list_tentative_review(db)
        moved = 0
        for row in rows:
            normalized = row["normalized"]
            try:
                if target == "allowed":
                    await authors_storage.promote_tentative_to_allowed(
                        db, normalized,
                    )
                else:
                    await authors_storage.promote_tentative_to_ignored(
                        db, normalized,
                    )
                moved += 1
            except Exception:
                _log.exception(
                    "authors bulk-move: failed on normalized=%r", normalized,
                )
    finally:
        await db.close()

    if moved > 0:
        await state.refresh_filter_authors()
    _log.info("authors bulk-move: tentative_review → %s (%d moved)",
              target, moved)
    return BulkMoveResponse(moved=moved)


@router.post("/{list_name}/{normalized}/move", response_model=SimpleOk)
async def move_author(
    list_name: str, normalized: str, body: MoveAuthorRequest
) -> SimpleOk:
    _validate_list(list_name)
    target = body.to
    if target not in ("allowed", "ignored"):
        raise HTTPException(400, "to must be 'allowed' or 'ignored'")
    if target == list_name:
        raise HTTPException(400, "source and target lists are the same")

    db = await get_db()
    try:
        if list_name == "allowed" and target == "ignored":
            ok = await authors_storage.move_allowed_to_ignored(db, normalized)
        elif list_name == "ignored" and target == "allowed":
            ok = await authors_storage.move_ignored_to_allowed(db, normalized)
        elif list_name == "tentative_review" and target == "allowed":
            await authors_storage.promote_tentative_to_allowed(db, normalized)
            ok = True
        elif list_name == "tentative_review" and target == "ignored":
            await authors_storage.promote_tentative_to_ignored(db, normalized)
            ok = True
        else:
            ok = False
    finally:
        await db.close()
    # Any successful move touches authors_allowed and/or authors_ignored,
    # so the dispatcher's filter_config needs to see the change before
    # the next IRC announce fires.
    if ok:
        await state.refresh_filter_authors()
    return SimpleOk(ok=ok, detail=None if ok else "no-op or not found")
