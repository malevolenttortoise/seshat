"""
MAM's own view of the snatch budget (2026-10 audit, issue 09).

MAM reports the account's unsatisfied count and limit in
`jsonLoad.php?snatch_summary` (`snatch_summary.unsat = {count, limit}`).
Seshat's own count (active ledger rows + qBit extras) is an inference;
MAM's is the real one, but it's a snapshot MAM caches and Seshat reads
about once an hour. So the budget takes the larger count and the lower
cap, and neither side's blind spot opens a slot:

  - Seshat sees a new grab at once; MAM only after its snapshot refreshes.
  - MAM keeps counting a torrent removed from qBit before 72h of seeding,
    stops counting a duplicate it deleted, and applies its own expiry;
    Seshat can see none of that.

The snapshot lives in `state._snatch_budget["mam"]`. Older than
`STALE_AFTER_S` (or never read, or MAM's response had no summary) →
Seshat's numbers alone, which is the budget as it was before.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from app import state

_log = logging.getLogger("seshat.rate_limit.mam_floor")

# How often the budget watcher reads MAM's summary (Mark, G43), and when
# a snapshot stops counting. A failed read waits the full interval too,
# so a dead cookie or a MAM outage can't turn into a call every tick.
REFRESH_EVERY_S = 3600
STALE_AFTER_S = 3 * 3600

_STATE_KEY = "mam"


@dataclass(frozen=True)
class MamSnatchSummary:
    """The part of MAM's snatch summary the budget uses."""

    unsat_count: int
    unsat_limit: int
    # Torrents MAM still counts that aren't seeding (`inactUnsat` +
    # `inactHnr`): removed from the client before they were satisfied.
    not_seeding: int
    # MAM's own `created` stamp for the snapshot (epoch seconds), if sent.
    as_of: Optional[float]


def parse_snatch_summary(data: Any) -> Optional[MamSnatchSummary]:
    """Pull the budget fields out of a `jsonLoad.php` response, or None.

    MAM's API page documents the `snatch_summary` flag but not its
    fields; the shape here was read live once (2026-10-08). Anything
    that doesn't look like it returns None and the budget falls back to
    Seshat's own count.
    """
    if not isinstance(data, dict):
        return None
    summary = data.get("snatch_summary")
    if not isinstance(summary, dict):
        return None
    unsat = summary.get("unsat")
    if not isinstance(unsat, dict):
        return None
    try:
        count = int(unsat["count"])
        limit = int(unsat.get("limit") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if count < 0 or limit < 0:
        return None
    not_seeding = 0
    for key in ("inactUnsat", "inactHnr"):
        block = summary.get(key)
        if not isinstance(block, dict):
            continue
        try:
            not_seeding += max(0, int(block.get("count") or 0))
        except (TypeError, ValueError):
            continue
    try:
        created = summary.get("created")
        as_of = float(created) if created is not None else None
    except (TypeError, ValueError):
        as_of = None
    return MamSnatchSummary(
        unsat_count=count,
        unsat_limit=limit,
        not_seeding=not_seeding,
        as_of=as_of,
    )


def _slot() -> dict:
    slot = state._snatch_budget.get(_STATE_KEY)
    if not isinstance(slot, dict):
        slot = {}
        state._snatch_budget[_STATE_KEY] = slot
    return slot


def record(summary: MamSnatchSummary, *, now: Optional[float] = None) -> None:
    """Remember a summary MAM just returned."""
    at = time.time() if now is None else now
    slot = _slot()
    slot["summary"] = summary
    slot["fetched_at"] = at
    slot["attempted_at"] = at


def note_attempt(*, now: Optional[float] = None) -> None:
    """A read was tried (whatever came of it); the next waits a full interval."""
    _slot()["attempted_at"] = time.time() if now is None else now


def current(*, now: Optional[float] = None) -> Optional[MamSnatchSummary]:
    """MAM's summary if it's fresh enough to count, else None."""
    slot = state._snatch_budget.get(_STATE_KEY)
    if not isinstance(slot, dict):
        return None
    summary = slot.get("summary")
    fetched_at = slot.get("fetched_at")
    if summary is None or fetched_at is None:
        return None
    at = time.time() if now is None else now
    if at - fetched_at > STALE_AFTER_S:
        return None
    return summary


def fetched_at() -> Optional[float]:
    """When Seshat last got a summary from MAM (epoch seconds), if ever."""
    slot = state._snatch_budget.get(_STATE_KEY)
    return slot.get("fetched_at") if isinstance(slot, dict) else None


def floor_count(seshat_count: int, *, now: Optional[float] = None) -> int:
    """The budget used: the larger of Seshat's count and MAM's (G41)."""
    summary = current(now=now)
    if summary is None:
        return seshat_count
    return max(seshat_count, summary.unsat_count)


def effective_cap(setting_cap: int, *, now: Optional[float] = None) -> int:
    """The budget cap: the lower of the setting and MAM's limit (G42)."""
    summary = current(now=now)
    if summary is None or summary.unsat_limit <= 0:
        return setting_cap
    return min(setting_cap, summary.unsat_limit)


def refresh_due(*, now: Optional[float] = None) -> bool:
    """True when the last read (successful or not) is an interval old."""
    slot = state._snatch_budget.get(_STATE_KEY)
    last = slot.get("attempted_at") if isinstance(slot, dict) else None
    if last is None:
        return True
    at = time.time() if now is None else now
    return at - last >= REFRESH_EVERY_S


async def refresh_if_due(token: str) -> bool:
    """Read MAM's summary when an interval has passed. Returns True on a read.

    Called by the budget watcher every tick; makes at most one MAM call
    per `REFRESH_EVERY_S`. No cookie → no call. The read goes through
    `get_user_status`, which records the summary (and refreshes the
    status cache the economy features share).
    """
    if not token or not refresh_due():
        return False
    from app.mam.user_status import UserStatusError, get_user_status

    note_attempt()
    try:
        await get_user_status(token=token, ttl=0)
    except UserStatusError as exc:
        _log.info(
            "MAM snatch summary unavailable (%s); the budget uses Seshat's "
            "count until the next hourly read",
            exc,
        )
        return False
    return True
