"""
v2.3.4 Metadata Manager backend.

Surfaces the dual-storage data model (`books`, `books_calibre_snapshot`,
`books_abs_snapshot`, `metadata_review_queue`) to two front-end pieces:

  - **Compare panel** (book sidebar) — `/books/{bid}/compare` returns
    Seshat-live + Calibre snapshot + ABS snapshot side-by-side, with
    per-field diff flags for UI highlighting. `/books/{bid}/pull`
    copies one or more snapshot fields into Seshat-live and flags
    them as user-edited so the next sync's auto-flow doesn't roll
    the change back.

  - **Metadata Manager page** (top-level) — `/queue` lists pending
    review-queue rows grouped by source, `/queue/{id}/apply` writes
    `new_value` to the books table and deletes the queue row,
    `/queue/{id}/dismiss` deletes the row without writing.

The legacy `series-suggestions` table stays — `/queue/series-moves`
exposes it under the same review-queue mental model so the
Suggestions page can retire (its functionality folds into the
Metadata Manager's "Series moves" tab).
"""
import json
import logging
from fastapi import APIRouter, Body, HTTPException, Query

from app.discovery.database import get_db

logger = logging.getLogger("seshat.discovery")

router = APIRouter(prefix="/api/discovery", tags=["metadata"])


# ── Field map: (books column, calibre snapshot column, abs snapshot column, label) ──
#
# Determines which fields the Compare panel surfaces. Order in this
# list = render order in the UI. Calibre's `pubdate` and ABS's
# `pubdate` both map to `pub_date` on the books table; the snapshot
# tables kept Calibre's column name for the snapshot to mirror its
# source schema verbatim.
COMPARE_FIELDS: list[tuple[str, str | None, str | None, str]] = [
    # books_col,        calibre_col,    abs_col,        label
    ("title",           "title",        "title",        "Title"),
    ("description",     "description",  "description",  "Description"),
    ("pub_date",        "pubdate",      "pubdate",      "Publication date"),
    ("isbn",            "isbn",         None,           "ISBN"),
    ("series_index",    "series_index", "series_index", "Series #"),
    ("tags",            "tags",         "tags",         "Tags"),
    ("language",        "language",     "language",     "Language"),
    ("publisher",       "publisher",    "publisher",    "Publisher"),
    ("cover_path",      "cover_path",   "cover_path",   "Cover path"),
    ("rating",          "rating",       None,           "Rating"),
    ("formats",         "formats",      None,           "Formats"),
    ("narrator",        None,           "narrator",     "Narrator"),
    ("duration_sec",    None,           "duration_sec", "Duration (s)"),
    ("abridged",        None,           "abridged",     "Abridged"),
    ("asin",            None,           "asin",         "ASIN"),
    ("audio_formats",   None,           "audio_formats", "Audio formats"),
]


def _parse_user_edited(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


@router.get("/books/{bid}/compare")
async def book_compare(bid: int, slug: str | None = Query(None)):
    """Return Seshat-live + Calibre snapshot + ABS snapshot for one
    book, side-by-side. Per-field `calibre_diff` / `abs_diff` flags
    let the UI highlight cells that differ from Seshat-live.

    Snapshot rows may be missing (book never came from Calibre / ABS)
    — those columns return null and `*_diff` is false everywhere.

    `slug` query param routes the read to a specific library —
    snapshots are per-library so passing the book's library_slug
    avoids reading a different library's row that happens to share
    the numeric id (see books.update_book for the same rationale).
    """
    db = await get_db(slug)
    try:
        book_row = await (await db.execute(
            "SELECT * FROM books WHERE id = ?", (bid,),
        )).fetchone()
        if not book_row:
            raise HTTPException(404, f"book {bid} not found")
        book = dict(book_row)
        # Resolve Seshat-live series name via the series table — the
        # books column is series_id (FK), but the snapshot tables
        # store series_name as text. The Compare panel surfaces the
        # name so the user can pull a Calibre/ABS series back to
        # Seshat-live (Mark's UAT 2026-05-07: post-recovery he had
        # to re-attach the series manually because Compare didn't
        # show it).
        seshat_series_name: str | None = None
        if book.get("series_id"):
            srow = await (await db.execute(
                "SELECT name FROM series WHERE id = ?", (book["series_id"],),
            )).fetchone()
            seshat_series_name = srow["name"] if srow else None
        cal_row = await (await db.execute(
            "SELECT * FROM books_calibre_snapshot WHERE book_id = ?", (bid,),
        )).fetchone()
        abs_row = await (await db.execute(
            "SELECT * FROM books_abs_snapshot WHERE book_id = ?", (bid,),
        )).fetchone()
        cal = dict(cal_row) if cal_row else None
        abs_ = dict(abs_row) if abs_row else None

        user_edited = _parse_user_edited(book.get("user_edited_fields"))

        fields_out: list[dict] = []
        for books_col, cal_col, abs_col, label in COMPARE_FIELDS:
            seshat_v = book.get(books_col)
            cal_v = cal.get(cal_col) if (cal and cal_col) else None
            abs_v = abs_.get(abs_col) if (abs_ and abs_col) else None
            # Skip rows where every value is empty — saves the UI
            # from rendering empty rows for ABS-only fields on
            # ebook-only books, etc.
            if seshat_v in (None, "") and cal_v in (None, "") and abs_v in (None, ""):
                continue
            fields_out.append({
                "field": books_col,
                "label": label,
                "seshat": seshat_v,
                "calibre": cal_v,
                "abs": abs_v,
                "calibre_diff": cal_col is not None
                    and cal is not None
                    and cal_v != seshat_v,
                "abs_diff": abs_col is not None
                    and abs_ is not None
                    and abs_v != seshat_v,
                "user_edited": books_col in user_edited,
            })

        # v2.3.4.4: synthetic Series row — books table has series_id,
        # snapshots have series_name. Compare displays the resolved
        # name; pull resolves snapshot's name → series_id via
        # find-or-create. Inserted right after the Series # row so
        # the two related fields render together in the UI.
        cal_series = cal.get("series_name") if cal else None
        abs_series = abs_.get("series_name") if abs_ else None
        if not (
            seshat_series_name in (None, "")
            and cal_series in (None, "")
            and abs_series in (None, "")
        ):
            series_field = {
                "field": "series_name",
                "label": "Series",
                "seshat": seshat_series_name,
                "calibre": cal_series,
                "abs": abs_series,
                "calibre_diff": cal is not None
                    and cal_series != seshat_series_name,
                "abs_diff": abs_ is not None
                    and abs_series != seshat_series_name,
                "user_edited": "series_name" in user_edited,
            }
            # Insert just before series_index for a logical UI order.
            inserted = False
            for i, f in enumerate(fields_out):
                if f["field"] == "series_index":
                    fields_out.insert(i, series_field)
                    inserted = True
                    break
            if not inserted:
                fields_out.append(series_field)

        return {
            "book_id": bid,
            "user_edited_fields": user_edited,
            "calibre_synced_at": cal.get("synced_at") if cal else None,
            "abs_synced_at": abs_.get("synced_at") if abs_ else None,
            "fields": fields_out,
        }
    finally:
        await db.close()


@router.post("/books/{bid}/pull")
async def book_pull(bid: int, payload: dict = Body(...), slug: str | None = Query(None)):
    """Pull one or more snapshot fields into Seshat-live.

    Request body (one of):
      {"source": "calibre"|"abs", "fields": ["description", ...]}
      {"source": "calibre"|"abs", "all_user_edited": true}

    The bulk variant iterates the book's current `user_edited_fields`
    (filtered to fields the source actually provides) and pulls each.

    Each named field is copied from the snapshot to the corresponding
    books column. Field names use the BOOKS column name (which is
    what the Compare endpoint exposes), not the snapshot column —
    the mapping happens here.

    **v2.3.5 — pull-clears semantics.** Pulled fields are *removed*
    from `books.user_edited_fields` because both DBs now agree on the
    value. The user's edit divergence is resolved; future upstream
    changes auto-flow on next sync (no review queue). The user
    re-enters watched state by editing the field again in the
    sidebar (PUT /books/{bid} re-adds to user_edited_fields on
    diff-vs-stored).

    400 if source is invalid or the body is malformed; 404 if the
    snapshot doesn't exist.
    """
    source = payload.get("source")
    if source not in ("calibre", "abs"):
        raise HTTPException(400, "source must be 'calibre' or 'abs'")

    snapshot_table = (
        "books_calibre_snapshot" if source == "calibre"
        else "books_abs_snapshot"
    )
    if source == "calibre":
        col_map = {b: c for b, c, _, _ in COMPARE_FIELDS if c is not None}
    else:
        col_map = {b: a for b, _, a, _ in COMPARE_FIELDS if a is not None}

    db = await get_db(slug)
    try:
        b_row = await (await db.execute(
            "SELECT id, "
            "(SELECT author_id FROM book_authors WHERE book_id=books.id AND position=0) AS author_id, "
            "user_edited_fields FROM books WHERE id = ?",
            (bid,),
        )).fetchone()
        if not b_row:
            raise HTTPException(404, f"book {bid} not found")
        snap_row = await (await db.execute(
            f"SELECT * FROM {snapshot_table} WHERE book_id = ?", (bid,),
        )).fetchone()
        if not snap_row:
            raise HTTPException(
                404, f"no {source} snapshot for book {bid}",
            )
        snap = dict(snap_row)

        existing_uef = _parse_user_edited(b_row["user_edited_fields"])
        fields = _resolve_fields(payload, existing_uef, col_map)
        if not fields:
            return {
                "book_id": bid, "source": source,
                "applied": [], "user_edited_fields": existing_uef,
            }

        sets = []
        vals: list = []
        applied: list[str] = []
        for f in fields:
            if f == "series_name":
                snap_name = (snap.get("series_name") or "").strip()
                if not snap_name:
                    sets.append("series_id=?")
                    vals.append(None)
                    applied.append(f)
                    continue
                aid = b_row["author_id"]
                srow = await (await db.execute(
                    "SELECT id FROM series WHERE LOWER(name) = LOWER(?) "
                    "AND author_id = ?", (snap_name, aid),
                )).fetchone()
                if srow:
                    sid = srow["id"]
                else:
                    cur = await db.execute(
                        "INSERT INTO series (name, author_id) VALUES (?, ?)",
                        (snap_name, aid),
                    )
                    sid = cur.lastrowid
                sets.append("series_id=?")
                vals.append(sid)
                applied.append(f)
                continue
            if f not in col_map:
                raise HTTPException(
                    400, f"field '{f}' not pullable from {source}",
                )
            snap_col = col_map[f]
            sets.append(f"{f}=?")
            vals.append(snap.get(snap_col))
            applied.append(f)

        # v2.3.5 pull-clears: remove applied fields from
        # user_edited_fields. Both DBs now agree → no edit divergence
        # to flag. Future upstream changes auto-flow.
        cleared_uef = sorted(set(existing_uef) - set(applied))
        if set(cleared_uef) != set(existing_uef):
            sets.append("user_edited_fields=?")
            vals.append(json.dumps(cleared_uef))

        vals.append(bid)
        await db.execute(
            f"UPDATE books SET {', '.join(sets)} WHERE id = ?", vals,
        )
        await db.commit()

        return {
            "book_id": bid,
            "source": source,
            "applied": applied,
            "user_edited_fields": cleared_uef,
        }
    finally:
        await db.close()


def _resolve_fields(
    payload: dict, existing_uef: list[str],
    col_map: dict[str, str],
) -> list[str]:
    """Common payload normalizer for pull/push endpoints.

    `{fields: [...]}` returns the explicit list (validated non-empty).
    `{all_user_edited: true}` returns the intersection of
    `existing_uef` with fields the source can write/read — series_name
    is allowed for both directions even though it's not in col_map.
    Returns [] when nothing applies (e.g. bulk + empty UEF).
    """
    bulk = bool(payload.get("all_user_edited"))
    if bulk:
        allowed = set(col_map.keys()) | {"series_name"}
        return sorted(set(existing_uef) & allowed)
    fields = payload.get("fields") or []
    if not isinstance(fields, list) or not fields:
        raise HTTPException(
            400, "body must include 'fields' (non-empty list) "
            "or 'all_user_edited: true'",
        )
    return list(fields)


@router.post("/books/{bid}/push")
async def book_push(bid: int, payload: dict = Body(...), slug: str | None = Query(None)):
    """Push one or more Seshat-live fields upstream to Calibre or ABS.

    Request body (one of):
      {"source": "calibre"|"abs", "fields": ["title", ...]}
      {"source": "calibre"|"abs", "all_user_edited": true}

    The bulk variant iterates the book's current `user_edited_fields`
    and pushes each one. Both forms clear the successful fields from
    `user_edited_fields` on success — both DBs now agree, so there's
    no edit divergence to keep flagging.

    Routing:
      - source='abs'     → push_abs (PATCH /api/items/{id}/media)
      - source='calibre' → push_calibre_full if calibredb is on PATH;
                           else push_cwa if CWA is configured;
                           else 409 with a "configure CWA" prompt.

    409 — push target not configured / not present in this image.
    400 — malformed payload (missing source, etc.).
    404 — book not found.
    502 — upstream rejected the push (calibredb non-zero, ABS 4xx,
          CWA login failure, etc.).
    """
    from app.discovery.push_back import (
        PushFailed, PushUnavailable,
        push_abs, push_calibre_full, push_cwa,
    )

    source = payload.get("source")
    if source not in ("calibre", "abs"):
        raise HTTPException(400, "source must be 'calibre' or 'abs'")

    # Push field map mirrors pull's col_map per source. series_name is
    # explicitly allowed for both pull and push.
    if source == "calibre":
        col_map = {b: c for b, c, _, _ in COMPARE_FIELDS if c is not None}
    else:
        col_map = {b: a for b, _, a, _ in COMPARE_FIELDS if a is not None}

    db = await get_db(slug)
    try:
        # Read enough columns for either source to build a payload.
        # `series_name` resolved via JOIN so the helper can format
        # ABS's series array / Calibre's --field series:NAME.
        b_row = await (await db.execute("""
            SELECT b.*, s.name AS series_name
            FROM books b LEFT JOIN series s ON b.series_id = s.id
            WHERE b.id = ?
        """, (bid,))).fetchone()
        if not b_row:
            raise HTTPException(404, f"book {bid} not found")
        book_dict = dict(b_row)

        existing_uef = _parse_user_edited(book_dict.get("user_edited_fields"))
        fields = _resolve_fields(payload, existing_uef, col_map)
        if not fields:
            return {
                "book_id": bid, "source": source,
                "applied": [], "failed": [],
                "user_edited_fields": existing_uef,
            }

        # Dispatch.
        try:
            if source == "abs":
                result = await push_abs(db, book_dict, fields)
            else:
                # Calibre — try calibredb first, fall back to CWA.
                try:
                    result = await push_calibre_full(db, book_dict, fields)
                except PushUnavailable:
                    result = await push_cwa(db, book_dict, fields)
        except PushUnavailable as e:
            raise HTTPException(409, str(e))
        except PushFailed as e:
            raise HTTPException(502, str(e))

        applied = list(result.get("applied") or [])
        failed = list(result.get("failed") or [])

        # Clear applied fields from user_edited_fields. Same rationale
        # as pull-clears: post-push the upstream value matches Seshat-
        # live, so there's no edit divergence to flag. The user re-
        # enters watched state by editing the field again.
        cleared_uef = sorted(set(existing_uef) - set(applied))
        if set(cleared_uef) != set(existing_uef):
            await db.execute(
                "UPDATE books SET user_edited_fields=? WHERE id=?",
                (json.dumps(cleared_uef), bid),
            )
            await db.commit()

        return {
            "book_id": bid,
            "source": source,
            "applied": applied,
            "failed": failed,
            "user_edited_fields": cleared_uef,
        }
    finally:
        await db.close()


# ── Pending manual edits view (v2.3.5) ──────────────────────────────


@router.get("/pending-edits")
async def list_pending_edits(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List every book with non-empty `user_edited_fields`, across all
    libraries. Surfaces "I edited this and haven't pushed yet" state
    that doesn't fit the review-queue model.

    Each row carries the book + author + library slug/name, the
    parsed `fields` array, and Calibre/ABS snapshot sync timestamps
    (so the UI can decide which Push/Pull actions are available
    without round-tripping `/compare` per row).

    The frontend uses this to render a "Pending manual edits" tab in
    the Metadata Manager — the user can drive bulk push/pull from
    here, or open the Compare modal for per-field control.
    """
    from app.discovery.cross_library import run_across_libraries

    async def _q(db) -> list[dict]:
        rows = await (await db.execute("""
            SELECT b.id AS book_id, b.title, b.user_edited_fields,
                   bpa.author_id, a.name AS author_name,
                   b.calibre_id, b.audiobookshelf_id,
                   cs.synced_at AS calibre_synced_at,
                   abs_s.synced_at AS abs_synced_at
            FROM books b
            JOIN book_authors bpa ON bpa.book_id = b.id AND bpa.position = 0
            JOIN authors a ON a.id = bpa.author_id
            LEFT JOIN books_calibre_snapshot cs ON cs.book_id = b.id
            LEFT JOIN books_abs_snapshot abs_s ON abs_s.book_id = b.id
            WHERE b.user_edited_fields IS NOT NULL
              AND b.user_edited_fields NOT IN ('', '[]')
        """)).fetchall()
        return [dict(r) for r in rows]

    # Actionable field set: union of every field that pull/push can
    # actually act on across both sources (Calibre + ABS), plus the
    # synthetic series_name. Fields outside this set are Seshat-only
    # (e.g. `expected_date`, `cover_url` — tracked by PUT diff but with
    # no upstream counterpart). They legitimately stay in
    # `user_edited_fields` after a bulk pull/push, but they shouldn't
    # show up in the Pending Manual Edits view because no action there
    # can clear them.
    actionable = (
        {b for b, c, _, _ in COMPARE_FIELDS if c is not None}
        | {b for b, _, a, _ in COMPARE_FIELDS if a is not None}
        | {"series_name"}
    )

    raw = await run_across_libraries(None, _q)
    out: list[dict] = []
    for r in raw:
        try:
            fields = json.loads(r["user_edited_fields"] or "[]")
            if not isinstance(fields, list):
                fields = []
        except (TypeError, ValueError):
            fields = []
        # Filter to actionable fields. A book with only Seshat-only
        # edits (e.g. just `expected_date`) drops out entirely.
        fields = [f for f in fields if f in actionable]
        if not fields:
            continue
        out.append({
            "book_id": r["book_id"],
            "title": r["title"],
            "author_name": r.get("author_name"),
            "library_slug": r.get("library_slug"),
            "library_name": r.get("library_name"),
            "fields": fields,
            "has_calibre_snapshot": r.get("calibre_synced_at") is not None,
            "has_abs_snapshot": r.get("abs_synced_at") is not None,
            "calibre_synced_at": r.get("calibre_synced_at"),
            "abs_synced_at": r.get("abs_synced_at"),
            "calibre_id": r.get("calibre_id"),
            "audiobookshelf_id": r.get("audiobookshelf_id"),
        })
    # Stable ordering: alphabetical by title within the merged list so
    # repeated polls don't shuffle. Pagination is client-friendly slice.
    out.sort(key=lambda x: ((x.get("title") or "").lower(), x["book_id"]))
    total = len(out)
    return {
        "rows": out[offset:offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ── Metadata Manager — review queue endpoints ────────────────────────


@router.get("/queue")
async def list_queue(
    source: str = Query(None),
    status: str = Query("pending"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List `metadata_review_queue` rows, grouped by source category.

    `source` filters to a specific source name ('calibre', 'abs',
    'goodreads', etc.). Omit for all.

    `status` is currently always 'pending' — the queue table has no
    status column today (rows are created on diff and deleted on
    accept/dismiss), so this param is reserved for future use when
    we add a soft-delete pattern. The Metadata Manager UI surfaces
    a status filter (currently no-op) so the contract is in place.

    Returns rows joined with book + author info for direct render.
    """
    db = await get_db()
    try:
        sql = (
            "SELECT q.id, q.book_id, q.field, q.old_value, q.new_value, "
            "q.source, q.proposed_at, "
            "b.title as book_title, a.name as author_name "
            "FROM metadata_review_queue q "
            "JOIN books b ON b.id = q.book_id "
            "JOIN book_authors bpa ON bpa.book_id = b.id AND bpa.position = 0 "
            "JOIN authors a ON a.id = bpa.author_id"
        )
        params: list = []
        clauses = []
        if source:
            clauses.append("q.source = ?")
            params.append(source)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY q.proposed_at DESC, q.id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = await (await db.execute(sql, params)).fetchall()

        # Total count (pre-pagination) so the UI can render
        # "showing X of Y" + paginate.
        count_sql = "SELECT COUNT(*) AS n FROM metadata_review_queue"
        count_params: list = []
        if source:
            count_sql += " WHERE source = ?"
            count_params.append(source)
        total = (await (await db.execute(
            count_sql, count_params,
        )).fetchone())["n"]

        return {
            "rows": [dict(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        await db.close()


async def _queue_apply_authors(db, qid: int, qrow: dict) -> dict:
    """v3.3.0 (ADR-0017) — accept an authors proposed-change.

    The full chain in one step:
      1. Parse the JSON proposal payload (`new_value`).
      2. Push the position-ordered names to the appropriate sink(s):
         calibre route (calibredb → fall back to CWA) when the book has
         a `calibre_id`; ABS when the book has an `audiobookshelf_id`.
         Both fire if both ids are present (dual-library co-owned book).
      3. Inline re-sync: resolve each `(name, source_id)` via
         `resolve_or_create_author` (mints missing, captures source IDs
         so v3.1.0's ID-first matching applies on subsequent scans);
         `write_book_authors` overwrites position-ordered links.
      4. Recompute `series.author_mode` for the affected series.
      5. Delete the queue row.

    Failure modes:
      - 502 if ALL configured sinks rejected the push (PushFailed) — the
        local state is untouched, operator can retry.
      - 409 if NO sinks are configured / unavailable for this book.
      - 502 if the re-sync resolves to an empty author set (Calibre and
        ABS both require ≥1).
    """
    from app.discovery.push_back import (
        PushFailed, PushUnavailable,
        push_abs, push_calibre_full, push_cwa,
    )
    from app.discovery.database import (
        resolve_or_create_author, write_book_authors,
    )

    bid = qrow["book_id"]
    source_name = qrow["source"]
    try:
        payload = json.loads(qrow["new_value"] or "[]")
    except (TypeError, ValueError):
        raise HTTPException(400, "authors proposal has malformed JSON payload")
    if not isinstance(payload, list) or not payload:
        raise HTTPException(400, "authors proposal payload is empty or non-list")

    # Read the book so we know which sinks to push to.
    b_row = await (await db.execute(
        "SELECT b.*, s.name AS series_name FROM books b "
        "LEFT JOIN series s ON b.series_id = s.id WHERE b.id = ?",
        (bid,),
    )).fetchone()
    if not b_row:
        # Book deleted out from under us — drop the queue row to stop
        # surfacing a now-orphaned proposal.
        await db.execute(
            "DELETE FROM metadata_review_queue WHERE id = ?", (qid,),
        )
        await db.commit()
        raise HTTPException(404, f"book {bid} not found")
    book_dict = dict(b_row)

    # ── Push to sinks ────────────────────────────────────────────
    push_errors: list[str] = []
    push_attempted: list[str] = []
    push_succeeded: list[str] = []

    if book_dict.get("calibre_id"):
        push_attempted.append("calibre")
        try:
            try:
                await push_calibre_full(
                    db, book_dict, ["authors"], authors=payload,
                )
                push_succeeded.append("calibredb")
            except PushUnavailable:
                await push_cwa(
                    db, book_dict, ["authors"], authors=payload,
                )
                push_succeeded.append("cwa")
        except PushUnavailable as e:
            push_errors.append(f"calibre: {e}")
        except PushFailed as e:
            push_errors.append(f"calibre: {e}")

    if book_dict.get("audiobookshelf_id"):
        push_attempted.append("abs")
        try:
            await push_abs(db, book_dict, ["authors"], authors=payload)
            push_succeeded.append("abs")
        except PushUnavailable as e:
            push_errors.append(f"abs: {e}")
        except PushFailed as e:
            push_errors.append(f"abs: {e}")

    if not push_attempted:
        raise HTTPException(
            409,
            f"book {bid} has neither calibre_id nor audiobookshelf_id; "
            "no sink to push authors to",
        )
    if not push_succeeded:
        raise HTTPException(
            502,
            "all push attempts failed: " + "; ".join(push_errors),
        )

    # ── Inline re-sync — book_authors + series author_mode ───────
    # Resolve each (name, source_id) from the proposal payload. Names
    # come from the operator-approved source; source IDs are namespaced
    # to `source_name` (the proposal's source column). Mints missing
    # author rows; v3.1.0's ID-first matching on resolve_or_create_author
    # consolidates persons across libraries when a source ID is present.
    ordered_ids: list[int] = []
    for rec in payload:
        name = (rec.get("name") or "").strip()
        if not name:
            continue
        aid = await resolve_or_create_author(
            db, name, allow_create=True,
            source=source_name, source_id=rec.get("source_id"),
        )
        if aid is not None and aid not in ordered_ids:
            ordered_ids.append(aid)

    if not ordered_ids:
        raise HTTPException(
            502,
            "authors re-sync resolved to empty set; book_authors left untouched",
        )

    await write_book_authors(db, bid, ordered_ids)

    # Recompute series author_mode for the affected series (ADR-0010).
    sid = book_dict.get("series_id")
    if sid is not None:
        try:
            from app.discovery.routers.series import _recompute_series_author
            await _recompute_series_author(db, {sid})
        except Exception as e:
            # Non-fatal: push succeeded + book_authors landed; series
            # taxonomy will catch up on next scan-converged recompute.
            logger.warning(
                "series author_mode recompute after authors approve failed "
                "for book_id=%s series_id=%s: %s", bid, sid, e,
            )

    await db.execute(
        "DELETE FROM metadata_review_queue WHERE id = ?", (qid,),
    )
    await db.commit()

    return {
        "applied": qid,
        "book_id": bid,
        "field": "authors",
        "push_succeeded": push_succeeded,
        "push_errors": push_errors,
    }


@router.post("/queue/{qid}/apply")
async def queue_apply(qid: int):
    """Accept a queue row.

    Scalar fields (the historical path): write `new_value` to the
    corresponding books column, add the field to `user_edited_fields`,
    and delete the queue row. Type coercion mirrors the column types.

    `field='authors'` (v3.3.0 / ADR-0017): the proposal carries a
    JSON `new_value` payload of contributor records. Approving runs
    the **full chain in one step** (push to upstream sinks → inline
    re-sync `book_authors` → recompute series `author_mode`) — see
    `_queue_apply_authors`. This is a different shape from scalar-field
    apply (which writes locally + waits for a separate push); for
    authors there's no useful "approve locally but don't push" state
    because `book_authors` is downstream of the upstream sync anyway.
    """
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT id, book_id, field, new_value, source "
            "FROM metadata_review_queue WHERE id = ?", (qid,),
        )).fetchone()
        if not row:
            raise HTTPException(404, f"queue row {qid} not found")
        field = row["field"]
        new_val_raw = row["new_value"]
        if field == "authors":
            return await _queue_apply_authors(db, qid, dict(row))

        # Type coercion — mirrors the books column types.
        new_val: object = new_val_raw
        try:
            if field in ("series_index", "duration_sec", "rating"):
                new_val = float(new_val_raw) if new_val_raw is not None else None
            elif field in ("page_count", "abridged", "is_unreleased"):
                new_val = int(new_val_raw) if new_val_raw is not None else None
        except (TypeError, ValueError):
            raise HTTPException(
                400, f"new_value cannot coerce to {field}'s type",
            )

        # Update the books row + merge field into user_edited_fields.
        bid = row["book_id"]
        b_row = await (await db.execute(
            "SELECT user_edited_fields FROM books WHERE id = ?", (bid,),
        )).fetchone()
        if not b_row:
            # Book deleted out from under us — drop the queue row.
            await db.execute(
                "DELETE FROM metadata_review_queue WHERE id = ?", (qid,),
            )
            await db.commit()
            raise HTTPException(404, f"book {bid} not found")
        uef = _parse_user_edited(b_row["user_edited_fields"])
        uef_merged = sorted(set(uef) | {field})
        await db.execute(
            f"UPDATE books SET {field}=?, user_edited_fields=? WHERE id=?",
            (new_val, json.dumps(uef_merged), bid),
        )
        await db.execute(
            "DELETE FROM metadata_review_queue WHERE id = ?", (qid,),
        )
        await db.commit()
        return {"applied": qid, "book_id": bid, "field": field}
    finally:
        await db.close()


@router.post("/queue/{qid}/dismiss")
async def queue_dismiss(qid: int):
    """Reject a queue row: delete it without writing to books."""
    db = await get_db()
    try:
        cur = await db.execute(
            "DELETE FROM metadata_review_queue WHERE id = ?", (qid,),
        )
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, f"queue row {qid} not found")
        return {"dismissed": qid}
    finally:
        await db.close()


@router.post("/queue/bulk")
async def queue_bulk(payload: dict = Body(...)):
    """Bulk apply or dismiss queue rows.

    Body: {"action": "apply" | "dismiss", "ids": [1, 2, 3]}.
    Returns per-id success/failure so the caller can resolve partial
    failures (e.g. one row's book_id was deleted) without abandoning
    the rest.
    """
    action = payload.get("action")
    ids = payload.get("ids") or []
    if action not in ("apply", "dismiss"):
        raise HTTPException(400, "action must be 'apply' or 'dismiss'")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids must be a non-empty list")

    results: list[dict] = []
    for qid in ids:
        try:
            if action == "apply":
                await queue_apply(qid)
            else:
                await queue_dismiss(qid)
            results.append({"id": qid, "ok": True})
        except HTTPException as e:
            results.append({"id": qid, "ok": False, "error": str(e.detail)})
    succeeded = sum(1 for r in results if r["ok"])
    return {"results": results, "succeeded": succeeded, "total": len(ids)}
