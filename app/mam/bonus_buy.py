"""
MAM bonus-point purchase API.

Thin async wrappers around MAM's `bonusBuy.php` endpoint. Three purchases
are exposed to the rest of Seshat:

  - `buy_vip(weeks)`              — spend BP on VIP time (4/8/12/max weeks)
  - `buy_upload_credit(gb)`       — spend BP on upload buffer (float GB)
  - `buy_personal_freeleech(tid)` — spend 50k BP to make a specific
                                    torrent personal-freeleech (MAM flags
                                    it on the user's account; the
                                    subsequent grab goes through normally
                                    with no `&fl=1` override needed)

All three share a response shape: MAM echoes the user's fresh seedbonus,
uploaded/downloaded totals, and ratio in the same JSON body. We parse
that into `BuyResult` AND warm `user_status._cache` from it — the buy
already told us everything a follow-up `jsonLoad.php` would, so there's
no reason to spend another round-trip.

Pricing constants are exposed as module-level values. Each has an env
override so a future MAM pricing change can be adjusted without a code
deploy, but the defaults are the authoritative 2026-04 values.

These functions NEVER raise on MAM-side failures — any non-200 or
`success: false` comes back as `BuyResult(success=False, message=...)`.
The scheduler and router layers rely on that: they want to log the
failure and continue, not handle exceptions.

Input validation (weeks not in {4,8,12,"max"}, negative GB, empty
torrent id) raises `ValueError` because that's a programming error
in the caller, distinct from MAM rejecting an otherwise-valid request.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional, Union
from urllib.parse import urlencode

from app.mam.cookie import _do_get
from app.mam.user_status import update_cache_from_buy

_log = logging.getLogger("seshat.mam.bonus_buy")


# ─── Endpoint ────────────────────────────────────────────────

# `www.myanonamouse.net` — all three bonusBuy call shapes route
# through the www host. The `t.` subdomain hosts other /json/
# endpoints (dynamicSeedbox.php) but returns 404 for bonusBuy.php
# regardless of spendtype. Verified against live MAM 2026-04-24.
_BONUS_BUY_URL = "https://www.myanonamouse.net/json/bonusBuy.php"


# ─── Pricing ─────────────────────────────────────────────────

# BP cost defaults, confirmed from MAM's store page 2026-04-23. Env
# vars exist so a MAM-side pricing change can be patched in the field
# without waiting on a Seshat release — the UI deliberately does NOT
# surface these (too easy to misconfigure into failed buys).
BP_PER_UPLOAD_GB: int = int(os.getenv("MAM_BP_PER_UPLOAD_GB", "500"))
BP_PER_VIP_WEEK: int = int(os.getenv("MAM_BP_PER_VIP_WEEK", "1250"))
BP_PER_PERSONAL_FL: int = int(os.getenv("MAM_BP_PER_PERSONAL_FL", "50000"))

# MAM-enforced minimum for programmatic upload-credit buys. A sub-50
# GB purchase comes back with `"Automated spenders are limited to
# buying at least 50 GB of upload at a time, due to log spam"`. The
# web UI has smaller presets, but the API does not. Not env-
# overridable — the floor lives on MAM's side, not ours.
MIN_UPLOAD_GB: int = 50


# ─── Result type ─────────────────────────────────────────────


@dataclass(frozen=True)
class BuyResult:
    """Outcome of a single bonusBuy.php call.

    `success` is the only field callers usually branch on. When True,
    the four `new_*` fields hold MAM's freshly-computed post-buy state;
    the caller is responsible for invalidating or warming whatever
    user-status cache it exposed.

    `amount_echo` is whatever MAM sent back in the `amount` field of
    the response — mostly useful for assertions ("yes, MAM actually
    processed 50 GB, not 5"). `raw` is the full decoded JSON payload,
    preserved so audit rows can store an error code verbatim.

    `dry_run=True` marks a simulated success produced by the
    `mam_economy_dry_run` settings toggle — no HTTP request was
    made. Callers use this to suppress the shared-timestamp bump
    (otherwise a simulated buy would lock out the next scheduler
    tick as if it were real).
    """

    success: bool
    message: str
    new_seedbonus: Optional[float] = None
    new_uploaded_bytes: Optional[int] = None
    new_downloaded_bytes: Optional[int] = None
    new_ratio: Optional[float] = None
    amount_echo: Optional[Union[int, float, str]] = None
    raw: Optional[dict] = None
    dry_run: bool = False


# ─── Dry-run mode ────────────────────────────────────────────


def _is_dry_run() -> bool:
    """True when the mam_economy_dry_run toggle is set.

    Reads lazily from settings.json (mtime-cached inside
    load_settings, so the file isn't re-parsed on every call). If
    settings can't be loaded at all — e.g. during a test that
    didn't set up DATA_DIR — default to False so real code paths
    stay exercised.
    """
    try:
        from app.config import load_settings
        return bool(load_settings().get("mam_economy_dry_run", False))
    except Exception:
        return False


def _dry_run_result(label: str, expected_cost_bp: Optional[int]) -> BuyResult:
    """Synthesize a plausible success for dry-run mode.

    Deliberately minimal: `new_*` fields stay None so the audit row
    shows no real balance change, `cost_points` derives as None in
    the caller (prev − None = nothing written). The message prefix
    `[DRY RUN]` is what the MamPage history tile surfaces to the
    operator so simulated rows don't visually blend with real ones.
    """
    cost_note = (
        f"~{expected_cost_bp:,} BP"
        if expected_cost_bp is not None
        else "unknown BP"
    )
    return BuyResult(
        success=True,
        message=f"[DRY RUN] would spend {cost_note} on {label}",
        raw={"dry_run": True, "expected_cost_bp": expected_cost_bp},
        dry_run=True,
    )


# ─── Public surface ──────────────────────────────────────────


VipWeeks = Union[int, Literal["max"]]


async def buy_vip(
    weeks: VipWeeks, token: Optional[str] = None
) -> BuyResult:
    """Spend BP to extend (or start) a VIP window.

    `weeks` must be 4, 8, 12, or the string "max". MAM caps VIP at 90
    days remaining, so "max" may end up buying fewer weeks than the
    full affordable amount — the actual credit is in the response.
    """
    if weeks != "max" and weeks not in (4, 8, 12):
        raise ValueError(
            f"buy_vip weeks must be 4, 8, 12, or 'max' (got {weeks!r})"
        )
    if _is_dry_run():
        cost = weeks * BP_PER_VIP_WEEK if isinstance(weeks, int) else None
        return _dry_run_result(f"VIP {weeks}w", cost)
    # MAM's VIP endpoint uses `duration=` as the query param, NOT
    # `amount=` — despite the parallel upload endpoint using `amount=`.
    # Passing `amount=` to VIP silently returns HTTP 404. The response
    # still echoes the credited value under `amount` in the JSON body,
    # so downstream parsing of `BuyResult.amount_echo` is unchanged.
    return await _do_buy(
        spendtype="VIP",
        extra_params={"duration": str(weeks)},
        token=token,
        log_label=f"VIP {weeks}w",
    )


async def buy_upload_credit(
    gb: Union[int, float], token: Optional[str] = None
) -> BuyResult:
    """Spend BP to add upload buffer (in gigabytes).

    Accepts floats — MAM's own UI offers 2.5 GB as a preset. The
    caller is responsible for making sure the user can afford it;
    the "Max Affordable" button computes `seedbonus / BP_PER_UPLOAD_GB`
    from a just-fetched `user_status` and passes that as `gb`.
    """
    if not isinstance(gb, (int, float)) or gb <= 0:
        raise ValueError(f"buy_upload_credit gb must be positive number (got {gb!r})")
    if gb < MIN_UPLOAD_GB:
        # Let the ValueError fire BEFORE the dry-run short-circuit so
        # a misconfigured caller gets the same feedback whether or not
        # we'd have hit real MAM. Sub-50 GB buys are MAM-rejected with
        # a log-spam warning, so there's no reason to let them through
        # even in dry-run.
        raise ValueError(
            f"buy_upload_credit gb must be at least {MIN_UPLOAD_GB} GB — "
            f"MAM rejects smaller programmatic buys (got {gb} GB)"
        )
    if _is_dry_run():
        return _dry_run_result(
            f"upload {gb} GB", int(round(gb * BP_PER_UPLOAD_GB)),
        )
    return await _do_buy(
        spendtype="upload",
        extra_params={"amount": str(gb)},
        token=token,
        log_label=f"upload {gb} GB",
    )


async def buy_personal_freeleech(
    torrent_id: str, token: Optional[str] = None
) -> BuyResult:
    """Spend BP to make a specific torrent personal-freeleech.

    Flat 50k BP per call. After this returns success, MAM flags the
    torrent as FL for the user's account, and the subsequent `.torrent`
    grab goes through normally — no `&fl=1` override needed, no wedge
    pool accounting.

    The URL shape for this endpoint is non-obvious: MAM wants the
    epoch-ms cache-buster BOTH as a trailing path segment AND
    duplicated as a `timestamp` query param. Matching that exactly
    is paranoia — the other two spendtypes work with a plain `_=`
    cache-buster, but personal-FL is the one the wider wild
    consistently sends in the path-segment form, so we do too.
    """
    if not torrent_id or not str(torrent_id).strip():
        raise ValueError("buy_personal_freeleech requires a non-empty torrent_id")

    if _is_dry_run():
        return _dry_run_result(
            f"personalFL tid={torrent_id}", BP_PER_PERSONAL_FL,
        )

    ts_ms = _epoch_ms()
    query = urlencode({
        "spendtype": "personalFL",
        "torrentid": str(torrent_id),
        "timestamp": str(ts_ms),
    })
    url = f"{_BONUS_BUY_URL}/{ts_ms}?{query}"
    return await _execute(
        url=url,
        log_label=f"personalFL tid={torrent_id}",
        token=token,
    )


# ─── Internals ───────────────────────────────────────────────


async def _do_buy(
    *,
    spendtype: str,
    extra_params: dict,
    token: Optional[str],
    log_label: str,
) -> BuyResult:
    """Build the VIP/upload URL shape and hand off to `_execute`."""
    params = {"spendtype": spendtype, **extra_params, "_": str(_epoch_ms())}
    # The trailing slash before `?` matches what MAM's own UI emits.
    # Not load-bearing as far as we've seen, but there's no reason to
    # deviate from the shape that's verified against production.
    url = f"{_BONUS_BUY_URL}/?{urlencode(params)}"
    return await _execute(url=url, log_label=log_label, token=token)


async def _execute(
    *, url: str, log_label: str, token: Optional[str]
) -> BuyResult:
    """Hit the endpoint, parse the response, warm the user-status cache.

    Everything below the httpx call is defensive: we log once on the
    failure path so the audit row has something meaningful, but the
    caller only ever sees a `BuyResult`.
    """
    try:
        resp = await _do_get(url, token=token, timeout=20)
    except Exception as exc:
        _log.warning("bonus buy %s: network error: %s", log_label, exc)
        return BuyResult(success=False, message=f"network error: {exc}")

    if resp.status_code != 200:
        preview = resp.text[:200] if resp.text else "(empty)"
        _log.warning(
            "bonus buy %s: HTTP %d (body preview: %s)",
            log_label, resp.status_code, preview,
        )
        return BuyResult(
            success=False,
            message=f"HTTP {resp.status_code} from bonusBuy.php",
        )

    try:
        data = resp.json()
    except Exception as exc:
        preview = resp.text[:200] if resp.text else "(empty)"
        _log.warning(
            "bonus buy %s: invalid JSON: %s (body: %s)",
            log_label, exc, preview,
        )
        return BuyResult(
            success=False,
            message=f"invalid JSON from bonusBuy.php: {preview}",
        )

    if not isinstance(data, dict):
        return BuyResult(
            success=False,
            message=f"unexpected response shape: {type(data).__name__}",
            raw=None,
        )

    if not data.get("success"):
        err = str(data.get("error") or "unknown error")
        _log.warning("bonus buy %s: MAM rejected: %s", log_label, err)
        return BuyResult(success=False, message=err, raw=data)

    # Success — parse the freshly-minted user state. Every field is
    # defensive because MAM has been known to omit one or two fields
    # under load; we'd rather return a partial BuyResult than raise.
    new_seedbonus = _coerce_float(data.get("seedbonus"))
    new_uploaded = _coerce_int(data.get("uploaded"))
    new_downloaded = _coerce_int(data.get("downloaded"))
    new_ratio = _parse_ratio(data.get("ratio"))

    # Warm the user_status cache so dashboard polls don't race a stale
    # value. Only attempts when we have at least a fresh seedbonus —
    # a partial payload isn't worth overwriting the cached baseline.
    if (
        new_seedbonus is not None
        and new_uploaded is not None
        and new_downloaded is not None
    ):
        try:
            update_cache_from_buy(
                token,
                seedbonus=new_seedbonus,
                uploaded_bytes=new_uploaded,
                downloaded_bytes=new_downloaded,
                ratio=new_ratio if new_ratio is not None else 0.0,
            )
        except Exception:
            # Cache warming is best-effort; never let it take down a buy.
            _log.exception("bonus buy %s: cache warm failed (non-fatal)", log_label)

    _log.info(
        "bonus buy %s succeeded (new seedbonus: %s)",
        log_label,
        new_seedbonus if new_seedbonus is not None else "?",
    )
    # Full response body at DEBUG — when the user flips on
    # `verbose_logging`, this gives them a ground-truth capture they
    # can paste back for future dry-run fidelity work. Don't log at
    # INFO: the payload includes current ratio/uploaded/downloaded
    # totals which the operator may not want in a shipped log bundle.
    _log.debug("bonus buy %s raw response: %s", log_label, data)
    return BuyResult(
        success=True,
        message="ok",
        new_seedbonus=new_seedbonus,
        new_uploaded_bytes=new_uploaded,
        new_downloaded_bytes=new_downloaded,
        new_ratio=new_ratio,
        amount_echo=data.get("amount"),
        raw=data,
    )


def _epoch_ms() -> int:
    """Current time as epoch milliseconds — matches MAM's cache-buster format."""
    return int(time.time() * 1000)


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_ratio(value: Any) -> Optional[float]:
    """Ratio comes in one of two shapes from MAM.

    Real bonusBuy.php (verified 2026-04-24 live capture): bare float
    e.g. `94743.55`. That matches jsonLoad.php's shape.

    Defensive dict form `{"source": "...", "parsedValue": N}`: earlier
    memory notes listed this shape, but production MAM does not
    actually use it for the three spendtypes we hit. Keep the branch
    anyway — MAM has historically shipped both forms on adjacent
    endpoints, and the cost of accepting either is two lines.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        parsed = value.get("parsedValue")
        if parsed is not None:
            return _coerce_float(parsed)
        return _coerce_float(value.get("source"))
    return _coerce_float(value)
