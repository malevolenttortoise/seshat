"""
v2.17.7 — claim-for-owned at announce time.

When a freshly-announced MAM torrent matches a book the user already
owns AND that owned row carries no confirmed MAM URL
(`mam_status != 'found'`), write the MAM URL/torrent_id/status onto
the owned row in place and skip the grab. The library system has
gained the linkage it was missing without paying buffer ratio for a
redundant download.

The motivating case: user owned "Amber's Hollow: Home of the
Homeless" by St. Arkham in Calibre well before the book existed on
MAM. Owned row sat at `mam_status='not_found'` because every prior
MAM scan came up empty. Last night the book got uploaded; the
autograbber happily grabbed a duplicate copy and parked it in the
review queue. With this hook installed, the new announce would
recognize the owned row and claim the torrent_id for it directly
instead.

Match rules — conservative on purpose, bail rather than guess:
  - Library scope: only libraries whose `content_type` matches the
    announce's media-type (ebook announces → ebook libraries only;
    audiobook ↔ audiobook). Stops an audiobook announce from being
    claimed for an ebook-only owned row.
  - Author: contributor-aware (v3.0.0 / ADR-0013). The announce's
    PRIMARY author (`normalize_author_name(author_blob primary)`)
    must be ANY contributor of the owned book (`book_authors`, any
    position) — not just the owned row's primary. Closes the
    co-author-ordering gap (announce primary differs from the owned
    stored primary). Same normalizer the rest of the codebase trusts
    ("St Arkham" vs "St. Arkham", etc).
  - Title: canonical `match_key` (apostrophe/diacritic/article
    folding) from `app.works.normalize` against
    `normalize_dedup_key(announce.title or torrent_name,
    author_blob)`. Same key the v2.9.0 format-dedup pass uses, so
    matches stay consistent across the system.
  - Owned row gate: `owned=1 AND hidden=0 AND
    (mam_status IS NULL OR mam_status != 'found')`. Confirmed
    linkages are never overwritten.
  - Ambiguity gate: 2+ owned rows match → bail. The user has a
    duplicate library state we shouldn't auto-resolve.

On a positive single match: UPDATE the owned row's
`mam_url`/`mam_torrent_id`/`mam_status='found'`/`mam_category`/
`source_url`/`mam_last_scanned_at`. Caller emits an event and
returns a skip from dispatch so no grab row is created.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger("seshat.orchestrator.owned_announce_claim")


@dataclass
class OwnedMatch:
    """A single owned book that could receive a MAM-URL claim."""
    library_slug: str
    book_id: int
    title: str
    mam_status: str
    calibre_id: Optional[int] = None


@dataclass
class OwnedClaimResult:
    """Outcome of a claim-for-owned attempt.

    `claimed=True` ⇒ owned row was updated, caller should return a
    skip from dispatch. Any other case ⇒ fall through to the normal
    grab path. `reason` is a short tag for logging / event payload.
    """
    claimed: bool
    library_slug: str = ""
    book_id: int = 0
    book_title: str = ""
    reason: str = ""


async def find_owned_matches(
    *, title: str, author_blob: str, category: str, libraries=None,
) -> list[OwnedMatch]:
    """Return every owned book that could receive a MAM-URL claim.

    Used by both the announce-time claim hook (which requires
    exactly-one match) and the review-queue duplicate banner (which
    surfaces every candidate to the user).

    Returns [] when:
      - title/author missing
      - the category resolves to an unsupported media type
      - no library matches the media type
      - no owned row matches the normalized title+author key

    Library scope is filtered by `content_type` — ebook announces
    only check ebook libraries, audiobook ↔ audiobook — so we never
    cross-link an audiobook torrent onto an ebook-only row.
    """
    from app import state
    from app.discovery.database import get_db as get_library_db
    from app.metadata.author_names import normalize_author_name
    from app.orchestrator.format_dedup import (
        media_type_from_category, normalize_dedup_key,
    )

    if not title or not author_blob:
        return []

    media_type = media_type_from_category(category or "")
    if media_type not in ("ebook", "audiobook"):
        return []

    dedup_key = normalize_dedup_key(title, author_blob)
    if not dedup_key:
        return []

    primary_author = author_blob.split(",", 1)[0].strip()
    norm_author = normalize_author_name(primary_author)
    if not norm_author:
        return []

    libs = (
        libraries if libraries is not None
        else list(state._discovered_libraries or [])
    )
    if not libs:
        return []

    matches: list[OwnedMatch] = []
    for lib in libs:
        slug = (lib or {}).get("slug")
        ctype = (lib or {}).get("content_type") or "ebook"
        if not slug or ctype != media_type:
            continue
        try:
            db = await get_library_db(slug)
        except Exception:
            _log.debug("owned-match: open %s failed; skipping", slug)
            continue
        try:
            # v3.0.0 Phase 10 (ADR-0013): contributor-aware — match owned
            # books where the announce's PRIMARY author is ANY contributor
            # (book_authors, any position), not just the owned primary. The
            # title gate below (row_key vs dedup_key, both keyed on the
            # announce primary) keeps it from over-matching.
            cur = await db.execute(
                """
                SELECT b.id, b.title, b.mam_status, b.calibre_id
                FROM books b
                WHERE b.owned = 1
                  AND b.hidden = 0
                  AND (b.mam_status IS NULL OR b.mam_status != 'found')
                  AND b.id IN (
                      SELECT ba.book_id FROM book_authors ba
                      JOIN authors a ON a.id = ba.author_id
                      WHERE a.normalized_name = ?
                  )
                """,
                (norm_author,),
            )
            rows = await cur.fetchall()
        finally:
            await db.close()

        for r in rows:
            # Recompute the owned-side key against the announce PRIMARY (the
            # confirmed-contributor), NOT the owned row's stored primary, so
            # the comparison reduces to a canonical title match once author
            # membership is established (ADR-0013).
            row_key = normalize_dedup_key(r["title"] or "", primary_author)
            if row_key and row_key == dedup_key:
                matches.append(OwnedMatch(
                    library_slug=slug,
                    book_id=int(r["id"]),
                    title=r["title"] or "",
                    mam_status=r["mam_status"] or "",
                    calibre_id=(
                        int(r["calibre_id"])
                        if r["calibre_id"] is not None else None
                    ),
                ))

    return matches


async def write_claim_to_owned(
    *, library_slug: str, book_id: int,
    mam_torrent_id: str, category: str = "",
) -> bool:
    """UPDATE the named owned row with a claimed MAM linkage.

    Returns True on a successful single-row UPDATE; False on any DB
    error or no-match. Caller is responsible for any audit row /
    event emission — this helper just touches the books table.
    """
    from app.discovery.database import get_db as get_library_db

    if not mam_torrent_id or not library_slug or not book_id:
        return False

    mam_url = f"https://www.myanonamouse.net/t/{mam_torrent_id}"
    try:
        db = await get_library_db(library_slug)
    except Exception as e:
        _log.warning(
            "write_claim_to_owned: open %s failed: %s", library_slug, e,
        )
        return False
    try:
        cur = await db.execute(
            """
            UPDATE books SET
                mam_url = ?,
                mam_torrent_id = ?,
                mam_status = 'found',
                mam_category = COALESCE(NULLIF(mam_category, ''), ?),
                source_url = COALESCE(NULLIF(source_url, ''), ?),
                mam_last_scanned_at = ?
            WHERE id = ?
            """,
            (
                mam_url, str(mam_torrent_id), category or "",
                mam_url, time.time(), book_id,
            ),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0
    finally:
        await db.close()


async def try_claim_announce_for_owned(
    *, announce, libraries=None,
) -> OwnedClaimResult:
    """Attempt a claim. See module docstring for match rules."""
    if not announce or not announce.torrent_id:
        return OwnedClaimResult(claimed=False, reason="no_announce")

    title = (announce.title or announce.torrent_name or "").strip()
    author_blob = (announce.author_blob or "").strip()
    if not title or not author_blob:
        return OwnedClaimResult(claimed=False, reason="empty_title_or_author")

    matches = await find_owned_matches(
        title=title,
        author_blob=author_blob,
        category=announce.category or "",
        libraries=libraries,
    )
    if not matches:
        return OwnedClaimResult(claimed=False, reason="no_owned_match")
    if len(matches) > 1:
        _log.info(
            "claim-for-owned: %d owned rows match tid=%s (%r) — bailing "
            "rather than guess which to claim",
            len(matches), announce.torrent_id, title[:60],
        )
        return OwnedClaimResult(
            claimed=False, reason="ambiguous_multi_match",
        )

    m = matches[0]
    ok = await write_claim_to_owned(
        library_slug=m.library_slug,
        book_id=m.book_id,
        mam_torrent_id=str(announce.torrent_id),
        category=announce.category or "",
    )
    if not ok:
        return OwnedClaimResult(
            claimed=False, reason="write_failed",
        )
    _log.info(
        "claim-for-owned: claimed tid=%s for owned book id=%d slug=%s "
        "title=%r (skipping grab)",
        announce.torrent_id, m.book_id, m.library_slug, m.title[:60],
    )
    return OwnedClaimResult(
        claimed=True,
        library_slug=m.library_slug,
        book_id=m.book_id,
        book_title=m.title,
        reason="owned_book_claimed",
    )


__all__ = [
    "OwnedMatch",
    "OwnedClaimResult",
    "find_owned_matches",
    "write_claim_to_owned",
    "try_claim_announce_for_owned",
]
