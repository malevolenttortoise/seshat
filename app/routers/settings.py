"""
Settings read + patch endpoints.

    GET   /api/v1/settings         — current settings with secrets redacted
    PATCH /api/v1/settings         — update whitelisted non-secret fields

Secrets (mam_session_id, mam_irc_password, qbit_password, and the
auth secret) are NEVER returned from this endpoint. The UI sees a
boolean "is configured" flag for each sensitive field instead; the
dedicated credentials page (Phase 5c / v1.0) will handle entry via
a separate secret store.

The PATCH whitelist is conservative. New keys must be added
explicitly to `_PATCHABLE_KEYS` below to prevent accidental exposure
of internal knobs through a weakly-validated surface.

Changes applied via PATCH persist to settings.json and rebuild the
dispatcher singleton so background loops pick up the new values
without a container restart.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from app import state
from app.config import load_settings, save_settings

_log = logging.getLogger("seshat.routers.settings")

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


# Keys the UI is allowed to write through the PATCH endpoint. Each
# entry MUST be a non-secret boolean, number, string, or list that
# the UI can render in a plain form control.
_PATCHABLE_KEYS: frozenset[str] = frozenset({
    # Filter gate
    "allowed_categories",
    "excluded_categories",
    "allowed_formats",
    "excluded_formats",
    "allowed_languages",
    "excluded_uploaders",
    "accept_audiobook_announces",
    "allowed_audiobook_categories",
    # Non-secret connection settings (visible in Settings as plain text)
    "ntfy_url",
    "qbit_url",
    "qbit_username",
    "mam_irc_nick",
    "mam_irc_account",
    # Policy engine
    "policy_vip_only",
    "policy_free_only",
    "policy_vip_always_grab",
    "policy_use_wedge",
    "policy_min_wedges_reserved",
    "policy_ratio_floor",
    # Snatch budget
    "snatch_budget_cap",
    "snatch_queue_max",
    "snatch_full_mode",
    "snatch_seed_hours_required",
    # Review queue + enrichment
    "review_queue_enabled",
    "review_staging_path",
    "metadata_review_timeout_days",
    "review_timeout_check_interval_seconds",
    "delayed_torrents_path",
    "metadata_enrichment_enabled",
    "metadata_provider_priority",
    "metadata_disabled_sources",
    "metadata_per_source_timeout",
    "metadata_accept_confidence",
    # Sinks
    "default_sink",
    "staging_path",
    "monthly_download_folders",
    # Notifications
    "ntfy_topic",
    "daily_digest_enabled",
    "daily_digest_hour",
    "per_event_notifications",
    "notify_on_grab",
    "notify_on_download_complete",
    "notify_on_pipeline_error",
    "notify_daily_accepted",
    "notify_daily_tentative",
    "notify_daily_ignored",
    "notify_weekly_digest",
    "download_folder_structure",
    # Pipeline toggles
    "download_client_type",
    "mam_irc_enabled",
    "pipeline_irc_enabled",
    "pipeline_qbit_watcher_enabled",
    "pipeline_auto_train_enabled",
    "pipeline_notifications_enabled",
    # Sink config
    "cwa_ingest_path",
    "calibre_library_path",
    "cwa_web_url",
    "calibre_web_url",
    "folder_sink_path",
    "delayed_torrents_path",
    "emergency_export_path",
    "sink_max_retries",
    # Audiobookshelf integration (Phase 4+) — all non-secret; the
    # API token itself lives in the encrypted store under
    # `abs_api_key` via /v1/credentials, not here.
    "abs_url",
    "abs_web_url",
    "abs_sink_library_id",
    "audiobookshelf_library_path",
    "audiobook_tracking_mode",
    "audiobook_format_priority",
    "audible_region",
    "audible_enabled",
    "rate_audible",
    "metadata_audiobook_priority",
    # Phase 7 unified metadata source configuration. The dedicated
    # /v1/metadata-sources panel is the primary editor for these two
    # keys; whitelisted here so the existing Settings PATCH router
    # doesn't strip them if the UI round-trips the full settings dict.
    "metadata_sources",
    "metadata_priority",
    # Operational
    "verbose_logging",
    "dry_run",
})

# Keys we redact entirely from the GET response. The UI gets a
# `_configured` boolean for each so it can render "Set" / "Not set"
# without ever seeing the value.
_SECRET_KEYS: frozenset[str] = frozenset({
    "mam_session_id",
    "mam_irc_password",
    "qbit_password",
})


class PatchResponse(BaseModel):
    ok: bool
    updated: list[str]
    rejected: list[str] = []


@router.get("")
async def get_settings() -> dict[str, Any]:
    """Return the current settings with secrets redacted.

    Each secret field is replaced with a sibling `<key>_configured`
    boolean so the UI can show "configured / not configured" without
    leaking the value itself.
    """
    settings = load_settings()
    out: dict[str, Any] = {}
    for key, value in settings.items():
        if key in _SECRET_KEYS:
            out[f"{key}_configured"] = bool(value)
            continue
        out[key] = value
    return out


@router.patch("", response_model=PatchResponse)
async def patch_settings(body: dict = Body(...)) -> PatchResponse:
    """Apply a whitelisted settings patch.

    Unknown keys and keys not in `_PATCHABLE_KEYS` are silently
    ignored (with the rejected list returned for transparency).
    Pydantic isn't used for the body so the UI can send sparse
    updates without pre-declaring every field.
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object")

    settings = dict(load_settings())
    updated: list[str] = []
    rejected: list[str] = []

    for key, value in body.items():
        if key not in _PATCHABLE_KEYS:
            rejected.append(key)
            continue
        if settings.get(key) == value:
            continue
        settings[key] = value
        updated.append(key)

    if updated:
        save_settings(settings)
        _log.info("settings patched: %s", updated)
        # Rebuild the dispatcher singleton so live loops pick up the
        # new values. main.py owns the build function. `_build_dispatcher`
        # is async (v1.1.1+) and reads credentials via `_resolve_secrets`;
        # the rebuild was producing a bare coroutine object through v1.1.2,
        # which silently corrupted `state.dispatcher`.
        try:
            from app.main import (  # type: ignore
                _build_dispatcher, _build_metadata_enricher, _resolve_secrets,
            )
            if state.dispatcher is not None:
                old_enricher = getattr(state.dispatcher, "metadata_enricher", None)
                resolved_secrets = await _resolve_secrets()
                state.dispatcher = await _build_dispatcher(settings, resolved_secrets)
                if old_enricher is not None:
                    try:
                        await old_enricher.aclose()
                    except Exception:
                        pass
                _log.info("dispatcher rebuilt after settings patch")
        except Exception:
            _log.exception(
                "dispatcher rebuild failed after settings patch (non-fatal)"
            )

    return PatchResponse(ok=True, updated=updated, rejected=rejected)
