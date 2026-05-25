"""Seshat notifications package (Bundle B.2, v2.28.0).

Centralizes notification dispatch behind an event bus:

    from app.notifications import bus, events

    await bus.emit(events.GRAB_SUCCESS, title="...", message="...")

See ``events.py`` for the event taxonomy and ``bus.py`` for the
dispatcher. Phase 1 establishes the registry + skeleton; per-event
topic routing arrives in Phase 2, quiet hours + priority overrides in
Phase 3, and full call-site migration in Phase 4.

While migration is in flight, the legacy ``app/notify/ntfy.py`` +
``app/discovery/notify.py`` modules remain operational. The bus reads
the same ``notify_on_*`` / ``ntfy_on_*`` settings keys as a fallback,
so behaviour is identical until the user opts into the new shape
through the Phase 5 settings UI.
"""
from app.notifications import bus, events

__all__ = ["bus", "events"]
