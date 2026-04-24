"""
MAM economy auto-buy scheduler.

Two independent supervised loops, one per feature:

  - `vip_autobuy_loop()`     — fires `vip_tick()` every 60s
  - `upload_autobuy_loop()`  — fires `upload_tick()` every 60s

The 60-second wake cadence is decoupled from the user-configured
`interval_hours`. Every wake is cheap — if the feature is disabled or
the interval hasn't elapsed, the tick returns silently without
reading MAM or writing an audit row. An audit row is only written
when the tick actually makes a decision at the interval boundary:

  - success        — `bonus_buy` returned success; settings
                     `mam_economy_last_*_buy_at` is bumped and the
                     seedbonus cost is recorded
  - failure        — `bonus_buy` returned success=False (MAM rejected)
  - skip_no_trigger — upload only: none of ratio/buffer/bonus fired
  - skip_insufficient_bonus — a trigger fired but the user can't afford it

Skips on `disabled` / `below_interval` are silent at the scheduler
layer by design. Those two outcome codes DO exist in the audit
vocabulary — but they're reserved for manual "Buy now" router hits
(where the user explicitly clicked something and deserves a visible
row). Scheduler ticks that short-circuit before the interval boundary
log at DEBUG and move on, keeping the audit table readable.

The scheduler AND the manual-buy router share `mam_economy_last_*_buy_at`
as a timestamp lockout — a manual click bumps the stamp and prevents a
double-buy on the next tick.

Token resolution goes through the discovery router helper so it
reads from the encrypted secrets store first (`mam_session_id`
secret) with a settings.json fallback — matching how the existing
`mam_scheduler_loop` resolves its token.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from app.config import load_settings, save_settings
from app.database import get_db
from app.mam.bonus_buy import (
    BuyResult,
    buy_upload_credit,
    buy_vip,
)
from app.mam.economy import (
    EconomyDecision,
    UploadBuyConfig,
    VipBuyConfig,
    decide_upload_buy,
    decide_vip_buy,
)
from app.mam.user_status import (
    UserStatusError,
    get_user_status,
)
from app.storage import economy_audit

_log = logging.getLogger("seshat.orchestrator.economy_scheduler")

# Fixed wake cadence — the interval-gate inside decide_*_buy decides
# whether the cycle actually fires. Matching `mam_scheduler_loop`'s
# 60s pattern keeps settings-change responsiveness consistent across
# the three Seshat schedulers.
_WAKE_SECONDS = 60


# ─── Config builders ────────────────────────────────────────


def build_vip_config(settings: dict) -> VipBuyConfig:
    """Assemble a VipBuyConfig from the flat settings keys."""
    return VipBuyConfig(
        enabled=bool(settings.get("mam_economy_vip_enabled", False)),
        interval_hours=float(settings.get("mam_economy_vip_interval_hours", 24) or 24),
        min_bonus=int(settings.get("mam_economy_vip_min_bonus", 0) or 0),
        weeks=settings.get("mam_economy_vip_weeks", 4),
    )


def build_upload_config(settings: dict) -> UploadBuyConfig:
    """Assemble an UploadBuyConfig from the flat settings keys."""
    return UploadBuyConfig(
        enabled=bool(settings.get("mam_economy_upload_enabled", False)),
        interval_hours=float(
            settings.get("mam_economy_upload_interval_hours", 6) or 6
        ),
        ratio_trigger=bool(settings.get("mam_economy_upload_ratio_trigger", False)),
        ratio_floor=float(settings.get("mam_economy_upload_ratio_floor", 1.5) or 1.5),
        ratio_chunk_gb=float(settings.get("mam_economy_upload_ratio_chunk_gb", 50) or 50),
        buffer_trigger=bool(settings.get("mam_economy_upload_buffer_trigger", False)),
        buffer_floor_gb=float(settings.get("mam_economy_upload_buffer_floor_gb", 10) or 10),
        buffer_chunk_gb=float(settings.get("mam_economy_upload_buffer_chunk_gb", 50) or 50),
        bonus_trigger=bool(settings.get("mam_economy_upload_bonus_trigger", False)),
        bonus_ceiling=int(settings.get("mam_economy_upload_bonus_ceiling", 5000) or 5000),
    )


# ─── Ticks ──────────────────────────────────────────────────


async def vip_tick() -> Optional[str]:
    """Run one VIP auto-buy cycle. Returns the audit outcome, or None
    when the tick short-circuits before an audit row would be written.

    Short-circuit paths (no audit, DEBUG log):
      - feature disabled
      - less than `interval_hours` since `mam_economy_last_vip_buy_at`
      - no MAM session token configured
    """
    settings = load_settings()
    config = build_vip_config(settings)
    if not config.enabled:
        return None

    last_bought_at = float(settings.get("mam_economy_last_vip_buy_at", 0.0) or 0.0)
    now_ts = time.time()

    # Cheap early-out — avoids a user_status fetch on every wake tick.
    if not _interval_elapsed(last_bought_at, now_ts, config.interval_hours):
        _log.debug("vip tick: interval not elapsed, skipping")
        return None

    token = await _resolve_mam_token()
    if not token:
        _log.debug("vip tick: no MAM token configured, skipping")
        return None

    try:
        status = await get_user_status(token=token, ttl=0)
    except UserStatusError as e:
        _log.warning("vip tick: user_status fetch failed: %s", e)
        return await _audit(
            action=economy_audit.ACTION_VIP,
            outcome=economy_audit.OUTCOME_FAILURE,
            trigger=economy_audit.TRIGGER_SCHEDULED,
            message=f"user_status fetch failed: {e}",
        )

    decision = decide_vip_buy(
        status, config, last_bought_at=last_bought_at, now_ts=now_ts,
    )
    if decision.action == "skip":
        # At this point `reason` can only be insufficient_bonus (the
        # disabled/below_interval gates ran above). We DO audit this —
        # the interval elapsed and we chose not to buy; the user
        # should be able to see why.
        return await _audit(
            action=economy_audit.ACTION_VIP,
            outcome=f"skip_{decision.reason}",
            trigger=economy_audit.TRIGGER_SCHEDULED,
            tier=decision.reason,
            amount=str(decision.weeks) if decision.weeks is not None else None,
            message="insufficient seedbonus",
        )

    # Decision said buy — fire bonus_buy.
    result = await buy_vip(decision.weeks, token=token)
    return await _record_buy_outcome(
        result,
        action=economy_audit.ACTION_VIP,
        trigger=economy_audit.TRIGGER_SCHEDULED,
        tier=decision.reason,
        amount=str(decision.weeks),
        prev_seedbonus=status.seedbonus,
        timestamp_key="mam_economy_last_vip_buy_at",
        now_ts=now_ts,
    )


async def upload_tick() -> Optional[str]:
    """Run one upload-credit auto-buy cycle."""
    settings = load_settings()
    config = build_upload_config(settings)
    if not config.enabled:
        return None

    last_bought_at = float(
        settings.get("mam_economy_last_upload_buy_at", 0.0) or 0.0
    )
    now_ts = time.time()

    if not _interval_elapsed(last_bought_at, now_ts, config.interval_hours):
        _log.debug("upload tick: interval not elapsed, skipping")
        return None

    token = await _resolve_mam_token()
    if not token:
        _log.debug("upload tick: no MAM token configured, skipping")
        return None

    try:
        status = await get_user_status(token=token, ttl=0)
    except UserStatusError as e:
        _log.warning("upload tick: user_status fetch failed: %s", e)
        return await _audit(
            action=economy_audit.ACTION_UPLOAD,
            outcome=economy_audit.OUTCOME_FAILURE,
            trigger=economy_audit.TRIGGER_SCHEDULED,
            message=f"user_status fetch failed: {e}",
        )

    decision = decide_upload_buy(
        status, config, last_bought_at=last_bought_at, now_ts=now_ts,
    )
    if decision.action == "skip":
        # Here `reason` is no_trigger or insufficient_bonus — both
        # worth recording because the interval elapsed and we chose
        # not to buy. `disabled`/`below_interval` can't reach here.
        return await _audit(
            action=economy_audit.ACTION_UPLOAD,
            outcome=f"skip_{decision.reason}",
            trigger=economy_audit.TRIGGER_SCHEDULED,
            mode=decision.mode,
            tier=decision.reason,
            amount=_format_gb(decision.amount_gb),
            message=_upload_skip_message(decision),
        )

    # Decision said buy.
    assert decision.amount_gb is not None  # by decide_upload_buy contract
    result = await buy_upload_credit(decision.amount_gb, token=token)
    return await _record_buy_outcome(
        result,
        action=economy_audit.ACTION_UPLOAD,
        trigger=economy_audit.TRIGGER_SCHEDULED,
        tier=decision.reason,
        mode=decision.mode,
        amount=_format_gb(decision.amount_gb),
        prev_seedbonus=status.seedbonus,
        timestamp_key="mam_economy_last_upload_buy_at",
        now_ts=now_ts,
    )


# ─── Loops ──────────────────────────────────────────────────


async def vip_autobuy_loop(
    *, stop_event: Optional[asyncio.Event] = None
) -> None:
    """Long-running supervised task wrapping `vip_tick()`."""
    await _run_loop("vip", vip_tick, stop_event=stop_event)


async def upload_autobuy_loop(
    *, stop_event: Optional[asyncio.Event] = None
) -> None:
    """Long-running supervised task wrapping `upload_tick()`."""
    await _run_loop("upload", upload_tick, stop_event=stop_event)


async def _run_loop(label, tick_fn, *, stop_event):
    """Generic 60s-wake loop shared by both auto-buy features.

    `tick_fn` is called without arguments; it reads settings on every
    invocation so live config edits take effect on the next wake.
    Exceptions inside `tick_fn` log a warning and the loop continues
    — the supervised_task wrapper restarts the whole coroutine on
    hard crashes, but per-tick transient failures shouldn't bounce
    the whole task.
    """
    _log.info(f"{label} auto-buy loop started (wake every {_WAKE_SECONDS}s)")
    while True:
        try:
            await tick_fn()
        except Exception:
            _log.exception(f"{label} auto-buy tick raised (continuing)")

        if stop_event is not None and stop_event.is_set():
            _log.info(f"{label} auto-buy stop_event signaled, exiting loop")
            return
        try:
            if stop_event is not None:
                await asyncio.wait_for(stop_event.wait(), timeout=_WAKE_SECONDS)
                _log.info(
                    f"{label} auto-buy stop_event during sleep, exiting loop"
                )
                return
            else:
                await asyncio.sleep(_WAKE_SECONDS)
        except asyncio.TimeoutError:
            continue


# ─── Internals ──────────────────────────────────────────────


def _interval_elapsed(
    last_bought_at: float, now_ts: float, interval_hours: float
) -> bool:
    """Same logic as `economy._interval_elapsed` — kept local to avoid
    a dependency on a private symbol in the decision module."""
    if interval_hours <= 0:
        return True
    if last_bought_at <= 0:
        return True
    return (now_ts - last_bought_at) >= (interval_hours * 3600.0)


async def _resolve_mam_token() -> str:
    """Look up the live MAM session cookie.

    Matches `mam_scheduler_loop`'s resolution path so the two
    schedulers always agree on which cookie they're using. The
    secrets store is the authoritative source; settings.json is a
    fallback for legacy setups that haven't migrated.
    """
    from app.discovery.routers.mam import _get_mam_token
    token = await _get_mam_token()
    return token or ""


async def _audit(
    *,
    action: str,
    outcome: str,
    trigger: str,
    mode: Optional[str] = None,
    amount: Optional[str] = None,
    torrent_id: Optional[str] = None,
    tier: Optional[str] = None,
    message: Optional[str] = None,
    cost_points: Optional[float] = None,
    user_bonus_after: Optional[float] = None,
) -> str:
    """Write one economy_audit row and return its outcome code.

    Wraps `economy_audit.record` so the scheduler can audit + return
    in one expression. Opens and closes a dedicated DB connection
    for the write; audit contention is low enough that holding one
    shared connection across ticks would be premature optimization.
    """
    db = await get_db()
    try:
        await economy_audit.record(
            db,
            action=action,
            trigger=trigger,
            outcome=outcome,
            mode=mode,
            amount=amount,
            torrent_id=torrent_id,
            tier=tier,
            message=message,
            cost_points=cost_points,
            user_bonus_after=user_bonus_after,
        )
    finally:
        await db.close()
    return outcome


async def _record_buy_outcome(
    result: BuyResult,
    *,
    action: str,
    trigger: str,
    tier: str,
    amount: str,
    prev_seedbonus: float,
    timestamp_key: str,
    now_ts: float,
    mode: Optional[str] = None,
) -> str:
    """Translate a `BuyResult` into an audit row + (on success) the
    shared-timestamp bump.

    Cost is derived as `prev_seedbonus − new_seedbonus` rather than
    trusted from MAM directly — the response doesn't expose a cost
    field, but the balance deltas are authoritative.
    """
    if not result.success:
        return await _audit(
            action=action,
            outcome=economy_audit.OUTCOME_FAILURE,
            trigger=trigger,
            tier=tier,
            mode=mode,
            amount=amount,
            message=result.message,
        )

    cost = None
    if result.new_seedbonus is not None:
        cost = max(0.0, prev_seedbonus - result.new_seedbonus)

    outcome = await _audit(
        action=action,
        outcome=economy_audit.OUTCOME_SUCCESS,
        trigger=trigger,
        tier=tier,
        mode=mode,
        amount=amount,
        message=result.message,
        cost_points=cost,
        user_bonus_after=result.new_seedbonus,
    )

    # Toast only fires for scheduled auto-buys. Manual buys go through
    # the economy router, which returns the outcome synchronously —
    # the router's own UI surface is already showing a response toast
    # and a second one would be noise.
    if trigger == economy_audit.TRIGGER_SCHEDULED:
        try:
            from app.orchestrator.sse_publishers import publish_toast
            label = "VIP" if action == economy_audit.ACTION_VIP else "upload credit"
            dry_prefix = "[dry-run] " if result.dry_run else ""
            await publish_toast(
                "success",
                f"{dry_prefix}Auto-buy: {label} {amount}"
                + (f" (cost {int(cost)} BP)" if cost else ""),
            )
        except Exception:
            _log.exception("auto-buy toast publish failed (non-fatal)")

    # Dry-run simulated buys skip the timestamp bump — otherwise
    # toggling dry-run off would leave a phantom lockout where the
    # scheduler thinks we "just bought" 10 minutes ago.
    if result.dry_run:
        return outcome

    # Bump the shared timestamp so the next scheduler tick AND any
    # manual Buy Now click within the interval window both see the
    # interval as "just fired" and short-circuit.
    settings = load_settings()
    settings[timestamp_key] = now_ts
    save_settings(settings)
    return outcome


def _format_gb(gb: Optional[float]) -> Optional[str]:
    """Render a GB amount for the audit row — `None` stays `None`."""
    if gb is None:
        return None
    # Whole-GB values render cleanly without a trailing `.0`.
    if gb == int(gb):
        return str(int(gb))
    return f"{gb:.2f}"


def _upload_skip_message(decision: EconomyDecision) -> str:
    """Human-readable reason for a `skip` upload decision.

    The audit `outcome` column already machine-encodes this, but the
    UI's history table renders the `message` column directly — a one-
    line English sentence is much nicer than forcing the reader to
    interpret `skip_insufficient_bonus` by eye.
    """
    if decision.reason == "no_trigger":
        return "no trigger fired at this interval boundary"
    if decision.reason == "insufficient_bonus":
        mode = decision.mode or "trigger"
        return f"{mode} trigger fired but seedbonus couldn't cover the buy"
    return decision.reason
