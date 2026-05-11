"""Per-library sync state.

Tracks last-observed source mtime, last successful sync wall-clock
timestamp, and last *full* sync timestamp. Drives the incremental-vs-
full decision in `sync_calibre` / `sync_audiobookshelf`.

State shape (under `settings["library_sync_state"]`):

    {
        "<slug>": {
            "last_mtime":         <number or string, from get_mtime()>,
            "last_sync_ts":       <float unix seconds — success of ANY sync>,
            "last_full_sync_ts":  <float unix seconds — success of a FULL sync>,
        }
    }

Why three fields:
- `last_mtime`        — gates the mtime fast-path skip (existing behavior).
- `last_sync_ts`      — floor for the `last_modified > X` incremental query.
- `last_full_sync_ts` — weekly safety-net trigger; a full sync periodically
                       re-covers any drift the incremental path might miss
                       (e.g. external scripts that bypass `calibredb` and
                       write directly to metadata.db without bumping
                       last_modified).
"""
from __future__ import annotations

import time
from typing import Any

WEEKLY_FULL_SECONDS = 7 * 86400
DRIFT_BIAS_SECONDS = 60.0

MODE_FULL_FIRST = "first_sync"
MODE_FULL_WEEKLY = "weekly_safety_net"
MODE_FULL_RECOVERY = "recovery"
MODE_INCREMENTAL = "incremental"


def _empty_entry() -> dict[str, Any]:
    return {"last_mtime": None, "last_sync_ts": 0.0, "last_full_sync_ts": 0.0}


def migrate_settings(settings: dict[str, Any]) -> bool:
    """Fold legacy `library_mtimes` entries into `library_sync_state`.

    Idempotent — safe to run on every startup. Slugs already present in
    the new store are left untouched (their timestamps are more accurate
    than the legacy zero-defaults we'd write). Returns True iff the
    settings dict was mutated, so the caller knows whether to persist.
    """
    legacy = settings.get("library_mtimes") or {}
    if not legacy:
        return False
    store = settings.setdefault("library_sync_state", {})
    changed = False
    for slug, mtime in legacy.items():
        if slug in store:
            continue
        entry = _empty_entry()
        entry["last_mtime"] = mtime
        store[slug] = entry
        changed = True
    return changed


def get_state(settings: dict[str, Any], slug: str) -> dict[str, Any]:
    """Return the per-slug state, with defaults filled in for missing keys."""
    store = settings.get("library_sync_state") or {}
    entry = store.get(slug) or {}
    return {
        "last_mtime": entry.get("last_mtime"),
        "last_sync_ts": float(entry.get("last_sync_ts") or 0.0),
        "last_full_sync_ts": float(entry.get("last_full_sync_ts") or 0.0),
    }


def record_completion(
    settings: dict[str, Any],
    slug: str,
    *,
    mtime: Any,
    mode: str,
) -> None:
    """Stamp a successful sync. `mode` is "full" or "incremental".

    Always advances `last_sync_ts`; "full" additionally advances
    `last_full_sync_ts`. Writes the same mtime to the legacy
    `library_mtimes` mirror so a downgrade to a pre-sync-state release
    still reads correct mtimes — drop the mirror in the release after
    this one.
    """
    if mode not in ("full", "incremental"):
        raise ValueError(f"unknown sync mode: {mode!r}")
    now = time.time()
    store = settings.setdefault("library_sync_state", {})
    entry = dict(store.get(slug) or _empty_entry())
    entry["last_mtime"] = mtime
    entry["last_sync_ts"] = now
    if mode == "full":
        entry["last_full_sync_ts"] = now
    store[slug] = entry
    legacy = settings.setdefault("library_mtimes", {})
    legacy[slug] = mtime


def record_failure(settings: dict[str, Any], slug: str) -> None:
    """Force the next sync for `slug` to escalate to full.

    Use when an incremental sync throws mid-run so Seshat can't drift
    silently from a half-applied state. Preserves `last_mtime` so the
    mtime fast-path still correctly skips on "source unchanged" — the
    forced full sync only fires when the source actually moves.
    """
    store = settings.setdefault("library_sync_state", {})
    entry = dict(store.get(slug) or _empty_entry())
    entry["last_sync_ts"] = 0.0
    store[slug] = entry


def resolve_threshold(
    state: dict[str, Any],
    *,
    now: float | None = None,
    weekly_full_seconds: float = WEEKLY_FULL_SECONDS,
    drift_bias_seconds: float = DRIFT_BIAS_SECONDS,
) -> tuple[float | None, str]:
    """Decide incremental vs full for the next sync.

    Returns `(threshold, reason)`. `threshold is None` means the caller
    should do a full sync; otherwise the float is a unix-seconds floor
    for a `last_modified > threshold` query, biased back by
    `drift_bias_seconds` to absorb clock jitter between the host running
    Seshat and the host writing the source DB.
    """
    now_ = now if now is not None else time.time()
    last_sync = state.get("last_sync_ts") or 0.0
    last_full = state.get("last_full_sync_ts") or 0.0

    if not last_full:
        return None, MODE_FULL_FIRST
    if (now_ - last_full) >= weekly_full_seconds:
        return None, MODE_FULL_WEEKLY
    if not last_sync:
        return None, MODE_FULL_RECOVERY
    return last_sync - drift_bias_seconds, MODE_INCREMENTAL
