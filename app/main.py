"""
Seshat FastAPI entrypoint.

Phase 1 wires:
  - The dispatcher singleton (built once at startup from settings)
  - The manual-inject endpoint
  - The MAM IRC listener (auto-starts on boot, supervised + restarts
    on crash, reconnects with exponential backoff on disconnect)
  - The snatch budget watcher loop (polls qBit, reconciles ledger,
    drains pending_queue when budget frees)

If settings change at runtime (via the eventual Settings UI in
Phase 3), the dispatcher will need to be rebuilt — that plumbing
lives where the Settings UI does, not here.

Both background loops are wrapped in `state.supervised_task` so
they restart automatically on unexpected crashes and a fatal
exception in one doesn't take down the other.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app import state
from app.clients.qbittorrent import QbitClient
from app.config import (
    ENV_VERBOSE_LOGGING,
    apply_logging,
    load_settings,
    save_settings,
)
from app.database import get_db, init_db
from app.storage.authors import load_normalized_sets
from app.filter.gate import Announce, FilterConfig
from app.filter.normalize import extract_format, normalize_category
from app.policy.engine import PolicyConfig
from app.mam.cookie import (
    aclose_session,
    set_current_token,
    set_rotation_callback,
)
from app.mam.grab import fetch_torrent
from app.mam.irc import IrcClient, IrcConfig
from app.orchestrator.budget_watcher import run_loop as budget_watcher_loop
from app.orchestrator.cookie_keepalive import run_loop as cookie_keepalive_loop
from app.orchestrator.cookie_retry import run_loop as cookie_retry_loop
from app.orchestrator.dispatch import DispatcherDeps, handle_announce
from app.orchestrator.review_timeout import run_loop as review_timeout_loop
from app.orchestrator.scheduler import (
    register_digest_jobs,
    register_goodreads_canary,
    register_metadata_cache_health,
)
from app.notify.digests import DigestContext
from app.notify.ntfy import aclose as ntfy_aclose
from app.auth_db import init_auth_db
from app.auth_sessions import SESSION_COOKIE_NAME, verify_session_token
from app.routers.announces import router as announces_router
from app.routers.grabs import router as grabs_router
from app.routers.auth import router as auth_router
from app.routers.authors import router as authors_router
from app.routers.covers import router as covers_router
from app.routers.credentials import router as credentials_router
from app.routers.data_management import router as data_mgmt_router
from app.routers.db_editor import router as db_editor_router
from app.routers.delayed import router as delayed_router
from app.routers.economy import router as economy_router
from app.routers.enums import router as enums_router
from app.routers.inject import router as inject_router
from app.routers.logs import router as logs_router, install_log_handler
from app.routers.mam import router as mam_router
from app.routers.metadata_cache import router as metadata_cache_router
from app.routers.metadata_sources import router as metadata_sources_router
from app.routers.goodreads_session import router as goodreads_session_router
from app.routers.migration import router as migration_router
from app.routers.review import router as review_router
from app.routers.search import router as search_router
from app.routers.settings import router as settings_router
from app.routers.sse import router as sse_router
from app.routers.works import router as works_router
from app.routers.tentative import router as tentative_router
from app.metadata.enricher import EnrichmentConfig, MetadataEnricher

# ── Discovery domain routers ──────────────────────────────────
from app.discovery.routers.books import router as disc_books_router
from app.discovery.routers.authors import router as disc_authors_router
from app.discovery.routers.series import router as disc_series_router
from app.discovery.routers.suggestions import router as disc_suggestions_router
from app.discovery.routers.metadata import router as disc_metadata_router
from app.discovery.routers.scan import router as disc_scan_router
from app.discovery.routers.mam import router as disc_mam_router
from app.discovery.routers.libraries import router as disc_libraries_router
from app.discovery.routers.covers import router as disc_covers_router
from app.discovery.routers.audiobookshelf import router as disc_abs_router
from app.discovery.routers.import_export import router as disc_import_export_router
from app.discovery.routers.config import router as disc_config_router
from app.discovery.routers.pipeline_send import router as disc_pipeline_send_router
from app.discovery.routers.reingest import router as disc_reingest_router
from app.discovery.routers.hygiene import router as disc_hygiene_router

# Configure logging once at import time. The verbose toggle gets re-applied
# from settings.json after load_settings() runs in the lifespan.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
apply_logging(ENV_VERBOSE_LOGGING)


class _QuietAccessFilter(logging.Filter):
    """Suppress uvicorn access-log records for high-frequency polling
    endpoints that drown out the real signal in `docker logs`.

    /api/health fires every 30s from the Docker healthcheck, plus any
    external monitors the user has pointed at it. The actual HTTP
    status lives in `docker inspect`; the access log just repeats it.
    """

    _QUIET_PATHS = ("/api/health",)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._QUIET_PATHS)


logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())

_log = logging.getLogger("seshat")


async def _resolve_secrets() -> dict:
    """Read every SECRET_KEY from the encrypted store into a plaintext dict.

    Called at startup and any time the dispatcher or enricher is
    rebuilt so downstream components see fresh credentials without
    falling back to the Sprint-6-blanked `settings.json` values.
    """
    from app.secrets import get_secret, SECRET_KEYS
    out: dict[str, str] = {}
    for key in SECRET_KEYS:
        val = await get_secret(key)
        if val:
            out[key] = val
    return out


async def _build_filter_config(settings: dict) -> FilterConfig:
    """Construct a FilterConfig from settings + a fresh DB snapshot.

    Allow / ignore lists are sourced from the `authors_allowed` and
    `authors_ignored` DB tables at construction time. Mutations
    (via the authors router, auto-train, tentative approval, weekly
    digest promotions) call `state.refresh_filter_authors()` to
    rebuild the filter_config's author sets without restarting the
    process.

    Prior to v1.1.0-post-release, this returned empty author sets —
    meaning every IRC announce fell through to "author_not_allowlisted"
    and silently piled up in tentative_torrents regardless of the
    authors_allowed table's contents. That was a latent bug dating
    to the v1.0 author-list UI ship; the filter build was never
    updated to actually consult the tables the UI writes to.
    """
    db = await get_db()
    try:
        allowed_authors, ignored_authors = await load_normalized_sets(db)
    finally:
        await db.close()

    # Start from the user's ebook category list. As of v2.9.0,
    # audiobook acceptance is driven by the Media Type filter
    # (`allowed_formats`) instead of a separate boolean: when the
    # filter is empty (= "accept all formats") OR when it contains
    # "audiobooks", we merge `allowed_audiobook_categories` into the
    # runtime category set so the filter admits audiobook announces
    # without the user having to edit their existing ebook
    # configuration. See `_apply_legacy_settings_migrations` in
    # app/config.py for the upgrade path from the old toggle.
    category_entries = list(settings.get("allowed_categories", []) or [])
    format_entries = list(settings.get("allowed_formats", []) or [])
    audiobook_accepted = (
        not format_entries  # empty = accept all
        or any(
            (extract_format(f) or normalize_category(f)) == "audiobooks"
            for f in format_entries
        )
    )
    if audiobook_accepted:
        category_entries.extend(
            settings.get("allowed_audiobook_categories", []) or []
        )

    return FilterConfig(
        allowed_categories=frozenset(
            normalize_category(c) for c in category_entries
        ),
        excluded_categories=frozenset(
            normalize_category(c) for c in settings.get("excluded_categories", [])
        ),
        allowed_formats=frozenset(
            extract_format(f) or normalize_category(f)
            for f in format_entries
        ),
        excluded_formats=frozenset(
            extract_format(f) or normalize_category(f)
            for f in settings.get("excluded_formats", [])
        ),
        allowed_languages=frozenset(
            lang.strip().lower() for lang in settings.get("allowed_languages", [])
        ),
        allowed_authors=allowed_authors,
        ignored_authors=ignored_authors,
    )


def _build_metadata_enricher(
    settings: dict, resolved_secrets: Optional[dict] = None
) -> MetadataEnricher:
    """Construct the Tier 4 metadata enricher from settings.

    Always returns a live enricher — the `enabled` flag on
    `EnrichmentConfig` gates whether it actually runs, so the
    pipeline can pass through `deps.metadata_enricher` without a
    None guard.

    `resolved_secrets` supplies plaintext credentials from the
    encrypted store so sources like Hardcover don't have to fall
    back to the Sprint-6-blanked `settings.json` (the bug that left
    Hardcover silently unauthenticated across every enrichment run
    before v1.1.3).
    """
    # Phase 7: priority lists derived from the unified
    # `metadata_sources` + `metadata_priority` shape. The derivation
    # filters the priority list to sources whose `*_enrich` toggle is
    # True, so a user who turns Audible off for audiobook enrichment
    # via the Metadata Sources panel sees that source drop out of the
    # priority tuple here without further plumbing.
    #
    # Fallback to the legacy lists when the new shape is empty (e.g.
    # pre-migration settings.json) so an upgrade-in-progress
    # deployment keeps working.
    from app.metadata.source_config import derive_enrich_priority
    ebook_priority = tuple(derive_enrich_priority(settings, audiobook=False))
    audiobook_priority = tuple(derive_enrich_priority(settings, audiobook=True))
    if not ebook_priority:
        ebook_priority = tuple(
            settings.get("metadata_provider_priority", [])
            or ("goodreads", "amazon", "hardcover", "kobo", "ibdb", "google_books")
        )
    if not audiobook_priority:
        audiobook_priority = tuple(
            settings.get("metadata_audiobook_priority", [])
            or ("audible", "goodreads", "hardcover", "google_books")
        )

    cfg = EnrichmentConfig(
        enabled=bool(settings.get("metadata_enrichment_enabled", False)),
        priority=ebook_priority,
        audiobook_priority=audiobook_priority,
        disabled_sources=frozenset(
            settings.get("metadata_disabled_sources", []) or []
        ),
        per_source_timeout=float(
            settings.get("metadata_per_source_timeout", 15.0)
        ),
        accept_confidence=float(
            settings.get("metadata_accept_confidence", 0.8)
        ),
    )
    rs = resolved_secrets or {}
    hardcover_key = rs.get("hardcover_api_key") or ""
    audible_region = (settings.get("audible_region") or "us").lower()
    return MetadataEnricher(
        cfg,
        hardcover_api_key=hardcover_key,
        audible_region=audible_region,
    )


async def _build_dispatcher(settings: dict, resolved_secrets: dict = None) -> DispatcherDeps:
    """Build the dispatcher from a settings snapshot.

    `resolved_secrets` is an optional dict of {key: plaintext} that
    takes priority over settings.json for secret fields. The lifespan
    reads from the encrypted store and passes them here; the settings
    PATCH rebuild doesn't have secrets so it falls back to whatever
    is in settings.json (which may be empty post-migration).
    """
    rs = resolved_secrets or {}
    client_type = settings.get("download_client_type", "qbittorrent")
    client_url = rs.get("qbit_url") or settings.get("qbit_url", "")
    client_user = rs.get("qbit_username") or settings.get("qbit_username", "")
    client_pass = rs.get("qbit_password") or settings.get("qbit_password", "")

    if client_type == "transmission":
        from app.clients.transmission import TransmissionClient
        torrent_client = TransmissionClient(
            base_url=client_url, username=client_user, password=client_pass,
        )
    elif client_type == "deluge":
        from app.clients.deluge import DelugeClient
        torrent_client = DelugeClient(
            base_url=client_url, password=client_pass,
        )
    elif client_type == "rtorrent":
        from app.clients.rtorrent import RtorrentClient
        torrent_client = RtorrentClient(
            base_url=client_url, username=client_user, password=client_pass,
        )
    else:
        torrent_client = QbitClient(
            base_url=client_url, username=client_user, password=client_pass,
        )
    # qbit_tag is a single string in settings.json, but the client
    # accepts a list so the future VIP/freeleech work can stack
    # additional tier-specific tags ("seshat-seed,vip" or
    # "seshat-seed,freeleech-wedge") without changing the data
    # type. Phase 1.5 ships with a single static tag.
    raw_tag = settings.get("qbit_tag", "seshat-seed").strip()
    qbit_tags = [t.strip() for t in raw_tag.split(",") if t.strip()]

    enricher = _build_metadata_enricher(settings, resolved_secrets)
    excluded_uploaders = frozenset(
        u.strip().lower()
        for u in (settings.get("excluded_uploaders") or [])
        if u and u.strip()
    )
    return DispatcherDeps(
        filter_config=await _build_filter_config(settings),
        policy_config=PolicyConfig(
            vip_only=bool(settings.get("policy_vip_only", False)),
            free_only=bool(settings.get("policy_free_only", False)),
            vip_always_grab=bool(settings.get("policy_vip_always_grab", True)),
            use_wedge=bool(settings.get("policy_use_wedge", False)),
            min_wedges_reserved=int(settings.get("policy_min_wedges_reserved", 0)),
            ratio_floor=float(settings.get("policy_ratio_floor", 0.0)),
            buffer_gate_enabled=bool(
                settings.get("mam_economy_buffer_gate_enabled", False)
            ),
            buffer_safety_margin_bytes=int(
                float(settings.get("mam_economy_buffer_gate_safety_margin_gb", 1))
                * 1_000_000_000
            ),
        ),
        mam_token=rs.get("mam_session_id") or settings.get("mam_session_id", ""),
        qbit_category=settings.get("qbit_watch_category", "[mam-reseed]"),
        qbit_tags=qbit_tags,
        budget_cap=int(settings.get("snatch_budget_cap", 200)),
        queue_max=int(settings.get("snatch_queue_max", 100)),
        queue_mode_enabled=settings.get("snatch_full_mode", "queue") == "queue",
        seed_seconds_required=int(
            settings.get("snatch_seed_hours_required", 72)
        ) * 3600,
        db_factory=get_db,
        fetch_torrent=fetch_torrent,
        qbit=torrent_client,
        dry_run=bool(settings.get("dry_run", False)),
        excluded_uploaders=excluded_uploaders,
        qbit_download_path=settings.get("qbit_download_path", ""),
        download_folder_structure=settings.get("download_folder_structure", "monthly") or "monthly",
        download_folder_template=settings.get("download_folder_template", "") or "",
        qbit_path_prefix=settings.get("qbit_path_prefix", "/data"),
        local_path_prefix=settings.get("local_path_prefix", "/downloads"),
        delayed_torrents_path=settings.get("delayed_torrents_path", ""),
        staging_path=settings.get("staging_path", ""),
        review_queue_enabled=bool(settings.get("review_queue_enabled", True)),
        review_staging_path=settings.get("review_staging_path", ""),
        metadata_review_timeout_days=int(settings.get("metadata_review_timeout_days", 14)),
        qbit_orphan_adoption_since=float(
            settings.get("qbit_orphan_adoption_since", 0) or 0
        ),
        audiobook_format_priority=list(
            settings.get("audiobook_format_priority", [])
            or ["m4b", "m4a", "mp3"]
        ),
        # Reuse the existing `mam_format_priority` setting (already
        # surfaced in Settings → Format Priorities for MAM scoring).
        # Same list drives both result-picking on the search side AND
        # primary-file-selection on the file-copier side.
        ebook_format_priority=list(
            settings.get("mam_format_priority", []) or []
        ),
        default_sink=settings.get("default_sink", "calibre"),
        calibre_library_path=settings.get("calibre_library_path", ""),
        folder_sink_path=settings.get("folder_sink_path", ""),
        audiobookshelf_library_path=settings.get("audiobookshelf_library_path", ""),
        abs_base_url=settings.get("abs_url", ""),
        abs_api_key=(resolved_secrets or {}).get("abs_api_key", "") or "",
        abs_library_id=settings.get("abs_sink_library_id", ""),
        cwa_ingest_path=settings.get("cwa_ingest_path", ""),
        cwa_min_inter_book_seconds=float(
            settings.get("cwa_min_inter_book_seconds", 10) or 0
        ),
        category_routing=settings.get("category_routing", {}),
        ntfy_url=settings.get("ntfy_url", ""),
        ntfy_topic=settings.get("ntfy_topic", "seshat"),
        per_event_notifications=bool(settings.get("per_event_notifications", False)),
        auto_train_enabled=bool(settings.get("pipeline_auto_train_enabled", True)),
        metadata_enricher=enricher,
        # v2.9.0 format-priority dedup.
        format_priority=dict(settings.get("format_priority") or {}),
        format_dedup_hold_seconds=int(
            settings.get("format_dedup_hold_seconds", 600) or 600
        ),
    )


# ─── Debounced cookie-rotation persistence ───────────────────
#
# MAM rotates the session cookie on every API call. If Seshat is
# running hot (several inject calls + a budget-watcher poll every
# 60s + any IRC-triggered grabs), that could mean a few
# settings.json writes per minute. Debounce so we only flush to disk
# at most every `_ROTATION_PERSIST_DEBOUNCE_SECONDS` — the in-memory
# token is always current; the only thing at stake is whether a
# hard container crash would lose up to 60s of rotation progress.
# Even the worst case is harmless: on restart we use the slightly
# older cookie from settings.json, immediately get a fresh one on
# the first MAM call, and we're back in sync.
_ROTATION_PERSIST_DEBOUNCE_SECONDS = 60.0
_rotation_pending_token: Optional[str] = None
_rotation_persist_task: Optional[asyncio.Task] = None


async def _rotation_callback(new_token: str) -> None:
    """Persist the rotated cookie to the encrypted secret store, debounced.

    Called by `app.mam.cookie._handle_response_cookie` on every
    successful rotation. We stash the new token and (re)schedule a
    background task to flush it after the debounce window. Multiple
    rotations within the window collapse into a single persistence
    write of whichever token was seen last.

    Target is `app.secrets.set_secret`, not `settings.json` — the
    encrypted store is the canonical location (startup reads from it
    first, settings.json is legacy fallback that `migrate_from_settings`
    blanks at boot). Writing to settings.json on rotation would leave
    the encrypted copy stale and defeat the migration.
    """
    global _rotation_pending_token, _rotation_persist_task
    _rotation_pending_token = new_token

    # Cancel any existing pending flush so the debounce timer
    # resets — the most recent rotation wins, and we don't want a
    # stale token hitting the secret store while a fresher one is
    # already in memory.
    if _rotation_persist_task is not None and not _rotation_persist_task.done():
        _rotation_persist_task.cancel()

    _rotation_persist_task = asyncio.create_task(
        _debounced_persist_rotation()
    )


async def _debounced_persist_rotation() -> None:
    """Wait out the debounce window, then write the latest token."""
    try:
        await asyncio.sleep(_ROTATION_PERSIST_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return  # superseded by a newer rotation; nothing to do

    token = _rotation_pending_token
    if not token:
        return

    try:
        from app.secrets import get_secret, set_secret
        if await get_secret("mam_session_id") == token:
            return  # already persisted (someone else wrote it)
        await set_secret("mam_session_id", token)
        _log.info(f"MAM session cookie persisted to encrypted store ({token[:8]}...)")
    except Exception:
        _log.exception("failed to persist rotated MAM cookie to encrypted store")


# mbsc rotation machinery was REMOVED in v2.4.0 — TOS-disallowed per
# MAM staff. See feedback_mam_mbsc_filelist_tos.md.


def _build_irc_config(settings: dict, resolved_secrets: dict = None) -> IrcConfig:
    """Construct an IrcConfig from a settings snapshot.

    Returns a config with `auth_mode="none"` if no IRC credentials
    are configured — the lifespan won't start the listener in that
    case (Seshat runs as a synchronous-call pipeline, useful for
    testing without IRC).
    """
    rs = resolved_secrets or {}
    nick = rs.get("mam_irc_nick") or settings.get("mam_irc_nick", "")
    account = rs.get("mam_irc_account") or settings.get("mam_irc_account", "")
    password = rs.get("mam_irc_password") or settings.get("mam_irc_password", "")
    auth_mode = "sasl" if (account and password) else "none"
    return IrcConfig(
        nick=nick,
        account=account,
        password=password,
        auth_mode=auth_mode,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup + shutdown wiring."""
    settings = load_settings()
    apply_logging(settings.get("verbose_logging", False))
    install_log_handler()
    _log.info("Seshat starting")

    # Grandfather-line initialization for qBit orphan adoption.
    # `qbit_orphan_adoption_since` defaults to 0; on the first boot of
    # any build that contains the adopter, set it to `time.time()` so
    # pre-existing torrents in the watch category (potentially
    # thousands on long-running qBit instances) aren't mass-adopted
    # into the pipeline. Once initialized it's never auto-updated —
    # the user can reset via Settings if they want a different cutoff.
    if settings.get("qbit_orphan_adoption_since", 0) == 0:
        import time as _time_init
        from app.config import save_settings as _save
        settings["qbit_orphan_adoption_since"] = _time_init.time()
        _save(settings)
        _log.info(
            "qBit orphan adoption cutoff initialized to %s (pre-existing "
            "torrents in the watch category will NOT be adopted)",
            settings["qbit_orphan_adoption_since"],
        )

    # Phase 7 unified metadata sources — migrate legacy settings on
    # first boot, seed fresh installs. Idempotent; no-op after the
    # first successful migration.
    try:
        from app.metadata.source_config import migrate_legacy_settings
        from app.config import save_settings as _save_sources
        if migrate_legacy_settings(settings):
            _save_sources(settings)
    except Exception:
        _log.exception(
            "metadata sources migration failed (non-fatal — enricher will "
            "fall back to legacy priority lists)"
        )

    await init_db()
    _log.info("Database initialized")
    await init_auth_db()
    _log.info("Auth database initialized")

    # Initialize the encrypted secret store and migrate any secrets
    # still in settings.json into the auth DB.
    from app.secrets import init_secrets_table, migrate_from_settings
    await init_secrets_table()
    migrated = await migrate_from_settings()
    if migrated:
        _log.info("Migrated %d secret(s) from settings.json to encrypted store", migrated)

    # Resolve secrets from the encrypted store for the dispatcher.
    resolved_secrets = await _resolve_secrets()

    # Seed the MAM cookie from the secret store (preferred) or settings.
    mam_cookie = resolved_secrets.get("mam_session_id") or settings.get("mam_session_id", "")
    set_current_token(mam_cookie)
    set_rotation_callback(_rotation_callback)
    _log.info("MAM cookie rotation handler wired")

    # mbsc browser-session cookie wiring was REMOVED in v2.4.0 (TOS).

    state.dispatcher = await _build_dispatcher(settings, resolved_secrets)
    _log.info("Dispatcher initialized")

    # ── Background loops (supervised) ────────────────────────
    #
    # Both loops capture the dispatcher singleton at startup time.
    # If a settings change rebuilds the dispatcher, the loops will
    # need to be restarted — that plumbing lives with the Settings
    # UI in Phase 3.

    deps_for_loops = state.dispatcher

    # Budget watcher: polls qBit, reconciles ledger, drains queue.
    # Auto-disabled if qBit isn't configured (the loop would just
    # error out on every tick otherwise).
    if settings.get("qbit_url"):
        interval = float(
            settings.get("qbit_poll_interval_seconds", 60)
        )

        async def _budget_loop_factory():
            await budget_watcher_loop(deps_for_loops, interval_seconds=interval)

        state._budget_watcher_task = state.supervised_task(
            _budget_loop_factory, name="snatch-budget-watcher"
        )
        _log.info(
            f"Budget watcher started (interval={interval}s, "
            f"qbit_category={settings.get('qbit_watch_category', '[mam-reseed]')})"
        )
    else:
        _log.info("Budget watcher disabled (qbit_url not configured)")

    # IRC listener: connects to MAM, parses announces, dispatches
    # to handle_announce. Auto-disabled if MAM auth isn't configured
    # OR if the user explicitly toggled mam_irc_enabled off in
    # settings (e.g. during cookie rotation, or in dry-run-friendly
    # test setups).
    irc_enabled = settings.get("mam_irc_enabled", True)
    irc_config = _build_irc_config(settings, resolved_secrets)
    if irc_enabled and irc_config.auth_mode != "none" and irc_config.nick:
        async def _on_announce(announce: Announce) -> None:
            # Bridge the IRC callback signature to the dispatcher.
            # The dispatcher's own try/except keeps a single bad
            # announce from killing the listener; this thin wrapper
            # is just signature glue.
            await handle_announce(deps_for_loops, announce)

        irc_client = IrcClient(irc_config, _on_announce)
        state.irc_client = irc_client

        async def _irc_loop_factory():
            await irc_client.run_forever()

        state._irc_task = state.supervised_task(
            _irc_loop_factory, name="mam-irc-listener"
        )
        _log.info(
            f"IRC listener started (server={irc_config.server}, "
            f"channel={irc_config.channel}, nick={irc_config.nick})"
        )
    else:
        _log.info(
            "IRC listener disabled (set mam_irc_nick + mam_irc_account + "
            "mam_irc_password to enable)"
        )

    # Cookie keep-alive: hits MAM's search endpoint on a fixed
    # interval (default 7 days) so the in-memory cookie always has
    # something to chew on, even if Seshat sees no other MAM
    # activity for weeks. Without this, a long quiet period would
    # silently expire the cookie despite all the rotation plumbing
    # working perfectly. Auto-disabled if no cookie is configured
    # (the keep-alive call would fail with "no MAM session" anyway).
    # Gate on the resolved cookie (encrypted store + settings.json),
    # not settings.json alone — Sprint 6 migration blanks that field.
    if mam_cookie:
        keepalive_seconds = float(
            settings.get("cookie_keepalive_interval_hours", 168)
        ) * 3600.0

        async def _keepalive_loop_factory():
            await cookie_keepalive_loop(interval_seconds=keepalive_seconds)

        state._cookie_keepalive_task = state.supervised_task(
            _keepalive_loop_factory, name="cookie-keepalive"
        )
        _log.info(
            f"Cookie keep-alive started "
            f"(interval={keepalive_seconds / 3600:.1f}h)"
        )
    else:
        _log.info("Cookie keep-alive disabled (mam_session_id not configured)")

    # Cookie retry: re-attempts grabs stuck in failed_cookie_expired.
    # Runs on a 5-minute interval (configurable). Auto-disabled if
    # neither MAM cookie nor qBit is configured (nothing to retry with).
    if mam_cookie and settings.get("qbit_url"):
        retry_seconds = float(
            settings.get("cookie_retry_interval_seconds", 300)
        )

        async def _cookie_retry_loop_factory():
            await cookie_retry_loop(deps_for_loops, interval_seconds=retry_seconds)

        state._cookie_retry_task = state.supervised_task(
            _cookie_retry_loop_factory, name="cookie-retry"
        )
        _log.info(f"Cookie retry loop started (interval={retry_seconds}s)")
    else:
        _log.info("Cookie retry loop disabled (mam_session_id or qbit_url not configured)")

    # Review-queue auto-add timeout: daily tick that promotes
    # undecided items past their grace period to the sink. Only
    # starts if the review queue is enabled (default True).
    if settings.get("review_queue_enabled", True):
        review_interval = float(
            settings.get("review_timeout_check_interval_seconds", 86400)
        )

        async def _review_timeout_factory():
            await review_timeout_loop(
                deps_for_loops, interval_seconds=review_interval
            )

        state._review_timeout_task = state.supervised_task(
            _review_timeout_factory, name="review-timeout"
        )
        _log.info(
            f"Review-timeout loop started (interval={review_interval}s, "
            f"grace={settings.get('metadata_review_timeout_days', 14)} days)"
        )
    else:
        _log.info("Review-timeout loop disabled (review_queue_enabled=false)")

    # v2.9.0 — format-priority dedup hold-release loop. Wakes any
    # `pending_holds` row whose timer has fired and re-evaluates the
    # dedup decision against current state. Always runs (cheap when
    # there are no holds; the format_priority gate dictates whether
    # holds get created in the first place).
    from app.orchestrator.hold_release import run_loop as hold_release_loop
    hold_release_interval = int(
        settings.get("format_dedup_release_tick_seconds", 60) or 60
    )

    async def _hold_release_factory():
        await hold_release_loop(
            deps_for_loops, interval_seconds=hold_release_interval,
        )

    state._hold_release_task = state.supervised_task(
        _hold_release_factory, name="hold-release",
    )
    _log.info(
        f"Hold-release loop started (interval={hold_release_interval}s)"
    )

    # APScheduler: always construct so discovery-domain interval jobs
    # (library sync + scheduled author lookup) have somewhere to land.
    # Digest jobs only register when daily_digest_enabled + ntfy_url are
    # both set — no point cron-ing notifications into the void.
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler()
    if settings.get("daily_digest_enabled", True) and settings.get("ntfy_url"):
        digest_ctx = DigestContext(
            ntfy_url=settings.get("ntfy_url", ""),
            ntfy_topic=settings.get("ntfy_topic", "seshat"),
            weekly_auto_promote_days=7,
            calibre_library_path=settings.get("calibre_library_path", ""),
        )
        register_digest_jobs(
            scheduler,
            daily_digest_hour=int(settings.get("daily_digest_hour", 9)),
            ctx=digest_ctx,
        )
        _log.info(
            f"Digest jobs registered (daily hour="
            f"{settings.get('daily_digest_hour', 9)}, weekly=Sun 23:30)"
        )
    else:
        _log.info(
            "Digest jobs skipped (daily_digest_enabled=false or ntfy_url empty)"
        )

    # v2.13.0 Stage 6 — weekly Goodreads canary. Unconditional (no
    # opt-out) because the only side effect on a healthy session is
    # one Goodreads request per week + an id_cache prune. ntfy
    # notification on detected soft-block is gated by
    # `notify_on_goodreads_canary_failed` (default True).
    register_goodreads_canary(scheduler)
    _log.info("Goodreads canary registered (Mon 03:00)")

    # v2.21.0 Phase G — metadata-cache worker health + daily summary.
    # Stall watchdog runs every 2 min and fires a Tier-1 ntfy if the
    # worker is enabled but hasn't ticked within
    # `metadata_cache_stall_threshold_s` (default 300s). Daily summary
    # fires once per day at `metadata_cache_daily_summary_hour` (default
    # 9), aggregates today's scan/block counts, then resets the
    # `today_*` columns so the UI shows for-today numbers rather than
    # monotonic-since-deploy.
    register_metadata_cache_health(
        scheduler,
        daily_summary_hour=int(
            settings.get("metadata_cache_daily_summary_hour", 9)
        ),
    )
    _log.info(
        "Metadata-cache health jobs registered (stall watch every 2min, "
        "daily summary hour=%s)",
        settings.get("metadata_cache_daily_summary_hour", 9),
    )

    # ── Discovery domain startup ─────────────────────────────
    # Library discovery, per-library DB init, initial Calibre sync.
    # This runs AFTER the pipeline startup so both domains are live
    # when the app starts serving requests.
    from app.config import discover_libraries
    from app.discovery.database import (
        init_db as init_discovery_db,
        set_active_library,
        get_active_library as get_active_disc_library,
    )
    from app.library_apps import get_app
    from app.discovery.log_buffer import init_log_buffer
    init_log_buffer(capacity=2000)

    state._discovered_libraries = discover_libraries(settings)
    if not state._discovered_libraries:
        _log.info("No libraries configured — discovery features available after setup wizard")
        await init_discovery_db()
        # No libraries means no startup sync to run. Flip the flag
        # immediately so `/scan-status` doesn't report a perpetual
        # "still syncing" state to the SetupWizard frontend.
        state._startup_sync_complete = True
    else:
        lib_names = [l["name"] for l in state._discovered_libraries]
        _log.info(f"Discovered {len(state._discovered_libraries)} libraries: {', '.join(lib_names)}")

        first_slug = state._discovered_libraries[0]["slug"]

        for lib in state._discovered_libraries:
            await init_discovery_db(lib["slug"])

        # v2.20.0 — one-time cross-library author identity migration.
        # Walks every per-library `authors` table and populates
        # `persons` + `author_links` in seshat.db, then consolidates
        # canonical names + flags low-confidence collisions + migrates
        # `pen_name_links` to `pen_name_links_v2`. Idempotent — the
        # migration sentinel skips when every author is already linked.
        # Runs AFTER per-library init_db completes so the per-library
        # schemas are guaranteed to exist before we read them.
        try:
            from app.discovery.author_identity import (
                migrate_to_cross_library_identity,
            )
            lib_slugs = [l["slug"] for l in state._discovered_libraries]
            mig_result = await migrate_to_cross_library_identity(lib_slugs)
            if not mig_result.get("skipped"):
                _log.info(
                    "Cross-library author identity migration: "
                    "%d persons, %d author_links, %d low-confidence, "
                    "%d pen_name_links",
                    mig_result.get("created_persons", 0),
                    mig_result.get("created_links", 0),
                    mig_result.get("low_confidence", 0),
                    mig_result.get("pen_name_migrated", 0),
                )
        except Exception:
            _log.exception(
                "Cross-library author identity migration failed "
                "(non-fatal — per-library writes still work; the "
                "unified author detail page will be empty until "
                "this succeeds)"
            )

        # v2.21.0 Phase B — metadata cache scaffolding.
        # Apply pending schema migrations for the Amazon metadata cache
        # (creates `metadata_cache_amazon.db` and its four tables on
        # first run) and one-shot-backfill the queue with every author
        # already carrying an `amazon_id`. The backfill is idempotent
        # (INSERT OR IGNORE on the queue PK), so subsequent restarts
        # only catch up on authors whose `amazon_id` was resolved after
        # the previous startup. The worker (Phase D) will pop from this
        # queue; nothing actually hits Amazon until that lands.
        try:
            from app.discovery import metadata_cache
            await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
            backfill_counts = await metadata_cache.backfill_amazon_queue_from_authors(
                [l["slug"] for l in state._discovered_libraries],
            )
            total_enqueued = sum(backfill_counts.values())
            if total_enqueued:
                _log.info(
                    "metadata_cache: amazon queue backfill enqueued "
                    "%d new author(s) across %d libraries",
                    total_enqueued, len(backfill_counts),
                )
        except Exception:
            _log.exception(
                "metadata_cache: amazon backfill failed (non-fatal — "
                "the cache DB still initializes; the worker (Phase D) "
                "will rebuild the queue lazily as authors are scanned)"
            )

        # v2.21.0 Phase D — background metadata cache worker.
        # Runs every ~30-90s (jittered) and pops one queue row per
        # iteration: fresh curl_cffi session, behavioral warmup, scan
        # via AmazonSource, cache write. Cooldown / 202 / escalation
        # are honored. Recovery resets stuck `in_progress` rows back
        # to `pending` on startup. Disabled by default — operator
        # opts in via `metadata_cache.amazon.enabled` in settings so a
        # fresh v2.21.0 deploy doesn't blast Akamai immediately.
        try:
            from app.discovery import metadata_cache_worker

            # Phase G — optional rotated log file. No-op when
            # `metadata_cache_log_file_enabled` is False (default).
            log_path = metadata_cache_worker.install_log_file_handler()
            if log_path:
                _log.info(
                    "metadata_cache_worker: rotated log file enabled at %s",
                    log_path,
                )

            async def _metadata_cache_amazon_worker_factory():
                await metadata_cache_worker.run_loop(
                    source_name=metadata_cache.SOURCE_AMAZON,
                )

            state._metadata_cache_amazon_worker_task = state.supervised_task(
                _metadata_cache_amazon_worker_factory,
                name="metadata-cache-amazon-worker",
            )
            _log.info(
                "metadata_cache_worker: amazon worker task spawned "
                "(disabled by default — enable via "
                "metadata_cache.amazon.enabled)"
            )
        except Exception:
            _log.exception(
                "metadata_cache_worker: failed to spawn amazon worker "
                "(non-fatal — the cache reader still serves whatever "
                "is in the cache; new authors just won't get scanned)"
            )

        active = settings.get("active_library") or first_slug
        valid_slugs = [l["slug"] for l in state._discovered_libraries]
        if active not in valid_slugs:
            active = first_slug
        set_active_library(active)
        settings["active_library"] = active
        save_settings(settings)
        _log.info(f"Active library: '{active}'")

        # Initial sync with mtime optimization — moved off the
        # lifespan's critical path. Pre v2.5 this `await`ed the
        # per-library sync loop synchronously, blocking FastAPI's
        # startup completion until every library finished. For Mark's
        # 2862-book Calibre library + 131-item ABS library that meant
        # 30-60 seconds of total webpage unavailability on every
        # container restart, with the user staring at a hung-loading
        # browser tab the entire time. After the incremental-sync
        # work in steps 1-5 most restarts will finish in seconds, but
        # the first-ever sync, post-failure recovery, and weekly
        # safety-net runs still take minutes. Running this as a
        # supervised background task lets FastAPI start accepting
        # requests immediately; the frontend gates its UX via the
        # `startup_complete` field exposed on `/scan-status`.
        from app.discovery import sync_state as _sync_state
        if _sync_state.migrate_settings(settings):
            save_settings(settings)

        async def _run_startup_sync():
            import os as _os
            import time as _time
            try:
                state._library_sync_in_progress = True
                any_synced = False
                # Reload settings inside the task so we see anything
                # the lifespan path saved (e.g. `active_library`).
                inner_settings = load_settings()
                for lib in state._discovered_libraries:
                    slug = lib["slug"]
                    set_active_library(slug)
                    try:
                        lib_app = get_app(lib.get("app_type", "calibre"))
                        current_mtime = (
                            await lib_app.get_mtime(lib)
                            if lib_app
                            else _os.path.getmtime(lib["source_db_path"])
                        )
                        last_mtime = _sync_state.get_state(
                            inner_settings, slug
                        )["last_mtime"]
                        if last_mtime is not None and current_mtime == last_mtime:
                            _log.info(
                                f"Library '{lib['name']}': source unchanged, "
                                "skipping sync"
                            )
                            # Backfill sync_state timestamps if missing
                            # so the next real sync (e.g. after the
                            # user adds a book) goes incremental
                            # rather than falling back to MODE_FULL_FIRST.
                            _sync_state.record_mtime_unchanged(
                                inner_settings, slug, mtime=current_mtime,
                            )
                            save_settings(inner_settings)
                        else:
                            _log.info(f"Library '{lib['name']}': syncing...")
                            sync_result = (
                                await lib_app.sync(lib) if lib_app else None
                            )
                            _sync_state.record_completion(
                                inner_settings, slug,
                                mtime=current_mtime,
                                mode=(sync_result or {}).get("mode", "full"),
                            )
                            save_settings(inner_settings)
                            any_synced = True
                    except Exception as e:
                        _log.warning(
                            f"Sync failed for library '{lib['name']}': {e}"
                        )
                        _sync_state.record_failure(inner_settings, slug)
                        save_settings(inner_settings)
                set_active_library(active)
                state._last_library_sync_check["at"] = _time.time()
                state._last_library_sync_check["synced"] = True
                # Seed `_library_sync_progress` with the startup
                # outcome so the Command Center has a "Last sync"
                # timestamp to display. See scheduled_jobs.py — only
                # `last_check_at` advances on skips.
                if not any_synced:
                    now_ts = _time.time()
                    for lib in state._discovered_libraries:
                        state.get_lib_progress(lib["slug"]).update({
                            "running": False,
                            "status": "complete",
                            "type": "startup_skip",
                            "current": 0,
                            "total": 0,
                            "current_book": "",
                            "last_check_at": now_ts,
                        })
            finally:
                state._library_sync_in_progress = False
                state._startup_sync_complete = True
                _log.info("Startup sync complete; first-boot splash can clear")

            # v2.12.1 #2 — one-shot dual-author-row backfill. Runs
            # AFTER the initial sync so all libraries have their
            # canonical author rows. Idempotent + guarded by a
            # settings flag so subsequent boots skip it. Failure to
            # backfill logs a warning but doesn't fail startup.
            inner_settings = load_settings()
            if not inner_settings.get("v2_12_1_dual_row_backfill_done"):
                try:
                    from app.discovery.author_mirror import (
                        backfill_dual_author_rows,
                    )
                    result = await backfill_dual_author_rows()
                    _log.info(
                        "dual-author-row backfill: checked=%d "
                        "stubs_inserted=%d by_lib=%s",
                        result.get("checked", 0),
                        result.get("stubs_inserted", 0),
                        result.get("by_library", {}),
                    )
                    inner_settings = load_settings()
                    inner_settings["v2_12_1_dual_row_backfill_done"] = True
                    save_settings(inner_settings)
                except Exception:
                    _log.exception(
                        "dual-author-row backfill failed (non-fatal — "
                        "stubs will accrue on next sync via the "
                        "per-author mirror hook in calibre_sync / "
                        "audiobookshelf_sync)"
                    )

        state._startup_sync_task = state.supervised_task(
            _run_startup_sync,
            name="startup-sync",
            restart_on_crash=False,
        )

    # Per-source rate limits and the Hardcover API key are read once
    # into module-level source instances at import time. Refresh them
    # now so the initial sync above, the IRC-triggered author scans,
    # and any dashboard source checks all see user-configured values
    # before the first scheduled lookup fires. run_full_lookup also
    # self-heals via its own reload_sources() call, but that leaves a
    # gap between startup and first scan.
    try:
        from app.discovery.lookup import reload_sources as _reload_sources
        _reload_sources()
    except Exception:
        _log.exception("reload_sources() failed at startup")

    _log.info("Discovery domain initialized")

    # ── Scheduler start + MAM/digest supervised tasks ────────
    # Register discovery interval jobs (library sync, scheduled author
    # lookup) onto the same scheduler used for digests, now that
    # state._discovered_libraries is populated. The MAM scheduler and
    # the discovery-side digest flush loop are supervised_task coroutines
    # rather than APScheduler jobs because their cadence is settings-
    # driven and they need to re-read settings on every tick.
    from app.discovery.scheduled_jobs import (
        add_discovery_jobs,
        mam_scheduler_loop,
    )
    from app.discovery.digest import run_digest_scheduler
    from app.orchestrator.economy_scheduler import (
        upload_autobuy_loop,
        vip_autobuy_loop,
    )
    add_discovery_jobs(scheduler, settings)
    scheduler.start()
    state.scheduler = scheduler

    state._mam_scheduler_task = state.supervised_task(
        mam_scheduler_loop, name="mam-scheduler"
    )
    state._digest_scheduler_task = state.supervised_task(
        run_digest_scheduler, name="digest-scheduler"
    )
    # MAM economy auto-buy loops. Both default-disabled via settings;
    # the tick short-circuits with no audit write when the feature
    # flag is off, so they're cheap to leave running unconditionally.
    state._economy_vip_task = state.supervised_task(
        vip_autobuy_loop, name="economy-vip-autobuy"
    )
    state._economy_upload_task = state.supervised_task(
        upload_autobuy_loop, name="economy-upload-autobuy"
    )
    _log.info(
        "MAM scheduler + digest scheduler + economy auto-buy tasks started"
    )

    try:
        yield
    finally:
        _log.info("Seshat shutting down")

        # Stop accepting new rotation notifications. Any request
        # in flight right now might still try to fire the callback
        # between now and when its response lands, and we don't
        # want that racing against the secret-store flush below or
        # the disk teardown.
        set_rotation_callback(None)

        # If there's a pending debounced rotation waiting to write,
        # cancel the timer so it doesn't sleep for 60s during an
        # otherwise-fast shutdown, then write the pending token to
        # the encrypted store synchronously. This guarantees we never
        # lose the most recent cookie to a shutdown.
        global _rotation_persist_task
        if _rotation_persist_task is not None and not _rotation_persist_task.done():
            _rotation_persist_task.cancel()
            try:
                await _rotation_persist_task
            except (asyncio.CancelledError, Exception):
                pass
            _rotation_persist_task = None
        if _rotation_pending_token:
            try:
                from app.secrets import get_secret, set_secret
                if await get_secret("mam_session_id") != _rotation_pending_token:
                    await set_secret("mam_session_id", _rotation_pending_token)
                    _log.info(
                        "Flushed pending MAM cookie rotation during shutdown"
                    )
            except Exception:
                _log.exception("error flushing pending cookie rotation on shutdown")

        # mbsc rotation flush block was REMOVED in v2.4.0 (TOS).

        # Stop APScheduler before cancelling tasks so its own jobs
        # don't fire during teardown. wait=False so shutdown doesn't
        # block on a currently-running digest send.
        if state.scheduler is not None:
            try:
                state.scheduler.shutdown(wait=False)
            except Exception:
                _log.exception("error stopping APScheduler")
            state.scheduler = None

        # Stop the IRC listener cleanly first so its run_forever
        # loop sees the stop signal and breaks out of any backoff
        # wait, instead of being hard-cancelled mid-handshake.
        if state.irc_client is not None:
            try:
                await state.irc_client.stop()
            except Exception:
                _log.exception("error stopping IRC client during shutdown")

        # Cancel the supervised tasks. supervised_task wraps the
        # coroutines with restart-on-crash logic, so we need to
        # cancel the wrapper task itself — the inner coroutine sees
        # CancelledError and unwinds cleanly. The digest scheduler
        # relies on CancelledError to trigger its final flush — it
        # catches the cancel, drains pending digest events with
        # force=True, and then re-raises — so cancelling it is a
        # feature, not just teardown.
        for task_attr in (
            "_irc_task",
            "_budget_watcher_task",
            "_cookie_keepalive_task",
            "_cookie_retry_task",
            "_review_timeout_task",
            "_mam_scheduler_task",
            "_digest_scheduler_task",
            "_economy_vip_task",
            "_economy_upload_task",
            "_startup_sync_task",
            # v2.21.0 Phase D — Amazon metadata cache worker.
            "_metadata_cache_amazon_worker_task",
        ):
            task = getattr(state, task_attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                setattr(state, task_attr, None)

        # Tear down whatever the dispatcher owns. The qBit client
        # holds an httpx.AsyncClient; the cookie module holds
        # another one. Both expose async close methods that are
        # safe to call multiple times.
        if state.dispatcher is not None:
            try:
                await state.dispatcher.qbit.aclose()
            except Exception:
                _log.exception("error closing qBit client during shutdown")
            enricher = getattr(state.dispatcher, "metadata_enricher", None)
            if enricher is not None:
                try:
                    await enricher.aclose()
                except Exception:
                    _log.exception("error closing metadata enricher")
        try:
            await aclose_session()
        except Exception:
            _log.exception("error closing MAM cookie session during shutdown")
        # The discovery domain holds its own long-lived httpx.AsyncClient
        # for MAM metadata calls — separate from the cookie-module one
        # that aclose_session() above closes. Close it explicitly so we
        # don't leak the transport.
        try:
            from app.discovery.sources.mam import aclose_session as disc_mam_aclose
            await disc_mam_aclose()
        except Exception:
            _log.exception("error closing discovery MAM session during shutdown")
        # Best-effort final flush of any pending discovery digest events
        # so a restart doesn't lose notifications queued during the day.
        # No-op when ntfy_digest_enabled=false (force=True still drains
        # but the queue is empty in that case).
        try:
            from app.discovery.digest import flush_digest
            await flush_digest(force=True)
        except Exception:
            _log.exception("error flushing discovery digest during shutdown")
        # Close the ntfy notifier's httpx client.
        try:
            await ntfy_aclose()
        except Exception:
            _log.exception("error closing ntfy client during shutdown")
        state.dispatcher = None
        state.irc_client = None


app = FastAPI(
    title="Seshat",
    description="Hermes for the meece — MAM courier and Calibre ingest pipeline",
    version="2.2.3",
    lifespan=lifespan,
)


# ─── Authentication middleware ─────────────────────────────────
# Routes that don't require authentication. Everything else under
# /api/ requires a valid session cookie. The frontend SPA bundle
# (anything not under /api/) is always public so the login page
# can render before the user has a session.
_PUBLIC_API_PATHS = frozenset({
    "/api/health",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/check",
})


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforce authentication on protected /api/* routes.

    Requests outside /api/ pass through unchanged so the frontend
    bundle (HTML, JS, CSS, images) loads without a cookie. API
    requests in the public allowlist also pass through. Every other
    API request must carry a valid signed session cookie.

    Also forces `Cache-Control: no-store` on every /api/* response
    so dynamic API payloads never get poisoned by stale cached HTML
    from the SPA fallback.
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        if path in _PUBLIC_API_PATHS:
            response = await call_next(request)
        else:
            token = request.cookies.get(SESSION_COOKIE_NAME, "")
            if verify_session_token(token) is not None:
                response = await call_next(request)
            else:
                response = JSONResponse(
                    status_code=401,
                    content={"detail": "Authentication required"},
                )
        response.headers["Cache-Control"] = "no-store"
        return response


app.add_middleware(AuthMiddleware)


# Auth router is registered first by convention since it gates
# everything else. The other routers are protected by the middleware.
app.include_router(grabs_router)
app.include_router(announces_router)
app.include_router(auth_router)
app.include_router(authors_router)
app.include_router(covers_router)
app.include_router(credentials_router)
app.include_router(data_mgmt_router)
app.include_router(db_editor_router)
app.include_router(delayed_router)
app.include_router(enums_router)
app.include_router(economy_router)
app.include_router(inject_router)
app.include_router(logs_router)
app.include_router(mam_router)
app.include_router(metadata_sources_router)
app.include_router(metadata_cache_router)
app.include_router(goodreads_session_router)
app.include_router(migration_router)
app.include_router(review_router)
app.include_router(search_router)
app.include_router(settings_router)
app.include_router(sse_router)
app.include_router(tentative_router)
app.include_router(works_router)

# ── Discovery domain routers ──────────────────────────────────
app.include_router(disc_books_router)
app.include_router(disc_authors_router)
app.include_router(disc_series_router)
app.include_router(disc_suggestions_router)
app.include_router(disc_metadata_router)
app.include_router(disc_scan_router)
app.include_router(disc_mam_router)
app.include_router(disc_libraries_router)
app.include_router(disc_covers_router)
app.include_router(disc_abs_router)
app.include_router(disc_import_export_router)
app.include_router(disc_config_router)
app.include_router(disc_pipeline_send_router)
app.include_router(disc_reingest_router)
app.include_router(disc_hygiene_router)


@app.get("/api/health")
async def health():
    """Liveness check."""
    return {
        "status": "ok",
        "service": "seshat",
        "dispatcher_ready": state.dispatcher is not None,
    }


# Cached at module load — /app/VERSION is baked into the image at
# Docker build time via `ARG GIT_SHA` (see Dockerfile). Standalone
# / dev runs fall back to "unknown" and the Settings page just
# shows that string instead of a SHA.
_VERSION_FILE = Path(__file__).parent.parent / "VERSION"
try:
    _BUILD_SHA = _VERSION_FILE.read_text().strip() or "unknown"
except Exception:
    _BUILD_SHA = "unknown"


@app.get("/api/version")
async def version():
    """Build identifier for the running container.

    Returns the full git SHA from /app/VERSION (baked at Docker
    build time) plus a 7-char short form suitable for UI display.
    Auth-gated by the same middleware as other /api/* routes.
    """
    short = _BUILD_SHA[:7] if _BUILD_SHA != "unknown" else "unknown"
    return {"sha": _BUILD_SHA, "short_sha": short}


# ─── Frontend SPA serving ──────────────────────────────────────
# Mounts `frontend/dist` if it exists. Anything not under /api/
# falls through to index.html so the SPA router can take over.
# Top-level files (favicon.ico, icon.svg, etc.) are served from a
# whitelist built at startup so user input is only ever used as a
# dict key — path traversal is structurally impossible.
_FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

if _FRONTEND_DIST.exists():
    if (_FRONTEND_DIST / "assets").exists():
        app.mount(
            "/assets",
            StaticFiles(directory=_FRONTEND_DIST / "assets"),
            name="assets",
        )

    _INDEX_HTML = (_FRONTEND_DIST / "index.html").resolve()
    _SERVE_FE_FILES: dict[str, Path] = {
        p.name: p.resolve() for p in _FRONTEND_DIST.iterdir() if p.is_file()
    }

    @app.get("/{path:path}")
    async def serve_fe(path: str):
        """SPA fallback handler.

        Top-level files emitted by vite (index.html, favicon.ico,
        icon.svg, etc.) are served from a startup-computed whitelist.
        Anything else falls through to index.html so the React app
        can take over client-side routing.

        Two important guards:
          1. Paths that look like API calls return a real 404 instead
             of the SPA index. Without this, browsers cache index.html
             against the API URL and silently break polling.
          2. The SPA index is served with `Cache-Control: no-cache` so
             the browser revalidates on every request. Hashed assets
             under /assets/ remain cacheable via StaticFiles.
        """
        if path.startswith("api/") or path == "api":
            raise HTTPException(status_code=404, detail="Not Found")
        safe_file = _SERVE_FE_FILES.get(path)
        if safe_file is not None:
            return FileResponse(safe_file)
        return FileResponse(_INDEX_HTML, headers={"Cache-Control": "no-cache"})
else:
    @app.get("/")
    async def serve_fe_missing():
        return {
            "error": "Frontend not built. Run "
                     "'cd frontend && npm install && npm run build' first.",
        }
