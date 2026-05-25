"""Notification event bus (Bundle B.2).

Single dispatcher behind ``emit(event_type, ...)``. Resolves event
metadata from the registry, gates the send via settings, and pushes
to the underlying ``app/notify/ntfy.py`` HTTP client.

Phase scope:
  - Phase 1: ``emit()`` + ``is_enabled()`` skeleton. Topic + priority +
    tags resolved from registry defaults only.
  - Phase 2: per-event topic routing via ``notifications.events`` exact
    / wildcard / universal rules. Both ``is_enabled`` and ``emit`` walk
    the routing config; legacy settings remain the fallback.
  - Phase 3 (this file's current form): priority overrides resolved
    through the same routing config; quiet-hours window silently drops
    suppressible events when active.
  - Phase 4 migrates the existing call sites onto ``bus.emit()``.

Call sites pass pre-rendered ``title`` + ``message`` strings — the
bus is render-agnostic so legacy helpers can keep their existing
copy verbatim during the migration.

Priority precedence (highest → lowest):
  1. Explicit ``priority=`` kwarg on the ``emit()`` call.
  2. ``notifications.events.<exact / wildcard / *>.priority`` override.
  3. ``EventMeta.default_priority`` from the registry.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.notifications import events, quiet_hours, routing

_log = logging.getLogger("seshat.notifications.bus")


def is_enabled(event_type: str) -> bool:
    """Return ``True`` iff ``event_type`` is currently eligible to send.

    Resolution order:

      1. Unknown event → ``False`` (warning logged).
      2. New shape: if ``notifications.master_enabled`` is explicitly
         ``False``, every event is suppressed regardless of per-event
         configuration.
      3. New shape: ``notifications.events`` is consulted via the
         routing resolver (exact match → longest-prefix wildcard →
         universal ``*``). If any entry supplies ``enabled``, it wins
         over the legacy fallback.
      4. Legacy fallback by ``legacy_setting_key``:
         - Events with ``legacy_requires_master=True`` additionally
           require ``per_event_notifications`` (default ``False``) to be
           on. This preserves the pre-v2.28.0 orchestrator gating model.
         - The per-event legacy key defaults to ``legacy_default_enabled``
           when absent from settings.
      5. Events without any ``legacy_setting_key`` default to enabled
         only when ``notifications.master_enabled`` is on (default ``True``).
    """
    meta = events.get(event_type)
    if meta is None:
        _log.warning("is_enabled() called with unknown event_type=%r", event_type)
        return False

    from app.config import load_settings

    s = load_settings()
    notif_cfg = s.get("notifications") or {}

    # New-shape master kill-switch wins over everything else.
    master_enabled = notif_cfg.get("master_enabled")
    if master_enabled is False:
        return False

    # Walk the routing config (exact / longest-prefix wildcard /
    # universal) for an explicit `enabled` value.
    routed_enabled = routing.resolve_enabled(event_type, s, default=None)
    if routed_enabled is not None:
        return routed_enabled

    # Legacy fallback.
    if meta.legacy_setting_key is None:
        return bool(master_enabled) if master_enabled is not None else True

    if meta.legacy_requires_master:
        if not bool(s.get("per_event_notifications", False)):
            return False
    return bool(s.get(meta.legacy_setting_key, meta.legacy_default_enabled))


async def emit(
    event_type: str,
    *,
    title: str,
    message: str,
    priority: Optional[int] = None,
    tags: Optional[list[str]] = None,
) -> bool:
    """Emit a notification event.

    Args:
        event_type: A registered event name (see
            ``app.notifications.events``). Unknown names log a warning
            and return ``False``.
        title: Pre-rendered notification title.
        message: Pre-rendered notification body.
        priority: Optional override for the event's default priority
            (1-5). ``None`` uses the registry default.
        tags: Optional override for the event's default tags. ``None``
            uses the registry default.

    Returns:
        ``True`` on a successful send, ``False`` otherwise (disabled,
        ntfy not configured, transient HTTP failure, or unknown
        event_type). Errors are logged but never raised — every call
        site is non-fatal.
    """
    meta = events.get(event_type)
    if meta is None:
        _log.warning("emit() ignored: unknown event_type=%r", event_type)
        return False

    if not is_enabled(event_type):
        return False

    from app.config import load_settings
    from app.notify import ntfy

    s = load_settings()

    # Quiet-hours gate. Suppressible events are silently dropped when
    # the window is active; errors + warnings pass through regardless.
    if meta.suppressible_during_quiet_hours and quiet_hours.is_in_quiet_hours(s):
        _log.debug(
            "emit(%s) suppressed by quiet hours", event_type,
        )
        return False

    url, topic = routing.resolve_url_and_topic(event_type, s)

    if priority is None:
        routed_priority = routing.resolve_event_field(
            event_type, s, "priority", default=None,
        )
        if routed_priority is not None:
            try:
                priority = int(routed_priority)
            except (TypeError, ValueError):
                _log.warning(
                    "ignoring non-int priority override for %s: %r",
                    event_type, routed_priority,
                )
                priority = None
    if priority is None:
        priority = meta.default_priority

    return await ntfy.send(
        url=url,
        topic=topic,
        title=title,
        message=message,
        priority=priority,
        tags=tags if tags is not None else list(meta.default_tags),
    )
