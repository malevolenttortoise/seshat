"""
Audiobookshelf setup helpers — connection test + library enumeration.

The Settings → Audiobookshelf section calls this to let the user
verify their ABS URL + API token without dropping into logs. Returns
the list of `mediaType=book` libraries so the UI can populate the
sink-target picker with real UUIDs.

    POST /api/discovery/audiobookshelf/test

Reads `abs_url` from settings and `abs_api_key` from the encrypted
secrets store (same plumbing the sync uses). Never exposes the API
token in responses.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.config import load_settings, ui_error
from app.library_apps.audiobookshelf import AudiobookshelfClient
from app.secrets import get_secret

_log = logging.getLogger("seshat.discovery.audiobookshelf")

router = APIRouter(
    prefix="/api/discovery/audiobookshelf",
    tags=["audiobookshelf"],
)


@router.post("/test")
async def test_connection() -> dict:
    """Ping ABS + list book libraries.

    Returns {"ok": bool, "libraries"?: [...], "error"?: str}. A 200 with
    `ok=false` signals a well-formed but failed test (bad token, bad
    URL) — the router never raises for configuration problems so the
    UI can render the error cleanly.
    """
    settings = load_settings()
    url = (settings.get("abs_url") or "").strip()
    if not url:
        return {"ok": False, "error": "abs_url is not configured"}

    api_key = await get_secret("abs_api_key")
    if not api_key:
        return {"ok": False, "error": "abs_api_key is not configured"}

    client = AudiobookshelfClient(url, api_key)
    try:
        libraries = await client.list_libraries()
    except Exception as e:
        _log.warning("ABS test connection failed: %s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {ui_error(e)}"}

    # Filter to book libraries — podcasts are explicitly ignored by
    # Seshat. Surface mediaType so the UI can show what it sees.
    book_libs = [
        {
            "id": lib.get("id"),
            "name": lib.get("name"),
            "mediaType": lib.get("mediaType"),
            "folders": [
                {"fullPath": f.get("fullPath")}
                for f in (lib.get("folders") or [])
                if f.get("fullPath")
            ],
            "lastUpdate": lib.get("lastUpdate"),
        }
        for lib in libraries
        if lib.get("mediaType") == "book"
    ]
    return {"ok": True, "libraries": book_libs}
