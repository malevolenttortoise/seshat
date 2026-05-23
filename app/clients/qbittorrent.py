"""
qBittorrent WebUI API client.

Implements the `TorrentClient` Protocol against qBit's `/api/v2/`
HTTP endpoints. The four operations Seshat needs:

  - login()                  → POST /api/v2/auth/login
  - add_torrent(...)         → POST /api/v2/torrents/add (multipart)
  - list_torrents(...)       → GET  /api/v2/torrents/info
  - get_torrent(hash)        → derived from list_torrents

Authentication is cookie-session based. After a successful login,
qBit sets an `SID` cookie that must accompany every subsequent
request. The httpx.AsyncClient instance held by this class manages
that cookie automatically via its cookie jar.

The client is per-instance (one client = one qBit instance) rather
than module-level like `app.mam.cookie._client`. The reason: future
phases may want to talk to multiple qBit deployments, and the SID
cookie is per-server. Per-instance cookies make that trivially safe.

The `transport=` constructor parameter exists for the test suite
(see `tests/fake_qbit.py`). Production code never passes it; it
defaults to httpx's real network transport.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from app.clients.base import AddResult, TorrentInfo

_log = logging.getLogger("seshat.clients.qbittorrent")


class QbitClient:
    """qBittorrent WebUI client.

    Parameters
    ----------
    base_url:
        Root URL of the qBit WebUI, e.g. ``http://10.0.10.20:8080``.
        Trailing slash optional. Seshat will hit `<base_url>/api/v2/...`.
    username, password:
        WebUI credentials.
    basic_auth:
        Optional `(user, pass)` tuple for an HTTP Basic Auth layer in
        front of the WebUI (some reverse-proxy setups). Passed straight
        to httpx as the `auth` parameter on every request.
    verify_tls:
        Whether to verify TLS certs. Set False for self-signed setups.
    timeout:
        Per-request timeout in seconds.
    transport:
        Test hook — pass an `httpx.MockTransport` to intercept requests.
        Production code leaves this as None and httpx uses its real
        network transport.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        basic_auth: Optional[tuple[str, str]] = None,
        verify_tls: bool = True,
        timeout: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.basic_auth = basic_auth
        self._timeout = timeout
        # The cookie jar on this client persists the SID across calls
        # automatically — we never have to manually thread it through.
        client_kwargs: dict = {
            "base_url": self.base_url,
            "timeout": httpx.Timeout(timeout, connect=10.0),
            "verify": verify_tls,
            "follow_redirects": True,
        }
        if basic_auth is not None:
            client_kwargs["auth"] = httpx.BasicAuth(*basic_auth)
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)
        self._logged_in = False

    # ─── Lifecycle ───────────────────────────────────────────

    async def aclose(self) -> None:
        """Close the underlying httpx client. Idempotent."""
        try:
            await self._client.aclose()
        except Exception as e:
            _log.warning(f"Error closing qBit client: {e}")

    # ─── Auth ────────────────────────────────────────────────

    async def login(self) -> bool:
        """POST /api/v2/auth/login.

        qBit returns 200 with body `Ok.` on success and `Fails.` on
        bad credentials. A 403 is returned when login is rate-limited
        (too many failed attempts in a row, qBit's IP-banning kicks
        in). Network exceptions return False; the caller can decide
        whether to retry.

        qBit 5.2+ also returns HTTP 204 No Content when the caller's
        IP is in `AuthSubnetWhitelist` — the credentials are ignored
        and the connection is authorized by IP. We treat that the same
        as a successful login: subsequent calls don't need a session
        cookie because qBit re-checks the subnet on every request.

        On success, any session cookie qBit set is captured by the
        client's cookie jar and attached to subsequent requests
        automatically.
        """
        try:
            resp = await self._client.post(
                "/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                headers={"Referer": self.base_url},
            )
        except httpx.HTTPError as e:
            _log.warning(f"qBit login transport error: {type(e).__name__}: {e}")
            self._logged_in = False
            return False

        if resp.status_code == 200 and resp.text.strip() == "Ok.":
            self._logged_in = True
            _log.info(f"qBit login OK at {self.base_url}")
            return True

        if resp.status_code == 204:
            self._logged_in = True
            _log.info(f"qBit login OK at {self.base_url} (IP whitelist bypass)")
            return True

        _log.warning(
            f"qBit login failed: HTTP {resp.status_code} body={resp.text[:80]!r}"
        )
        self._logged_in = False
        return False

    async def _ensure_logged_in(self) -> bool:
        """Login if we haven't already this session."""
        if self._logged_in:
            return True
        return await self.login()

    # ─── Add torrent ─────────────────────────────────────────

    async def add_torrent(
        self,
        torrent_bytes: bytes,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> AddResult:
        """POST /api/v2/torrents/add (multipart).

        On 403, transparently re-authenticates once and retries — qBit
        invalidates sessions after a configurable timeout, and Seshat
        runs long enough that this happens routinely. The single retry
        bound stops a broken-credentials situation from spinning.
        """
        if not torrent_bytes:
            return AddResult(
                success=False,
                failure_kind="rejected",
                failure_detail="empty torrent_bytes",
            )

        if not await self._ensure_logged_in():
            return AddResult(
                success=False,
                failure_kind="auth_failed",
                failure_detail="qBit login rejected credentials",
            )

        result = await self._do_add(torrent_bytes, category, save_path, tags)
        if result.success or result.failure_kind != "auth_failed":
            return result

        # 403 path — session probably expired. Re-login and retry once.
        _log.info("qBit add returned auth failure; re-logging in and retrying once")
        self._logged_in = False
        if not await self.login():
            return AddResult(
                success=False,
                failure_kind="auth_failed",
                failure_detail="qBit re-login failed after session expiry",
            )
        return await self._do_add(torrent_bytes, category, save_path, tags)

    async def _do_add(
        self,
        torrent_bytes: bytes,
        category: Optional[str],
        save_path: Optional[str],
        tags: Optional[list[str]] = None,
    ) -> AddResult:
        files = {
            # The form field name MUST be "torrents" (plural) — qBit's
            # API uses that exact name. Filename is cosmetic but qBit
            # logs it, so use something recognizable.
            "torrents": ("seshat.torrent", torrent_bytes, "application/x-bittorrent"),
        }
        data: dict[str, str] = {}
        if category:
            data["category"] = category
        if save_path:
            data["savepath"] = save_path
        if tags:
            # qBit's API takes a comma-separated list of tag names with
            # NO whitespace around the commas. Tag names themselves
            # CAN contain spaces — qBit splits strictly on commas.
            # Filter empty strings out so a list with [""] doesn't
            # produce a literal-empty tag (which qBit accepts but
            # then renders awkwardly in the UI).
            data["tags"] = ",".join(t for t in tags if t)

        try:
            resp = await self._client.post(
                "/api/v2/torrents/add",
                files=files,
                data=data,
                headers={"Referer": self.base_url},
            )
        except httpx.HTTPError as e:
            _log.warning(f"qBit add transport error: {type(e).__name__}: {e}")
            return AddResult(
                success=False,
                failure_kind="network_error",
                failure_detail=f"{type(e).__name__}: {e}",
            )

        if resp.status_code == 200:
            body = resp.text.strip()
            if body == "Ok.":
                return AddResult(success=True)
            if body == "Fails.":
                # qBit returns HTTP 200 with body "Fails." when the
                # torrent is already in the client (duplicate hash).
                # NOT a real failure from Seshat's perspective —
                # the torrent IS in qBit, which is what we wanted.
                # Surface as `duplicate` so the dispatcher can
                # decide policy: in Phase 1 it still records the
                # grab as failed (because we couldn't verify the
                # add we expected), but a future iteration could
                # treat duplicates as success and look up the
                # existing torrent's hash via list_torrents.
                return AddResult(
                    success=False,
                    failure_kind="duplicate",
                    failure_detail="qBit reports torrent already exists (HTTP 200 'Fails.')",
                )
            # qBittorrent 5.2+ replaced the plain-text "Ok." / "Fails."
            # contract with a JSON body shaped:
            #   {"added_torrent_ids": ["<hash>", ...], "failure_count": N}
            # We treat `added_torrent_ids` non-empty AND failure_count==0
            # as success. failure_count>0 with non-empty added implies a
            # mixed batch (we only ever send one torrent, so this is
            # effectively a duplicate from qBit's side); failure_count>0
            # with empty added is a real rejection.
            #
            # Discovered post-v2.21.0 deploy on the hotio image's qBit
            # 5.2 build: every torrents/add call was returning JSON,
            # so the dispatcher marked every grab `failed_unknown` even
            # though qBit had accepted and was downloading them. The
            # `login()` 204 handler was patched (df69a79); this is the
            # matching add-torrent handler.
            if body.startswith("{") or body.startswith("["):
                try:
                    import json as _json
                    parsed = _json.loads(body)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    added = parsed.get("added_torrent_ids") or []
                    failure_count = parsed.get("failure_count", 0)
                    if added and not failure_count:
                        return AddResult(success=True)
                    if added and failure_count:
                        # Mixed-batch — we only send one torrent so this
                        # path shouldn't fire in practice, but treat the
                        # `added` presence as authoritative.
                        return AddResult(success=True)
                    if not added and failure_count:
                        return AddResult(
                            success=False,
                            failure_kind="rejected",
                            failure_detail=(
                                f"qBit rejected torrent "
                                f"(failure_count={failure_count})"
                            ),
                        )
                    # Empty JSON or unknown shape — fall through to the
                    # generic "unexpected" path below so we don't
                    # silently swallow new response variants.

        if resp.status_code == 403:
            return AddResult(
                success=False,
                failure_kind="auth_failed",
                failure_detail="HTTP 403 from qBit",
            )

        if resp.status_code == 415:
            # qBit returns 415 when it can't parse the file as a
            # valid .torrent. The bytes we got from MAM might be
            # corrupted, or might be an HTML error page that slipped
            # past the grab classifier.
            return AddResult(
                success=False,
                failure_kind="rejected",
                failure_detail="qBit rejected the .torrent file as invalid",
            )

        if 500 <= resp.status_code < 600:
            return AddResult(
                success=False,
                failure_kind="unknown",
                failure_detail=f"qBit server error HTTP {resp.status_code}",
            )

        return AddResult(
            success=False,
            failure_kind="unknown",
            failure_detail=f"unexpected qBit response: HTTP {resp.status_code} body={resp.text[:80]!r}",
        )

    # ─── List / get torrents ─────────────────────────────────

    async def list_torrents(
        self, category: Optional[str] = None
    ) -> list[TorrentInfo]:
        """GET /api/v2/torrents/info.

        Returns a list of `TorrentInfo` snapshots. Filters by category
        if one is provided. Returns an empty list on auth failure or
        network errors so the caller (the budget watcher) can degrade
        gracefully — no surprise exceptions in a long-running poll loop.
        """
        if not await self._ensure_logged_in():
            _log.warning("qBit list_torrents: not authenticated")
            return []

        params = {}
        if category:
            params["category"] = category

        try:
            resp = await self._client.get(
                "/api/v2/torrents/info",
                params=params,
                headers={"Referer": self.base_url},
            )
        except httpx.HTTPError as e:
            # Transport error = we can't reach qBit at all (container
            # stopped, network partition, DNS miss). Flip _logged_in
            # so the next tick's reachability check correctly reports
            # unreachable — the SSE `client-status` publisher driving
            # the Downloader pill reads this flag. Without the reset
            # it stays True from the prior successful tick and the UI
            # never learns qBit went down.
            self._logged_in = False
            _log.warning(f"qBit list_torrents transport error: {type(e).__name__}: {e}")
            return []

        if resp.status_code == 403:
            self._logged_in = False
            _log.info("qBit list_torrents got 403; session expired")
            return []

        if resp.status_code != 200:
            # Anything that isn't 200 or 403 (500s, 502 from a reverse
            # proxy with qBit down behind it, etc.) also means we
            # can't trust the session — clear the flag so reachability
            # reports false until we successfully re-auth.
            self._logged_in = False
            _log.warning(f"qBit list_torrents unexpected HTTP {resp.status_code}")
            return []

        try:
            raw = resp.json()
        except json.JSONDecodeError as e:
            _log.warning(f"qBit list_torrents invalid JSON: {e}")
            return []

        return [_parse_torrent(t) for t in raw]

    async def get_torrent(self, torrent_hash: str) -> Optional[TorrentInfo]:
        """Get one torrent by hash, or None if not found.

        Implemented as a list_torrents() filter rather than a separate
        endpoint — qBit's `/api/v2/torrents/info?hashes=...` does
        support direct lookup, but the unified path keeps the code
        smaller and the budget watcher already iterates the list anyway.
        """
        if not torrent_hash:
            return None
        for t in await self.list_torrents():
            if t.hash == torrent_hash:
                return t
        return None

    async def list_torrent_files(self, torrent_hash: str) -> list[str]:
        """GET /api/v2/torrents/files?hash=<hash>.

        Returns the relative paths of every file in the torrent — the
        same list qBit's WebUI shows under the "Content" tab. Paths
        are relative to the torrent's `content_path` (a root folder
        for multi-file torrents, or just the filename for single-file
        ones). The pipeline joins these against `save_path` to locate
        book files on disk without guessing from `torrent_name`.

        Returns an empty list on auth failure, transport errors, or
        unexpected JSON — the pipeline then falls back to the older
        name-heuristic search instead of propagating an exception.
        """
        if not torrent_hash:
            return []
        if not await self._ensure_logged_in():
            _log.warning("qBit list_torrent_files: not authenticated")
            return []
        try:
            resp = await self._client.get(
                "/api/v2/torrents/files",
                params={"hash": torrent_hash},
                headers={"Referer": self.base_url},
            )
        except httpx.HTTPError as e:
            _log.warning(
                f"qBit list_torrent_files transport error: {type(e).__name__}: {e}"
            )
            return []
        if resp.status_code == 403:
            self._logged_in = False
            return []
        if resp.status_code != 200:
            _log.warning(
                f"qBit list_torrent_files unexpected HTTP {resp.status_code}"
            )
            return []
        try:
            raw = resp.json()
        except json.JSONDecodeError as e:
            _log.warning(f"qBit list_torrent_files invalid JSON: {e}")
            return []
        if not isinstance(raw, list):
            return []
        return [str(f.get("name", "")) for f in raw if isinstance(f, dict) and f.get("name")]


    # ─── Migration helpers ───────────────────────────────────

    async def pause_torrent(self, torrent_hash: str) -> bool:
        """Pause/stop a torrent.

        Tries v5 API (stop) first, falls back to v4 (pause).
        qBit v5 renamed pause → stop.
        """
        if not await self._ensure_logged_in():
            return False
        try:
            # v5: stop
            resp = await self._client.post(
                "/api/v2/torrents/stop",
                data={"hashes": torrent_hash},
            )
            if resp.status_code == 200:
                return True
            # v4 fallback: pause
            resp = await self._client.post(
                "/api/v2/torrents/pause",
                data={"hashes": torrent_hash},
            )
            return resp.status_code == 200
        except httpx.HTTPError as e:
            _log.warning("qBit pause error: %s", e)
            return False

    async def resume_torrent(self, torrent_hash: str) -> bool:
        """Resume/start a torrent.

        Tries v5 API (start) first, falls back to v4 (resume).
        qBit v5 renamed resume → start.
        """
        if not await self._ensure_logged_in():
            return False
        try:
            # v5: start
            resp = await self._client.post(
                "/api/v2/torrents/start",
                data={"hashes": torrent_hash},
            )
            if resp.status_code == 200:
                return True
            # v4 fallback: resume
            resp = await self._client.post(
                "/api/v2/torrents/resume",
                data={"hashes": torrent_hash},
            )
            return resp.status_code == 200
        except httpx.HTTPError as e:
            _log.warning("qBit resume error: %s", e)
            return False

    async def set_location(self, torrent_hash: str, location: str) -> bool:
        """Move a torrent to a new save path.

        Tries two qBit API endpoints:
          1. setSavePath (qBit v5+) — the modern endpoint
          2. setLocation (qBit v4) — fallback for older versions

        qBit v5 deprecated setLocation; it may return 200 but silently
        do nothing. setSavePath is the correct v5 endpoint but requires
        the target directory to exist AND be writable by the qBit process.
        """
        if not await self._ensure_logged_in():
            return False
        try:
            # Try v5 API first: setSavePath
            resp = await self._client.post(
                "/api/v2/torrents/setSavePath",
                data={"id": torrent_hash, "path": location},
            )
            if resp.status_code == 200:
                return True
            # Log the failure reason for debugging.
            _log.info("qBit setSavePath returned %d: %s — trying setLocation",
                       resp.status_code, resp.text[:80])

            # Fallback to v4 API: setLocation
            resp = await self._client.post(
                "/api/v2/torrents/setLocation",
                data={"hashes": torrent_hash, "location": location},
            )
            return resp.status_code == 200
        except httpx.HTTPError as e:
            _log.warning("qBit set_location error: %s", e)
            return False

    async def recheck_torrent(self, torrent_hash: str) -> bool:
        """POST /api/v2/torrents/recheck."""
        if not await self._ensure_logged_in():
            return False
        try:
            resp = await self._client.post(
                "/api/v2/torrents/recheck",
                data={"hashes": torrent_hash},
            )
            return resp.status_code == 200
        except httpx.HTTPError as e:
            _log.warning("qBit recheck error: %s", e)
            return False


def _parse_torrent(raw: dict) -> TorrentInfo:
    """Map a qBit torrent JSON object onto our TorrentInfo dataclass.

    Defensive about missing fields — qBit has changed its response
    shape across versions, and we want a clean degradation rather
    than KeyErrors in a poll loop.
    """
    return TorrentInfo(
        hash=str(raw.get("hash", "")),
        name=str(raw.get("name", "")),
        category=str(raw.get("category", "")),
        state=str(raw.get("state", "")),
        seeding_seconds=int(raw.get("seeding_time", 0) or 0),
        save_path=str(raw.get("save_path", "")),
        added_on=int(raw.get("added_on", 0) or 0),
        progress=float(raw.get("progress", 0.0) or 0.0),
        dlspeed=int(raw.get("dlspeed", 0) or 0),
        eta=int(raw.get("eta", 0) or 0),
        size=int(raw.get("size", 0) or 0),
    )


