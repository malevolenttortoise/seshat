"""
MAM .torrent file grabber.

`fetch_torrent(torrent_id, token)` is the only public surface — it
takes a MAM torrent ID and the current session cookie, hits MAM's
download endpoint, and returns a `GrabResult` describing what
happened.

The function deliberately does NOT write the .torrent bytes to disk,
submit them to qBittorrent, or update any database row. Those side
effects belong upstream in the orchestration layer (the IRC listener
or the inject endpoint), which already needs to know about success and
failure separately so it can keep the `grabs` table in sync.

Failure detection — the hybrid you approved:

  Status code first, body sniff as tiebreaker. The hybrid matters
  because MAM does not always use HTTP status codes consistently:
  they sometimes serve a 200 OK response with the HTML login page
  body, which looks like success at the transport layer but is
  actually an auth failure, so we defensively sniff the body.

  Decision matrix:

    HTTP 200 + body starts with `d` (bencoded torrent)  → SUCCESS
    HTTP 200 + body starts with `<` or contains `<html` → COOKIE_EXPIRED
    HTTP 200 + empty body                               → COOKIE_EXPIRED
    HTTP 200 + unrecognized non-empty body              → UNKNOWN
    HTTP 401 / 403                                      → COOKIE_EXPIRED
    HTTP 404 / 410                                      → TORRENT_NOT_FOUND
    HTTP 5xx                                            → UNKNOWN (transient)
    Network exception (timeout, conn refused, etc.)     → NETWORK_ERROR

  All three cookie-related failure shapes (HTML 200, 401, 403) funnel
  into the same `cookie_expired` failure kind, so the cookie-rotation
  retry flow only has one state to check when deciding which grabs to
  re-run after a fresh cookie is uploaded.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

import httpx

from app.mam.announce import build_download_url
from app.mam.cookie import _do_get
from app.config import ui_error

_log = logging.getLogger("seshat.mam.grab")


FailureKind = Literal[
    "cookie_expired",
    "torrent_not_found",
    "network_error",
    "unknown",
]


@dataclass(frozen=True)
class GrabResult:
    """Outcome of a single .torrent fetch attempt.

    On success: `success=True`, `torrent_bytes` populated, `failure_kind`
    and `failure_detail` are unset.

    On failure: `success=False`, `torrent_bytes` is None, `failure_kind`
    is one of the four enum values, `failure_detail` is a short human-
    readable message safe to surface in the UI. `http_status` is set
    when the failure was a real HTTP response (None when the failure
    was a transport-layer exception).
    """

    success: bool
    torrent_bytes: Optional[bytes] = None
    failure_kind: Optional[FailureKind] = None
    failure_detail: str = ""
    http_status: Optional[int] = None


# ─── The grab ────────────────────────────────────────────────


async def fetch_torrent(
    torrent_id: str,
    token: str,
    timeout: int = 30,
    *,
    use_fl_wedge: bool = False,
) -> GrabResult:
    """Fetch a single .torrent file from MAM.

    Pure I/O — no DB writes, no qBit submission, no logging beyond
    structured log lines for observability. The caller decides what
    to do with the result.

    Args:
        torrent_id: The numeric MAM torrent ID (string).
        token: The current `mam_id` session cookie value.
        timeout: HTTP timeout in seconds. Default 30 — .torrent files
                 are tiny so this is generous; we want a clear
                 timeout failure rather than the default httpx
                 30-minute hang on a half-open connection.
        use_fl_wedge: If True, appends `&fl=1` to spend a freeleech
                      wedge on this torrent.

    Returns:
        A `GrabResult` describing success or the specific failure
        mode. Never raises — all exception paths are translated into
        `network_error` results so the caller has a single shape to
        handle.
    """
    if not torrent_id:
        return GrabResult(
            success=False,
            failure_kind="unknown",
            failure_detail="empty torrent_id",
        )
    if not token:
        # Treat "no cookie configured" the same as "cookie expired"
        # so the retry-on-cookie-update path picks it up automatically.
        return GrabResult(
            success=False,
            failure_kind="cookie_expired",
            failure_detail="no MAM session cookie configured",
        )

    url = build_download_url(torrent_id, use_fl_wedge=use_fl_wedge)

    try:
        # Route through cookie._do_get so the cookie auto-rotation
        # handler fires on every response. If MAM sent back a fresh
        # mam_id in the Set-Cookie header, the in-memory token and
        # (eventually) settings.json both get updated before this
        # function returns to its caller.
        resp = await _do_get(url, token=token, timeout=timeout)
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        _log.warning(f"grab transport error for tid={torrent_id}: {type(e).__name__}: {e}")
        return GrabResult(
            success=False,
            failure_kind="network_error",
            failure_detail=f"{type(e).__name__}: {ui_error(e)}",
        )
    except Exception as e:
        # Catch-all for anything httpx didn't classify — e.g. SSL
        # errors during cert rotation. Funneled into network_error
        # because they all share the same retry semantics ("try again
        # later, the cookie is probably fine").
        _log.exception(f"grab unexpected exception for tid={torrent_id}")
        return GrabResult(
            success=False,
            failure_kind="network_error",
            failure_detail=f"{type(e).__name__}: {ui_error(e)}",
        )

    return _classify_response(torrent_id, resp)


def _classify_response(torrent_id: str, resp: httpx.Response) -> GrabResult:
    """Apply the hybrid status-then-body-sniff classification.

    Pulled out as a separate function so it's trivially unit-testable
    without needing the full HTTP fixture for every edge case.
    """
    status = resp.status_code
    body = resp.content

    # Hard-fail status codes — no need to look at the body.
    if status in (401, 403):
        _log.info(f"grab tid={torrent_id} → cookie_expired (HTTP {status})")
        return GrabResult(
            success=False,
            failure_kind="cookie_expired",
            failure_detail=f"HTTP {status} from MAM",
            http_status=status,
        )

    if status in (404, 410):
        _log.info(f"grab tid={torrent_id} → torrent_not_found (HTTP {status})")
        return GrabResult(
            success=False,
            failure_kind="torrent_not_found",
            failure_detail=f"HTTP {status} from MAM",
            http_status=status,
        )

    if 500 <= status < 600:
        _log.warning(f"grab tid={torrent_id} → unknown (HTTP {status} server error)")
        return GrabResult(
            success=False,
            failure_kind="unknown",
            failure_detail=f"MAM server error HTTP {status}",
            http_status=status,
        )

    if status != 200:
        _log.warning(f"grab tid={torrent_id} → unknown (HTTP {status})")
        return GrabResult(
            success=False,
            failure_kind="unknown",
            failure_detail=f"unexpected HTTP {status}",
            http_status=status,
        )

    # Status is 200 — now sniff the body. MAM has been observed
    # serving the HTML login page with a 200 status code when the
    # cookie is invalid, so we can't trust the status alone.
    if not body:
        _log.info(f"grab tid={torrent_id} → cookie_expired (200 + empty body)")
        return GrabResult(
            success=False,
            failure_kind="cookie_expired",
            failure_detail="HTTP 200 with empty body — cookie likely expired",
            http_status=status,
        )

    head = body[:200].lstrip().lower()
    if head.startswith(b"<html") or head.startswith(b"<!doctype html") or b"<html" in head:
        _log.info(f"grab tid={torrent_id} → cookie_expired (HTML body)")
        return GrabResult(
            success=False,
            failure_kind="cookie_expired",
            failure_detail="MAM returned HTML login page — cookie expired",
            http_status=status,
        )

    # A real .torrent file is bencoded — always starts with `d` for a
    # top-level dictionary. This is the cleanest "is this actually a
    # torrent file" check we can do without a full bencode parser.
    if body[:1] == b"d":
        _log.info(
            f"grab tid={torrent_id} → success ({len(body)} bytes)"
        )
        return GrabResult(
            success=True,
            torrent_bytes=body,
            http_status=status,
        )

    # Status 200 + non-empty body + neither HTML nor bencode — could
    # be anything (a JSON error message, a plain-text rejection, an
    # unexpected MAM response shape). Mark as unknown rather than
    # silently treating it as success.
    _log.warning(
        f"grab tid={torrent_id} → unknown "
        f"(200 + non-bencode body, head={body[:32]!r})"
    )
    return GrabResult(
        success=False,
        failure_kind="unknown",
        failure_detail=f"unrecognized response body shape (head={body[:32]!r})",
        http_status=status,
    )
