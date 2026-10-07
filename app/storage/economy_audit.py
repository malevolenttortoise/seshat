"""
CRUD for the `economy_audit` table.

Every economy-engine path writes exactly one row here, including skips:

  - VIP / upload auto-buy loops (scheduled trigger) — one row per tick,
    even when the decision was "skip_disabled" / "skip_below_interval" /
    "skip_no_trigger". Skips are load-bearing because the UI's
    "Auto-buy history" answers "why didn't the loop fire last tick?"
    from exactly this table.
  - Manual "Buy now" button — one row with `trigger='manual'`.
  - Personal-freeleech buy attached to a grab (F4) — one row with
    `action='personal_fl'` and `torrent_id` set.
  - Buffer-gate skip — one row with `action='buffer_gate_block'`,
    `outcome='buffer_gate_block'`, and `trigger` set to `irc_autograb`
    or `user_grab` so the UI can distinguish automatic blocks from
    manual-inject blocks.

The module is intentionally thin: the callers (scheduler, router,
dispatcher) compose the field values from their own context. This
module only persists them and reads them back. Keeping it dumb means
the decision logic in `app/mam/economy.py` stays pure and the audit
columns stay easy to reason about.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import aiosqlite

_log = logging.getLogger("seshat.storage.economy_audit")


# ─── Column vocabulary ──────────────────────────────────────
# Kept as plain string constants so SQL queries remain obvious. The
# scheduler, router, and dispatcher all reference these by name.

ACTION_VIP = "vip"
ACTION_UPLOAD = "upload"
ACTION_PERSONAL_FL = "personal_fl"
ACTION_BUFFER_GATE_BLOCK = "buffer_gate_block"
# A freeleech wedge spent through `download.php?fl` (D30).
ACTION_WEDGE = "wedge"

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"
TRIGGER_IRC_AUTOGRAB = "irc_autograb"
TRIGGER_USER_GRAB = "user_grab"

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_BUFFER_GATE_BLOCK = "buffer_gate_block"

# Skip outcomes mirror `EconomyDecision.reason` values, prefixed with
# `skip_` so the UI can distinguish them from the non-skip outcomes
# at a glance. The scheduler composes these with `f"skip_{reason}"`.
OUTCOME_SKIP_DISABLED = "skip_disabled"
OUTCOME_SKIP_BELOW_INTERVAL = "skip_below_interval"
OUTCOME_SKIP_NO_TRIGGER = "skip_no_trigger"
OUTCOME_SKIP_INSUFFICIENT_BONUS = "skip_insufficient_bonus"


@dataclass(frozen=True)
class EconomyAuditRow:
    """One row from the `economy_audit` table, shaped for UI rendering."""

    id: int
    occurred_at: str
    action: str
    trigger: str
    outcome: str
    mode: Optional[str] = None
    amount: Optional[str] = None
    torrent_id: Optional[str] = None
    tier: Optional[str] = None
    message: Optional[str] = None
    cost_points: Optional[float] = None
    user_bonus_after: Optional[float] = None


# ─── Writes ─────────────────────────────────────────────────


async def record(
    db: aiosqlite.Connection,
    *,
    action: str,
    trigger: str,
    outcome: str,
    mode: Optional[str] = None,
    amount: Optional[str] = None,
    torrent_id: Optional[str] = None,
    tier: Optional[str] = None,
    message: Optional[str] = None,
    cost_points: Optional[float] = None,
    user_bonus_after: Optional[float] = None,
) -> int:
    """Append one audit row; return its rowid.

    All kwargs are keyword-only so call sites at three different
    orchestration layers stay legible. The required fields (action,
    trigger, outcome) match the NOT NULL columns — anything less and
    the row would be unusable in the UI.
    """
    cursor = await db.execute(
        """
        INSERT INTO economy_audit
            (action, trigger, mode, amount, torrent_id,
             outcome, tier, message, cost_points, user_bonus_after)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action, trigger, mode, amount, torrent_id,
            outcome, tier, message, cost_points, user_bonus_after,
        ),
    )
    await db.commit()
    return cursor.lastrowid or 0


# ─── Reads ──────────────────────────────────────────────────


async def list_recent(
    db: aiosqlite.Connection,
    *,
    limit: int = 50,
    action: Optional[str] = None,
) -> list[EconomyAuditRow]:
    """Most-recent-first audit rows, optionally filtered by action.

    Ordered by `id DESC` rather than `occurred_at DESC` because the
    timestamp has second-granularity and two rows in the same second
    would otherwise be returned in undefined order. The id is
    monotonic per the autoincrement PK — good enough.
    """
    if action:
        cursor = await db.execute(
            """
            SELECT id, occurred_at, action, trigger, mode, amount,
                   torrent_id, outcome, tier, message,
                   cost_points, user_bonus_after
            FROM economy_audit
            WHERE action = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (action, limit),
        )
    else:
        cursor = await db.execute(
            """
            SELECT id, occurred_at, action, trigger, mode, amount,
                   torrent_id, outcome, tier, message,
                   cost_points, user_bonus_after
            FROM economy_audit
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
    rows = await cursor.fetchall()
    return [_row_to_dataclass(r) for r in rows]


async def latest_success(
    db: aiosqlite.Connection, *, action: str
) -> Optional[EconomyAuditRow]:
    """Most recent successful buy of the given action type, or None.

    Used by the MamPage status tile ("Last bought: 2h ago") and by
    the scheduler's defense-in-depth check when `settings`'s
    `mam_economy_last_*_buy_at` sentinel drifts (e.g. after a manual
    settings.json edit).
    """
    cursor = await db.execute(
        """
        SELECT id, occurred_at, action, trigger, mode, amount,
               torrent_id, outcome, tier, message,
               cost_points, user_bonus_after
        FROM economy_audit
        WHERE action = ? AND outcome = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (action, OUTCOME_SUCCESS),
    )
    row = await cursor.fetchone()
    return _row_to_dataclass(row) if row else None


# ─── Internals ──────────────────────────────────────────────


def _row_to_dataclass(row) -> EconomyAuditRow:
    """Convert an aiosqlite Row into our frozen EconomyAuditRow."""
    return EconomyAuditRow(
        id=int(row["id"]),
        occurred_at=str(row["occurred_at"]),
        action=str(row["action"]),
        trigger=str(row["trigger"]),
        outcome=str(row["outcome"]),
        mode=row["mode"],
        amount=row["amount"],
        torrent_id=row["torrent_id"],
        tier=row["tier"],
        message=row["message"],
        cost_points=row["cost_points"],
        user_bonus_after=row["user_bonus_after"],
    )
