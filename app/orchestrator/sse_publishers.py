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
# reachable=True on every 60s tick.
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


# ─── Test hooks ────────────────────────────────────────────

def reset_for_tests() -> None:
    """Clear the module-global snapshot + reachability between tests."""
    global _client_reachable
    _last_snapshot.clear()
    _client_reachable = None
