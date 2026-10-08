"""
Deluge Web UI JSON-RPC client.

Implements the TorrentClient Protocol against Deluge's web interface
at `/json`. Auth is a two-step dance: `auth.login(password)` then
`web.connected()` (or `web.connect(hostID)` if not auto-connected).

Deluge doesn't have native categories. The Label plugin (bundled but
must be enabled) provides single-string labels per torrent. Seshat
maps its category concept to Deluge labels. If the Label plugin isn't
enabled, category filtering is skipped and add_torrent still works.

Key API methods:
  - auth.login(password)
  - core.add_torrent_file(filename, base64, options)
  - label.set_torrent(hash, label)
  - core.get_torrents_status(filter, keys)
  - core.pause_torrent / resume_torrent / force_recheck / move_storage
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx

from app.clients.base import AddResult, TorrentInfo

_log = logging.getLogger("seshat.clients.deluge")

_REQ_ID = 0


def _next_id() -> int:
    global _REQ_ID
    _REQ_ID += 1
    return _REQ_ID


class DelugeClient:
    """Deluge Web UI JSON-RPC client."""

    def __init__(
        self,
        base_url: str,
        password: str = "deluge",
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.json_url = f"{self.base_url}/json"
        self.password = password
        client_kwargs: dict = {
            "timeout": httpx.Timeout(30.0, connect=10.0),
            "follow_redirects": True,
        }
        if transport:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)
        self._logged_in = False
        self._label_plugin = False

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def _rpc(self, method: str, params: list = None) -> dict:
        """Send a JSON-RPC call to Deluge's web API."""
        body = {"method": method, "params": params or [], "id": _next_id()}
        resp = await self._client.post(self.json_url, json=body)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            err = data["error"]
            raise RuntimeError(f"Deluge RPC error: {err.get('message', err)}")
        return data.get("result")

    async def login(self) -> bool:
        try:
            result = await self._rpc("auth.login", [self.password])
            if not result:
                _log.warning("Deluge auth.login returned false")
                return False
            self._logged_in = True
            # Ensure connected to a daemon.
            connected = await self._rpc("web.connected")
            if not connected:
                hosts = await self._rpc("web.get_hosts")
                if hosts:
                    await self._rpc("web.connect", [hosts[0][0]])
            # Check if Label plugin is available.
            try:
                plugins = await self._rpc("core.get_enabled_plugins")
                self._label_plugin = "Label" in (plugins or [])
            except Exception:
                self._label_plugin = False
            _log.info("Deluge login OK at %s (label plugin: %s)", self.base_url, self._label_plugin)
            return True
        except Exception as e:
            _log.warning("Deluge login error: %s", e)
            return False

    async def _ensure_logged_in(self) -> bool:
        if self._logged_in:
            return True
        return await self.login()

    async def add_torrent(
        self,
        torrent_bytes: bytes,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> AddResult:
        if not await self._ensure_logged_in():
            return AddResult(success=False, failure_kind="auth_failed", failure_detail="not logged in")
        options: dict = {}
        if save_path:
            options["download_location"] = save_path
        try:
            b64 = base64.b64encode(torrent_bytes).decode()
            torrent_hash = await self._rpc("core.add_torrent_file", ["seshat.torrent", b64, options])
            if not torrent_hash:
                return AddResult(success=False, failure_kind="rejected", failure_detail="add returned None")
            # Set label if the plugin is available.
            if category and self._label_plugin:
                try:
                    # Ensure the label exists.
                    labels = await self._rpc("label.get_labels") or []
                    if category.lower() not in [l.lower() for l in labels]:
                        await self._rpc("label.add", [category])
                    await self._rpc("label.set_torrent", [torrent_hash, category])
                except Exception:
                    _log.debug("Deluge: failed to set label %r on %s", category, torrent_hash)
            return AddResult(success=True, torrent_hash=str(torrent_hash))
        except httpx.HTTPError as e:
            _log.warning("Deluge add_torrent network error: %s", e)
            return AddResult(success=False, failure_kind="network_error", failure_detail=str(e))
        except Exception as e:
            _log.warning("Deluge add_torrent failed: %s", e)
            return AddResult(success=False, failure_kind="unknown", failure_detail=str(e))

    async def list_torrents(self, category: Optional[str] = None) -> list[TorrentInfo]:
        return await self.list_torrents_checked(category) or []

    async def list_torrents_checked(
        self, category: Optional[str] = None
    ) -> Optional[list[TorrentInfo]]:
        """`list_torrents`, but None when the list couldn't be read (the
        snatch ledger must not reconcile against a failed read)."""
        if not await self._ensure_logged_in():
            return None
        try:
            filter_dict: dict = {}
            if category and self._label_plugin:
                filter_dict["label"] = category
            result = await self._rpc("core.get_torrents_status", [
                filter_dict,
                ["hash", "name", "label", "state", "seeding_time", "save_path", "time_added"],
            ])
        except Exception as e:
            _log.warning("Deluge list_torrents failed: %s", e)
            return None
        if not isinstance(result, dict):
            return None
        out: list[TorrentInfo] = []
        for h, t in result.items():
            out.append(TorrentInfo(
                hash=str(h),
                name=str(t.get("name", "")),
                category=str(t.get("label", "")),
                state=str(t.get("state", "")).lower(),
                seeding_seconds=int(t.get("seeding_time", 0)),
                save_path=str(t.get("save_path", "")),
                added_on=int(t.get("time_added", 0)),
            ))
        return out

    async def get_torrent(self, torrent_hash: str) -> Optional[TorrentInfo]:
        if not await self._ensure_logged_in():
            return None
        try:
            result = await self._rpc("core.get_torrent_status", [
                torrent_hash,
                ["hash", "name", "label", "state", "seeding_time", "save_path", "time_added"],
            ])
            if not result:
                return None
            return TorrentInfo(
                hash=torrent_hash,
                name=str(result.get("name", "")),
                category=str(result.get("label", "")),
                state=str(result.get("state", "")).lower(),
                seeding_seconds=int(result.get("seeding_time", 0)),
                save_path=str(result.get("save_path", "")),
                added_on=int(result.get("time_added", 0)),
            )
        except Exception:
            return None

    async def pause_torrent(self, torrent_hash: str) -> bool:
        try:
            await self._rpc("core.pause_torrent", [[torrent_hash]])
            return True
        except Exception:
            return False

    async def resume_torrent(self, torrent_hash: str) -> bool:
        try:
            await self._rpc("core.resume_torrent", [[torrent_hash]])
            return True
        except Exception:
            return False

    async def set_location(self, torrent_hash: str, location: str) -> bool:
        try:
            await self._rpc("core.move_storage", [[torrent_hash], location])
            return True
        except Exception:
            return False

    async def recheck_torrent(self, torrent_hash: str) -> bool:
        try:
            await self._rpc("core.force_recheck", [[torrent_hash]])
            return True
        except Exception:
            return False
