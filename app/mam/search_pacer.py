"""
Manual Grab's cached MAM lookups.

A Manual Grab batch holds up to 30 torrents, and MAM's search API looks
them up one ID per call (`tor.id` is single-ID; ADR-0023). Every MAM
request is paced by `app.mam.pacer` at the HTTP layer (one at a time,
`rate_mam` apart; since the 2026-10 audit, not just Manual Grab's), so a
pasted batch never bursts. These two helpers add the cache in front: a
torrent or account looked up in the last couple of minutes isn't asked
for again, and so doesn't wait for a turn.
"""
from __future__ import annotations

from typing import Optional

from app.mam.torrent_info import (
    TorrentInfo,
    cached_torrent_info,
    get_torrent_info,
)
from app.mam.user_status import UserStatus, cached_user_status, get_user_status


async def paced_torrent_info(
    torrent_id: str, token: Optional[str],
) -> TorrentInfo:
    """`get_torrent_info`, paced; a cache hit makes no request.

    Raises what `get_torrent_info` raises.
    """
    hit = cached_torrent_info(torrent_id)
    if hit is not None:
        return hit
    return await get_torrent_info(torrent_id, token=token)


async def paced_user_status(token: Optional[str]) -> UserStatus:
    """`get_user_status`, paced; a cache hit makes no request.

    Raises what `get_user_status` raises.
    """
    hit = cached_user_status(token)
    if hit is not None:
        return hit
    return await get_user_status(token=token)
