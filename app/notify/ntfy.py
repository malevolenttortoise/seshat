"""
ntfy.sh notification sender.

`send()` posts a notification to the configured ntfy topic. Used for:
  - Grab events (new book grabbed)
  - Download completion
  - Pipeline errors (Calibre rejection, staging failure)
  - Daily digest summaries

ntfy.sh is a simple HTTP-based pub/sub notification service. Sending a
notification is just an HTTP POST with the message body as plain text
and metadata in headers. Authentication: BasicAuth via the
`ntfy_username` setting + encrypted `ntfy_password` secret (v2.24.0);
legacy inline `https://user:pass@host/topic` URLs still parse but the
dedicated fields are preferred so the URL doesn't leak through
intermediary logs.

The module is a no-op when `ntfy_url` is empty in settings, so callers
don't need to guard against "notifications not configured".
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

_log = logging.getLogger("seshat.notify")

# Module-level httpx client for connection reuse.
_client: Optional[httpx.AsyncClient] = None


# Common typographic Unicode characters → ASCII fallbacks. ntfy's
# Title header has to be ASCII (httpx rejects raw non-ASCII). Most
# titles contain at most an em-dash or smart-quote, so a small
# substitution table covers the realistic cases without dragging in
# RFC 2047 encoded-word machinery. Anything that survives the
# substitution table gets dropped by `.encode("ascii", "ignore")`
# rather than crashing the send.
_HEADER_FOLDS = {
    "—": "-",   # em-dash (U+2014)
    "–": "-",   # en-dash (U+2013)
    "−": "-",   # minus sign (U+2212)
    "…": "...", # ellipsis (U+2026)
    "“": '"',   # left double quote
    "”": '"',   # right double quote
    "‘": "'",   # left single quote
    "’": "'",   # right single quote
    "•": "*",   # bullet
    "→": "->",  # right arrow
    "←": "<-",  # left arrow
    " ": " ",   # non-breaking space → regular space
}


def _ascii_header_safe(s: str) -> str:
    """Fold typographic punctuation to ASCII and drop anything else.

    Covers the common em-dash / smart-quote / ellipsis cases that
    show up in titles. Anything outside the fold table that's still
    non-ASCII gets stripped rather than crashing httpx's header
    encoder. The resulting string is always pure ASCII.
    """
    if not s:
        return ""
    out = "".join(_HEADER_FOLDS.get(ch, ch) for ch in s)
    return out.encode("ascii", "ignore").decode("ascii")


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    return _client


async def aclose() -> None:
    """Tear down the HTTP client (called during app shutdown)."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        finally:
            _client = None


def _resolve_endpoint(url: str, topic: str) -> Optional[str]:
    """Resolve the full ntfy endpoint URL from user-provided settings.

    Accepts any of these forms (all equivalent):
      url="https://ntfy.sh", topic="seshat"  → https://ntfy.sh/seshat
      url="ntfy.sh", topic="seshat"          → https://ntfy.sh/seshat
      url="https://ntfy.sh/seshat", topic="" → https://ntfy.sh/seshat
      url="ntfy.sh/seshat", topic=""         → https://ntfy.sh/seshat

    Inline `user:pass@host` credentials in the URL are STRIPPED here —
    `_resolve_auth_from_url()` extracts them so they don't leak into
    the path / Host header. Use the encrypted-store credentials
    (`ntfy_username` setting + `ntfy_password` secret) for new
    installs; the inline-strip is only for backwards compatibility.

    Returns None if neither url nor topic is set, or if url is empty.
    """
    if not url:
        return None
    url = url.strip()
    if not url:
        return None

    # Add scheme if missing.
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Strip inline `user:pass@host` if present. The credentials get
    # picked up by `_resolve_auth_from_url()` separately; the endpoint
    # itself must NOT carry them or every nginx/cloudflare in the
    # middle logs the password.
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
        url = urlunparse(parsed)

    # If the URL already has a path component (topic in URL), use it as-is.
    # Otherwise, append the topic.
    if parsed.path and parsed.path != "/":
        # Topic is already in the URL — use the URL as the full endpoint.
        return url.rstrip("/")

    # No path — need a separate topic.
    if not topic or not topic.strip():
        return None
    return f"{url.rstrip('/')}/{topic.strip()}"


def _resolve_auth_from_url(url: str) -> Optional[tuple[str, str]]:
    """Extract inline `user:pass@host` BasicAuth credentials from a URL.

    Backwards-compat seam — pre-v2.24.0 users embedded credentials in
    the ntfy server URL (`https://user:pass@host/topic`). The new
    dedicated `ntfy_username` setting + encrypted `ntfy_password`
    secret take precedence; this only fires if those are empty AND
    the URL still carries inline creds.

    Returns None if no inline credentials are present.
    """
    if not url:
        return None
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.username and parsed.password:
        return (parsed.username, parsed.password)
    return None


async def _resolve_auth() -> Optional[httpx.BasicAuth]:
    """Resolve the BasicAuth credentials for the next ntfy send.

    Precedence:
      1. `ntfy_username` setting + `ntfy_password` secret (dedicated fields)
      2. Inline `user:pass@host` in `ntfy_url` (backwards-compat only)
      3. None (no auth header sent)

    Read every call so a credential rotation at runtime takes effect
    on the next send without a restart.
    """
    from app.config import load_settings
    from app.secrets import get_secret

    s = load_settings()
    username = (s.get("ntfy_username") or "").strip()
    if username:
        password = await get_secret("ntfy_password") or ""
        if password:
            return httpx.BasicAuth(username, password)
        # Username set, no password — likely a misconfiguration mid-setup.
        # Fall through to inline-URL fallback rather than sending a half
        # auth header.

    inline = _resolve_auth_from_url(s.get("ntfy_url") or "")
    if inline is not None:
        return httpx.BasicAuth(*inline)
    return None


async def send(
    *,
    url: str,
    topic: str,
    title: str,
    message: str,
    priority: int = 3,
    tags: Optional[list[str]] = None,
) -> bool:
    """Send a notification via ntfy.

    Args:
        url: The ntfy server URL. Can be:
             - Just the server: "https://ntfy.sh" (with separate topic)
             - Server + topic combined: "https://ntfy.sh/seshat"
             - Without scheme: "ntfy.sh" or "ntfy.sh/seshat"
        topic: The topic to publish to. Optional if topic is in the URL.
        title: Notification title.
        message: Notification body.
        priority: 1-5 (1=min, 3=default, 5=max).
        tags: Optional list of emoji/tag strings (e.g. ["books", "white_check_mark"]).

    Returns True on success, False on failure (logged but never raised).
    """
    endpoint = _resolve_endpoint(url, topic)
    if not endpoint:
        return False
    # HTTP headers default to ASCII (latin-1 if you push it). The
    # daily digest title contains an em-dash ("—") which crashes the
    # whole send. Fold common typographic punctuation back to ASCII
    # so the title still reads correctly when ntfy renders it.
    # Bodies are sent UTF-8 in the request body so they're unaffected.
    headers = {
        "Title": _ascii_header_safe(title),
        "Priority": str(priority),
    }
    if tags:
        headers["Tags"] = ",".join(tags)

    # v2.24.0 — resolve BasicAuth from the dedicated ntfy_username +
    # ntfy_password secret (or the legacy inline-URL form). Resolved
    # per call so a credential rotation at runtime applies on the
    # next send without restarting the container.
    auth = await _resolve_auth()

    try:
        resp = await _get_client().post(
            endpoint,
            content=message.encode("utf-8"),
            headers=headers,
            auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT,
        )
        if resp.status_code == 200:
            _log.debug("ntfy sent: %s", title)
            return True
        _log.warning("ntfy HTTP %d for %s: %s", resp.status_code, endpoint, resp.text[:200])
        return False
    except Exception:
        _log.exception("ntfy send failed")
        return False


# ─── Per-event gate (v2.11.1 N1) ────────────────────────────


def is_event_enabled(event_key: str) -> bool:
    """Per-event ntfy gate. True iff both the master
    `per_event_notifications` setting is on AND the per-event
    `notify_on_{event_key}` setting is on (default True).

    Centralizes the gate logic so every call site stays in sync.
    Pre-v2.11.1 the master gate was checked at each call site but
    the per-event sub-toggle was NOT — so ntfy events fired even
    when the user had explicitly disabled them in Settings →
    Notifications. UAT-confirmed bug; this helper closes that gap.

    Recognised `event_key` values map to the existing config.py
    settings (default True if missing):
      - "grab"               → notify_on_grab
      - "download_complete"  → notify_on_download_complete
      - "pipeline_error"     → notify_on_pipeline_error
      - "review_queued"      → notify_on_review_queued       (v2.12.0)
      - "library_ingest"     → notify_on_library_ingest      (v2.12.0)
      - "buffer_gate_block"  → notify_on_buffer_gate_block   (v2.12.0)

    Settings are mtime-cached in `app.config.load_settings`, so a
    per-event call is effectively free.
    """
    from app.config import load_settings
    s = load_settings()
    if not s.get("per_event_notifications", False):
        return False
    return bool(s.get(f"notify_on_{event_key}", True))


# ─── Convenience senders ────────────────────────────────────


async def notify_grab(
    url: str, topic: str, torrent_name: str, author: str, category: str
) -> bool:
    """Notify that a new book was grabbed."""
    return await send(
        url=url,
        topic=topic,
        title="New book grabbed",
        message=f"{torrent_name}\nby {author}\n{category}",
        tags=["books"],
    )


async def notify_buffer_gate_block(
    url: str, topic: str, torrent_name: str, size_gb: float, buffer_gb: float
) -> bool:
    """Notify that an auto-grab was refused by the buffer gate.

    Fired at most once per rolling 6h window per trigger type (IRC
    autograb vs user manual grab) — the dispatcher throttles
    upstream of this call. Wording emphasizes "feed went quiet"
    because silent rejections are the scarier failure mode: the user
    needs to know at least one announce was blocked, without getting
    hammered when the buffer stays low for days.
    """
    return await send(
        url=url,
        topic=topic,
        title="Buffer gate blocked a grab",
        message=(
            f"{torrent_name}\n"
            f"Size {size_gb:.1f} GB exceeds available buffer "
            f"({buffer_gb:.1f} GB). Further blocks suppressed for 6h."
        ),
        priority=4,
        tags=["no_entry_sign"],
    )


async def notify_download_complete(
    url: str, topic: str, torrent_name: str, author: str
) -> bool:
    """Notify that a download completed."""
    return await send(
        url=url,
        topic=topic,
        title="Download complete",
        message=f"{torrent_name}\nby {author}",
        tags=["white_check_mark"],
    )


async def notify_pipeline_complete(
    url: str, topic: str, torrent_name: str, sink: str
) -> bool:
    """Notify that the post-download pipeline completed."""
    return await send(
        url=url,
        topic=topic,
        title=f"Added to {sink}",
        message=torrent_name,
        tags=["books", "white_check_mark"],
    )


async def notify_error(
    url: str, topic: str, torrent_name: str, error: str
) -> bool:
    """Notify of a pipeline error."""
    return await send(
        url=url,
        topic=topic,
        title="Pipeline error",
        message=f"{torrent_name}\n{error}",
        priority=4,
        tags=["warning"],
    )
