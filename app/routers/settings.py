"""
Settings read + patch endpoints.

    GET   /api/v1/settings         — current settings with secrets redacted
    PATCH /api/v1/settings         — update settings (shared helper)

Secrets (mam_session_id, mam_irc_password, qbit_password, abs_api_key,
hardcover_api_key, and the auth secret) are NEVER returned verbatim;
the GET endpoint replaces each with a `{key}_configured` boolean.

The PATCH endpoint delegates to `apply_settings_patch()`, which is
ALSO called by the discovery-domain POST /api/discovery/settings
endpoint (used by the setup wizard). Keeping both endpoints share
one code path means there's exactly one write policy — any drift
between "what the main Settings page can save" and "what the setup
wizard writes" can't happen.

Write policy:
  - Runtime-state keys (written by background jobs) are rejected.
  - Secret keys are routed through the encrypted store, not
    settings.json. Masked/truncated values are ignored so the UI
    can safely re-submit a redacted settings dict.
  - Unknown keys (not present in DEFAULT_SETTINGS) are rejected.
  - Everything else saves.

Post-save, all three live-reload hooks fire:
  - dispatcher rebuild (pipeline background loops)
  - metadata source reload (discovery lookup + scan config)
  - logging level re-application (verbose_logging toggle)
so changes take effect without a container restart.
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


# Derived from app.secrets.SECRET_KEYS so the redact list stays in
# sync automatically — drift here previously leaked hardcover_api_key
# + abs_api_key via GET when lingering settings.json values existed
# (pre-migration or manual edits).
from app.secrets import SECRET_KEYS as _APP_SECRET_KEYS  # noqa: E402
_SECRET_KEYS: frozenset[str] = frozenset(_APP_SECRET_KEYS.keys())

# Runtime-state keys live in settings.json because they need to
# persist across restarts, but are WRITTEN by background jobs
# (circuit breakers, validation loops, grandfather-line stamps,
# library-sync cache) and should NEVER be overwritten by a PATCH.
# Clobbering `qbit_orphan_adoption_since` with 0 flooded the pipeline
# with thousands of adopted-orphan grabs in the past.
_RUNTIME_STATE_KEYS: frozenset[str] = frozenset({
    "google_books_auto_disabled_at",
    "mam_validation_ok",
    "mam_last_validated_at",
    "last_mam_validated_at",
    "qbit_orphan_adoption_since",
    # v2.13.0 — Goodreads Cloudflare soft-block state. Written by
    # `app/metadata/goodreads_session.py` on every response. Cleared
    # to "active" manually via the Settings panel "Mark as active"
    # button (which POSTs to a dedicated endpoint, not PATCH, so the
    # runtime-state protection here stays intact).
    "goodreads_session_state",
    "goodreads_session_state_since",
    "goodreads_session_last_status",
})


class PatchResponse(BaseModel):
    ok: bool
    updated: list[str]
    rejected: list[str] = []


def _looks_masked(value: Any) -> bool:
    """True if `value` is a redacted-looking string the UI might re-submit.

    The GET endpoint replaces secrets with `{key}_configured` booleans,
    but older flows still shipped truncated values like `"abc123..."` or
    `"***"`. Re-submitting those would overwrite the real secret with
    garbage, so any PATCH that contains them is a no-op for that key.
    """
    if not isinstance(value, str):
        return False
    return value == "" or value == "***" or "..." in value


async def apply_settings_patch(body: dict[str, Any]) -> PatchResponse:
    """Apply a partial settings update.

    Shared between the pipeline PATCH /v1/settings endpoint and the
    discovery POST /api/discovery/settings endpoint so both flows
    obey the same validation + post-save hook policy.
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object")

    from app.secrets import set_secret

    settings = dict(load_settings())
    updated: list[str] = []
    rejected: list[str] = []

    for key, value in body.items():
        # Protect runtime-state keys written by background jobs.
        if key in _RUNTIME_STATE_KEYS:
            rejected.append(key)
            continue
        # Reject keys that aren't in DEFAULT_SETTINGS — the canonical
        # list of known settings. `load_settings()` always returns
        # DEFAULT_SETTINGS merged with on-disk overrides, so this
        # covers every real key.
        if key not in settings:
            rejected.append(key)
            continue
        # Route secrets through the encrypted store. Skip masked
        # round-trips so re-saving a redacted GET response doesn't
        # blow away the real secret.
        if key in _SECRET_KEYS:
            if _looks_masked(value):
                continue
            await set_secret(key, value)
            # Clear any legacy plaintext copy that might still live in
            # settings.json from pre-encrypted-store installs.
            if settings.get(key):
                settings[key] = ""
            updated.append(key)
            continue
        # Plain value — no-op if unchanged.
        if settings.get(key) == value:
            continue
        settings[key] = value
        updated.append(key)

    if updated:
        save_settings(settings)
        _log.info("settings patched: %s", updated)
        await _run_post_save_hooks(settings)

    return PatchResponse(ok=True, updated=updated, rejected=rejected)


async def _run_post_save_hooks(settings: dict[str, Any]) -> None:
    """Fire all three live-reload hooks after a settings save.

    Called unconditionally after any update so callers never have to
    know which domain their change affects. Each hook is wrapped in
    try/except because a live-reload failure must not fail the save
    itself — settings.json is already on disk and the next container
    restart would pick it up.
    """
    # Discovery-side hooks: source plugins + logging level.
    try:
        from app.discovery.lookup import reload_sources
        from app.config import apply_logging
        reload_sources()
        apply_logging(bool(settings.get("verbose_logging", False)))
    except Exception:
        _log.exception("discovery post-save hooks failed (non-fatal)")

    # Pipeline-side hook: rebuild the dispatcher so live IRC /
    # qBit / snatch-budget loops read the new settings without
    # a container restart. Only runs when the dispatcher is live
    # — e.g. during tests the dispatcher is None and we skip.
    if state.dispatcher is None:
        return
    try:
        from app.main import (  # type: ignore
            _build_dispatcher, _resolve_secrets,
        )
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
    return await apply_settings_patch(body)
