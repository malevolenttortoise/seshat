"""
rTorrent XML-RPC client.

Implements the TorrentClient Protocol against rTorrent's XML-RPC API,
typically exposed at `/RPC2` behind an nginx/ruTorrent reverse proxy.
Auth is HTTP basic auth on the proxy.

rTorrent has no native categories or labels. The convention (used by
ruTorrent) is to store labels in `d.custom1`. Seshat follows this
convention: category is written to `d.custom1` on add and read from
`d.custom1` on list.

rTorrent also has no native seeding-time counter. We compute it from
`d.timestamp.finished` (when download completed) relative to now.
If the torrent is still downloading, seeding_seconds is 0.

XML-RPC calls are synchronous via `xmlrpc.client` wrapped in
`asyncio.to_thread` since no good async XML-RPC library exists.

Key API methods:
  - load.raw_start: add torrent with options
  - d.multicall2: batch query all torrents
  - d.stop / d.start: pause/resume
  - d.directory_base.set + execute.throw(mv): move files
  - d.check_hash: recheck
"""
from __future__ import annotations

import asyncio
import logging
import time
import xmlrpc.client
from typing import Optional

from app.clients.base import AddResult, TorrentInfo

_log = logging.getLogger("seshat.clients.rtorrent")


class RtorrentClient:
    """rTorrent XML-RPC client with async wrappers."""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        # Build the XML-RPC URL with embedded basic auth if provided.
        if username:
            from urllib.parse import urlparse, urlunparse
            p = urlparse(self.base_url)
            auth_url = urlunparse(p._replace(
                netloc=f"{username}:{password}@{p.hostname}" + (f":{p.port}" if p.port else "")
            ))
            self._url = auth_url
        else:
            self._url = self.base_url
        self._proxy: Optional[xmlrpc.client.ServerProxy] = None
        self._logged_in = False

    def _get_proxy(self) -> xmlrpc.client.ServerProxy:
        if self._proxy is None:
            self._proxy = xmlrpc.client.ServerProxy(self._url)
        return self._proxy

    async def aclose(self) -> None:
        if self._proxy is not None:
            try:
                self._proxy("close")()
            except Exception:
                pass
            self._proxy = None

    async def login(self) -> bool:
        """Test the connection by calling system.client_version."""
        try:
            version = await asyncio.to_thread(
                self._get_proxy().system.client_version
            )
            self._logged_in = True
            _log.info("rTorrent connected: version %s at %s", version, self.base_url)
            return True
        except Exception as e:
            _log.warning("rTorrent login failed: %s", e)
            return False

    async def add_torrent(
        self,
        torrent_bytes: bytes,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> AddResult:
        try:
            proxy = self._get_proxy()
            binary = xmlrpc.client.Binary(torrent_bytes)
            # Build the command list applied on load.
            cmds: list[str] = []
            if save_path:
                cmds.append(f"d.directory_base.set={save_path}")
            if category:
                cmds.append(f"d.custom1.set={category}")
            # Set addtime custom field for tracking.
            cmds.append(f"d.custom.set=addtime,{int(time.time())}")

            await asyncio.to_thread(
                proxy.load.raw_start, "", binary, *cmds
            )
            return AddResult(success=True)
        except xmlrpc.client.Fault as e:
            _log.warning("rTorrent add_torrent fault: %s", e)
            if "already loaded" in str(e).lower():
                return AddResult(success=False, failure_kind="duplicate", failure_detail=str(e))
            return AddResult(success=False, failure_kind="rejected", failure_detail=str(e))
        except Exception as e:
            _log.warning("rTorrent add_torrent error: %s", e)
            return AddResult(success=False, failure_kind="network_error", failure_detail=str(e))

    async def list_torrents(self, category: Optional[str] = None) -> list[TorrentInfo]:
        return await self.list_torrents_checked(category) or []

    async def list_torrents_checked(
        self, category: Optional[str] = None
    ) -> Optional[list[TorrentInfo]]:
        """`list_torrents`, but None when the list couldn't be read (the
        snatch ledger must not reconcile against a failed read)."""
        try:
            proxy = self._get_proxy()
            rows = await asyncio.to_thread(
                proxy.d.multicall2, "", "main",
                "d.hash=",
                "d.name=",
                "d.custom1=",
                "d.state=",
                "d.timestamp.finished=",
                "d.directory_base=",
                "d.timestamp.started=",
            )
        except Exception as e:
            _log.warning("rTorrent list_torrents failed: %s", e)
            return None

        now = int(time.time())
        out: list[TorrentInfo] = []
        for row in rows:
            if len(row) < 7:
                continue
            torrent_hash = str(row[0])
            name = str(row[1])
            label = str(row[2])
            state_int = int(row[3])  # 0=stopped, 1=started
            finished_ts = int(row[4])
            save_path = str(row[5])
            started_ts = int(row[6])

            if category and label != category:
                continue

            # Compute seeding time: time since download finished.
            if finished_ts > 0:
                seeding_secs = max(0, now - finished_ts)
            else:
                seeding_secs = 0

            # Map state: 0=stopped, 1=active (could be seeding or downloading).
            state = "uploading" if state_int == 1 and finished_ts > 0 else \
                    "downloading" if state_int == 1 else "stopped"

            added_on = started_ts if started_ts > 0 else 0

            out.append(TorrentInfo(
                hash=torrent_hash.lower(),
                name=name,
                category=label,
                state=state,
                seeding_seconds=seeding_secs,
                save_path=save_path,
                added_on=added_on,
            ))
        return out

    async def get_torrent(self, torrent_hash: str) -> Optional[TorrentInfo]:
        for t in await self.list_torrents():
            if t.hash == torrent_hash.lower():
                return t
        return None

    async def pause_torrent(self, torrent_hash: str) -> bool:
        try:
            await asyncio.to_thread(self._get_proxy().d.stop, torrent_hash)
            return True
        except Exception as e:
            _log.warning("rTorrent pause failed: %s", e)
            return False

    async def resume_torrent(self, torrent_hash: str) -> bool:
        try:
            await asyncio.to_thread(self._get_proxy().d.start, torrent_hash)
            return True
        except Exception as e:
            _log.warning("rTorrent resume failed: %s", e)
            return False

    async def set_location(self, torrent_hash: str, location: str) -> bool:
        """Move torrent to a new directory.

        rTorrent's `d.directory_base.set` changes the path but doesn't
        physically move files. We set the new path, then the caller is
        expected to handle the file move externally (or the user does
        it via ruTorrent/filesystem). For a full move, rTorrent 0.9.8+
        supports d.directory_base.set + execute(mv), but the execute
        approach is fragile across different rTorrent setups.

        We take the safe approach: set the directory and let the
        recheck step verify the files are in place.
        """
        try:
            proxy = self._get_proxy()
            await asyncio.to_thread(proxy.d.directory_base.set, torrent_hash, location)
            return True
        except Exception as e:
            _log.warning("rTorrent set_location failed: %s", e)
            return False

    async def recheck_torrent(self, torrent_hash: str) -> bool:
        try:
            await asyncio.to_thread(self._get_proxy().d.check_hash, torrent_hash)
            return True
        except Exception as e:
            _log.warning("rTorrent recheck failed: %s", e)
            return False
