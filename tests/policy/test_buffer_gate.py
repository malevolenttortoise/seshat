"""
Unit tests for the buffer gate in the grab policy engine.

The gate is step 7 of `evaluate_policy` — it runs after VIP/free/wedge
paths (which return early with their own tiers) and after the ratio
floor, but before the "normal" grab. These tests exhaustively cover
the gate's firing conditions, fail-open behavior, and interaction
with the higher-priority steps.
"""
from app.policy.engine import (
    EconomicContext,
    PolicyConfig,
    evaluate_policy,
)


GB = 1_000_000_000


def _ctx(**overrides) -> EconomicContext:
    return EconomicContext(**overrides)


def _cfg(**overrides) -> PolicyConfig:
    # Gate-on by default for brevity — individual tests that need it
    # off pass `buffer_gate_enabled=False`, which `overrides` wins.
    defaults = {"buffer_gate_enabled": True}
    return PolicyConfig(**{**defaults, **overrides})


# ─── Firing conditions ─────────────────────────────────────


class TestBufferInsufficient:
    def test_size_exceeds_buffer_skips(self):
        d = evaluate_policy(
            _ctx(torrent_size_bytes=50 * GB, user_upload_buffer_bytes=10 * GB),
            _cfg(),
        )
        assert d.action == "skip"
        assert d.tier == "buffer_insufficient"

    def test_size_equals_buffer_skips(self):
        # size + 0 margin > buffer is false when equal, but the
        # policy says `> buffer`, so equal should NOT skip.
        d = evaluate_policy(
            _ctx(torrent_size_bytes=10 * GB, user_upload_buffer_bytes=10 * GB),
            _cfg(),
        )
        assert d.action == "grab"
        assert d.tier == "normal"

    def test_size_plus_margin_exceeds_buffer_skips(self):
        # 9 GB torrent + 2 GB margin = 11 GB, over a 10 GB buffer.
        d = evaluate_policy(
            _ctx(torrent_size_bytes=9 * GB, user_upload_buffer_bytes=10 * GB),
            _cfg(buffer_safety_margin_bytes=2 * GB),
        )
        assert d.action == "skip"
        assert d.tier == "buffer_insufficient"

    def test_size_within_buffer_minus_margin_grabs(self):
        d = evaluate_policy(
            _ctx(torrent_size_bytes=5 * GB, user_upload_buffer_bytes=20 * GB),
            _cfg(buffer_safety_margin_bytes=1 * GB),
        )
        assert d.action == "grab"
        assert d.tier == "normal"


# ─── Higher-priority gates bypass the buffer check ─────────


class TestHigherPriorityBypass:
    def test_vip_torrent_bypasses_gate(self):
        # VIP is step 1 — returns before buffer gate is considered.
        d = evaluate_policy(
            _ctx(announce_vip=True,
                 torrent_size_bytes=100 * GB,
                 user_upload_buffer_bytes=1 * GB),
            _cfg(),
        )
        assert d.action == "grab"
        assert d.tier == "vip"

    def test_free_torrent_bypasses_gate(self):
        d = evaluate_policy(
            _ctx(torrent_free=True,
                 torrent_size_bytes=100 * GB,
                 user_upload_buffer_bytes=1 * GB),
            _cfg(),
        )
        assert d.action == "grab"
        assert d.tier == "free"

    def test_personal_freeleech_bypasses_gate(self):
        d = evaluate_policy(
            _ctx(personal_freeleech=True,
                 torrent_size_bytes=100 * GB,
                 user_upload_buffer_bytes=1 * GB),
            _cfg(),
        )
        assert d.action == "grab"
        assert d.tier == "free"

    def test_wedge_path_bypasses_gate(self):
        # Wedge makes the torrent free, so buffer gate doesn't apply.
        d = evaluate_policy(
            # Known not free (a wedge is only spent on those, D29).
            _ctx(torrent_size_bytes=100 * GB,
                 user_upload_buffer_bytes=1 * GB,
                 user_wedges=10,
                 torrent_vip=False, torrent_free=False),
            _cfg(use_wedge=True, min_wedges_reserved=0),
        )
        assert d.action == "grab"
        assert d.tier == "wedge"

    def test_ratio_floor_fires_before_buffer_gate(self):
        # Both ratio too low AND buffer insufficient — ratio wins
        # since it's cheaper to evaluate and comes first in the matrix.
        d = evaluate_policy(
            _ctx(torrent_size_bytes=100 * GB,
                 user_upload_buffer_bytes=1 * GB,
                 user_ratio=0.5),
            _cfg(ratio_floor=1.0),
        )
        assert d.action == "skip"
        assert d.tier == "ratio_too_low"


# ─── Fail-open on missing data ─────────────────────────────


class TestFailOpen:
    def test_missing_torrent_size_falls_through(self):
        d = evaluate_policy(
            _ctx(torrent_size_bytes=None, user_upload_buffer_bytes=1 * GB),
            _cfg(),
        )
        assert d.action == "grab"
        assert d.tier == "normal"

    def test_missing_buffer_falls_through(self):
        d = evaluate_policy(
            _ctx(torrent_size_bytes=100 * GB, user_upload_buffer_bytes=None),
            _cfg(),
        )
        assert d.action == "grab"
        assert d.tier == "normal"

    def test_both_missing_falls_through(self):
        d = evaluate_policy(
            _ctx(torrent_size_bytes=None, user_upload_buffer_bytes=None),
            _cfg(),
        )
        assert d.action == "grab"


# ─── Gate disabled ─────────────────────────────────────────


class TestGateDisabled:
    def test_disabled_ignores_size_and_buffer(self):
        # Enormous torrent vs tiny buffer — gate OFF means grab proceeds.
        d = evaluate_policy(
            _ctx(torrent_size_bytes=1000 * GB, user_upload_buffer_bytes=1 * GB),
            _cfg(buffer_gate_enabled=False),
        )
        assert d.action == "grab"
        assert d.tier == "normal"


# ─── Edge cases on the safety margin ───────────────────────


class TestSafetyMargin:
    def test_zero_margin_compares_strictly_greater(self):
        # With margin 0, size == buffer means "not greater", allowed.
        d = evaluate_policy(
            _ctx(torrent_size_bytes=10 * GB, user_upload_buffer_bytes=10 * GB),
            _cfg(buffer_safety_margin_bytes=0),
        )
        assert d.action == "grab"

    def test_large_margin_reserves_headroom(self):
        # 5 GB torrent + 10 GB margin = 15 GB, over a 10 GB buffer.
        d = evaluate_policy(
            _ctx(torrent_size_bytes=5 * GB, user_upload_buffer_bytes=10 * GB),
            _cfg(buffer_safety_margin_bytes=10 * GB),
        )
        assert d.action == "skip"
        assert d.tier == "buffer_insufficient"
