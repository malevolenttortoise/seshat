"""
CWA import check (2026-10 audit wave 5a, G121 / G129).

CWA imports a drop on its own and tells Seshat nothing. From 2026-09-29 to
10-09 it deleted every patched ebook unimported (a file mode it couldn't
read) while Seshat recorded each one as delivered. So every CWA drop gets an
`import_checks` row, and this loop looks for the book in Calibre's
metadata.db (read-only):

  * a record added or modified since the drop (2 min slack) whose title or
    one of whose authors matches what was dropped → confirmed; the review's
    kept files are removed;
  * none 15 minutes after the drop (CWA's own ingest timeout) → failed: a
    `pipeline.import_failed` notification, the review → "import failed"
    (Re-drop / Mark as imported on the Review page), the grab back to
    `processing`, the run `failed`.

The reason tells apart a drop still sitting in the ingest folder (CWA isn't
taking files) from one CWA took without a book appearing in Calibre.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app import state
from app.database import get_db
from app.metadata.author_names import normalize_author_name
from app.storage import grabs as grabs_storage
from app.storage import import_checks as checks_storage
from app.storage import pipeline as pipe_storage
from app.storage import review_queue as review_storage

_log = logging.getLogger("seshat.orchestrator.import_check")

FAIL_AFTER = timedelta(minutes=15)
SLACK = timedelta(minutes=2)
STILL_IN_INGEST = (
    "CWA hasn't taken the file: it's still in the ingest folder. Is CWA running?"
)
TAKEN_NOT_IMPORTED = (
    "CWA took the file, but no book appeared in Calibre within 15 minutes"
)


def _utc(stamp: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(stamp).replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _calibre_db_path(library_slug: Optional[str]) -> Optional[str]:
    """metadata.db of the drop's library (or of the first Calibre library)."""
    libraries = [
        lib for lib in (state._discovered_libraries or [])
        if lib.get("source_db_path") and (lib.get("content_type") or "ebook") == "ebook"
    ]
    for lib in libraries:
        if library_slug and lib.get("slug") == library_slug:
            return lib["source_db_path"]
    return libraries[0]["source_db_path"] if libraries else None


def _calibre_books_since(db_path: str, since: str) -> list[tuple[int, str, list[str]]]:
    """(id, title, authors) of every Calibre book added or modified since
    `since` (naive UTC text). Read-only: never holds Calibre's write lock."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, title FROM books WHERE timestamp >= ? OR last_modified >= ?",
            (since, since),
        ).fetchall()
        found = []
        for book_id, title in rows:
            authors = [
                r[0] for r in conn.execute(
                    "SELECT a.name FROM authors a "
                    "JOIN books_authors_link l ON l.author = a.id WHERE l.book = ?",
                    (book_id,),
                )
            ]
            found.append((int(book_id), str(title or ""), authors))
        return found
    finally:
        conn.close()


def _match(check: checks_storage.ImportCheck, books, taken: set[int]) -> Optional[int]:
    """The Calibre id whose title or one of whose authors matches (G129).

    A title match wins over an author-only one, and a record that already
    confirmed another drop can't confirm this one: three books by one
    author dropped 10s apart each need their own record."""
    want_title = _title_key(check.title)
    want_authors = {normalize_author_name(a) for a in check.authors if a}
    want_authors.discard("")
    free = [b for b in books if b[0] not in taken]
    for book_id, title, _authors in free:
        if want_title and _title_key(title) == want_title:
            return book_id
    for book_id, _title, authors in free:
        if want_authors & {normalize_author_name(a) for a in authors}:
            return book_id
    return None


async def tick(*, now: Optional[datetime] = None) -> dict[str, int]:
    """Check every pending drop once. Returns counts for the log/tests."""
    now = now or datetime.now(timezone.utc)
    counts = {"confirmed": 0, "failed": 0, "waiting": 0}
    db = await get_db()
    try:
        pending = await checks_storage.list_pending(db)
        taken = await checks_storage.confirmed_calibre_ids(db) if pending else set()
        for check in pending:
            dropped = _utc(check.dropped_at)
            if dropped is None:
                continue
            db_path = _calibre_db_path(check.library_slug)
            found: Optional[int] = None
            error: Optional[str] = None
            if db_path is None:
                error = "no Calibre library to check against"
            else:
                since = (dropped - SLACK).strftime("%Y-%m-%d %H:%M:%S")
                try:
                    books = await asyncio.get_running_loop().run_in_executor(
                        None, _calibre_books_since, db_path, since,
                    )
                    found = _match(check, books, taken)
                except Exception as e:
                    error = f"couldn't read Calibre's library: {type(e).__name__}: {e}"
            if found is not None:
                taken.add(found)
                await _confirm(db, check, found)
                counts["confirmed"] += 1
            elif now - dropped >= FAIL_AFTER:
                if error:
                    reason = error
                elif Path(check.drop_path).exists():
                    reason = STILL_IN_INGEST
                else:
                    reason = TAKEN_NOT_IMPORTED
                await _fail(db, check, reason)
                counts["failed"] += 1
            else:
                counts["waiting"] += 1
    finally:
        await db.close()
    return counts


async def _confirm(db, check: checks_storage.ImportCheck, calibre_id: int) -> None:
    await checks_storage.resolve(
        db, check.id, checks_storage.STATE_CONFIRMED, calibre_book_id=calibre_id,
    )
    _log.info(
        "import check: %r is in Calibre (id %d) — CWA imported grab_id=%d",
        check.title, calibre_id, check.grab_id,
    )
    if check.review_id is None:
        return
    review = await review_storage.get_entry(db, check.review_id)
    if review is not None and review.status == review_storage.STATUS_DELIVERED:
        shutil.rmtree(review.staged_path, ignore_errors=True)


async def _fail(db, check: checks_storage.ImportCheck, reason: str) -> None:
    await checks_storage.resolve(db, check.id, checks_storage.STATE_FAILED, reason=reason)
    _log.warning(
        "import check: %r (grab_id=%d) never reached Calibre: %s",
        check.title, check.grab_id, reason,
    )
    if check.review_id is not None:
        await review_storage.set_status(
            db, check.review_id, review_storage.STATUS_IMPORT_FAILED,
            decision_note=reason,
        )
        # The review is open again: the grab waits on it like any review.
        await grabs_storage.set_state(db, check.grab_id, grabs_storage.STATE_PROCESSING)
    if check.pipeline_run_id:
        await pipe_storage.set_state(
            db, check.pipeline_run_id, pipe_storage.PIPE_FAILED,
            error=f"import failed: {reason}",
        )
    try:
        from app.notifications import bus, events
        await bus.emit(
            events.PIPELINE_IMPORT_FAILED,
            title="Import failed",
            message=f"{check.title}\n{reason}",
        )
    except Exception:
        _log.exception("pipeline.import_failed bus emit failed (non-fatal)")


async def run_loop(*, interval_seconds: float = 60.0) -> None:
    """Supervised loop: one tick a minute, never raises."""
    _log.info("import check loop starting (interval=%.0fs)", interval_seconds)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("import check tick crashed (non-fatal)")
        await asyncio.sleep(interval_seconds)
