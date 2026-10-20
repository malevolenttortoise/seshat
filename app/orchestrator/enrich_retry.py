"""
Retry a source enrichment skipped while it was backing off (G182).

When a grab's enrichment skips a source because the source is backing
off after a block (Amazon's search / product pages cooling down,
Goodreads' book pages, Kobo), the review comes in without that source's
fields, and the 2026-10 trial showed it often: ~22 of 72 grabs skipped
Amazon inside a cooldown. Once the backoff ends, that source alone is run
again for the still-pending review and its record is merged as if it had
never been skipped (Mark, review gate 2026-10-16):

  - each enriched field takes the retried source's value when the field
    is empty or the source ranks above the field's current source
    (`enriched.field_sources`); description keeps the longer text, as the
    enricher's own merge does;
  - a top-level review field follows only when it was empty or still
    holds the old enriched value — a file-embedded value or your edit
    stays;
  - a cover the retried source wins is downloaded like a re-enrich's.

Only pending reviews; nothing re-runs after a decision. Each skipped
source is retried once: its `source_log` entry records the outcome
(`retried: true`). A source still backing off waits for the next run.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from app import state
from app.database import get_db
from app.storage import grabs as grabs_storage
from app.storage import review_queue as review_storage

_log = logging.getLogger("seshat.orchestrator.enrich_retry")

SKIPPED = "soft_blocked"
_MAX_PER_RUN = 10

# Enriched field -> top-level review field.
_TOP_LEVEL = {
    "title": "title", "series": "series", "series_index": "series_index",
    "language": "language", "publisher": "publisher",
    "description": "description", "isbn": "isbn", "asin": "asin",
    "narrator": "narrator", "duration_sec": "duration_sec", "abridged": "abridged",
}


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == []


def remerge(
    enriched: dict, new: dict, source: str, priority: list[str],
) -> tuple[dict, list[str]]:
    """`enriched` with `new` (a source's record as a dict) merged in as if
    the source had run in its place: (merged copy, the fields it changed)."""
    from app.metadata.enricher import _RECORD_FIELDS

    rank = {name: i for i, name in enumerate(priority)}
    rank["mam"] = -1                       # an exact-ID lookup outranks every source
    unranked = len(priority)
    out = dict(enriched)
    sources = dict(out.get("field_sources") or {})
    changed: list[str] = []
    for f in _RECORD_FIELDS:
        value = new.get(f)
        if _empty(value):
            continue
        current = out.get(f)
        if f == "description":
            take = _empty(current) or len(str(value)) > len(str(current))
        else:
            take = _empty(current) or (
                rank.get(source, unranked) < rank.get(sources.get(f), unranked)
            )
        if take and value != current:
            out[f] = value
            sources[f] = source
            changed.append(f)
    out["field_sources"] = sources
    return out, changed


def apply_to_review(metadata: dict, old_enriched: dict, new_enriched: dict,
                    changed: list[str]) -> dict:
    """The review's top-level fields after a re-merge: a field follows the
    enriched value only when it was empty or still the old enriched one."""
    md = dict(metadata)
    for f in changed:
        if f == "authors":
            old = ", ".join(old_enriched.get("authors") or [])
            if _empty(md.get("author")) or md.get("author") == old:
                md["author"] = ", ".join(new_enriched.get("authors") or [])
            continue
        top = _TOP_LEVEL.get(f)
        if top and (_empty(md.get(top)) or md.get(top) == old_enriched.get(f)):
            md[top] = new_enriched.get(f)
    md["enriched"] = new_enriched
    return md


def _skipped_sources(metadata: dict) -> list[str]:
    log = (metadata.get("enriched") or {}).get("source_log") or []
    return [e.get("source") for e in log if e.get("status") == SKIPPED and not e.get("retried")]


async def retry_skipped_sources(*, limit: int = _MAX_PER_RUN) -> int:
    """One pass over the pending reviews; returns how many source retries
    ran (whatever their outcome)."""
    deps = state.dispatcher
    enricher = getattr(deps, "metadata_enricher", None) if deps else None
    if enricher is None or not getattr(enricher.config, "enabled", False):
        return 0
    db = await get_db()
    try:
        pending = await review_storage.list_pending(db)
    finally:
        await db.close()
    ran = 0
    for row in pending:
        for source in _skipped_sources(row.metadata or {}):
            if ran >= limit:
                return ran
            if await _retry_one(enricher, row.id, source):
                ran += 1
    return ran


async def _retry_one(enricher, review_id: int, source: str) -> bool:
    """Retry `source` for one review. False when nothing ran (the source is
    still backing off, isn't configured, or the review moved on)."""
    from app.metadata.enricher import MetadataEnricher, _hook
    from app.metadata.author_lookup import (
        get_amazon_id_for_author, get_goodreads_id_for_author,
        get_library_slug_for_content_type,
    )
    from app.orchestrator.pipeline import _is_audiobook_grab

    db = await get_db()
    try:
        row = await review_storage.get_entry(db, review_id)
        if row is None or row.status != review_storage.STATUS_PENDING:
            return False
        grab = await grabs_storage.get_grab(db, row.grab_id)
    finally:
        await db.close()
    md = row.metadata or {}
    audiobook = _is_audiobook_grab(row.book_format or "", grab.category if grab else "")
    pool = enricher._audiobook_sources if audiobook else enricher._sources
    src = next((s for s in pool if s.name == source), None)
    if src is None:
        return False
    if _hook(src, "backing_off") or _still_backing_off(source):
        return False

    old_enriched = md.get("enriched") or {}
    title = str(md.get("title") or old_enriched.get("title") or "").strip()
    author = str(md.get("author") or ", ".join(old_enriched.get("authors") or []) or "").strip()
    if not title:
        return False
    library_slug = row.library_slug or get_library_slug_for_content_type(
        "audiobook" if audiobook else "ebook",
    )
    single = MetadataEnricher(enricher.config, sources=[src], audiobook_sources=[src])
    result = await single.enrich(
        title=title, author=author,
        isbn=str(md.get("isbn") or old_enriched.get("isbn") or ""),
        asin=str(md.get("asin") or old_enriched.get("asin") or ""),
        audiobook=audiobook, skip_mam=True,
        author_goodreads_id=await get_goodreads_id_for_author(author),
        author_amazon_id=await get_amazon_id_for_author(author, library_slug=library_slug or ""),
        library_slug=library_slug or "",
    )
    status = "no_result"
    if result is not None:
        status = "matched"
    else:
        # The single enricher's log says why: still blocked, below the
        # bar, another volume.
        status = _status_of(single, source) or "no_result"
    if status == SKIPPED:
        return False                       # backing off again: next run

    db = await get_db()
    try:
        fresh = await review_storage.get_entry(db, review_id)
        if fresh is None or fresh.status != review_storage.STATUS_PENDING:
            return True
        if json.dumps(fresh.metadata, sort_keys=True, default=str) != json.dumps(
            md, sort_keys=True, default=str,
        ):
            return False                   # edited meanwhile: next run reads it again
        new_md = dict(md)
        if result is not None:
            priority = list(enricher.config.audiobook_priority if audiobook else enricher.config.priority)
            new_enriched, changed = remerge(old_enriched, result.to_dict(), source, priority)
            new_md = apply_to_review(md, old_enriched, new_enriched, changed)
            if "cover_url" in changed and row.staged_path:
                cover = await _fetch_cover(new_enriched.get("cover_url"), row.staged_path)
                if cover:
                    new_md["cover_enriched"] = cover
        else:
            changed = []
        _mark_retried(new_md, source, status, changed)
        await review_storage.set_status(
            db, review_id, review_storage.STATUS_PENDING, metadata=new_md,
        )
    finally:
        await db.close()
    _log.info(
        "enrich retry: review_id=%d %s → %s%s", review_id, source, status,
        f" (fields: {', '.join(changed)})" if changed else "",
    )
    return True


def _still_backing_off(source: str) -> bool:
    if source == "goodreads":
        from app.metadata import goodreads_session
        from app.metadata.source_gate import KIND_BOOK_PAGE
        return goodreads_session.is_backing_off(KIND_BOOK_PAGE)
    if source == "amazon":
        from app.discovery.amazon_author_id_resolver import KIND_CATALOG, is_amazon_blocked
        return is_amazon_blocked(KIND_CATALOG)
    return False


def _status_of(enricher, source: str) -> Optional[str]:
    log = getattr(enricher, "last_source_log", None) or []
    for e in log:
        if e.get("source") == source:
            return e.get("status")
    return None


def _mark_retried(metadata: dict, source: str, status: str, changed: list[str]) -> None:
    enriched = dict(metadata.get("enriched") or {})
    log = [dict(e) for e in (enriched.get("source_log") or [])]
    for e in log:
        if e.get("source") == source and e.get("status") == SKIPPED and not e.get("retried"):
            e.update(status=status, retried=True, retried_fields=changed)
            break
    enriched["source_log"] = log
    metadata["enriched"] = enriched


async def _fetch_cover(url: Optional[str], staged_path: str) -> Optional[str]:
    if not url:
        return None
    try:
        from app.metadata.covers import fetch_cover
        target = Path(staged_path)
        target.mkdir(parents=True, exist_ok=True)
        path = await fetch_cover(url, dest_dir=target, basename="cover-enriched")
        return str(path) if path is not None else None
    except Exception:
        _log.exception("enrich retry: cover fetch failed (non-fatal)")
        return None
