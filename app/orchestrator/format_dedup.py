"""
Format-priority dedup logic (v2.9.0).

Decides whether an announce that passed the filter gate should:

  * be grabbed immediately ("allow") — possibly preempting an in-flight
    lower-priority hold of the same book;
  * be parked in `pending_holds` for `format_dedup_hold_seconds`
    ("hold") so a higher-priority sibling has a chance to arrive
    before we commit to grabbing a lower-priority format;
  * be skipped because a higher-priority sibling is already in flight
    or because the book is already Owned in some format ("skip").

The four user-facing scenarios from the v2.9.0 spec map onto this gate:

  Scenario 1 — highest-priority arrives, nothing else races          → allow
  Scenario 2 — higher-priority already in flight, lower arrives      → skip
  Scenario 3 — disabled-only arrives, nothing else races             → hold,
               then grab when the timer fires
  Scenario 3.5 — higher-priority arrives later (even after the lower
               was already Owned)                                    → allow
               (the user ends up with both formats in Calibre,
               which is fine — Calibre stores N formats per book)

Plus the v2.9.0 design's "Delves preempt" case:

  Disabled lower-priority is currently held; a higher-priority enabled
  format arrives during the hold window → allow the higher AND drop
  the lower's hold (preempt_hold_ids).

This module is intentionally pure — `evaluate_format_dedup` takes
the announce, the user's `format_priority` setting, the hold window,
and a pre-fetched list of `SiblingMatch` objects. The caller does
the actual DB writes (insert grab / insert hold / drop preempted
holds) inside one BEGIN IMMEDIATE so the four-way race window
between two concurrent announces never produces double-grabs.

The companion async helper `lookup_dedup_siblings` does the impure
work — querying `grabs`, `pending_holds`, and the per-library `books`
tables — so production callers get a one-call data fetch and tests
can stub by passing siblings directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

from app import state
from app.database import get_db
from app.discovery.database import get_db as get_library_db
from app.filter.gate import Announce
from app.quality.extract import QualitySnapshot
from app.quality.scoring import (
    QualityProfile,
    format_is_enabled,
    format_rank,
    resolve_profile_from_settings,
    score_quality,
)
from app.works.normalize import match_key

_log = logging.getLogger(__name__)


# Category prefix → media-type slug. The prefix is the lowercased text
# before " - " in the MAM category (e.g., "Ebooks - Fantasy" → "ebooks").
# Only media types with a Format Priority list are dedup-aware; others
# (comics, etc.) fall through to allow regardless of filetype.
_CATEGORY_PREFIX_TO_MEDIA_TYPE: dict[str, str] = {
    "ebooks": "ebook",
    "audiobooks": "audiobook",
}

# Grab states that count as "in flight" for dedup purposes. Anything
# else (complete, any failed_* variant) means the grab is no longer
# competing with future siblings.
#
# Kept as a literal set rather than importing from app/storage/grabs.py
# because that module's constants are about the state-machine itself;
# the dedup gate's "still racing" predicate is a separate concern that
# happens to align today. If the state set drifts, we want the dedup
# gate to keep its own definition rather than silently shift behavior.
_IN_FLIGHT_STATES: frozenset[str] = frozenset({
    "pending_queue",
    "fetched",
    "submitted",
    "duplicate_in_qbit",
    "downloading",
    "downloaded",
    "processing",
})


# ─── Public data types ─────────────────────────────────────────


@dataclass(frozen=True)
class SiblingMatch:
    """One sibling of an announce, found via dedup-key match.

    `where` distinguishes the lifecycle stage of the sibling:
      * "in_flight" — `grabs` row, still racing through the pipeline
      * "held"      — `pending_holds` row, timer hasn't fired yet
      * "owned"     — `books` row in some per-library DB with owned=1

    `book_format` is the lowercased filetype (epub, azw3, m4b, mp3, ...)
    so the gate can compare priorities. For owned books — where Calibre
    stores a CSV like "EPUB,AZW3" — `book_format` is the first listed
    format. Owned siblings block lower-priority disabled announces
    regardless of which specific format is owned, so picking the first
    is good enough; the priority check still fires correctly.

    `quality_snapshot` (v2.26.0, optional) carries the sibling's
    quality-metadata row when available. Phase 5 active-replacement
    callers populate this for owned/in-flight siblings so the gate can
    score-compare on numeric axes. Default None preserves v2.9.0
    format-only dedup behavior for announce-time callers.
    """
    where: Literal["in_flight", "held", "owned"]
    book_format: str
    grab_id: Optional[int] = None
    hold_id: Optional[int] = None
    library_slug: Optional[str] = None
    quality_snapshot: Optional[QualitySnapshot] = None


@dataclass(frozen=True)
class FormatDedupDecision:
    """The result of `evaluate_format_dedup`.

    `action` is one of "allow", "skip", "hold". `reason` is a machine-
    stable string the caller writes into `announces.decision_reason`.

    `preempt_hold_ids` is the set of `pending_holds.id` rows the caller
    must mark as state='dropped' as part of acting on this decision.
    Populated on "allow" when a higher-priority arrival is preempting a
    held lower-priority sibling, and on "hold" when a higher-priority
    disabled is replacing a lower-priority hold (we always keep at most
    one active hold per dedup_key, always for the highest priority seen).

    `hold_seconds` is set only when action == "hold" — the caller adds
    this to "now" to compute `pending_holds.release_at`.
    """
    action: Literal["allow", "skip", "hold"]
    reason: str
    dedup_key: str
    media_type: Optional[str]
    book_format: str
    hold_seconds: Optional[int] = None
    preempt_hold_ids: tuple[int, ...] = ()


# ─── Pure helpers ─────────────────────────────────────────────


def media_type_from_category(category: str) -> Optional[str]:
    """Map an MAM category like 'Ebooks - Fantasy' to a media-type slug.

    Returns None for unknown prefixes (comics, etc.). Those announces
    fall through the dedup gate unchanged — v2.9.0 only ships dedup
    rules for ebook + audiobook.
    """
    if not category:
        return None
    prefix = category.split(" - ", 1)[0].strip().lower()
    return _CATEGORY_PREFIX_TO_MEDIA_TYPE.get(prefix)


def normalize_dedup_key(title: str, author_blob: str) -> str:
    """Compute the dedup match key for an announce or owned book.

    Built on top of `app.works.normalize.match_key`, which is the same
    normalizer used by the cross-library works matcher. Reusing it
    means an announce's key lines up exactly with an owned book's key
    when computed against the per-library `books` table — including
    Unicode-folding, leading-article stripping, and format-paren
    stripping (so "The Delves (Unabridged)" and "The Delves" collapse).

    The author input is the comma-separated `author_blob` from the
    announce; only the first author is used (matches the spec — same
    book in different formats has the same primary author, and
    co-authors can drift across formats e.g. when an audiobook lists
    the narrator).

    Returns "" if either half is empty; the caller treats that as
    "no key, can't dedup" and short-circuits to allow.
    """
    if not title or not author_blob:
        return ""
    first_author = author_blob.split(",", 1)[0].strip()
    return match_key(first_author, title)


# ─── The gate ─────────────────────────────────────────────────


def evaluate_format_dedup(
    *,
    announce: Announce,
    format_priority: dict,
    hold_seconds: int,
    siblings: list[SiblingMatch],
    quality_axes: Optional[dict] = None,
    candidate_snapshot: Optional[QualitySnapshot] = None,
) -> FormatDedupDecision:
    """Pure decision function for format-priority dedup.

    The caller has already established that the filter gate said
    "allow"; this is the second gate that applies the dedup rules.
    No I/O — siblings is pre-fetched by `lookup_dedup_siblings` or
    stubbed by tests.

    v2.26.0: scoring is delegated to app/quality/scoring.py via a
    composed QualityProfile. `quality_axes` carries the numeric-axis
    tiebreakers (audiobook bitrate/channels); when absent or empty for
    the media type, scoring degrades cleanly to format-only — identical
    to v2.9.0 dedup behavior. `candidate_snapshot` and the per-sibling
    `quality_snapshot` field on SiblingMatch let Phase 5 active-
    replacement callers pass real metadata for multi-axis comparison.
    """
    media_type = media_type_from_category(announce.category)
    fmt = (announce.filetype or "").strip().lower()
    title = announce.torrent_name or announce.title or ""
    dedup_key = normalize_dedup_key(title, announce.author_blob or "")

    base = dict(dedup_key=dedup_key, media_type=media_type, book_format=fmt)

    # ── Fall-throughs: any of these mean "we can't apply the rule". ──
    if not media_type or media_type not in format_priority:
        return FormatDedupDecision(
            action="allow", reason="format_dedup_no_media_type_rule", **base,
        )
    if not fmt:
        return FormatDedupDecision(
            action="allow", reason="format_dedup_no_filetype", **base,
        )
    if not dedup_key:
        return FormatDedupDecision(
            action="allow", reason="format_dedup_no_match_key", **base,
        )

    profile = resolve_profile_from_settings(
        media_type,
        {
            "format_priority": format_priority,
            "quality_axes": quality_axes or {},
        },
    )
    if profile is None or not profile.format_tiers:
        return FormatDedupDecision(
            action="allow", reason="format_dedup_empty_priority_list", **base,
        )

    # `format_rank` returns len(format_tiers) for unknown formats; treat
    # that as "not in the list" the same way the old _format_index == -1
    # branch did. Could be a new MAM filetype, could be a one-off
    # (fb2, djvu) — don't punish the unfamiliar.
    fmt_idx = format_rank(profile, fmt)
    if fmt_idx >= len(profile.format_tiers):
        return FormatDedupDecision(
            action="allow", reason="format_dedup_unknown_fmt", **base,
        )

    cand_score = score_quality(
        profile=profile, fmt=fmt, snapshot=candidate_snapshot,
    )

    def _sib_score(s: SiblingMatch) -> tuple[int, ...]:
        return score_quality(
            profile=profile, fmt=s.book_format, snapshot=s.quality_snapshot,
        )

    def _sib_known_format(s: SiblingMatch) -> bool:
        # Mirror the old _format_index >= 0 check: only siblings whose
        # format is actually in the profile participate in comparisons.
        return format_rank(profile, s.book_format) < len(profile.format_tiers)

    # ── Enabled branch ───────────────────────────────────────
    # An enabled format always grabs (Scenarios 1 + 3.5). The only
    # side-effect is to drop any pending hold for a LOWER-quality
    # sibling of the same book (the Delves preempt case): the user
    # said they want EPUB > AZW3, and now EPUB is arriving while
    # AZW3 sits in a 10-min hold, so the AZW3 hold should die.
    if format_is_enabled(profile, fmt):
        preempt = tuple(
            s.hold_id for s in siblings
            if s.where == "held" and s.hold_id is not None
            and _sib_known_format(s) and cand_score < _sib_score(s)
        )
        return FormatDedupDecision(
            action="allow",
            reason="format_dedup_enabled_grab",
            preempt_hold_ids=preempt,
            **base,
        )

    # ── Disabled branch ──────────────────────────────────────
    # Rule (a): any owned sibling of any format blocks a disabled grab.
    # "We already have the book" wins, regardless of which format.
    if any(s.where == "owned" for s in siblings):
        return FormatDedupDecision(
            action="skip", reason="format_dedup_owned_sibling", **base,
        )

    # Rule (b): a higher-quality sibling currently in flight (grab) or
    # held blocks us — let the higher one resolve first. This is the
    # Duchy case: EPUB grabs immediately, AZW3 arrives 29s later and
    # sees EPUB sitting in `grabs` with state != complete.
    higher_siblings = [
        s for s in siblings
        if _sib_known_format(s) and _sib_score(s) < cand_score
    ]
    if any(s.where in ("in_flight", "held") for s in higher_siblings):
        return FormatDedupDecision(
            action="skip",
            reason="format_dedup_higher_priority_inflight",
            **base,
        )

    # No blocking sibling. Hold for `hold_seconds` and let the scheduler
    # tick re-evaluate at release_at. While we're here, if any LOWER-
    # quality hold exists for the same dedup_key, drop it — we're
    # better among disabled options, so we become the active hold.
    # (Invariant: at most one pending hold per dedup_key.)
    preempt = tuple(
        s.hold_id for s in siblings
        if s.where == "held" and s.hold_id is not None
        and _sib_known_format(s) and cand_score < _sib_score(s)
    )
    return FormatDedupDecision(
        action="hold",
        reason="format_dedup_hold",
        hold_seconds=hold_seconds,
        preempt_hold_ids=preempt,
        **base,
    )


# ─── Sibling lookup (impure, the data half) ───────────────────


async def lookup_dedup_siblings(
    *,
    dedup_key: str,
    media_type: str,
    libraries: Optional[list[dict]] = None,
) -> list[SiblingMatch]:
    """Fetch every currently-known sibling of the given dedup key.

    Three sources:
      1. `grabs` — rows whose state is still in `_IN_FLIGHT_STATES`.
      2. `pending_holds` — rows whose state is 'pending'.
      3. Per-library `books` tables — owned=1, normalized key matches.

    Owned-side scope is filtered by media type: an ebook announce only
    cross-checks ebook libraries, an audiobook announce only cross-
    checks audiobook libraries. Cross-format ownership ("we have this
    book as an audiobook, an ebook announce came in") is intentionally
    NOT a block — the user wants both formats; the cross-library work-
    linker stitches them together post-acquisition.

    `libraries` defaults to `state._discovered_libraries`; tests can
    pass a tighter subset.
    """
    if not dedup_key:
        return []

    matches: list[SiblingMatch] = []

    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, book_format FROM grabs "
            "WHERE dedup_key = ? AND state IN (%s)" % (
                ", ".join("?" * len(_IN_FLIGHT_STATES))
            ),
            (dedup_key, *_IN_FLIGHT_STATES),
        )
        for row in await cur.fetchall():
            matches.append(SiblingMatch(
                where="in_flight",
                book_format=(row["book_format"] or "").lower(),
                grab_id=row["id"],
            ))

        cur = await db.execute(
            "SELECT id, book_format FROM pending_holds "
            "WHERE dedup_key = ? AND state = 'pending'",
            (dedup_key,),
        )
        for row in await cur.fetchall():
            matches.append(SiblingMatch(
                where="held",
                book_format=(row["book_format"] or "").lower(),
                hold_id=row["id"],
            ))
    finally:
        await db.close()

    libs = libraries if libraries is not None else list(state._discovered_libraries)
    for lib in libs:
        slug = lib.get("slug")
        ctype = lib.get("content_type") or "ebook"
        if not slug or ctype != media_type:
            continue
        try:
            lib_db = await get_library_db(slug)
        except Exception:
            _log.debug("dedup owned-scan: skipping library %s (open failed)", slug)
            continue
        try:
            cur = await lib_db.execute(
                "SELECT b.id AS book_id, b.title, b.formats, "
                "       a.name AS author_name "
                "FROM books b "
                "JOIN book_authors bpa ON bpa.book_id = b.id AND bpa.position = 0 "
                "JOIN authors a ON a.id = bpa.author_id "
                "WHERE b.hidden = 0 AND b.owned = 1"
            )
            rows = await cur.fetchall()
        finally:
            await lib_db.close()

        for r in rows:
            row_key = normalize_dedup_key(
                r["title"] or "", r["author_name"] or "",
            )
            if row_key and row_key == dedup_key:
                formats_csv = (r["formats"] or "").lower()
                first_fmt = (formats_csv.split(",")[0] or "").strip()
                matches.append(SiblingMatch(
                    where="owned",
                    book_format=first_fmt,
                    library_slug=slug,
                ))

    return matches
