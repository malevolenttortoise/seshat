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
  - Title: the title identity (`works/title_identity.same_book`,
    ADR-0028) between the announce title and the owned row's title +
    series placement, with the author's labels. Format dedup's owned
    check uses the same lookup (`owned_books_matching`), so the two
    agree.
  - Owned row gate: `owned=1 AND hidden=0`. A row already linked to
    MAM (`mam_status = 'found'`) is never claimed: its linkage is
    never overwritten.
  - Ambiguity gate: 2+ owned rows match → bail. The user has a
    duplicate library state we shouldn't auto-resolve.

On a positive single match on an unlinked row: UPDATE the owned row's
`mam_url`/`mam_torrent_id`/`mam_status='found'`/`mam_category`/
`source_url`/`mam_last_scanned_at`. Caller emits an event and
returns a skip from dispatch so no grab row is created.

Anything else that matched (a linked row, 2+ rows, a failed write) is
returned in `OwnedClaimResult.owned`: for an IRC announce dispatch
skips it as `owned_same_format` (D7, ADR-0004 amended).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger("seshat.orchestrator.owned_announce_claim")


@dataclass
class OwnedMatch:
    """An owned book an announce matched by author + title identity."""
    library_slug: str
    book_id: int
    title: str
    mam_status: str
    calibre_id: Optional[int] = None
    formats: str = ""

    @property
    def linked(self) -> bool:
        """Already carries a confirmed MAM linkage (never overwritten)."""
        return self.mam_status == "found"


@dataclass
class OwnedClaimResult:
    """Outcome of a claim-for-owned attempt.

    `claimed=True` ⇒ owned row was updated, caller should return a
    skip from dispatch. Otherwise `owned` holds the owned rows the
    announce matched but couldn't claim (already linked, 2+ rows, or
    the write failed): an IRC announce is skipped for them
    (`owned_same_format`), a grab the user sent goes ahead. `reason` is
    a short tag for logging / event payload.
    """
    claimed: bool
    library_slug: str = ""
    book_id: int = 0
    book_title: str = ""
    reason: str = ""
    owned: tuple[OwnedMatch, ...] = ()


async def owned_books_matching(
    *, title: str, author_blob: str, media_type: str, libraries=None,
    include_linked: bool = True,
) -> list[OwnedMatch]:
    """Every owned book of this media type the announce is (ADR-0028).

    Author: the announce's PRIMARY author is any contributor of the
    owned book (ADR-0013). Title: `same_book` against the owned row's
    title + series placement, with that author's labels (read from all
    their books in the library, owned or not). Library scope: only
    libraries whose `content_type` is `media_type`, so an audiobook is
    never matched to an ebook row.

    `include_linked=False` leaves out rows already linked to MAM (the
    rows a claim could write to).
    """
    from app import state
    from app.discovery.database import get_db as get_library_db
    from app.metadata.author_names import normalize_author_name
    from app.works.title_identity import TitleRef, author_labels, same_book

    if not title or not author_blob or media_type not in ("ebook", "audiobook"):
        return []
    primary_author = author_blob.split(",", 1)[0].strip()
    norm_author = normalize_author_name(primary_author)
    if not norm_author:
        return []

    libs = (
        libraries if libraries is not None
        else list(state._discovered_libraries or [])
    )
    announce = TitleRef(title)
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
            # Every book the announce primary contributes to (any
            # position): the owned ones are the candidates, all of them
            # give the author's labels.
            cur = await db.execute(
                """
                SELECT b.id, b.title, b.owned, b.hidden, b.mam_status,
                       b.calibre_id, b.formats, b.series_index,
                       s.name AS series_name
                FROM books b
                LEFT JOIN series s ON s.id = b.series_id
                WHERE b.id IN (
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

        labels = author_labels(r["title"] or "" for r in rows)
        for r in rows:
            if not r["owned"] or r["hidden"]:
                continue
            if not include_linked and r["mam_status"] == "found":
                continue
            owned = TitleRef(r["title"] or "", r["series_name"], r["series_index"])
            if not same_book(announce, owned, labels):
                continue
            matches.append(OwnedMatch(
                library_slug=slug,
                book_id=int(r["id"]),
                title=r["title"] or "",
                mam_status=r["mam_status"] or "",
                calibre_id=(
                    int(r["calibre_id"]) if r["calibre_id"] is not None else None
                ),
                formats=r["formats"] or "",
            ))
    return matches


async def find_owned_matches(
    *, title: str, author_blob: str, category: str, libraries=None,
    include_linked: bool = False,
) -> list[OwnedMatch]:
    """Owned books the announce is, by its MAM category's media type.

    Used by the announce-time claim (`include_linked=True`: it needs
    the linked rows to skip the grab) and the review-queue duplicate
    banner (the default: only rows a "Claim torrent for owned" could
    write to).

    Returns [] when title/author is missing, the category isn't an
    ebook / audiobook one, no library has that media type, or nothing
    matches.
    """
    from app.orchestrator.format_dedup import media_type_from_category

    return await owned_books_matching(
        title=title,
        author_blob=author_blob,
        media_type=media_type_from_category(category or "") or "",
        libraries=libraries,
        include_linked=include_linked,
    )


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
        include_linked=True,
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
            claimed=False, reason="ambiguous_multi_match", owned=tuple(matches),
        )

    m = matches[0]
    if m.linked:
        return OwnedClaimResult(
            claimed=False, reason="owned_already_linked", owned=(m,),
        )
    ok = await write_claim_to_owned(
        library_slug=m.library_slug,
        book_id=m.book_id,
        mam_torrent_id=str(announce.torrent_id),
        category=announce.category or "",
    )
    if not ok:
        return OwnedClaimResult(
            claimed=False, reason="write_failed", owned=(m,),
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
    "owned_books_matching",
    "write_claim_to_owned",
    "try_claim_announce_for_owned",
]
