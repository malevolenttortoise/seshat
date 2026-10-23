"""The missing-books cleanup: re-applies the 2026-10 fix pass's rules to the
missing books already listed (discovery fix pass S8; F13 / F14, G209).

The fix pass changed what a scan lists: only authors you own or picked are
scanned (D1 / F15), OpenLibrary never creates a book (D11), comics, box
sets, anthologies and other non-books are created hidden with a reason
(ADR-0029), copies of books you own merge into them (ADR-0028 + F10), and
a book whose title names its series is filed there (F14). Rows from before
the fix pass don't follow those rules; this applies them, in order:

  1. Remove missing books none of whose authors you scan (an owner here, or
     your own allow-list entry; the List-missing switch doesn't count).
  2. Remove missing books only OpenLibrary knows (no other source's id or
     URL, not on MAM).
  3. Hide the filtered families, with their reason (Hidden → Filtered).
  4. Fold missing copies of an owned book (and owned rows with no Calibre /
     Audiobookshelf item, which a Calibre sync's flip leaves) into it, when
     exactly one owned book matches.
  5. File a standalone missing book in the series its title names.
  6. Merge the cached Amazon books and Goodreads candidates again with the
     title identity, as a cache-only scan would (no requests): books the
     old merge folded into another row come back as their own.

Never touched: owned books (except as a fold's winner), books you hid
yourself, books you showed (`shown`), books hidden because an author's
List-missing switch is off (`author_off`: they come back when it goes on),
and, for the removals, books a grab is linked to. No source requests.

**Preview → Apply** (G209). A preview copies every library DB and
`seshat.db` (SQLite backup) and runs the same steps on the copies, with
this task's connections pointed at them (`using_copies` / `using_copy`):
what it reports is what Apply does to the real DBs, give or take what
changes in between. Apply needs the id of a preview from the last hour.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app import state
from app.config import load_settings
from app.discovery import filters

logger = logging.getLogger("seshat.discovery.missing_cleanup")

# (key, what the step is about, what it does to a row)
STEPS: tuple[tuple[str, str, str], ...] = (
    ("no_eligible_author", "No author you scan", "remove"),
    ("openlibrary_only", "Found only by OpenLibrary", "remove"),
    ("filtered", "Comics, box sets, anthologies and other non-books", "hide"),
    ("owned_copy", "Copies of books you own", "fold"),
    ("listed_copy", "Books listed twice", "fold"),
    ("series_in_title", "Series named in the title", "file"),
    ("cache_remerge", "Amazon and Goodreads books merged again", "merge"),
)
PREVIEW_TTL_S = 3600.0
# Rows per step a summary carries (the full lists are in the report).
SAMPLE_ROWS = 12

# A source other than OpenLibrary knows a row by one of these (D11).
_OTHER_SOURCE_ID_COLS = (
    "hardcover_id", "goodreads_id", "amazon_id", "kobo_id", "ibdb_id",
    "google_books_id", "fictiondb_id", "asin", "audible_id", "calibre_id",
    "audiobookshelf_id", "mam_torrent_id",
)

_last: dict[str, Optional[dict]] = {"preview": None, "apply": None}
_progress: dict[str, Any] = {"running": False, "mode": None, "library": "", "library_name": "", "step": "",
                             "step_name": ""}


# ─── The report ──────────────────────────────────────────────


def _new_report(mode: str) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "mode": mode,
        "started_at": time.time(),
        "finished_at": None,
        "steps": [
            {"key": k, "name": n, "action": a, "count": 0, "rows": [], "skipped": []}
            for k, n, a in STEPS
        ],
        "errors": [],
    }


def _step(report: dict, key: str) -> dict:
    return next(s for s in report["steps"] if s["key"] == key)


def summary(report: Optional[dict]) -> Optional[dict]:
    """A report without its full row lists: counts and a sample per step."""
    if report is None:
        return None
    return {
        **{k: v for k, v in report.items() if k != "steps"},
        "steps": [
            {**{k: v for k, v in s.items() if k not in ("rows", "skipped")},
             "sample": s["rows"][:SAMPLE_ROWS], "skipped_count": len(s["skipped"])}
            for s in report["steps"]
        ],
    }


def last_report(mode: str) -> Optional[dict]:
    return _last.get(mode)


def was_applied(preview_id: str) -> bool:
    """A preview describes one Apply: after it, the library has changed."""
    done = _last["apply"]
    return bool(done and done.get("preview_id") == preview_id)


def status() -> dict:
    return {
        **_progress,
        "preview_ttl_s": PREVIEW_TTL_S,
        "last_preview": summary(_last["preview"]),
        "last_apply": summary(_last["apply"]),
    }


def is_running() -> bool:
    task = getattr(state, "_missing_cleanup_task", None)
    return bool(_progress["running"] or (task is not None and not task.done()))


def blocked_by() -> Optional[str]:
    """What a cleanup run waits for: a scan, a library sync, Hygiene, or
    another cleanup (they all switch or write the same library DBs; the
    Goodreads worker's merge waits on the same list)."""
    from app.discovery.goodreads_candidates import merge_blocked_by
    return merge_blocked_by()


# ─── One library's rows ──────────────────────────────────────


class _Library:
    """What the steps read from one library DB, refreshed per step."""

    def __init__(self, db, pdb, slug: str, settings: dict, content_type: str = "ebook") -> None:
        self.db, self.pdb, self.slug, self.settings = db, pdb, slug, settings
        self.content_type = content_type
        self.grab_linked: frozenset[int] = frozenset()
        self.rows: dict[int, Any] = {}
        self.contribs: dict[int, list[tuple[int, str]]] = {}
        self.n_authors: dict[int, int] = {}

    async def load(self) -> None:
        self.grab_linked = frozenset(int(r[0]) for r in await (await self.pdb.execute(
            "SELECT book_id FROM book_grab_links WHERE library_slug = ?", (self.slug,),
        )).fetchall())
        self.rows = {r["id"]: r for r in await (await self.db.execute(
            "SELECT b.*, s.name AS series_name FROM books b "
            "LEFT JOIN series s ON s.id = b.series_id",
        )).fetchall()}
        from app.discovery.sources.base import contributor_is_author
        self.contribs, self.n_authors = {}, {}
        for r in await (await self.db.execute(
            "SELECT ba.book_id, ba.author_id, ba.role, a.name FROM book_authors ba "
            "JOIN authors a ON a.id = ba.author_id ORDER BY ba.book_id, ba.position",
        )).fetchall():
            self.contribs.setdefault(r["book_id"], []).append((r["author_id"], r["name"] or ""))
            if contributor_is_author(r["role"]):
                self.n_authors[r["book_id"]] = self.n_authors.get(r["book_id"], 0) + 1

    def kept(self, row) -> bool:
        """Rows no step changes (C28): owned, yours, shown, author off."""
        if row["owned"]:
            return True
        reason = row["hidden_reason"]
        if reason in (filters.SHOWN, filters.AUTHOR_OFF):
            return True
        return bool(row["hidden"]) and not reason

    def record(self, row, *, detail: str = "", **extra: Any) -> dict:
        """One row of a step's report: `detail` for the log and the full
        report, `extra` for the page (the filter reason, the owned book a
        copy folds into)."""
        return {
            "library": self.slug,
            "id": row["id"],
            "title": row["title"],
            "authors": [n for _, n in self.contribs.get(row["id"], [])],
            "source": row["discovered_by"] or row["source"],
            "series": row["series_name"],
            "index": row["series_index"],
            "hidden": bool(row["hidden"]),
            "hidden_reason": row["hidden_reason"],
            "detail": detail,
            **extra,
        }


async def _delete_books(db, ids: list[int]) -> None:
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        await db.execute(f"DELETE FROM book_authors WHERE book_id IN ({ph})", chunk)
        await db.execute(f"DELETE FROM books WHERE id IN ({ph})", chunk)


# ─── The steps ───────────────────────────────────────────────


async def _no_eligible_author(lib: _Library, report: dict) -> None:
    """Step 1 (D1 / F15, C6, C29): an author you scan owns a book here or
    is on your allow list by your doing, or is another spelling of one
    (G245); a switched-off author still counts (their books are
    `author_off`, kept)."""
    from app.discovery.roster import load_roster

    roster = await load_roster(lib.db, lib.slug, force=True)

    def scanned(aid: int, name: str) -> bool:
        return roster.counts_for_scans(name, aid)

    step, doomed = _step(report, "no_eligible_author"), []
    for row in lib.rows.values():
        if lib.kept(row) or row["id"] in lib.grab_linked:
            continue
        contribs = lib.contribs.get(row["id"], [])
        if any(scanned(aid, name) for aid, name in contribs):
            continue
        doomed.append(row["id"])
        step["rows"].append(lib.record(row, detail="no author" if not contribs else ""))
    await _delete_books(lib.db, doomed)
    step["count"] += len(doomed)


def _only_openlibrary(row) -> bool:
    if (row["discovered_by"] or row["source"]) != "openlibrary":
        return False
    if any(row[c] not in (None, "") for c in _OTHER_SOURCE_ID_COLS if c in row.keys()):
        return False
    try:
        urls = json.loads(row["source_url"] or "{}")
    except (TypeError, ValueError):
        urls = {}
    return not (isinstance(urls, dict) and any(k != "openlibrary" and v for k, v in urls.items()))


async def _openlibrary_only(lib: _Library, report: dict) -> None:
    """Step 2 (D11, C30)."""
    step, doomed = _step(report, "openlibrary_only"), []
    for row in lib.rows.values():
        if lib.kept(row) or row["id"] in lib.grab_linked or not _only_openlibrary(row):
            continue
        doomed.append(row["id"])
        step["rows"].append(lib.record(row))
    await _delete_books(lib.db, doomed)
    step["count"] += len(doomed)


async def _goodreads_genres(goodreads_ids) -> dict[str, list[str]]:
    """Stored Goodreads pages' genres for these book ids (read only)."""
    ids = sorted({str(g) for g in goodreads_ids if g})
    if not ids:
        return {}
    from app.discovery import metadata_cache
    out: dict[str, list[str]] = {}
    try:
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
    except Exception:
        return {}
    try:
        bt = metadata_cache.books_table(metadata_cache.SOURCE_GOODREADS)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in await (await db.execute(
                f"SELECT book_id, page_json FROM {bt} WHERE book_id IN ({ph}) "
                f"AND page_json IS NOT NULL", chunk,
            )).fetchall():
                try:
                    genres = (json.loads(r[1]) or {}).get("genres") or []
                except (TypeError, ValueError):
                    continue
                if genres:
                    out[str(r[0])] = [str(g) for g in genres]
    except Exception:
        logger.debug("missing cleanup: Goodreads genres unread", exc_info=True)
    finally:
        await db.close()
    return out


async def _filtered(lib: _Library, report: dict) -> None:
    """Step 3 (ADR-0029, C31): the creation verdict on a stored, visible,
    unclassified row: title, description, series name, page count, the
    stored Goodreads page's genres; C22's owned-stem rule and the language
    rule as at creation."""
    languages = lib.settings.get("languages", ["English"])
    owned_stems: dict[int, set[str]] = {}
    for row in lib.rows.values():
        if row["owned"]:
            stem = filters.series_ref_stem(row["title"] or "")
            if stem:
                for aid, _ in lib.contribs.get(row["id"], []):
                    owned_stems.setdefault(aid, set()).add(stem)
    todo = [
        r for r in lib.rows.values()
        if not r["owned"] and not r["hidden"] and r["hidden_reason"] is None
    ]
    genres = await _goodreads_genres(r["goodreads_id"] for r in todo)
    step = _step(report, "filtered")
    for row in todo:
        title = row["title"] or ""
        contribs = lib.contribs.get(row["id"], [])
        reason = filters.filter_reason(
            title, description=row["description"],
            n_authors=lib.n_authors.get(row["id"], 0), authors=[n for _, n in contribs],
            series_name=row["series_name"], page_count=row["page_count"],
            genres=genres.get(str(row["goodreads_id"] or ""), ()), content_type=lib.content_type,
        )
        if reason == filters.JUNK and filters.series_ref_stem(title) in {
                s for aid, _ in contribs for s in owned_stems.get(aid, ())}:
            reason = None
        if reason == filters.FOREIGN and "English" not in languages:
            reason = None
        if reason is None:
            continue
        hide = filters.hides(reason, lib.settings)
        await lib.db.execute(
            "UPDATE books SET hidden_reason = ?, hidden = ? WHERE id = ?",
            (reason, 1 if hide else 0, row["id"]),
        )
        step["rows"].append(lib.record(row, detail=reason if hide else f"{reason} (shown: its setting is off)",
                                       reason=reason, stays_shown=not hide))
        step["count"] += 1


async def _owned_copy(lib: _Library, report: dict) -> None:
    """Step 4 (ADR-0028 + F10, C32): fold a row into the one owned library
    book (Calibre or Audiobookshelf) the identity and a shared or linked
    contributor match, within its filter family. The owned book keeps its
    library byline."""
    from app.discovery.book_merge import MergeError, merge_books
    from app.discovery.calibre_sync import _absorb_matches
    from app.discovery.database import write_book_authors

    winners = [
        r for r in lib.rows.values()
        if r["owned"] and (r["calibre_id"] is not None or r["audiobookshelf_id"])
    ]
    step = _step(report, "owned_copy")
    matches: dict[int, list[int]] = {}
    for w in winners:
        ids = [aid for aid, _ in lib.contribs.get(w["id"], [])]
        ambiguous: list = []
        for c in await _absorb_matches(
                lib.db, ids, w["title"] or "", w["series_name"], w["series_index"],
                exclude_id=w["id"], ambiguous=ambiguous):
            matches.setdefault(c["id"], []).append(w["id"])
        # Several rows that aren't one book (G211): which, if any, is the
        # owned book is a guess, so none folds.
        for c in ambiguous:
            if c["id"] in lib.rows:
                step["skipped"].append(lib.record(lib.rows[c["id"]], detail=(
                    f"one of {len(ambiguous)} different books matching owned "
                    f"{w['id']} {w['title']!r}")))
    for loser_id, owners in sorted(matches.items()):
        loser = lib.rows.get(loser_id)
        if loser is None:
            continue
        # A flipped owned row (no Calibre / ABS item) folds too; other kept
        # rows stay as they are.
        flipped = bool(loser["owned"]) and loser["calibre_id"] is None and not loser["audiobookshelf_id"]
        if lib.kept(loser) and not flipped:
            continue
        if len(owners) != 1:
            step["skipped"].append(lib.record(loser, detail="matches %d owned books: %s" % (
                len(owners), "; ".join(f"{o} {lib.rows[o]['title']!r}" for o in owners))))
            continue
        winner = lib.rows[owners[0]]
        byline = [aid for aid, _ in lib.contribs.get(winner["id"], [])]
        try:
            await merge_books(
                lib.db, lib.pdb, library_slug=lib.slug,
                winner_id=winner["id"], loser_id=loser_id, reason="missing_cleanup_owned_copy",
            )
        except MergeError as exc:
            step["skipped"].append(lib.record(loser, detail=f"not merged: {exc}"))
            continue
        if byline:
            await write_book_authors(lib.db, winner["id"], byline)
        step["rows"].append(lib.record(
            loser, detail=f"→ owned {winner['id']} {winner['title']!r}"
            + (" (an owned row with no library item)" if flipped else ""),
            into_id=winner["id"], into_title=winner["title"]))
        step["count"] += 1


# What makes one listed copy the one to keep (G237): the row more sources know.
_SOURCE_ID_COLS = (
    "goodreads_id", "hardcover_id", "amazon_id", "kobo_id", "ibdb_id", "openlibrary_id",
    "google_books_id", "fictiondb_id", "isbn", "asin", "audible_id", "mam_torrent_id",
)


async def _listed_copy(lib: _Library, report: dict) -> None:
    """Step 5 (G237, C44): fold listed copies of one book into each other: one
    shared contributor and the identity calls them one book. The row more
    sources know (then the older one) is kept; ids, links, contributors and
    grab links carry over (`merge_books`). Not folded: a match on series
    position alone (a title that is only its series name), and a bare "X"
    against "X: Y" whose "Y" isn't the series. (A family, omnibus or
    two-series check measured on the reference install: no change, or only
    right folds lost.)"""
    from app.discovery.book_merge import MergeError, merge_books
    from app.works.title_identity import (
        TitleIndex, TitleRef, _no_real_title, _without_description, author_labels, parse_title, series_key,
        title_parts,
    )

    step = _step(report, "listed_copy")
    listed = [r for r in lib.rows.values() if not r["owned"] and not r["hidden"] and not lib.kept(r)]
    by_author: dict[int, list] = {}
    for r in listed:
        for aid, _ in lib.contribs.get(r["id"], []):
            by_author.setdefault(aid, []).append(r)
    titles_by_author: dict[int, list[str]] = {}
    for r in lib.rows.values():
        for aid, _ in lib.contribs.get(r["id"], []):
            titles_by_author.setdefault(aid, []).append(r["title"] or "")

    def known(r) -> int:
        return sum(1 for c in _SOURCE_ID_COLS if r[c] not in (None, ""))

    def ref(r) -> TitleRef:
        return TitleRef(r["title"] or "", r["series_name"], r["series_index"])

    def only_series(r) -> bool:
        return _no_real_title(parse_title(r["title"] or "").proper, r["series_name"])

    def head_of_another(a, b) -> bool:
        """A bare "X" against "X: Y" whose "Y" isn't the series: "Without
        Warning" and "Without Warning - After America", "Dark Faith" and
        "Dark Faith: Invocations" are two books (C44). "Zero Point" and
        "Zero Point: The Owner: Book Two" (the Owner series) fold."""
        names = {series_key(s).replace(" ", "") for s in (a["series_name"], b["series_name"]) if s}
        pa = title_parts(_without_description(parse_title(a["title"] or "").proper))
        pb = title_parts(_without_description(parse_title(b["title"] or "").proper))
        for one, many in ((pa, pb), (pb, pa)):
            if len(one) == 1 and len(many) >= 2 and one[0] == many[0]:
                if series_key(" ".join(many[1:])).replace(" ", "") not in names:
                    return True
        return False

    folded: set[int] = set()
    for aid, rows in sorted(by_author.items()):
        rows = sorted(rows, key=lambda r: (-known(r), r["id"]))
        index = TitleIndex(author_labels(titles_by_author.get(aid, [])))
        for r in rows:
            index.add(ref(r), r)
        for winner in rows:
            if winner["id"] in folded or only_series(winner):
                continue
            for loser in index.find_all(ref(winner)):
                if loser["id"] == winner["id"] or loser["id"] in folded or only_series(loser):
                    continue
                if head_of_another(winner, loser):
                    continue
                try:
                    await merge_books(
                        lib.db, lib.pdb, library_slug=lib.slug,
                        winner_id=winner["id"], loser_id=loser["id"], reason="missing_cleanup_listed_copy",
                    )
                except MergeError as exc:
                    step["skipped"].append(lib.record(loser, detail=f"not merged: {exc}"))
                    continue
                folded.add(loser["id"])
                step["rows"].append(lib.record(
                    loser, detail=f"→ {winner['id']} {winner['title']!r}",
                    into_id=winner["id"], into_title=winner["title"]))
                step["count"] += 1


async def _series_in_title(lib: _Library, report: dict) -> None:
    """Step 6 (F14 / G171, C33): a standalone missing book whose title names
    its series and number is filed there, in a contributor's series of that
    name first, as a scan files a new one."""
    from app.discovery.lookup import _is_omnibus, _norm_consensus_series
    from app.works.title_identity import series_in_title

    by_name: dict[tuple[int, str], int] = {}
    for r in await (await lib.db.execute("SELECT id, name, author_id FROM series")).fetchall():
        if r["name"] and r["author_id"] is not None:
            by_name.setdefault((r["author_id"], r["name"].lower()), r["id"])
            norm = _norm_consensus_series(r["name"])
            if norm:
                by_name.setdefault((r["author_id"], norm), r["id"])
    step = _step(report, "series_in_title")
    for row in lib.rows.values():
        if lib.kept(row) or row["series_id"] is not None or row["hidden_reason"] is not None:
            continue
        title = row["title"] or ""
        if _is_omnibus(title):
            continue
        contribs = lib.contribs.get(row["id"], [])
        aids = {aid for aid, _ in contribs}
        named = series_in_title(title, known=[name for aid, name in by_name if aid in aids])
        if named is None or not contribs:
            continue
        name, index = named
        sid = next((
            by_name.get((aid, key)) for aid, _ in contribs
            for key in (name.lower(), _norm_consensus_series(name))
            if key and by_name.get((aid, key))
        ), None)
        made = sid is None
        if made:
            cur = await lib.db.execute(
                "INSERT INTO series (name, author_id, last_lookup_at) VALUES (?,?,?)",
                (name, contribs[0][0], time.time()),
            )
            sid = cur.lastrowid
            by_name[(contribs[0][0], name.lower())] = sid
        await lib.db.execute(
            "UPDATE books SET series_id = ?, series_index = ? WHERE id = ?", (sid, index, row["id"]),
        )
        step["rows"].append(lib.record(
            row, detail=f"{name} #{index:g}" + (" (new series)" if made else "")))
        step["count"] += 1


async def _linked(db, author_id: int) -> tuple[list[int], dict[int, str]]:
    linked, kinds = [], {}
    for r in await (await db.execute(
        "SELECT canonical_author_id, alias_author_id, link_type FROM pen_name_links "
        "WHERE canonical_author_id = ? OR alias_author_id = ?", (author_id, author_id),
    )).fetchall():
        for col in ("canonical_author_id", "alias_author_id"):
            if r[col] != author_id and r[col] not in linked:
                linked.append(r[col])
                kinds[r[col]] = r["link_type"]
    return linked, kinds


async def _owned_refs(db, author_ids: list[int]) -> tuple[list[str], list[tuple]]:
    """The owned titles a scan validates a source against, and its owned
    omnibuses (`_validate_author`)."""
    from app.discovery.lookup import _is_omnibus
    ph = ",".join("?" * len(author_ids))
    rows = await (await db.execute(
        f"SELECT b.title, s.name AS series_name FROM books b LEFT JOIN series s ON s.id = b.series_id "
        f"WHERE b.owned = 1 AND b.id IN (SELECT book_id FROM book_authors WHERE author_id IN ({ph}))",
        author_ids,
    )).fetchall()
    titles = [r["title"] for r in rows]
    return titles, [(r["title"], r["series_name"]) for r in rows if _is_omnibus(r["title"] or "")]


async def _cache_remerge(lib: _Library, report: dict) -> None:
    """Step 7 (F13): the books cached for each scan-eligible author merged
    again with the title identity, as a cache-only scan would (Amazon's
    author cache; Goodreads' candidates the old merge called "existing",
    from their stored pages), with no author-level writes and no requests.
    A book the old merge folded into the wrong row comes back as its own.
    What stops a scan or the candidate worker stops it too: the roster, a
    blacklisted author record, the library-only setting, a source the scan
    doesn't ask, a Goodreads id a row already holds.
    Ids the old merge left on the wrong row stay where they are (C34,
    measured: which of two rows holds an id wrongly hinges on stored series
    numbers that are often wrong themselves, so a moved id landed on a
    duplicate about as often as on the book)."""
    from app.discovery import lookup as L
    from app.discovery import source_blacklist
    from app.discovery.metadata_cache_reader import make_amazon_cached_source, read_cached_author
    from app.discovery.roster import load_roster
    from app.discovery.sources.base import AuthorResult, SeriesResult

    # Scans list only owned books under this setting: nothing to bring back.
    if lib.settings.get("author_scan_owned_only"):
        return
    roster = await load_roster(lib.db, lib.slug, force=True)
    languages = lib.settings.get("languages", ["English"])
    exclude_audio = bool(lib.settings.get("exclude_audiobooks", True))
    first_new = (await (await lib.db.execute("SELECT COALESCE(MAX(id), 0) FROM books")).fetchone())[0]
    step = _step(report, "cache_remerge")

    async def merge(author_id, name, result, source):
        linked, kinds = await _linked(lib.db, author_id)
        await L._merge_result(
            author_id, result, source, languages, full_scan=False, owned_only=False,
            exclude_audiobooks=exclude_audio, linked_author_ids=linked, link_type_by_id=kinds,
            touch_author=False,
        )

    # Amazon: each author's cached books, where a scan of this library asks Amazon.
    amz = (lib.settings.get("metadata_sources") or {}).get("amazon") or {}
    amazon_on = L.scan_uses_source("amazon", lib.settings, lib.content_type)
    src = make_amazon_cached_source(
        format_filter=amz.get("format") or "kindle",
        audiobook_format_filter=amz.get("audiobook_format") or "audible_audiobook",
        language=amz.get("language") or "English",
    )
    src._content_type = src.AUDIOBOOK if lib.content_type == "audiobook" else src.EBOOK
    # Only authors a scan has merged before: the worker caches every
    # library author, and re-merging one never scanned is that author's
    # first scan (on the reference install ~21,000 new rows: F–Z and the
    # audiobook library), which is the re-scans' job, after the cleanup.
    authors = await (await lib.db.execute(
        "SELECT id, name, amazon_id FROM authors WHERE amazon_id IS NOT NULL AND amazon_id != '' "
        "AND last_lookup_at IS NOT NULL",
    )).fetchall() if amazon_on else []
    for a in authors:
        if roster.scan_skip_reason(a["name"] or "", a["id"]):
            continue
        # An author record you blacklisted is one a scan never imports from.
        if await source_blacklist.is_blacklisted("amazon", a["amazon_id"]):
            step["skipped"].append({"library": lib.slug, "id": None, "title": a["name"],
                                    "detail": "Amazon author record blacklisted"})
            continue
        linked, _ = await _linked(lib.db, a["id"])
        our, owned_sets = await _owned_refs(lib.db, [a["id"], *linked])
        result = await read_cached_author(
            source_name="amazon", author_id=a["amazon_id"], library_slug=lib.slug,
            language=src.language, format_filter=src._active_format_filter(),
            owned_titles=our, owned_only=False,
        )
        if result is None:
            continue
        if not await L._validate_author(a["name"], our, result, owned_sets):
            step["skipped"].append({"library": lib.slug, "id": None, "title": a["name"],
                                    "detail": "Amazon's cached catalogue shares no title with the owned books"})
            continue
        await merge(a["id"], a["name"], AuthorResult(name=a["name"], books=result.books,
                                                     series=result.series), "amazon")

    # Goodreads: candidates the old merge put into an existing book.
    from app.discovery import goodreads_candidates as gc
    from app.discovery import goodreads_store, metadata_cache
    gdb = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
    try:
        ct = metadata_cache.candidates_table(metadata_cache.SOURCE_GOODREADS)
        at = metadata_cache.candidate_authors_table(metadata_cache.SOURCE_GOODREADS)
        cands = [dict(r) for r in await (await gdb.execute(
            f"SELECT c.*, a.seshat_author_id FROM {ct} c JOIN {at} a "
            f"ON a.author_id = c.author_id AND a.library_slug = c.library_slug "
            f"WHERE c.state = 'created' AND c.reason = 'existing' AND c.library_slug = ? "
            f"ORDER BY c.author_id, c.position",
            (lib.slug,),
        )).fetchall()]
    finally:
        await gdb.close()
    names = {r["id"]: r["name"] for r in await (await lib.db.execute("SELECT id, name FROM authors")).fetchall()}
    # A Goodreads book whose id a row already holds is one the candidate
    # worker calls known and never merges (`_known_index`); re-merged by
    # title it came back as a duplicate of a renamed book ("Drix Alien
    # HalfBreed" beside "Drix Alien Hybrid", same Goodreads id).
    known_ids = {str(r[0]) for r in await (await lib.db.execute(
        "SELECT goodreads_id FROM books WHERE goodreads_id IS NOT NULL AND goodreads_id != ''",
    )).fetchall()}
    for c in cands:
        aid = c.get("seshat_author_id")
        if aid is None or aid not in names or roster.scan_skip_reason(names[aid] or "", aid):
            continue
        if await source_blacklist.is_blacklisted("goodreads", c["author_id"]):
            step["skipped"].append({"library": lib.slug, "id": None, "title": c.get("title") or "",
                                    "detail": "Goodreads author record blacklisted"})
            continue
        if str(c["book_id"]) in known_ids:
            step["skipped"].append({"library": lib.slug, "id": None, "title": c.get("title") or "",
                                    "detail": "a row already holds this Goodreads book's id"})
            continue
        details = goodreads_store.page_details_from_row(await goodreads_store.get_book(c["book_id"]))
        if details is None:
            continue
        book = gc._book_result(c, details)
        result = AuthorResult(name=names[aid])
        if book.series_name:
            result.series = [SeriesResult(name=book.series_name, books=[book])]
        else:
            result.books = [book]
        await merge(aid, names[aid], result, "goodreads")

    # What came back as its own row.
    await lib.load()
    for row in lib.rows.values():
        if row["id"] > first_new:
            step["rows"].append(lib.record(row, detail="created (hidden: %s)" % row["hidden_reason"]
                                           if row["hidden"] else "created"))
            step["count"] += 1


_STEP_FUNCS = (_no_eligible_author, _openlibrary_only, _filtered, _owned_copy, _listed_copy,
               _series_in_title, _cache_remerge)


# ─── Running it ──────────────────────────────────────────────


def _libraries() -> list[dict]:
    from app.discovery import cross_library
    return [l for l in cross_library.libraries_for("all") if l.get("slug")]


async def _run_steps(report: dict) -> None:
    from app.database import get_db as get_pipeline_db
    from app.discovery import roster as roster_mod
    from app.discovery.database import (
        cleanup_empty_series, get_db, recompute_all_series_author_mode, with_active_library,
    )
    settings = load_settings()
    pdb = await get_pipeline_db()
    try:
        for libinfo in _libraries():
            slug = libinfo["slug"]
            _progress.update({"library": slug, "library_name": libinfo.get("name") or slug})
            with with_active_library(slug):
                db = await get_db(slug)
                try:
                    lib = _Library(db, pdb, slug, settings, libinfo.get("content_type") or "ebook")
                    for (key, name, _action), func in zip(STEPS, _STEP_FUNCS):
                        _progress.update({"step": key, "step_name": name})
                        await lib.load()
                        await func(lib, report)
                        await db.commit()
                    await cleanup_empty_series(db)
                    await recompute_all_series_author_mode(db, context="missing-books cleanup")
                    await db.commit()
                except Exception as e:
                    logger.exception("missing cleanup: %s failed in %s", _progress["step"], slug)
                    from app.config import ui_error
                    report["errors"].append(f"{slug} / {_progress['step']}: {ui_error(e)}")
                finally:
                    await db.close()
                roster_mod.invalidate(slug)
    finally:
        await pdb.close()


def _backup(src: Path, dst: Path) -> None:
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


async def preview() -> dict:
    """Run every step on copies of the DBs; nothing real changes."""
    from app import database as pipeline
    from app.discovery import roster as roster_mod
    from app.discovery.database import get_db_path, using_copies

    report = _new_report("preview")
    tmp = Path(tempfile.mkdtemp(prefix="seshat-cleanup-preview-"))
    try:
        paths = {}
        for libinfo in _libraries():
            slug = libinfo["slug"]
            src = get_db_path(slug)
            if src.exists():
                paths[slug] = tmp / src.name
                await asyncio.to_thread(_backup, src, paths[slug])
        pipeline_copy = tmp / "seshat.db"
        await asyncio.to_thread(_backup, Path(pipeline.APP_DB_PATH), pipeline_copy)
        with using_copies(paths), pipeline.using_copy(pipeline_copy):
            await _run_steps(report)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for slug in [l["slug"] for l in _libraries()]:
            roster_mod.invalidate(slug)
    report["finished_at"] = time.time()
    _last["preview"] = report
    _log_summary(report)
    return report


class PreviewNeeded(Exception):
    """Apply refused: no preview with that id from the last hour."""


async def apply(preview_id: str) -> dict:
    """Run every step on the real DBs, after a preview."""
    p = _last["preview"]
    if not p or p["id"] != preview_id or time.time() - (p["finished_at"] or 0) > PREVIEW_TTL_S:
        raise PreviewNeeded("run a preview first (Apply follows a preview from the last hour)")
    if was_applied(preview_id):
        raise PreviewNeeded("that preview was applied: run a new one")
    report = _new_report("apply")
    report["preview_id"] = preview_id
    await _run_steps(report)
    report["finished_at"] = time.time()
    _last["apply"] = report
    _log_summary(report)
    return report


async def run(mode: str, preview_id: Optional[str] = None) -> dict:
    """The background task the routes start: one run at a time."""
    _progress.update({"running": True, "mode": mode, "library": "", "library_name": "", "step": "",
                      "step_name": ""})
    try:
        if mode == "apply":
            return await apply(preview_id or "")
        return await preview()
    finally:
        _progress.update({"running": False, "library": "", "library_name": "", "step": "", "step_name": ""})


def _log_summary(report: dict) -> None:
    logger.info(
        "missing cleanup %s: %s%s", report["mode"],
        ", ".join(f"{s['key']}={s['count']}" + (f" (+{len(s['skipped'])} not)" if s["skipped"] else "")
                  for s in report["steps"]),
        f"; errors: {report['errors']}" if report["errors"] else "",
    )


__all__ = [
    "PREVIEW_TTL_S", "PreviewNeeded", "STEPS", "apply", "blocked_by", "is_running",
    "last_report", "preview", "run", "status", "summary", "was_applied",
]
