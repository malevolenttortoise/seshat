"""
MAM economy decision engine.

Pure decision functions — no I/O, no database, no HTTP. Given a
`UserStatus` snapshot, a config object, and the last-buy timestamp,
each `decide_*` call returns an `EconomyDecision` that the scheduler
or router can act on (or audit as a skip).

The split mirrors `app/policy/engine.py`: all orchestration (cookie
resolution, scheduling, HTTP calls, audit writes) lives one layer up.
This module only encodes the rules.

Outcome codes on the returned `EconomyDecision.reason` are the same
strings the `economy_audit` table stores — there's exactly one column
for this field, so the scheduler can stamp it directly without a
translation layer.

    disabled               — feature flag is off
    below_interval         — less time elapsed since last buy than the
                             configured interval
    no_trigger             — upload: none of ratio/buffer/bonus fired
    insufficient_bonus     — a trigger fired but the user can't afford
                             the computed amount
    trigger:interval       — VIP: the interval elapsed; buy the
                             configured weeks
    trigger:ratio          — upload: ratio dropped below floor
    trigger:buffer         — upload: buffer dropped below floor
    trigger:bonus          — upload: seedbonus exceeded ceiling; spend
                             the excess on buffer

For the bonus trigger, the spend amount is computed here:
`(seedbonus − bonus_ceiling) / BP_PER_UPLOAD_GB`. That exact
formulation guarantees the post-buy seedbonus lands at the ceiling
when MAM charges linearly, which is the whole point of the knob
("I want to keep at most N BP reserved"). Every other mode uses a
fixed `*_chunk_gb` value from config.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Union

from app.mam.bonus_buy import (
    BP_PER_UPLOAD_GB,
    BP_PER_VIP_WEEK,
    MIN_UPLOAD_GB,
)
from app.mam.user_status import UserStatus


Action = Literal["buy", "skip"]

# VIP-weeks values MAM's endpoint accepts.
VipWeeks = Union[int, Literal["max"]]


@dataclass(frozen=True)
class VipBuyConfig:
    """User-configured rules for the VIP auto-buy loop."""

    enabled: bool = False
    interval_hours: float = 24.0
    # Refuse to buy when seedbonus is below this value — a floor so
    # the auto-buy doesn't drain the account when a manual big spend
    # has already happened. 0 disables the floor.
    min_bonus: int = 0
    # Amount to buy on each firing tick. Numeric values (4/8/12)
    # cost exactly `weeks * BP_PER_VIP_WEEK`; "max" lets MAM decide
    # how much to credit based on current balance and 90-day cap.
    weeks: VipWeeks = 4


@dataclass(frozen=True)
class UploadBuyConfig:
    """User-configured rules for the upload-credit auto-buy loop."""

    enabled: bool = False
    interval_hours: float = 6.0

    # Ratio trigger — fire when ratio drops below `ratio_floor` and
    # buy a fixed `ratio_chunk_gb` of upload credit.
    ratio_trigger: bool = False
    ratio_floor: float = 1.5
    ratio_chunk_gb: float = 50.0

    # Buffer trigger — fire when upload buffer drops below
    # `buffer_floor_gb` and buy a fixed `buffer_chunk_gb`.
    buffer_trigger: bool = False
    buffer_floor_gb: float = 10.0
    buffer_chunk_gb: float = 50.0

    # Bonus-excess trigger — fire when seedbonus exceeds
    # `bonus_ceiling` and convert the entire overage into upload
    # credit. Amount is (seedbonus − ceiling) / BP_PER_UPLOAD_GB.
    bonus_trigger: bool = False
    bonus_ceiling: int = 5000


@dataclass(frozen=True)
class EconomyDecision:
    """Outcome of a single `decide_*` call.

    `action` is the one field every caller branches on. `reason` maps
    1:1 to the audit `outcome`/`tier` columns, so the scheduler just
    stamps it verbatim.

    `amount_gb` / `weeks` carry the computed buy amount on `action="buy"`
    decisions. `estimated_cost_bp` is best-effort — None when the real
    cost depends on MAM-side state ("max" VIP, server-side pricing
    changes), in which case the caller should read the actual cost from
    the `BuyResult` after the purchase fires.
    """

    action: Action
    reason: str
    amount_gb: Optional[float] = None
    weeks: Optional[VipWeeks] = None
    mode: Optional[str] = None  # "ratio" | "buffer" | "bonus" | None
    estimated_cost_bp: Optional[int] = None


# ─── Cost helpers ───────────────────────────────────────────


def estimate_upload_cost_bp(gb: float) -> int:
    """Bonus points a given GB buy will cost at MAM's linear rate.

    Used both by decision functions (for pre-buy affordability checks)
    and by the router (for the confirm-dialog cost estimate). Rounded
    to the nearest int because MAM quantizes the displayed cost.
    """
    return int(round(gb * BP_PER_UPLOAD_GB))


def max_affordable_upload_gb(seedbonus: float) -> float:
    """Biggest upload-credit amount a balance can fund.

    Used by the router's "Max Affordable" preset. Floor-divided is
    deliberate — offering 49.98 GB when the user has 24,992 BP looks
    worse than returning 0 and surfacing the minimum-buy error. MAM
    rejects programmatic buys under `MIN_UPLOAD_GB`, so "max
    affordable" for a balance below that threshold is genuinely
    zero — the button should be unclickable, not silently submit a
    doomed 49.98 GB request.
    """
    if seedbonus <= 0:
        return 0.0
    whole_gb = int(seedbonus // BP_PER_UPLOAD_GB)
    if whole_gb < MIN_UPLOAD_GB:
        return 0.0
    return float(whole_gb)


# ─── Decisions ──────────────────────────────────────────────


def decide_vip_buy(
    status: UserStatus,
    config: VipBuyConfig,
    *,
    last_bought_at: float,
    now_ts: float,
) -> EconomyDecision:
    """Should the VIP auto-buy loop fire right now?

    Pure function. The scheduler reads `last_bought_at` from settings
    (shared with the manual-trigger path so a click bumps the timer
    and prevents a double-buy on the next tick) and `now_ts` from the
    clock.
    """
    if not config.enabled:
        return EconomyDecision(action="skip", reason="disabled")

    if not _interval_elapsed(last_bought_at, now_ts, config.interval_hours):
        return EconomyDecision(action="skip", reason="below_interval")

    if config.min_bonus > 0 and status.seedbonus < config.min_bonus:
        return EconomyDecision(
            action="skip",
            reason="insufficient_bonus",
            weeks=config.weeks,
        )

    # When the caller chose a numeric weeks value we can compute the
    # exact cost and short-circuit if the balance won't cover it. For
    # "max", only MAM knows how much will actually get credited, so
    # we let the request fire and handle insufficient-bonus on the
    # response path.
    estimated_cost: Optional[int] = None
    if isinstance(config.weeks, int):
        estimated_cost = config.weeks * BP_PER_VIP_WEEK
        if status.seedbonus < estimated_cost:
            return EconomyDecision(
                action="skip",
                reason="insufficient_bonus",
                weeks=config.weeks,
                estimated_cost_bp=estimated_cost,
            )

    return EconomyDecision(
        action="buy",
        reason="trigger:interval",
        weeks=config.weeks,
        estimated_cost_bp=estimated_cost,
    )


def decide_upload_buy(
    status: UserStatus,
    config: UploadBuyConfig,
    *,
    last_bought_at: float,
    now_ts: float,
) -> EconomyDecision:
    """Should the upload-credit auto-buy loop fire right now?

    Three independent triggers are evaluated in a fixed priority
    order: ratio → buffer → bonus. The first one to fire wins; the
    others are ignored for this tick. Ordering matters because a user
    who has multiple triggers enabled is usually saying "ratio matters
    most, then buffer, then spend excess BP" — ratio-low is the most
    urgent economic problem.
    """
    if not config.enabled:
        return EconomyDecision(action="skip", reason="disabled")

    if not _interval_elapsed(last_bought_at, now_ts, config.interval_hours):
        return EconomyDecision(action="skip", reason="below_interval")

    # Trigger check in priority order. `mode` stays None if nothing fires.
    mode: Optional[str] = None
    gb: Optional[float] = None

    if config.ratio_trigger and status.ratio < config.ratio_floor:
        mode = "ratio"
        gb = config.ratio_chunk_gb
    elif (
        config.buffer_trigger
        and (status.upload_buffer_bytes / 1_000_000_000.0) < config.buffer_floor_gb
    ):
        mode = "buffer"
        gb = config.buffer_chunk_gb
    elif config.bonus_trigger and status.seedbonus > config.bonus_ceiling:
        excess = status.seedbonus - config.bonus_ceiling
        gb = excess / BP_PER_UPLOAD_GB
        mode = "bonus"

    if mode is None or gb is None:
        return EconomyDecision(action="skip", reason="no_trigger")

    # MAM's minimum-buy floor. A sub-50 GB buy would round-trip to
    # MAM only to come back as a log-spam rejection; skip it here
    # so the audit shows a clean no_trigger instead of a noisy
    # failure. Reached when:
    #   - ratio/buffer chunk_gb is misconfigured below 50
    #     (router's PUT /config rejects this, but the decision
    #     engine stays defensive in case settings.json is hand-
    #     edited),
    #   - bonus mode's computed gb is below 50 because the excess
    #     over the ceiling is smaller than `MIN_UPLOAD_GB *
    #     BP_PER_UPLOAD_GB` (= 25,000 BP at MAM's default pricing).
    if gb < MIN_UPLOAD_GB:
        return EconomyDecision(
            action="skip",
            reason="no_trigger",
            amount_gb=gb,
            mode=mode,
        )

    estimated_cost = estimate_upload_cost_bp(gb)
    if status.seedbonus < estimated_cost:
        return EconomyDecision(
            action="skip",
            reason="insufficient_bonus",
            amount_gb=gb,
            mode=mode,
            estimated_cost_bp=estimated_cost,
        )

    return EconomyDecision(
        action="buy",
        reason=f"trigger:{mode}",
        amount_gb=gb,
        mode=mode,
        estimated_cost_bp=estimated_cost,
    )


# ─── Internals ──────────────────────────────────────────────


def _interval_elapsed(
    last_bought_at: float, now_ts: float, interval_hours: float
) -> bool:
    """True when enough wall time has elapsed since the last buy.

    `last_bought_at == 0` is the "never bought" sentinel — it counts
    as infinitely long ago, so the first real tick always fires.
    Negative `interval_hours` is treated as "always fire" (useful
    for manual triggers that bypass the interval gate).
    """
    if interval_hours <= 0:
        return True
    if last_bought_at <= 0:
        return True
    return (now_ts - last_bought_at) >= (interval_hours * 3600.0)
