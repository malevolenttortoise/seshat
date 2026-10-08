"""
MAM user-status API.

`get_user_status()` hits MAM's `jsonLoad.php` endpoint, which returns
the authenticated user's current ratio, wedge count, class name,
seedbonus, and upload/download totals. The policy engine uses these
values to make per-announce decisions:

  - ratio → should we spend download credit on this torrent?
  - wedges → can we apply a freeleech wedge to avoid ratio cost?
  - classname → does "VIP" appear in the class name (affects some
    site-level perks, though the per-torrent VIP flag is separate)?
  - seedbonus → how many bonus points are available to buy more wedges?

The response is cached in-memory for a configurable TTL (default 5
minutes) so that a burst of announces doesn't hammer MAM with
redundant user-status lookups.

Routes through `cookie._do_get` so cookie auto-rotation fires on
every response.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.mam.cookie import _do_get

_log = logging.getLogger("seshat.mam")

MAM_USER_URL = "https://www.myanonamouse.net/jsonLoad.php"
# Every status read also asks for the snatch summary, MAM's own count of
# unsatisfied torrents; the snatch budget uses it as a floor
# (`app.rate_limit.mam_floor`, 2026-10 audit issue 09).
_STATUS_URL = f"{MAM_USER_URL}?snatch_summary"

# Logged once per process when MAM's response has no usable summary.
_missing_summary_logged = False

# Default cache TTL in seconds (5 minutes).
_CACHE_TTL = 300


@dataclass(frozen=True)
class UserStatus:
    """Snapshot of the authenticated MAM user's account status.

    `seedbonus` is a float because `bonusBuy.php` returns fractional
    values (e.g. 26512.091) — widening the type keeps the cache that
    gets warmed from a buy response representable without lossy
    rounding. `jsonLoad.php` always returns an int; `float(...)`
    handles both sources transparently.
    """

    ratio: float
    wedges: int
    seedbonus: float
    classname: str
    username: str
    uid: int
    uploaded_bytes: int
    downloaded_bytes: int
    # Account upload buffer in bytes — MAM exposes this directly in
    # `jsonLoad.php` when available (e.g. when the account has
    # gift-buffer beyond simple uploaded−downloaded), and we fall back
    # to the raw difference when the field is absent. Consumed by the
    # buffer-floor auto-buy trigger and the pre-download buffer gate.
    # Default 0 keeps unrelated UserStatus constructors (tests,
    # fixtures) from needing to care about this field.
    upload_buffer_bytes: int = 0


# ─── In-memory cache ────────────────────────────────────────

_cache: dict[str, tuple[float, UserStatus]] = {}


def _cache_key(token: str) -> str:
    """One entry for the account, whatever the cookie value.

    Keyed by the token until 2026-10-06, but MAM rotates the `mam_id`
    cookie on every jsonLoad response, so the next read always carried a
    new token and the 5-minute cache never hit: every status read was a
    MAM call. Seshat runs one MAM account; a newly saved cookie clears
    the cache instead (`cookie.set_current_token`).
    """
    return "account"


def invalidate_cache() -> None:
    """Clear the user-status cache (e.g. after a cookie rotation)."""
    _cache.clear()


def cached_user_status(
    token: Optional[str], ttl: int = _CACHE_TTL,
) -> Optional[UserStatus]:
    """The cached status for `token` if still fresh, else None (no MAM call)."""
    entry = _cache.get(_cache_key(token or ""))
    if entry is None or time.monotonic() - entry[0] >= ttl:
        return None
    return entry[1]


def update_cache_from_buy(
    token: Optional[str],
    *,
    seedbonus: float,
    uploaded_bytes: int,
    downloaded_bytes: int,
    ratio: float,
) -> None:
    """Merge fresh economic fields from a bonusBuy response into the cache.

    A successful bonusBuy.php call echoes back the user's brand-new
    seedbonus, ratio, and upload/download totals — exactly the subset
    of UserStatus that changes when BP is spent. Warming the cache
    directly from that payload saves a redundant `jsonLoad.php` round
    trip on the next dashboard poll or policy check.

    The cache entry is only updated when a baseline already exists
    for this token; wedges/classname/username/uid are preserved from
    that baseline. If the token has never been fetched, we can't
    synthesize a full UserStatus (those immutable fields aren't in
    the buy response), so this is a no-op and the next
    `get_user_status` call will populate normally.
    """
    key = _cache_key(token or "")
    prev = _cache.get(key)
    if prev is None:
        return
    _, prev_status = prev
    merged = UserStatus(
        ratio=ratio,
        wedges=prev_status.wedges,
        seedbonus=seedbonus,
        classname=prev_status.classname,
        username=prev_status.username,
        uid=prev_status.uid,
        uploaded_bytes=uploaded_bytes,
        downloaded_bytes=downloaded_bytes,
        # The buy response doesn't echo upload_buffer, but we can
        # derive the same fallback the parser uses. Good enough for
        # downstream triggers until the next real jsonLoad.php read.
        upload_buffer_bytes=max(0, uploaded_bytes - downloaded_bytes),
    )
    _cache[key] = (time.monotonic(), merged)


# ─── Public API ─────────────────────────────────────────────


async def get_user_status(
    token: Optional[str] = None,
    ttl: int = _CACHE_TTL,
) -> UserStatus:
    """Fetch the current user's MAM account status.

    Returns a cached result if one exists within `ttl` seconds.
    Raises `UserStatusError` on any failure.

    Args:
        token: Explicit mam_id cookie value. If None, uses the
               module-level current token from cookie.py.
        ttl: Cache lifetime in seconds. Pass 0 to force a fresh fetch.
    """
    key = _cache_key(token or "")
    now = time.monotonic()

    if ttl > 0 and key in _cache:
        cached_at, cached_status = _cache[key]
        if now - cached_at < ttl:
            _log.debug("user_status cache hit (age %.0fs)", now - cached_at)
            return cached_status

    _log.info("Fetching MAM user status from jsonLoad.php")

    try:
        resp = await _do_get(_STATUS_URL, token=token, timeout=15)
    except Exception as exc:
        raise UserStatusError(f"network error: {str(exc) or type(exc).__name__}") from exc

    if resp.status_code != 200:
        raise UserStatusError(f"HTTP {resp.status_code} from jsonLoad.php")

    try:
        data = resp.json()
    except Exception as exc:
        body_preview = resp.text[:200] if resp.text else "(empty)"
        raise UserStatusError(
            f"invalid JSON from jsonLoad.php: {body_preview}"
        ) from exc

    # jsonLoad.php returns an object on success. If it returns a bare
    # string or list, the cookie is probably invalid.
    if not isinstance(data, dict):
        raise UserStatusError(f"unexpected response shape: {type(data).__name__}")

    try:
        uploaded = int(data.get("uploaded_bytes", 0))
        downloaded = int(data.get("downloaded_bytes", 0))
        # MAM exposes `upload_buffer` when it's richer than the raw
        # difference (gift buffer, promotional credit); when absent
        # we fall back to uploaded − downloaded so every user has a
        # meaningful buffer figure for the buffer-trigger auto-buy.
        raw_buffer = data.get("upload_buffer")
        if raw_buffer is None:
            upload_buffer = max(0, uploaded - downloaded)
        else:
            upload_buffer = int(raw_buffer)
        status = UserStatus(
            ratio=float(data.get("ratio", 0)),
            wedges=int(data.get("wedges", 0)),
            seedbonus=float(data.get("seedbonus", 0)),
            classname=str(data.get("classname", "")),
            username=str(data.get("username", "")),
            uid=int(data.get("uid", 0)),
            uploaded_bytes=uploaded,
            downloaded_bytes=downloaded,
            upload_buffer_bytes=upload_buffer,
        )
    except (ValueError, TypeError) as exc:
        raise UserStatusError(f"failed to parse user status fields: {exc}") from exc

    _cache[key] = (now, status)
    _log.info(
        "MAM user status: %s (ratio=%.1f, wedges=%d, class=%s)",
        status.username,
        status.ratio,
        status.wedges,
        status.classname,
    )
    _record_snatch_summary(data)
    # Fan economic-field deltas out to SSE subscribers. Transition-gated
    # inside the publisher so steady-state polls don't re-fire the
    # event every 60s. Best-effort — never raise out of a status fetch.
    try:
        from app.orchestrator.sse_publishers import publish_mam_stats
        await publish_mam_stats(status)
    except Exception:
        _log.exception("mam-stats SSE publish failed (non-fatal)")
    return status


def _record_snatch_summary(data: dict) -> None:
    """Hand MAM's snatch summary to the budget floor; never raises."""
    global _missing_summary_logged
    try:
        from app.rate_limit import mam_floor

        summary = mam_floor.parse_snatch_summary(data)
        if summary is None:
            if not _missing_summary_logged:
                _missing_summary_logged = True
                _log.warning(
                    "MAM's jsonLoad.php response has no usable snatch_summary; "
                    "the snatch budget uses Seshat's own count"
                )
            return
        mam_floor.record(summary)
        _log.info(
            "MAM snatch summary: unsatisfied %d/%d (%d not seeding)",
            summary.unsat_count, summary.unsat_limit, summary.not_seeding,
        )
    except Exception:
        _log.exception("snatch summary record failed (non-fatal)")


class UserStatusError(Exception):
    """Raised when the user-status fetch fails."""
