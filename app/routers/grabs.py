"""
External grabs endpoint.

    POST /api/v1/grabs/inject-batch

Accepts a batch of MAM torrent URLs (or bare IDs) from any external
caller (browser bookmarklet, user script, cron job, curl) and routes
each one through `inject_grab`, which handles the full
filter-skip → fetch → qBit pipeline.

Authors from the request are optionally auto-trained to the allow
list if they're not already present. This covers the case where
the caller knows the author but Seshat hasn't seen it in an IRC
announce yet.

Session auth (auth_secret cookie) is enforced by the global
middleware — no separate API key.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import state
from app.database import get_db
from app.mam.torrent_id import extract_torrent_id
from app.orchestrator.auto_train import train_author
from app.orchestrator.dispatch import inject_grab

_log = logging.getLogger("seshat.routers.grabs")

router = APIRouter(prefix="/api/v1/grabs", tags=["grabs"])

class GrabItem(BaseModel):
    url_or_id: str
    author: Optional[str] = None
    # Book title as the caller knows it. Passed to `inject_grab` as
    # `torrent_name` so the grab row, dashboard, review queue label,
    # and enricher fuzzy-search all use the real title instead of the
    # `manual_inject_<id>` placeholder. Optional — when omitted the
    # placeholder still lands on the row.
    title: Optional[str] = None
    # MAM category (e.g. "Ebooks - Fantasy"). Lands on the grab row's
    # `category` field so the dashboard filter, budget-watcher category
    # reconciliation, and any cross-ref against the IRC announce
    # category gate all have the right value. Optional.
    category: Optional[str] = None
    # Optional pre-fetched metadata bundle. When present, Seshat stores
    # the dict on the grab row and skips its own enricher chain in
    # _prepare_book — saves 6 outbound scraper requests per book.
    #
    # Expected keys (all optional):
    #   goodreads_url, hardcover_url, kobo_url, amazon_url,
    #   isbn, cover_url, page_count, description,
    #   series_name, series_index
    # Unknown keys are stored as-is; the pipeline reads only the
    # keys it recognizes.
    metadata: Optional[dict[str, Any]] = None
    # Optional MAM filetype hint (epub / azw3 / m4b / mp3 / ...). When
    # the caller has it (Discovery's send-to-pipeline does — it pulls
    # the format from the MAM search row), passing it lets the
    # v2.9.0 format-dedup gate make the right call instead of
    # falling through on "no filetype" with a warning.
    filetype: Optional[str] = None


class InjectBatchRequest(BaseModel):
    items: list[GrabItem] = Field(..., min_length=1, max_length=100)
    # v2.9.0 — bypass the format-priority dedup gate for every item
    # in this batch. Discovery's send-to-pipeline UI surfaces this
    # as a "Snatch anyway" checkbox at the batch level (the typical
    # "I know I already own this format but want it again" path).
    override_format_dedup: bool = False
    # Phase 0 snatch safety — let this grab through when MAM says the
    # account already snatched the torrent (`my_snatched`). Never
    # overrides `already_grabbed` (a torrent Seshat itself fetched).
    override_mam_snatched: bool = False


class GrabResultItem(BaseModel):
    torrent_id: str
    ok: bool
    action: Optional[str] = None
    error: Optional[str] = None


class InjectBatchResponse(BaseModel):
    submitted: int
    failed: int
    results: list[GrabResultItem]


@router.post("/inject-batch", response_model=InjectBatchResponse)
async def inject_batch(body: InjectBatchRequest) -> InjectBatchResponse:
    if state.dispatcher is None:
        raise HTTPException(503, "dispatcher not initialized")

    results: list[GrabResultItem] = []
    submitted = 0
    failed = 0

    for item in body.items:
        tid = extract_torrent_id(item.url_or_id)
        if tid is None:
            results.append(
                GrabResultItem(
                    torrent_id=item.url_or_id,
                    ok=False,
                    error=f"could not parse torrent ID from: {item.url_or_id}",
                )
            )
            failed += 1
            continue

        # Auto-train the author if provided and not already known.
        if item.author:
            db = await get_db()
            try:
                await train_author(db, item.author, source="external_grab")
            except Exception:
                pass
            finally:
                await db.close()

        try:
            result = await inject_grab(
                state.dispatcher,
                torrent_id=tid,
                torrent_name=(item.title or "").strip(),
                category=(item.category or "").strip(),
                author_blob=item.author or "",
                filetype=(item.filetype or "").strip(),
                raw_line=f"external:{item.url_or_id}",
                apply_format_dedup=not body.override_format_dedup,
                override_mam_snatched=body.override_mam_snatched,
            )
            ok = result.action in ("submit", "queue") and result.error is None

            # Persist the metadata bundle on the grab row so
            # _prepare_book can use it to skip the enricher later.
            # Best-effort: a JSON serialization or DB failure here must
            # not flip the grab's success — the torrent was accepted;
            # we just lose the short-circuit optimization.
            if ok and item.metadata and result.grab_id:
                try:
                    db = await get_db()
                    try:
                        await db.execute(
                            "UPDATE grabs SET source_metadata = ? WHERE id = ?",
                            (json.dumps(item.metadata), result.grab_id),
                        )
                        await db.commit()
                    finally:
                        await db.close()
                except Exception:
                    _log.warning(
                        "inject-batch: failed to persist metadata for grab_id=%s (non-fatal)",
                        result.grab_id, exc_info=True,
                    )

            results.append(
                GrabResultItem(
                    torrent_id=tid,
                    ok=ok,
                    action=result.action,
                    error=result.error,
                )
            )
            if ok:
                submitted += 1
            else:
                failed += 1
        except Exception as e:
            results.append(
                GrabResultItem(
                    torrent_id=tid,
                    ok=False,
                    error=str(e),
                )
            )
            failed += 1

    _log.info(
        "inject-batch: %d submitted, %d failed out of %d",
        submitted, failed, len(body.items),
    )
    return InjectBatchResponse(
        submitted=submitted, failed=failed, results=results
    )
