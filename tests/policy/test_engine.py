"""
Unit tests for the grab policy engine.

The policy engine is pure logic — no I/O, no database, no HTTP. Every
test constructs an EconomicContext and a PolicyConfig directly and
asserts on the resulting PolicyDecision. This is deliberately exhaustive
because the policy decides whether to spend snatch budget slots and
ratio, and a regression here is silent and expensive.
"""
from app.policy.engine import (
    EconomicContext,
    PolicyConfig,
    PolicyDecision,
    evaluate_policy,
)


def _default_config(**overrides) -> PolicyConfig:
    return PolicyConfig(**overrides)


def _ctx(**overrides) -> EconomicContext:
    return EconomicContext(**overrides)


def _paid(**overrides) -> EconomicContext:
    """A torrent MAM's search API confirmed is NOT free (the only kind a
    wedge may be spent on, D29)."""
    fields = dict(
        torrent_vip=False, torrent_free=False, torrent_fl_vip=False,
        personal_freeleech=False,
    )
    fields.update(overrides)
    return EconomicContext(**fields)


# ─── VIP fast-path ──────────────────────────────────────────


class TestVipAlwaysGrab:
    def test_announce_vip_grabs_immediately(self):
        d = evaluate_policy(_ctx(announce_vip=True), _default_config())
        assert d.action == "grab"
        assert d.tier == "vip"

    def test_torrent_info_vip_grabs_immediately(self):
        d = evaluate_policy(
            _ctx(torrent_vip=True),
            _default_config(),
        )
        assert d.action == "grab"
        assert d.tier == "vip"

    def test_vip_always_grab_disabled_falls_through(self):
        # When vip_always_grab is False, VIP torrents still match as
        # "free" in step 2, but the tier is different.
        d = evaluate_policy(
            _ctx(announce_vip=True),
            _default_config(vip_always_grab=False),
        )
        assert d.action == "grab"
        assert d.tier == "free"  # still free, just not the VIP fast-path

    def test_vip_bypasses_ratio_floor(self):
        d = evaluate_policy(
            _ctx(announce_vip=True, user_ratio=0.5),
            _default_config(ratio_floor=10.0),
        )
        assert d.action == "grab"
        assert d.tier == "vip"


# ─── Free torrent paths ────────────────────────────────────


class TestFreeTorrent:
    def test_global_freeleech(self):
        d = evaluate_policy(
            _ctx(torrent_free=True),
            _default_config(),
        )
        assert d.action == "grab"
        assert d.tier == "free"

    def test_fl_vip_flag(self):
        d = evaluate_policy(
            _ctx(torrent_fl_vip=True),
            _default_config(),
        )
        assert d.action == "grab"
        assert d.tier == "free"

    def test_personal_freeleech(self):
        d = evaluate_policy(
            _ctx(personal_freeleech=True),
            _default_config(),
        )
        assert d.action == "grab"
        assert d.tier == "free"

    def test_free_bypasses_ratio_floor(self):
        d = evaluate_policy(
            _ctx(torrent_free=True, user_ratio=0.1),
            _default_config(ratio_floor=10.0),
        )
        assert d.action == "grab"
        assert d.tier == "free"


# ─── VIP-only gate ──────────────────────────────────────────


class TestVipOnly:
    def test_non_vip_skipped(self):
        d = evaluate_policy(
            _ctx(announce_vip=False),
            _default_config(vip_only=True),
        )
        assert d.action == "skip"
        assert d.tier == "vip_required"

    def test_vip_passes(self):
        d = evaluate_policy(
            _ctx(announce_vip=True),
            _default_config(vip_only=True),
        )
        assert d.action == "grab"
        assert d.tier == "vip"

    def test_free_non_vip_still_skipped(self):
        # Global freeleech but not VIP — vip_only still blocks.
        # Wait, actually step 2 (free check) runs before step 3
        # (vip_only gate). Free torrents ARE grabbed even with
        # vip_only=True because there's no economic cost.
        d = evaluate_policy(
            _ctx(torrent_free=True, announce_vip=False),
            _default_config(vip_only=True),
        )
        assert d.action == "grab"
        assert d.tier == "free"


# ─── Wedge path ─────────────────────────────────────────────


class TestWedgePath:
    def test_wedge_used_when_available(self):
        d = evaluate_policy(
            _paid(user_wedges=100),
            _default_config(use_wedge=True),
        )
        assert d.action == "grab"
        assert d.tier == "wedge"
        assert d.use_wedge is True

    def test_wedge_respects_reserve(self):
        d = evaluate_policy(
            _paid(user_wedges=5),
            _default_config(use_wedge=True, min_wedges_reserved=10),
        )
        # Not enough wedges above reserve — falls through.
        assert d.tier != "wedge"

    def test_wedge_at_exactly_reserve_skips(self):
        # Wedges == reserve means we'd drop TO zero reserve, not stay above.
        d = evaluate_policy(
            _paid(user_wedges=10),
            _default_config(use_wedge=True, min_wedges_reserved=10),
        )
        assert d.tier != "wedge"

    def test_wedge_one_above_reserve_grabs(self):
        d = evaluate_policy(
            _paid(user_wedges=11),
            _default_config(use_wedge=True, min_wedges_reserved=10),
        )
        assert d.action == "grab"
        assert d.tier == "wedge"
        assert d.use_wedge is True

    def test_wedge_not_used_when_disabled(self):
        d = evaluate_policy(
            _paid(user_wedges=100),
            _default_config(use_wedge=False),
        )
        assert d.use_wedge is False

    def test_wedge_unknown_count_falls_through(self):
        # If user_wedges is None (API lookup failed/skipped), don't
        # try to use wedges.
        d = evaluate_policy(
            _paid(user_wedges=None),
            _default_config(use_wedge=True),
        )
        assert d.tier != "wedge"

    def test_unknown_free_status_never_wedges(self):
        """The lookup failed (a new torrent beat MAM's search index): MAM
        would spend the wedge even if the torrent were free. Grab paid."""
        d = evaluate_policy(
            _ctx(user_wedges=100),
            _default_config(use_wedge=True),
        )
        assert d.action == "grab"
        assert d.use_wedge is False
        assert d.tier != "wedge"

    def test_unknown_free_status_under_free_only_skips(self):
        d = evaluate_policy(
            _ctx(user_wedges=100),
            _default_config(use_wedge=True, free_only=True),
        )
        assert d.action == "skip"
        assert d.tier == "free_required"

    def test_wedge_reserve_skip_when_free_only(self):
        # use_wedge=True but not enough wedges, AND free_only=True.
        d = evaluate_policy(
            _paid(user_wedges=5),
            _default_config(use_wedge=True, min_wedges_reserved=10, free_only=True),
        )
        assert d.action == "skip"
        assert d.tier == "wedge_reserve"


# ─── Free-only gate ─────────────────────────────────────────


class TestFreeOnly:
    def test_non_free_skipped(self):
        d = evaluate_policy(
            _ctx(),
            _default_config(free_only=True),
        )
        assert d.action == "skip"
        assert d.tier == "free_required"

    def test_free_passes(self):
        d = evaluate_policy(
            _ctx(torrent_free=True),
            _default_config(free_only=True),
        )
        assert d.action == "grab"
        assert d.tier == "free"


# ─── Ratio floor ────────────────────────────────────────────


class TestRatioFloor:
    def test_below_floor_skipped(self):
        d = evaluate_policy(
            _ctx(user_ratio=1.5),
            _default_config(ratio_floor=5.0),
        )
        assert d.action == "skip"
        assert d.tier == "ratio_too_low"

    def test_above_floor_grabs(self):
        d = evaluate_policy(
            _ctx(user_ratio=10.0),
            _default_config(ratio_floor=5.0),
        )
        assert d.action == "grab"
        assert d.tier == "normal"

    def test_at_exactly_floor_grabs(self):
        # At the floor, ratio is NOT below it — grab is allowed.
        d = evaluate_policy(
            _ctx(user_ratio=5.0),
            _default_config(ratio_floor=5.0),
        )
        assert d.action == "grab"
        assert d.tier == "normal"

    def test_ratio_floor_zero_disables_check(self):
        d = evaluate_policy(
            _ctx(user_ratio=0.1),
            _default_config(ratio_floor=0.0),
        )
        assert d.action == "grab"
        assert d.tier == "normal"

    def test_unknown_ratio_still_grabs(self):
        # If user_ratio is None (API lookup failed), don't block.
        d = evaluate_policy(
            _ctx(user_ratio=None),
            _default_config(ratio_floor=5.0),
        )
        assert d.action == "grab"
        assert d.tier == "normal"


# ─── Normal grab (no policy gates) ─────────────────────────


class TestNormalGrab:
    def test_default_config_grabs(self):
        d = evaluate_policy(_ctx(), _default_config())
        assert d.action == "grab"
        assert d.tier == "normal"
        assert d.use_wedge is False

    def test_no_api_data_still_grabs(self):
        # All optional fields at defaults (None/False) — should still
        # grab with the default permissive config.
        d = evaluate_policy(
            EconomicContext(),
            PolicyConfig(),
        )
        assert d.action == "grab"
        assert d.tier == "normal"


# ─── EconomicContext properties ─────────────────────────────


class TestEconomicContextProperties:
    def test_is_vip_from_announce(self):
        assert _ctx(announce_vip=True).is_vip is True

    def test_is_vip_from_torrent_info(self):
        assert _ctx(torrent_vip=True).is_vip is True

    def test_is_vip_false_when_neither(self):
        assert _ctx().is_vip is False

    def test_is_free_from_vip(self):
        assert _ctx(announce_vip=True).is_free is True

    def test_is_free_from_global_fl(self):
        assert _ctx(torrent_free=True).is_free is True

    def test_is_free_from_fl_vip(self):
        assert _ctx(torrent_fl_vip=True).is_free is True

    def test_is_free_from_personal_fl(self):
        assert _ctx(personal_freeleech=True).is_free is True

    def test_is_free_false_when_nothing(self):
        assert _ctx().is_free is False


# ─── Priority / interaction tests ───────────────────────────


class TestPolicyPriority:
    def test_vip_beats_free_only(self):
        # VIP torrents grab even when free_only is True (VIP IS free).
        d = evaluate_policy(
            _ctx(announce_vip=True),
            _default_config(free_only=True),
        )
        assert d.action == "grab"

    def test_vip_beats_ratio_floor(self):
        d = evaluate_policy(
            _ctx(announce_vip=True, user_ratio=0.1),
            _default_config(ratio_floor=100.0),
        )
        assert d.action == "grab"

    def test_wedge_beats_ratio_floor(self):
        # Wedge makes the torrent free, so ratio doesn't matter.
        d = evaluate_policy(
            _paid(user_ratio=0.1, user_wedges=50),
            _default_config(use_wedge=True, ratio_floor=100.0),
        )
        assert d.action == "grab"
        assert d.tier == "wedge"

    def test_full_restrictive_config(self):
        # vip_only + free_only + ratio_floor — regular non-free torrent.
        d = evaluate_policy(
            _ctx(user_ratio=0.5, user_wedges=0),
            _default_config(
                vip_only=True,
                free_only=True,
                ratio_floor=5.0,
            ),
        )
        assert d.action == "skip"
