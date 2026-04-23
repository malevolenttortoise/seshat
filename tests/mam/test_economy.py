"""
Unit tests for the MAM economy decision engine.

Pure logic, no I/O. Every test constructs a UserStatus + config and
asserts on the returned EconomyDecision. Exhaustive because the
scheduler spends real bonus points based on these decisions and a
regression is silent and expensive.
"""
from __future__ import annotations

import pytest

from app.mam.bonus_buy import BP_PER_UPLOAD_GB, BP_PER_VIP_WEEK
from app.mam.economy import (
    EconomyDecision,
    UploadBuyConfig,
    VipBuyConfig,
    decide_upload_buy,
    decide_vip_buy,
    estimate_upload_cost_bp,
    max_affordable_upload_gb,
)
from app.mam.user_status import UserStatus


def _status(
    *,
    ratio: float = 2.0,
    seedbonus: float = 100_000.0,
    upload_buffer_bytes: int = 20_000_000_000,  # 20 GB
    wedges: int = 5,
    uploaded_bytes: int = 1_000_000_000_000,
    downloaded_bytes: int = 500_000_000_000,
) -> UserStatus:
    return UserStatus(
        ratio=ratio,
        wedges=wedges,
        seedbonus=seedbonus,
        classname="Power User",
        username="tester",
        uid=1,
        uploaded_bytes=uploaded_bytes,
        downloaded_bytes=downloaded_bytes,
        upload_buffer_bytes=upload_buffer_bytes,
    )


# ─── VIP decisions ──────────────────────────────────────────


class TestVipDisabled:
    def test_disabled_skips(self):
        d = decide_vip_buy(
            _status(),
            VipBuyConfig(enabled=False),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "skip"
        assert d.reason == "disabled"


class TestVipIntervalGate:
    def test_never_bought_fires_immediately(self):
        d = decide_vip_buy(
            _status(),
            VipBuyConfig(enabled=True, interval_hours=24),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "buy"
        assert d.reason == "trigger:interval"

    def test_too_soon_skips(self):
        now = 1_000_000
        # Last buy 1h ago, interval is 24h
        d = decide_vip_buy(
            _status(),
            VipBuyConfig(enabled=True, interval_hours=24),
            last_bought_at=now - 3600, now_ts=now,
        )
        assert d.action == "skip"
        assert d.reason == "below_interval"

    def test_exactly_at_interval_fires(self):
        now = 1_000_000
        d = decide_vip_buy(
            _status(),
            VipBuyConfig(enabled=True, interval_hours=24),
            last_bought_at=now - 24 * 3600, now_ts=now,
        )
        assert d.action == "buy"


class TestVipMinBonusFloor:
    def test_below_floor_skips(self):
        d = decide_vip_buy(
            _status(seedbonus=1000),
            VipBuyConfig(enabled=True, min_bonus=5000),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "skip"
        assert d.reason == "insufficient_bonus"

    def test_zero_floor_does_not_gate(self):
        d = decide_vip_buy(
            _status(seedbonus=10),
            VipBuyConfig(enabled=True, min_bonus=0, weeks="max"),
            last_bought_at=0, now_ts=1_000_000,
        )
        # Even with 10 BP, "max" lets MAM decide — we don't block.
        assert d.action == "buy"


class TestVipAffordabilityCheck:
    def test_numeric_weeks_cost_checked(self):
        # 4 weeks × 1250 = 5000 BP; balance 4000 → insufficient.
        d = decide_vip_buy(
            _status(seedbonus=4000),
            VipBuyConfig(enabled=True, weeks=4),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "skip"
        assert d.reason == "insufficient_bonus"
        assert d.estimated_cost_bp == 4 * BP_PER_VIP_WEEK

    def test_numeric_weeks_balance_covers_cost(self):
        d = decide_vip_buy(
            _status(seedbonus=10_000),
            VipBuyConfig(enabled=True, weeks=4),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "buy"
        assert d.weeks == 4
        assert d.estimated_cost_bp == 4 * BP_PER_VIP_WEEK

    def test_max_weeks_defers_cost_to_mam(self):
        # "max" lets MAM decide the credit amount — no pre-check.
        d = decide_vip_buy(
            _status(seedbonus=100),
            VipBuyConfig(enabled=True, weeks="max"),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "buy"
        assert d.weeks == "max"
        assert d.estimated_cost_bp is None


# ─── Upload decisions ──────────────────────────────────────


class TestUploadDisabled:
    def test_disabled_skips(self):
        d = decide_upload_buy(
            _status(),
            UploadBuyConfig(enabled=False),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.reason == "disabled"


class TestUploadIntervalGate:
    def test_never_bought_fires_when_trigger_matches(self):
        d = decide_upload_buy(
            _status(ratio=1.0),
            UploadBuyConfig(
                enabled=True, interval_hours=6,
                ratio_trigger=True, ratio_floor=1.5,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "buy"

    def test_too_soon_skips_before_trigger_check(self):
        now = 1_000_000
        d = decide_upload_buy(
            _status(ratio=1.0),  # would trigger, but interval blocks
            UploadBuyConfig(
                enabled=True, interval_hours=6,
                ratio_trigger=True, ratio_floor=1.5,
            ),
            last_bought_at=now - 60, now_ts=now,
        )
        assert d.action == "skip"
        assert d.reason == "below_interval"


class TestUploadRatioTrigger:
    def test_fires_below_floor(self):
        d = decide_upload_buy(
            _status(ratio=1.2),
            UploadBuyConfig(
                enabled=True, ratio_trigger=True,
                ratio_floor=1.5, ratio_chunk_gb=50,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "buy"
        assert d.mode == "ratio"
        assert d.reason == "trigger:ratio"
        assert d.amount_gb == 50

    def test_does_not_fire_at_floor(self):
        d = decide_upload_buy(
            _status(ratio=1.5),
            UploadBuyConfig(
                enabled=True, ratio_trigger=True, ratio_floor=1.5,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "skip"
        assert d.reason == "no_trigger"

    def test_trigger_disabled_does_not_fire(self):
        d = decide_upload_buy(
            _status(ratio=0.1),
            UploadBuyConfig(
                enabled=True, ratio_trigger=False, ratio_floor=1.5,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "skip"
        assert d.reason == "no_trigger"


class TestUploadBufferTrigger:
    def test_fires_below_floor_gb(self):
        # 5 GB buffer, floor is 10 GB
        d = decide_upload_buy(
            _status(upload_buffer_bytes=5_000_000_000),
            UploadBuyConfig(
                enabled=True, buffer_trigger=True,
                buffer_floor_gb=10, buffer_chunk_gb=50,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "buy"
        assert d.mode == "buffer"
        assert d.amount_gb == 50

    def test_does_not_fire_above_floor(self):
        d = decide_upload_buy(
            _status(upload_buffer_bytes=15_000_000_000),
            UploadBuyConfig(
                enabled=True, buffer_trigger=True, buffer_floor_gb=10,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "skip"


class TestUploadBonusTrigger:
    def test_fires_above_ceiling(self):
        # 40000 seedbonus, ceiling 5000 → excess 35000 / 500 = 70 GB.
        # Amounts must be >= MAM's 50 GB programmatic floor.
        d = decide_upload_buy(
            _status(seedbonus=40_000),
            UploadBuyConfig(
                enabled=True, bonus_trigger=True, bonus_ceiling=5000,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "buy"
        assert d.mode == "bonus"
        assert d.reason == "trigger:bonus"
        assert d.amount_gb == pytest.approx(70.0)
        assert d.estimated_cost_bp == 35_000

    def test_spend_drops_balance_to_ceiling(self):
        # Post-buy: seedbonus − cost = ceiling (the design invariant).
        # Needs excess >= 25000 BP (50 GB minimum × 500 BP/GB).
        d = decide_upload_buy(
            _status(seedbonus=32_345),
            UploadBuyConfig(
                enabled=True, bonus_trigger=True, bonus_ceiling=5000,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        post = 32_345 - d.estimated_cost_bp
        assert post == 5000

    def test_at_ceiling_does_not_fire(self):
        d = decide_upload_buy(
            _status(seedbonus=5000),
            UploadBuyConfig(
                enabled=True, bonus_trigger=True, bonus_ceiling=5000,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "skip"
        assert d.reason == "no_trigger"

    def test_excess_below_min_buy_floor_skips(self):
        # Ceiling 5000, balance 10100 → excess 5100 / 500 = 10.2 GB.
        # Even though the user technically has enough to buy 10 GB,
        # MAM rejects sub-50 GB programmatic buys, so we skip instead
        # of letting a doomed request through. Next tick retries once
        # the excess grows enough for a 50+ GB buy (25,000+ BP above
        # the ceiling).
        d = decide_upload_buy(
            _status(seedbonus=10_100),
            UploadBuyConfig(
                enabled=True, bonus_trigger=True, bonus_ceiling=5000,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "skip"
        assert d.reason == "no_trigger"


class TestUploadTriggerPriority:
    def test_ratio_beats_buffer_and_bonus(self):
        d = decide_upload_buy(
            _status(ratio=1.0, upload_buffer_bytes=1_000_000_000, seedbonus=100_000),
            UploadBuyConfig(
                enabled=True,
                ratio_trigger=True, ratio_floor=1.5, ratio_chunk_gb=50,
                buffer_trigger=True, buffer_floor_gb=10, buffer_chunk_gb=60,
                bonus_trigger=True, bonus_ceiling=1000,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.mode == "ratio"
        assert d.amount_gb == 50

    def test_buffer_beats_bonus_when_ratio_ok(self):
        d = decide_upload_buy(
            _status(ratio=5.0, upload_buffer_bytes=1_000_000_000, seedbonus=100_000),
            UploadBuyConfig(
                enabled=True,
                ratio_trigger=True, ratio_floor=1.5,
                buffer_trigger=True, buffer_floor_gb=10, buffer_chunk_gb=60,
                bonus_trigger=True, bonus_ceiling=1000,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.mode == "buffer"
        assert d.amount_gb == 60

    def test_bonus_fires_when_ratio_and_buffer_ok(self):
        d = decide_upload_buy(
            _status(ratio=5.0, upload_buffer_bytes=50_000_000_000, seedbonus=10_000),
            UploadBuyConfig(
                enabled=True,
                ratio_trigger=True, ratio_floor=1.5,
                buffer_trigger=True, buffer_floor_gb=10,
                bonus_trigger=True, bonus_ceiling=1000,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.mode == "bonus"


class TestUploadAffordability:
    def test_ratio_trigger_cannot_afford_skips(self):
        # 50 GB costs 25000 BP; balance 10000 → skip.
        d = decide_upload_buy(
            _status(ratio=1.0, seedbonus=10_000),
            UploadBuyConfig(
                enabled=True, ratio_trigger=True,
                ratio_floor=1.5, ratio_chunk_gb=50,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "skip"
        assert d.reason == "insufficient_bonus"
        assert d.mode == "ratio"
        assert d.amount_gb == 50
        assert d.estimated_cost_bp == 25_000

    def test_bonus_trigger_cannot_underrun_balance(self):
        # Bonus mode's formula guarantees seedbonus >= cost — there
        # is no scenario where affordability fails in bonus mode.
        # Need enough excess for MAM's 50 GB floor: 25,000 BP above
        # the ceiling minimum.
        d = decide_upload_buy(
            _status(seedbonus=35_000),
            UploadBuyConfig(
                enabled=True, bonus_trigger=True, bonus_ceiling=5000,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "buy"

    def test_ratio_chunk_below_min_floor_skips(self):
        # Router's PUT /config rejects sub-50 chunk values, but if
        # settings.json gets hand-edited the decision engine stays
        # defensive — skip with no_trigger rather than fire a doomed
        # request that MAM will reject with a log-spam error.
        d = decide_upload_buy(
            _status(ratio=1.0),
            UploadBuyConfig(
                enabled=True, ratio_trigger=True,
                ratio_floor=1.5, ratio_chunk_gb=20,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "skip"
        assert d.reason == "no_trigger"
        assert d.mode == "ratio"
        assert d.amount_gb == 20  # preserved so audit row shows what was attempted

    def test_buffer_chunk_below_min_floor_skips(self):
        d = decide_upload_buy(
            _status(upload_buffer_bytes=1_000_000_000),
            UploadBuyConfig(
                enabled=True, buffer_trigger=True,
                buffer_floor_gb=10, buffer_chunk_gb=10,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.action == "skip"
        assert d.reason == "no_trigger"
        assert d.mode == "buffer"


# ─── Cost helpers ───────────────────────────────────────────


class TestCostHelpers:
    def test_estimate_upload_cost_linear(self):
        assert estimate_upload_cost_bp(10) == 10 * BP_PER_UPLOAD_GB
        assert estimate_upload_cost_bp(2.5) == int(2.5 * BP_PER_UPLOAD_GB)

    def test_estimate_upload_cost_rounds_half(self):
        # 0.001 GB × 500 = 0.5 BP → rounds to 0 (banker's) or 1
        # (traditional); our code uses round() which on Python is
        # banker's. Either way, the production path never calls
        # estimate with such tiny amounts.
        assert estimate_upload_cost_bp(0.001) in (0, 1)

    def test_max_affordable_floors_to_whole_gb(self):
        # Above MAM's 50 GB minimum — floors to whole GB normally.
        assert max_affordable_upload_gb(25_000) == 50  # exactly at floor
        assert max_affordable_upload_gb(49_992) == 99  # 49992 // 500 = 99
        assert max_affordable_upload_gb(50_000) == 100

    def test_max_affordable_returns_zero_below_min_buy(self):
        # Below 25,000 BP, the "Max Affordable" button should be
        # unclickable rather than submit a sub-50-GB buy that MAM
        # will reject. max_affordable_upload_gb returning 0 is the
        # signal the router uses to short-circuit with 400.
        assert max_affordable_upload_gb(9_992) == 0
        assert max_affordable_upload_gb(24_999) == 0
        assert max_affordable_upload_gb(499) == 0

    def test_max_affordable_on_zero_or_negative(self):
        assert max_affordable_upload_gb(0) == 0
        assert max_affordable_upload_gb(-100) == 0


# ─── Decision shape contract ────────────────────────────────


class TestDecisionShape:
    def test_frozen_dataclass_cannot_mutate(self):
        d = EconomyDecision(action="skip", reason="disabled")
        with pytest.raises(Exception):
            d.action = "buy"  # type: ignore[misc]

    def test_buy_decision_carries_mode_for_audit(self):
        # The scheduler writes `mode` to the economy_audit table —
        # confirm decide_upload_buy always sets it on `buy` outcomes.
        d = decide_upload_buy(
            _status(ratio=1.0),
            UploadBuyConfig(
                enabled=True, ratio_trigger=True,
                ratio_floor=1.5, ratio_chunk_gb=50,
            ),
            last_bought_at=0, now_ts=1_000_000,
        )
        assert d.mode is not None
        assert d.action == "buy"
