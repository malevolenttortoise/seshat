"""
Cover image serving endpoint.

    GET /api/v1/covers/{path:path} — serve a cover image from disk

The review queue and tentative list store cover paths as absolute
filesystem paths. The UI can't access those directly — this endpoint
serves them through the auth middleware so the browser can display
them as regular <img> tags.

Security: the path is validated to ensure it lives under one of the
known staging/cover directories. Arbitrary path traversal is
blocked by checking that the resolved path starts with the
configured staging_path or review_staging_path.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import load_settings

router = APIRouter(prefix="/api/v1/covers", tags=["covers"])


@router.get("/{cover_path:path}")
async def serve_cover(cover_path: str):
    """Serve a cover image file by its stored path.

    The `cover_path` is the absolute path stored in the DB row.
    We validate it resolves to a real file under known directories.
    """
    settings = load_settings()
    allowed_roots = set()
    for key in ("staging_path", "review_staging_path"):
        val = settings.get(key, "")
        if val:
            allowed_roots.add(Path(val).resolve())

    # Also allow /tmp for tentative covers stored in temp staging
    allowed_roots.add(Path("/tmp").resolve())

    target = Path(cover_path).resolve()

    # Security: target must be under one of the allowed roots.
    # is_relative_to() avoids the prefix-match edge case where a sibling
    # directory like /data/staging-evil would slip past startswith("/data/staging").
    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(403, "Cover path not in allowed directory")

    if not target.exists() or not target.is_file():
        raise HTTPException(404, "Cover not found")

    # Guess content type from suffix
    suffix = target.suffix.lower()
    ct_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
    content_type = ct_map.get(suffix, "image/jpeg")

    return FileResponse(
        str(target),
        media_type=content_type,
        headers={"Cache-Control": "max-age=86400"},
    )
