"""
Credentials management endpoints.

    GET  /api/v1/credentials         — list which secrets are configured
    POST /api/v1/credentials/{key}   — set a secret value
    DELETE /api/v1/credentials/{key}  — remove a secret

Secrets are stored Fernet-encrypted in seshat_auth.db. The GET
endpoint only returns boolean "is configured" per key — never the
raw values. Setting a secret triggers a dispatcher rebuild so
background loops pick up the new credential immediately.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from app import secrets, state

_log = logging.getLogger("seshat.routers.credentials")

router = APIRouter(prefix="/api/v1/credentials", tags=["credentials"])


class CredentialStatus(BaseModel):
    key: str
    label: str
    configured: bool


class CredentialListResponse(BaseModel):
    items: list[CredentialStatus]


class SetCredentialRequest(BaseModel):
    value: str


class SimpleOk(BaseModel):
    ok: bool


@router.get("", response_model=CredentialListResponse)
async def list_credentials() -> CredentialListResponse:
    configured = await secrets.list_configured()
    items = [
        CredentialStatus(
            key=key,
            label=secrets.SECRET_KEYS[key],
            configured=configured.get(key, False),
        )
        for key in secrets.SECRET_KEYS
    ]
    return CredentialListResponse(items=items)


@router.post("/{key}", response_model=SimpleOk)
async def set_credential(key: str, body: SetCredentialRequest) -> SimpleOk:
    if key not in secrets.SECRET_KEYS:
        raise HTTPException(400, f"Unknown secret key: {key}")
    value = (body.value or "").strip()
    if not value:
        raise HTTPException(400, "Value cannot be empty")

    await secrets.set_secret(key, value)
    _log.info("credential %r updated via UI", key)

    # Apply the new credential to the live dispatcher immediately.
    await _apply_credential(key, value)

    return SimpleOk(ok=True)


@router.delete("/{key}", response_model=SimpleOk)
async def delete_credential(key: str) -> SimpleOk:
    if key not in secrets.SECRET_KEYS:
        raise HTTPException(400, f"Unknown secret key: {key}")
    await secrets.delete_secret(key)
    _log.info("credential %r deleted via UI", key)
    return SimpleOk(ok=True)


async def _apply_credential(key: str, value: str) -> None:
    """Push a just-updated credential into the live dispatcher.

    For MAM cookie: update the in-memory token so the next API call
    uses it. For qBit: rebuild the dispatcher. For others: rebuild.
    """
    if key == "mam_session_id":
        try:
            from app.mam.cookie import set_current_token
            set_current_token(value)
        except Exception:
            _log.exception("failed to apply MAM cookie update")

    # mam_browser_session_id (mbsc) apply-hook was REMOVED in v2.4.0 (TOS).

    # Rebuild the dispatcher to pick up any credential change.
    # `_build_dispatcher` became async in v1.1.1 when filter-author
    # loading moved to a DB query; this call site and the one in
    # settings.py were silently producing coroutine objects instead
    # of a DispatcherDeps up through v1.1.2, which broke every
    # dispatcher attribute access after the first credential save.
    try:
        from app.config import load_settings
        from app.main import _build_dispatcher, _resolve_secrets

        settings = dict(load_settings())

        # Pull fresh secrets from the encrypted store rather than
        # just injecting the one being updated — otherwise the
        # rebuilt dispatcher would read every OTHER secret from the
        # Sprint-6-blanked settings.json and lose qbit/mam/hardcover
        # credentials in one go.
        resolved_secrets = await _resolve_secrets()

        if state.dispatcher is not None:
            old_enricher = getattr(state.dispatcher, "metadata_enricher", None)
            state.dispatcher = await _build_dispatcher(settings, resolved_secrets)
            if old_enricher is not None:
                try:
                    await old_enricher.aclose()
                except Exception:
                    pass
            _log.info("dispatcher rebuilt after credential %r update", key)
    except Exception:
        _log.exception("dispatcher rebuild failed after credential update")
