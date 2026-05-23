"""
v2.3.7 — acquisition link-back.

When Seshat downloads a book through the IRC pipeline (tentative or
auto-approve), the originating MAM torrent_id is recorded in the
global `grabs` table. Once the file lands in Calibre/ABS and the
discovery sync creates a per-library `books` row, that row arrives
with `mam_status=NULL` — the discovery sync has no awareness of the
grabs table, so the link is broken.

Without this module, the next MAM scan tick runs `check_book` (a
fuzzy title+author search) on the new row. For IRC-acquired
audiobooks specifically, the cleaned ABS title diverges from the
MAM torrent name often enough that `check_book` mis-grades many of
them as `not_found` or low-confidence `possible` — even though we
acquired the book from MAM with the exact torrent_id in hand.

`link_new_book` looks up recent unlinked grabs in the global app DB,
matches by normalized title + author, and writes the known
`mam_url` / `mam_status='found'` / `mam_torrent_id` directly to the
discovery row. Conservative match rules (substring on normalized
fields + audiobook/ebook category gate) — false positives are bad
(wrong MAM URL on the wrong book), false negatives just fall back
to the legacy MAM-scan path.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from app.database import get_db as get_app_db

logger = logging.getLogger("seshat.discovery.acquisition_linkback")


# How far back to look for an unlinked grab. 30 days covers slow ABS
# scan delays + manual review-queue holds + user-approval pauses.
_LOOKBACK_DAYS = 30

# Match score thresholds. The link only fires when both author and
# title are confident matches — neither dimension on its own is
# enough because torrent_name often contains the series name without
# the book title (e.g. "Wheel of Time Box Set #1-3 [m4b]").
_RE_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(s: str) -> str:
    """Lowercase + strip non-alphanumerics + collapse whitespace.

    Works on both clean book titles ("The Eye of the World") and raw
    torrent names ("Robert Jordan - The Eye of the World [m4b]") so
    a substring check after normalizing tells us whether the book's
    title appears in the torrent name.
    """
    if not s:
        return ""
    return _RE_NON_ALNUM.sub(" ", s.lower()).strip()


def _title_token_overlap(title: str, torrent_name: str) -> float:
    """Fraction of book-title tokens (>=3 chars) present in the
    normalized torrent name. Returns 0..1.

    Token overlap rather than substring because torrent names commonly
    interleave the title with the author / series ("Author - Title -
    Series #N [format]"). Stop-word filtering by length avoids
    "the"/"a"/"of" inflating the score.
    """
    nt = _normalize(title)
    nn = _normalize(torrent_name)
    if not nt or not nn:
        return 0.0
    title_tokens = [t for t in nt.split() if len(t) >= 3]
    if not title_tokens:
        return 0.0
    name_tokens = set(nn.split())
    hits = sum(1 for t in title_tokens if t in name_tokens)
    return hits / len(title_tokens)


def _author_appears_in(author_name: str, torrent_name: str, author_blob: str) -> bool:
    """True if the book's author surname appears in the torrent_name
    OR matches the grab's stored author_blob.

    `author_blob` is what MAM put in the announce — usually a clean
    "Last, First" or "First Last" string. Surname check on the
    torrent_name covers the common "Author - Title" naming pattern
    even when author_blob diverges (pseudonyms, author re-releases).
    """
    if not author_name:
        return False
    norm_author = _normalize(author_name)
    if not norm_author:
        return False
    # Surname is the last whitespace token of the normalized form;
    # falls back to the full normalized string for single-word names.
    parts = norm_author.split()
    surname = parts[-1] if parts else norm_author
    if len(surname) < 3:
        return False
    norm_name = _normalize(torrent_name)
    if surname in norm_name.split():
        return True
    norm_blob = _normalize(author_blob or "")
    if surname in norm_blob.split():
        return True
    return False


# Title-overlap threshold: at least 60% of >=3-char title tokens must
# appear in the torrent_name. Tuned to accept "Free Companions" vs
# "Snekguy - Free Companions [m4b]" (2/2 = 1.0) but reject "Free
# Companions" vs "Wheel of Time #4 [m4b]" (0/2 = 0.0).
_MIN_TITLE_OVERLAP = 0.6


async def link_new_book(
    library_db,
    library_slug: str,
    book_id: int,
    title: str,
    author_name: str,
    *,
    is_audiobook: bool,
) -> bool:
    """Try to link a freshly-inserted book to a recent unlinked grab.

    Returns True if a link was made (and `library_db` was updated to
    set mam_url/mam_status='found'/mam_torrent_id). The caller owns
    `library_db`'s commit lifecycle — we execute UPDATE statements
    but don't commit. Cross-DB write to the global app DB is
    committed inside this function (separate connection).

    No-ops on:
      - This book already has a non-NULL mam_status (don't stomp
        existing scan results / user edits).
      - This book is already linked (book_grab_links row exists).
      - No unlinked grab matches confidently.
      - Multiple grabs match equally well (ambiguous — defer to MAM
        scan rather than guess).
    """
    # Guard: if the row already has any mam_status, leave it alone.
    # Manual edits and prior scans take precedence over auto-link.
    row = await (await library_db.execute(
        "SELECT mam_status FROM books WHERE id=?", (book_id,)
    )).fetchone()
    if row is None:
        return False
    if row["mam_status"]:
        return False

    app_db = await get_app_db()
    try:
        # Already linked? (book_grab_links has a row for this book)
        existing = await (await app_db.execute(
            "SELECT grab_id FROM book_grab_links "
            "WHERE library_slug=? AND book_id=?",
            (library_slug, book_id),
        )).fetchone()
        if existing:
            return False

        # Candidate grabs: completed (state='complete' or anything
        # that indicates the file was delivered), unlinked, recent,
        # matching content type. We don't filter on `state` strictly
        # because state values evolved over time — `grabbed_at` recency
        # is the more robust constraint.
        cat_filter = (
            "LOWER(g.category) LIKE 'audiobook%'"
            if is_audiobook
            else "LOWER(g.category) NOT LIKE 'audiobook%'"
        )
        rows = await (await app_db.execute(
            f"""
            SELECT g.id, g.mam_torrent_id, g.torrent_name, g.author_blob,
                   g.grabbed_at
            FROM grabs g
            LEFT JOIN book_grab_links bgl ON bgl.grab_id = g.id
            WHERE bgl.grab_id IS NULL
              AND {cat_filter}
              AND g.grabbed_at >= datetime('now', '-{_LOOKBACK_DAYS} days')
            ORDER BY g.grabbed_at DESC
            """
        )).fetchall()

        # Score each candidate; the one with the highest title overlap
        # wins, provided its score clears the threshold AND the author
        # appears. Tied top scores → ambiguous → bail.
        scored: list[tuple[float, int, str]] = []  # (score, grab_id, mam_torrent_id)
        for r in rows:
            grab_id = r["id"]
            torrent_id = r["mam_torrent_id"]
            torrent_name = r["torrent_name"] or ""
            author_blob = r["author_blob"] or ""
            if not _author_appears_in(author_name, torrent_name, author_blob):
                continue
            score = _title_token_overlap(title, torrent_name)
            if score >= _MIN_TITLE_OVERLAP:
                scored.append((score, grab_id, torrent_id))

        if not scored:
            return False

        # Sort descending; check for a tie at the top.
        scored.sort(key=lambda t: t[0], reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            logger.info(
                "acquisition link-back: ambiguous match for "
                "book_id=%d (%r by %r) — %d grabs tied at score %.2f, "
                "skipping auto-link",
                book_id, title[:60], author_name[:40], len(scored), scored[0][0],
            )
            return False

        best_score, best_grab_id, best_torrent_id = scored[0]
        if not best_torrent_id:
            return False
        mam_url = f"https://www.myanonamouse.net/t/{best_torrent_id}"

        # Write the link row first so a duplicate-link race fails
        # noisily before we touch the books row. UNIQUE constraint on
        # (library_slug, book_id) catches concurrent ABS+Calibre sync
        # attempts on the same book.
        try:
            await app_db.execute(
                "INSERT INTO book_grab_links (grab_id, library_slug, book_id) "
                "VALUES (?, ?, ?)",
                (best_grab_id, library_slug, book_id),
            )
            await app_db.commit()
        except Exception as e:
            logger.debug(
                "acquisition link-back: link insert failed for "
                "book_id=%d (likely concurrent claim): %s",
                book_id, e,
            )
            return False

        # Now update the books row in the per-library DB. Caller
        # commits.
        await library_db.execute(
            "UPDATE books SET mam_url=?, mam_status='found', "
            "mam_torrent_id=? WHERE id=?",
            (mam_url, best_torrent_id, book_id),
        )
        logger.info(
            "acquisition link-back: linked book_id=%d (%r) to grab_id=%d "
            "(mam_torrent_id=%s, score=%.2f)",
            book_id, title[:60], best_grab_id, best_torrent_id, best_score,
        )

        # v2.25.0 — quality-metadata extraction for the newly-linked
        # torrent. Fail-soft: extraction errors must not roll back the
        # link-back. Skips automatically if this torrent has already
        # been extracted (bundle dispatch links N books to one torrent).
        try:
            from app.quality.pipeline import extract_for_torrent
            await extract_for_torrent(app_db, best_torrent_id)
        except Exception as e:
            logger.warning(
                "acquisition link-back: quality extraction failed for "
                "torrent %s (link-back itself succeeded): %s",
                best_torrent_id, e,
            )

        return True
    finally:
        await app_db.close()


__all__ = ["link_new_book"]
