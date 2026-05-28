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
import re

from fastapi import APIRouter, Body, HTTPException

from app import state
from app.config import load_settings
from app.database import get_db as get_pipeline_db
from app.discovery.database import get_db as get_discovery_db
from app.orchestrator.auto_train import train_author
from app.orchestrator.dispatch import inject_grab

logger = logging.getLogger("seshat.discovery")

router = APIRouter(prefix="/api/discovery", tags=["pipeline-send"])

_MAM_URL_RX = re.compile(r"/t/(\d+)")
_BARE_ID_RX = re.compile(r"^\d+$")


def _extract_torrent_id(url_or_id: str) -> str | None:
    s = url_or_id.strip()
    if _BARE_ID_RX.match(s):
        return s
    m = _MAM_URL_RX.search(s)
    return m.group(1) if m else None


@router.post("/send-to-pipeline")
async def send_to_pipeline(data: dict = Body(...)):
    """Send one or more books to the pipeline for download.

    Accepts a list of book IDs. Only books with mam_status="found"
    are sent — others are silently skipped.

    Two optional per-batch flags line up with the manual-inject
    confirm dialog's per-grab knobs:

      - `buy_personal_fl=True` — before each grab, spend 50k BP to
        flag that specific torrent as personal freeleech on MAM.
        Best-effort: a failed buy doesn't block the grab, it's just
        audited. Applies to EVERY torrent in the batch, so the UI
        typically only exposes this when sending one book at a time.
      - `use_wedge_override=True` — forces `&fl=1` on every grab in
        the batch, draining one wedge from the pool per torrent.
    """
    book_ids = data.get("book_ids", [])
    if not book_ids:
        raise HTTPException(400, "No books specified")
    buy_personal_fl = bool(data.get("buy_personal_fl", False))
    use_wedge_override = bool(data.get("use_wedge_override", False))
    # v2.9.0 — bypass the format-priority dedup gate for this batch.
    # Equivalent to the manual-inject "Snatch anyway" checkbox.
    override_format_dedup = bool(data.get("override_format_dedup", False))

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
        tid = _extract_torrent_id(str(r["mam_torrent_id"]))
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

        # F4 path: spend 50k BP to flag this torrent as personal
        # freeleech BEFORE the inject. Best-effort — a failed buy is
        # audited and the grab proceeds anyway. Calls the same helper
        # the manual-inject router uses so the audit row shape stays
        # consistent between the two entry points.
        if buy_personal_fl:
            try:
                from app.routers.inject import _buy_personal_fl_for_inject
                await _buy_personal_fl_for_inject(
                    tid, state.dispatcher.mam_token or "",
                )
            except Exception:
                logger.exception(
                    "personal-FL buy raised for tid=%s (non-fatal)", tid
                )

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
