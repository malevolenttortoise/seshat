"""Per-library cover_phash management — backfill + lazy compute.

Sits between the per-library `books` table (where `cover_phash` lives)
and the pure pHash helpers in `app/mam/cover_hash.py`. Handles the
three cover sources Seshat supports:

  - `cover_path` (Calibre-style local file)        → hash from disk
  - `audiobookshelf_id` (ABS-served cover)         → fetch + hash
  - `cover_url` (source-scan external URL)         → fetch + hash

Eager backfill runs on startup ONLY for the cover_path path because
file I/O is cheap and the owned-Calibre case is the largest slice of
the library. Remote-fetch sources (ABS / cover_url) populate lazily
via `ensure_cover_phash` at MAM-scan time so we don't hammer external
hosts on every container restart.
"""
from __future__ import annotations

import logging
from typing import Optional

import aiosqlite

from app.mam.cover_hash import hash_image_file, hash_image_bytes

_log = logging.getLogger("seshat.discovery.cover_phash")


async def backfill_cover_phashes_from_paths(
    db: aiosqlite.Connection,
) -> tuple[int, int]:
    """One-shot backfill of `books.cover_phash` from local `cover_path`.

    Walks every owned book whose `cover_phash IS NULL` AND `cover_path
    IS NOT NULL`, hashes the file, persists the hash. Idempotent —
    re-runs touch zero rows once everything's filled. Files that fail
    to hash get `cover_phash` left NULL so a later attempt can retry.

    Returns `(rows_updated, rows_skipped)`. The skip count includes
    rows whose file was missing or failed to decode.
    """
    rows = await (await db.execute(
        "SELECT id, cover_path FROM books "
        "WHERE cover_phash IS NULL AND cover_path IS NOT NULL"
    )).fetchall()
    updated = 0
    skipped = 0
    for r in rows:
        h = hash_image_file(r["cover_path"])
        if not h:
            skipped += 1
            continue
        await db.execute(
            "UPDATE books SET cover_phash = ? WHERE id = ?",
            (h, r["id"]),
        )
        updated += 1
    if updated or skipped:
        await db.commit()
        _log.info(
            "cover_phash backfill: %d updated, %d skipped (missing/unreadable)",
            updated, skipped,
        )
    return (updated, skipped)


async def ensure_cover_phash(
    db: aiosqlite.Connection,
    book_id: int,
    *,
    token: Optional[str] = None,
) -> Optional[str]:
    """Return the book's cover_phash, computing + persisting if missing.

    Resolution order mirrors the debug-match endpoint's lookup chain:
      1. `books.cover_phash` if already populated → return as-is
      2. `books.cover_path` → hash file → persist → return
      3. `books.cover_url` → fetch via httpx → hash → persist → return

    `token` is only consulted for MAM CDN URLs (rare for Seshat covers
    but possible for source-discovered books that point at MAM). Other
    URLs go through plain httpx with no auth headers.

    Returns None when no cover info is available OR all attempts fail.
    Callers MUST treat None as "no signal" — production cover
    verification gracefully degrades to text-only behavior.
    """
    row = await (await db.execute(
        "SELECT cover_phash, cover_path, cover_url FROM books WHERE id = ?",
        (int(book_id),),
    )).fetchone()
    if not row:
        return None
    if row["cover_phash"]:
        return row["cover_phash"]

    h: Optional[str] = None
    if row["cover_path"]:
        h = hash_image_file(row["cover_path"])
    if not h and row["cover_url"]:
        h = await _fetch_and_hash_url(row["cover_url"], token=token)
    if h:
        await db.execute(
            "UPDATE books SET cover_phash = ? WHERE id = ?",
            (h, book_id),
        )
        await db.commit()
    return h


async def _fetch_and_hash_url(
    url: str, *, token: Optional[str] = None,
) -> Optional[str]:
    """Fetch a URL's bytes (auth-aware for MAM CDN) and pHash them."""
    if not url:
        return None
    try:
        if "myanonamouse.net" in url and token:
            from app.mam.cookie import _do_get
            resp = await _do_get(url, token=token, timeout=15)
        else:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
        if resp.status_code != 200 or not resp.content:
            return None
        return hash_image_bytes(resp.content)
    except Exception as e:
        _log.debug("cover URL fetch failed for %s: %s", url, e)
        return None
