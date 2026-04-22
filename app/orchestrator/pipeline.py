"""
Post-download pipeline orchestrator.

The post-download pipeline has two halves with a mandatory manual
review queue between them:

    HALF 1 (`process_completion`):
        1. Locate book files in the download directory
        2. Optionally copy to staging
        3. Extract + enrich metadata (announce + embedded)
        4. Patch metadata into a temp copy of the epub
        5a. If review_queue_enabled: copy the patched file to the
            review staging dir and insert a `book_review_queue` row,
            set pipeline state to `awaiting_review`, STOP. The user
            (or the auto-add timeout job) resumes with `deliver_reviewed`.
        5b. If review_queue_enabled is False (legacy/direct path):
            fall straight through to HALF 2.

    HALF 2 (`deliver_reviewed`):
        6. Route to the configured sink
        7. Auto-train: add author(s) to the allow list
        8. Send ntfy notification
        9. Record a calibre_additions row + mark pipeline complete

The split matters because CWA's inotify watcher only reacts to the
final atomic rename — if we handed CWA a partial or unenriched file
during review, it would ingest it before the user could approve or
edit the metadata. Keeping the patched file in a separate review
staging dir until the user signs off is what makes the manual review
step actually manual.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import aiosqlite

from app import state
from app.metadata.covers import fetch_cover
from app.metadata.extract import BookMetadata, extract as extract_metadata
from app.metadata.enricher import MetadataEnricher
from app.metadata.record import MetaRecord
from app.metadata.writer import patch_epub_metadata
from app.notify import ntfy
from app.orchestrator.auto_train import train_authors_from_blob
from app.orchestrator.download_watcher import CompletionEvent
from app.orchestrator.file_copier import copy_to_staging, find_book_files
from app.sinks.base import SinkResult
from app.sinks.audiobookshelf import AudiobookshelfSink
from app.sinks.calibre import CalibreSink
from app.sinks.cwa import CWASink
from app.sinks.folder import FolderSink
from app.storage import calibre_adds as calibre_adds_storage
from app.storage import grabs as grabs_storage
from app.storage import pipeline as pipe_storage
from app.storage import review_queue as review_storage

_log = logging.getLogger("seshat.orchestrator.pipeline")

# Book extensions used for single-file torrent matching.
_BOOK_EXTS = (".epub", ".mobi", ".azw", ".azw3", ".pdf", ".m4b", ".mp3", ".m4a", ".cbz", ".cbr")

# File extensions that identify an audiobook grab. The sink router and
# enricher both consult this — audiobook grabs go to ABS (when
# configured) and use `metadata_audiobook_priority` (Audible leads;
# it hydrates via Audnexus internally) instead of the ebook priority
# list.
_AUDIOBOOK_EXTS = frozenset({"m4b", "mp3", "m4a", "aax", "aa"})


def _is_audiobook_grab(book_format: str, category: str = "") -> bool:
    """Decide whether a grab is an audiobook.

    Prefers the file extension when available (authoritative for
    post-download routing). Falls back to the MAM category prefix
    — "AudioBooks - Fantasy" etc. — for the pre-download path where
    we haven't seen a file yet. Callers that only know one of the
    two should pass "" for the other.
    """
    if book_format and book_format.lstrip(".").lower() in _AUDIOBOOK_EXTS:
        return True
    if category and category.strip().lower().startswith("audiobook"):
        return True
    return False


async def _backfill_audio_companions(
    db: aiosqlite.Connection,
    *,
    review_dir: Path,
    primary_name: str,
    qbit_hash: str,
    pipeline_run_id: Optional[int] = None,
) -> None:
    """Copy missing audio companions from the torrent's download location.

    Repairs multi-file audiobooks that were staged to review with only
    the primary MP3 (pre-v1.3 bug where _stage_for_review copied just
    delivery_source). Queries qBit for the torrent's file list, resolves
    each to an absolute path under the correct save_path, and copies
    any audio-format files that aren't already in `review_dir`.

    Path resolution precedence: `pipeline_runs.source_path` first
    (captured when the original run executed — guaranteed to be the
    Seshat-side mount, which is what we can actually read), then
    qBit's live-reported save_path as a fallback. This matters on
    setups where qBit and Seshat mount the same share at different
    paths (/data in qBit vs /downloads in Seshat, common on Unraid).

    Best-effort. qBit offline, torrent removed, or save_path missing
    all fall through silently — the caller proceeds with whatever IS
    already staged.
    """
    dispatcher = state.dispatcher
    if dispatcher is None or not hasattr(dispatcher, "qbit"):
        return
    qbit = dispatcher.qbit

    existing = {
        p.name for p in review_dir.iterdir()
        if p.is_file() and p.suffix.lstrip(".").lower() in _AUDIOBOOK_EXTS
    }
    # Skip the round-trip when the dir already has multiple audio files
    # — the original _stage_for_review fix made it whole. Only the
    # single-audio-file case needs repair.
    if len(existing) > 1:
        return

    # Try pipeline_runs.source_path first — this is the path Seshat
    # actually used when the download completed, so it's known-reachable
    # from this container. Fall back to qBit's live save_path.
    candidate_paths: list[Path] = []
    if pipeline_run_id:
        try:
            row = await (await db.execute(
                "SELECT source_path FROM pipeline_runs WHERE id = ?",
                (pipeline_run_id,),
            )).fetchone()
            if row and row["source_path"]:
                candidate_paths.append(Path(row["source_path"]))
        except Exception:
            _log.debug("backfill: pipeline_runs lookup failed", exc_info=True)

    torrent = await qbit.get_torrent(qbit_hash)
    if torrent is not None and torrent.save_path:
        # Apply the qbit_path_prefix → local_path_prefix translation
        # (the rest of the pipeline does this in budget_watcher before
        # emitting a CompletionEvent; the live qBit query bypasses that
        # path). Unraid-style setups where qBit mounts /data and
        # Seshat mounts /downloads need this or the raw qBit string
        # doesn't resolve on our side.
        from app.orchestrator.download_folders import translate_path
        translated = translate_path(
            torrent.save_path,
            dispatcher.qbit_path_prefix,
            dispatcher.local_path_prefix,
        )
        qbit_save = Path(translated)
        if qbit_save not in candidate_paths:
            candidate_paths.append(qbit_save)

    save_path: Optional[Path] = None
    for cand in candidate_paths:
        if cand.exists():
            save_path = cand
            break
    if save_path is None:
        _log.warning(
            "backfill: no reachable save_path among %s (qBit reports %s); "
            "skipping companion copy",
            [str(p) for p in candidate_paths],
            torrent.save_path if torrent else "<no torrent>",
        )
        return

    rel_files = await qbit.list_torrent_files(qbit_hash)
    copied = 0
    for rel in rel_files:
        if not rel:
            continue
        src = save_path / rel
        if not src.is_file():
            continue
        ext = src.suffix.lstrip(".").lower()
        if ext not in _AUDIOBOOK_EXTS:
            continue
        if src.name in existing or src.name == primary_name:
            continue
        dest = review_dir / src.name
        if dest.exists():
            continue
        try:
            shutil.copy2(str(src), str(dest))
            copied += 1
        except Exception:
            _log.exception(
                "backfill_audio_companions: copy failed %s → %s", src, dest,
            )
    if copied:
        _log.info(
            "deliver_reviewed: backfilled %d audio companion(s) into %s "
            "from qBit save_path=%s", copied, review_dir, save_path,
        )


def _find_torrent_file(parent: Path, torrent_name: str) -> Optional[Path]:
    """Find a single-file torrent's actual file on disk.

    qBit's torrent name often differs from the filename:
      - "Down Below" → "Down Below by Scott Moon.epub"
      - "The Triangulum Fold" → "Nick Adams - [The Fold 8] - The Triangulum Fold.epub"

    Tries in order:
      1. Exact name with any book extension
      2. Any file whose stem starts with the torrent name (prefix)
      3. Any file whose stem contains the torrent name (substring)
    Returns the matched Path, or None to let the caller fall back.
    """
    if not parent.is_dir():
        return None

    name_lower = torrent_name.lower()

    # Try exact name + extension.
    for ext in _BOOK_EXTS:
        candidate = parent / f"{torrent_name}{ext}"
        if candidate.exists():
            return candidate

    # Collect book files once for prefix + substring passes.
    book_files = [
        f for f in parent.iterdir()
        if f.is_file() and f.suffix.lower() in _BOOK_EXTS
    ]

    # Try prefix match: file stem starts with the torrent name.
    for f in book_files:
        if f.stem.lower().startswith(name_lower):
            return f

    # Try substring match: torrent name appears anywhere in the stem.
    # If multiple match, prefer the shortest filename (closest match).
    substring_matches = [
        f for f in book_files
        if name_lower in f.stem.lower()
    ]
    if len(substring_matches) == 1:
        return substring_matches[0]
    if len(substring_matches) > 1:
        # Shortest name is most likely the right file.
        return min(substring_matches, key=lambda f: len(f.name))

    return None


def _get_mam_token() -> str:
    """Read the current MAM token from the cookie module's in-memory cache."""
    try:
        from app.mam.cookie import get_current_token
        return get_current_token() or ""
    except Exception:
        return ""


async def process_completion(
    db: aiosqlite.Connection,
    event: CompletionEvent,
    *,
    staging_path: str,
    default_sink: str,
    calibre_library_path: str,
    folder_sink_path: str,
    audiobookshelf_library_path: str = "",
    abs_base_url: str = "",
    abs_api_key: str = "",
    abs_library_id: str = "",
    cwa_ingest_path: str = "",
    category_routing: dict[str, str] = None,
    ntfy_url: str,
    ntfy_topic: str,
    auto_train_enabled: bool = True,
    review_queue_enabled: bool = False,
    review_staging_path: str = "",
    per_event_notifications: bool = False,
    metadata_enricher: Optional[MetadataEnricher] = None,
    torrent_files: Optional[list[str]] = None,
    audiobook_format_priority: Optional[list[str]] = None,
) -> bool:
    """Drive one completed download through the pipeline.

    When `review_queue_enabled` is True, the pipeline stops after
    inserting a `book_review_queue` row and leaves the patched file
    in `review_staging_path`. The return value is still True for
    "successfully staged for review" — failures (missing files,
    patch errors, etc.) return False and record the error.

    When `review_queue_enabled` is False, the legacy straight-to-sink
    path is used — kept for tests and for users who explicitly disable
    review in settings.

    Never raises on expected-failure paths. All errors go through
    `_fail()` and end up on the pipeline_run row.
    """
    run_id = event.pipeline_run_id

    try:
        prep = await _prepare_book(
            db, event, staging_path=staging_path, run_id=run_id,
            ntfy_url=ntfy_url, ntfy_topic=ntfy_topic,
            metadata_enricher=metadata_enricher,
            torrent_files=torrent_files,
            audiobook_format_priority=audiobook_format_priority,
        )
        if prep is None:
            return False

        if per_event_notifications and ntfy_url and ntfy_topic:
            try:
                await ntfy.notify_download_complete(
                    ntfy_url, ntfy_topic,
                    event.torrent_name,
                    prep.metadata.author or "",
                )
            except Exception:
                _log.exception(
                    "per-event notify_download_complete failed (non-fatal)"
                )

        if review_queue_enabled:
            ok = await _stage_for_review(
                db, event, prep,
                review_staging_path=review_staging_path,
                ntfy_url=ntfy_url, ntfy_topic=ntfy_topic,
                per_event_notifications=per_event_notifications,
            )
            return ok

        return await _deliver_prepared(
            db, event, prep,
            default_sink=default_sink,
            calibre_library_path=calibre_library_path,
            folder_sink_path=folder_sink_path,
            per_event_notifications=per_event_notifications,
            audiobookshelf_library_path=audiobookshelf_library_path,
            abs_base_url=abs_base_url,
            abs_api_key=abs_api_key,
            abs_library_id=abs_library_id,
            cwa_ingest_path=cwa_ingest_path,
            ntfy_url=ntfy_url,
            ntfy_topic=ntfy_topic,
            auto_train_enabled=auto_train_enabled,
            review_id=None,
            was_timeout=False,
        )
    except Exception:
        _log.exception("pipeline: unexpected error for grab_id=%d", event.grab_id)
        try:
            await pipe_storage.set_state(
                db, run_id, pipe_storage.PIPE_FAILED,
                error="unexpected error (see logs)",
            )
        except Exception:
            pass
        return False


# ─── Phase halves ───────────────────────────────────────────────


class _PreparedBook:
    """Internal carrier for the outputs of `_prepare_book`."""
    __slots__ = (
        "book_path", "book_filename", "book_format",
        "metadata", "enriched", "announce_author",
        "delivery_source", "temp_dir", "cleanup_temp",
    )

    def __init__(
        self,
        *,
        book_path: Path,
        book_filename: str,
        book_format: str,
        metadata: BookMetadata,
        announce_author: str,
        delivery_source: Path,
        temp_dir: Optional[Path],
        cleanup_temp: bool,
        enriched: Optional[MetaRecord] = None,
    ):
        self.book_path = book_path
        self.book_filename = book_filename
        self.book_format = book_format
        self.metadata = metadata
        self.enriched = enriched
        self.announce_author = announce_author
        self.delivery_source = delivery_source
        self.temp_dir = temp_dir
        self.cleanup_temp = cleanup_temp


async def _prepare_book(
    db: aiosqlite.Connection,
    event: CompletionEvent,
    *,
    staging_path: str,
    run_id: int,
    ntfy_url: str,
    ntfy_topic: str,
    metadata_enricher: Optional[MetadataEnricher] = None,
    torrent_files: Optional[list[str]] = None,
    audiobook_format_priority: Optional[list[str]] = None,
) -> Optional[_PreparedBook]:
    """Steps 1-4: locate file, optional staging, metadata, patch.

    `torrent_files` is the list of file paths qBit reports for the
    completed torrent (from `list_torrent_files(hash)`), relative to
    `save_path`. When present, it's authoritative: we use those exact
    paths rather than guessing. When absent or empty we fall back to
    the older name-heuristic search (exact/prefix/substring match on
    `torrent_name`) for legacy clients and tests.

    Returns a `_PreparedBook` on success, or None after recording
    a failure on the pipeline run.
    """
    loop = asyncio.get_event_loop()
    save_path = Path(event.save_path)

    # Authoritative path resolution via the qBit file list. Every
    # book file we find here comes straight from the client's
    # view of what actually got written to disk — no string-match
    # heuristics, so a torrent announce called "Infinite Warship"
    # that lands as `Infinite_Warship_-_Scott_Bartlett.epub`
    # resolves correctly even though the two strings share almost
    # no characters after casefolding.
    book_files: list[Path] = []
    source: Path = save_path
    if torrent_files:
        book_extensions = _BOOK_EXTS
        matched_paths: list[Path] = []
        for rel in torrent_files:
            if not rel:
                continue
            candidate = save_path / rel
            if candidate.suffix.lower() in book_extensions and candidate.is_file():
                matched_paths.append(candidate)
        if matched_paths:
            # Sort for deterministic primary selection when a torrent
            # carries multiple book files (e.g. bundle / omnibus pack).
            # Secondary pass applies the user's audiobook format
            # priority (mixed-format bundles pick the preferred
            # extension first; single-format is a no-op).
            from app.orchestrator.file_copier import (
                _apply_audiobook_priority,
            )
            book_files = _apply_audiobook_priority(
                sorted(matched_paths, key=lambda p: p.name.lower()),
                audiobook_format_priority,
            )
            # `source` is a representative directory for logging +
            # staging copy fallback. Prefer the common parent when
            # every matched file shares one; otherwise use save_path.
            common_parents = {p.parent for p in matched_paths}
            source = next(iter(common_parents)) if len(common_parents) == 1 else save_path

    if not book_files:
        # Legacy fallback: scope the search to the torrent's specific
        # directory. qBit's save_path is the parent folder; the
        # torrent_name is the subfolder (or file) the torrent created.
        # If that path doesn't exist we try a name-heuristic match
        # before ever scanning the wider save_path — scanning blindly
        # is what caused the v1.2.2 cross-grab frankensteining bug.
        fallback_source = save_path / event.torrent_name
        if not fallback_source.exists():
            matched = _find_torrent_file(save_path, event.torrent_name)
            if matched is None:
                await _fail(db, run_id, event,
                            f"torrent files unavailable from client; "
                            f"no file matching {event.torrent_name!r} in {save_path}",
                            ntfy_url, ntfy_topic)
                return None
            fallback_source = matched
        source = fallback_source
        book_files = await loop.run_in_executor(
            None,
            functools.partial(
                find_book_files, source,
                audiobook_priority=audiobook_format_priority,
            ),
        )

    if not book_files:
        await _fail(db, run_id, event,
                    f"no book files found in {source}",
                    ntfy_url, ntfy_topic)
        return None

    primary_book = book_files[0]
    book_filename = primary_book.name
    book_format = primary_book.suffix.lstrip(".").lower()

    _log.debug(
        "pipeline: found %d book file(s) for grab_id=%d, primary=%s",
        len(book_files), event.grab_id, book_filename,
    )

    # Optional staging copy. When qBit gave us an explicit file list,
    # pass it through to the copier so only torrent-owned files get
    # staged — otherwise the copier would rglob `source` and, if
    # `source` is the shared save_path, pull in unrelated files from
    # other torrents (the v1.2.2 cross-grab frankensteining bug).
    if staging_path:
        explicit = book_files if torrent_files else None
        copy_result = await loop.run_in_executor(
            None,
            functools.partial(
                copy_to_staging,
                source, Path(staging_path), event.torrent_name,
                explicit_files=explicit,
                audiobook_priority=audiobook_format_priority,
            ),
        )
        if not copy_result.success:
            await _fail(db, run_id, event,
                        f"staging failed: {copy_result.error}",
                        ntfy_url, ntfy_topic)
            return None
        book_dir = Path(copy_result.staged_path)
        book_path = book_dir / (copy_result.book_filename or book_filename)
        book_filename = copy_result.book_filename or book_filename
        book_format = copy_result.book_format or book_format
        await pipe_storage.set_state(
            db, run_id, pipe_storage.PIPE_EXTRACTED,
            staged_path=str(book_dir),
            book_filename=book_filename,
            book_format=book_format,
        )
    else:
        book_path = primary_book
        await pipe_storage.set_state(
            db, run_id, pipe_storage.PIPE_EXTRACTED,
            staged_path=str(source),
            book_filename=book_filename,
            book_format=book_format,
        )

    # Extract + enrich metadata.
    file_metadata = BookMetadata()
    if book_path.exists():
        file_metadata = extract_metadata(book_path)

    grab = await grabs_storage.get_grab(db, event.grab_id)
    announce_author = grab.author_blob if grab else ""
    announce_title = grab.torrent_name if grab else ""

    metadata = BookMetadata(
        title=file_metadata.title or announce_title or "",
        author=announce_author or file_metadata.author or "",
        series=file_metadata.series,
        series_index=file_metadata.series_index,
        language=file_metadata.language,
        publisher=file_metadata.publisher,
        description=file_metadata.description,
        isbn=file_metadata.isbn,
        format=file_metadata.format,
    )

    # Tier 4: enrich via online metadata sources (Goodreads, etc.).
    # Only runs when an enricher was passed AND the enricher itself
    # is enabled. Result fills nulls in `metadata` — we never
    # overwrite values we already have from embedded metadata.
    #
    # Short-circuit: if the grab was submitted via AthenaScout's
    # /from-athenascout endpoint with a pre-baked metadata bundle
    # (plan item 1.2), use that INSTEAD of calling the enricher.
    # Saves 6 outbound scraper requests per book and guarantees
    # consistency between the two apps. If AS metadata exists but
    # is malformed JSON, fall through to the normal enricher path.
    enriched: Optional[MetaRecord] = None
    as_metadata_raw = await grabs_storage.get_source_metadata(db, event.grab_id)
    if as_metadata_raw:
        try:
            as_meta = json.loads(as_metadata_raw)
            enriched = MetaRecord(
                title=as_meta.get("title") or "",
                authors=[as_meta.get("author")] if as_meta.get("author") else [],
                series=as_meta.get("series_name"),
                series_index=as_meta.get("series_index"),
                isbn=as_meta.get("isbn"),
                language=as_meta.get("language"),
                publisher=as_meta.get("publisher"),
                description=as_meta.get("description"),
                cover_url=as_meta.get("cover_url"),
                page_count=as_meta.get("page_count"),
                source="athenascout",
                confidence=1.0,  # AS scanned the user's library; trust it
            )
            _log.debug(
                "pipeline: grab_id=%d using AthenaScout-provided metadata (enricher skipped)",
                event.grab_id,
            )
        except (ValueError, TypeError, KeyError):
            _log.warning(
                "pipeline: grab_id=%d has malformed source_metadata; falling back to enricher",
                event.grab_id,
            )
            enriched = None

    if enriched is None and metadata_enricher is not None:
        grab_category = grab.category if grab else ""
        is_audiobook = _is_audiobook_grab(book_format, grab_category)
        try:
            enriched = await metadata_enricher.enrich(
                title=metadata.title,
                author=metadata.author,
                mam_torrent_id=grab.mam_torrent_id if grab else "",
                mam_token=_get_mam_token(),
                audiobook=is_audiobook,
            )
        except Exception:
            _log.exception(
                "pipeline: enricher crashed for grab_id=%d (non-fatal)",
                event.grab_id,
            )
            enriched = None

    if enriched is not None:
        metadata = BookMetadata(
            title=metadata.title or enriched.title or "",
            author=metadata.author or ", ".join(enriched.authors) or "",
            series=metadata.series or enriched.series,
            series_index=metadata.series_index or enriched.series_index,
            language=metadata.language or enriched.language,
            publisher=metadata.publisher or enriched.publisher,
            description=metadata.description or enriched.description,
            isbn=metadata.isbn or enriched.isbn,
            format=metadata.format,
        )

    await pipe_storage.set_state(
        db, run_id, pipe_storage.PIPE_METADATA_DONE,
        metadata_title=metadata.title or None,
        metadata_author=metadata.author or None,
        metadata_series=metadata.series or None,
        metadata_language=metadata.language or None,
    )

    # Patch metadata into a temp copy of the epub so the seeding
    # original is untouched.
    delivery_source = book_path
    temp_dir: Optional[Path] = None
    if (
        book_path.exists()
        and book_path.suffix.lower() == ".epub"
        and metadata.author
    ):
        temp_dir = Path(tempfile.mkdtemp(prefix="seshat-patch-"))
        try:
            temp_book = temp_dir / book_path.name
            shutil.copy2(str(book_path), str(temp_book))
            authors = [a.strip() for a in metadata.author.split(",") if a.strip()]
            # Description and language have been present on BookMetadata
            # since v1.0 but the initial staging patch was passing only
            # title/authors/series/series_index. As a result the enricher's
            # description + language fields were stored in the review-queue
            # row + shown in the UI but never written to the OPF — books
            # would land in Calibre with a blank description even though
            # Seshat's review card displayed a full synopsis.
            patched_ok = patch_epub_metadata(
                temp_book,
                title=metadata.title or None,
                authors=authors if authors else None,
                series=metadata.series or None,
                series_index=metadata.series_index or None,
                language=metadata.language or None,
                description=metadata.description or None,
            )
            if patched_ok:
                delivery_source = temp_book
                _log.debug(
                    "pipeline: patched epub metadata for grab_id=%d",
                    event.grab_id,
                )
        except Exception:
            _log.exception(
                "pipeline: failed to patch epub for grab_id=%d, "
                "using original file", event.grab_id,
            )

    return _PreparedBook(
        book_path=book_path,
        book_filename=book_filename,
        book_format=book_format,
        metadata=metadata,
        announce_author=announce_author,
        delivery_source=delivery_source,
        temp_dir=temp_dir,
        cleanup_temp=True,
        enriched=enriched,
    )


async def _stage_for_review(
    db: aiosqlite.Connection,
    event: CompletionEvent,
    prep: _PreparedBook,
    *,
    review_staging_path: str,
    ntfy_url: str,
    ntfy_topic: str,
    per_event_notifications: bool = False,
) -> bool:
    """Move the patched file into the review staging dir and insert a
    `book_review_queue` row. Pipeline transitions to awaiting_review.
    """
    run_id = event.pipeline_run_id

    if not review_staging_path:
        await _fail(db, run_id, event,
                    "review_queue_enabled but review_staging_path not configured",
                    ntfy_url, ntfy_topic)
        return False

    try:
        target_dir = Path(review_staging_path) / f"grab-{event.grab_id}"
        target_dir.mkdir(parents=True, exist_ok=True)
        # Copy the (possibly patched) delivery source into the review
        # staging dir. Don't move the temp file — keep _prepare_book's
        # cleanup semantics simple.
        src = prep.delivery_source
        dest = target_dir / src.name
        if src.exists():
            shutil.copy2(str(src), str(dest))
        else:
            await _fail(db, run_id, event,
                        "prepared book file missing before review staging",
                        ntfy_url, ntfy_topic)
            return False

        # Multi-file audiobook support: also copy every OTHER book-
        # format file from the original staging dir so the review
        # directory is self-contained. Without this a 26-file
        # Audible rip arrived in Review with only the primary MP3
        # (usually the last alphabetical part) and ABS ended up
        # with a broken 1-chapter book. Uses prep.book_path.parent
        # — that's the per-torrent staging subdir populated by
        # copy_to_staging. For single-file ebooks this loop runs
        # once and no-ops.
        staging_parent = prep.book_path.parent
        if staging_parent.exists() and staging_parent.is_dir():
            from app.orchestrator.file_copier import BOOK_EXTENSIONS
            for sibling in staging_parent.iterdir():
                if not sibling.is_file():
                    continue
                if sibling.name == prep.book_filename:
                    continue  # primary already staged via delivery_source
                ext = sibling.suffix.lstrip(".").lower()
                if ext not in BOOK_EXTENSIONS:
                    continue
                sibling_dest = target_dir / sibling.name
                if sibling_dest.exists():
                    continue
                shutil.copy2(str(sibling), str(sibling_dest))
    except Exception as e:
        _log.exception("pipeline: review staging copy failed")
        await _fail(db, run_id, event,
                    f"review staging copy failed: {type(e).__name__}: {e}",
                    ntfy_url, ntfy_topic)
        return False
    finally:
        if prep.cleanup_temp and prep.temp_dir and prep.temp_dir.exists():
            shutil.rmtree(str(prep.temp_dir), ignore_errors=True)

    # Fetch cover images. MAM poster is the primary (authoritative),
    # Goodreads/enricher cover is the alternative the user can choose.
    # Both are best-effort — missing covers aren't pipeline failures.
    from app.metadata.covers import fetch_mam_cover

    mam_cover_str: Optional[str] = None
    enricher_cover_str: Optional[str] = None
    grab = await grabs_storage.get_grab(db, event.grab_id)

    # MAM cover (primary): uses the CDN poster endpoint + cookie auth.
    if grab and grab.mam_torrent_id:
        try:
            mam_token = _get_mam_token()
            mam_path = await fetch_mam_cover(
                grab.mam_torrent_id,
                dest_dir=target_dir,
                basename="cover-mam",
                token=mam_token,
            )
            if mam_path is not None:
                mam_cover_str = str(mam_path)
        except Exception:
            _log.exception(
                "pipeline: MAM cover fetch crashed for grab_id=%d", event.grab_id
            )

    # Enricher cover (alternative): from Goodreads or other scrapers.
    if prep.enriched and prep.enriched.cover_url:
        try:
            enricher_path = await fetch_cover(
                prep.enriched.cover_url,
                dest_dir=target_dir,
                basename="cover-enriched",
            )
            if enricher_path is not None:
                enricher_cover_str = str(enricher_path)
        except Exception:
            _log.exception(
                "pipeline: enricher cover fetch crashed for grab_id=%d", event.grab_id
            )

    # Use MAM cover as the primary, enricher as fallback.
    cover_path_str = mam_cover_str or enricher_cover_str

    # Insert the review queue row. Metadata serialized as plain dict,
    # merged with the enriched source record so the UI can display
    # both provider-side fields (description, page count, etc.) and
    # the embedded-file values.
    metadata_dict = {k: v for k, v in asdict(prep.metadata).items() if v is not None}
    if prep.enriched is not None:
        enriched_dict = prep.enriched.to_dict()
        metadata_dict["enriched"] = enriched_dict
    # Store both cover paths so the UI can show both + let user pick.
    metadata_dict["cover_mam"] = mam_cover_str
    metadata_dict["cover_enriched"] = enricher_cover_str
    await review_storage.create_entry(
        db,
        grab_id=event.grab_id,
        pipeline_run_id=run_id,
        staged_path=str(target_dir),
        book_filename=dest.name,
        book_format=prep.book_format,
        metadata=metadata_dict,
        cover_path=cover_path_str,
    )
    await pipe_storage.set_state(db, run_id, pipe_storage.PIPE_AWAITING_REVIEW)
    await grabs_storage.set_state(
        db, event.grab_id, grabs_storage.STATE_PROCESSING
    )

    _log.debug(
        "pipeline: staged for review grab_id=%d %s → %s",
        event.grab_id, event.torrent_name, dest,
    )

    if per_event_notifications and ntfy_url and ntfy_topic:
        try:
            await ntfy.notify_pipeline_complete(
                ntfy_url, ntfy_topic,
                event.torrent_name, "review_queue",
            )
        except Exception:
            _log.exception("ntfy review-queue notify failed (non-fatal)")

    return True


async def _deliver_prepared(
    db: aiosqlite.Connection,
    event: CompletionEvent,
    prep: _PreparedBook,
    *,
    default_sink: str,
    calibre_library_path: str,
    folder_sink_path: str,
    audiobookshelf_library_path: str,
    cwa_ingest_path: str,
    ntfy_url: str,
    ntfy_topic: str,
    auto_train_enabled: bool,
    review_id: Optional[int],
    was_timeout: bool,
    per_event_notifications: bool = False,
    abs_base_url: str = "",
    abs_api_key: str = "",
    abs_library_id: str = "",
) -> bool:
    """Steps 6-9: sink delivery, auto-train, counter, notify."""
    run_id = event.pipeline_run_id

    sink = _pick_sink(
        default_sink, calibre_library_path,
        folder_sink_path, audiobookshelf_library_path,
        cwa_ingest_path,
        abs_base_url=abs_base_url,
        abs_api_key=abs_api_key,
        abs_library_id=abs_library_id,
        book_format=prep.book_format,
    )

    try:
        if prep.delivery_source.exists():
            sink_result = await sink.deliver(
                str(prep.delivery_source), prep.metadata
            )
        else:
            sink_result = SinkResult(
                success=False,
                sink_name=sink.name,
                error="no book file to deliver",
            )
    finally:
        if prep.cleanup_temp and prep.temp_dir and prep.temp_dir.exists():
            shutil.rmtree(str(prep.temp_dir), ignore_errors=True)

    if not sink_result.success:
        await _fail(db, run_id, event,
                    f"sink {sink_result.sink_name} failed: {sink_result.error}",
                    ntfy_url, ntfy_topic)
        return False

    await pipe_storage.set_state(
        db, run_id, pipe_storage.PIPE_SUNK,
        sink_name=sink_result.sink_name,
        sink_result=sink_result.detail,
    )

    if auto_train_enabled:
        author_blob = prep.announce_author or prep.metadata.author or ""
        if author_blob:
            added = await train_authors_from_blob(db, author_blob)
            if added:
                _log.debug(
                    "pipeline: auto-trained %d author(s) from %s",
                    added, event.torrent_name,
                )

    await pipe_storage.set_state(db, run_id, pipe_storage.PIPE_COMPLETE)
    await grabs_storage.set_state(
        db, event.grab_id, grabs_storage.STATE_COMPLETE
    )

    # Record the Calibre-additions counter for digest reporting.
    try:
        await calibre_adds_storage.record_addition(
            db,
            grab_id=event.grab_id,
            review_id=review_id,
            title=prep.metadata.title or None,
            author=prep.metadata.author or None,
            sink_name=sink_result.sink_name,
            was_timeout=was_timeout,
        )
    except Exception:
        _log.exception("calibre_additions record failed (non-fatal)")

    _log.info(
        "pipeline: complete grab_id=%d %s → %s",
        event.grab_id, event.torrent_name, sink_result.sink_name,
    )

    if per_event_notifications and ntfy_url and ntfy_topic:
        try:
            await ntfy.notify_pipeline_complete(
                ntfy_url, ntfy_topic,
                event.torrent_name, sink_result.sink_name,
            )
        except Exception:
            _log.exception("ntfy pipeline-complete notify failed (non-fatal)")

    return True


# ─── Review-queue resume entrypoint ─────────────────────────────


async def deliver_reviewed(
    db: aiosqlite.Connection,
    *,
    review_id: int,
    default_sink: str,
    calibre_library_path: str,
    folder_sink_path: str,
    audiobookshelf_library_path: str = "",
    abs_base_url: str = "",
    abs_api_key: str = "",
    abs_library_id: str = "",
    cwa_ingest_path: str = "",
    ntfy_url: str = "",
    ntfy_topic: str = "",
    auto_train_enabled: bool = True,
    was_timeout: bool = False,
    per_event_notifications: bool = False,
) -> bool:
    """Deliver a reviewed book from the review queue to the sink.

    Called by:
      - the approve endpoint (user said yes)
      - the auto-add timeout job (grace period expired)
    """
    entry = await review_storage.get_entry(db, review_id)
    if entry is None:
        _log.warning("deliver_reviewed: review_id=%d not found", review_id)
        return False
    if entry.status != review_storage.STATUS_PENDING:
        _log.info(
            "deliver_reviewed: review_id=%d already in status %s",
            review_id, entry.status,
        )
        return False

    grab = await grabs_storage.get_grab(db, entry.grab_id)
    if grab is None:
        await review_storage.set_status(
            db, review_id, review_storage.STATUS_FAILED,
            decision_note="grab row missing",
        )
        return False

    staged = Path(entry.staged_path) / entry.book_filename
    if not staged.exists():
        _log.warning(
            "deliver_reviewed: review_id=%d staged file missing (%s)",
            review_id, staged,
        )
        await review_storage.set_status(
            db, review_id, review_storage.STATUS_FAILED,
            decision_note=f"staged file missing: {staged}",
        )
        return False

    # Multi-file audiobook backfill. Pre-v1.3 audiobooks arrived in
    # the review queue with only the primary MP3 staged (v1.2 bug);
    # if we don't repair now the sink can only deliver the one file
    # and ABS shows a broken 1-chapter book. Query qBit for the
    # torrent's full file list and copy any missing audio companions
    # from the original download location into the review dir before
    # the sink runs. Best-effort — qBit offline / torrent removed
    # falls through to delivering whatever IS present.
    if (
        _is_audiobook_grab(entry.book_format, grab.category or "")
        and grab.qbit_hash
        and state.dispatcher is not None
    ):
        try:
            await _backfill_audio_companions(
                db,
                review_dir=Path(entry.staged_path),
                primary_name=entry.book_filename,
                qbit_hash=grab.qbit_hash,
                pipeline_run_id=entry.pipeline_run_id,
            )
        except Exception:
            _log.exception(
                "deliver_reviewed: audio-companion backfill failed "
                "for review_id=%d (non-fatal)", review_id,
            )

    metadata = BookMetadata(
        title=entry.metadata.get("title", "") or "",
        author=entry.metadata.get("author", "") or "",
        series=entry.metadata.get("series"),
        series_index=entry.metadata.get("series_index"),
        language=entry.metadata.get("language"),
        publisher=entry.metadata.get("publisher"),
        description=entry.metadata.get("description"),
        isbn=entry.metadata.get("isbn"),
        format=entry.metadata.get("format"),
    )

    # Re-patch the staged epub with the review-queue metadata. The
    # file was already patched at staging time, but the user may
    # have edited title/author/description/etc. through the Review
    # page since then — without re-patching, the sink receives the
    # pre-edit file and those edits are silently lost (v1.2.0 bug).
    #
    # We patch a temp copy and point delivery_source at it so the
    # staged file stays intact for retries. If patching fails (rare
    # — non-epub format, zip corruption, etc.) we fall through to
    # the unpatched staged file rather than refusing delivery.
    delivery_source = staged
    patch_temp_dir: Optional[Path] = None
    if (
        staged.exists()
        and staged.suffix.lower() == ".epub"
        and metadata.author
    ):
        patch_temp_dir = Path(tempfile.mkdtemp(prefix="seshat-repatch-"))
        try:
            temp_book = patch_temp_dir / staged.name
            shutil.copy2(str(staged), str(temp_book))
            authors = [a.strip() for a in metadata.author.split(",") if a.strip()]
            patched_ok = patch_epub_metadata(
                temp_book,
                title=metadata.title or None,
                authors=authors if authors else None,
                series=metadata.series or None,
                series_index=metadata.series_index or None,
                language=metadata.language or None,
                description=metadata.description or None,
            )
            if patched_ok:
                delivery_source = temp_book
                _log.info(
                    "deliver_reviewed: re-patched epub with review-queue "
                    "edits for review_id=%d", review_id,
                )
        except Exception:
            _log.exception(
                "deliver_reviewed: re-patch failed for review_id=%d "
                "(non-fatal — delivering pre-edit file)", review_id,
            )

    prep = _PreparedBook(
        book_path=staged,
        book_filename=entry.book_filename,
        book_format=entry.book_format or "",
        metadata=metadata,
        announce_author=entry.metadata.get("author", "") or grab.author_blob,
        delivery_source=delivery_source,
        temp_dir=patch_temp_dir,
        cleanup_temp=patch_temp_dir is not None,
    )

    # Synthesize a CompletionEvent so _deliver_prepared can reuse
    # its existing pipeline-run bookkeeping.
    synthetic_event = CompletionEvent(
        grab_id=entry.grab_id,
        qbit_hash=grab.qbit_hash or "",
        torrent_name=grab.torrent_name,
        save_path=str(Path(entry.staged_path)),
        pipeline_run_id=entry.pipeline_run_id or 0,
    )

    ok = await _deliver_prepared(
        db, synthetic_event, prep,
        default_sink=default_sink,
        calibre_library_path=calibre_library_path,
        folder_sink_path=folder_sink_path,
        audiobookshelf_library_path=audiobookshelf_library_path,
        abs_base_url=abs_base_url,
        abs_api_key=abs_api_key,
        abs_library_id=abs_library_id,
        cwa_ingest_path=cwa_ingest_path,
        ntfy_url=ntfy_url,
        ntfy_topic=ntfy_topic,
        auto_train_enabled=auto_train_enabled,
        review_id=review_id,
        was_timeout=was_timeout,
        per_event_notifications=per_event_notifications,
    )

    if ok:
        await review_storage.set_status(
            db, review_id, review_storage.STATUS_DELIVERED,
            decision_note="timeout auto-add" if was_timeout else "approved",
        )
        # Clean up the review staging dir now that the book has
        # been delivered.
        try:
            review_dir = Path(entry.staged_path)
            if review_dir.exists():
                shutil.rmtree(str(review_dir), ignore_errors=True)
        except Exception:
            pass
    else:
        # Sink failed. Track the attempt count and either queue for
        # retry or dump to the emergency export folder.
        prev_note = entry.decision_note or ""
        attempt = 1
        if "sink_attempt:" in prev_note:
            try:
                attempt = int(prev_note.split("sink_attempt:")[1].split()[0]) + 1
            except (ValueError, IndexError):
                pass

        from app.config import load_settings
        settings = load_settings()
        max_retries = int(settings.get("sink_max_retries", 3))
        emergency_path = settings.get("emergency_export_path", "") or ""

        if attempt >= max_retries and emergency_path:
            # Max retries exceeded — dump to emergency folder.
            try:
                emer_dir = Path(emergency_path)
                emer_dir.mkdir(parents=True, exist_ok=True)
                staged = Path(entry.staged_path) / entry.book_filename
                if staged.exists():
                    dest = emer_dir / entry.book_filename
                    shutil.copy2(str(staged), str(dest))
                    _log.warning(
                        "pipeline: sink failed %d times for review_id=%d — "
                        "exported to emergency folder: %s",
                        attempt, review_id, dest,
                    )
            except Exception:
                _log.exception("pipeline: emergency export failed")
            await review_storage.set_status(
                db, review_id, review_storage.STATUS_FAILED,
                decision_note=f"sink failed after {attempt} attempts, exported to emergency folder",
            )
        else:
            # Queue for retry on next review-timeout tick.
            await review_storage.set_status(
                db, review_id, review_storage.STATUS_SINK_PENDING,
                decision_note=f"sink_attempt:{attempt} — will retry on next tick",
            )
            _log.info(
                "pipeline: sink delivery failed for review_id=%d (attempt %d/%d), "
                "queued for retry",
                review_id, attempt, max_retries,
            )

    return ok


# ─── Sink picker + failure recorder ─────────────────────────────


def _pick_sink(
    default_sink: str,
    calibre_library_path: str,
    folder_sink_path: str,
    audiobookshelf_library_path: str,
    cwa_ingest_path: str,
    *,
    abs_base_url: str = "",
    abs_api_key: str = "",
    abs_library_id: str = "",
    book_format: str = "",
    category: str = "",
):
    """Select the sink for a delivered book.

    `book_format` (file extension) and `category` (MAM category)
    enable audiobook-aware routing: when the book is an audiobook
    AND AudiobookshelfSink is configured (library path set), route
    to ABS regardless of `default_sink`. Falls back to the default
    sink when ABS isn't configured so ebook-only setups keep working
    without flipping their default.
    """
    if _is_audiobook_grab(book_format, category) and audiobookshelf_library_path:
        return AudiobookshelfSink(
            audiobookshelf_library_path,
            abs_base_url=abs_base_url,
            abs_api_key=abs_api_key,
            abs_library_id=abs_library_id,
        )
    if default_sink == "calibre":
        return CalibreSink(calibre_library_path)
    if default_sink == "cwa":
        return CWASink(cwa_ingest_path)
    if default_sink == "audiobookshelf":
        return AudiobookshelfSink(
            audiobookshelf_library_path,
            abs_base_url=abs_base_url,
            abs_api_key=abs_api_key,
            abs_library_id=abs_library_id,
        )
    return FolderSink(folder_sink_path)


async def _fail(
    db: aiosqlite.Connection,
    run_id: int,
    event: CompletionEvent,
    error: str,
    ntfy_url: str,
    ntfy_topic: str,
) -> None:
    _log.warning(
        "pipeline: failed grab_id=%d %s: %s",
        event.grab_id, event.torrent_name, error,
    )
    await pipe_storage.set_state(
        db, run_id, pipe_storage.PIPE_FAILED, error=error,
    )
    if ntfy_url and ntfy_topic:
        try:
            await ntfy.notify_error(ntfy_url, ntfy_topic, event.torrent_name, error)
        except Exception:
            _log.exception("ntfy error notify failed (non-fatal)")
