"""Unit tests for the quiet-hours window check."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.notifications import quiet_hours


def _at(hh: int, mm: int, tz: str = "UTC") -> datetime:
    """Construct a tz-aware datetime at HH:MM on 2026-01-01."""
    return datetime(2026, 1, 1, hh, mm, tzinfo=ZoneInfo(tz))


def _qh(**kw):
    """Wrap a quiet_hours block in the full settings shape."""
    qh = {"enabled": True, "timezone": "UTC", **kw}
    return {"notifications": {"quiet_hours": qh}}


class TestSameDayWindow:
    """e.g. 13:00 → 17:00 — start < end."""

    def test_before_start(self):
        s = _qh(start="13:00", end="17:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(12, 59)) is False

    def test_exactly_at_start(self):
        s = _qh(start="13:00", end="17:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(13, 0)) is True

    def test_middle_of_window(self):
        s = _qh(start="13:00", end="17:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(15, 30)) is True

    def test_exactly_at_end(self):
        """End is exclusive — at exactly the end time, quiet hours
        are over."""
        s = _qh(start="13:00", end="17:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(17, 0)) is False

    def test_after_end(self):
        s = _qh(start="13:00", end="17:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(17, 1)) is False


class TestOvernightWindow:
    """e.g. 23:00 → 07:00 — start > end, window wraps midnight."""

    def test_before_start_in_evening(self):
        s = _qh(start="23:00", end="07:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(22, 59)) is False

    def test_exactly_at_start(self):
        s = _qh(start="23:00", end="07:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(23, 0)) is True

    def test_late_evening(self):
        s = _qh(start="23:00", end="07:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(23, 59)) is True

    def test_after_midnight(self):
        s = _qh(start="23:00", end="07:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(2, 30)) is True

    def test_exactly_at_end(self):
        s = _qh(start="23:00", end="07:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(7, 0)) is False

    def test_morning_after_window(self):
        s = _qh(start="23:00", end="07:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(8, 0)) is False


class TestEdgeCases:
    def test_disabled(self):
        s = _qh(enabled=False, start="00:00", end="23:59")
        assert quiet_hours.is_in_quiet_hours(s, _at(12, 0)) is False

    def test_missing_quiet_hours_section(self):
        assert quiet_hours.is_in_quiet_hours({}, _at(12, 0)) is False
        assert quiet_hours.is_in_quiet_hours({"notifications": {}}, _at(12, 0)) is False

    def test_zero_length_window_never_active(self):
        """``start == end`` is a misconfiguration — interpret as 'off'
        rather than 'always on'. Better to ship notifications the user
        didn't expect to silence than to silence them all."""
        s = _qh(start="12:00", end="12:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(12, 0)) is False
        assert quiet_hours.is_in_quiet_hours(s, _at(0, 0)) is False
        assert quiet_hours.is_in_quiet_hours(s, _at(23, 59)) is False

    def test_malformed_start_falls_through(self):
        s = _qh(start="not a time", end="07:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(3, 0)) is False

    def test_malformed_end_falls_through(self):
        s = _qh(start="23:00", end="garbage")
        assert quiet_hours.is_in_quiet_hours(s, _at(3, 0)) is False

    def test_missing_start_falls_through(self):
        s = {"notifications": {"quiet_hours": {"enabled": True, "end": "07:00"}}}
        assert quiet_hours.is_in_quiet_hours(s, _at(3, 0)) is False

    def test_invalid_hour_value(self):
        s = _qh(start="25:00", end="07:00")
        assert quiet_hours.is_in_quiet_hours(s, _at(3, 0)) is False

    def test_invalid_timezone_falls_back_to_local(self):
        """Bad timezone shouldn't crash — bus must never crash a
        producing call site."""
        s = _qh(start="23:00", end="07:00", timezone="Mars/Olympus")
        # With explicit `now`, the tz fallback path is exercised but
        # the test stays deterministic.
        assert quiet_hours.is_in_quiet_hours(s, _at(2, 0)) is True


class TestTimezoneAware:
    """Quiet hours should evaluate the user's wall-clock, not UTC."""

    def test_ny_quiet_hours_matches_ny_evening(self):
        # 23:30 in NY = 04:30 UTC next day. The settings says 23:00 →
        # 07:00 in NY, so a "now" of 23:30 NY should be inside.
        s = _qh(start="23:00", end="07:00", timezone="America/New_York")
        now_ny = datetime(2026, 1, 1, 23, 30, tzinfo=ZoneInfo("America/New_York"))
        assert quiet_hours.is_in_quiet_hours(s, now_ny) is True

    def test_ny_quiet_hours_does_not_match_ny_afternoon(self):
        s = _qh(start="23:00", end="07:00", timezone="America/New_York")
        now_ny = datetime(2026, 1, 1, 15, 0, tzinfo=ZoneInfo("America/New_York"))
        assert quiet_hours.is_in_quiet_hours(s, now_ny) is False
