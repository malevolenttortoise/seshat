"""
SSE publisher helpers — pure functions + thin state wrappers that
convert backend events into `sse_broadcast.publish` calls.

Kept out of `budget_watcher.py` and the other hot loops so the diff /
transition logic stays unit-testable without dragging in a fake qBit
and a full `DispatcherDeps`.

Conventions:
  * `diff_torrent_progress(prev, curr)` returns the list of events to
    publish; the caller is responsible for awaiting `publish()`.
  * `_client_reachable` / `_last_torrent_snapshot` are module-global
    per-process state. That matches the single-user model — if we ever
    run two budget watcher loops in one process we have a bigger
    problem than SSE dupe events.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.clients.base import TorrentInfo
from app.mam.user_status import UserStatus
from app.orchestrator import sse_broadcast


# ─── torrent-progress ───────────────────────────────────────

# Previous snapshot keyed by torrent hash, values are the fields we
# diff against. Reset to {} on process start — first tick emits
# events for every active torrent, which is fine (clients just
# paint the initial state).
_last_snapshot: dict[str, tuple[float, int, str]] = {}


@dataclass(frozen=True)
class ProgressEvent:
    """Payload shape for one `torrent-progress` SSE event.

    Matches the frontend's `TorrentProgressEvent` type verbatim so
    `useVisibleEventSource<TorrentProgressEvent>` type-checks end to end.
    """

    hash: str
    name: str
    state: str
    progress: float
    dlspeed: int
    eta: int
    size: int


def _key(t: TorrentInfo) -> tuple[float, int, str]:
    """Fields we care about for change detection.

    `progress` + `state` are the visible-to-user bits; `dlspeed` is
    included so the UI shows a live rate — without it a stalled
    torrent would look identical to one progressing at 0.01% every
    poll and we'd stop publishing events unnecessarily.
    """
    return (round(t.progress, 4), t.dlspeed, t.state)


def diff_torrent_progress(
    current: Iterable[TorrentInfo],
) -> list[ProgressEvent]:
    """Return the list of progress events to publish for this tick.

    Mutates `_last_snapshot` so the next call has the new baseline.
    An event fires when:
      * The torrent is new (not in previous snapshot).
      * Any of (progress, dlspeed, state) changed from the last tick.
    A torrent that disappears from the current snapshot is NOT
    emitted — the UI drops it on its own when the grab row moves to
    a terminal state, and a bare disappearance could just be a qBit
    race (e.g. mid-recheck). The frontend doesn't need a "removed"
    event for the torrent-progress feed.
    """
    events: list[ProgressEvent] = []
    new_snapshot: dict[str, tuple[float, int, str]] = {}
    for t in current:
        if not t.hash:
            continue
        k = _key(t)
        new_snapshot[t.hash] = k
        if _last_snapshot.get(t.hash) != k:
            events.append(ProgressEvent(
                hash=t.hash,
                name=t.name,
                state=t.state,
                progress=t.progress,
                dlspeed=t.dlspeed,
                eta=t.eta,
                size=t.size,
            ))
    _last_snapshot.clear()
    _last_snapshot.update(new_snapshot)
    return events


async def publish_torrent_progress(current: Iterable[TorrentInfo]) -> None:
    """Diff + publish in one call. Safe to invoke even when no clients
    are connected (publish is a no-op then)."""
    for ev in diff_torrent_progress(current):
        await sse_broadcast.publish("torrent-progress", {
            "hash": ev.hash,
            "name": ev.name,
            "state": ev.state,
            "progress": ev.progress,
            "dlspeed": ev.dlspeed,
            "eta": ev.eta,
            "size": ev.size,
        })


# ─── client-status ─────────────────────────────────────────

# None = unknown (pre-first-tick). True/False = last published state.
# Transition-only publish so the stream doesn't re-assert the same
# reachable=True on every 60s tick. Replay-on-connect uses the same
# value so a browser tab opened after the first transition still
# sees the current reachability (otherwise the Downloader pill stays
# stuck at its "unknown" default).
_client_reachable: bool | None = None


async def publish_client_status(reachable: bool) -> None:
    """Publish `client-status` only on transitions.

    First call after process start always publishes so the frontend
    gets an initial state regardless of what its cached `?reachable=`
    was when the tab opened.
    """
    global _client_reachable
    if _client_reachable == reachable:
        return
    _client_reachable = reachable
    await sse_broadcast.publish("client-status", {"reachable": reachable})


def client_status_last_state() -> dict | None:
    """Return the last-published `client-status` payload, or None.

    Used by the SSE route to seed newly-connected subscribers so
    tabs opened AFTER the container's first tick still see the
    current qBit reachability immediately.
    """
    if _client_reachable is None:
        return None
    return {"reachable": _client_reachable}


# ─── mam-stats ─────────────────────────────────────────────

# Two separate trackers: `_last_mam_stats_key` is the rounded tuple
# used to decide whether the NEXT publish should fire (jitter filter).
# `_last_mam_stats_payload` is the full payload of the LAST publish,
# replayed verbatim to new subscribers so they don't have to wait for
# the next rounded-away change to see any value at all. Separating
# them keeps the filter aggressive (sub-0.1 ratio jitter suppressed)
# while still giving new tabs a current snapshot.
_last_mam_stats_key: tuple[float, int, float, int] | None = None
_last_mam_stats_payload: dict | None = None


def _mam_stats_key(s: UserStatus) -> tuple[float, int, float, int]:
    """Fields we diff for change detection.

    Ratio rounds to 1 decimal — MAM ratios run into the thousands,
    so sub-0.1 jitter shouldn't spam events. Seedbonus is whole
    because fractional spend is uncommon. Wedges + upload buffer
    are integer fields already.
    """
    return (
        round(s.ratio, 1),
        int(s.seedbonus),
        float(s.upload_buffer_bytes),
        s.wedges,
    )


async def publish_mam_stats(status: UserStatus) -> None:
    """Publish `mam-stats` on changes to the user's economic fields.

    Fires from two places today:
      * `get_user_status` after a successful jsonLoad.php refresh.
      * `bonus_buy` callers, after `update_cache_from_buy` warms the
        cache from a fresh buy response — this gives the UI an
        immediate post-action update without waiting for the next
        periodic poll.
    """
    global _last_mam_stats_key, _last_mam_stats_payload
    key = _mam_stats_key(status)
    if _last_mam_stats_key == key:
        return
    _last_mam_stats_key = key
    payload = {
        "ratio": status.ratio,
        "seedbonus": status.seedbonus,
        "upload_buffer_bytes": status.upload_buffer_bytes,
        "wedges": status.wedges,
    }
    _last_mam_stats_payload = payload
    await sse_broadcast.publish("mam-stats", payload)


def mam_stats_last_state() -> dict | None:
    """Return the last-published `mam-stats` payload, or None."""
    return _last_mam_stats_payload


# ─── Subscriber seeding ────────────────────────────────────

def seed_new_subscriber(queue) -> None:
    """Push current stateful-event values to a freshly-registered queue.

    Called synchronously by the SSE route handler right after
    `sse_broadcast.register()` so new subscribers immediately receive
    the last-known `client-status` + `mam-stats` values instead of
    waiting for the NEXT publish (which, for steady-state reachable=
    True + unchanged ratio, would never come).

    `torrent-progress` is deliberately not seeded: a full snapshot of
    every active torrent would flood a fresh queue on every connect,
    and the budget watcher's next tick (≤60s) will re-emit any
    changed torrents naturally. The BookSidebar live-progress wiring
    would need its own snapshot API on connect, not replay.

    `toast` is ephemeral by design — old toasts shouldn't replay on
    reconnect (the user already saw them, or they're stale context).
    """
    cs = client_status_last_state()
    if cs is not None:
        queue.put_nowait(("client-status", cs))
    ms = mam_stats_last_state()
    if ms is not None:
        queue.put_nowait(("mam-stats", ms))


# ─── toast ─────────────────────────────────────────────────

# Valid toast levels, matching the frontend's `lib/toast.ts` vocabulary.
# Any out-of-band value gets coerced to "info" on publish so the UI
# always has a known level to render.
_TOAST_LEVELS = {"success", "info", "warn", "error"}


async def publish_toast(level: str, message: str) -> None:
    """Push an ephemeral in-browser notification to every subscriber.

    Complementary to the existing ntfy path: ntfy is durable push to
    the user's phone, toast is a transient flash while they have the
    tab open. Both fire for meaningful background events (grab
    injected, scan complete, buffer gate tripped, bonus-buy outcome)
    so the user sees them regardless of which surface they're on.

    `level` maps to the frontend's four toast kinds — anything else
    coerces to "info".
    """
    if level not in _TOAST_LEVELS:
        level = "info"
    await sse_broadcast.publish("toast", {"level": level, "message": message})


# ─── Test hooks ────────────────────────────────────────────

def reset_for_tests() -> None:
    """Clear the module-global state between tests."""
    global _client_reachable, _last_mam_stats_key, _last_mam_stats_payload
    _last_snapshot.clear()
    _client_reachable = None
    _last_mam_stats_key = None
    _last_mam_stats_payload = None
