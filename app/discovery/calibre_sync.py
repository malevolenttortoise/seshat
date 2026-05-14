"""
Calibre library sync.

Reads Calibre's `metadata.db` (read-only, via SQLite URI mode) and
upserts the user's library into the discovery database. Runs in three
passes — authors, series, books — so foreign key targets exist before
the rows that reference them get inserted.

Calibre is the user's curated source of truth. Everything this module
imports is flagged `owned=1`, and the merge layer in `lookup.py`
protects calibre-sourced rows with field-level rules so source scans
can enrich metadata without ever clobbering curated content. The
"match external books by title" pass at the end of book upsert is the
inverse: a book the user just added to Calibre may already exist in
the DB as an unowned discovery from a previous source scan, and we
flip its `owned` bit instead of creating a duplicate.

Sync surfaces progress via `state._library_sync_progress` so the
unified Dashboard widget can show "Syncing 142/675 — The Final Empire"
the same way it shows MAM/source scan progress.
"""
import json
import sqlite3
import time
import logging
import os
from pathlib import Path
from app.config import CALIBRE_DB_PATH, CALIBRE_LIBRARY_PATH
from app.discovery.database import get_db, _norm_series_name
from app import state

logger = logging.getLogger("seshat.discovery.calibre_sync")

# Delimiters that won't appear in author/series names — used by helper
# code that needs to flatten multi-author / multi-series fields into
# single strings for logging or grouping.
SEP = "|||"
FIELD_SEP = ":::"

# v2.3 dual-source-of-truth: the user-editable fields that participate
# in auto-flow-vs-review-queue routing on every Calibre sync. Tuples
# are (books_column, calibre_book_dict_key) — they differ for pubdate
# vs books.pub_date.
#
# Structural fields (author_id, series_id, owned, calibre_id, source)
# are NOT in this list — they always write through directly because
# they're identity fields, not metadata-the-user-might-curate.
_DIFFABLE_FIELDS = [
    ("title", "title"),
    ("series_index", "series_index"),
    ("isbn", "isbn"),
    ("cover_path", "cover_path"),
    ("description", "description"),
    ("tags", "tags"),
    ("rating", "rating"),
    ("language", "language"),
    ("publisher", "publisher"),
    ("formats", "formats"),
    ("pub_date", "pubdate"),
]


async def _write_calibre_snapshot(
    db, book_id: int, book: dict, series_name: str | None,
) -> None:
    """INSERT OR REPLACE the books_calibre_snapshot row for this book.

    The snapshot is a frozen reproduction of what Calibre's metadata.db
    says NOW. Always overwrites on every sync — the snapshot belongs
    to Calibre, not the user.

    `series_name` is denormalized (no FK into our series table) because
    the snapshot represents Calibre's POV, independent of how Seshat
    resolves series identity (per-author vs shared). Author info is
    similarly denormalized as a JSON array.
    """
    authors_json = (
        json.dumps([
            {"id": a["id"], "name": a["name"], "sort": a.get("sort")}
            for a in book["authors"]
        ])
        if book.get("authors") else None
    )
    rating = book.get("rating")
    # Calibre stores ratings 0-10 (half-star integer); our `books`
    # column uses REAL. Snapshot mirrors Calibre's int form.
    rating_int = int(round(rating)) if rating is not None else None
    await db.execute("""
        INSERT OR REPLACE INTO books_calibre_snapshot
        (book_id, title, authors_json, series_name, series_index, isbn,
         cover_path, description, tags, rating, language, publisher,
         formats, pubdate, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        book_id, book.get("title"), authors_json, series_name,
        book.get("series_index"), book.get("isbn"),
        book.get("cover_path"), book.get("description"),
        book.get("tags"), rating_int, book.get("language"),
        book.get("publisher"), book.get("formats"), book.get("pubdate"),
        time.time(),
    ))


async def _apply_calibre_diff(db, book_id: int, book: dict) -> tuple[int, int]:
    """Per-field diff between Calibre's incoming values and the
    Seshat-live `books` row, routed by `user_edited_fields`.

    For each diffable field where Calibre's value differs from the
    current books row:
      - If the field IS in `user_edited_fields` → enqueue a row in
        `metadata_review_queue` with source='calibre'. UPSERT on
        `(book_id, field, source)` so a fresh proposal replaces the
        prior pending one rather than piling up.
      - Else → auto-flow: UPDATE the books column directly.

    Returns `(auto_flowed_count, queued_count)`.

    Skips the field entirely when Calibre's value equals the current
    one — the no-op case is the common one on incremental syncs.
    """
    row = await (await db.execute(
        "SELECT title, series_index, isbn, cover_path, description, tags, "
        "rating, language, publisher, formats, pub_date, user_edited_fields "
        "FROM books WHERE id = ?",
        (book_id,),
    )).fetchone()
    if not row:
        return (0, 0)

    try:
        user_edited = set(json.loads(row["user_edited_fields"] or "[]"))
    except (ValueError, TypeError):
        user_edited = set()

    now = time.time()
    auto_flowed = 0
    queued = 0
    for col_name, calibre_key in _DIFFABLE_FIELDS:
        new_val = book.get(calibre_key)
        cur_val = row[col_name]
        if new_val == cur_val:
            continue
        # Cover-path edge: Calibre may emit None on a sync where the
        # cover hasn't been computed yet (rare, but seen). Don't blow
        # away an existing cover with NULL silently — skip the diff.
        if col_name == "cover_path" and new_val is None and cur_val is not None:
            continue
        if col_name in user_edited:
            await db.execute("""
                INSERT OR REPLACE INTO metadata_review_queue
                (book_id, field, old_value, new_value, source, proposed_at)
                VALUES (?, ?, ?, ?, 'calibre', ?)
            """, (
                book_id, col_name,
                None if cur_val is None else str(cur_val),
                None if new_val is None else str(new_val),
                now,
            ))
            queued += 1
        else:
            await db.execute(
                f"UPDATE books SET {col_name} = ? WHERE id = ?",
                (new_val, book_id),
            )
            auto_flowed += 1
            # Cover-pHash sync (Part C). When cover_path lands or
            # changes, recompute the perceptual hash in the same row.
            # This keeps `cover_phash` aligned with the current cover
            # so MAM scans get accurate cover-verification signals
            # without waiting for the next backfill pass.
            if col_name == "cover_path" and new_val:
                try:
                    from app.mam.cover_hash import hash_image_file
                    new_phash = hash_image_file(new_val)
                    if new_phash:
                        await db.execute(
                            "UPDATE books SET cover_phash = ? WHERE id = ?",
                            (new_phash, book_id),
                        )
                except Exception:
                    # Hash failure is non-fatal — falls back to lazy
                    # compute at MAM-scan time. Logged at debug in
                    # `hash_image_file` itself.
                    pass
    return (auto_flowed, queued)


def _read_calibre_db(
    calibre_path: str,
    library_path: str = None,
    *,
    last_modified_threshold: float | None = None,
) -> dict:
    """Read Calibre metadata.db into a list of book dicts.

    Synchronous + read-only, opened via the SQLite URI `mode=ro` flag
    so we never accidentally hold a write lock on the user's Calibre
    database (which Calibre itself wants exclusive write access to).

    The function fans out per-book to fetch authors, series, ISBN,
    tags, rating, languages, publisher, and formats from their
    respective Calibre link tables. This is N+1 by design — Calibre's
    schema makes a single-query alternative awkward, and N is small
    enough (a few thousand rows) that the per-book overhead doesn't
    matter compared to the main async upsert pass that follows.

    `library_path` is the on-disk root of the Calibre library; we use
    it to resolve `cover.jpg` paths so the discovery domain can serve
    covers without re-uploading them.

    `last_modified_threshold` (unix seconds) limits the result to books
    whose Calibre `last_modified` is newer than the threshold. Drives
    incremental sync. Step 0 on Mark's library confirmed that all four
    common mutation paths bump `last_modified`: GUI tag edits, GUI
    cover replacement, external `calibredb set_metadata`, and format
    add/remove via KFX→EPUB conversion.
    """
    lib_path = library_path or CALIBRE_LIBRARY_PATH
    if not Path(calibre_path).exists():
        raise FileNotFoundError(f"Calibre database not found at {calibre_path}")

    conn = sqlite3.connect(f"file:{calibre_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # `julianday()` on both sides normalizes Calibre's mixed
        # `last_modified` formats — observed in Mark's library:
        #   "2026-05-11 15:13:27.887295+00:00"  (most rows)
        #   "2026-05-10 19:57:29.995709"        (some rows, no tz)
        # Naive string comparison breaks across that boundary; numeric
        # Julian Day comparison handles both shapes uniformly.
        if last_modified_threshold is not None:
            books_raw = conn.execute("""
                SELECT
                    b.id as book_id,
                    b.title,
                    b.pubdate,
                    b.series_index,
                    b.path as book_path,
                    COALESCE(c.text, '') as comments
                FROM books b
                LEFT JOIN comments c ON c.book = b.id
                WHERE julianday(b.last_modified) > julianday(?, 'unixepoch')
            """, (last_modified_threshold,)).fetchall()
        else:
            books_raw = conn.execute("""
                SELECT
                    b.id as book_id,
                    b.title,
                    b.pubdate,
                    b.series_index,
                    b.path as book_path,
                    COALESCE(c.text, '') as comments
                FROM books b
                LEFT JOIN comments c ON c.book = b.id
            """).fetchall()

        result = []
        for bk in books_raw:
            book_id = bk["book_id"]

            # Get authors
            authors = conn.execute("""
                SELECT a.id, a.name, a.sort
                FROM books_authors_link bal
                JOIN authors a ON bal.author = a.id
                WHERE bal.book = ?
            """, (book_id,)).fetchall()

            # Get series
            series_list = conn.execute("""
                SELECT s.id, s.name
                FROM books_series_link bsl
                JOIN series s ON bsl.series = s.id
                WHERE bsl.book = ?
            """, (book_id,)).fetchall()

            # Get ISBN
            isbn_row = conn.execute("""
                SELECT val FROM identifiers
                WHERE book = ? AND type IN ('isbn', 'isbn13', 'isbn10')
                LIMIT 1
            """, (book_id,)).fetchone()

            # Get tags
            tags = conn.execute("""
                SELECT t.name FROM books_tags_link btl
                JOIN tags t ON btl.tag = t.id
                WHERE btl.book = ?
            """, (book_id,)).fetchall()

            # Get rating (Calibre stores 0-10, we want 0-5)
            rating_row = conn.execute("""
                SELECT r.rating FROM books_ratings_link brl
                JOIN ratings r ON brl.rating = r.id
                WHERE brl.book = ?
            """, (book_id,)).fetchone()

            # Get languages
            langs = conn.execute("""
                SELECT l.lang_code FROM books_languages_link bll
                JOIN languages l ON bll.lang_code = l.id
                WHERE bll.book = ?
            """, (book_id,)).fetchall()

            # Get publisher
            pub_row = conn.execute("""
                SELECT p.name FROM books_publishers_link bpl
                JOIN publishers p ON bpl.publisher = p.id
                WHERE bpl.book = ?
            """, (book_id,)).fetchone()

            # Get formats
            formats = conn.execute("""
                SELECT format FROM data WHERE book = ?
            """, (book_id,)).fetchall()

            # Build cover path
            cover_path = None
            if bk["book_path"]:
                candidate = os.path.join(lib_path, bk["book_path"], "cover.jpg")
                if os.path.exists(candidate):
                    cover_path = candidate

            # Clean description (strip HTML tags)
            desc = bk["comments"] or ""
            if desc:
                import re as _re
                desc = _re.sub(r'<[^>]+>', '', desc).strip()
                if len(desc) > 1000:
                    desc = desc[:1000]

            result.append({
                "book_id": book_id,
                "title": bk["title"],
                "pubdate": bk["pubdate"],
                "series_index": bk["series_index"],
                "book_path": bk["book_path"],
                "cover_path": cover_path,
                "isbn": isbn_row["val"] if isbn_row else None,
                "authors": [{"id": a["id"], "name": a["name"], "sort": a["sort"]} for a in authors],
                "series": [{"id": s["id"], "name": s["name"]} for s in series_list],
                "tags": ", ".join(t["name"] for t in tags) if tags else None,
                "rating": (rating_row["rating"] / 2.0) if rating_row and rating_row["rating"] else None,
                "description": desc if desc else None,
                "language": langs[0]["lang_code"] if langs else None,
                "publisher": pub_row["name"] if pub_row else None,
                "formats": ", ".join(f["format"] for f in formats) if formats else None,
            })

        return {"books": result}
    finally:
        conn.close()


def _read_calibre_ids(calibre_path: str) -> list[int]:
    """Full-library `book_id` list, no joins. Cheap.

    Incremental sync's `last_modified > threshold` filter misses
    deletes — a book removed from Calibre never appears in that query.
    We diff this list against the discovery DB's `calibre_id` set to
    find the dropouts and prune them.
    """
    if not Path(calibre_path).exists():
        raise FileNotFoundError(f"Calibre database not found at {calibre_path}")
    conn = sqlite3.connect(f"file:{calibre_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id FROM books").fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def _read_calibre_series_authors(calibre_path: str) -> list[dict]:
    """Full-library shallow read of (book_id, authors[id], series[id]).

    Pass 2 of `sync_calibre` decides whether each Calibre series is
    "shared" (2+ contributing authors → one row with author_id=NULL)
    or per-author. That decision MUST see the full Calibre book set
    even when only a filtered subset is being upserted, or else a
    multi-author series whose only-modified book belongs to a single
    author would be misclassified as per-author and re-split.

    Two index-aware joins on the link tables + one `SELECT id FROM
    books`. Books without authors/series still appear (with empty
    lists) so callers can rely on every book_id being represented.

    Result shape mirrors the relevant subset of `_read_calibre_db`'s
    output so Pass 2's loop body works against either input unchanged:
        [{"book_id": int,
          "authors": [{"id": int}, ...],
          "series":  [{"id": int}, ...]}, ...]

    `ORDER BY ... id` on the link queries preserves the same insertion
    order as the per-book queries in `_read_calibre_db` — important
    because `book["authors"][0]` is treated as the primary author by
    downstream passes.
    """
    if not Path(calibre_path).exists():
        raise FileNotFoundError(f"Calibre database not found at {calibre_path}")
    conn = sqlite3.connect(f"file:{calibre_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        all_ids = conn.execute("SELECT id FROM books").fetchall()
        author_rows = conn.execute("""
            SELECT bal.book as book_id, bal.author as author_id
            FROM books_authors_link bal
            ORDER BY bal.book, bal.id
        """).fetchall()
        series_rows = conn.execute("""
            SELECT bsl.book as book_id, bsl.series as series_id
            FROM books_series_link bsl
            ORDER BY bsl.book, bsl.id
        """).fetchall()
    finally:
        conn.close()

    by_book: dict[int, dict] = {
        row["id"]: {"book_id": row["id"], "authors": [], "series": []}
        for row in all_ids
    }
    for row in author_rows:
        entry = by_book.get(row["book_id"])
        if entry is not None:
            entry["authors"].append({"id": row["author_id"]})
    for row in series_rows:
        entry = by_book.get(row["book_id"])
        if entry is not None:
            entry["series"].append({"id": row["series_id"]})
    return list(by_book.values())


async def _heal_legacy_duplicates(db, pipeline_db, slug):
    """End-of-sync sweep across every Calibre row in the discovery DB.

    Catches duplicates the per-UPDATE sweep would miss — typically
    legacy pairs whose title was fixed in Calibre BEFORE v2.10.0
    shipped, where the corresponding UPDATE event already fired
    (without sweeping) and the next sync runs incrementally with
    nothing to UPDATE. The query shape mirrors the per-UPDATE
    sweep and the INSERT-path merge: exact-title match with
    article-stripping for "The X" vs "X", same author, only when
    EXACTLY one unowned non-Calibre candidate exists.

    Idempotent — a re-run after all candidates have been merged
    finds nothing and is a no-op.

    Returns the count of merges performed so the sync's summary
    line + progress dict can surface it.
    """
    from app.discovery.book_merge import merge_books, MergeError

    cal_rows = await (await db.execute(
        "SELECT id, author_id, title FROM books "
        "WHERE source = 'calibre' AND calibre_id IS NOT NULL",
    )).fetchall()
    healed = 0
    for row in cal_rows:
        candidates = await (await db.execute("""
            SELECT id FROM books
            WHERE author_id = ? AND calibre_id IS NULL AND source != 'calibre'
            AND id != ?
            AND (
                LOWER(TRIM(title)) = LOWER(TRIM(?))
                OR REPLACE(LOWER(TRIM(title)), 'the ', '') =
                   REPLACE(LOWER(TRIM(?)), 'the ', '')
            )
        """, (row["author_id"], row["id"], row["title"], row["title"]))
        ).fetchall()
        if len(candidates) != 1:
            continue
        loser_id = candidates[0]["id"]
        try:
            await merge_books(
                db, pipeline_db,
                library_slug=slug,
                winner_id=row["id"],
                loser_id=loser_id,
                reason="calibre_sync_legacy_heal",
            )
        except MergeError as exc:
            logger.warning(
                "calibre_sync: legacy heal skipped winner=%d (%r): %s",
                row["id"], (row["title"] or "")[:60], exc,
            )
            continue
        healed += 1
        logger.info(
            "calibre_sync: legacy heal merged unowned id=%d into "
            "Calibre row id=%d (%r)",
            loser_id, row["id"], (row["title"] or "")[:60],
        )
    return healed


async def _post_update_merge_sweep(
    db, pipeline_db, slug, our_author_id, calibre_title, calibre_row_id,
):
    """If a Calibre-row UPDATE just landed and the title now matches an
    unowned discovery row, fold the discovery row into the Calibre row.

    Runs the same exact-title-match (with article-stripping for
    "The X" vs "X") that the INSERT path uses, so the sweep's behavior
    is symmetric: the Calibre row is the canonical "this book is in
    the library" record, and a single unambiguous discovery match
    gets merged in. Multi-match (2+) or zero-match cases are no-ops —
    the same conservative semantics that prevent the INSERT path
    from merging a book into the wrong row.

    Returns True if a merge happened, False otherwise.
    """
    from app.discovery.book_merge import merge_books, MergeError

    candidates = await (await db.execute("""
        SELECT id FROM books
        WHERE author_id = ? AND calibre_id IS NULL AND source != 'calibre'
        AND id != ?
        AND (
            LOWER(TRIM(title)) = LOWER(TRIM(?))
            OR REPLACE(LOWER(TRIM(title)), 'the ', '') =
               REPLACE(LOWER(TRIM(?)), 'the ', '')
        )
    """, (our_author_id, calibre_row_id, calibre_title, calibre_title))
    ).fetchall()
    if len(candidates) != 1:
        return False
    loser_id = candidates[0]["id"]
    try:
        await merge_books(
            db, pipeline_db,
            library_slug=slug,
            winner_id=calibre_row_id,
            loser_id=loser_id,
            reason="calibre_sync_post_update",
        )
    except MergeError as exc:
        # MergeError here means a precondition the sweep can't
        # satisfy (e.g. both rows owned-calibre, which shouldn't
        # happen given the source != 'calibre' filter above, but
        # belt-and-suspenders). Log and continue rather than crash
        # the whole sync run.
        logger.warning(
            "calibre_sync: post-update merge sweep skipped for "
            f"book_id={calibre_row_id} ('{calibre_title[:60]}'): {exc}"
        )
        return False
    logger.info(
        f"calibre_sync: post-update merged unowned id={loser_id} into "
        f"Calibre row id={calibre_row_id} ('{calibre_title[:60]}')"
    )
    return True


async def sync_calibre(calibre_db_path=None, calibre_library_path=None):
    """Run a Calibre → discovery DB import.

    Three passes, in this exact order (because each pass needs the FK
    targets from the previous one to exist):
      1. Authors — upsert by name, link calibre_id.
      2. Series  — upsert by lowercased name, scoped to author for
                   uniqueness but matched globally so multi-author
                   series collapse to one row.
      3. Books   — upsert by (calibre_id, source='calibre'), then run
                   a "flip ownership" pass that marks pre-existing
                   discovery rows as owned when they title-match the
                   book we just imported.

    Mode is decided by `sync_state.resolve_threshold` against the
    persisted per-slug state. Incremental mode reads only books whose
    Calibre `last_modified` is newer than the last successful sync
    (with a 60s drift bias), supplemented by a cheap shallow full read
    so Pass 2's multi-author series detection still sees every book.
    Full mode runs on first sync, weekly safety-net intervals, and
    after a recorded failure.

    Paths default to `CALIBRE_DB_PATH` / `CALIBRE_LIBRARY_PATH` from
    config when not supplied — used by single-library setups. The
    multi-library code path always passes the discovered library's
    paths explicitly via `library_apps.calibre.CalibreApp.sync`.
    """
    cal_path = calibre_db_path or CALIBRE_DB_PATH
    lib_path = calibre_library_path or CALIBRE_LIBRARY_PATH
    start_time = time.time()
    # Slug is whichever library is active during this call — callers
    # (main.py lifespan, scheduled_jobs, trigger_sync) always set it
    # via `set_active_library` before invoking us. Per-slug progress
    # means Calibre + ABS each keep their own last-sync timestamp and
    # in-flight display, instead of stomping a single shared dict.
    from app.discovery.database import get_active_library
    from app.discovery import sync_state as _sync_state
    from app.config import load_settings as _load_settings
    slug = get_active_library() or "calibre"
    progress = state.get_lib_progress(slug)

    settings = _load_settings()
    threshold, reason = _sync_state.resolve_threshold(
        _sync_state.get_state(settings, slug)
    )
    mode = "full" if threshold is None else "incremental"
    logger.info(
        f"Starting Calibre sync from {cal_path} "
        f"(mode={mode}, reason={reason}"
        + (f", threshold={threshold:.0f}" if threshold is not None else "")
        + ")"
    )

    # Initialize so the except handler below can reference sync_id
    # safely even if the INSERT INTO sync_log itself fails (e.g.,
    # database is locked). Previously the except did
    # `... WHERE id=sync_id` unconditionally and crashed with
    # UnboundLocalError, masking the original exception.
    sync_id: int | None = None
    db = await get_db()
    # v2.10.0 post-UPDATE merge sweep reads/writes `book_grab_links`
    # via the shared `merge_books` helper. Opening the pipeline DB
    # once per sync (rather than once per merge) avoids the overhead
    # of repeatedly opening the global SQLite file on UPDATE-heavy
    # syncs. The connection stays unused if no sweep fires.
    from app.database import get_db as _get_pipeline_db
    pipeline_db = await _get_pipeline_db()
    try:
        cursor = await db.execute(
            "INSERT INTO sync_log (sync_type, started_at) VALUES (?, ?)",
            ("calibre", start_time)
        )
        sync_id = cursor.lastrowid
        await db.commit()

        calibre_data = _read_calibre_db(
            cal_path, lib_path, last_modified_threshold=threshold,
        )
        # Pass 2's multi-author detection needs the full library's
        # (author, series) tuples even when only a filtered subset is
        # being upserted. In full mode the filtered set IS the full
        # set; in incremental we do a cheap shallow read.
        if mode == "incremental":
            shallow_books = _read_calibre_series_authors(cal_path)
        else:
            shallow_books = calibre_data["books"]
        books_found = 0
        books_new = 0
        # Surface progress in the unified scan widget. The initial dict
        # captures the total upfront (right after metadata.db has been
        # read, before any upserts start) so the widget can render a
        # real progress bar instead of an indeterminate spinner.
        progress.update({
            "running": True,
            "current": 0,
            "total": len(calibre_data["books"]),
            "current_book": "",
            "books_new": 0,
            "books_updated": 0,
            "books_pruned": 0,
            "status": "scanning",
            "type": "manual",
        })
        progress.pop("completed_at", None)

        # Pass 1: upsert authors
        # Lookup keys off `normalized_name` instead of `name` so two
        # Calibre author rows with punctuation drift (e.g. "A. K. DuBoff"
        # at calibre_id=254 and "A K DuBoff" at calibre_id=1179) collapse
        # into ONE Seshat row. Calibre's UI hides such duplicates but
        # the underlying metadata.db keeps both; without this collapse
        # we'd recreate them as two Seshat authors every sync.
        #
        # When an existing row matches, `pick_canonical_display_name`
        # picks the more-punctuated variant as the stored display name
        # ("A. K. DuBoff" beats "A K DuBoff") — matches Goodreads'
        # convention and is what source scans expect.
        from app.metadata.author_names import (
            normalize_author_name,
            pick_canonical_display_name,
        )
        # In incremental mode, Pass 2's `cal_series_authors` map must
        # resolve `our_author_id` for authors that may appear only on
        # un-modified books (and therefore aren't processed by Pass 1
        # this sync). Pre-loading the full calibre_id → seshat_id map
        # from the discovery DB covers those — Pass 1 then augments it
        # with any newly-upserted authors. Full mode keeps the empty
        # start so existing re-canonicalization behavior is preserved.
        author_map = {}  # calibre_author_id -> our_id
        if mode == "incremental":
            existing = await (await db.execute(
                "SELECT id, calibre_id FROM authors WHERE calibre_id IS NOT NULL"
            )).fetchall()
            author_map = {row["calibre_id"]: row["id"] for row in existing}
        for book in calibre_data["books"]:
            for author in book["authors"]:
                cal_id = author["id"]
                if cal_id in author_map:
                    continue

                incoming_name = author["name"]
                norm = normalize_author_name(incoming_name)
                row = await (await db.execute(
                    "SELECT id, name FROM authors WHERE normalized_name = ?",
                    (norm,),
                )).fetchone()
                if row:
                    author_map[cal_id] = row["id"]
                    canonical = pick_canonical_display_name(
                        row["name"], incoming_name,
                    )
                    await db.execute(
                        "UPDATE authors SET name = ?, sort_name = ?, "
                        "calibre_id = ?, normalized_name = ? WHERE id = ?",
                        (canonical, author["sort"], cal_id, norm, row["id"]),
                    )
                else:
                    cur = await db.execute(
                        "INSERT INTO authors (name, sort_name, calibre_id, "
                        "normalized_name) VALUES (?, ?, ?, ?)",
                        (incoming_name, author["sort"], cal_id, norm),
                    )
                    author_map[cal_id] = cur.lastrowid
                    # v2.12.1 #2 — dual author-row pattern. Stub this
                    # author into every audiobook library so the
                    # cross-library Scan Audiobooks button can always
                    # run discovery against them. Stub is name-only;
                    # the audiobook lib's row is owned by ABS sync (or
                    # remains empty if the author has no audiobook
                    # presence). Best-effort: failure to mirror logs a
                    # warning but doesn't fail the Calibre sync.
                    try:
                        from app.discovery.author_mirror import (
                            mirror_new_author_to_other_type_libs,
                        )
                        await mirror_new_author_to_other_type_libs(
                            incoming_name,
                            source_content_type="ebook",
                            sort_name=author["sort"],
                            normalized_name=norm,
                        )
                    except Exception:
                        logger.debug(
                            "calibre_sync: stub-mirror failed for '%s' "
                            "(non-fatal)", incoming_name, exc_info=True,
                        )

        # Pass 2: upsert series
        #
        # First, aggregate which Seshat author_ids contribute to each
        # Calibre series id. A single Calibre series with books from
        # 2+ authors is a genuinely shared series (Halo, Star Wars
        # Legends) and gets a single shared row (author_id=NULL).
        # Distinct Calibre series ids that happen to share a name
        # remain per-author (Cressman/Savarovsky "The Last Paladin"
        # case — they have different Calibre ids).
        #
        # `shallow_books` is the full library in incremental mode and
        # the filtered set in full mode — either way it covers every
        # book whose contribution to multi-author detection matters.
        cal_series_authors: dict[int, set[int]] = {}
        for book in shallow_books:
            if not book["series"] or not book["authors"]:
                continue
            primary_cal_id = book["authors"][0]["id"]
            our_author_id = author_map.get(primary_cal_id)
            if not our_author_id:
                continue
            for s in book["series"]:
                cal_series_authors.setdefault(s["id"], set()).add(our_author_id)

        # series_map keys: (calibre_series_id, our_author_id) for
        # per-author series, OR (calibre_series_id, None) for shared
        # series. Using None deliberately so Pass 3's lookup can find
        # the shared row regardless of which author the book belongs
        # to.
        series_map = {}
        for book in calibre_data["books"]:
            if not book["series"] or not book["authors"]:
                continue
            primary_author_cal_id = book["authors"][0]["id"]
            our_author_id = author_map.get(primary_author_cal_id)
            if not our_author_id:
                continue

            for s in book["series"]:
                cal_sid = s["id"]
                contributors = cal_series_authors.get(cal_sid, set())
                is_shared = len(contributors) >= 2

                key = (cal_sid, None if is_shared else our_author_id)
                if key in series_map:
                    continue

                # Lookup target row.
                #
                # Shared (is_shared=True): look for an existing
                # author_id IS NULL row by name; if not found, INSERT
                # with author_id=NULL. We also opportunistically
                # collapse pre-v2.3 per-author rows (legacy split
                # state) into the new shared row by re-pointing books
                # later — done via Pass 3's series_map assignment +
                # an explicit cleanup at the end of Pass 2.
                #
                # Per-author (is_shared=False): identical to the
                # pre-v2.3 author-scoped lookup that prevented the
                # Cressman/Savarovsky merge.
                if is_shared:
                    row = await (await db.execute(
                        "SELECT id FROM series WHERE LOWER(name) = LOWER(?) "
                        "AND author_id IS NULL",
                        (s["name"],)
                    )).fetchone()
                    if row:
                        series_map[key] = row["id"]
                    else:
                        cur = await db.execute(
                            "INSERT INTO series (name, author_id) VALUES (?, NULL)",
                            (s["name"],)
                        )
                        series_map[key] = cur.lastrowid
                else:
                    row = await (await db.execute(
                        "SELECT id FROM series WHERE LOWER(name) = LOWER(?) AND author_id = ?",
                        (s["name"], our_author_id)
                    )).fetchone()
                    if row:
                        series_map[key] = row["id"]
                    else:
                        target_norm = _norm_series_name(s["name"])
                        matched = None
                        if target_norm:
                            author_series = await (await db.execute(
                                "SELECT id, name FROM series WHERE author_id = ?",
                                (our_author_id,),
                            )).fetchall()
                            for ar in author_series:
                                if _norm_series_name(ar["name"]) == target_norm:
                                    matched = ar["id"]
                                    break
                        if matched is not None:
                            series_map[key] = matched
                        else:
                            cur = await db.execute(
                                "INSERT INTO series (name, author_id) VALUES (?, ?)",
                                (s["name"], our_author_id)
                            )
                            series_map[key] = cur.lastrowid

        # Cleanup: any per-author rows that have been superseded by a
        # shared row this sync should be merged into the shared row
        # so books from earlier scans get re-pointed. Delete the
        # per-author rows AFTER re-pointing their books — the FK on
        # books.series_id is RESTRICT-by-default in SQLite without
        # ON DELETE CASCADE on this relationship.
        for cal_sid, contributors in cal_series_authors.items():
            if len(contributors) < 2:
                continue
            shared_id = series_map.get((cal_sid, None))
            if shared_id is None:
                continue
            # Find the (just-upserted) row's name to match by-name on
            # legacy per-author rows. Calibre's series name is the
            # authoritative one here.
            shared_row = await (await db.execute(
                "SELECT name FROM series WHERE id = ?", (shared_id,)
            )).fetchone()
            if not shared_row:
                continue
            shared_name = shared_row["name"]
            # Re-point books on per-author rows with the same
            # (case-insensitive) name to the shared row. Limit the
            # re-pointing to authors who contribute to this Calibre
            # series so we don't sweep up unrelated same-named series
            # (the Cressman/Savarovsky guard).
            placeholders = ",".join("?" * len(contributors))
            old_rows = await (await db.execute(
                f"SELECT id FROM series WHERE LOWER(name) = LOWER(?) "
                f"AND author_id IN ({placeholders}) AND id != ?",
                (shared_name, *contributors, shared_id)
            )).fetchall()
            for old in old_rows:
                await db.execute(
                    "UPDATE books SET series_id = ? WHERE series_id = ?",
                    (shared_id, old["id"])
                )
                await db.execute(
                    "DELETE FROM series WHERE id = ?", (old["id"],)
                )

        # Pass 3: upsert books
        for i, book in enumerate(calibre_data["books"]):
            if not book["authors"]:
                continue
            books_found += 1
            # `current` advances AFTER the no-author skip so the
            # progress bar reflects books actually being processed,
            # not raw iteration count.
            progress["current"] = books_found
            progress["current_book"] = book["title"]

            primary_cal_id = book["authors"][0]["id"]
            our_author_id = author_map.get(primary_cal_id)
            if not our_author_id:
                continue

            our_series_id = None
            series_index = book["series_index"]
            if book["series"]:
                first_series = book["series"][0]
                cal_sid = first_series["id"]
                # Shared series: key uses None for author. Per-author:
                # uses our_author_id. Mirrors Pass 2's keying.
                if len(cal_series_authors.get(cal_sid, set())) >= 2:
                    key = (cal_sid, None)
                else:
                    key = (cal_sid, our_author_id)
                our_series_id = series_map.get(key)

            row = await (await db.execute(
                "SELECT id FROM books WHERE calibre_id = ? AND source = 'calibre'",
                (book["book_id"],)
            )).fetchone()

            # Calibre's series name for the snapshot. Independent of
            # how we resolved Seshat's series_id (per-author vs shared).
            cal_series_name = (
                book["series"][0]["name"] if book["series"] else None
            )

            if row:
                # v2.3: structural fields (identity-tier) are written
                # through directly. User-editable metadata fields go
                # through the auto-flow-vs-review-queue helper which
                # routes per `user_edited_fields`. Snapshot table
                # mirrors Calibre's POV regardless.
                await db.execute(
                    "UPDATE books SET author_id=?, series_id=?, owned=1 "
                    "WHERE id=?",
                    (our_author_id, our_series_id, row["id"]),
                )
                await _apply_calibre_diff(db, row["id"], book)
                await _write_calibre_snapshot(
                    db, row["id"], book, cal_series_name
                )
                progress["books_updated"] += 1
                logger.debug(f"  Calibre: updated '{book['title']}' (calibre_id={book['book_id']}, tags={book['tags']}, rating={book['rating']})")
                # v2.10.0 post-UPDATE merge sweep. When a user edits
                # the title of an existing Calibre book to match
                # what their discovery scan recorded (e.g. fixing
                # "Right of Retribution: Book 2" → "Right of
                # Retribution 2" so it aligns with the Goodreads-
                # source unowned row), the INSERT-path merge query
                # above (line ~806) doesn't run because the Calibre
                # row already exists. Pre-v2.10.0 the duplicate
                # stayed forever even after metadata cleanup. The
                # sweep re-runs the same exact-title-match query
                # against unowned non-calibre rows and folds in the
                # single unambiguous match, if any. Safe by design:
                # multi-match or no-match cases leave both rows
                # alone (same conservative semantics the INSERT
                # path uses).
                merged = await _post_update_merge_sweep(
                    db, pipeline_db, slug,
                    our_author_id, book["title"], row["id"],
                )
                if merged:
                    progress["books_merged_post_update"] = (
                        progress.get("books_merged_post_update", 0) + 1
                    )
            else:
                # Before INSERTing a new Calibre row, look for a matching
                # discovery row (typically a Missing entry created by an
                # earlier source scan + later fulfilled in Calibre via the
                # pipeline). Without this fallback, every fulfilled
                # Missing book ends up duplicated: the original row gets
                # `owned=1` flipped by the safety net below, but stays
                # without a calibre_id, and the new Calibre row sits next
                # to it carrying the series + tags + rating + formats.
                #
                # Match shape mirrors the ownership-flip pass: same author,
                # no calibre_id, title match (with article-stripping for
                # "The X" vs "X"). Only merge if EXACTLY one candidate —
                # ambiguity falls back to INSERT so we never merge a book
                # into the wrong row.
                # Pull `hidden` alongside id so we can log when an
                # auto-unhide fires (see UPDATE below).
                merge_candidates = await (await db.execute("""
                    SELECT id, hidden FROM books
                    WHERE author_id = ? AND calibre_id IS NULL AND source != 'calibre'
                    AND (
                        LOWER(TRIM(title)) = LOWER(TRIM(?))
                        OR REPLACE(LOWER(TRIM(title)), 'the ', '') =
                           REPLACE(LOWER(TRIM(?)), 'the ', '')
                    )
                """, (our_author_id, book["title"], book["title"]))).fetchall()

                if len(merge_candidates) == 1:
                    target_id = merge_candidates[0]["id"]
                    was_hidden = bool(merge_candidates[0]["hidden"])
                    # v2.3 merge path: convert the Missing/source-discovered
                    # row into a Calibre row by setting structural fields
                    # (owned=1, calibre_id, source='calibre', series_id),
                    # then route Calibre's metadata fields through the
                    # diff helper. Pre-existing source-scan values that
                    # the user manually edited are preserved via the
                    # user_edited_fields gate.
                    #
                    # Auto-unhide on merge (added 2026-05-09): "hidden"
                    # on a source-discovered row means "I'm not interested
                    # in getting this book." Once the book actually lands
                    # in Calibre (user's pipeline grabbed it, or they
                    # imported it manually), the relevant question is
                    # "do I want to see this in my library view" — and
                    # the answer is overwhelmingly yes. Mark hit this
                    # 2026-05-09 with five Fantasy World Farm books that
                    # got auto-grabbed via his author allowlist after
                    # he'd hidden the unowned source rows; UI count
                    # showed 5 books missing because they merged owned
                    # but stayed hidden. Scoped narrowly to the merge
                    # path — the existing-Calibre-row UPDATE branch
                    # leaves hidden alone, so a user who explicitly
                    # hides a book they own (duplicate edition, wrong
                    # language, etc.) keeps that hide.
                    await db.execute("""
                        UPDATE books SET author_id=?, series_id=?,
                        owned=1, calibre_id=?, source='calibre', hidden=0
                        WHERE id=?
                    """, (our_author_id, our_series_id,
                          book["book_id"], target_id))
                    await _apply_calibre_diff(db, target_id, book)
                    await _write_calibre_snapshot(
                        db, target_id, book, cal_series_name
                    )
                    progress["books_updated"] += 1
                    unhide_note = ", unhidden" if was_hidden else ""
                    logger.info(
                        f"  Calibre: merged Missing row id={target_id} with "
                        f"new Calibre book_id={book['book_id']} "
                        f"('{book['title']}'{unhide_note})"
                    )
                else:
                    # New Calibre book — INSERT the books row with
                    # Calibre values (no diff routing; nothing to
                    # diff against). Snapshot mirrors. user_edited_fields
                    # defaults to '[]' via the column default.
                    cur = await db.execute("""
                        INSERT INTO books (title, author_id, series_id, series_index,
                        isbn, calibre_id, source, owned, pub_date, cover_path,
                        description, tags, rating, language, publisher, formats)
                        VALUES (?, ?, ?, ?, ?, ?, 'calibre', 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (book["title"], our_author_id, our_series_id,
                          series_index, book["isbn"], book["book_id"],
                          book["pubdate"], book["cover_path"],
                          book["description"], book["tags"], book["rating"],
                          book["language"], book["publisher"], book["formats"]))
                    new_book_id = cur.lastrowid
                    await _write_calibre_snapshot(
                        db, new_book_id, book, cal_series_name
                    )
                    # v2.3.7 acquisition link-back. Symmetric with ABS
                    # sync — if this Calibre book corresponds to a
                    # recent ebook IRC grab, write mam_url + 'found'
                    # directly so the next MAM scan doesn't fuzzy-
                    # search and risk mis-grading the row.
                    try:
                        from app.discovery.acquisition_linkback import link_new_book
                        primary_author = (
                            book["authors"][0].get("name")
                            if book.get("authors") else ""
                        )
                        await link_new_book(
                            db, slug, new_book_id,
                            book["title"], primary_author or "",
                            is_audiobook=False,
                        )
                    except Exception as link_exc:
                        logger.warning(
                            "Calibre sync: acquisition link-back crashed "
                            f"for book_id={new_book_id} "
                            f"({book['title'][:60]!r}): {link_exc}"
                        )
                    books_new += 1
                    progress["books_new"] += 1
                    logger.debug(f"  Calibre: NEW '{book['title']}' by {book['authors'][0]['name']} (tags={book['tags']}, lang={book['language']})")

            # Flip ownership on any pre-existing discovery row that
            # matches this book's title for the same author. The
            # `REPLACE(...,'the ',...)` half handles articles — "The
            # Final Empire" and "Final Empire" should collapse onto
            # the same row. Without this pass, importing a book the
            # user already owned would leave the original discovery
            # row sitting around as an unowned duplicate.
            await db.execute("""
                UPDATE books SET owned = 1
                WHERE author_id = ? AND owned = 0 AND source != 'calibre'
                AND (
                    LOWER(TRIM(title)) = LOWER(TRIM(?))
                    OR REPLACE(LOWER(TRIM(title)), 'the ', '') =
                       REPLACE(LOWER(TRIM(?)), 'the ', '')
                )
            """, (our_author_id, book["title"], book["title"]))

        # Pass 4: reconcile. Books previously imported from Calibre but
        # no longer in metadata.db (user removed them in Calibre, or
        # switched to a different library that reuses calibre_ids) would
        # otherwise linger as ghost `owned=1` rows and inflate library
        # counts. Prune them here. Discovery-only rows (source != 'calibre')
        # are untouched — the user's enrichment state for unowned books
        # is independent of what's in Calibre.
        #
        # Incremental mode needs a full ID-only re-read here because
        # `calibre_data["books"]` only carries books whose last_modified
        # crossed the threshold — using it directly would prune every
        # un-modified row.
        #
        # SAFETY: zero-book current_ids is almost always a transient
        # read error, not a deliberate "I deleted everything" — skip
        # the prune in that case rather than wipe every calibre row.
        if mode == "incremental":
            current_ids = _read_calibre_ids(cal_path)
        else:
            current_ids = [book["book_id"] for book in calibre_data["books"]]
        books_pruned = 0
        if current_ids:
            ph = ",".join("?" * len(current_ids))
            cur = await db.execute(
                f"DELETE FROM books WHERE source='calibre' "
                f"AND calibre_id NOT IN ({ph})",
                current_ids,
            )
            books_pruned = cur.rowcount or 0
            if books_pruned:
                logger.info(
                    f"Calibre sync: pruned {books_pruned} stale row(s) "
                    f"no longer in metadata.db"
                )
        progress["books_pruned"] = books_pruned

        await db.commit()

        # v2.10.1 end-of-sync legacy-duplicate heal pass. The per-UPDATE
        # sweep added in v2.10.0 only fires for books Calibre touched
        # in THIS sync run. In incremental mode (or whenever the user
        # fixed Calibre metadata BEFORE deploying v2.10.0), an
        # outstanding duplicate can survive: the calibre row's title
        # already matches an unowned discovery row, but no UPDATE
        # event fires because nothing in Calibre changed. This pass
        # scans every Calibre row at end-of-sync and re-runs the
        # exact same merge query the per-UPDATE sweep uses. Same
        # conservative semantics — only fires when exactly one
        # unambiguous candidate matches.
        books_healed = await _heal_legacy_duplicates(db, pipeline_db, slug)
        progress["books_merged_legacy_heal"] = books_healed

        await db.execute("""
            UPDATE sync_log SET finished_at=?, status='complete',
            books_found=?, books_new=? WHERE id=?
        """, (time.time(), books_found, books_new, sync_id))
        await db.commit()

        logger.info(
            f"Calibre sync complete ({mode}): {books_found} books, "
            f"{books_new} new, {books_pruned} pruned, "
            f"{books_healed} legacy-duplicates healed"
        )
        progress.update({
            "running": False,
            "current_book": "",
            "status": "complete",
            "completed_at": time.time(),
            "last_check_at": time.time(),
            "sync_mode": mode,
        })
        return {
            "books_found": books_found,
            "books_new": books_new,
            "books_pruned": books_pruned,
            "mode": mode,
        }

    except Exception as e:
        logger.error(f"Calibre sync error: {e}", exc_info=True)
        # Only mark the sync_log row as errored when we actually got
        # one inserted. If the original exception fired before sync_id
        # was assigned, there's no row to update — and trying anyway
        # would mask the real exception with UnboundLocalError.
        if sync_id is not None:
            try:
                await db.execute(
                    "UPDATE sync_log SET finished_at=?, status='error', error=? "
                    "WHERE id=?",
                    (time.time(), str(e), sync_id)
                )
                await db.commit()
            except Exception:
                # Don't let the audit-log write swallow the real
                # error. Worst case the sync_log row stays in
                # 'running' state — visible to the user as a stuck
                # sync, which is preferable to silent loss.
                logger.exception(
                    "Failed to mark sync_log as errored — original "
                    "error stands"
                )
        progress.update({
            "running": False,
            "current_book": "",
            "status": f"error: {e}",
            "completed_at": time.time(),
        })
        raise
    finally:
        await db.close()
        await pipeline_db.close()
