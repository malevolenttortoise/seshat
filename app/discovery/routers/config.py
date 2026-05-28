"""
App-level config and status endpoints.

  GET  /api/settings        — current saved settings (DEFAULT_SETTINGS
                              merged with user overrides)
  POST /api/settings        — partial update; merged into settings.json
  POST /api/settings/reset  — wipe back to defaults
  GET  /api/health          — liveness probe (public, no auth)
  GET  /api/platform        — runtime mode + OS info for the setup wizard
  GET  /api/stats           — Dashboard stats (counts, last sync time, etc.)
"""
import logging
import time
from pathlib import Path
from fastapi import APIRouter, Body

from app.config import (
    LANGUAGE_OPTIONS,
    load_settings,
    save_settings,
    apply_logging,
    get_extra_mount_paths,
)
from app.discovery.database import get_db, get_active_library, HF
from app.discovery.lookup import reload_sources
from app.discovery.sources.mam import get_mam_stats
from app import state

logger = logging.getLogger("seshat.discovery")

router = APIRouter(prefix="/api/discovery", tags=["config"])


# ─── Settings ────────────────────────────────────────────────
@router.get("/settings")
async def get_settings():
    from app.secrets import get_secret, SECRET_KEYS
    s = load_settings()
    d = dict(s)
    # Check the encrypted store for secret keys — if present there,
    # the settings.json value is stale/blank (migration already ran).
    for key in SECRET_KEYS:
        encrypted_val = await get_secret(key)
        if encrypted_val:
            d[f"{key}_set"] = True
            d[key] = encrypted_val[:8] + "..." if len(encrypted_val) > 8 else "***"
        elif d.get(key):
            # Fallback: still in settings.json (pre-migration)
            d[f"{key}_set"] = True
            d[key] = d[key][:8] + "..."
        else:
            d[f"{key}_set"] = False
    d["language_options"] = LANGUAGE_OPTIONS
    d["_extra_mount_paths"] = get_extra_mount_paths()
    d["_discovered_libraries"] = [
        {"name": l["name"], "slug": l["slug"],
         "app_type": l.get("app_type", "calibre"),
         "content_type": l.get("content_type", "ebook"),
         "source_db_path": l["source_db_path"],
         "active": l["slug"] == get_active_library()}
        for l in state._discovered_libraries
    ]
    return d


@router.post("/settings")
async def update_settings(body: dict = Body(...)):
    """Partial settings update — used by the setup wizard.

    Delegates to `apply_settings_patch` so this endpoint shares
    identical validation and post-save hooks with the main
    `PATCH /api/v1/settings` endpoint.
    """
    from app.routers.settings import apply_settings_patch
    result = await apply_settings_patch(body)
    return {
        "status": "ok",
        "updated": result.updated,
        "rejected": result.rejected,
    }


@router.post("/settings/reset")
async def reset_settings():
    """Reset all settings to factory defaults."""
    from app.config import DEFAULT_SETTINGS
    fresh = dict(DEFAULT_SETTINGS)
    save_settings(fresh)
    reload_sources()
    apply_logging(False)
    logger.info("All settings reset to defaults")
    return {"status": "ok"}


# ─── Health & Stats ──────────────────────────────────────────
@router.get("/health")
async def health():
    return {"status": "ok", "time": time.time()}


@router.get("/version")
async def version_info():
    """Return the build version (git SHA) baked into the Docker image."""
    from pathlib import Path
    version_file = Path("/app/VERSION")
    sha = version_file.read_text().strip() if version_file.exists() else "dev"
    return {"sha": sha, "short_sha": sha[:7] if len(sha) > 7 else sha}


@router.get("/platform")
async def platform_info():
    """Return platform/runtime info for the frontend.

    Used by the setup wizard to detect first-run state, suggest
    library paths, and adapt UI to the runtime environment.
    """
    from app.runtime import get_platform_info
    info = get_platform_info()
    s = load_settings()
    # First run: no libraries discovered AND no user-configured sources AND setup not completed
    info["first_run"] = (
        not state._discovered_libraries
        and not s.get("library_sources")
        and not s.get("setup_complete")
    )
    # Check which suggested default paths actually exist on this system
    info["existing_default_paths"] = [
        p for p in info["default_library_paths"]
        if Path(p["path"]).exists()
    ]
    return info


@router.get("/stats")
async def get_stats(slug: str | None = None):
    """Return per-library stats used by the Dashboard.

    `slug` targets a specific discovered library; omitted falls back
    to the active library (back-compat with callers that assume a
    single active DB). The Dashboard calls this once per discovered
    library so the Athena widget can show Calibre + ABS rows with
    independent owned/MAM/audiobook counts side-by-side.

    When the queried library is an audiobook backend, the response
    also includes `narrator_count`, `total_duration_sec`,
    `abridged_count`, and `unabridged_count` — null-safe columns that
    are populated by ABS sync and left null by Calibre sync.
    """
    target_slug = slug or get_active_library()
    db = await get_db(target_slug)
    try:
        g = lambda sql: db.execute(sql)
        # Match the Authors page browse view (routers/authors.py:get_authors)
        # by excluding orphans. Otherwise the Dashboard's author count is
        # higher than the Authors page list and looks like a bug to users.
        authors = (await (await g("SELECT COUNT(*) c FROM authors WHERE id IN (SELECT DISTINCT author_id FROM book_authors)")).fetchone())["c"]
        total = (await (await g(f"SELECT COUNT(*) c FROM books b WHERE {HF}")).fetchone())["c"]
        owned = (await (await g(f"SELECT COUNT(*) c FROM books b WHERE owned=1 AND {HF}")).fetchone())["c"]
        missing = (await (await g(f"SELECT COUNT(*) c FROM books b WHERE owned=0 AND {HF}")).fetchone())["c"]
        new = (await (await g(f"SELECT COUNT(*) c FROM books b WHERE is_new=1 AND owned=0 AND {HF}")).fetchone())["c"]
        upcoming = (await (await g(f"SELECT COUNT(*) c FROM books b WHERE is_unreleased=1 AND owned=0 AND {HF}")).fetchone())["c"]
        series = (await (await g("SELECT COUNT(*) c FROM series")).fetchone())["c"]
        hidden = (await (await g("SELECT COUNT(*) c FROM books WHERE hidden=1")).fetchone())["c"]
        # Pull the most recent library-sync row from sync_log. The set
        # of "library sync" types is whatever's currently registered in
        # the library_apps registry, so this query stays correct as new
        # backends land — no future code change needed.
        from app.library_apps import get_all_apps
        lib_types = list(get_all_apps().keys())
        if lib_types:
            placeholders = ",".join("?" * len(lib_types))
            ls = await (await db.execute(
                f"SELECT * FROM sync_log WHERE sync_type IN ({placeholders}) "
                f"ORDER BY started_at DESC LIMIT 1",
                lib_types,
            )).fetchone()
        else:
            ls = None
        ll = await (await g("SELECT * FROM sync_log WHERE sync_type='lookup' ORDER BY started_at DESC LIMIT 1")).fetchone()
        s = load_settings()
        mam_stats = None
        from app.discovery.routers.mam import _mam_ready
        if await _mam_ready(s):
            mam_stats = await get_mam_stats(db)
        lib_info = next((l for l in state._discovered_libraries if l["slug"] == target_slug), None)
        content_type = lib_info.get("content_type", "ebook") if lib_info else "ebook"
        app_type = lib_info.get("app_type", "calibre") if lib_info else "calibre"

        # Audiobook-specific aggregates. Columns `narrator`,
        # `duration_sec`, `abridged` are only populated for books
        # sourced from an audiobook backend (ABS sync). For ebook
        # libraries these return zeros naturally, so we skip the
        # queries entirely rather than returning misleading zeros.
        narrator_count = None
        total_duration_sec = None
        abridged_count = None
        unabridged_count = None
        if content_type == "audiobook":
            row = await (await g(
                "SELECT COUNT(DISTINCT narrator) c FROM books "
                "WHERE narrator IS NOT NULL AND narrator != '' AND owned=1 AND hidden=0"
            )).fetchone()
            narrator_count = row["c"] if row else 0
            row = await (await g(
                "SELECT COALESCE(SUM(duration_sec),0) s FROM books "
                "WHERE owned=1 AND hidden=0"
            )).fetchone()
            total_duration_sec = row["s"] if row else 0
            row = await (await g(
                "SELECT COUNT(*) c FROM books WHERE abridged=1 AND owned=1 AND hidden=0"
            )).fetchone()
            abridged_count = row["c"] if row else 0
            row = await (await g(
                "SELECT COUNT(*) c FROM books WHERE abridged=0 AND owned=1 AND hidden=0"
            )).fetchone()
            unabridged_count = row["c"] if row else 0

        return {
            "authors": authors, "total_books": total, "owned_books": owned,
            "missing_books": missing, "new_books": new, "upcoming_books": upcoming,
            "total_series": series, "hidden_books": hidden,
            "last_library_sync": dict(ls) if ls else None,
            "last_lookup": dict(ll) if ll else None,
            "calibre_web_url": s.get("calibre_web_url", ""),
            "calibre_url": s.get("calibre_url", ""),
            "abs_web_url": s.get("abs_web_url", ""),
            "mam": mam_stats,
            "mam_enabled": s.get("mam_enabled", False),
            "mam_scanning_enabled": s.get("mam_scanning_enabled", True),
            "author_scanning_enabled": s.get("author_scanning_enabled", True),
            "library_slug": target_slug,
            "library_name": lib_info["name"] if lib_info else target_slug,
            "library_display_name": (lib_info or {}).get("display_name") or (lib_info or {}).get("name") or target_slug,
            "content_type": content_type,
            "app_type": app_type,
            # Back-compat aliases — callers that predate the multi-library
            # refactor still read the `active_*` fields. Kept until Phase 8
            # cleanup retires the old consumers.
            "active_library": target_slug,
            "active_library_name": lib_info["name"] if lib_info else target_slug,
            "active_content_type": content_type,
            "active_app_type": app_type,
            "library_count": len(state._discovered_libraries),
            "narrator_count": narrator_count,
            "total_duration_sec": total_duration_sec,
            "abridged_count": abridged_count,
            "unabridged_count": unabridged_count,
            "last_library_sync_check": state._last_library_sync_check,
        }
    finally:
        await db.close()
