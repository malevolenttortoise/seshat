"""
MAM economy HTTP endpoints.

Mounts at `/api/v1/mam/economy`. Three role groups:

  1. **Config** (`GET`/`PUT /config`) — returns and updates the flat
     `mam_economy_*` keys in settings.json so the MamPage can render
     the auto-buy UI without hardcoding any of the key names.

  2. **Manual buys** (`POST /vip/buy`, `POST /upload/buy`,
     `POST /personal-fl/buy`) — trigger a bonusBuy.php call directly
     and audit it with `trigger='manual'`. Manual buys BYPASS the
     enable flag (so the user can test integration before flipping
     it on) but HONOR the shared-timestamp lockout (so a click
     can't cause a double-buy on the next scheduler tick within the
     same interval window).

  3. **Audit + preflight** (`GET /audit`, `POST /preflight`) — read
     the audit history for the MamPage tile and answer "would the
     buffer gate allow this torrent right now?" for the
     BufferInsufficientBanner on the inject dialog.

Auth: the global session-cookie middleware already gates every
`/api/v1/*` path — no additional auth logic lives here.
"""
from __future__ import annotations

import logging
import time
from typing import Literal, Optional, Union

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import load_settings, save_settings
from app.database import get_db
from app.mam.bonus_buy import (
    BP_PER_PERSONAL_FL,
    BP_PER_UPLOAD_GB,
    MIN_UPLOAD_GB,
    BuyResult,
    buy_personal_freeleech,
    buy_upload_credit,
    buy_vip,
)
from app.mam.economy import max_affordable_upload_gb
from app.mam.torrent_info import (
    TorrentInfoError,
    get_torrent_info,
    invalidate_cache as invalidate_torrent_info,
)
from app.mam.user_status import (
    UserStatusError,
    get_user_status,
    invalidate_cache as invalidate_user_status,
)
from app.storage import economy_audit

_log = logging.getLogger("seshat.routers.economy")

router = APIRouter(prefix="/api/v1/mam/economy", tags=["mam", "economy"])


# ─── Config ─────────────────────────────────────────────────


# Keys the frontend reads/writes. Keeping the list explicit (rather
# than prefix-matching `mam_economy_*`) makes the contract obvious on
# both sides and prevents a rogue `mam_economy_last_*_buy_at` update
# from sneaking through the PUT path.
_CONFIG_KEYS = (
    "mam_economy_vip_enabled",
    "mam_economy_vip_interval_hours",
    "mam_economy_vip_min_bonus",
    "mam_economy_vip_weeks",
    "mam_economy_upload_enabled",
    "mam_economy_upload_interval_hours",
    "mam_economy_upload_ratio_trigger",
    "mam_economy_upload_ratio_floor",
    "mam_economy_upload_ratio_chunk_gb",
    "mam_economy_upload_buffer_trigger",
    "mam_economy_upload_buffer_floor_gb",
    "mam_economy_upload_buffer_chunk_gb",
    "mam_economy_upload_bonus_trigger",
    "mam_economy_upload_bonus_ceiling",
    "mam_economy_buffer_gate_enabled",
    "mam_economy_buffer_gate_safety_margin_gb",
    "mam_economy_manual_wedge_offer_enabled",
    "mam_economy_fl_wedge_offer_enabled",
    "mam_economy_intro_dismissed",
    "mam_economy_dry_run",
)

# Read-only surface — timestamps the user can't edit directly.
_CONFIG_READONLY_KEYS = (
    "mam_economy_last_vip_buy_at",
    "mam_economy_last_upload_buy_at",
)


@router.get("/config")
async def get_config() -> dict:
    s = load_settings()
    return {k: s.get(k) for k in (_CONFIG_KEYS + _CONFIG_READONLY_KEYS)}


@router.put("/config")
async def put_config(updates: dict) -> dict:
    """Merge a partial config dict into settings.json.

    Silently drops any key not in `_CONFIG_KEYS` — the user can't
    corrupt timestamps or unrelated settings through this endpoint.

    Chunk-GB values are additionally range-checked: MAM rejects
    programmatic upload-credit buys under `MIN_UPLOAD_GB`, so a
    sub-50 chunk would just produce a silent no_trigger skip every
    interval. Reject at write time with a clear message instead.
    """
    s = dict(load_settings())
    allowed = {k: v for k, v in updates.items() if k in _CONFIG_KEYS}
    if not allowed:
        raise HTTPException(400, "No recognized economy keys in request body")
    for chunk_key in (
        "mam_economy_upload_ratio_chunk_gb",
        "mam_economy_upload_buffer_chunk_gb",
    ):
        if chunk_key in allowed:
            try:
                chunk_gb = float(allowed[chunk_key])
            except (TypeError, ValueError):
                raise HTTPException(400, f"{chunk_key} must be a number")
            if chunk_gb < MIN_UPLOAD_GB:
                raise HTTPException(
                    400,
                    f"{chunk_key} must be >= {MIN_UPLOAD_GB} — MAM rejects "
                    f"programmatic upload buys below that floor (got {chunk_gb})",
                )
    s.update(allowed)
    save_settings(s)
    return {k: s.get(k) for k in (_CONFIG_KEYS + _CONFIG_READONLY_KEYS)}


# ─── Manual buys ────────────────────────────────────────────


class VipBuyRequest(BaseModel):
    weeks: Union[int, Literal["max"]] = 4


class UploadBuyRequest(BaseModel):
    # Either `gb` (explicit amount) or `mode="max_affordable"`.
    gb: Optional[float] = None
    mode: Optional[Literal["max_affordable"]] = None


class PersonalFlBuyRequest(BaseModel):
    torrent_id: str = Field(..., min_length=1)


class BuyResponse(BaseModel):
    ok: bool
    message: str
    new_seedbonus: Optional[float] = None
    cost_points: Optional[float] = None
    amount: Optional[str] = None


@router.post("/vip/buy", response_model=BuyResponse)
async def vip_buy(body: VipBuyRequest) -> BuyResponse:
    token = _require_token()
    prev_seedbonus = await _fetch_prev_seedbonus(token)
    result = await buy_vip(body.weeks, token=token)
    return await _persist_manual_buy_result(
        result,
        action=economy_audit.ACTION_VIP,
        tier="trigger:manual",
        amount=str(body.weeks),
        prev_seedbonus=prev_seedbonus,
        timestamp_key="mam_economy_last_vip_buy_at",
    )


@router.post("/upload/buy", response_model=BuyResponse)
async def upload_buy(body: UploadBuyRequest) -> BuyResponse:
    token = _require_token()
    prev_seedbonus = await _fetch_prev_seedbonus(token)
    if body.mode == "max_affordable":
        gb = max_affordable_upload_gb(prev_seedbonus)
        if gb <= 0:
            min_bp = MIN_UPLOAD_GB * BP_PER_UPLOAD_GB
            raise HTTPException(
                400,
                f"Not enough seedbonus for MAM's minimum {MIN_UPLOAD_GB} GB "
                f"programmatic buy (have {prev_seedbonus:.0f} BP, need "
                f"at least {min_bp:,} BP)",
            )
    else:
        if body.gb is None or body.gb <= 0:
            raise HTTPException(400, "gb must be a positive number")
        if body.gb < MIN_UPLOAD_GB:
            raise HTTPException(
                400,
                f"MAM rejects programmatic upload-credit buys under "
                f"{MIN_UPLOAD_GB} GB (got {body.gb} GB). Use a larger "
                f"amount or switch to `mode=max_affordable`.",
            )
        gb = body.gb

    result = await buy_upload_credit(gb, token=token)
    return await _persist_manual_buy_result(
        result,
        action=economy_audit.ACTION_UPLOAD,
        tier="trigger:manual",
        amount=_format_gb(gb),
        prev_seedbonus=prev_seedbonus,
        timestamp_key="mam_economy_last_upload_buy_at",
    )


@router.post("/personal-fl/buy", response_model=BuyResponse)
async def personal_fl_buy(body: PersonalFlBuyRequest) -> BuyResponse:
    """Spend `BP_PER_PERSONAL_FL` BP to flag the torrent as personal
    freeleech on MAM's side.

    After a successful buy, the torrent-info cache is invalidated so
    the next grab (manual or IRC) re-reads the authoritative
    `personal_freeleech=True` from MAM. Callers that chain an inject
    onto this endpoint benefit from that invalidation: they get a
    free tier on the grab decision without further configuration.
    """
    token = _require_token()
    prev_seedbonus = await _fetch_prev_seedbonus(token)
    result = await buy_personal_freeleech(body.torrent_id, token=token)

    # Whether or not the buy succeeded, we audit through the shared
    # helper; `torrent_id` distinguishes it from the other actions.
    response = await _persist_manual_buy_result(
        result,
        action=economy_audit.ACTION_PERSONAL_FL,
        tier="trigger:manual",
        amount=None,
        prev_seedbonus=prev_seedbonus,
        # Personal-FL doesn't consume a scheduler interval, so there's
        # no shared-timestamp to bump.
        timestamp_key=None,
        torrent_id=body.torrent_id,
    )
    if result.success:
        invalidate_torrent_info()
    return response


# ─── Audit + preflight ──────────────────────────────────────


class AuditRow(BaseModel):
    id: int
    occurred_at: str
    action: str
    trigger: str
    outcome: str
    mode: Optional[str] = None
    amount: Optional[str] = None
    torrent_id: Optional[str] = None
    tier: Optional[str] = None
    message: Optional[str] = None
    cost_points: Optional[float] = None
    user_bonus_after: Optional[float] = None


@router.get("/audit", response_model=list[AuditRow])
async def audit_rows(
    limit: int = 50, action: Optional[str] = None
) -> list[AuditRow]:
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    db = await get_db()
    try:
        rows = await economy_audit.list_recent(db, limit=limit, action=action)
    finally:
        await db.close()
    return [AuditRow(**row.__dict__) for row in rows]


class PreflightRequest(BaseModel):
    torrent_id: str = Field(..., min_length=1)


class PreflightResponse(BaseModel):
    size_gb: float
    buffer_gb: float
    safety_margin_gb: float
    sufficient: bool
    shortfall_gb: float
    # Cost to cover the shortfall with an upload-credit buy, rounded
    # up to the nearest whole GB so the frontend can wire a one-click
    # "buy exactly enough" button.
    recommended_buy_gb: float
    recommended_buy_cost_bp: int


@router.post("/preflight", response_model=PreflightResponse)
async def preflight(body: PreflightRequest) -> PreflightResponse:
    """Answer "would the buffer gate allow this torrent right now?"

    Used by the BufferInsufficientBanner on the manual-inject
    confirm dialog so users get a real-time readout without having
    to attempt the grab and parse the skip reason. Also powers the
    "Buy N GB" one-click button that ships that exact shortfall to
    `/upload/buy`.
    """
    token = _require_token()
    settings = load_settings()
    margin_gb = float(
        settings.get("mam_economy_buffer_gate_safety_margin_gb", 1) or 0
    )

    try:
        info = await get_torrent_info(body.torrent_id, token=token, ttl=0)
    except TorrentInfoError as e:
        raise HTTPException(502, f"Couldn't fetch torrent info: {e}") from e
    try:
        status = await get_user_status(token=token, ttl=0)
    except UserStatusError as e:
        raise HTTPException(502, f"Couldn't fetch user status: {e}") from e

    try:
        size_bytes = int(info.size) if info.size else 0
    except (TypeError, ValueError):
        size_bytes = 0
    buffer_bytes = int(status.upload_buffer_bytes or 0)
    margin_bytes = int(margin_gb * 1_000_000_000)

    size_gb = size_bytes / 1_000_000_000.0
    buffer_gb = buffer_bytes / 1_000_000_000.0
    sufficient = size_bytes + margin_bytes <= buffer_bytes
    shortfall_bytes = max(
        0, (size_bytes + margin_bytes) - buffer_bytes
    )
    shortfall_gb = shortfall_bytes / 1_000_000_000.0
    # Round up to the nearest whole GB so the user can click "Buy 6 GB"
    # rather than "Buy 5.37 GB" — MAM accepts fractions but integers
    # are friendlier in the UI.
    recommended_buy_gb = (
        0.0 if sufficient else float(int(shortfall_gb) + (0 if shortfall_gb == int(shortfall_gb) else 1))
    )
    return PreflightResponse(
        size_gb=size_gb,
        buffer_gb=buffer_gb,
        safety_margin_gb=margin_gb,
        sufficient=sufficient,
        shortfall_gb=shortfall_gb,
        recommended_buy_gb=recommended_buy_gb,
        recommended_buy_cost_bp=int(recommended_buy_gb * BP_PER_UPLOAD_GB),
    )


# ─── Internals ──────────────────────────────────────────────


def _require_token() -> str:
    """Return the configured MAM token, or 412 if none.

    412 (Precondition Failed) distinguishes "Seshat isn't set up for
    MAM yet" from "the MAM call itself failed" (502).
    """
    settings = load_settings()
    token = settings.get("mam_session_id", "") or ""
    if not token:
        raise HTTPException(
            412,
            "No MAM session cookie configured — set one in Settings first",
        )
    return token


async def _fetch_prev_seedbonus(token: str) -> float:
    """Fresh fetch so the cost-derivation post-buy is accurate.

    The 5-minute user_status cache would otherwise let a series of
    manual buys racewith each other and compute cost against a stale
    baseline.
    """
    invalidate_user_status()
    try:
        status = await get_user_status(token=token, ttl=0)
    except UserStatusError as e:
        raise HTTPException(502, f"Couldn't fetch current seedbonus: {e}") from e
    return float(status.seedbonus)


async def _persist_manual_buy_result(
    result: BuyResult,
    *,
    action: str,
    tier: str,
    amount: Optional[str],
    prev_seedbonus: float,
    timestamp_key: Optional[str],
    torrent_id: Optional[str] = None,
) -> BuyResponse:
    """Write the audit row, bump the shared timestamp on success,
    and return the response the frontend wants."""
    if result.success:
        cost = None
        if result.new_seedbonus is not None:
            cost = max(0.0, prev_seedbonus - result.new_seedbonus)
        db = await get_db()
        try:
            await economy_audit.record(
                db,
                action=action,
                trigger=economy_audit.TRIGGER_MANUAL,
                outcome=economy_audit.OUTCOME_SUCCESS,
                tier=tier,
                amount=amount,
                torrent_id=torrent_id,
                message=result.message,
                cost_points=cost,
                user_bonus_after=result.new_seedbonus,
            )
        finally:
            await db.close()

        # Dry-run simulated buys skip the timestamp bump — otherwise
        # toggling dry-run off would leave a phantom lockout where
        # the scheduler thinks we "just bought" a moment ago.
        if timestamp_key is not None and not result.dry_run:
            s = dict(load_settings())
            s[timestamp_key] = time.time()
            save_settings(s)

        return BuyResponse(
            ok=True,
            message=result.message,
            new_seedbonus=result.new_seedbonus,
            cost_points=cost,
            amount=amount,
        )

    # Failure path.
    db = await get_db()
    try:
        await economy_audit.record(
            db,
            action=action,
            trigger=economy_audit.TRIGGER_MANUAL,
            outcome=economy_audit.OUTCOME_FAILURE,
            tier=tier,
            amount=amount,
            torrent_id=torrent_id,
            message=result.message,
        )
    finally:
        await db.close()
    return BuyResponse(ok=False, message=result.message, amount=amount)


def _format_gb(gb: float) -> str:
    if gb == int(gb):
        return str(int(gb))
    return f"{gb:.2f}"


# Small sanity check during import — catch a renamed constant before
# production traffic hits the endpoint.
assert BP_PER_PERSONAL_FL > 0
