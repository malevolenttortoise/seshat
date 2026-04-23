"""
Unit tests for the MAM economy auto-buy scheduler.

Tests target `vip_tick()` and `upload_tick()` directly — the
surrounding `_run_loop` is a thin wake-interval wrapper modeled on
`cookie_keepalive.run_loop`, already covered by `test_cookie_keepalive`.

Each test composes three fixtures:
  - `temp_db` — fresh SQLite with the economy_audit table
  - `isolated_settings` — isolates settings.json under tmp_path
  - `fake_mam` — programmable httpx transport swapped into the shared
    MAM client so bonus_buy + user_status hit a canned server

Token resolution is monkeypatched per-test (instead of going through
the real `_get_mam_token`) so the tests don't need to touch the
encrypted secrets store.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import config
from app.database import get_db
from app.mam.user_status import invalidate_cache
from app.orchestrator import economy_scheduler
from app.storage import economy_audit


# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Monkeypatch SETTINGS_PATH to a tmp file and clear the settings cache.

    Each test gets a clean slate — no DEFAULT_SETTINGS seeding has
    happened yet, so tests must call `_write_settings()` with the
    keys they want. The cache is reset both before and after so
    leftover state from a neighboring test never leaks.
    """
    p = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", p)
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = object()
    yield p
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = object()


def _write_settings(path: Path, overrides: dict) -> None:
    """Write a settings.json merged over DEFAULT_SETTINGS.

    Mirrors the layering `load_settings()` does at read time, so tests
    can pass only the keys they care about.
    """
    merged = {**config.DEFAULT_SETTINGS, **overrides}
    path.write_text(json.dumps(merged))
    config._settings_cache["data"] = None


@pytest.fixture(autouse=True)
def _clear_user_status_cache():
    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture
def stub_token(monkeypatch):
    """Replace `_resolve_mam_token` with a one-shot stub."""
    def _install(token: str) -> None:
        async def _return_token() -> str:
            return token
        monkeypatch.setattr(
            economy_scheduler, "_resolve_mam_token", _return_token
        )
    return _install


async def _audit_rows():
    db = await get_db()
    try:
        return await economy_audit.list_recent(db)
    finally:
        await db.close()


# ─── Config builders ────────────────────────────────────────


class TestConfigBuilders:
    def test_vip_config_defaults(self, isolated_settings):
        _write_settings(isolated_settings, {})
        cfg = economy_scheduler.build_vip_config(config.load_settings())
        assert cfg.enabled is False
        assert cfg.interval_hours == 24
        assert cfg.min_bonus == 0
        assert cfg.weeks == 4

    def test_vip_config_reads_overrides(self, isolated_settings):
        _write_settings(isolated_settings, {
            "mam_economy_vip_enabled": True,
            "mam_economy_vip_interval_hours": 12,
            "mam_economy_vip_min_bonus": 5000,
            "mam_economy_vip_weeks": "max",
        })
        cfg = economy_scheduler.build_vip_config(config.load_settings())
        assert cfg.enabled is True
        assert cfg.interval_hours == 12
        assert cfg.min_bonus == 5000
        assert cfg.weeks == "max"

    def test_upload_config_reads_three_triggers(self, isolated_settings):
        _write_settings(isolated_settings, {
            "mam_economy_upload_enabled": True,
            "mam_economy_upload_ratio_trigger": True,
            "mam_economy_upload_ratio_floor": 2.0,
            "mam_economy_upload_buffer_trigger": True,
            "mam_economy_upload_buffer_floor_gb": 25,
            "mam_economy_upload_bonus_trigger": True,
            "mam_economy_upload_bonus_ceiling": 10_000,
        })
        cfg = economy_scheduler.build_upload_config(config.load_settings())
        assert cfg.enabled is True
        assert cfg.ratio_trigger is True
        assert cfg.ratio_floor == 2.0
        assert cfg.buffer_trigger is True
        assert cfg.buffer_floor_gb == 25
        assert cfg.bonus_trigger is True
        assert cfg.bonus_ceiling == 10_000


# ─── vip_tick short-circuits ────────────────────────────────


class TestVipShortCircuits:
    async def test_disabled_returns_none_no_audit(
        self, temp_db, isolated_settings, fake_mam, stub_token
    ):
        _write_settings(isolated_settings, {"mam_economy_vip_enabled": False})
        stub_token("tok")

        result = await economy_scheduler.vip_tick()
        assert result is None
        assert await _audit_rows() == []
        # No MAM calls either — nothing should have hit the fake.
        assert fake_mam.requests == []

    async def test_below_interval_returns_none_no_audit(
        self, temp_db, isolated_settings, fake_mam, stub_token
    ):
        import time
        _write_settings(isolated_settings, {
            "mam_economy_vip_enabled": True,
            "mam_economy_vip_interval_hours": 24,
            "mam_economy_last_vip_buy_at": time.time() - 60,  # 1 min ago
        })
        stub_token("tok")

        result = await economy_scheduler.vip_tick()
        assert result is None
        assert await _audit_rows() == []
        assert fake_mam.requests == []

    async def test_no_token_returns_none_no_audit(
        self, temp_db, isolated_settings, fake_mam, stub_token
    ):
        _write_settings(isolated_settings, {"mam_economy_vip_enabled": True})
        stub_token("")  # empty token

        result = await economy_scheduler.vip_tick()
        assert result is None
        assert await _audit_rows() == []


# ─── vip_tick happy paths + failures ────────────────────────


class TestVipTickBuy:
    async def test_happy_path_writes_success_and_bumps_timestamp(
        self, temp_db, isolated_settings, fake_mam, stub_token
    ):
        _write_settings(isolated_settings, {
            "mam_economy_vip_enabled": True,
            "mam_economy_vip_interval_hours": 24,
            "mam_economy_vip_weeks": 4,
            "mam_economy_last_vip_buy_at": 0,
        })
        stub_token("tok")
        # Default fake_mam.user_status has seedbonus=71088 and
        # default fake_mam.bonus_buy returns success with new
        # seedbonus=26512.091 — cost ≈ 44575.9
        outcome = await economy_scheduler.vip_tick()
        assert outcome == economy_audit.OUTCOME_SUCCESS

        rows = await _audit_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row.action == economy_audit.ACTION_VIP
        assert row.trigger == economy_audit.TRIGGER_SCHEDULED
        assert row.outcome == economy_audit.OUTCOME_SUCCESS
        assert row.tier == "trigger:interval"
        assert row.amount == "4"
        assert row.user_bonus_after == pytest.approx(26512.091)
        assert row.cost_points == pytest.approx(71088 - 26512.091)

        # Timestamp bumped so next tick short-circuits.
        settings = config.load_settings()
        assert settings["mam_economy_last_vip_buy_at"] > 0

    async def test_mam_rejects_writes_failure_no_timestamp_bump(
        self, temp_db, isolated_settings, fake_mam, stub_token
    ):
        _write_settings(isolated_settings, {
            "mam_economy_vip_enabled": True,
            "mam_economy_last_vip_buy_at": 0,
        })
        stub_token("tok")
        fake_mam.bonus_buy.body = (
            b'{"success":false,"error":"Not enough bonus, s1"}'
        )

        outcome = await economy_scheduler.vip_tick()
        assert outcome == economy_audit.OUTCOME_FAILURE

        rows = await _audit_rows()
        assert len(rows) == 1
        assert rows[0].outcome == economy_audit.OUTCOME_FAILURE
        assert "Not enough bonus" in (rows[0].message or "")

        settings = config.load_settings()
        # Failures do NOT advance the shared timestamp — we want the
        # next tick to retry immediately once the user's BP recovers.
        assert settings["mam_economy_last_vip_buy_at"] == 0

    async def test_insufficient_bonus_skip_writes_audit(
        self, temp_db, isolated_settings, fake_mam, stub_token
    ):
        _write_settings(isolated_settings, {
            "mam_economy_vip_enabled": True,
            "mam_economy_vip_weeks": 4,
            "mam_economy_last_vip_buy_at": 0,
        })
        stub_token("tok")
        # Drop user_status seedbonus below 4 × BP_PER_VIP_WEEK so the
        # decision engine short-circuits to insufficient_bonus.
        fake_mam.user_status.body = (
            b'{"seedbonus":100,"wedges":1,"ratio":2.0,'
            b'"username":"t","uid":1,"uploaded_bytes":0,'
            b'"downloaded_bytes":0}'
        )

        outcome = await economy_scheduler.vip_tick()
        assert outcome == "skip_insufficient_bonus"

        rows = await _audit_rows()
        assert len(rows) == 1
        assert rows[0].outcome == "skip_insufficient_bonus"
        # No bonus_buy request — we short-circuited at the decision layer.
        assert not any(
            "bonusBuy.php" in str(r.url) for r in fake_mam.requests
        )

    async def test_user_status_fetch_failure_writes_audit(
        self, temp_db, isolated_settings, fake_mam, stub_token
    ):
        _write_settings(isolated_settings, {
            "mam_economy_vip_enabled": True,
        })
        stub_token("tok")
        fake_mam.user_status.status = 503
        fake_mam.user_status.body = b"bad gateway"

        outcome = await economy_scheduler.vip_tick()
        assert outcome == economy_audit.OUTCOME_FAILURE

        rows = await _audit_rows()
        assert len(rows) == 1
        assert "user_status fetch failed" in (rows[0].message or "")


# ─── upload_tick ────────────────────────────────────────────


class TestUploadTickBuy:
    async def test_ratio_trigger_happy_path(
        self, temp_db, isolated_settings, fake_mam, stub_token
    ):
        _write_settings(isolated_settings, {
            "mam_economy_upload_enabled": True,
            "mam_economy_upload_ratio_trigger": True,
            "mam_economy_upload_ratio_floor": 10.0,  # current fake ratio is 91184 — never below this
        })
        # Drop ratio below floor so the trigger fires.
        fake_mam.user_status.body = (
            b'{"seedbonus":200000,"wedges":1,"ratio":1.0,'
            b'"username":"t","uid":1,"uploaded_bytes":0,'
            b'"downloaded_bytes":0}'
        )
        stub_token("tok")

        outcome = await economy_scheduler.upload_tick()
        assert outcome == economy_audit.OUTCOME_SUCCESS

        rows = await _audit_rows()
        assert len(rows) == 1
        assert rows[0].action == economy_audit.ACTION_UPLOAD
        assert rows[0].mode == "ratio"
        assert rows[0].tier == "trigger:ratio"
        assert rows[0].amount == "50"  # default ratio_chunk_gb

    async def test_no_trigger_writes_skip(
        self, temp_db, isolated_settings, fake_mam, stub_token
    ):
        _write_settings(isolated_settings, {
            "mam_economy_upload_enabled": True,
            "mam_economy_upload_ratio_trigger": True,
            "mam_economy_upload_ratio_floor": 0.5,  # default ratio 91184 never below this
        })
        stub_token("tok")

        outcome = await economy_scheduler.upload_tick()
        assert outcome == "skip_no_trigger"

        rows = await _audit_rows()
        assert len(rows) == 1
        assert rows[0].outcome == "skip_no_trigger"
        # No buy request was made.
        assert not any(
            "bonusBuy.php" in str(r.url) for r in fake_mam.requests
        )

    async def test_disabled_returns_none_no_audit(
        self, temp_db, isolated_settings, fake_mam, stub_token
    ):
        _write_settings(isolated_settings, {"mam_economy_upload_enabled": False})
        stub_token("tok")

        result = await economy_scheduler.upload_tick()
        assert result is None
        assert await _audit_rows() == []

    async def test_bonus_trigger_with_fractional_gb(
        self, temp_db, isolated_settings, fake_mam, stub_token
    ):
        _write_settings(isolated_settings, {
            "mam_economy_upload_enabled": True,
            "mam_economy_upload_bonus_trigger": True,
            "mam_economy_upload_bonus_ceiling": 5000,
        })
        # seedbonus 40250 → excess 35250 / 500 = 70.5 GB — above
        # MAM's 50 GB programmatic floor. Fractional amounts must
        # format with two decimals in the audit row so the history
        # tile renders "70.50" not "70.5".
        fake_mam.user_status.body = (
            b'{"seedbonus":40250,"wedges":1,"ratio":2.0,'
            b'"username":"t","uid":1,"uploaded_bytes":0,'
            b'"downloaded_bytes":0}'
        )
        stub_token("tok")

        outcome = await economy_scheduler.upload_tick()
        assert outcome == economy_audit.OUTCOME_SUCCESS

        rows = await _audit_rows()
        assert rows[0].amount == "70.50"  # formatted with two decimals
        assert rows[0].mode == "bonus"


# ─── Amount formatter ──────────────────────────────────────


class TestAmountFormatter:
    def test_whole_gb_no_decimal(self):
        assert economy_scheduler._format_gb(50.0) == "50"
        assert economy_scheduler._format_gb(1.0) == "1"

    def test_fractional_gb_two_decimals(self):
        assert economy_scheduler._format_gb(2.5) == "2.50"
        assert economy_scheduler._format_gb(10.2) == "10.20"

    def test_none_stays_none(self):
        assert economy_scheduler._format_gb(None) is None
