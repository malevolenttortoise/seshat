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


def _read_calibre_db(calibre_path: str, library_path: str = None) -> dict:
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
    """
    lib_path = library_path or CALIBRE_LIBRARY_PATH
    if not Path(calibre_path).exists():
        raise FileNotFoundError(f"Calibre database not found at {calibre_path}")

    conn = sqlite3.connect(f"file:{calibre_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
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


async def sync_calibre(calibre_db_path=None, calibre_library_path=None):
    """Run a full Calibre → discovery DB import.

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

    Paths default to `CALIBRE_DB_PATH` / `CALIBRE_LIBRARY_PATH` from
    config when not supplied — used by single-library setups. The
    multi-library code path always passes the discovered library's
    paths explicitly via `library_apps.calibre.CalibreApp.sync`.
    """
    cal_path = calibre_db_path or CALIBRE_DB_PATH
    lib_path = calibre_library_path or CALIBRE_LIBRARY_PATH
    logger.info(f"Starting Calibre sync from {cal_path}...")
    start_time = time.time()
    # Slug is whichever library is active during this call — callers
    # (main.py lifespan, scheduled_jobs, trigger_sync) always set it
    # via `set_active_library` before invoking us. Per-slug progress
    # means Calibre + ABS each keep their own last-sync timestamp and
    # in-flight display, instead of stomping a single shared dict.
    from app.discovery.database import get_active_library
    slug = get_active_library() or "calibre"
    progress = state.get_lib_progress(slug)

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO sync_log (sync_type, started_at) VALUES (?, ?)",
            ("calibre", start_time)
        )
        sync_id = cursor.lastrowid
        await db.commit()

        calibre_data = _read_calibre_db(cal_path, lib_path)
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
        author_map = {}  # calibre_author_id -> our_id
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

        # Pass 2: upsert series
        series_map = {}  # (calibre_series_id, our_author_id) -> our_id
        for book in calibre_data["books"]:
            if not book["series"] or not book["authors"]:
                continue
            primary_author_cal_id = book["authors"][0]["id"]
            our_author_id = author_map.get(primary_author_cal_id)
            if not our_author_id:
                continue

            for s in book["series"]:
                key = (s["id"], our_author_id)
                if key in series_map:
                    continue

                # Lookup order matters:
                #   1. Exact LOWER(name) match — fast path for the
                #      common case where Calibre's series name already
                #      matches what we have stored.
                #   2. Normalized-name match scoped to THIS author —
                #      catches drift like "The Witcher" vs "Witcher
                #      Series" without the lazy upsert in lookup.py
                #      having to clean up after us. Cross-author hits
                #      are deliberately ignored: two authors who
                #      happen to share a series name are different
                #      physical series.
                row = await (await db.execute(
                    "SELECT id FROM series WHERE LOWER(name) = LOWER(?)",
                    (s["name"],)
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
                key = (first_series["id"], our_author_id)
                our_series_id = series_map.get(key)

            row = await (await db.execute(
                "SELECT id FROM books WHERE calibre_id = ? AND source = 'calibre'",
                (book["book_id"],)
            )).fetchone()

            if row:
                await db.execute("""
                    UPDATE books SET title=?, author_id=?, series_id=?,
                    series_index=?, isbn=?, owned=1, cover_path=?,
                    description=COALESCE(?,description), tags=?, rating=?,
                    language=?, publisher=?, formats=?
                    WHERE id=?
                """, (book["title"], our_author_id, our_series_id,
                      series_index, book["isbn"], book["cover_path"],
                      book["description"], book["tags"], book["rating"],
                      book["language"], book["publisher"], book["formats"],
                      row["id"]))
                progress["books_updated"] += 1
                logger.debug(f"  Calibre: updated '{book['title']}' (calibre_id={book['book_id']}, tags={book['tags']}, rating={book['rating']})")
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
                merge_candidates = await (await db.execute("""
                    SELECT id FROM books
                    WHERE author_id = ? AND calibre_id IS NULL AND source != 'calibre'
                    AND (
                        LOWER(TRIM(title)) = LOWER(TRIM(?))
                        OR REPLACE(LOWER(TRIM(title)), 'the ', '') =
                           REPLACE(LOWER(TRIM(?)), 'the ', '')
                    )
                """, (our_author_id, book["title"], book["title"]))).fetchall()

                if len(merge_candidates) == 1:
                    target_id = merge_candidates[0]["id"]
                    await db.execute("""
                        UPDATE books SET title=?, series_id=?, series_index=?,
                        isbn=?, owned=1, calibre_id=?, source='calibre',
                        cover_path=?,
                        description=COALESCE(?,description), tags=?, rating=?,
                        language=?, publisher=?, formats=?
                        WHERE id=?
                    """, (book["title"], our_series_id, series_index,
                          book["isbn"], book["book_id"], book["cover_path"],
                          book["description"], book["tags"], book["rating"],
                          book["language"], book["publisher"], book["formats"],
                          target_id))
                    progress["books_updated"] += 1
                    logger.info(
                        f"  Calibre: merged Missing row id={target_id} with "
                        f"new Calibre book_id={book['book_id']} ('{book['title']}')"
                    )
                else:
                    await db.execute("""
                        INSERT INTO books (title, author_id, series_id, series_index,
                        isbn, calibre_id, source, owned, pub_date, cover_path,
                        description, tags, rating, language, publisher, formats)
                        VALUES (?, ?, ?, ?, ?, ?, 'calibre', 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (book["title"], our_author_id, our_series_id,
                          series_index, book["isbn"], book["book_id"],
                          book["pubdate"], book["cover_path"],
                          book["description"], book["tags"], book["rating"],
                          book["language"], book["publisher"], book["formats"]))
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
        # SAFETY: zero-book calibre_data is almost always a transient
        # read error, not a deliberate "I deleted everything" — skip
        # the prune in that case rather than wipe every calibre row.
        books_pruned = 0
        if calibre_data["books"]:
            current_ids = [book["book_id"] for book in calibre_data["books"]]
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

        await db.execute("""
            UPDATE sync_log SET finished_at=?, status='complete',
            books_found=?, books_new=? WHERE id=?
        """, (time.time(), books_found, books_new, sync_id))
        await db.commit()

        logger.info(
            f"Calibre sync complete: {books_found} books, "
            f"{books_new} new, {books_pruned} pruned"
        )
        progress.update({
            "running": False,
            "current_book": "",
            "status": "complete",
            "completed_at": time.time(),
        })
        return {
            "books_found": books_found,
            "books_new": books_new,
            "books_pruned": books_pruned,
        }

    except Exception as e:
        logger.error(f"Calibre sync error: {e}", exc_info=True)
        await db.execute(
            "UPDATE sync_log SET finished_at=?, status='error', error=? WHERE id=?",
            (time.time(), str(e), sync_id)
        )
        await db.commit()
        progress.update({
            "running": False,
            "current_book": "",
            "status": f"error: {e}",
            "completed_at": time.time(),
        })
        raise
    finally:
        await db.close()
