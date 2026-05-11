"""
CRUD for the `tentative_torrents` and `ignored_torrents_seen` tables.

Tentative torrents: announces that passed every filter EXCEPT the
author allow-list. We stash the MAM torrent ID + scraped metadata
(not the .torrent bytes — user decision #3) so the user can review
and approve later. Approval triggers the normal grab pipeline and
also trains the author onto the allow list.

Ignored torrents seen: announces that were skipped because the author
was on the ignored list. We still record them for weekly review so
the user can change their mind — with a scraped cover + metadata
to make the "do I want this after all?" decision fast.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import aiosqlite

_log = logging.getLogger("seshat.storage.tentative")


TENTATIVE_PENDING = "pending"
TENTATIVE_APPROVED = "approved"
TENTATIVE_REJECTED = "rejected"
TENTATIVE_EXPIRED = "expired"
# `dismissed` differs from `rejected` in that it leaves every author
# on the blob untouched — neither trained to allowed nor pushed to the
# weekly tentative-review list. The user is saying "this specific
# torrent isn't what I want, but I'm not making a call about the
# author(s) yet." Used when the same uploader posts duplicate
# format-variants (MP3 + M4B) and the user wants one without
# committing to / against the author overall.
TENTATIVE_DISMISSED = "dismissed"


@dataclass(frozen=True)
class TentativeRow:
    id: int
    mam_torrent_id: str
    torrent_name: str
    author_blob: str
    category: Optional[str]
    language: Optional[str]
    format: Optional[str]
    vip: bool
    scraped_metadata: dict
    cover_path: Optional[str]
    status: str
    created_at: str
    decided_at: Optional[str]


async def upsert_tentative(
    db: aiosqlite.Connection,
    *,
    mam_torrent_id: str,
    torrent_name: str,
    author_blob: str,
    category: Optional[str] = None,
    language: Optional[str] = None,
    format: Optional[str] = None,
    vip: bool = False,
    scraped_metadata: Optional[dict] = None,
    cover_path: Optional[str] = None,
) -> int:
    """Insert a tentative torrent row, or return the existing id.

    We don't want duplicate rows for the same torrent if the same
    announce appears twice (re-announces, reconnects, etc.), so this
    function checks for an existing pending row first.
    """
    cursor = await db.execute(
        """
        SELECT id FROM tentative_torrents
        WHERE mam_torrent_id = ? AND status = ?
        LIMIT 1
        """,
        (mam_torrent_id, TENTATIVE_PENDING),
    )
    row = await cursor.fetchone()
    if row is not None:
        return int(row["id"])

    meta_json = json.dumps(scraped_metadata or {}, ensure_ascii=False)
    cursor = await db.execute(
        """
        INSERT INTO tentative_torrents
            (mam_torrent_id, torrent_name, author_blob, category,
             language, format, vip, scraped_metadata_json, cover_path, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mam_torrent_id, torrent_name, author_blob, category,
            language, format, 1 if vip else 0, meta_json, cover_path,
            TENTATIVE_PENDING,
        ),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def get_tentative(
    db: aiosqlite.Connection, tentative_id: int
) -> Optional[TentativeRow]:
    cursor = await db.execute(
        "SELECT * FROM tentative_torrents WHERE id = ?", (tentative_id,)
    )
    row = await cursor.fetchone()
    return _row_to_tentative(row) if row else None


async def list_tentative(
    db: aiosqlite.Connection, *, status: str = TENTATIVE_PENDING, limit: int = 500
) -> list[TentativeRow]:
    cursor = await db.execute(
        """
        SELECT * FROM tentative_torrents WHERE status = ?
        ORDER BY created_at DESC LIMIT ?
        """,
        (status, limit),
    )
    rows = await cursor.fetchall()
    return [_row_to_tentative(r) for r in rows]


async def list_tentative_since(
    db: aiosqlite.Connection, *, hours: int, status: str = TENTATIVE_PENDING
) -> list[TentativeRow]:
    cursor = await db.execute(
        """
        SELECT * FROM tentative_torrents
        WHERE status = ? AND created_at >= datetime('now', ?)
        ORDER BY created_at DESC
        """,
        (status, f"-{int(hours)} hours"),
    )
    rows = await cursor.fetchall()
    return [_row_to_tentative(r) for r in rows]


async def set_tentative_status(
    db: aiosqlite.Connection,
    tentative_id: int,
    status: str,
) -> None:
    await db.execute(
        """
        UPDATE tentative_torrents
        SET status = ?, decided_at = datetime('now')
        WHERE id = ?
        """,
        (status, tentative_id),
    )
    await db.commit()


def _row_to_tentative(row) -> TentativeRow:
    try:
        meta = json.loads(row["scraped_metadata_json"]) if row["scraped_metadata_json"] else {}
    except (ValueError, TypeError):
        meta = {}
    return TentativeRow(
        id=int(row["id"]),
        mam_torrent_id=str(row["mam_torrent_id"] or ""),
        torrent_name=str(row["torrent_name"] or ""),
        author_blob=str(row["author_blob"] or ""),
        category=row["category"],
        language=row["language"],
        format=row["format"],
        vip=bool(row["vip"]),
        scraped_metadata=meta,
        cover_path=row["cover_path"],
        status=str(row["status"] or ""),
        created_at=str(row["created_at"] or ""),
        decided_at=row["decided_at"],
    )


# ─── Ignored torrents seen ──────────────────────────────────────


@dataclass(frozen=True)
class IgnoredSeenRow:
    id: int
    mam_torrent_id: str
    torrent_name: str
    author_blob: str
    category: Optional[str]
    info_url: Optional[str]
    cover_path: Optional[str]
    seen_at: str


async def record_ignored_seen(
    db: aiosqlite.Connection,
    *,
    mam_torrent_id: str,
    torrent_name: str,
    author_blob: str,
    category: Optional[str],
    info_url: Optional[str] = None,
    cover_path: Optional[str] = None,
) -> int:
    cursor = await db.execute(
        """
        INSERT INTO ignored_torrents_seen
            (mam_torrent_id, torrent_name, author_blob, category,
             info_url, cover_path)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (mam_torrent_id, torrent_name, author_blob, category, info_url, cover_path),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def list_ignored_grouped_by_author(
    db: aiosqlite.Connection, *, days: int = 7
) -> list[dict]:
    """Group ignored-seen torrents by author for the weekly review.

    Returns a list of {author_blob, count, torrents: [{torrent_name, mam_torrent_id, cover_path}]}.
    """
    cursor = await db.execute(
        """
        SELECT author_blob, mam_torrent_id, torrent_name, cover_path
        FROM ignored_torrents_seen
        WHERE seen_at >= datetime('now', ?)
        ORDER BY author_blob, seen_at DESC
        """,
        (f"-{int(days)} days",),
    )
    rows = await cursor.fetchall()
    groups: dict[str, dict] = {}
    for r in rows:
        author = str(r["author_blob"] or "")
        if author not in groups:
            groups[author] = {"author_blob": author, "count": 0, "torrents": []}
        groups[author]["count"] += 1
        groups[author]["torrents"].append({
            "torrent_name": str(r["torrent_name"] or ""),
            "mam_torrent_id": str(r["mam_torrent_id"] or ""),
            "cover_path": r["cover_path"],
        })
    return sorted(groups.values(), key=lambda g: g["count"], reverse=True)


async def list_ignored_seen_since(
    db: aiosqlite.Connection, *, hours: int
) -> list[IgnoredSeenRow]:
    cursor = await db.execute(
        """
        SELECT id, mam_torrent_id, torrent_name, author_blob, category,
               info_url, cover_path, seen_at
        FROM ignored_torrents_seen
        WHERE seen_at >= datetime('now', ?)
        ORDER BY seen_at DESC
        """,
        (f"-{int(hours)} hours",),
    )
    rows = await cursor.fetchall()
    return [
        IgnoredSeenRow(
            id=int(r["id"]),
            mam_torrent_id=str(r["mam_torrent_id"] or ""),
            torrent_name=str(r["torrent_name"] or ""),
            author_blob=str(r["author_blob"] or ""),
            category=r["category"],
            info_url=r["info_url"],
            cover_path=r["cover_path"],
            seen_at=str(r["seen_at"] or ""),
        )
        for r in rows
    ]
