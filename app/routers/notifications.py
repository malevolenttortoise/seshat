"""Notification taxonomy + routing API (Bundle B.2, v2.28.0).

    GET /api/v1/notifications/events
      Returns the full event registry so the Settings UI can render
      the per-event configuration grid without hardcoding event
      names. Includes each event's default priority + tags + quiet-
      hours suppressibility + legacy setting mapping so the UI can
      reflect the effective state when no new-shape override exists.

The actual per-event overrides live under the ``notifications`` key
in settings.json and are written through the existing
``PATCH /api/v1/settings`` endpoint — there is no separate writer
here so the post-save hook chain (dispatcher rebuild, source
reload, logging reapply) fires uniformly.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.notifications import events


router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


class EventMetaModel(BaseModel):
    name: str
    description: str
    default_priority: int
    default_tags: list[str]
    suppressible_during_quiet_hours: bool
    legacy_setting_key: str | None
    legacy_requires_master: bool
    legacy_default_enabled: bool


class EventCatalogueResponse(BaseModel):
    events: list[EventMetaModel]


@router.get("/events", response_model=EventCatalogueResponse)
async def get_event_catalogue() -> EventCatalogueResponse:
    """Return every catalogued notification event in declaration order."""
    return EventCatalogueResponse(
        events=[
            EventMetaModel(
                name=meta.name,
                description=meta.description,
                default_priority=meta.default_priority,
                default_tags=list(meta.default_tags),
                suppressible_during_quiet_hours=meta.suppressible_during_quiet_hours,
                legacy_setting_key=meta.legacy_setting_key,
                legacy_requires_master=meta.legacy_requires_master,
                legacy_default_enabled=meta.legacy_default_enabled,
            )
            for meta in events.REGISTRY.values()
        ]
    )
