"""
Send to Pipeline — hands a confirmed-match book off to the acquisition
pipeline.

When a book has a confirmed MAM match (mam_status="found"), the user
can send it to the pipeline for automatic download and processing.
Supports both single-book sends and bulk sends. Calls `inject_grab`
directly (no HTTP round-trip) since both domains live in the same
process.
"""
import json
import logging

from fastapi import APIRouter, Body, HTTPException

from app import state
from app.config import load_settings
from app.database import get_db as get_pipeline_db
from app.discovery.database import get_db as get_discovery_db
from app.mam.torrent_id import extract_torrent_id
from app.orchestrator.auto_train import train_author
from app.orchestrator.dispatch import inject_grab

logger = logging.getLogger("seshat.discovery")

router = APIRouter(prefix="/api/discovery", tags=["pipeline-send"])

@router.post("/send-to-pipeline")
async def send_to_pipeline(data: dict = Body(...)):
    """Send one or more books to the pipeline for download.

    Accepts a list of book IDs. Only books with mam_status="found"
    are sent — others are silently skipped.

    `use_wedge_override=True` forces `&fl=1` on every grab in the
    batch, one wedge per torrent (never on one that's already free or
    of unknown status). A `buy_personal_fl` flag existed until MAM
    refused personal FL via the API (2026-10-07); it's ignored now.
    """
    book_ids = data.get("book_ids", [])
    if not book_ids:
        raise HTTPException(400, "No books specified")
    use_wedge_override = bool(data.get("use_wedge_override", False))
    # v2.9.0 — bypass the format-priority dedup gate for this batch.
    # Equivalent to the manual-inject "Snatch anyway" checkbox.
    override_format_dedup = bool(data.get("override_format_dedup", False))
    # Phase 0 snatch safety — confirmed second download of a torrent
    # MAM says this account already snatched. Never overrides
    # `already_grabbed`.
    override_mam_snatched = bool(data.get("override_mam_snatched", False))

    if state.dispatcher is None:
        raise HTTPException(503, "Pipeline dispatcher not initialized")

    db = await get_discovery_db()
    try:
        placeholders = ",".join("?" * len(book_ids))
        rows = await (await db.execute(
            f"SELECT b.id, b.title, b.mam_url, b.mam_status, b.mam_torrent_id, "
            f"b.mam_category, b.mam_formats, "
            f"b.source_url, b.isbn, b.series_id, b.series_index, b.cover_url, "
            f"b.description, b.page_count, "
            f"a.name as author_name, s.name as series_name "
            f"FROM books b "
            f"JOIN book_authors bpa ON bpa.book_id = b.id AND bpa.position = 0 "
            f"JOIN authors a ON a.id = bpa.author_id "
            f"LEFT JOIN series s ON b.series_id = s.id "
            f"WHERE b.id IN ({placeholders})",
            book_ids,
        )).fetchall()
    finally:
        await db.close()

    if not rows:
        raise HTTPException(404, "No books found for the given IDs")

    found_rows = [r for r in rows if r["mam_status"] == "found" and r["mam_torrent_id"]]
    skipped = len(rows) - len(found_rows)

    if not found_rows:
        return {
            "sent": 0,
            "skipped": skipped,
            "message": "No books with 'Found' MAM status to send",
        }

    submitted = 0
    failed = 0
    results = []

    for r in found_rows:
        tid = extract_torrent_id(str(r["mam_torrent_id"]))
        if tid is None:
            results.append({"torrent_id": str(r["mam_torrent_id"]), "ok": False, "error": "bad torrent ID"})
            failed += 1
            continue

        author = r["author_name"] or ""

        # Auto-train the author in the pipeline's allow-list.
        if author:
            pdb = await get_pipeline_db()
            try:
                await train_author(pdb, author, source="discovery")
            except Exception:
                pass
            finally:
                await pdb.close()

        try:
            # v2.9.0 — feed the first MAM format (e.g. "epub" from a
            # comma-joined "epub,azw3") into inject_grab so the dedup
            # gate can recognize the format. Without this hint the
            # gate falls through to allow because filetype is empty.
            mam_formats_csv = (r["mam_formats"] or "").strip().lower()
            filetype_hint = (mam_formats_csv.split(",")[0] or "").strip()

            result = await inject_grab(
                state.dispatcher,
                torrent_id=tid,
                torrent_name=(r["title"] or "").strip(),
                category=(r["mam_category"] or "").strip(),
                author_blob=author,
                filetype=filetype_hint,
                # Phase 5: pass clean book metadata so download-folder
                # template-mode (`{author}/{series}/{title}`) renders
                # with the real values instead of the noisy torrent
                # filename. Empty for standalones — the template
                # renderer drops the {series} segment cleanly.
                series_name=(r["series_name"] or "").strip(),
                book_title=(r["title"] or "").strip(),
                raw_line=f"discovery:{r['mam_torrent_id']}",
                force_fl_wedge=use_wedge_override,
                apply_format_dedup=not override_format_dedup,
                override_mam_snatched=override_mam_snatched,
            )
            ok = result.action in ("submit", "queue") and result.error is None

            # Persist discovery metadata on the grab row for the enricher.
            if ok and result.grab_id:
                metadata = {}
                if r["isbn"]:
                    metadata["isbn"] = r["isbn"]
                if r["cover_url"]:
                    metadata["cover_url"] = r["cover_url"]
                if r["description"]:
                    metadata["description"] = r["description"]
                if r["page_count"]:
                    metadata["page_count"] = r["page_count"]
                if r["series_name"]:
                    metadata["series_name"] = r["series_name"]
                if r["series_index"]:
                    metadata["series_index"] = r["series_index"]
                # Include which discovery-side sources contributed to
                # this book's record. `books.source_url` is a JSON
                # dict `{"goodreads": "https://...", "hardcover":
                # "https://...", ...}` populated as sources respond
                # during lookup. The pipeline uses the keys to render
                # "via discovery (goodreads, hardcover)" on the review
                # card instead of the opaque "via source_metadata".
                if r["source_url"]:
                    try:
                        src_map = json.loads(r["source_url"])
                        if isinstance(src_map, dict) and src_map:
                            metadata["sources_used"] = sorted(
                                k for k in src_map if k
                            )
                    except (ValueError, TypeError):
                        pass
                if metadata:
                    try:
                        pdb = await get_pipeline_db()
                        try:
                            await pdb.execute(
                                "UPDATE grabs SET source_metadata = ? WHERE id = ?",
                                (json.dumps(metadata), result.grab_id),
                            )
                            await pdb.commit()
                        finally:
                            await pdb.close()
                    except Exception:
                        logger.warning("Failed to persist metadata for grab_id=%s", result.grab_id, exc_info=True)

            results.append({"torrent_id": tid, "ok": ok, "action": result.action, "error": result.error})
            if ok:
                submitted += 1
            else:
                failed += 1
        except Exception as e:
            results.append({"torrent_id": tid, "ok": False, "error": str(e)})
            failed += 1

    # Notification
    try:
        from app.discovery.notify import notify_pipeline_sent
        await notify_pipeline_sent(submitted, skipped)
    except Exception:
        pass

    logger.info(f"Send-to-pipeline: {submitted} submitted, {failed} failed, {skipped} skipped")

    return {
        "sent": submitted,
        "skipped": skipped,
        "failed": failed,
        "message": f"Sent {submitted} to pipeline" + (f", {skipped} skipped (not Found)" if skipped else ""),
        "results": results,
    }


@router.get("/pipeline/status")
async def pipeline_status():
    """Check if the pipeline dispatcher is initialized."""
    return {
        "configured": True,
        "reachable": state.dispatcher is not None,
        "internal": True,
    }
