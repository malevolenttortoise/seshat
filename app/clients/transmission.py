"""
Transmission RPC client.

Implements the TorrentClient Protocol against Transmission's JSON-RPC
API at `/transmission/rpc`. Auth uses HTTP basic auth + the
X-Transmission-Session-Id header (obtained from the initial 409
response and resent on every subsequent request).

Transmission doesn't have "categories" — it uses "labels" (list of
strings, Transmission 3.0+). We map the Seshat category to the
first label.

Key API methods used:
  - torrent-add: metainfo (base64), download-dir, labels
  - torrent-get: hashString, name, labels, status, downloadDir,
                 addedDate, secondsSeeding
  - torrent-stop / torrent-start: pause/resume
  - torrent-set-location: move files
  - torrent-verify: recheck
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx

from app.clients.base import AddResult, TorrentInfo

_log = logging.getLogger("seshat.clients.transmission")


class TransmissionClient:
    """Transmission RPC client with session-id auto-refresh."""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.rpc_url = f"{self.base_url}/transmission/rpc"
        self.username = username
        self.password = password
        self._session_id = ""
        auth = httpx.BasicAuth(username, password) if username else None
        client_kwargs: dict = {
            "timeout": httpx.Timeout(30.0, connect=10.0),
            "follow_redirects": True,
        }
        if auth:
            client_kwargs["auth"] = auth
        if transport:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)
        self._logged_in = False

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def login(self) -> bool:
        """Obtain a session ID by triggering a 409 response."""
        try:
            resp = await self._client.post(
                self.rpc_url,
                json={"method": "session-get"},
                headers={"X-Transmission-Session-Id": "invalid"},
            )
            if resp.status_code == 409:
                self._session_id = resp.headers.get("X-Transmission-Session-Id", "")
                self._logged_in = bool(self._session_id)
                if self._logged_in:
                    _log.info("Transmission session ID obtained at %s", self.base_url)
                return self._logged_in
            if resp.status_code == 200:
                self._session_id = resp.headers.get("X-Transmission-Session-Id", self._session_id)
                self._logged_in = True
                return True
            _log.warning("Transmission login: unexpected HTTP %d", resp.status_code)
            return False
        except Exception as e:
            _log.warning("Transmission login error: %s", e)
            return False

    async def _rpc(self, method: str, arguments: dict = None) -> dict:
        """Send an RPC call, auto-refreshing the session ID on 409."""
        if not self._session_id:
            await self.login()
        body = {"method": method}
        if arguments:
            body["arguments"] = arguments
        for attempt in range(2):
            resp = await self._client.post(
                self.rpc_url,
                json=body,
                headers={"X-Transmission-Session-Id": self._session_id},
            )
            if resp.status_code == 409:
                self._session_id = resp.headers.get("X-Transmission-Session-Id", "")
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("result") != "success":
                raise RuntimeError(f"Transmission RPC error: {data.get('result')}")
            return data.get("arguments", {})
        raise RuntimeError("Transmission: session ID refresh loop")

    async def add_torrent(
        self,
        torrent_bytes: bytes,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> AddResult:
        args: dict = {"metainfo": base64.b64encode(torrent_bytes).decode()}
        if save_path:
            args["download-dir"] = save_path
        labels = []
        if category:
            labels.append(category)
        if tags:
            labels.extend(tags)
        if labels:
            args["labels"] = labels
        try:
            result = await self._rpc("torrent-add", args)
            added = result.get("torrent-added") or result.get("torrent-duplicate")
            if added:
                h = added.get("hashString", "")
                is_dup = "torrent-duplicate" in result
                return AddResult(
                    success=not is_dup,
                    torrent_hash=h,
                    failure_kind="duplicate" if is_dup else None,
                    failure_detail="already exists" if is_dup else "",
                )
            return AddResult(success=True)
        except httpx.HTTPError as e:
            _log.warning("Transmission add_torrent network error: %s", e)
            return AddResult(success=False, failure_kind="network_error", failure_detail=str(e))
        except Exception as e:
            _log.warning("Transmission add_torrent failed: %s", e)
            return AddResult(success=False, failure_kind="unknown", failure_detail=str(e))

    async def list_torrents(self, category: Optional[str] = None) -> list[TorrentInfo]:
        return await self.list_torrents_checked(category) or []

    async def list_torrents_checked(
        self, category: Optional[str] = None
    ) -> Optional[list[TorrentInfo]]:
        """`list_torrents`, but None when the list couldn't be read (the
        snatch ledger must not reconcile against a failed read)."""
        try:
            result = await self._rpc("torrent-get", {
                "fields": ["hashString", "name", "labels", "status",
                           "downloadDir", "addedDate", "secondsSeeding"],
            })
        except Exception as e:
            _log.warning("Transmission list_torrents failed: %s", e)
            return None
        torrents = result.get("torrents", [])
        out: list[TorrentInfo] = []
        for t in torrents:
            labels = t.get("labels", [])
            torrent_cat = labels[0] if labels else ""
            if category and torrent_cat != category:
                continue
            state = _map_status(t.get("status", 0))
            out.append(TorrentInfo(
                hash=str(t.get("hashString", "")),
                name=str(t.get("name", "")),
                category=torrent_cat,
                state=state,
                seeding_seconds=int(t.get("secondsSeeding", 0)),
                save_path=str(t.get("downloadDir", "")),
                added_on=int(t.get("addedDate", 0)),
            ))
        return out

    async def get_torrent(self, torrent_hash: str) -> Optional[TorrentInfo]:
        for t in await self.list_torrents():
            if t.hash == torrent_hash:
                return t
        return None

    async def pause_torrent(self, torrent_hash: str) -> bool:
        try:
            await self._rpc("torrent-stop", {"ids": [torrent_hash]})
            return True
        except Exception:
            return False

    async def resume_torrent(self, torrent_hash: str) -> bool:
        try:
            await self._rpc("torrent-start", {"ids": [torrent_hash]})
            return True
        except Exception:
            return False

    async def set_location(self, torrent_hash: str, location: str) -> bool:
        try:
            await self._rpc("torrent-set-location", {
                "ids": [torrent_hash], "location": location, "move": True,
            })
            return True
        except Exception:
            return False

    async def recheck_torrent(self, torrent_hash: str) -> bool:
        try:
            await self._rpc("torrent-verify", {"ids": [torrent_hash]})
            return True
        except Exception:
            return False


def _map_status(status: int) -> str:
    """Map Transmission's numeric status to a human-readable string."""
    return {
        0: "stopped",
        1: "checkingDL",
        2: "checkingUP",
        3: "queuedDL",
        4: "downloading",
        5: "queuedUP",
        6: "uploading",
    }.get(status, f"unknown_{status}")
