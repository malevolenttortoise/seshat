"""
Manual grab-injection HTTP endpoint.

POST /api/v1/grabs/inject

Two callers hit this endpoint:

  1. **Cookie-rotation manual test recipe** — paste a torrent ID,
     verify the full grab path works, then rotate the cookie and
     repeat to verify the failure + retry flow.
  2. **Operator manual override** — when an announce is missed
     (Seshat was offline) and the operator wants to grab it
     anyway from the MAM web UI's "Recent Activity" page.

The endpoint reads the dispatcher singleton out of `app.state`,
calls `inject_grab`, and serializes the result as JSON. Session
auth (auth_secret cookie) is enforced by the global middleware.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import state
from app.mam.bonus_buy import buy_personal_freeleech, personal_fl_worth_buying
from app.mam.torrent_info import invalidate_cache as invalidate_torrent_info
from app.orchestrator.dispatch import inject_grab

_log = logging.getLogger("seshat.routers.inject")

router = APIRouter(prefix="/api/v1/grabs", tags=["grabs"])


class InjectRequest(BaseModel):
    """Request body for POST /api/v1/grabs/inject.

    Only `torrent_id` is required. The metadata fields exist for
    audit-log readability (so the UI shows a name + author instead
    of just an ID), but the dispatcher doesn't need them to operate.

    `use_wedge_override` and `buy_personal_fl` are two mutually
    independent per-grab checkboxes on the manual-inject dialog:

      - `use_wedge_override=True` forces `&fl=1` on the download
        URL for this one grab, draining one wedge from the user's
        pool. Overrides the global `policy_use_wedge` setting on a
        per-grab basis.
      - `buy_personal_fl=True` spends ONE FL WEDGE via `bonusBuy.php?
        spendtype=personalFL` BEFORE the inject. MAM then flags the
        torrent as personal freeleech on the user's account, and
        the existing grab path picks up `torrent_free=True` via
        torrent_info — no `&fl=1` override needed.

    Both cost a wedge. Setting both never spends two: a successful FL
    buy makes the grab read as free, so the dispatcher's wedge guard
    leaves `&fl=1` off. Neither is spent on a torrent that's already
    free or whose status MAM can't confirm.
    """

    torrent_id: str = Field(..., min_length=1)
    torrent_name: str = ""
    category: str = ""
    author_blob: str = ""
    filetype: str = ""
    source: str = "manual_inject"
    use_wedge_override: bool = False
    buy_personal_fl: bool = False
    # v2.9.0 — bypass the format-priority dedup gate for this one
    # grab. The manual-inject UI surfaces this as a "Snatch anyway"
    # checkbox the user can flip if they explicitly want a duplicate
    # format of an already-in-flight or already-owned book.
    override_format_dedup: bool = False
    # Phase 0 snatch safety — let this grab through when MAM says the
    # account already snatched the torrent (`my_snatched`). Never
    # overrides `already_grabbed` (a torrent Seshat itself fetched).
    override_mam_snatched: bool = False


class InjectResponse(BaseModel):
    """Response body for POST /api/v1/grabs/inject.

    Mirrors `DispatchResult` plus a top-level `ok` boolean for
    machine consumers that just want a thumbs-up. The full result
    fields are included so the UI can render the audit row link
    or the queue position immediately.
    """

    ok: bool
    action: str
    reason: str
    announce_id: int
    grab_id: Optional[int] = None
    qbit_hash: Optional[str] = None
    error: Optional[str] = None


@router.get("/recent")
async def recent_grabs():
    """Last 5 grabs for the dashboard mini-display."""
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            SELECT torrent_name, author_blob, grabbed_at
            FROM grabs
            ORDER BY grabbed_at DESC
            LIMIT 5
            """
        )
        rows = await cursor.fetchall()
        return {
            "grabs": [
                {
                    "torrent_name": str(r["torrent_name"] or ""),
                    "author_blob": str(r["author_blob"] or ""),
                    "grabbed_at": str(r["grabbed_at"] or ""),
                }
                for r in rows
            ]
        }
    finally:
        await db.close()


@router.get("/budget")
async def snatch_budget():
    """Snatch budget overview for the dashboard.

    Returns active ledger count, qbit extras, budget cap, queue size,
    seed_seconds_required, and ALL under-threshold entries (both
    Seshat-submitted and manual/external) with seedtimes so the UI
    can show a countdown to the true next release.
    """
    from app.database import get_db
    from app.rate_limit import ledger as ledger_mod, queue as queue_mod

    if state.dispatcher is None:
        raise HTTPException(503, "dispatcher not initialized")

    deps = state.dispatcher
    db = await get_db()
    try:
        active_rows = await ledger_mod.list_active(db)
        queue_size = await queue_mod.size(db)
        known_hashes = {row.qbit_hash for row in active_rows}

        # Enrich ledger entries with torrent names from grabs table.
        entries = []
        for row in active_rows:
            cursor = await db.execute(
                "SELECT torrent_name, author_blob FROM grabs WHERE id = ?",
                (row.grab_id,),
            )
            grab = await cursor.fetchone()
            remaining = max(0, deps.seed_seconds_required - row.seeding_seconds)
            entries.append({
                "grab_id": row.grab_id,
                "torrent_name": str(grab["torrent_name"]) if grab else "?",
                "author_blob": str(grab["author_blob"] or "") if grab else "",
                "seeding_seconds": row.seeding_seconds,
                "remaining_seconds": remaining,
                "source": "seshat",
            })

        # Include qBit extras (manual/Autobrr adds) under the seedtime
        # threshold so the widget shows the true full budget picture.
        extras_count = 0
        try:
            qbit_torrents = await deps.qbit.list_torrents(category=deps.qbit_category)
            for t in qbit_torrents:
                if t.hash and t.hash not in known_hashes:
                    if t.seeding_seconds < deps.seed_seconds_required:
                        extras_count += 1
                        remaining = max(0, deps.seed_seconds_required - t.seeding_seconds)
                        entries.append({
                            "grab_id": None,
                            "torrent_name": t.name,
                            "author_blob": "",
                            "seeding_seconds": t.seeding_seconds,
                            "remaining_seconds": remaining,
                            "source": "external",
                        })
        except Exception:
            # If qBit is unreachable, fall back to the cached count.
            extras_count = int(state._snatch_budget.get("qbit_extras", 0) or 0)

        # Sort by remaining time ascending (closest to release first).
        entries.sort(key=lambda e: e["remaining_seconds"])

        budget_used = len(active_rows) + max(0, extras_count)
        next_release = entries[0]["remaining_seconds"] if entries else None

        return {
            "budget_used": budget_used,
            "budget_cap": deps.budget_cap,
            "ledger_active": len(active_rows),
            "qbit_extras": extras_count,
            "queue_size": queue_size,
            "seed_seconds_required": deps.seed_seconds_required,
            "next_release_seconds": next_release,
            "entries": entries,
        }
    finally:
        await db.close()


async def _buy_personal_fl_for_inject(torrent_id: str, token: str) -> bool:
    """Spend one FL wedge (the site's "Buy as FL") to flag this torrent
    as personal freeleech. Not bought on a torrent that's already free or
    whose status MAM can't confirm (`personal_fl_worth_buying`): MAM
    would spend the wedge regardless.

    Returns True when MAM confirmed the buy. The caller passes that to
    `inject_grab(personal_fl_bought=...)`: MAM's search API takes 5-20
    min to report the new personal FL, so re-reading it can't be trusted.

    Called before `inject_grab` when the user checked the "buy
    personal FL" box on the manual-inject dialog. Failure is audited
    but NOT raised — the inject still proceeds with whatever
    freeleech state the torrent already had. Rationale: the user
    confirmed the grab regardless of the FL buy, so a transient MAM
    rejection shouldn't cost them the snatch.

    On success, the torrent-info cache is invalidated so later lookups
    refetch; the grab itself is marked free through the return value,
    since the refetch reads `personal_freeleech=False` for 5-20 min.
    """
    # Deferred imports keep the router's top-level import graph
    # light — the economy_audit + database modules haul in aiosqlite
    # and we'd rather not pay that cost at module load when the
    # feature isn't being used.
    from app.database import get_db
    from app.storage import economy_audit

    if not torrent_id or not token:
        return False

    worth, why = await personal_fl_worth_buying(torrent_id, token)
    if not worth:
        _log.info("personal FL not bought on tid=%s: %s", torrent_id, why)
        return False

    result = await buy_personal_freeleech(torrent_id, token=token)
    db = await get_db()
    try:
        await economy_audit.record(
            db,
            action=economy_audit.ACTION_PERSONAL_FL,
            trigger=economy_audit.TRIGGER_USER_GRAB,
            outcome=(
                economy_audit.OUTCOME_SUCCESS
                if result.success
                else economy_audit.OUTCOME_FAILURE
            ),
            torrent_id=torrent_id,
            message=f"1 FL wedge (Buy as FL): {result.message}",
            user_bonus_after=result.new_seedbonus,
        )
    finally:
        await db.close()

    if result.success:
        invalidate_torrent_info()
    return bool(result.success)


@router.post("/inject", response_model=InjectResponse)
async def inject_endpoint(request: InjectRequest) -> InjectResponse:
    if state.dispatcher is None:
        # Hit during startup before lifespan completed, or during
        # tests that forgot to install a dispatcher fixture. Return
        # a 503 rather than a 500 so the client knows it can retry.
        raise HTTPException(
            status_code=503,
            detail="dispatcher not initialized yet",
        )

    # F4 path: buy personal-FL for this torrent BEFORE the inject.
    # On buy failure the caller probably still wants the grab to
    # proceed normally (the checkbox is optional), so we audit the
    # failure and fall through rather than aborting.
    fl_bought = False
    if request.buy_personal_fl:
        fl_bought = await _buy_personal_fl_for_inject(
            request.torrent_id, state.dispatcher.live_mam_token() or ""
        )

    result = await inject_grab(
        state.dispatcher,
        torrent_id=request.torrent_id,
        torrent_name=request.torrent_name,
        category=request.category,
        author_blob=request.author_blob,
        filetype=request.filetype,
        raw_line=f"manual_inject:source={request.source}",
        force_fl_wedge=request.use_wedge_override,
        apply_format_dedup=not request.override_format_dedup,
        override_mam_snatched=request.override_mam_snatched,
        personal_fl_bought=fl_bought,
    )

    # ok=True means the grab successfully entered the pipeline
    # (submit or queue) with no error. A drop is not an error per
    # se — it's a valid outcome — but the client probably wants
    # ok=False so its UI can surface "this didn't go anywhere."
    # Same for fetch / qBit failures: action might still be
    # "submit" or "queue" (the rate decision), but the grab is in
    # a failed state and `error` is set, so ok must be False.
    pipeline_ok = (
        result.action in ("submit", "queue") and result.error is None
    )
    return InjectResponse(
        ok=pipeline_ok,
        action=result.action,
        reason=result.reason,
        announce_id=result.announce_id,
        grab_id=result.grab_id,
        qbit_hash=result.qbit_hash,
        error=result.error,
    )
