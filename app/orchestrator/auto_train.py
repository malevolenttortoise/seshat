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

Both trainings run when the book is DELIVERED (discovery fix pass S20,
G203): the announce's authors, and MAM's co-author list the dispatcher
saved on the grab (`coauthor_train`). A rejected review, a grab the
policy skipped or a failed download trains nobody. A book credited to
more than `MAX_TRAINED_AUTHORS` authors trains nobody (G204: anthologies
and RPG books, where one namesake match once trained 56 names).
"""
from __future__ import annotations

import logging

import aiosqlite

from app.filter.normalize import normalize_author

_log = logging.getLogger("seshat.orchestrator.auto_train")

# G204: a book with more credited authors than this trains none of them
# (the matched author is already allowed). F7's anthology line is 4+.
MAX_TRAINED_AUTHORS = 3


async def _train_names(
    db: aiosqlite.Connection, names: list[str], source: str, what: str,
) -> int:
    names = [n for n in names if n and n.strip()]
    if len(names) > MAX_TRAINED_AUTHORS:
        _log.info(
            "auto-train: %s credits %d authors (> %d), none trained: %s",
            what, len(names), MAX_TRAINED_AUTHORS, ", ".join(names[:6]),
        )
        return 0
    added = 0
    for name in names:
        if await train_author(db, name, source):
            added += 1
    return added


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
        # Roll back: the failed INSERT leaves its implicit transaction
        # (and SQLite's write lock) open on this connection. Holding it
        # into the dispatcher's grab-claim lock deadlocked two
        # concurrent grabs of one torrent until busy_timeout (30s).
        await db.rollback()
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
    """Split an author blob and train each author (none when it credits
    more than `MAX_TRAINED_AUTHORS`).

    Returns the count of newly-added authors.
    """
    from app.filter.gate import split_authors

    return await _train_names(db, split_authors(author_blob), source, repr(author_blob[:80]))


async def train_coauthors(
    db: aiosqlite.Connection, names: list[str], *, what: str = "the book",
    source: str = "coauthor_train",
) -> int:
    """Train a delivered grab's saved co-author list (G203 / G204)."""
    return await _train_names(db, list(names), source, what)


async def coauthor_names(
    torrent_id,
    *,
    token: str | None,
    fallback_blob: str = "",
    looked_up: bool = False,
    info=None,
) -> list[str]:
    """v3.0.0 Phase 10 — the AUTHORITATIVE MAM authorlist for a grab, to
    save on it and train at delivery (S20).

    Reads `torrent_info` (cached, ttl=300): every author in its
    `author_info` map — the complete co-author set, unlike the announce blob
    which may name only the primary. Falls back to `fallback_blob` (the
    announce author string, split into authors) when the fetch fails or
    yields nothing, so a torrent_info outage never loses the baseline
    announce-derived list.

    MAM economy: at most one cached-or-fetched `torrent_info` call per GRAB
    (grabs are rare + deliberate; the snatch dwarfs it). Do NOT call this on
    the per-announce path.

    `looked_up=True` means the caller already asked MAM: `info` is its
    result (None = the lookup failed), and nothing is fetched here.
    """
    names: list[str] = []
    if looked_up:
        if info is not None:
            names = [n for n in (info.authors or {}).values() if n and n.strip()]
    elif torrent_id and token:
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
    return names
