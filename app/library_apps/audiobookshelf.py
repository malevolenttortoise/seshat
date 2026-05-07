"""
Audiobookshelf Library App — adapter for ABS audiobook management.

ABS is API-backed, not file-backed. Discovery hits the ABS REST API
to enumerate libraries (filtered to `mediaType=book` — podcasts are
ignored by design; Seshat is a book app). Sync paginates through
`/api/libraries/{id}/items` and upserts into Seshat's discovery DB
with audiobook-specific fields populated (narrator, duration_sec,
abridged, asin, audio_formats).

Credentials live in the encrypted secrets store (`abs_api_key`) and
settings.json (`abs_url`). Neither is ever written to `library_sources`
so a settings.json dump never leaks the bearer token.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.library_apps.base import LibraryApp

logger = logging.getLogger("seshat.library_apps.audiobookshelf")


class AudiobookshelfApp(LibraryApp):
    """Audiobookshelf audiobook library source."""

    app_type = "audiobookshelf"
    content_type = "audiobook"
    display_name = "Audiobookshelf"
    # No sentinel file — discovery is API-based.
    db_filename = ""
    # ABS_URL seeds the base URL on first run; after that settings.json
    # is source of truth. No env var for the API key (secrets only).
    env_root_var = ""
    env_extra_var = ""

    def get_root_path(self) -> str:
        """Return the configured ABS base URL (settings-first, env fallback)."""
        from app.config import load_settings
        import os
        settings = load_settings()
        url = settings.get("abs_url", "") or os.getenv("ABS_URL", "")
        return url.rstrip("/") if url else ""

    def discover(self, root_path: str) -> list:
        """Discover ABS libraries via `/api/libraries`.

        Called synchronously by `discover_libraries()`; uses a blocking
        httpx request because the base `discover()` is synchronous.
        Podcasts are filtered out — we only want `mediaType=book`.

        Returns [] if ABS is unreachable or no API key is configured.
        The caller already handles an empty list gracefully (same code
        path as "no calibre library found").
        """
        if not root_path:
            return []

        from app.config import slugify

        api_key = _get_abs_api_key_sync()
        if not api_key:
            logger.warning("Audiobookshelf: no API key configured, skipping discovery")
            return []

        client = AudiobookshelfClient(root_path, api_key)
        try:
            libraries_json = client.list_libraries_sync()
        except Exception as e:
            logger.warning(f"Audiobookshelf: discovery failed ({type(e).__name__}): {e}")
            return []

        libraries: list[dict] = []
        seen_slugs: set[str] = set()
        for lib in libraries_json:
            if lib.get("mediaType") != "book":
                continue  # Ignore podcast libraries.
            name = lib.get("name") or f"abs-{lib.get('id', '')[:8]}"
            slug = slugify(f"abs-{name}")
            base_slug = slug
            counter = 2
            while slug in seen_slugs:
                slug = f"{base_slug}-{counter}"
                counter += 1
            seen_slugs.add(slug)
            # First folder's fullPath is the on-disk library root inside
            # the ABS container. Seshat never reads it directly (covers
            # come via the API), but we stash it so the frontend can
            # show the user where ABS is watching.
            folders = lib.get("folders") or []
            library_path = folders[0].get("fullPath", "") if folders else ""
            libraries.append({
                "name": name,
                "slug": slug,
                "app_type": self.app_type,
                "content_type": self.content_type,
                "display_name": self.display_name,
                # No local file — `source_db_path` stays empty for ABS.
                # Callers that still use it for mtime checks go through
                # `get_mtime(lib)` which we override below.
                "source_db_path": "",
                "library_path": library_path,
                "abs_library_id": lib.get("id"),
                "abs_base_url": root_path,
                "abs_last_update": lib.get("lastUpdate", 0),
            })
        return libraries

    async def sync(self, library: dict) -> dict:
        from app.discovery.audiobookshelf_sync import sync_audiobookshelf
        return await sync_audiobookshelf(library)

    def get_cover_path(self, book_path: str, library_path: str) -> Optional[str]:
        """ABS covers are fetched via the API, not from a local path."""
        return None

    async def get_mtime(self, library: dict) -> float | str:
        """ABS change-detection signal: `f"{lastUpdate}:{numItems}"`.

        Pre-v2.3.4.1 this returned `lastUpdate` alone. UAT 2026-05-07
        revealed that ABS's `lastUpdate` advances on library settings
        changes but NOT reliably when items are added — Mark added 5
        audiobooks, ABS UI showed 113 items, but `lastUpdate` was
        17 days stale. Scheduled sync read the same `lastUpdate` on
        every tick and skipped, even though the library content had
        grown.

        The fix: combine `lastUpdate` with the library's total item
        count (via `/api/libraries/{id}/items?limit=0`). Either signal
        moving forces a re-sync. Composite returned as a string —
        `sync_all_libraries`'s equality check doesn't care about type.

        Self-heals across the v2.3.4.0 → v2.3.4.1 boundary: the saved
        `library_mtimes[slug]` is a number from before the fix; the
        new return is a string; the `==` comparison fails; one
        re-sync runs and stores the new string format.

        Falls back to the previous return shape on API failure so a
        transient ABS outage doesn't trigger a fake re-sync. The
        cached `library["abs_last_update"]` is updated in-place on
        success.
        """
        cached = library.get("abs_last_update")
        try:
            cached_f = float(cached) if cached is not None else 0.0
        except (TypeError, ValueError):
            cached_f = 0.0

        base_url = library.get("abs_base_url")
        lib_id = library.get("abs_library_id")
        if not base_url or not lib_id:
            return cached_f

        api_key = await _get_abs_api_key()
        if not api_key:
            return cached_f

        try:
            client = AudiobookshelfClient(base_url, api_key)
            libs = await client.list_libraries()
        except Exception as e:
            logger.warning(
                "Audiobookshelf: get_mtime API fetch failed (%s: %s); "
                "falling back to cached value", type(e).__name__, e,
            )
            return cached_f

        fresh_f = cached_f
        for lib in libs:
            if lib.get("id") == lib_id:
                fresh = lib.get("lastUpdate")
                try:
                    fresh_f = float(fresh) if fresh is not None else 0.0
                except (TypeError, ValueError):
                    fresh_f = 0.0
                library["abs_last_update"] = fresh_f
                break
        else:
            # Library no longer exists in ABS — bail with the cached
            # value. We don't want to wipe Seshat state on a transient
            # API misreport.
            return cached_f

        # Second signal: total item count via the items endpoint.
        # `limit=0` returns metadata only — `total` is the item count.
        # Failure here is non-fatal: degrade to lastUpdate-only (the
        # pre-v2.3.4.1 behavior) rather than blocking the sync.
        try:
            page = await client.list_items(lib_id, limit=0, page=0)
            total_items = int(page.get("total") or 0)
        except Exception as e:
            logger.warning(
                "Audiobookshelf: get_mtime item-count fetch failed "
                "(%s: %s); using lastUpdate alone",
                type(e).__name__, e,
            )
            return fresh_f

        return f"{fresh_f}:{total_items}"


async def _get_abs_api_key() -> Optional[str]:
    """Read the ABS API key from the encrypted secrets store."""
    from app.secrets import get_secret
    return await get_secret("abs_api_key")


def _get_abs_api_key_sync() -> Optional[str]:
    """Blocking read of `abs_api_key` from the encrypted secrets store.

    Used by `AudiobookshelfApp.discover()` which runs in a synchronous
    context (called from `discover_libraries()` during startup and
    from the config router). Mirrors the async `get_secret` logic
    without the aiosqlite dependency: plain sqlite3 + Fernet decrypt.
    """
    import base64
    import hashlib
    import sqlite3
    from cryptography.fernet import Fernet, InvalidToken
    from app.auth_db import get_auth_db_path
    from app.auth_secret import get_auth_secret

    path = get_auth_db_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT value FROM secrets WHERE key = ?", ("abs_api_key",)
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # secrets table doesn't exist yet (fresh install before init).
        return None
    if row is None:
        return None
    try:
        digest = hashlib.sha256(get_auth_secret().encode("utf-8")).digest()
        fernet = Fernet(base64.urlsafe_b64encode(digest))
        return fernet.decrypt(str(row["value"]).encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception):
        logger.warning("Audiobookshelf: failed to decrypt abs_api_key")
        return None


# ─── HTTP client ───────────────────────────────────────────────

class AudiobookshelfClient:
    """Thin ABS REST API client.

    Async methods are used by the sync path; `*_sync` helpers exist for
    the discover path which is called from a synchronous context.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    # ── Headers ───────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    # ── Sync helpers (used by discover) ───────────────────────
    def list_libraries_sync(self) -> list[dict]:
        """Blocking GET /api/libraries."""
        import httpx
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(
                f"{self.base_url}/api/libraries",
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json() or {}
            return data.get("libraries", []) or []

    # ── Async helpers (used by sync) ──────────────────────────
    async def list_libraries(self) -> list[dict]:
        import httpx
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/api/libraries",
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json() or {}
            return data.get("libraries", []) or []

    async def list_items(
        self,
        library_id: str,
        *,
        limit: int = 500,
        page: int = 0,
    ) -> dict:
        """GET /api/libraries/{id}/items — paginated.

        Returns the raw page dict with keys:
          results (list of item dicts), total, limit, page, offset
        """
        import httpx
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/api/libraries/{library_id}/items",
                headers=self._headers(),
                params={"limit": limit, "page": page},
            )
            resp.raise_for_status()
            return resp.json() or {}

    async def iter_all_items(self, library_id: str, *, page_size: int = 500):
        """Yield every item across all pages. Stops when offset+len >= total."""
        page = 0
        while True:
            data = await self.list_items(library_id, limit=page_size, page=page)
            results = data.get("results") or []
            for item in results:
                yield item
            total = int(data.get("total") or 0)
            offset = int(data.get("offset") or 0)
            if not results or offset + len(results) >= total:
                break
            page += 1

    async def trigger_scan(self, library_id: str) -> bool:
        """POST /api/libraries/{id}/scan — ask ABS to rescan the folder.

        Called from the sink path after dropping a new audiobook into
        the library directory. Returns True on 2xx, False otherwise.
        """
        import httpx
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/api/libraries/{library_id}/scan",
                    headers=self._headers(),
                )
                return 200 <= resp.status_code < 300
            except httpx.HTTPError:
                return False
