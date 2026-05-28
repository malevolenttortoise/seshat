"""
Auto-train: feed successfully-grabbed authors back to the filter.

When a book is successfully processed through the pipeline, the
author(s) from the metadata are added to the `authors_allowed` table
(if not already present) so future announces by the same author
pass the filter automatically.

This closes the feedback loop:
  IRC announce → filter → grab → download → metadata → auto-train
                   ↑                                        ↓
                   └────────── authors_allowed ←────────────┘

The auto-train only adds to the allow list — it never removes or
modifies existing entries, and it never touches the ignore list.
Authors added by auto-train get `source = "auto_train"` so the UI
can distinguish them from manually-curated entries.
"""
from __future__ import annotations

import logging

import aiosqlite

from app.filter.normalize import normalize_author

_log = logging.getLogger("seshat.orchestrator.auto_train")


async def train_author(
    db: aiosqlite.Connection,
    author_name: str,
    source: str = "auto_train",
) -> bool:
    """Add an author to the allow list if not already present.

    Returns True if the author was newly added, False if already
    present (in either allowed or ignored lists).
    """
    if not author_name or not author_name.strip():
        return False

    normalized = normalize_author(author_name)
    if not normalized:
        return False

    # Check if already in allowed list.
    cursor = await db.execute(
        "SELECT 1 FROM authors_allowed WHERE normalized = ?",
        (normalized,),
    )
    if await cursor.fetchone():
        _log.debug("auto-train: %s already in allow list", author_name)
        return False

    # Check if explicitly ignored — don't override the user's decision.
    cursor = await db.execute(
        "SELECT 1 FROM authors_ignored WHERE normalized = ?",
        (normalized,),
    )
    if await cursor.fetchone():
        _log.debug(
            "auto-train: %s is on ignore list, not adding to allow",
            author_name,
        )
        return False

    # Add to the allow list.
    try:
        await db.execute(
            """
            INSERT INTO authors_allowed (name, normalized, source)
            VALUES (?, ?, ?)
            """,
            (author_name.strip(), normalized, source),
        )
        await db.commit()
        _log.info("auto-train: added %s to allow list", author_name)
    except Exception:
        # IntegrityError from a race condition — another task already
        # added the same author between our check and our insert.
        _log.debug("auto-train: %s already exists (race)", author_name)
        return False

    # Refresh the dispatcher's filter_config so the new author
    # takes effect on the NEXT announce, not at the next restart.
    # No-op during tests / early startup when dispatcher is None.
    try:
        from app import state
        await state.refresh_filter_authors()
    except Exception:
        _log.debug("auto-train: filter-config refresh failed (non-fatal)", exc_info=True)
    return True


async def train_authors_from_blob(
    db: aiosqlite.Connection,
    author_blob: str,
    source: str = "auto_train",
) -> int:
    """Split an author blob and train each author.

    Returns the count of newly-added authors.
    """
    from app.filter.gate import split_authors

    raw_authors = split_authors(author_blob)
    added = 0
    for author in raw_authors:
        if await train_author(db, author, source):
            added += 1
    return added


async def train_authors_from_torrent_info(
    db: aiosqlite.Connection,
    torrent_id,
    *,
    token: str | None,
    fallback_blob: str = "",
    source: str = "author_info",
) -> int:
    """v3.0.0 Phase 10 — train the AUTHORITATIVE MAM authorlist for a grab.

    Fetches `torrent_info` (cached, ttl=300) and trains every author in its
    `author_info` map into the allow list — the complete co-author set,
    unlike the announce blob which may name only the primary. Falls back to
    `fallback_blob` (the announce author string, split into authors) when the
    fetch fails or yields nothing, so a torrent_info outage never loses the
    baseline announce-derived train.

    MAM economy: at most one cached-or-fetched `torrent_info` call per GRAB
    (grabs are rare + deliberate; the snatch dwarfs it). Do NOT call this on
    the per-announce path. Returns the count of newly-added authors.
    """
    names: list[str] = []
    if torrent_id and token:
        from app.mam.torrent_info import TorrentInfoError, get_torrent_info
        try:
            info = await get_torrent_info(str(torrent_id), token=token)
            names = [
                n for n in (info.authors or {}).values()
                if n and n.strip()
            ]
        except TorrentInfoError:
            names = []
    if not names and fallback_blob:
        from app.filter.gate import split_authors
        names = split_authors(fallback_blob)

    added = 0
    for name in names:
        if await train_author(db, name, source):
            added += 1
    return added
