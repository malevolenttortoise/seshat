"""
Audiobookshelf library sync.

Pulls the user's ABS library via the REST API and upserts into Seshat's
discovery database. Mirrors the three-pass shape of `calibre_sync.py`
(authors → series → books) so the ordering guarantees and ownership-flip
logic behave identically for audiobook libraries.

Unlike Calibre, ABS carries audiobook-specific metadata: narrator,
duration, abridged flag, ASIN, audio file formats. Those populate
columns that are null in ebook-library DBs and non-null here.

Progress is reported through the same `state._library_sync_progress`
widget as Calibre, so the unified Dashboard "Syncing N/M" display
works the same for both library types.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from app.discovery.database import get_db, _norm_series_name
from app import state

logger = logging.getLogger("seshat.discovery.audiobookshelf_sync")

# v2.3 dual-source-of-truth: user-editable fields routed through
# auto-flow-vs-review-queue on every ABS sync. Mirrors calibre_sync's
# `_DIFFABLE_FIELDS` shape but with audiobook-specific columns
# (narrator, duration_sec, abridged, asin, audio_formats) instead of
# tags/rating/formats. Structural fields (author_id, series_id, owned,
# audiobookshelf_id, source) are not in this list — they always
# write through directly.
_DIFFABLE_ABS_FIELDS = [
    ("title", "title"),
    ("series_index", "series_index"),
    ("isbn", "isbn"),
    ("asin", "asin"),
    ("description", "description"),
    ("language", "language"),
    ("publisher", "publisher"),
    ("pub_date", "pub_date"),
    ("narrator", "narrator"),
    ("duration_sec", "duration_sec"),
    ("abridged", "abridged"),
    ("audio_formats", "audio_formats"),
]


def _normalize_abs_value(field: str, value):
    """Normalize an ABS-emitted value to match its books-column form.

    ABS's `abridged` flag comes through as bool/int/None from the API
    flatten layer. The books column stores it as INTEGER NOT NULL
    DEFAULT 0 (mirrors the existing INSERT's `1 if ... else 0`),
    so we coerce here for the diff comparison to be apples-to-apples.
    """
    if field == "abridged":
        return 1 if value else 0
    return value


async def _write_abs_snapshot(db, book_id: int, book: dict) -> None:
    """INSERT OR REPLACE the books_abs_snapshot row for this book.

    Like the Calibre snapshot helper, the ABS snapshot is a frozen
    reproduction of what ABS says NOW. Always overwrites on every
    sync. authors_json is denormalized (no FK into our authors table)
    because the snapshot represents ABS's POV.
    """
    authors_json = (
        json.dumps([{"id": None, "name": a} for a in book.get("authors") or []])
        if book.get("authors") else None
    )
    await db.execute("""
        INSERT OR REPLACE INTO books_abs_snapshot
        (book_id, title, authors_json, series_name, series_index,
         narrator, duration_sec, abridged, asin, description, tags,
         cover_path, language, publisher, audio_formats, pubdate, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        book_id, book.get("title"), authors_json,
        book.get("series_name"), book.get("series_index"),
        book.get("narrator"), book.get("duration_sec"),
        _normalize_abs_value("abridged", book.get("abridged")),
        book.get("asin"), book.get("description"),
        None,  # tags — ABS doesn't currently expose tags via flatten
        None,  # cover_path — covers fetched via API on demand
        book.get("language"), book.get("publisher"),
        book.get("audio_formats"), book.get("pub_date"),
        time.time(),
    ))


async def _apply_abs_diff(db, book_id: int, book: dict) -> tuple[int, int]:
    """Per-field diff between ABS's incoming values and the Seshat-live
    books row, routed by `user_edited_fields`. Mirrors
    `calibre_sync._apply_calibre_diff` semantics; see that helper for
    the full rule. Returns `(auto_flowed_count, queued_count)`.
    """
    row = await (await db.execute(
        "SELECT title, series_index, isbn, asin, description, language, "
        "publisher, pub_date, narrator, duration_sec, abridged, "
        "audio_formats, user_edited_fields "
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
    for col_name, abs_key in _DIFFABLE_ABS_FIELDS:
        new_val = _normalize_abs_value(col_name, book.get(abs_key))
        cur_val = row[col_name]
        if new_val == cur_val:
            continue
        if col_name in user_edited:
            await db.execute("""
                INSERT OR REPLACE INTO metadata_review_queue
                (book_id, field, old_value, new_value, source, proposed_at)
                VALUES (?, ?, ?, ?, 'abs', ?)
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
    return (auto_flowed, queued)


async def sync_audiobookshelf(library: dict) -> dict:
    """Run a full ABS → Seshat discovery DB import for one library.

    `library` is the dict returned by `AudiobookshelfApp.discover()` —
    carries `abs_base_url`, `abs_library_id`, plus the standard
    `slug`/`name`/etc. fields.
    """
    from app.library_apps.audiobookshelf import (
        AudiobookshelfClient,
        _get_abs_api_key,
    )

    abs_url = library.get("abs_base_url", "")
    abs_library_id = library.get("abs_library_id", "")
    slug = library.get("slug", "")
    progress = state.get_lib_progress(slug) if slug else {}
    if not abs_url or not abs_library_id:
        raise ValueError(
            f"ABS sync requires abs_base_url and abs_library_id; got library={library}"
        )

    api_key = await _get_abs_api_key()
    if not api_key:
        raise RuntimeError("ABS sync called but no abs_api_key is configured")

    client = AudiobookshelfClient(abs_url, api_key)
    logger.info(f"Starting ABS sync from {abs_url} library={abs_library_id}")
    start_time = time.time()

    db = await get_db()
    sync_id = None
    try:
        cursor = await db.execute(
            "INSERT INTO sync_log (sync_type, started_at) VALUES (?, ?)",
            ("audiobookshelf", start_time),
        )
        sync_id = cursor.lastrowid
        await db.commit()

        # ── Pull the entire library into memory ────────────────
        # The dataset is small (user has ~22 audiobooks; expected
        # ceiling is low thousands even for heavy users). In-memory
        # keeps the three-pass shape simple — no back-and-forth
        # between pagination and upsert.
        items: list[dict] = []
        async for item in client.iter_all_items(abs_library_id):
            items.append(item)
        books = [_flatten_item(it) for it in items]
        books = [b for b in books if b is not None]

        progress.update({
            "running": True,
            "current": 0,
            "total": len(books),
            "current_book": "",
            "books_new": 0,
            "books_updated": 0,
            "books_pruned": 0,
            "status": "scanning",
            "type": "manual",
        })
        progress.pop("completed_at", None)

        # ── Pass 1: authors ────────────────────────────────────
        # ABS gives a single comma-joined `authorName` string. We
        # split on ", " to match Calibre's multi-author handling;
        # collaborative audiobooks ("King & Straub") stay together
        # as one author because ABS joins with " & " in that case.
        author_id_map: dict[str, int] = {}
        for book in books:
            for author_name in book["authors"]:
                if author_name in author_id_map:
                    continue
                row = await (await db.execute(
                    "SELECT id FROM authors WHERE name = ?", (author_name,)
                )).fetchone()
                if row:
                    author_id_map[author_name] = row["id"]
                    # Stamp the ABS marker so cross-library linking
                    # (Phase 2) can match on it. Null-preserving: if
                    # we already have a Calibre-sourced author with
                    # the same name, we only add the ABS id, never
                    # overwrite their calibre_id.
                    await db.execute(
                        "UPDATE authors SET audiobookshelf_id = COALESCE(audiobookshelf_id, ?) "
                        "WHERE id = ?",
                        (abs_library_id, row["id"]),
                    )
                else:
                    sort_name = _sort_name(author_name)
                    cur = await db.execute(
                        "INSERT INTO authors (name, sort_name, audiobookshelf_id) "
                        "VALUES (?, ?, ?)",
                        (author_name, sort_name, abs_library_id),
                    )
                    author_id_map[author_name] = cur.lastrowid

        # ── Pass 2: series ─────────────────────────────────────
        series_id_map: dict[tuple[str, int], int] = {}
        for book in books:
            if not book["series_name"] or not book["authors"]:
                continue
            primary_author_id = author_id_map.get(book["authors"][0])
            if not primary_author_id:
                continue
            key = (book["series_name"].lower(), primary_author_id)
            if key in series_id_map:
                continue

            row = await (await db.execute(
                "SELECT id FROM series WHERE LOWER(name) = LOWER(?)",
                (book["series_name"],),
            )).fetchone()
            if row:
                series_id_map[key] = row["id"]
            else:
                target_norm = _norm_series_name(book["series_name"])
                matched = None
                if target_norm:
                    author_series = await (await db.execute(
                        "SELECT id, name FROM series WHERE author_id = ?",
                        (primary_author_id,),
                    )).fetchall()
                    for ar in author_series:
                        if _norm_series_name(ar["name"]) == target_norm:
                            matched = ar["id"]
                            break
                if matched is not None:
                    series_id_map[key] = matched
                else:
                    cur = await db.execute(
                        "INSERT INTO series (name, author_id, audiobookshelf_id) "
                        "VALUES (?, ?, ?)",
                        (book["series_name"], primary_author_id, abs_library_id),
                    )
                    series_id_map[key] = cur.lastrowid

        # ── Pass 3: books ──────────────────────────────────────
        books_found = 0
        books_new = 0
        current_abs_ids: list[str] = []
        for book in books:
            if not book["authors"]:
                continue
            books_found += 1
            progress["current"] = books_found
            progress["current_book"] = book["title"]

            our_author_id = author_id_map.get(book["authors"][0])
            if not our_author_id:
                continue
            our_series_id = None
            if book["series_name"]:
                our_series_id = series_id_map.get(
                    (book["series_name"].lower(), our_author_id)
                )
            current_abs_ids.append(book["abs_id"])

            existing = await (await db.execute(
                "SELECT id FROM books WHERE audiobookshelf_id = ? AND source = 'audiobookshelf'",
                (book["abs_id"],),
            )).fetchone()

            if existing:
                # v2.3: structural fields write through directly;
                # user-editable metadata routes per `user_edited_fields`.
                # Snapshot mirrors ABS's POV regardless.
                await db.execute(
                    "UPDATE books SET author_id=?, series_id=?, owned=1 "
                    "WHERE id=?",
                    (our_author_id, our_series_id, existing["id"]),
                )
                await _apply_abs_diff(db, existing["id"], book)
                await _write_abs_snapshot(db, existing["id"], book)
                progress["books_updated"] += 1
            else:
                cur = await db.execute(
                    """
                    INSERT INTO books (
                        title, author_id, series_id, series_index,
                        isbn, asin, audiobookshelf_id, source, owned,
                        pub_date, description, language, publisher,
                        narrator, duration_sec, abridged, audio_formats
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'audiobookshelf', 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        book["title"], our_author_id, our_series_id, book["series_index"],
                        book["isbn"], book["asin"], book["abs_id"],
                        book["pub_date"], book["description"],
                        book["language"], book["publisher"],
                        book["narrator"], book["duration_sec"],
                        1 if book["abridged"] else 0,
                        book["audio_formats"],
                    ),
                )
                await _write_abs_snapshot(db, cur.lastrowid, book)
                books_new += 1
                progress["books_new"] += 1

        # ── Pass 4: reconcile ──────────────────────────────────
        # Prune audiobookshelf-sourced rows that no longer exist in ABS
        # (user deleted the file, renamed the folder, etc.). Same safety
        # net as Calibre: zero-book payloads are treated as transient
        # read errors and skip the prune.
        books_pruned = 0
        if current_abs_ids:
            ph = ",".join("?" * len(current_abs_ids))
            cur = await db.execute(
                f"DELETE FROM books WHERE source='audiobookshelf' "
                f"AND audiobookshelf_id NOT IN ({ph})",
                current_abs_ids,
            )
            books_pruned = cur.rowcount or 0
            if books_pruned:
                logger.info(
                    f"ABS sync: pruned {books_pruned} stale row(s) "
                    f"no longer in ABS"
                )
        progress["books_pruned"] = books_pruned

        await db.commit()
        await db.execute(
            "UPDATE sync_log SET finished_at=?, status='complete', "
            "books_found=?, books_new=? WHERE id=?",
            (time.time(), books_found, books_new, sync_id),
        )
        await db.commit()

        logger.info(
            f"ABS sync complete: {books_found} books, "
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
        logger.error(f"ABS sync error: {e}", exc_info=True)
        if sync_id is not None:
            await db.execute(
                "UPDATE sync_log SET finished_at=?, status='error', error=? WHERE id=?",
                (time.time(), str(e), sync_id),
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


# ─── Item flattening ────────────────────────────────────────────

def _flatten_item(item: dict) -> Optional[dict]:
    """Project an ABS library item into the shape the upsert loop wants.

    Returns None if the item is missing essentials (no title or no
    author). ABS occasionally emits items for in-progress scans that
    lack metadata; the upsert loop treats None as "skip".
    """
    media = item.get("media") or {}
    meta = media.get("metadata") or {}
    title = (meta.get("title") or "").strip()
    if not title:
        return None

    author_str = (meta.get("authorName") or "").strip()
    if not author_str:
        return None
    # ABS joins multiple authors with ", ". "A & B" is ABS's shorthand
    # for a genuinely collaborative pair that should stay together.
    authors = [a.strip() for a in author_str.split(", ") if a.strip()]

    narrator_str = (meta.get("narratorName") or "").strip()
    narrator = narrator_str if narrator_str else None

    series_name = (meta.get("seriesName") or "").strip() or None
    # ABS stores series index inline in `seriesName` ("Halo #7") or as
    # a separate field; we pull the numeric tail where present and
    # leave the name clean. Handled conservatively — if parsing fails,
    # the book still syncs without an index rather than blocking.
    series_index = _parse_series_index(series_name)
    if series_name and series_index is not None:
        series_name = _strip_series_index(series_name)

    # Published date — ABS gives `publishedYear` as string or
    # `publishedDate` as ISO date. Prefer the full date when present.
    pub_date = meta.get("publishedDate") or meta.get("publishedYear") or None
    if pub_date and not isinstance(pub_date, str):
        pub_date = str(pub_date)

    # Description — ABS wraps in "Publisher's Summary" prefix sometimes.
    # Keep the text but trim aggressive length the same way calibre_sync
    # does (1000 char cap) so the DB column stays bounded.
    description = (meta.get("description") or "").strip() or None
    if description and len(description) > 1000:
        description = description[:1000]

    duration_sec = media.get("duration")
    try:
        duration_sec = float(duration_sec) if duration_sec is not None else None
    except (TypeError, ValueError):
        duration_sec = None

    # `audio_formats` is derived from the item's track file extensions.
    # ABS doesn't give us per-file extensions on the list endpoint; we
    # leave it null on discovery sync and let the metadata-scrape pass
    # (Phase 2) fill it in from the actual files if needed. For now we
    # use a coarse signal: numAudioFiles > 0 means at least one audio
    # file, so we tag the format as unknown-audiobook.
    num_audio = media.get("numAudioFiles") or 0
    audio_formats = "audiobook" if num_audio else None

    # ASIN / ISBN: ABS sometimes mirrors one into the other. If
    # ASIN is set, the ISBN field frequently holds the same value
    # — keep both separate on the row so downstream can tell the
    # difference when a lookup hits Audible (ASIN) vs Goodreads
    # (ISBN).
    asin = (meta.get("asin") or "").strip() or None
    isbn = (meta.get("isbn") or "").strip() or None
    if isbn and asin and isbn == asin:
        # ABS falls back to ASIN for `isbn` when no real ISBN exists.
        # Null the ISBN so downstream ISBN-matching queries don't
        # pick up ASINs by mistake.
        isbn = None

    return {
        "abs_id": item.get("id"),
        "title": title,
        "authors": authors,
        "narrator": narrator,
        "series_name": series_name,
        "series_index": series_index,
        "pub_date": pub_date,
        "description": description,
        "language": (meta.get("language") or None),
        "publisher": (meta.get("publisher") or None),
        "isbn": isbn,
        "asin": asin,
        "abridged": bool(meta.get("abridged")),
        "duration_sec": duration_sec,
        "audio_formats": audio_formats,
    }


def _parse_series_index(name: Optional[str]) -> Optional[float]:
    """Pull a trailing '#N' or '#N.5' from an ABS series name."""
    if not name:
        return None
    import re as _re
    m = _re.search(r"#\s*(\d+(?:\.\d+)?)\s*$", name)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _strip_series_index(name: str) -> str:
    """Remove a trailing '#N' from a series name."""
    import re as _re
    return _re.sub(r"\s*#\s*\d+(?:\.\d+)?\s*$", "", name).strip()


def _sort_name(name: str) -> str:
    """Compute a `sort_name` for an author (Last, First).

    ABS gives us `authorNameLF` but only for items, not the author
    list — so we roll our own simple transform. Single-word names
    pass through unchanged.
    """
    parts = name.strip().split()
    if len(parts) < 2:
        return name
    return f"{parts[-1]}, {' '.join(parts[:-1])}"
