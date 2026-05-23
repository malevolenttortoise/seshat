"""
MAM user status + cookie management endpoints.

    GET  /api/v1/mam/status      — ratio, wedges, class, seedbonus,
                                    plus current cookie freshness
    POST /api/v1/mam/refresh     — force a fresh fetch (bypass cache)
    POST /api/v1/mam/validate    — run the cookie validation flow and
                                    record the result on settings.json
    POST /api/v1/mam/cookie      — emergency paste: replace the live
                                    cookie with a fresh one and validate

The status endpoint never raises on a stale or missing cookie — it
returns a payload with `cookie_configured: false` or `error: "..."` so
the UI can render an actionable banner instead of a 500 page. Useful
for the dashboard's "session expiring soon" surface.

Why this is its own router rather than living under /settings: the
operator-facing flow is "see ratio + wedges + run a validation",
which is a different mental model from "edit configuration values".
Keeping them split lets the dashboard hit `/api/v1/mam/status` on a
poll without colliding with settings reads.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import load_settings, save_settings
from app.mam import cookie as mam_cookie
from app.mam.user_status import (
    UserStatusError,
    get_user_status,
    invalidate_cache,
)

_log = logging.getLogger("seshat.routers.mam")

router = APIRouter(prefix="/api/v1/mam", tags=["mam"])


class MamStatusResponse(BaseModel):
    cookie_configured: bool
    cookie_age_seconds: Optional[float] = None
    last_validated_at: Optional[str] = None
    validation_ok: bool = False
    username: Optional[str] = None
    uid: Optional[int] = None
    classname: Optional[str] = None
    ratio: Optional[float] = None
    wedges: Optional[int] = None
    seedbonus: Optional[float] = None
    uploaded_bytes: Optional[int] = None
    downloaded_bytes: Optional[int] = None
    error: Optional[str] = None


class ValidateResponse(BaseModel):
    ok: bool
    message: str


class CookieRequest(BaseModel):
    cookie: str


async def _build_status(
    *, force_refresh: bool = False
) -> MamStatusResponse:
    settings = load_settings()
    token = await mam_cookie.get_active_token()
    last_validated_at = settings.get("mam_last_validated_at")
    validation_ok = bool(settings.get("mam_validation_ok"))

    if not token:
        return MamStatusResponse(
            cookie_configured=False,
            last_validated_at=last_validated_at,
            validation_ok=False,
            error="No MAM session cookie configured",
        )

    cookie_age = None
    if last_validated_at:
        try:
            cookie_age = max(
                0.0,
                time.time()
                - time.mktime(time.strptime(last_validated_at, "%Y-%m-%dT%H:%M:%S")),
            )
        except (ValueError, TypeError):
            cookie_age = None

    ttl = 0 if force_refresh else 300  # default 5min cache
    try:
        status = await get_user_status(token=token, ttl=ttl)
    except UserStatusError as e:
        return MamStatusResponse(
            cookie_configured=True,
            last_validated_at=last_validated_at,
            validation_ok=validation_ok,
            cookie_age_seconds=cookie_age,
            error=str(e),
        )

    return MamStatusResponse(
        cookie_configured=True,
        cookie_age_seconds=cookie_age,
        last_validated_at=last_validated_at,
        validation_ok=True,
        username=status.username,
        uid=status.uid,
        classname=status.classname,
        ratio=status.ratio,
        wedges=status.wedges,
        seedbonus=status.seedbonus,
        uploaded_bytes=status.uploaded_bytes,
        downloaded_bytes=status.downloaded_bytes,
    )


@router.get("/status", response_model=MamStatusResponse)
async def status() -> MamStatusResponse:
    """Cached MAM user status. ~5min freshness window."""
    return await _build_status()


# /mbsc-status endpoint was REMOVED in v2.4.0 — TOS-disallowed mbsc
# scraping no longer ships. See feedback_mam_mbsc_filelist_tos.md.


@router.post("/refresh", response_model=MamStatusResponse)
async def refresh() -> MamStatusResponse:
    """Force a fresh fetch from MAM, bypassing the in-memory cache."""
    invalidate_cache()
    return await _build_status(force_refresh=True)


@router.post("/validate", response_model=ValidateResponse)
async def validate() -> ValidateResponse:
    """Run the full cookie validation (IP registration + session probe).

    Updates `mam_last_validated_at` and `mam_validation_ok` in
    settings.json based on the outcome so the dashboard banner clears
    automatically when the user re-validates after a refresh.
    """
    settings = load_settings()
    token = await mam_cookie.get_active_token()

    result = await mam_cookie.validate(token)
    ok = bool(result.get("success"))
    message = str(result.get("message") or "")

    settings = dict(settings)
    settings["mam_last_validated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    settings["mam_validation_ok"] = ok
    save_settings(settings)

    return ValidateResponse(ok=ok, message=message)


@router.post("/cookie", response_model=ValidateResponse)
async def replace_cookie(body: CookieRequest) -> ValidateResponse:
    """Emergency cookie paste.

    Updates the in-memory token, persists it to settings.json, and
    runs validate() so the response carries an immediate yes/no on
    whether the new cookie works. Useful when MAM has rotated and
    Seshat's auto-rotation didn't catch it (e.g. after a long
    quiet period that exceeded the keep-alive interval).
    """
    cookie = (body.cookie or "").strip()
    if not cookie:
        raise HTTPException(400, "Cookie cannot be empty")
    if len(cookie) < 32:
        raise HTTPException(400, "Cookie looks too short to be valid")

    # Persist + load into the in-memory token immediately. Subsequent
    # MAM API calls (validate, get_user_status, future grab fetches)
    # will pick this up.
    mam_cookie.set_current_token(cookie)
    invalidate_cache()
    settings = dict(load_settings())
    settings["mam_session_id"] = cookie
    save_settings(settings)

    # Run validation and stamp the result.
    result = await mam_cookie.validate(cookie)
    ok = bool(result.get("success"))
    message = str(result.get("message") or "")

    settings = dict(load_settings())
    settings["mam_last_validated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    settings["mam_validation_ok"] = ok
    save_settings(settings)

    if ok:
        _log.info("MAM cookie replaced and validated via UI emergency paste")
    else:
        _log.warning("MAM cookie replaced via UI but validation FAILED: %s", message)
    return ValidateResponse(ok=ok, message=message)


@router.get("/debug-match")
async def debug_match(
    title: str,
    author: str,
    series: str = "",
    content_type: str = "ebook",
    seshat_cover_phash: Optional[str] = None,
    book_id: Optional[int] = None,
    slug: Optional[str] = None,
    seshat_cover_url: Optional[str] = None,
    test_scoped: bool = False,
) -> dict:
    """Toggle-gated developer endpoint: replay the MAM cascade for one book
    and return a structured trace (raw response shape + per-result scoring
    breakdown + decision per result).

    Off by default — enabled via Settings → Operational. Used to diagnose
    "Possible" matches that are actually correct (and the inverse) and to
    verify field names in MAM's response shape when refining scoring logic.

    **Part C cover-verification surface.** Pass any ONE of three inputs to
    enable per-candidate cover-pHash distance comparisons in the trace
    (resolution priority: direct → book lookup → URL fetch):

      - `seshat_cover_phash` — 16-char hex pHash, used directly. Easiest
        for repeated testing once you've hashed an image once.
      - `book_id` (+ optional `slug`) — looks up the book in the chosen
        per-library DB. Tries `cover_phash` column first, falls back to
        hashing the local `cover_path`, then fetching `cover_url`. The
        most ergonomic path once step 3 backfill ships.
      - `seshat_cover_url` — fetches the URL through the cookie-aware
        client (works for MAM CDN URLs as well as external sources)
        and hashes the bytes.

    The resolution path lands in `trace.cover_input.source` so callers
    can see which input drove the comparison. Cover signals appear on
    each result as `cover_check: {distance, signal, mam_phash}`.
    """
    settings = load_settings()
    if not settings.get("mam_debug_match_enabled"):
        raise HTTPException(
            403,
            "MAM debug-match is disabled. Enable it under "
            "Settings → Operational → MAM Debug Match.",
        )
    if not title or not author:
        raise HTTPException(400, "Both 'title' and 'author' are required")
    if content_type not in ("ebook", "audiobook"):
        raise HTTPException(400, "content_type must be 'ebook' or 'audiobook'")

    token = await mam_cookie.get_active_token()
    if not token:
        raise HTTPException(400, "No MAM cookie configured")

    # Resolve seshat_cover_phash from one of the three optional inputs.
    # Resolution metadata threads back to the caller via the trace's
    # cover_input block so the endpoint user can see which path fired.
    resolved_phash: Optional[str] = None
    cover_resolution: dict = {"source": None, "error": None}
    if seshat_cover_phash:
        resolved_phash = seshat_cover_phash.strip().lower()
        cover_resolution["source"] = "direct"
    elif book_id is not None:
        try:
            resolved_phash = await _resolve_phash_from_book_lookup(
                book_id=book_id, slug=slug, token=token,
                resolution_meta=cover_resolution,
            )
        except Exception as e:
            cover_resolution["error"] = f"book_lookup_failed: {e}"
            _log.exception("cover phash book lookup failed")
    elif seshat_cover_url:
        try:
            resolved_phash = await _resolve_phash_from_url(
                url=seshat_cover_url, token=token,
                resolution_meta=cover_resolution,
            )
        except Exception as e:
            cover_resolution["error"] = f"url_fetch_failed: {e}"
            _log.exception("cover phash url fetch failed")

    from app.discovery.sources.mam import debug_check_book

    try:
        trace = await debug_check_book(
            token=token,
            title=title,
            authors=author,
            series_name=series,
            content_type=content_type,
            seshat_cover_phash=resolved_phash,
            test_scoped=test_scoped,
        )
    except Exception as e:
        _log.exception("debug_match cascade failed")
        raise HTTPException(500, f"Debug match failed: {e}")

    # Surface resolution metadata even when the caller passed nothing —
    # makes the trace self-describing for diagnostic export.
    trace["cover_input"]["resolution"] = cover_resolution
    return trace


async def _resolve_phash_from_book_lookup(
    *,
    book_id: int,
    slug: Optional[str],
    token: str,
    resolution_meta: dict,
) -> Optional[str]:
    """Look up book in per-library DB; resolve cover_phash via fallback chain.

    Resolution chain:
      1. `books.cover_phash` (after step 3 backfill) — already hashed
      2. `books.cover_path` (local file on disk, e.g. Calibre cover)
      3. `books.cover_url` (external URL, fetched via auth-aware client)

    Updates `resolution_meta` in place with `source`, `book_id`, `slug`,
    and any of `cover_path` / `cover_url` that drove the resolution.
    """
    from app.discovery.database import get_db
    from app.mam.cover_hash import hash_image_file

    db = await get_db(slug=slug)
    try:
        row = await (await db.execute(
            "SELECT cover_phash, cover_path, cover_url "
            "FROM books WHERE id = ?",
            (int(book_id),),
        )).fetchone()
    finally:
        await db.close()

    resolution_meta["book_id"] = book_id
    resolution_meta["slug"] = slug
    if not row:
        resolution_meta["error"] = f"book {book_id} not found in slug={slug or '(active)'}"
        return None

    # Tier 1: pre-computed phash on the row (post-step-3 path)
    if row["cover_phash"]:
        resolution_meta["source"] = "book_lookup_phash"
        return str(row["cover_phash"]).strip().lower()

    # Tier 2: local cover file
    if row["cover_path"]:
        h = hash_image_file(row["cover_path"])
        if h:
            resolution_meta["source"] = "book_lookup_path"
            resolution_meta["cover_path"] = row["cover_path"]
            return h
        resolution_meta["error"] = f"cover_path hash failed: {row['cover_path']}"

    # Tier 3: remote cover_url
    if row["cover_url"]:
        h = await _resolve_phash_from_url(
            url=row["cover_url"], token=token,
            resolution_meta=resolution_meta,
        )
        if h:
            # Override source to indicate the indirection path.
            resolution_meta["source"] = "book_lookup_url"
            return h

    if not resolution_meta.get("error"):
        resolution_meta["error"] = "book has no cover_phash / cover_path / cover_url"
    return None


async def _resolve_phash_from_url(
    *,
    url: str,
    token: str,
    resolution_meta: dict,
) -> Optional[str]:
    """Fetch URL via auth-aware client (MAM CDN-compatible) and hash bytes."""
    from app.mam.cookie import _do_get, _is_mam_url
    from app.mam.cover_hash import hash_image_bytes

    resolution_meta["cover_url"] = url
    if _is_mam_url(url):
        resp = await _do_get(url, token=token, timeout=15)
    else:
        # External source (Goodreads / Hardcover / etc.) — no MAM auth needed.
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
    if resp.status_code != 200:
        resolution_meta["error"] = f"url fetch HTTP {resp.status_code}"
        return None
    h = hash_image_bytes(resp.content)
    if not h:
        resolution_meta["error"] = "url fetch decoded but pHash failed"
        return None
    resolution_meta["source"] = resolution_meta.get("source") or "url_fetch"
    return h


@router.post("/test-notification", response_model=ValidateResponse)
async def test_notification() -> ValidateResponse:
    """Send a test notification via ntfy to verify the topic works."""
    from app.notify import ntfy
    settings = load_settings()
    url = settings.get("ntfy_url", "") or ""
    topic = settings.get("ntfy_topic", "") or ""
    if not url:
        return ValidateResponse(ok=False, message="ntfy URL not configured")
    if not topic:
        return ValidateResponse(ok=False, message="ntfy topic not configured")
    ok = await ntfy.send(
        url=url, topic=topic,
        title="Seshat Test",
        message="If you see this, ntfy notifications are working!",
        tags=["white_check_mark", "seshat"],
    )
    return ValidateResponse(
        ok=ok,
        message="Test notification sent!" if ok else "Failed to send — check URL and topic",
    )
