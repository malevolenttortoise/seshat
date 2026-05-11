"""
Tentative torrent HTTP endpoints.

    GET  /api/v1/tentative            — list pending tentative torrents
    POST /api/v1/tentative/{id}/approve — fetch the .torrent, inject into
                                          the normal pipeline, AND add the
                                          author to the allow list
    POST /api/v1/tentative/{id}/reject  — mark rejected + put the author
                                          on the weekly tentative-review
                                          list (3-tier taxonomy)

Approval is the only path that burns a MAM snatch for a tentative
torrent — up until that moment we've only kept the torrent ID and
whatever metadata the announce (+ future scrapers) gave us.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import state
from app.database import get_db
from app.filter.gate import split_authors
from app.orchestrator.auto_train import train_author
from app.orchestrator.dispatch import inject_grab
from app.storage import authors as authors_storage
from app.storage import tentative as tentative_storage

_log = logging.getLogger("seshat.routers.tentative")

router = APIRouter(prefix="/api/v1/tentative", tags=["tentative"])


class TentativeItem(BaseModel):
    id: int
    mam_torrent_id: str
    torrent_name: str
    author_blob: str
    category: Optional[str]
    language: Optional[str]
    format: Optional[str]
    vip: bool
    scraped_metadata: dict[str, Any]
    cover_path: Optional[str]
    status: str
    created_at: str


class TentativeListResponse(BaseModel):
    items: list[TentativeItem]


class TentativeActionResponse(BaseModel):
    ok: bool
    id: int
    status: str
    grab_id: Optional[int] = None
    error: Optional[str] = None


class BulkRequest(BaseModel):
    """Optional id subset for bulk endpoints. None = every pending row."""
    ids: Optional[list[int]] = None


class BulkResponse(BaseModel):
    processed: int
    failed: int
    errors: list[str] = []


def _to_item(row: tentative_storage.TentativeRow) -> TentativeItem:
    return TentativeItem(
        id=row.id,
        mam_torrent_id=row.mam_torrent_id,
        torrent_name=row.torrent_name,
        author_blob=row.author_blob,
        category=row.category,
        language=row.language,
        format=row.format,
        vip=row.vip,
        scraped_metadata=row.scraped_metadata,
        cover_path=row.cover_path,
        status=row.status,
        created_at=row.created_at,
    )


@router.get("", response_model=TentativeListResponse)
async def list_pending() -> TentativeListResponse:
    db = await get_db()
    try:
        rows = await tentative_storage.list_tentative(db)
        return TentativeListResponse(items=[_to_item(r) for r in rows])
    finally:
        await db.close()


@router.get("/ignored-weekly")
async def ignored_weekly():
    """Weekly ignored-author review: authors grouped with their rejected books."""
    db = await get_db()
    try:
        groups = await tentative_storage.list_ignored_grouped_by_author(db, days=7)
        return {"groups": groups}
    finally:
        await db.close()


# ─── Bulk actions ─────────────────────────────────────────────
# NOTE: declared BEFORE the `/{tentative_id}/...` routes so FastAPI's
# ordered matcher doesn't try to int-parse "bulk" as a tentative_id.
# The path_params collision is silent — moving the wrong one down
# breaks bulk with a 422 like "Input should be a valid integer".


async def _pending_ids(db, subset: Optional[list[int]]) -> list[int]:
    """Return the pending tentative IDs to act on.

    `subset=None` expands to "every pending row." When a subset is
    provided we still filter to pending — rejected/approved rows in
    the subset are silently skipped instead of double-processing.
    """
    rows = await tentative_storage.list_tentative(db)
    pending_ids = [r.id for r in rows]
    if subset is None:
        return pending_ids
    wanted = set(subset)
    return [rid for rid in pending_ids if rid in wanted]


@router.post("/bulk/approve", response_model=BulkResponse)
async def bulk_approve(body: Optional[BulkRequest] = None) -> BulkResponse:
    """Approve many tentative torrents in one call.

    Each approval fetches a .torrent from MAM (same path as the
    single-item approve), so this DOES burn snatches — the caller
    UI is expected to confirm with the user before invoking.

    `body.ids=None` → approve every pending row. Failures on
    individual items don't halt the batch.
    """
    if state.dispatcher is None:
        raise HTTPException(status_code=503, detail="dispatcher not initialized")

    db = await get_db()
    try:
        ids = await _pending_ids(db, body.ids if body else None)
    finally:
        await db.close()

    processed = 0
    failed = 0
    errors: list[str] = []
    for tid in ids:
        try:
            result = await approve(tid)
            if result.ok:
                processed += 1
            else:
                failed += 1
                errors.append(f"tid={tid}: {result.error or 'unknown failure'}")
        except Exception as e:
            failed += 1
            errors.append(f"tid={tid}: {type(e).__name__}: {e}")
            _log.exception(
                "bulk tentative approve: tid=%d crashed (non-fatal)", tid,
            )
    _log.info("bulk tentative approve: processed=%d failed=%d",
              processed, failed)
    return BulkResponse(processed=processed, failed=failed, errors=errors[:20])


@router.post("/bulk/reject", response_model=BulkResponse)
async def bulk_reject(body: Optional[BulkRequest] = None) -> BulkResponse:
    """Reject many tentative torrents in one call.

    Pure local-state change — no MAM traffic. Each rejected item's
    authors land on the weekly tentative_review list, same as the
    single-item reject. `body.ids=None` → reject every pending row.
    """
    db = await get_db()
    try:
        ids = await _pending_ids(db, body.ids if body else None)
    finally:
        await db.close()

    processed = 0
    failed = 0
    errors: list[str] = []
    for tid in ids:
        try:
            result = await reject(tid)
            if result.ok:
                processed += 1
            else:
                failed += 1
                errors.append(f"tid={tid}: {result.error or 'unknown failure'}")
        except Exception as e:
            failed += 1
            errors.append(f"tid={tid}: {type(e).__name__}: {e}")
            _log.exception(
                "bulk tentative reject: tid=%d crashed (non-fatal)", tid,
            )
    _log.info("bulk tentative reject: processed=%d failed=%d",
              processed, failed)
    return BulkResponse(processed=processed, failed=failed, errors=errors[:20])


@router.post("/bulk/dismiss", response_model=BulkResponse)
async def bulk_dismiss(body: Optional[BulkRequest] = None) -> BulkResponse:
    """Dismiss many tentative torrents in one call.

    Pure local-state change — no MAM traffic, no author-list writes.
    `body.ids=None` → dismiss every pending row.
    """
    db = await get_db()
    try:
        ids = await _pending_ids(db, body.ids if body else None)
    finally:
        await db.close()

    processed = 0
    failed = 0
    errors: list[str] = []
    for tid in ids:
        try:
            result = await dismiss(tid)
            if result.ok:
                processed += 1
            else:
                failed += 1
                errors.append(f"tid={tid}: {result.error or 'unknown failure'}")
        except Exception as e:
            failed += 1
            errors.append(f"tid={tid}: {type(e).__name__}: {e}")
            _log.exception(
                "bulk tentative dismiss: tid=%d crashed (non-fatal)", tid,
            )
    _log.info("bulk tentative dismiss: processed=%d failed=%d",
              processed, failed)
    return BulkResponse(processed=processed, failed=failed, errors=errors[:20])


@router.post("/{tentative_id}/approve", response_model=TentativeActionResponse)
async def approve(tentative_id: int) -> TentativeActionResponse:
    if state.dispatcher is None:
        raise HTTPException(status_code=503, detail="dispatcher not initialized")

    db = await get_db()
    try:
        row = await tentative_storage.get_tentative(db, tentative_id)
        if row is None:
            raise HTTPException(status_code=404, detail="tentative not found")
        if row.status != tentative_storage.TENTATIVE_PENDING:
            return TentativeActionResponse(
                ok=False, id=tentative_id, status=row.status,
                error=f"already in status {row.status}",
            )

        # Train every author on the blob to the allow list — this is
        # the whole point of the tentative flow. The user said "yes,
        # I want books by this author even though they weren't on
        # the list before," so we close the loop.
        #
        # Cross-list cleanup: if this author is still sitting on the
        # weekly tentative-review list from a prior reject, remove
        # them from it. Approval is unambiguous: the author has
        # graduated, the review status no longer applies. Without
        # this cleanup the author would simultaneously appear in
        # `authors_allowed` AND `authors_tentative_review`, causing
        # the weekly review UI to keep prompting about a settled
        # author. Latent bug pre-dating the Dismiss feature; folded
        # in here because the same cross-list semantics need to be
        # right for Dismiss to make sense.
        for raw in split_authors(row.author_blob):
            try:
                await train_author(db, raw, source="tentative_approve")
            except Exception:
                _log.exception(
                    "tentative approve: failed to train author %r", raw
                )
            try:
                await authors_storage.remove_tentative_review(db, raw)
            except Exception:
                _log.exception(
                    "tentative approve: failed to clear tentative-review "
                    "entry for %r", raw,
                )
    finally:
        await db.close()

    # Route the actual grab through the dispatcher's inject path so
    # budget + policy + folder creation all run normally. This fetches
    # the .torrent for the first time — we intentionally did NOT
    # store .torrent bytes on tentative insert (user decision #3).
    result = await inject_grab(
        state.dispatcher,
        torrent_id=row.mam_torrent_id,
        torrent_name=row.torrent_name,
        category=row.category or "",
        author_blob=row.author_blob,
        raw_line=f"tentative_approve:id={tentative_id}",
    )

    # Mark the tentative row as approved regardless of the injection
    # outcome. If the injection failed (cookie expired, qBit down),
    # the user can retry via the cookie-retry job or manual re-inject.
    db = await get_db()
    try:
        await tentative_storage.set_tentative_status(
            db, tentative_id, tentative_storage.TENTATIVE_APPROVED
        )
    finally:
        await db.close()

    pipeline_ok = (
        result.action in ("submit", "queue") and result.error is None
    )
    return TentativeActionResponse(
        ok=pipeline_ok,
        id=tentative_id,
        status=tentative_storage.TENTATIVE_APPROVED,
        grab_id=result.grab_id,
        error=result.error,
    )


@router.post("/{tentative_id}/reject", response_model=TentativeActionResponse)
async def reject(tentative_id: int) -> TentativeActionResponse:
    db = await get_db()
    try:
        row = await tentative_storage.get_tentative(db, tentative_id)
        if row is None:
            raise HTTPException(status_code=404, detail="tentative not found")
        if row.status != tentative_storage.TENTATIVE_PENDING:
            return TentativeActionResponse(
                ok=False, id=tentative_id, status=row.status,
                error=f"already in status {row.status}",
            )

        await tentative_storage.set_tentative_status(
            db, tentative_id, tentative_storage.TENTATIVE_REJECTED
        )

        # Put every author from the blob on the tentative-review
        # weekly list — UNLESS the author is already on the allow
        # list. An author who's previously been allowed has already
        # proven worthy of the user's library; rejecting one specific
        # book by them shouldn't drag them back into the review
        # queue. Without this guard the same reject would push them
        # into BOTH `authors_allowed` AND `authors_tentative_review`,
        # producing the same dual-state confusion the approve-path
        # cleanup above prevents.
        for raw in split_authors(row.author_blob):
            try:
                if await authors_storage.is_allowed(db, raw):
                    _log.info(
                        "tentative reject: author %r is already allowed; "
                        "skipping tentative-review add", raw,
                    )
                    continue
                await authors_storage.add_tentative_review(
                    db, raw, source="tentative_reject"
                )
            except Exception:
                _log.exception(
                    "tentative reject: failed to add review author %r", raw
                )

        return TentativeActionResponse(
            ok=True, id=tentative_id,
            status=tentative_storage.TENTATIVE_REJECTED,
        )
    finally:
        await db.close()


@router.post("/{tentative_id}/dismiss", response_model=TentativeActionResponse)
async def dismiss(tentative_id: int) -> TentativeActionResponse:
    """Drop a tentative torrent from the queue without affecting any
    author list.

    Use case: an uploader posts the same book in MP3 + M4B, the user
    wants only one format — Dismiss the unwanted one, Approve the
    other, and the author still gets onto the allow list cleanly.
    Or: the specific book on offer doesn't interest the user but the
    author's other work might, so they want to leave the author
    genuinely undecided for the next tentative torrent that arrives.

    Distinct from Reject (which pushes the author onto the weekly
    tentative-review list) and from Approve (which trains the author
    to the allow list). Touches the tentative row only.
    """
    db = await get_db()
    try:
        row = await tentative_storage.get_tentative(db, tentative_id)
        if row is None:
            raise HTTPException(status_code=404, detail="tentative not found")
        if row.status != tentative_storage.TENTATIVE_PENDING:
            return TentativeActionResponse(
                ok=False, id=tentative_id, status=row.status,
                error=f"already in status {row.status}",
            )

        await tentative_storage.set_tentative_status(
            db, tentative_id, tentative_storage.TENTATIVE_DISMISSED
        )
        _log.info(
            "tentative dismiss: id=%d torrent_id=%s (no author-list changes)",
            tentative_id, row.mam_torrent_id,
        )
        return TentativeActionResponse(
            ok=True, id=tentative_id,
            status=tentative_storage.TENTATIVE_DISMISSED,
        )
    finally:
        await db.close()


