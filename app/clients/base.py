"""
Torrent client contract.

The `TorrentClient` Protocol is the interface every concrete client
implements. It's small on purpose — Seshat only needs four
operations:

  1. login()                       — establish a session
  2. add_torrent(...)              — submit a .torrent file
  3. list_torrents(category=...)   — for the qBit poller / budget watcher
  4. get_torrent(hash)             — for budget release detection

The shared dataclasses (`AddResult`, `TorrentInfo`) are deliberately
client-agnostic so the rest of Seshat never has to know whether
it's talking to qBittorrent, Deluge, or anything else. Failure
classification is the caller's job; a concrete client maps native
errors into the `failure_kind` enum below.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Protocol


# Failure modes a torrent client add operation can produce. Same shape
# discipline as `mam.grab.GrabResult`: every failure has a stable enum
# value plus a free-text detail message safe to surface in the UI.
AddFailureKind = Literal[
    "auth_failed",       # login rejected (wrong creds, expired session, etc.)
    "duplicate",         # torrent already exists in the client
    "rejected",          # client rejected the .torrent file as invalid
    "network_error",     # transport-level failure (timeout, connection refused)
    "unknown",           # everything else
]


@dataclass(frozen=True)
class AddResult:
    """Outcome of a single add_torrent call.

    On success: `success=True`. The torrent_hash MAY be set if the
    client returned it; some backends (qBit's add endpoint) don't,
    in which case the caller can reconcile via list_torrents.
    """

    success: bool
    torrent_hash: Optional[str] = None
    failure_kind: Optional[AddFailureKind] = None
    failure_detail: str = ""


@dataclass(frozen=True)
class TorrentInfo:
    """Snapshot of one torrent as reported by the client.

    Fields are the intersection of what we actually use downstream —
    adding a field here means a real consumer needs it. The trailing
    progress/size group is used by the SSE `torrent-progress` feed;
    clients that don't report them (Transmission, Deluge, rTorrent)
    leave them at the defaults.
    """

    hash: str
    name: str
    category: str
    state: str             # client-native state string ("uploading",
                           # "downloading", "pausedUP", etc.)
    seeding_seconds: int   # how long this torrent has been seeding
    save_path: str
    added_on: int          # unix timestamp
    progress: float = 0.0  # 0.0-1.0 download completion
    dlspeed: int = 0       # current download rate, bytes/sec
    eta: int = 0           # seconds until complete (qBit: 8640000 = inf)
    size: int = 0          # total torrent size, bytes


class TorrentClient(Protocol):
    """Contract every concrete client implements.

    The Protocol is structural — concrete classes don't need to inherit
    from it explicitly. Type checkers verify the shape; the rest of
    Seshat can take a `TorrentClient` parameter and the qBit
    implementation will satisfy it without an `isinstance` chain.
    """

    async def login(self) -> bool: ...

    async def add_torrent(
        self,
        torrent_bytes: bytes,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> AddResult: ...

    async def list_torrents(
        self,
        category: Optional[str] = None,
    ) -> list[TorrentInfo]: ...

    async def get_torrent(self, torrent_hash: str) -> Optional[TorrentInfo]: ...

    async def list_torrent_files(self, torrent_hash: str) -> list[str]:
        """Return the file paths inside a torrent, relative to save_path.

        Used by the post-download pipeline to locate the actual book
        file(s) on disk without guessing from the torrent name — a
        torrent named "Infinite Warship" may well save as
        "Infinite_Warship_-_Scott_Bartlett.epub", and multi-file
        torrents can drop 37 loose files into the save_path with no
        parent folder to anchor against. Clients that can't introspect
        file listings return an empty list; callers then fall back to
        the older name-heuristic search.
        """
        return []

    async def aclose(self) -> None: ...
