"""
CRUD for the `grabs` and `announces` tables.

Every dispatcher decision produces an `announces` audit row, and
every "yes, fetch this" decision produces a `grabs` row that tracks
state through the pipeline (fetched → submitted → completed / failed).

State machine for `grabs.state`:

    pending_queue → fetched → submitted → downloading → complete
                                       ↘ failed
                       ↘ failed_cookie_expired
                       ↘ failed_torrent_gone
                       ↘ failed_qbit_rejected
                       ↘ failed_unknown

The dispatcher writes the initial state at insert time; later
phases (the qBit poller, the post-download stages) update it as
the torrent moves through the rest of the pipeline.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional, Sequence

import aiosqlite

from app.filter.gate import Decision

_log = logging.getLogger("seshat.storage.grabs")


# Grab states — kept as plain string constants rather than an Enum
# so SQL queries against them are obvious. The dispatcher and the
# tests both reference these by name.
STATE_PENDING_QUEUE = "pending_queue"
STATE_FETCHED = "fetched"
STATE_SUBMITTED = "submitted"
STATE_FAILED_COOKIE_EXPIRED = "failed_cookie_expired"
STATE_FAILED_TORRENT_GONE = "failed_torrent_gone"
STATE_FAILED_QBIT_REJECTED = "failed_qbit_rejected"
STATE_FAILED_UNKNOWN = "failed_unknown"
# qBit reported the torrent already exists in the client. Not a real
# failure (the torrent IS in qBit, which is what Seshat wanted),
# but the dispatcher couldn't verify the add it expected — the
# `qbit_hash` we computed via info_hash IS the existing torrent's
# hash, so the ledger entry is still meaningful. Future iteration
# could detect this as soft-success and stop counting it as failed.
STATE_DUPLICATE_IN_QBIT = "duplicate_in_qbit"

# Phase 2 post-download states.
STATE_DOWNLOADING = "downloading"
STATE_DOWNLOADED = "downloaded"
STATE_PROCESSING = "processing"
STATE_COMPLETE = "complete"
# Every book the grab staged was rejected at review (2026-10 audit
# issue 11). Terminal. MAM served the bytes, so the torrent ID stays
# blocked (BLOCKING_STATES, ADR-0022); it is NOT in format dedup's
# in-flight set, so another format of the book can still be grabbed
# (Mark, G37). Before this state, rejected grabs sat in `processing`.
STATE_REJECTED = "rejected"


@dataclass(frozen=True)
class GrabRow:
    """One row from the `grabs` table."""

    id: int
    announce_id: Optional[int]
    mam_torrent_id: str
    torrent_name: str
    category: str
    author_blob: str
    torrent_file_path: Optional[str]
    qbit_hash: Optional[str]
    state: str
    grabbed_at: str
    submitted_at: Optional[str]
    failed_reason: Optional[str]


# ─── Announces (audit log) ───────────────────────────────────


async def record_announce(
    db: aiosqlite.Connection,
    *,
    raw: str,
    torrent_id: str,
    torrent_name: str,
    category: str,
    author_blob: str,
    decision: Decision,
    filetype: str = "",
    categories: Sequence[str] = (),
) -> int:
    """Insert one row in the `announces` table.

    Called for EVERY announce the dispatcher sees, regardless of
    whether the filter allowed it. The `decision` field captures
    the filter outcome so the audit log + UI can show why a given
    announce was allowed or skipped.

    `filetype` is the lowercased IRC `Filetype: ( xxx )` value
    (epub / azw3 / m4b / ...). v2.9.0 persists this for audit so
    format-dedup decisions can be reviewed retroactively. Pre-v2.9.0
    callers that didn't pass it land an empty string, which the
    schema accepts via the column's NULL-able definition.

    `categories` is every MAM content tag the announce carried (IRC
    announces since MAM's 2026-08-11 format), stored as a JSON list
    beside the single `category` string (wave 5a, G124).
    """
    cursor = await db.execute(
        """
        INSERT INTO announces
            (raw, torrent_id, torrent_name, category, author_blob,
             decision, decision_reason, matched_author, filetype,
             categories_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            raw,
            torrent_id,
            torrent_name,
            category,
            author_blob,
            decision.action,
            decision.reason,
            decision.matched_author,
            (filetype or "").lower() or None,
            categories_json(categories),
        ),
    )
    await db.commit()
    return cursor.lastrowid or 0


def categories_json(categories: Sequence[str]) -> Optional[str]:
    """MAM content tags as stored (`categories_json`): a JSON list, or
    NULL when there's none to keep (one category is already `category`)."""
    tags = [str(c).strip() for c in categories or () if str(c).strip()]
    return json.dumps(tags, ensure_ascii=False) if tags else None


def parse_categories(raw: Optional[str]) -> Optional[list[str]]:
    """`categories_json` back to a list; None when absent or unreadable."""
    if not raw:
        return None
    try:
        tags = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return [str(t) for t in tags] if isinstance(tags, list) else None


async def announce_categories(
    db: aiosqlite.Connection, announce_id: Optional[int],
) -> list[str]:
    """The content tags recorded on an announce row (empty if none)."""
    if not announce_id:
        return []
    row = await (await db.execute(
        "SELECT categories_json FROM announces WHERE id = ?", (announce_id,),
    )).fetchone()
    return parse_categories(row["categories_json"] if row else None) or []


async def update_announce_decision(
    db: aiosqlite.Connection,
    *,
    announce_id: int,
    action: str,
    reason: str,
) -> None:
    """Overwrite an existing announce row's decision + reason.

    Used by the v2.9.0 format-dedup gate, which runs AFTER the filter
    has already recorded its allow/skip verdict. When dedup overrides
    the outcome (filter said allow, dedup says skip or hold) we update
    the same audit row in-place rather than writing a second row —
    keeping one-row-per-announce makes the dashboard counts clean.
    """
    await db.execute(
        "UPDATE announces SET decision = ?, decision_reason = ? "
        "WHERE id = ?",
        (action, reason, announce_id),
    )
    await db.commit()


# ─── Grabs (lifecycle) ───────────────────────────────────────


async def create_grab(
    db: aiosqlite.Connection,
    *,
    announce_id: Optional[int],
    mam_torrent_id: str,
    torrent_name: str,
    category: str,
    author_blob: str,
    state: str,
    qbit_hash: Optional[str] = None,
    is_reingest: bool = False,
    book_format: str = "",
    dedup_key: str = "",
    policy_tier: str = "",
    categories: Sequence[str] = (),
) -> int:
    """Insert a new row in the `grabs` table.

    Called by the dispatcher right after the filter says "allow".
    The initial state depends on what the dispatcher is about to do
    next: `STATE_FETCHED` for the immediate-submit path,
    `STATE_PENDING_QUEUE` for the queue-mode path.

    `qbit_hash` and `is_reingest` are populated up-front by the v2.8.0
    reingest path (which skips the MAM fetch + qBit submit phases —
    the file is already on disk, the hash is already known). Normal
    grab callers leave them at their defaults; `qbit_hash` gets
    stamped later via `set_state(STATE_SUBMITTED, qbit_hash=...)`.

    `book_format` and `dedup_key` are the v2.9.0 format-dedup fields.
    The dispatcher computes both at announce time (book_format from
    `announce.filetype`; dedup_key from
    `app.orchestrator.format_dedup.normalize_dedup_key`) and stamps
    them here so subsequent dedup-gate lookups can find this grab as
    an "in-flight sibling" via the `idx_grabs_dedup_key` index.
    Empty strings are written as NULL — pre-v2.9.0 grabs and reingests
    have no announce-time filetype hint.

    `policy_tier` is the grab policy's tier for this grab (`vip`,
    `free`, `normal`, ...; `policy/engine.py`), so a grab's economics can
    be read back later (2026-10 audit issue 11). Empty → NULL: grabs that
    never went through the policy (adoptions, reingests, older rows).

    `categories`: the announce's MAM content tags (wave 5a, G124).
    """
    cursor = await db.execute(
        """
        INSERT INTO grabs
            (announce_id, mam_torrent_id, torrent_name, category,
             author_blob, state, qbit_hash, is_reingest,
             book_format, dedup_key, policy_tier, categories_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            announce_id,
            mam_torrent_id,
            torrent_name,
            category,
            author_blob,
            state,
            qbit_hash,
            1 if is_reingest else 0,
            (book_format or "").lower() or None,
            dedup_key or None,
            policy_tier or None,
            categories_json(categories),
        ),
    )
    await db.commit()
    return cursor.lastrowid or 0


async def set_torrent_name(
    db: aiosqlite.Connection, grab_id: int, torrent_name: str,
) -> None:
    """Overwrite the human-readable torrent name on a grab row.

    Used by the review-queue edit flow when the user corrects a
    bad title (e.g. a `manual_inject_<id>` placeholder). The new
    value flows into the dashboard's Snatch Budget widget, the
    Recent Activity feed, and anywhere else that renders
    `grabs.torrent_name`.
    """
    await db.execute(
        "UPDATE grabs SET torrent_name = ? WHERE id = ?",
        (torrent_name, grab_id),
    )
    await db.commit()


async def set_state(
    db: aiosqlite.Connection,
    grab_id: int,
    state: str,
    *,
    failed_reason: Optional[str] = None,
    qbit_hash: Optional[str] = None,
    torrent_file_path: Optional[str] = None,
    failed_with_cookie_id: Optional[int] = None,
) -> None:
    """Transition a grab to a new state.

    Updates `state_updated_at` automatically and bumps `submitted_at`
    when transitioning to `STATE_SUBMITTED`. Optional fields are
    only written if explicitly passed (so re-calling for state-only
    updates doesn't clobber the hash or file path).
    `failed_with_cookie_id` is `app.mam.cookie.fingerprint()` of the
    cookie a `failed_cookie_expired` download was refused with.
    """
    sets = ["state = ?", "state_updated_at = datetime('now')"]
    params: list = [state]

    if state == STATE_SUBMITTED:
        sets.append("submitted_at = datetime('now')")
    if failed_reason is not None:
        sets.append("failed_reason = ?")
        params.append(failed_reason)
    if qbit_hash is not None:
        sets.append("qbit_hash = ?")
        params.append(qbit_hash)
    if torrent_file_path is not None:
        sets.append("torrent_file_path = ?")
        params.append(torrent_file_path)
    if failed_with_cookie_id is not None:
        sets.append("failed_with_cookie_id = ?")
        params.append(failed_with_cookie_id)

    params.append(grab_id)
    await db.execute(
        f"UPDATE grabs SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    await db.commit()


async def get_grab(
    db: aiosqlite.Connection, grab_id: int
) -> Optional[GrabRow]:
    """Fetch one grab by id."""
    cursor = await db.execute(
        """
        SELECT id, announce_id, mam_torrent_id, torrent_name, category,
               author_blob, torrent_file_path, qbit_hash, state, grabbed_at,
               submitted_at, failed_reason
        FROM grabs WHERE id = ?
        """,
        (grab_id,),
    )
    row = await cursor.fetchone()
    return _row_to_grab(row) if row else None


async def get_source_metadata(
    db: aiosqlite.Connection, grab_id: int
) -> Optional[str]:
    """Fetch the raw source_metadata JSON blob for a grab, if any.

    Separated from `get_grab` so the GrabRow dataclass stays narrow
    and callers that don't care about the blob (the vast majority)
    don't pay any cost. Only the pipeline's _prepare_book reads this
    column — to short-circuit the enricher when the submitter pre-
    baked metadata at submission time.
    """
    cursor = await db.execute(
        "SELECT source_metadata FROM grabs WHERE id = ?", (grab_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return row[0]  # None if column is NULL


async def find_grab_by_torrent_id(
    db: aiosqlite.Connection, mam_torrent_id: str
) -> Optional[GrabRow]:
    """Look up the most recent grab for a given MAM torrent ID, in any
    state. For the snatch-safety question ("may we fetch this torrent
    again?") use `find_blocking_grab` — the most recent row can be a
    harmless pre-fetch failure sitting on top of an earlier download.
    """
    cursor = await db.execute(
        """
        SELECT id, announce_id, mam_torrent_id, torrent_name, category,
               author_blob, torrent_file_path, qbit_hash, state, grabbed_at,
               submitted_at, failed_reason
        FROM grabs WHERE mam_torrent_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (mam_torrent_id,),
    )
    row = await cursor.fetchone()
    return _row_to_grab(row) if row else None


# States that mean a grab of this torrent is in flight or MAM has
# already served its .torrent. `pending_queue` and `fetched` are written
# by `create_grab` BEFORE the fetch, so they also cover a concurrent
# grab that hasn't reached MAM yet. `duplicate_in_qbit` and
# `failed_qbit_rejected` are only ever written after a successful fetch.
# The remaining failure states are ambiguous by name (`failed_torrent_gone`
# is also the download watcher's "absent from qBit for 12h"), so
# `find_blocking_grab` tells them apart by `qbit_hash`, which is only
# stamped once MAM has handed over the bytes.
BLOCKING_STATES = frozenset({
    STATE_PENDING_QUEUE,
    STATE_FETCHED,
    STATE_SUBMITTED,
    STATE_DOWNLOADING,
    STATE_DOWNLOADED,
    STATE_PROCESSING,
    STATE_COMPLETE,
    STATE_REJECTED,
    STATE_DUPLICATE_IN_QBIT,
    STATE_FAILED_QBIT_REJECTED,
})


async def find_blocking_grab(
    db: aiosqlite.Connection,
    mam_torrent_id: str,
    *,
    exclude_grab_id: Optional[int] = None,
) -> Optional[GrabRow]:
    """The most recent grab that forbids fetching this torrent again.

    Snatch safety: MAM sees one download per torrent, ever. A row
    blocks when MAM already served the bytes (`qbit_hash` set) or a
    grab is still in flight (`BLOCKING_STATES`). A pre-fetch failure —
    cookie expired, 404, network error, all with no hash — does not
    block, so the user can retry it.

    `exclude_grab_id` lets the cookie-retry job ask "does anything
    OTHER than the row I'm about to retry block this torrent?"
    """
    if not mam_torrent_id:
        return None
    placeholders = ", ".join("?" for _ in BLOCKING_STATES)
    cursor = await db.execute(
        f"""
        SELECT id, announce_id, mam_torrent_id, torrent_name, category,
               author_blob, torrent_file_path, qbit_hash, state, grabbed_at,
               submitted_at, failed_reason
        FROM grabs
        WHERE mam_torrent_id = ?
          AND id != ?
          AND (qbit_hash IS NOT NULL OR state IN ({placeholders}))
        ORDER BY id DESC LIMIT 1
        """,
        (mam_torrent_id, exclude_grab_id or 0, *sorted(BLOCKING_STATES)),
    )
    row = await cursor.fetchone()
    return _row_to_grab(row) if row else None


async def find_grab_by_hash(
    db: aiosqlite.Connection, qbit_hash: str,
) -> Optional[GrabRow]:
    """The most recent grab carrying this info hash, in any state.

    Manual Grab's upload check (ADR-0023): a .torrent whose hash Seshat
    already holds is the same torrent, whatever ID its grab row has.
    Orphan-adopted rows (no torrent ID) are only findable this way.
    """
    if not qbit_hash:
        return None
    cursor = await db.execute(
        """
        SELECT id, announce_id, mam_torrent_id, torrent_name, category,
               author_blob, torrent_file_path, qbit_hash, state, grabbed_at,
               submitted_at, failed_reason
        FROM grabs WHERE qbit_hash = ?
        ORDER BY id DESC LIMIT 1
        """,
        (qbit_hash.lower(),),
    )
    row = await cursor.fetchone()
    return _row_to_grab(row) if row else None


def _row_to_grab(row) -> GrabRow:
    return GrabRow(
        id=int(row["id"]),
        announce_id=row["announce_id"],
        mam_torrent_id=str(row["mam_torrent_id"] or ""),
        torrent_name=str(row["torrent_name"] or ""),
        category=str(row["category"] or ""),
        author_blob=str(row["author_blob"] or ""),
        torrent_file_path=row["torrent_file_path"],
        qbit_hash=row["qbit_hash"],
        state=str(row["state"] or ""),
        grabbed_at=str(row["grabbed_at"] or ""),
        submitted_at=row["submitted_at"],
        failed_reason=row["failed_reason"],
    )
