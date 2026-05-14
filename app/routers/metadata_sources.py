"""
Unified metadata source configuration endpoints.

    GET  /api/v1/metadata-sources        — full panel state
    PUT  /api/v1/metadata-sources        — replace the full panel state

The GET response also includes read-only metadata from
`KNOWN_SOURCES` (display name, which content types the source
supports, whether it's the MAM "always-first" special case) so the
frontend can render locked rows and hide toggles for content types
a source doesn't support.

PUT accepts the full `{metadata_sources, metadata_priority}` shape
and writes it atomically alongside a legacy-key sync so any code
still reading `goodreads_enabled` / `rate_goodreads` / etc. during
the Phase 7 transition stays consistent. After writing, the
dispatcher is rebuilt so the live enricher picks up the new
priority + rate limits without a container restart.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import state
from app.config import load_settings, save_settings
from app.metadata.source_config import (
    KNOWN_SOURCES,
    derive_enrich_priority,
    derive_scan_priority,
    sync_legacy_keys,
)
from app.metadata.source_config import (
    _DEFAULT_AUDIOBOOK_PRIORITY,
    _DEFAULT_EBOOK_PRIORITY,
    _DEFAULT_NEW_INSTALL_STATE,
)

_log = logging.getLogger("seshat.routers.metadata_sources")

router = APIRouter(prefix="/api/v1/metadata-sources", tags=["metadata-sources"])


class SourceEntry(BaseModel):
    rate_limit: float = Field(..., ge=0.0, le=100.0)
    ebook_enrich: bool
    ebook_scan: bool
    audiobook_enrich: bool
    audiobook_scan: bool
    # v2.3.2: when True, source-scan keeps DETAIL-fetching books
    # missing this source's URL even when other sources have URLs
    # for them. See `is_source_mandatory` + `_lookup_author_inner`
    # in `app/discovery/lookup.py`. Default False so unknown/upgraded
    # entries decay safely; the migration seeds the right per-source
    # default at install time.
    mandatory: bool = False
    # v2.11.0 Stage 5++ — Amazon-specific config strings that drive
    # the server-side authorFilters API on /juvec. Other sources
    # leave these None. The frontend renders dropdowns for the
    # Amazon row only.
    # `format` = ebook-tab format filter; `audiobook_format` = the
    # parallel knob added in v2.11.1 when AmazonSource gained
    # audiobook discovery via /juvec format=["audible_audiobook"].
    format: str | None = None
    audiobook_format: str | None = None
    language: str | None = None
    # v2.11.1 N5 — Kobo-specific. Parallel detail-fetch worker count
    # for the asyncio.Semaphore in `app/discovery/sources/kobo.py`.
    # None for non-Kobo sources.
    concurrency: int | None = None


class PriorityLists(BaseModel):
    ebook: list[str]
    audiobook: list[str]


class SourceMetadata(BaseModel):
    """Read-only descriptor the UI renders alongside the editable row."""
    name: str
    display: str
    available_for: list[str]
    mam_only: bool = False


class MetadataSourcesState(BaseModel):
    """Full panel payload."""
    sources: dict[str, SourceEntry]
    priority: PriorityLists


class MetadataSourcesResponse(BaseModel):
    """GET response: state + known-source descriptors + live-derived lists.

    `derived` is informational — the frontend can render "these are
    the sources that will actually run for ebook enrich" without
    re-implementing the filter logic. The enricher / scanner code
    uses the same derivation helpers internally.
    """
    state: MetadataSourcesState
    known: list[SourceMetadata]
    derived: dict[str, list[str]]


class PutResponse(BaseModel):
    ok: bool
    dispatcher_rebuilt: bool


def _build_known() -> list[SourceMetadata]:
    return [
        SourceMetadata(
            name=name,
            display=meta["display"],
            available_for=list(meta["available_for"]),
            mam_only=bool(meta.get("mam_only", False)),
        )
        for name, meta in KNOWN_SOURCES.items()
    ]


def _state_from_settings(settings: dict) -> MetadataSourcesState:
    sources_raw = settings.get("metadata_sources") or {}
    priority_raw = settings.get("metadata_priority") or {}
    sources: dict[str, SourceEntry] = {}
    for name, entry in sources_raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            from app.metadata.source_config import is_source_mandatory
            sources[name] = SourceEntry(
                rate_limit=float(entry.get("rate_limit", 1.0)),
                ebook_enrich=bool(entry.get("ebook_enrich", False)),
                ebook_scan=bool(entry.get("ebook_scan", False)),
                audiobook_enrich=bool(entry.get("audiobook_enrich", False)),
                audiobook_scan=bool(entry.get("audiobook_scan", False)),
                # Read via the accessor so missing-field upgraded
                # settings.json files surface the ship-with default
                # to the panel — UI shows the user the value the
                # backend would actually use, not always-False.
                mandatory=is_source_mandatory(settings, name),
                # Amazon-specific. Read with defaults so pre-Stage-5++
                # entries surface the ship-defaults to the panel.
                format=entry.get("format") if name == "amazon" else None,
                audiobook_format=(
                    entry.get("audiobook_format")
                    if name == "amazon" else None
                ),
                language=entry.get("language") if name == "amazon" else None,
                # Kobo-specific (v2.11.1 N5). Parallel detail-fetch
                # worker count.
                concurrency=(
                    int(entry["concurrency"])
                    if name == "kobo" and entry.get("concurrency") is not None
                    else None
                ),
            )
        except Exception:
            _log.exception(
                "metadata_sources: ignoring malformed entry for %r", name,
            )
    return MetadataSourcesState(
        sources=sources,
        priority=PriorityLists(
            ebook=list(priority_raw.get("ebook") or []),
            audiobook=list(priority_raw.get("audiobook") or []),
        ),
    )


@router.get("", response_model=MetadataSourcesResponse)
async def get_state() -> MetadataSourcesResponse:
    settings = load_settings()
    state_obj = _state_from_settings(settings)
    derived = {
        "ebook_enrich": derive_enrich_priority(settings, audiobook=False),
        "ebook_scan": derive_scan_priority(settings, audiobook=False),
        "audiobook_enrich": derive_enrich_priority(settings, audiobook=True),
        "audiobook_scan": derive_scan_priority(settings, audiobook=True),
    }
    return MetadataSourcesResponse(
        state=state_obj, known=_build_known(), derived=derived,
    )


@router.put("", response_model=PutResponse)
async def put_state(body: MetadataSourcesState) -> PutResponse:
    # Validate that every name referenced in the priority lists has
    # a matching entry in `sources`. Silently skip unknown names
    # rather than 400-ing so the frontend can send a "tentative"
    # state without hitting a hard error on typos.
    known_names = set(body.sources.keys())
    ebook = [n for n in body.priority.ebook if n in known_names]
    audiobook = [n for n in body.priority.audiobook if n in known_names]

    settings = load_settings()
    # Detect a Google Books re-enable so the circuit-breaker timestamp
    # can be cleared. Without this, re-enabling via the panel after a
    # trip would keep the Dashboard banner up forever and let
    # reload_sources() rebuild a fresh instance only to immediately
    # re-trip since the breaker reads the "was tripped" stamp.
    prev_gb = (settings.get("metadata_sources") or {}).get("google_books") or {}
    new_gb_entry = body.sources.get("google_books")
    new_gb = new_gb_entry.model_dump() if new_gb_entry else {}
    gb_was_off = not (prev_gb.get("ebook_scan") or prev_gb.get("ebook_enrich")
                      or prev_gb.get("audiobook_scan") or prev_gb.get("audiobook_enrich"))
    gb_now_on = bool(new_gb.get("ebook_scan") or new_gb.get("ebook_enrich")
                     or new_gb.get("audiobook_scan") or new_gb.get("audiobook_enrich"))
    gb_reenabled = gb_was_off and gb_now_on
    settings["metadata_sources"] = {
        name: entry.model_dump() for name, entry in body.sources.items()
    }
    settings["metadata_priority"] = {
        "ebook": ebook,
        "audiobook": audiobook,
    }
    if gb_reenabled:
        settings["google_books_auto_disabled_at"] = None
    # Keep the legacy keys in sync so any code still reading them
    # during the Phase 7 transition sees the user's new intent.
    sync_legacy_keys(settings)
    save_settings(settings)

    # Rebuild the dispatcher so the live enricher picks up the new
    # priority + rate limits without a container restart.
    rebuilt = False
    try:
        from app.main import _build_dispatcher
        resolved = await _resolve_secrets_lazy()
        state.dispatcher = await _build_dispatcher(settings, resolved)
        rebuilt = True
    except Exception:
        _log.exception(
            "metadata_sources PUT: dispatcher rebuild failed "
            "(settings saved — restart container to apply)"
        )

    # v2.11.1 N9: also rebuild the discovery-side source singletons.
    # The dispatcher rebuild above only covers the enricher path; the
    # `amazon`, `kobo`, `goodreads`, etc. module-level instances in
    # `app/discovery/lookup.py` are constructed at startup from the
    # settings then mutated nowhere — so a Rate field change saved via
    # this endpoint never reached the running discovery scanner until
    # the next container restart. UAT 2026-05-13 caught this when
    # Mark bumped Amazon Rate 3 → 30 and the next scan still fired
    # at rate=3. The reload also picks up format/language config
    # introduced in Stage 5++.
    sources_reloaded = False
    try:
        from app.discovery.lookup import reload_sources
        reload_sources()
        sources_reloaded = True
    except Exception:
        _log.exception(
            "metadata_sources PUT: discovery source reload failed "
            "(settings saved — restart container to apply)"
        )

    _log.info(
        "metadata_sources updated: %d sources, ebook priority=%d, "
        "audiobook priority=%d, dispatcher_rebuilt=%s, "
        "sources_reloaded=%s",
        len(body.sources), len(ebook), len(audiobook),
        rebuilt, sources_reloaded,
    )
    return PutResponse(ok=True, dispatcher_rebuilt=rebuilt)


@router.post("/reset", response_model=MetadataSourcesResponse)
async def reset_to_defaults() -> MetadataSourcesResponse:
    """Wipe `metadata_sources` + `metadata_priority` and rebuild from
    `_DEFAULT_NEW_INSTALL_STATE` + the default priority lists.

    v2.11.1 N-item: the original v2.11.0 priority reshuffle migration
    only fires for fresh installs (`migrate_legacy_settings` skips
    the legacy pass on populated installs and the backfill pass only
    ADDS missing entries, never touches existing ones). Upgraded
    users were left with whatever priority + per-source state they
    had at upgrade time. This endpoint gives them an in-app way to
    forcibly adopt the current ship-defaults.

    Returns the freshly-reset state in the same shape as GET so the
    frontend can update its display without a follow-up GET. Side
    effects: settings persisted to disk, dispatcher rebuilt, and
    `reload_sources()` re-instantiates the discovery-side singletons
    (matching the v2.11.1 N9 fix in PUT).
    """
    settings = load_settings()
    # Bypass the legacy-migration path entirely — it would honour
    # the legacy `metadata_provider_priority` key (still in the
    # settings dict from a pre-Phase-7 install) and emit a state
    # that mirrors the user's pre-overhaul ordering, not the
    # current ship-defaults. Build the panel state directly from
    # `_DEFAULT_NEW_INSTALL_STATE` + `KNOWN_SOURCES`.
    fresh_sources: dict[str, dict[str, Any]] = {}
    for name, meta in KNOWN_SOURCES.items():
        defaults = _DEFAULT_NEW_INSTALL_STATE.get(name) or {
            "ebook_enrich": False, "ebook_scan": False,
            "audiobook_enrich": False, "audiobook_scan": False,
            "mandatory": False,
        }
        fresh_sources[name] = {
            **defaults,
            "rate_limit": float(meta.get("default_rate", 1.0)),
        }
    # Default priority lists, filtered to sources that support each
    # surface (audiobook list drops ebook-only sources, etc.).
    fresh_ebook = [
        n for n in _DEFAULT_EBOOK_PRIORITY
        if n in KNOWN_SOURCES
        and "ebook" in (KNOWN_SOURCES[n].get("available_for") or ())
    ]
    fresh_audiobook = [
        n for n in _DEFAULT_AUDIOBOOK_PRIORITY
        if n in KNOWN_SOURCES
        and "audiobook" in (KNOWN_SOURCES[n].get("available_for") or ())
    ]
    settings["metadata_sources"] = fresh_sources
    settings["metadata_priority"] = {
        "ebook": fresh_ebook,
        "audiobook": fresh_audiobook,
    }
    sync_legacy_keys(settings)
    save_settings(settings)

    # Same dispatcher + sources-reload chain as PUT.
    try:
        from app.main import _build_dispatcher
        resolved = await _resolve_secrets_lazy()
        state.dispatcher = await _build_dispatcher(settings, resolved)
    except Exception:
        _log.exception(
            "metadata_sources reset: dispatcher rebuild failed "
            "(settings reset — restart container to apply)"
        )
    try:
        from app.discovery.lookup import reload_sources
        reload_sources()
    except Exception:
        _log.exception(
            "metadata_sources reset: source reload failed "
            "(settings reset — restart container to apply)"
        )

    _log.info(
        "metadata_sources reset to v2.11.x defaults: %d sources, "
        "ebook priority=%d, audiobook priority=%d",
        len(settings.get("metadata_sources", {})),
        len(settings.get("metadata_priority", {}).get("ebook") or []),
        len(settings.get("metadata_priority", {}).get("audiobook") or []),
    )

    # Return the new state in GET-shape so the panel can re-render.
    state_obj = _state_from_settings(settings)
    derived = {
        "ebook_enrich": derive_enrich_priority(settings, audiobook=False),
        "ebook_scan": derive_scan_priority(settings, audiobook=False),
        "audiobook_enrich": derive_enrich_priority(settings, audiobook=True),
        "audiobook_scan": derive_scan_priority(settings, audiobook=True),
    }
    return MetadataSourcesResponse(
        state=state_obj, known=_build_known(), derived=derived,
    )


async def _resolve_secrets_lazy() -> dict[str, Any]:
    """Pull secrets from the encrypted store for the dispatcher rebuild."""
    from app.secrets import get_secret, SECRET_KEYS
    out: dict[str, str] = {}
    for key in SECRET_KEYS:
        val = await get_secret(key)
        if val:
            out[key] = val
    return out
