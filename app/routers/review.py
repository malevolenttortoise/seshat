"""
Book review queue HTTP endpoints.

    GET    /api/v1/review              — list pending reviews
    GET    /api/v1/review/{id}         — fetch one pending review
    POST   /api/v1/review/{id}/approve — approve (+ optional metadata edits)
    POST   /api/v1/review/{id}/reject  — reject + delete staged file

Approval triggers sink delivery via `deliver_reviewed`. Rejection
removes the staged file from disk (seeding original is untouched)
and marks the queue row rejected.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import state
from app.database import get_db
from app.mam.cookie import get_current_token as _get_mam_token
from app.orchestrator.pipeline import deliver_reviewed
from app.storage import grabs as grabs_storage
from app.storage import review_queue as review_storage

_log = logging.getLogger("seshat.routers.review")

router = APIRouter(prefix="/api/v1/review", tags=["review"])


class ReviewItem(BaseModel):
    id: int
    grab_id: int
    staged_path: str
    book_filename: str
    book_format: Optional[str]
    metadata: dict[str, Any]
    cover_path: Optional[str]
    status: str
    created_at: str
    decided_at: Optional[str]
    decision_note: Optional[str]


class ReviewListResponse(BaseModel):
    items: list[ReviewItem]
    pending_count: int


class ApproveRequest(BaseModel):
    metadata: Optional[dict[str, Any]] = None
    note: Optional[str] = None


class SaveRequest(BaseModel):
    """Metadata-only edit without approving or delivering.

    Lets the user fix a mistitled/misauthored review row (common
    symptom: a pre-v1.1.4 AthenaScout send left a
    `manual_inject_<id>` placeholder on the grab row that the
    enricher then chased against garbage) and re-run enrichment
    in a separate step.
    """
    metadata: dict[str, Any]


class RejectRequest(BaseModel):
    note: Optional[str] = None


class ReviewActionResponse(BaseModel):
    ok: bool
    id: int
    status: str
    error: Optional[str] = None


def _to_item(row: review_storage.ReviewRow) -> ReviewItem:
    return ReviewItem(
        id=row.id,
        grab_id=row.grab_id,
        staged_path=row.staged_path,
        book_filename=row.book_filename,
        book_format=row.book_format,
        metadata=row.metadata,
        cover_path=row.cover_path,
        status=row.status,
        created_at=row.created_at,
        decided_at=row.decided_at,
        decision_note=row.decision_note,
    )


@router.get("", response_model=ReviewListResponse)
async def list_pending() -> ReviewListResponse:
    db = await get_db()
    try:
        rows = await review_storage.list_pending(db, limit=500)
        count = await review_storage.count_by_status(
            db, review_storage.STATUS_PENDING
        )
        return ReviewListResponse(
            items=[_to_item(r) for r in rows], pending_count=count
        )
    finally:
        await db.close()


@router.get("/{review_id}", response_model=ReviewItem)
async def get_one(review_id: int) -> ReviewItem:
    db = await get_db()
    try:
        row = await review_storage.get_entry(db, review_id)
        if row is None:
            raise HTTPException(status_code=404, detail="review not found")
        return _to_item(row)
    finally:
        await db.close()


@router.post("/{review_id}/approve", response_model=ReviewActionResponse)
async def approve(review_id: int, body: ApproveRequest) -> ReviewActionResponse:
    if state.dispatcher is None:
        raise HTTPException(status_code=503, detail="dispatcher not initialized")
    deps = state.dispatcher
    db = await get_db()
    try:
        row = await review_storage.get_entry(db, review_id)
        if row is None:
            raise HTTPException(status_code=404, detail="review not found")
        if row.status != review_storage.STATUS_PENDING:
            return ReviewActionResponse(
                ok=False, id=review_id, status=row.status,
                error=f"already in status {row.status}",
            )

        # Persist any user metadata edits before sink delivery.
        if body.metadata:
            merged, new_title = await _merge_metadata(row.metadata, body.metadata)
            await review_storage.set_status(
                db, review_id, review_storage.STATUS_PENDING,
                metadata=merged,
            )
            if new_title:
                await grabs_storage.set_torrent_name(
                    db, row.grab_id, new_title,
                )

        ok = await deliver_reviewed(
            db,
            review_id=review_id,
            default_sink=deps.default_sink,
            calibre_library_path=deps.calibre_library_path,
            folder_sink_path=deps.folder_sink_path,
            audiobookshelf_library_path=deps.audiobookshelf_library_path,
            cwa_ingest_path=deps.cwa_ingest_path,
            ntfy_url=deps.ntfy_url,
            ntfy_topic=deps.ntfy_topic,
            auto_train_enabled=deps.auto_train_enabled,
            was_timeout=False,
            per_event_notifications=deps.per_event_notifications,
        )
        refreshed = await review_storage.get_entry(db, review_id)
        return ReviewActionResponse(
            ok=ok,
            id=review_id,
            status=refreshed.status if refreshed else "unknown",
            error=None if ok else "sink delivery failed",
        )
    finally:
        await db.close()


async def _merge_metadata(
    existing: dict[str, Any], edits: dict[str, Any]
) -> tuple[dict[str, Any], Optional[str]]:
    """Apply user-facing edits onto the stored metadata dict.

    Returns (merged_dict, new_title_for_grab_row). The second element
    is set when the `title` field was edited, so callers know to
    propagate the change to `grabs.torrent_name` (which drives the
    Snatch Budget widget + Recent Activity label).
    """
    merged = dict(existing)
    merged.update(edits)
    new_title = edits.get("title") if "title" in edits else None
    if new_title is not None:
        new_title = str(new_title).strip() or None
    return merged, new_title


@router.post("/{review_id}/save", response_model=ReviewItem)
async def save_edits(review_id: int, body: SaveRequest) -> ReviewItem:
    """Persist metadata edits on a pending review row.

    The approve endpoint also accepts edits, but only as part of the
    final "Save & Approve" action. This separate save lets the user
    fix bad metadata, click "Re-enrich" to rerun the scraper chain
    against the corrected title/author, and only then approve — a
    workflow the v1.1.4 `manual_inject_<id>` bug made necessary.
    """
    db = await get_db()
    try:
        row = await review_storage.get_entry(db, review_id)
        if row is None:
            raise HTTPException(status_code=404, detail="review not found")
        if row.status != review_storage.STATUS_PENDING:
            raise HTTPException(
                status_code=409,
                detail=f"cannot edit a review in status {row.status!r}",
            )

        merged, new_title = await _merge_metadata(row.metadata, body.metadata)
        await review_storage.set_status(
            db, review_id, review_storage.STATUS_PENDING, metadata=merged,
        )
        if new_title:
            await grabs_storage.set_torrent_name(
                db, row.grab_id, new_title,
            )
        refreshed = await review_storage.get_entry(db, review_id)
        assert refreshed is not None
        _log.info(
            "review edit saved: review_id=%d grab_id=%d (title=%r)",
            review_id, row.grab_id, merged.get("title"),
        )
        return _to_item(refreshed)
    finally:
        await db.close()


@router.post("/{review_id}/re-enrich", response_model=ReviewItem)
async def re_enrich(review_id: int, body: SaveRequest) -> ReviewItem:
    """Re-run the metadata enricher against the row's current title+author.

    Any `body.metadata` edits are applied first so the user can fix
    the title in the same request that rebuilds enrichment. The
    enricher result replaces `metadata.enriched`, and if it returns
    a better title than what's on the grab row, the grab name is
    updated too.
    """
    if state.dispatcher is None:
        raise HTTPException(status_code=503, detail="dispatcher not initialized")
    enricher = getattr(state.dispatcher, "metadata_enricher", None)
    if enricher is None or not getattr(enricher.config, "enabled", False):
        raise HTTPException(
            status_code=409,
            detail="metadata enrichment is disabled; enable it in Settings first",
        )

    db = await get_db()
    try:
        row = await review_storage.get_entry(db, review_id)
        if row is None:
            raise HTTPException(status_code=404, detail="review not found")
        if row.status != review_storage.STATUS_PENDING:
            raise HTTPException(
                status_code=409,
                detail=f"cannot re-enrich a review in status {row.status!r}",
            )

        # Apply pending edits so the enricher sees the user's correction.
        merged, _ = await _merge_metadata(row.metadata, body.metadata or {})

        # Pull the authoritative title/author from the merged metadata —
        # enriched-field fallback covers the case where the user hasn't
        # edited the title directly but enrichment previously found it.
        enriched_prior = merged.get("enriched") or {}
        title = (merged.get("title")
                 or enriched_prior.get("title")
                 or "").strip()
        author = (merged.get("author")
                  or ", ".join(enriched_prior.get("authors") or [])
                  or "").strip()
        if not title:
            raise HTTPException(
                status_code=400,
                detail="no title available — set one before re-enriching",
            )

        grab = await grabs_storage.get_grab(db, row.grab_id)
        mam_torrent_id = grab.mam_torrent_id if grab else ""

        # Route audiobook re-enrichment through the audiobook priority
        # list (Audible + Audnexus first) so narrator / duration / ASIN
        # come from the audiobook-aware sources. Without this flag the
        # enricher uses the ebook priority and Goodreads tends to
        # short-circuit at confidence 1.00 before Audible gets a chance.
        from app.orchestrator.pipeline import _is_audiobook_grab
        grab_category = grab.category if grab else ""
        is_audiobook = _is_audiobook_grab(
            row.book_format or "", grab_category,
        )

        result = await enricher.enrich(
            title=title,
            author=author,
            mam_torrent_id=mam_torrent_id,
            mam_token=_get_mam_token(),
            audiobook=is_audiobook,
        )

        if result is None:
            # Still persist the user's edits; just tell them enrichment
            # came back empty so they can adjust + retry.
            await review_storage.set_status(
                db, review_id, review_storage.STATUS_PENDING, metadata=merged,
            )
            raise HTTPException(
                status_code=404,
                detail="enricher returned no match for the edited title/author",
            )

        merged["enriched"] = result.to_dict()

        # Download the fresh cover into the review staging dir so the
        # UI can show it without re-running the whole staging pass.
        # Without this the enriched metadata carries `cover_url` but
        # the UI has no local file to serve, so the placeholder glyph
        # shows despite a successful Audible match. Mirrors the
        # initial `_stage_for_review` cover-fetch step.
        if result.cover_url:
            try:
                from app.metadata.covers import fetch_cover
                target_dir = Path(row.staged_path)
                target_dir.mkdir(parents=True, exist_ok=True)
                cover_path = await fetch_cover(
                    result.cover_url,
                    dest_dir=target_dir,
                    basename="cover-enriched",
                )
                if cover_path is not None:
                    merged["cover_enriched"] = str(cover_path)
            except Exception:
                _log.exception(
                    "review re-enrich: cover fetch crashed "
                    "for review_id=%d (non-fatal)", review_id,
                )

        await review_storage.set_status(
            db, review_id, review_storage.STATUS_PENDING, metadata=merged,
        )

        # Propagate a better title to the grab row if the user hasn't
        # set one explicitly and the enricher found one.
        new_title = str(merged.get("title") or result.title or "").strip()
        if new_title and grab and new_title != grab.torrent_name:
            await grabs_storage.set_torrent_name(db, row.grab_id, new_title)

        refreshed = await review_storage.get_entry(db, review_id)
        assert refreshed is not None
        _log.info(
            "review re-enriched: review_id=%d grab_id=%d title=%r confidence=%.2f",
            review_id, row.grab_id, result.title or title,
            getattr(result, "confidence", 0.0),
        )
        return _to_item(refreshed)
    finally:
        await db.close()


@router.post("/{review_id}/reject", response_model=ReviewActionResponse)
async def reject(review_id: int, body: RejectRequest) -> ReviewActionResponse:
    db = await get_db()
    try:
        row = await review_storage.get_entry(db, review_id)
        if row is None:
            raise HTTPException(status_code=404, detail="review not found")
        if row.status != review_storage.STATUS_PENDING:
            return ReviewActionResponse(
                ok=False, id=review_id, status=row.status,
                error=f"already in status {row.status}",
            )

        # Remove the staged file + its enclosing grab-<id> dir. The
        # seeding original in the download directory is untouched.
        try:
            staged_dir = Path(row.staged_path)
            if staged_dir.exists():
                shutil.rmtree(str(staged_dir), ignore_errors=True)
        except Exception:
            _log.exception(
                "review reject: failed to remove staged dir for review_id=%d",
                review_id,
            )

        await review_storage.set_status(
            db, review_id, review_storage.STATUS_REJECTED,
            decision_note=body.note or "user rejected",
        )
        return ReviewActionResponse(
            ok=True, id=review_id, status=review_storage.STATUS_REJECTED,
        )
    finally:
        await db.close()
