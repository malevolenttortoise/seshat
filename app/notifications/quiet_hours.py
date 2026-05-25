"""Quiet-hours window check (Bundle B.2 — Phase 3 verbosity).

Settings shape::

    {
      "notifications": {
        "quiet_hours": {
          "enabled": false,
          "start": "23:00",
          "end":   "07:00",
          "timezone": "America/New_York"
        }
      }
    }

When the window is active and the event is marked
``suppressible_during_quiet_hours`` in the registry, ``bus.emit`` drops
the send. Errors and other ``suppressible_during_quiet_hours=False``
events fire through regardless — quiet hours mute routine successes,
not failures.

Overnight windows (``start > end``, e.g. 23:00 → 07:00) are supported:
the window is treated as `[start, 24:00) ∪ [00:00, end)`. ``start ==
end`` is treated as a zero-length window (never active). Malformed
config (invalid HH:MM, unknown timezone) silently degrades to "quiet
hours off" rather than erroring — notification logic must never crash
a producing call site.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_log = logging.getLogger("seshat.notifications.quiet_hours")


def is_in_quiet_hours(
    settings: dict,
    now: Optional[datetime] = None,
) -> bool:
    """Return ``True`` iff quiet hours are configured and ``now``
    falls inside the window.

    Args:
        settings: Full settings dict; reads
            ``notifications.quiet_hours.{enabled,start,end,timezone}``.
        now: Optional current-time override (primarily for tests).
            When ``None``, the function reads the current wall-clock in
            the configured timezone (or system local time if no zone is
            set).
    """
    qh = (settings.get("notifications") or {}).get("quiet_hours") or {}
    if not isinstance(qh, dict) or not qh.get("enabled"):
        return False

    start = _parse_hhmm(qh.get("start"))
    end = _parse_hhmm(qh.get("end"))
    if start is None or end is None:
        return False
    if start == end:
        # Zero-length window — treat as "always off" rather than
        # "always on" so a misconfiguration doesn't silently mute the
        # entire app forever.
        return False

    tz = _resolve_tz(qh.get("timezone"))
    if now is None:
        now = datetime.now(tz=tz)
    current = now.time()

    if start < end:
        # Same-day window (e.g. 13:00 → 17:00).
        return start <= current < end

    # Overnight window (e.g. 23:00 → 07:00) — two disjoint intervals.
    return current >= start or current < end


def _parse_hhmm(value: object) -> Optional[time]:
    """Parse a ``"HH:MM"`` string into a ``time``. ``None`` if
    malformed."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if ":" not in s:
        return None
    try:
        hh, mm = s.split(":", 1)
        return time(int(hh), int(mm))
    except (TypeError, ValueError):
        return None


def _resolve_tz(name: object) -> Optional[tzinfo]:
    """Resolve a timezone name (IANA) to a ``tzinfo``. ``None`` =
    fall back to system local time, which is what
    ``datetime.now(tz=None)`` gives us."""
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        _log.warning(
            "quiet_hours timezone %r is invalid — using system local time",
            name,
        )
        return None
