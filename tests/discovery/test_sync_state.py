"""Tests for `app.discovery.sync_state`."""
from __future__ import annotations

import pytest

from app.discovery import sync_state


class TestMigrateSettings:
    def test_folds_legacy_entries(self):
        settings = {"library_mtimes": {"calibre": 1234.5, "abs": "999|12"}}
        changed = sync_state.migrate_settings(settings)
        assert changed is True
        store = settings["library_sync_state"]
        assert store["calibre"] == {
            "last_mtime": 1234.5,
            "last_sync_ts": 0.0,
            "last_full_sync_ts": 0.0,
        }
        assert store["abs"]["last_mtime"] == "999|12"

    def test_no_legacy_no_change(self):
        settings = {"library_mtimes": {}}
        changed = sync_state.migrate_settings(settings)
        assert changed is False
        assert settings.get("library_sync_state", {}) == {}

    def test_idempotent_when_slug_already_migrated(self):
        settings = {
            "library_mtimes": {"calibre": 9999.0},
            "library_sync_state": {
                "calibre": {
                    "last_mtime": 9999.0,
                    "last_sync_ts": 5000.0,
                    "last_full_sync_ts": 5000.0,
                }
            },
        }
        changed = sync_state.migrate_settings(settings)
        assert changed is False
        # Existing rich entry preserved — we never zero out real timestamps.
        assert settings["library_sync_state"]["calibre"]["last_sync_ts"] == 5000.0

    def test_partial_migration(self):
        settings = {
            "library_mtimes": {"calibre": 1.0, "abs": 2.0},
            "library_sync_state": {
                "calibre": {
                    "last_mtime": 1.0,
                    "last_sync_ts": 100.0,
                    "last_full_sync_ts": 100.0,
                }
            },
        }
        changed = sync_state.migrate_settings(settings)
        assert changed is True
        # Calibre untouched, abs newly migrated.
        assert settings["library_sync_state"]["calibre"]["last_sync_ts"] == 100.0
        assert settings["library_sync_state"]["abs"]["last_sync_ts"] == 0.0


class TestGetState:
    def test_missing_slug_returns_defaults(self):
        state = sync_state.get_state({}, "absent")
        assert state == {
            "last_mtime": None,
            "last_sync_ts": 0.0,
            "last_full_sync_ts": 0.0,
        }

    def test_populated_entry(self):
        settings = {
            "library_sync_state": {
                "calibre": {
                    "last_mtime": 42.0,
                    "last_sync_ts": 1000.0,
                    "last_full_sync_ts": 500.0,
                }
            }
        }
        state = sync_state.get_state(settings, "calibre")
        assert state == {
            "last_mtime": 42.0,
            "last_sync_ts": 1000.0,
            "last_full_sync_ts": 500.0,
        }


class TestRecordCompletion:
    def test_full_mode_bumps_both_timestamps(self, monkeypatch):
        monkeypatch.setattr(sync_state.time, "time", lambda: 2000.0)
        settings: dict = {}
        sync_state.record_completion(settings, "calibre", mtime=123.0, mode="full")
        entry = settings["library_sync_state"]["calibre"]
        assert entry["last_mtime"] == 123.0
        assert entry["last_sync_ts"] == 2000.0
        assert entry["last_full_sync_ts"] == 2000.0
        # Legacy mirror updated for downgrade-safety.
        assert settings["library_mtimes"]["calibre"] == 123.0

    def test_incremental_mode_leaves_full_timestamp(self, monkeypatch):
        monkeypatch.setattr(sync_state.time, "time", lambda: 3000.0)
        settings = {
            "library_sync_state": {
                "calibre": {
                    "last_mtime": 100.0,
                    "last_sync_ts": 1000.0,
                    "last_full_sync_ts": 500.0,
                }
            }
        }
        sync_state.record_completion(settings, "calibre", mtime=200.0, mode="incremental")
        entry = settings["library_sync_state"]["calibre"]
        assert entry["last_mtime"] == 200.0
        assert entry["last_sync_ts"] == 3000.0
        assert entry["last_full_sync_ts"] == 500.0  # unchanged

    def test_rejects_unknown_mode(self):
        with pytest.raises(ValueError):
            sync_state.record_completion({}, "calibre", mtime=1.0, mode="bogus")


class TestRecordFailure:
    def test_resets_only_last_sync_ts(self):
        settings = {
            "library_sync_state": {
                "calibre": {
                    "last_mtime": 100.0,
                    "last_sync_ts": 1000.0,
                    "last_full_sync_ts": 500.0,
                }
            }
        }
        sync_state.record_failure(settings, "calibre")
        entry = settings["library_sync_state"]["calibre"]
        assert entry["last_sync_ts"] == 0.0
        assert entry["last_mtime"] == 100.0
        assert entry["last_full_sync_ts"] == 500.0


class TestResolveThreshold:
    def test_first_sync_returns_none(self):
        state = {"last_mtime": None, "last_sync_ts": 0.0, "last_full_sync_ts": 0.0}
        threshold, reason = sync_state.resolve_threshold(state, now=10_000.0)
        assert threshold is None
        assert reason == sync_state.MODE_FULL_FIRST

    def test_weekly_safety_net_returns_none(self):
        # last_full was 8 days ago.
        state = {
            "last_mtime": 1.0,
            "last_sync_ts": 9_000.0,
            "last_full_sync_ts": 10_000.0 - 8 * 86400,
        }
        threshold, reason = sync_state.resolve_threshold(state, now=10_000.0)
        assert threshold is None
        assert reason == sync_state.MODE_FULL_WEEKLY

    def test_recovery_after_failure(self):
        # last_full fresh, but last_sync was zeroed by record_failure().
        state = {
            "last_mtime": 1.0,
            "last_sync_ts": 0.0,
            "last_full_sync_ts": 10_000.0 - 3600,
        }
        threshold, reason = sync_state.resolve_threshold(state, now=10_000.0)
        assert threshold is None
        assert reason == sync_state.MODE_FULL_RECOVERY

    def test_incremental_applies_drift_bias(self):
        state = {
            "last_mtime": 1.0,
            "last_sync_ts": 9_000.0,
            "last_full_sync_ts": 5_000.0,
        }
        threshold, reason = sync_state.resolve_threshold(state, now=10_000.0)
        assert reason == sync_state.MODE_INCREMENTAL
        assert threshold == 9_000.0 - sync_state.DRIFT_BIAS_SECONDS

    def test_custom_drift_bias(self):
        state = {
            "last_mtime": 1.0,
            "last_sync_ts": 9_000.0,
            "last_full_sync_ts": 5_000.0,
        }
        threshold, reason = sync_state.resolve_threshold(
            state, now=10_000.0, drift_bias_seconds=300.0
        )
        assert threshold == 8_700.0
        assert reason == sync_state.MODE_INCREMENTAL

    def test_weekly_threshold_boundary(self):
        # Exactly 7 days → triggers weekly safety net.
        state = {
            "last_mtime": 1.0,
            "last_sync_ts": 9_000.0,
            "last_full_sync_ts": 10_000.0 - 7 * 86400,
        }
        _, reason = sync_state.resolve_threshold(state, now=10_000.0)
        assert reason == sync_state.MODE_FULL_WEEKLY

    def test_weekly_threshold_just_under(self):
        # 1 second under 7 days → still incremental.
        state = {
            "last_mtime": 1.0,
            "last_sync_ts": 9_000.0,
            "last_full_sync_ts": 10_000.0 - (7 * 86400 - 1),
        }
        _, reason = sync_state.resolve_threshold(state, now=10_000.0)
        assert reason == sync_state.MODE_INCREMENTAL
