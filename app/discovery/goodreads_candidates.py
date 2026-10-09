"""
Goodreads candidate worker (2026-10 audit wave 4b, S4, ADR-0026).

Goodreads lists books no other source has, mostly small indie catalogues
(it imports Kindle editions from Amazon). Its book pages sit behind AWS WAF,
which blocks a cluster of them from one IP for a few minutes, so scans no
longer fetch them (G-C); this worker checks the books Goodreads lists that
discovery doesn't have yet, slowly, in the background, and creates the ones
that pass.

**Candidates** (G86 / G94). For every author a source scan has covered
(`authors.last_lookup_at` set) whose Goodreads list page is cached, every
list entry gets a row in `metadata_cache_goodreads_candidates`: `known`
(discovery has it: its Goodreads ID, or its title, also after a "Series :"
prefix), `skipped` (audio, a box set, a title filter), `baseline` (an author
listing more than 100 books: not part of the first fill), or `pending`. A
later list refresh adds the new entries as `pending` whatever the author's
size (weekly new IDs, G94).

**Steps** (G95 / G97). A pending candidate gets one autocomplete for its
title, and one more for "title author" when the first finds nothing under
the author's Goodreads ID. A confirming hit (same author ID and the same
book ID or title, G76) is stored and filtered (`goodreads_filters`); a pass
waits for its book page (`awaiting_page`), fetched at the book-page gap
(G89, default 2 min). A page that passes the page filters is `accepted` and
merged into discovery at once (G102) — through `_merge_result`, the scan's
own path — then `created`. A block leaves the candidate where it was; the
per-kind backoff ends by itself.

**Order + durability** (G73 / G75 / G86). State is written as each step
finishes, so a restart, a block or an outage loses nothing. The author the
worker last worked on comes first while it has work, then authors a scan has
just covered, then weekly new IDs (smallest catalogue first), then the
first-fill backlog (smallest catalogue first). Phase 2 (`phase2_step`,
G88) walks every cached list entry's page, only when switched on and nothing
else is waiting.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

from app import state
from app.config import load_settings
from app.discovery import goodreads_filters, goodreads_store, metadata_cache
from app.metadata import goodreads_session, source_gate
from app.metadata.source_config import goodreads_includes_nonfiction

_log = logging.getLogger("seshat.discovery.goodreads_candidates")

_SOURCE = metadata_cache.SOURCE_GOODREADS
_BASE = "https://www.goodreads.com"
_AUTOCOMPLETE = _BASE + "/book/auto_complete?format=json&q="

# G86: the first fill covers authors listing at most this many books.
FIRST_FILL_MAX_LISTED = 100
# A scan within this long before an author is first seeded puts it at the
# front (G86: "newly scanned authors jump the line").
_RECENT_SCAN_S = 7 * 86400.0
# A page that errors (not a block) this many times is given up on.
MAX_PAGE_ERRORS = 5

KNOWN = "known"
SKIPPED = "skipped"
BASELINE = "baseline"
PENDING = "pending"
AWAITING_PAGE = "awaiting_page"
ACCEPTED = "accepted"
REJECTED = "rejected"
CREATED = "created"
UNDECIDED = (PENDING, AWAITING_PAGE, ACCEPTED)

ORIGIN_FIRST_FILL = "first_fill"
ORIGIN_WEEKLY = "weekly"


def _ct() -> str:
    return metadata_cache.candidates_table(_SOURCE)


def _at() -> str:
    return metadata_cache.candidate_authors_table(_SOURCE)


def _is_ebook_library(slug: str) -> bool:
    for lib in state._discovered_libraries:
        if lib.get("slug") == slug:
            return (lib.get("content_type") or "ebook") == "ebook"
    return False


# ─── Known books ─────────────────────────────────────────────


async def _discovery_author(slug: str, gr_author_id: str) -> Optional[dict]:
    """The scanned Seshat author holding `gr_author_id` in `slug` (the most
    recently scanned one if several), or None."""
    from app.discovery.database import get_db
    db = await get_db(slug=slug)
    try:
        cur = await db.execute(
            "SELECT id, name, last_lookup_at FROM authors "
            "WHERE goodreads_id = ? AND last_lookup_at IS NOT NULL "
            "ORDER BY last_lookup_at DESC LIMIT 1",
            (str(gr_author_id),),
        )
        row = await cur.fetchone()
    finally:
        await db.close()
    if row is None:
        return None
    return {"id": int(row["id"]), "name": row["name"],
            "last_lookup_at": float(row["last_lookup_at"])}


async def _known_index(slug: str, author_id: int) -> tuple[set[str], set[str]]:
    """(Goodreads book IDs anywhere in the library, title keys of the
    author's and linked authors' books — hidden ones included)."""
    from app.discovery.database import get_db
    from app.discovery.sources.goodreads import known_title_keys
    db = await get_db(slug=slug)
    try:
        cur = await db.execute(
            "SELECT goodreads_id FROM books "
            "WHERE goodreads_id IS NOT NULL AND goodreads_id != ''"
        )
        gr_ids = {str(r[0]) for r in await cur.fetchall()}
        linked = [author_id]
        cur = await db.execute(
            "SELECT canonical_author_id, alias_author_id FROM pen_name_links "
            "WHERE canonical_author_id = ? OR alias_author_id = ?",
            (author_id, author_id),
        )
        for r in await cur.fetchall():
            for v in (r[0], r[1]):
                if v not in linked:
                    linked.append(v)
        ph = ",".join("?" * len(linked))
        cur = await db.execute(
            f"SELECT title FROM books WHERE id IN "
            f"(SELECT book_id FROM book_authors WHERE author_id IN ({ph}))",
            linked,
        )
        titles = [r[0] for r in await cur.fetchall()]
    finally:
        await db.close()
    return gr_ids, known_title_keys(titles)


def title_is_known(title: str, keys: set[str]) -> bool:
    """A Goodreads title discovery already has: whole, or the part after
    its last colon ("Knights of Eternity : Calamity" is an owned
    "Calamity"). Never the part before a colon: "Magic's Toll: Cursebound"
    stays new next to "Magic's Toll: Fatebound"."""
    from app.discovery.sources.goodreads import _norm_title
    if _norm_title(title) in keys:
        return True
    if ":" in (title or ""):
        tail = _norm_title(title.rsplit(":", 1)[1])
        if tail and tail in keys:
            return True
    return False


def _classify(rec: dict, gr_ids: set[str], keys: set[str], *,
              include_nonfiction: bool) -> tuple[str, Optional[str]]:
    """(state, reason) for a list entry before any request."""
    from app.discovery.sources.goodreads import is_set_title
    title = rec.get("title") or ""
    if rec.get("is_audio_list"):
        return SKIPPED, "audio"
    if is_set_title(title):
        return SKIPPED, "set_title"
    reason = goodreads_filters.title_skip_reason(
        title, include_nonfiction=include_nonfiction,
    )
    if reason:
        return SKIPPED, reason
    if str(rec.get("book_id")) in gr_ids or title_is_known(title, keys):
        return KNOWN, None
    return PENDING, None


# ─── Seeding (who has candidates) ────────────────────────────


async def refresh_author(
    gr_author_id: str, slug: str, *, now: Optional[float] = None,
    scanned_now: bool = False,
) -> Optional[dict[str, int]]:
    """Bring `gr_author_id`'s candidate rows in `slug` up to date with its
    cached list page. Returns counts per new state, or None when the author
    isn't tracked (an audiobook library, no scan has covered it, or no
    cached list).

    First time: every entry is classified; an author listing ≤ 100 books
    gets `pending` rows (the first fill), a bigger one `baseline` rows.
    Later: only entries new to the list are added, `pending` whatever the
    size (weekly new IDs). Every time: undecided rows discovery has since
    found are marked `known`. `scanned_now` (a scan just covered it) puts the
    author at the front.
    """
    from app.discovery.metadata_cache_reader import read_cached_goodreads_raw_books
    now = now if now is not None else time.time()
    if not _is_ebook_library(slug):
        return None
    author = await _discovery_author(slug, gr_author_id)
    if author is None:
        return None
    records = await read_cached_goodreads_raw_books(
        author_id=str(gr_author_id), library_slug=slug,
    )
    if records is None:
        return None
    gr_ids, keys = await _known_index(slug, author["id"])
    include_nf = goodreads_includes_nonfiction(load_settings())

    counts: dict[str, int] = {}
    db = await metadata_cache.get_db(_SOURCE)
    try:
        cur = await db.execute(
            f"SELECT seeded_at FROM {_at()} WHERE author_id = ? AND library_slug = ?",
            (str(gr_author_id), slug),
        )
        seeded = await cur.fetchone() is not None
        cur = await db.execute(
            f"SELECT book_id, state, title FROM {_ct()} "
            f"WHERE author_id = ? AND library_slug = ?",
            (str(gr_author_id), slug),
        )
        existing = {r["book_id"]: (r["state"], r["title"]) for r in await cur.fetchall()}
        listed = len(records)
        first_fill = listed <= FIRST_FILL_MAX_LISTED
        origin = ORIGIN_WEEKLY if seeded else ORIGIN_FIRST_FILL
        for pos, rec in enumerate(records):
            bid = str(rec.get("book_id") or "")
            if not bid or bid in existing:
                continue
            st, reason = _classify(rec, gr_ids, keys, include_nonfiction=include_nf)
            if st == PENDING and not seeded and not first_fill:
                st = BASELINE
            await db.execute(
                f"INSERT OR IGNORE INTO {_ct()} (author_id, library_slug, book_id, "
                f"title, position, list_json, state, reason, origin, first_seen_at, "
                f"updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(gr_author_id), slug, bid, rec.get("title") or "", pos,
                 json.dumps(rec), st, reason, origin, now, now),
            )
            existing[bid] = (st, rec.get("title") or "")
            counts[st] = counts.get(st, 0) + 1
        # Undecided rows another source has created since.
        for bid, (st, title) in existing.items():
            if st in (PENDING, AWAITING_PAGE, BASELINE) and (
                bid in gr_ids or title_is_known(title or "", keys)
            ):
                await db.execute(
                    f"UPDATE {_ct()} SET state = ?, updated_at = ? "
                    f"WHERE author_id = ? AND library_slug = ? AND book_id = ?",
                    (KNOWN, now, str(gr_author_id), slug, bid),
                )
                counts["became_known"] = counts.get("became_known", 0) + 1
        bump = now if scanned_now else (
            author["last_lookup_at"]
            if not seeded and now - author["last_lookup_at"] <= _RECENT_SCAN_S
            else None
        )
        if seeded:
            await db.execute(
                f"UPDATE {_at()} SET listed = ?, seshat_author_id = ?, "
                f"scan_bumped_at = COALESCE(?, scan_bumped_at) "
                f"WHERE author_id = ? AND library_slug = ?",
                (listed, author["id"], bump, str(gr_author_id), slug),
            )
        else:
            await db.execute(
                f"INSERT INTO {_at()} (author_id, library_slug, seshat_author_id, "
                f"listed, first_fill, seeded_at, scan_bumped_at) "
                f"VALUES (?,?,?,?,?,?,?)",
                (str(gr_author_id), slug, author["id"], listed,
                 1 if first_fill else 0, now, bump),
            )
        await db.commit()
    finally:
        await db.close()
    if counts.get(PENDING):
        _log.info(
            "goodreads candidates: %s %s in %s — %d new candidate(s) "
            "(%d listed, %s)",
            "added" if seeded else "seeded", gr_author_id, slug,
            counts[PENDING], listed,
            "weekly new IDs" if seeded else (
                "first fill" if first_fill else "over the first-fill cap"),
        )
    return counts


async def seed_new_authors(*, now: Optional[float] = None, limit: int = 40) -> int:
    """Seed up to `limit` cached authors that have no candidate rows yet
    (and that a scan has covered); returns how many were seeded."""
    now = now if now is not None else time.time()
    st_table = metadata_cache.state_table(_SOURCE)
    db = await metadata_cache.get_db(_SOURCE)
    try:
        cur = await db.execute(
            f"SELECT s.author_id, s.library_slug FROM {st_table} s "
            f"LEFT JOIN {_at()} a ON a.author_id = s.author_id "
            f"AND a.library_slug = s.library_slug "
            f"WHERE s.last_outcome = 'ok' AND a.author_id IS NULL "
            f"ORDER BY s.book_count ASC, s.author_id"
        )
        rows = [(r[0], r[1]) for r in await cur.fetchall()]
    finally:
        await db.close()
    scanned: dict[str, set[str]] = {}
    seeded = 0
    for gr_author_id, slug in rows:
        if seeded >= limit:
            break
        if slug not in scanned:
            scanned[slug] = await _scanned_goodreads_ids(slug)
        if str(gr_author_id) not in scanned[slug]:
            continue
        if await refresh_author(gr_author_id, slug, now=now) is not None:
            seeded += 1
    return seeded


async def _scanned_goodreads_ids(slug: str) -> set[str]:
    """Goodreads author IDs of the authors a scan has covered in `slug`
    (none for an audiobook library)."""
    if not _is_ebook_library(slug):
        return set()
    from app.discovery.database import get_db
    db = await get_db(slug=slug)
    try:
        cur = await db.execute(
            "SELECT goodreads_id FROM authors WHERE goodreads_id IS NOT NULL "
            "AND goodreads_id != '' AND last_lookup_at IS NOT NULL"
        )
        return {str(r[0]) for r in await cur.fetchall()}
    finally:
        await db.close()


async def note_author_scanned(seshat_author_id: int, slug: str) -> None:
    """A source scan just covered this author (end of `lookup_author`): its
    candidates go to the front, and the books other sources just created
    drop out (G86). Never raises."""
    try:
        if not _is_ebook_library(slug):
            return
        from app.discovery.database import get_db
        db = await get_db(slug=slug)
        try:
            cur = await db.execute(
                "SELECT goodreads_id FROM authors WHERE id = ?", (seshat_author_id,),
            )
            row = await cur.fetchone()
        finally:
            await db.close()
        if not row or not row[0]:
            return
        await refresh_author(str(row[0]), slug, scanned_now=True)
    except Exception:
        _log.exception(
            "goodreads candidates: refresh after a scan of author %s failed "
            "(non-fatal)", seshat_author_id,
        )


# ─── Picking the next step ───────────────────────────────────


async def _next_candidate(db, st: str) -> Optional[dict]:
    cur = await db.execute(
        f"SELECT c.*, a.seshat_author_id, a.listed FROM {_ct()} c "
        f"JOIN {_at()} a ON a.author_id = c.author_id AND a.library_slug = c.library_slug "
        f"WHERE c.state = ? "
        f"ORDER BY (a.last_worked_at IS NOT NULL AND a.last_worked_at = "
        f"  (SELECT MAX(last_worked_at) FROM {_at()})) DESC, "
        f"  (a.scan_bumped_at IS NOT NULL) DESC, a.scan_bumped_at DESC, "
        f"  (c.origin = ?) DESC, a.listed ASC, c.author_id, c.position "
        f"LIMIT 1",
        (st, ORIGIN_WEEKLY),
    )
    row = await cur.fetchone()
    return dict(row) if row is not None else None


async def _set_state(
    row: dict, st: str, *, reason: Optional[str] = None,
    attempts: Optional[int] = None, now: Optional[float] = None,
) -> None:
    now = now if now is not None else time.time()
    db = await metadata_cache.get_db(_SOURCE)
    try:
        await db.execute(
            f"UPDATE {_ct()} SET state = ?, reason = ?, attempts = ?, updated_at = ? "
            f"WHERE author_id = ? AND library_slug = ? AND book_id = ?",
            (st, reason, row.get("attempts", 0) if attempts is None else attempts,
             now, row["author_id"], row["library_slug"], row["book_id"]),
        )
        await db.execute(
            f"UPDATE {_at()} SET last_worked_at = ? "
            f"WHERE author_id = ? AND library_slug = ?",
            (now, row["author_id"], row["library_slug"]),
        )
        await db.commit()
    finally:
        await db.close()
    row["state"], row["reason"] = st, reason


def merge_blocked_by() -> Optional[str]:
    """What the worker's merge waits for (it switches the process-wide
    active library, as scans, syncs and Hygiene do): None when free."""
    if getattr(state, "_source_scan_refs", 0) > 0:
        return "a source scan"
    if getattr(state, "_library_sync_in_progress", False):
        return "a library sync"
    task = getattr(state, "_hygiene_task", None)
    if task is not None and not task.done():
        return "Hygiene"
    return None


@dataclass
class StepResult:
    outcome: str            # merged | autocomplete | page | phase2 | blocked | idle
    detail: str = ""
    # When the next step could do something (seconds); the worker sleeps at
    # most this long.
    retry_in_s: float = 0.0


# ─── Autocomplete step ───────────────────────────────────────


async def _autocomplete(query: str) -> tuple[str, list]:
    """('ok', hits) | ('blocked', []) | ('error', [])."""
    session = await goodreads_session.get_session()
    try:
        resp = await session.get(_AUTOCOMPLETE + urllib.parse.quote(query))
    except goodreads_session.GoodreadsBackingOff:
        return "blocked", []
    except Exception as e:
        _log.debug("goodreads candidates: autocomplete error for %r: %s", query, e)
        return "error", []
    if goodreads_session.is_soft_block(resp):
        return "blocked", []
    if getattr(resp, "status_code", None) != 200:
        return "error", []
    try:
        data = resp.json()
    except Exception:
        return "error", []
    return "ok", [h for h in data if isinstance(h, dict)] if isinstance(data, list) else []


def _confirming_hits(hits: list, gr_author_id: str, book_id: str, title: str) -> list:
    """G76: the same Goodreads author ID AND (the same book ID OR the same
    normalised title). The list's own book ID first."""
    from app.discovery.sources.goodreads import _norm_title
    want = _norm_title(title)
    out = []
    for h in hits:
        if str((h.get("author") or {}).get("id")) != str(gr_author_id):
            continue
        same_id = str(h.get("bookId")) == str(book_id)
        same_title = _norm_title(h.get("bookTitleBare") or h.get("title") or "") == want
        if same_id or same_title:
            out.append((0 if same_id else 1, h))
    return [h for _, h in sorted(out, key=lambda x: x[0])]


async def _autocomplete_step(row: dict, author_name: str) -> StepResult:
    title = row.get("title") or ""
    gr_author_id, book_id = row["author_id"], row["book_id"]
    status, hits = await _autocomplete(title)
    if status != "ok":
        return await _autocomplete_failed(row, status)
    confirming = _confirming_hits(hits, gr_author_id, book_id, title)
    if not confirming and author_name:
        # G97: a generic title's book can be past autocomplete's top five.
        status, hits = await _autocomplete(f"{title} {author_name}")
        if status != "ok":
            return await _autocomplete_failed(row, status)
        confirming = _confirming_hits(hits, gr_author_id, book_id, title)
    if not confirming:
        await _set_state(row, REJECTED, reason="no_autocomplete_match")
        return StepResult("autocomplete", f"{title!r}: no match under the author")
    hit = confirming[0]
    await goodreads_store.save_hit(hit)
    reason = goodreads_filters.hit_reject_reason(
        hit.get("bookTitleBare") or hit.get("title") or title,
        hit.get("numPages"),
        goodreads_store.hit_snippet(hit),
        include_nonfiction=goodreads_includes_nonfiction(load_settings()),
    )
    if reason:
        await _set_state(row, REJECTED, reason=reason)
        return StepResult("autocomplete", f"{title!r}: rejected ({reason})")
    await _set_state(row, AWAITING_PAGE)
    return StepResult("autocomplete", f"{title!r}: confirmed, page next")


async def _autocomplete_failed(row: dict, status: str) -> StepResult:
    if status == "blocked":
        return StepResult(
            "blocked", "autocomplete backing off",
            retry_in_s=_backoff_remaining(source_gate.KIND_AUTOCOMPLETE),
        )
    attempts = int(row.get("attempts") or 0) + 1
    if attempts >= MAX_PAGE_ERRORS:
        await _set_state(row, REJECTED, reason="autocomplete_error", attempts=attempts)
    else:
        await _set_state(row, PENDING, attempts=attempts)
    return StepResult("autocomplete", f"{row.get('title')!r}: autocomplete error")


def _backoff_remaining(kind: str) -> float:
    until = goodreads_session.backoff_until(kind)
    return max(0.0, until - time.time()) if until else 0.0


# ─── Book page step ──────────────────────────────────────────


async def fetch_page(book_id: str) -> tuple[str, Optional[dict]]:
    """Fetch and store one book page: ('ok', details) | ('blocked', None) |
    ('not_found', None) | ('error', None)."""
    session = await goodreads_session.get_session()
    try:
        resp = await session.get(f"{_BASE}/book/show/{book_id}")
    except goodreads_session.GoodreadsBackingOff:
        return "blocked", None
    except Exception as e:
        _log.debug("goodreads candidates: page error for %s: %s", book_id, e)
        return "error", None
    if goodreads_session.is_soft_block(resp):
        return "blocked", None
    status = getattr(resp, "status_code", None)
    if status == 404:
        return "not_found", None
    if status != 200 or not getattr(resp, "text", ""):
        return "error", None
    return "ok", await goodreads_store.save_page(book_id, resp.text)


async def _page_step(row: dict) -> StepResult:
    title = row.get("title") or ""
    status, details = await fetch_page(row["book_id"])
    if status == "blocked":
        return StepResult(
            "blocked", "book pages backing off",
            retry_in_s=_backoff_remaining(source_gate.KIND_BOOK_PAGE),
        )
    if status == "not_found":
        await _set_state(row, REJECTED, reason="page_404")
        return StepResult("page", f"{title!r}: page gone (404)")
    if status == "error":
        attempts = int(row.get("attempts") or 0) + 1
        if attempts >= MAX_PAGE_ERRORS:
            await _set_state(row, REJECTED, reason="page_error", attempts=attempts)
        else:
            await _set_state(row, AWAITING_PAGE, attempts=attempts)
        return StepResult("page", f"{title!r}: page error")
    reason = goodreads_filters.page_reject_reason(
        details or {}, include_nonfiction=goodreads_includes_nonfiction(load_settings()),
    )
    if reason:
        await _set_state(row, REJECTED, reason=reason)
        return StepResult("page", f"{title!r}: rejected ({reason})")
    await _set_state(row, ACCEPTED)
    merged = await _merge(row, details or {})
    return StepResult("page", f"{title!r}: accepted{merged}")


# ─── Creating the book (G87 / G102) ──────────────────────────


def _book_result(row: dict, details: dict):
    from app.discovery.sources.base import BookResult
    try:
        rec = json.loads(row.get("list_json") or "{}")
    except (TypeError, ValueError):
        rec = {}
    book_id = row["book_id"]
    unreleased = bool(details.get("is_unreleased"))
    return BookResult(
        title=rec.get("title") or row.get("title") or "",
        series_name=details.get("series_name") or rec.get("list_series"),
        series_index=details.get("series_index") or rec.get("list_series_idx"),
        isbn=details.get("isbn13") or None,
        cover_url=details.get("cover_url") or rec.get("list_cover"),
        pub_date=None if unreleased else details.get("pub_date"),
        expected_date=details.get("expected_date"),
        is_unreleased=unreleased,
        description=details.get("description"),
        page_count=details.get("page_count"),
        external_id=book_id,
        language=details.get("language") or "English",
        source="goodreads",
        source_url=f"{_BASE}/book/show/{book_id}",
        contributors=list(details.get("contributors") or []),
    )


async def _merge(row: dict, details: dict) -> str:
    """Merge one accepted candidate now, unless a scan / sync / Hygiene is
    running (it stays `accepted` and `merge_waiting` picks it up). Returns
    a note for the step's log line."""
    blocker = merge_blocked_by()
    if blocker:
        return f", merge waits for {blocker}"
    from app.discovery.lookup import merge_goodreads_book
    try:
        created, updated = await merge_goodreads_book(
            int(row["seshat_author_id"]), row["library_slug"], row["author_id"],
            _book_result(row, details),
        )
    except Exception:
        attempts = int(row.get("attempts") or 0) + 1
        _log.exception(
            "goodreads candidates: merging %s (%s) failed (attempt %d)",
            row["book_id"], row.get("title"), attempts,
        )
        if attempts >= MAX_PAGE_ERRORS:
            await _set_state(row, REJECTED, reason="merge_error", attempts=attempts)
        else:
            await _set_state(row, ACCEPTED, attempts=attempts)
        return ", merge failed"
    await _set_state(row, CREATED, reason="new" if created else "existing")
    return ", created" if created else ", merged into an existing book"


async def merge_waiting() -> Optional[StepResult]:
    """Merge one accepted candidate whose merge had to wait, if any and
    nothing blocks it."""
    if merge_blocked_by():
        return None
    db = await metadata_cache.get_db(_SOURCE)
    try:
        row = await _next_candidate(db, ACCEPTED)
    finally:
        await db.close()
    if row is None:
        return None
    stored = goodreads_store.page_details_from_row(
        await goodreads_store.get_book(row["book_id"]),
    )
    if stored is None:
        await _set_state(row, AWAITING_PAGE)
        return StepResult("merged", f"{row.get('title')!r}: page missing, refetch")
    note = await _merge(row, stored)
    return StepResult("merged", f"{row.get('title')!r}{note}")


# ─── One step ────────────────────────────────────────────────


async def _author_name(row: dict) -> str:
    from app.discovery.database import get_db
    db = await get_db(slug=row["library_slug"])
    try:
        cur = await db.execute(
            "SELECT name FROM authors WHERE id = ?", (row["seshat_author_id"],),
        )
        r = await cur.fetchone()
    finally:
        await db.close()
    return str(r[0]) if r and r[0] else ""


async def _still_unknown(row: dict) -> bool:
    """Re-check just before spending a request: a scan may have created
    the book since the row was written."""
    gr_ids, keys = await _known_index(row["library_slug"], int(row["seshat_author_id"]))
    if row["book_id"] in gr_ids or title_is_known(row.get("title") or "", keys):
        await _set_state(row, KNOWN)
        return False
    return True


async def step() -> StepResult:
    """Do the next piece of candidate work: a waiting merge, a book page
    (when its gap and backoff allow), an autocomplete, or a phase-2 page.
    Never raises."""
    try:
        done = await merge_waiting()
        if done is not None:
            return done
        page_wait = source_gate.kind_wait_seconds("goodreads", source_gate.KIND_BOOK_PAGE)
        page_backoff = _backoff_remaining(source_gate.KIND_BOOK_PAGE)
        db = await metadata_cache.get_db(_SOURCE)
        try:
            awaiting = await _next_candidate(db, AWAITING_PAGE)
            pending = await _next_candidate(db, PENDING)
        finally:
            await db.close()
        if awaiting is not None and page_wait <= 0 and page_backoff <= 0:
            if await _still_unknown(awaiting):
                return await _page_step(awaiting)
            return StepResult("page", f"{awaiting.get('title')!r}: now known")
        ac_backoff = _backoff_remaining(source_gate.KIND_AUTOCOMPLETE)
        if pending is not None and ac_backoff <= 0:
            if await _still_unknown(pending):
                return await _autocomplete_step(pending, await _author_name(pending))
            return StepResult("autocomplete", f"{pending.get('title')!r}: now known")
        if awaiting is None and pending is None and page_wait <= 0 and page_backoff <= 0:
            done = await phase2_step()
            if done is not None:
                return done
        waits = [w for w in (
            page_wait if awaiting is not None or phase2_enabled() else 0.0,
            page_backoff if awaiting is not None else 0.0,
            ac_backoff if pending is not None else 0.0,
        ) if w > 0]
        return StepResult("idle", retry_in_s=min(waits) if waits else 0.0)
    except Exception:
        _log.exception("goodreads candidates: step failed (non-fatal)")
        return StepResult("idle", "step failed", retry_in_s=60.0)


# ─── Phase 2 (G88): every cached entry's page, while switched on ─


def phase2_enabled() -> bool:
    mc = (load_settings().get("metadata_cache") or {}).get("goodreads") or {}
    return bool(mc.get("phase2_enabled", False))


_PHASE2_SEED_EVERY_S = 3600.0
_phase2_seeded_at = 0.0


async def _seed_phase2(now: float) -> None:
    """Add every cached list entry without a stored page (title filters
    applied) to the phase-2 table, smallest catalogue first."""
    global _phase2_seeded_at
    if now - _phase2_seeded_at < _PHASE2_SEED_EVERY_S:
        return
    _phase2_seeded_at = now
    from app.discovery.sources.goodreads import is_set_title
    lp = metadata_cache.list_pages_table(_SOURCE)
    st_table = metadata_cache.state_table(_SOURCE)
    p2 = metadata_cache.phase2_table(_SOURCE)
    bt = metadata_cache.books_table(_SOURCE)
    include_nf = goodreads_includes_nonfiction(load_settings())
    db = await metadata_cache.get_db(_SOURCE)
    try:
        cur = await db.execute(
            f"SELECT lp.author_id, lp.book_ids_json, MIN(s.book_count) AS listed "
            f"FROM {lp} lp JOIN {st_table} s ON s.author_id = lp.author_id "
            f"AND s.library_slug = lp.library_slug GROUP BY lp.author_id, lp.page_num"
        )
        rows = await cur.fetchall()
        cur = await db.execute(f"SELECT book_id FROM {bt} WHERE page_fetched_at IS NOT NULL")
        have = {r[0] for r in await cur.fetchall()}
        for r in rows:
            try:
                recs = json.loads(r["book_ids_json"]) or []
            except (TypeError, ValueError):
                continue
            for rec in recs:
                if not isinstance(rec, dict):
                    continue
                bid = str(rec.get("book_id") or "")
                title = rec.get("title") or ""
                if not bid or bid in have or rec.get("is_audio_list") or is_set_title(title):
                    continue
                if goodreads_filters.title_skip_reason(title, include_nonfiction=include_nf):
                    continue
                await db.execute(
                    f"INSERT OR IGNORE INTO {p2} (book_id, author_id, title, listed, "
                    f"state, updated_at) VALUES (?,?,?,?,'pending',?)",
                    (bid, r["author_id"], title, int(r["listed"] or 0), now),
                )
        await db.commit()
    finally:
        await db.close()


async def phase2_step(*, now: Optional[float] = None) -> Optional[StepResult]:
    """One phase-2 page, or None when phase 2 is off or has nothing left."""
    if not phase2_enabled():
        return None
    now = now if now is not None else time.time()
    await _seed_phase2(now)
    p2 = metadata_cache.phase2_table(_SOURCE)
    db = await metadata_cache.get_db(_SOURCE)
    try:
        cur = await db.execute(
            f"SELECT * FROM {p2} WHERE state = 'pending' "
            f"ORDER BY listed ASC, author_id, book_id LIMIT 1"
        )
        row = await cur.fetchone()
    finally:
        await db.close()
    if row is None:
        return None
    row = dict(row)
    stored = await goodreads_store.get_book(row["book_id"])
    if stored and stored.get("page_fetched_at"):
        # Fetched since the seed (a candidate, or a grab's enrichment).
        status = "ok"
    else:
        status, _details = await fetch_page(row["book_id"])
    if status == "blocked":
        return StepResult(
            "blocked", "book pages backing off (phase 2)",
            retry_in_s=_backoff_remaining(source_gate.KIND_BOOK_PAGE),
        )
    attempts = int(row.get("attempts") or 0) + (0 if status == "ok" else 1)
    new_state = (
        "fetched" if status == "ok" else
        "gone" if status == "not_found" else
        "failed" if attempts >= MAX_PAGE_ERRORS else "pending"
    )
    db = await metadata_cache.get_db(_SOURCE)
    try:
        await db.execute(
            f"UPDATE {p2} SET state = ?, attempts = ?, updated_at = ? WHERE book_id = ?",
            (new_state, attempts, now, row["book_id"]),
        )
        await db.commit()
    finally:
        await db.close()
    return StepResult("phase2", f"{row.get('title')!r}: {new_state}")


# ─── Status (G73 rule 5) ─────────────────────────────────────


async def status() -> dict[str, Any]:
    """Progress for the worker's status card."""
    db = await metadata_cache.get_db(_SOURCE)
    try:
        cur = await db.execute(
            f"SELECT c.origin, c.state, COUNT(*) FROM {_ct()} c GROUP BY c.origin, c.state"
        )
        by = [(r[0], r[1], int(r[2])) for r in await cur.fetchall()]
        cur = await db.execute(
            f"SELECT COUNT(*), SUM(first_fill) FROM {_at()}"
        )
        a = await cur.fetchone()
        cur = await db.execute(
            f"SELECT COUNT(*) FROM {_at()} a WHERE a.first_fill = 1 AND EXISTS ("
            f"SELECT 1 FROM {_ct()} c WHERE c.author_id = a.author_id AND "
            f"c.library_slug = a.library_slug AND c.state IN ('pending','awaiting_page','accepted'))"
        )
        authors_left = int((await cur.fetchone())[0])
        cur = await db.execute(
            f"SELECT c.title, c.state, c.author_id FROM {_ct()} c "
            f"WHERE c.state IN ('pending','awaiting_page','accepted','created','rejected') "
            f"ORDER BY c.updated_at DESC LIMIT 1"
        )
        last = await cur.fetchone()
        p2 = metadata_cache.phase2_table(_SOURCE)
        cur = await db.execute(f"SELECT state, COUNT(*) FROM {p2} GROUP BY state")
        p2_counts = {r[0]: int(r[1]) for r in await cur.fetchall()}
        cur = await db.execute(
            f"SELECT reason, COUNT(*) FROM {_ct()} WHERE state = 'rejected' "
            f"GROUP BY reason ORDER BY COUNT(*) DESC"
        )
        rejected_by = {str(r[0]): int(r[1]) for r in await cur.fetchall()}
    finally:
        await db.close()

    def _sum(origin: Optional[str], states: tuple[str, ...]) -> int:
        return sum(n for o, s, n in by if (origin is None or o == origin) and s in states)

    decided = (REJECTED, CREATED)
    page_wait = source_gate.kind_wait_seconds("goodreads", source_gate.KIND_BOOK_PAGE)
    return {
        "first_fill": {
            "candidates": _sum(ORIGIN_FIRST_FILL, UNDECIDED + decided),
            "decided": _sum(ORIGIN_FIRST_FILL, decided),
            "authors": int(a[1] or 0) if a else 0,
            "authors_left": authors_left,
        },
        "weekly": {
            "candidates": _sum(ORIGIN_WEEKLY, UNDECIDED + decided),
            "decided": _sum(ORIGIN_WEEKLY, decided),
        },
        "pending": _sum(None, (PENDING,)),
        "awaiting_page": _sum(None, (AWAITING_PAGE,)),
        "accepted_waiting_merge": _sum(None, (ACCEPTED,)),
        "created": _sum(None, (CREATED,)),
        "rejected": _sum(None, (REJECTED,)),
        "rejected_by_reason": rejected_by,
        "tracked_authors": int(a[0] or 0) if a else 0,
        "last": (
            {"title": last["title"], "state": last["state"], "author_id": last["author_id"]}
            if last else None
        ),
        "book_page_next_at": (time.time() + page_wait) if page_wait > 0 else None,
        "phase2": {
            "enabled": phase2_enabled(),
            "fetched": p2_counts.get("fetched", 0),
            "left": p2_counts.get("pending", 0),
            "gone": p2_counts.get("gone", 0),
            "failed": p2_counts.get("failed", 0),
        },
    }
