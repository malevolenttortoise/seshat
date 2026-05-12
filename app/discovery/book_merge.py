"""
Shared books-row merge logic — folds two rows representing the same
work into one, with an audit row.

Used by:
  - `calibre_sync.py`'s post-UPDATE sweep, which heals
    title-mismatch duplicates left over from a previous sync after
    the user fixes Calibre metadata.
  - The manual-merge HTTP endpoint, which lets a user resolve a
    duplicate by searching the library and clicking Merge.

Both paths converge on `merge_books()` so field resolution, FK
redirect, and audit row format stay consistent. The two callers
differ only in how they decide which row is the winner — the sweep
always passes the calibre row as winner; the HTTP endpoint computes
the winner from the two row states (see `_pick_winner`).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import aiosqlite

_log = logging.getLogger("seshat.discovery.book_merge")


# Identity fields — unique-ish external IDs and torrent metadata.
# Resolution: COALESCE(winner, loser) — winner's value wins if set,
# loser fills in the gaps. These are the most valuable fields to
# carry over from the loser since the unowned discovery row often
# holds the only goodreads_id / mam_torrent_id / etc. the library
# has for that work.
_IDENTITY_FIELDS = (
    "isbn",
    "hardcover_id",
    "goodreads_id",
    "fictiondb_id",
    "kobo_id",
    "amazon_id",
    "google_books_id",
    "ibdb_id",
    "audiobookshelf_id",
    "asin",
    "mam_torrent_id",
    "mam_url",
    "mam_status",
    "mam_formats",
    "mam_category",
    "source_url",
)

# Metadata fields — descriptive content. Resolution: prefer winner
# when non-null/non-empty; loser fills in the gaps. Same coalesce
# semantics as identity fields but stylistically separate so future
# field additions land in the right bucket.
_METADATA_FIELDS = (
    "cover_url",
    "cover_path",
    "cover_phash",
    "pub_date",
    "expected_date",
    "description",
    "page_count",
    "language",
    "rating",
    "tags",
    "publisher",
    "formats",
    "narrator",
    "duration_sec",
    "audio_formats",
    "series_id",
    "series_index",
)


class MergeError(Exception):
    """Raised when a merge cannot proceed safely.

    Distinct exception class so callers (HTTP endpoint vs. sweep)
    can decide whether to surface the error to the user or just log
    and continue.
    """


async def merge_books(
    discovery_db: aiosqlite.Connection,
    pipeline_db: aiosqlite.Connection,
    *,
    library_slug: str,
    winner_id: int,
    loser_id: int,
    reason: str,
) -> dict[str, Any]:
    """Fold `loser_id` into `winner_id` and audit the merge.

    `discovery_db` is the per-library books DB; `pipeline_db` is the
    global seshat.db (where `book_grab_links` lives). Both must be
    passed in by the caller and are NOT closed here — callers manage
    their own connection lifecycle.

    `library_slug` is the slug the discovery_db was opened with. The
    `book_grab_links` redirect is scoped to (slug, book_id) per the
    multi-library safety rule.

    `reason` is a short tag stored in the audit row — typically
    `"calibre_sync_post_update"` for the sweep or `"manual"` for the
    HTTP endpoint.

    Returns the post-merge winner row as a dict. Raises `MergeError`
    on any precondition violation (same id, missing row, two
    calibre+owned rows, etc.). On error the discovery_db transaction
    is rolled back via aiosqlite's exception handling — callers
    should not see partial state.

    Field resolution rules:
      - `owned` → max of both (1 if either)
      - `source` → 'calibre' if either is calibre, else winner's
      - `calibre_id` → coalesce(winner, loser)  — for the sweep this
        always picks the calibre row's id since the loser has NULL
      - `hidden` → MIN(both)  — visible wins
      - `is_new` → MIN(both)  — false wins (once dismissed, stays)
      - `mam_has_multiple` → MAX(both)
      - `mam_my_snatched` → MAX(both)
      - `is_unreleased` → MIN(both)  — released wins
      - `is_omnibus` → MAX(both)  — flag survives
      - `mam_is_bundle` → MAX(both)
      - identity fields (see `_IDENTITY_FIELDS`) → coalesce(winner, loser)
      - metadata fields (see `_METADATA_FIELDS`) → coalesce(winner, loser)
      - `field_source_map`, `user_edited_fields`,
        `metadata_source_pref` → winner's preserved (per-book user
        intent stays anchored to the surviving row)
      - `first_seen_at`, `created_at` → MIN(both)  — oldest preserved
      - `mam_last_scanned_at` → MAX(both)  — most recent scan
        timestamp survives
      - `title`, `author_id` → winner's (the merge is asserting "this
        is the same book", so the winner's identity is canonical)
    """
    if winner_id == loser_id:
        raise MergeError(f"refusing to merge a row into itself (id={winner_id})")

    winner = await _fetch_book(discovery_db, winner_id)
    if winner is None:
        raise MergeError(f"winner book id={winner_id} not found")
    loser = await _fetch_book(discovery_db, loser_id)
    if loser is None:
        raise MergeError(f"loser book id={loser_id} not found")

    # Both rows being calibre+owned means the user has duplicate
    # Calibre entries (e.g. one calibre_id per metadata edit pass)
    # and Seshat shouldn't auto-pick a winner — the right fix is in
    # Calibre. Refuse and let the caller surface a clear error.
    if (winner["source"] == "calibre" and bool(winner["owned"])
            and loser["source"] == "calibre" and bool(loser["owned"])):
        raise MergeError(
            f"both rows ({winner_id}, {loser_id}) are owned Calibre rows — "
            "remove one from Calibre and re-sync rather than merging in "
            "Seshat",
        )

    resolved = _resolve_fields(winner, loser)

    # Build the UPDATE statement dynamically from the resolved dict.
    # Skip 'id' — that's the WHERE clause, never updated.
    update_cols = [k for k in resolved.keys() if k != "id"]
    set_clause = ", ".join(f"{c} = ?" for c in update_cols)
    update_values = [resolved[c] for c in update_cols] + [winner_id]

    # Snapshot the loser row (full state) into the audit row so a
    # later forensic / rollback step has everything it needs.
    snapshot_json = json.dumps(dict(loser), default=str, sort_keys=True)

    # Redirect any book_grab_links that pointed at the loser to the
    # winner. UNIQUE(library_slug, book_id) means if the winner
    # already has a link the loser's link must be deleted instead of
    # redirected (else INSERT/UPDATE collision).
    winner_link = await (await pipeline_db.execute(
        "SELECT grab_id FROM book_grab_links "
        "WHERE library_slug = ? AND book_id = ?",
        (library_slug, winner_id),
    )).fetchone()
    if winner_link is None:
        await pipeline_db.execute(
            "UPDATE book_grab_links SET book_id = ? "
            "WHERE library_slug = ? AND book_id = ?",
            (winner_id, library_slug, loser_id),
        )
    else:
        # Winner already has a link — drop the loser's. The loser's
        # grab history is preserved on the grabs table itself; only
        # the linkage row goes away.
        await pipeline_db.execute(
            "DELETE FROM book_grab_links "
            "WHERE library_slug = ? AND book_id = ?",
            (library_slug, loser_id),
        )
    await pipeline_db.commit()

    # Apply the winner update and delete the loser. Cascade FKs on
    # the per-library DB (book_series_suggestions, metadata_review_queue,
    # books_calibre_snapshot, books_abs_snapshot) handle their own
    # rows on DELETE.
    await discovery_db.execute(
        f"UPDATE books SET {set_clause} WHERE id = ?",
        update_values,
    )
    await discovery_db.execute(
        "DELETE FROM books WHERE id = ?", (loser_id,),
    )
    await discovery_db.execute(
        "INSERT INTO book_merges "
        "(winner_id, loser_id, loser_snapshot_json, reason) "
        "VALUES (?, ?, ?, ?)",
        (winner_id, loser_id, snapshot_json, reason),
    )
    await discovery_db.commit()

    _log.info(
        "merge_books: winner=%d loser=%d reason=%s slug=%s",
        winner_id, loser_id, reason, library_slug,
    )

    merged = await _fetch_book(discovery_db, winner_id)
    return dict(merged) if merged else {}


async def _fetch_book(db: aiosqlite.Connection, book_id: int):
    return await (await db.execute(
        "SELECT * FROM books WHERE id = ?", (book_id,),
    )).fetchone()


def _resolve_fields(winner, loser) -> dict[str, Any]:
    """Compute the merged column values per the rules in `merge_books`."""
    w = dict(winner)
    l = dict(loser)
    out: dict[str, Any] = {}

    # Title + author_id stay anchored to the winner (the merge is
    # the user's assertion that the loser IS the winner).
    out["title"] = w["title"]
    out["author_id"] = w["author_id"]

    # Boolean / counter aggregates.
    out["owned"] = 1 if (w.get("owned") or l.get("owned")) else 0
    out["hidden"] = min(int(w.get("hidden") or 0), int(l.get("hidden") or 0))
    out["is_new"] = min(int(w.get("is_new") or 0), int(l.get("is_new") or 0))
    out["is_unreleased"] = min(
        int(w.get("is_unreleased") or 0), int(l.get("is_unreleased") or 0),
    )
    out["is_omnibus"] = max(
        int(w.get("is_omnibus") or 0), int(l.get("is_omnibus") or 0),
    )
    out["mam_has_multiple"] = max(
        int(w.get("mam_has_multiple") or 0),
        int(l.get("mam_has_multiple") or 0),
    )
    out["mam_my_snatched"] = max(
        int(w.get("mam_my_snatched") or 0),
        int(l.get("mam_my_snatched") or 0),
    )
    out["mam_is_bundle"] = max(
        int(w.get("mam_is_bundle") or 0), int(l.get("mam_is_bundle") or 0),
    )
    out["abridged"] = max(
        int(w.get("abridged") or 0), int(l.get("abridged") or 0),
    )

    # Source: 'calibre' wins over discovery sources. Beyond that
    # we preserve the winner's value.
    if w.get("source") == "calibre" or l.get("source") == "calibre":
        out["source"] = "calibre"
    else:
        out["source"] = w.get("source") or l.get("source") or "calibre"

    out["calibre_id"] = _coalesce(w.get("calibre_id"), l.get("calibre_id"))

    # Identity + metadata: coalesce(winner, loser).
    for f in _IDENTITY_FIELDS:
        out[f] = _coalesce(w.get(f), l.get(f))
    for f in _METADATA_FIELDS:
        out[f] = _coalesce(w.get(f), l.get(f))

    # Per-book user intent stays anchored to the winner.
    out["metadata_source_pref"] = (
        w.get("metadata_source_pref") or "seshat"
    )
    out["field_source_map"] = w.get("field_source_map")
    out["user_edited_fields"] = w.get("user_edited_fields") or "[]"

    # Timestamps.
    fs_w = _as_float(w.get("first_seen_at"))
    fs_l = _as_float(l.get("first_seen_at"))
    out["first_seen_at"] = min(fs_w, fs_l) if fs_w and fs_l else (fs_w or fs_l)
    cr_w = _as_float(w.get("created_at"))
    cr_l = _as_float(l.get("created_at"))
    out["created_at"] = min(cr_w, cr_l) if cr_w and cr_l else (cr_w or cr_l)
    ls_w = _as_float(w.get("mam_last_scanned_at"))
    ls_l = _as_float(l.get("mam_last_scanned_at"))
    out["mam_last_scanned_at"] = (
        max(ls_w, ls_l) if ls_w and ls_l else (ls_w or ls_l)
    )

    return out


def _coalesce(*values):
    """Return the first value that is non-None and non-empty-string."""
    for v in values:
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        return v
    return None


def _as_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── Winner-selection helpers (used by the manual-merge endpoint) ───


def pick_winner_id(book_a: dict, book_b: dict) -> int:
    """Pick the winner from two row dicts using a deterministic policy.

    Policy (highest score wins; tiebreak by lowest id so the
    surviving row id is stable across re-runs):

      4  — owned and source='calibre' (the canonical "this is in the
           library on disk" state)
      3  — source='calibre' but not owned (rare; transitional)
      2  — owned but not from calibre (the "safety-net flipped"
           goodreads rows in Mark's library — these are the loser
           when paired with a fresh calibre row)
      1  — not owned, discovery source

    Both inputs must be from the same library — the caller validates
    that before calling.
    """

    def score(b: dict) -> int:
        is_cal = (b.get("source") == "calibre")
        is_owned = bool(b.get("owned"))
        if is_cal and is_owned:
            return 4
        if is_cal:
            return 3
        if is_owned:
            return 2
        return 1

    sa, sb = score(book_a), score(book_b)
    if sa > sb:
        return int(book_a["id"])
    if sb > sa:
        return int(book_b["id"])
    return min(int(book_a["id"]), int(book_b["id"]))
