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
    cwa_min_inter_book_seconds: float = 10.0,
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
    ebook_format_priority: Optional[list[str]] = None,
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
        preps = await _prepare_book(
            db, event, staging_path=staging_path, run_id=run_id,
            ntfy_url=ntfy_url, ntfy_topic=ntfy_topic,
            metadata_enricher=metadata_enricher,
            torrent_files=torrent_files,
            audiobook_format_priority=audiobook_format_priority,
            ebook_format_priority=ebook_format_priority,
        )
        if not preps:
            return False

        # One download-complete notification per torrent (not per
        # group) — bundle children share the same torrent name and
        # spamming the ntfy channel N times would be noisy.
        if ntfy_url and ntfy_topic and ntfy.is_event_enabled("download_complete"):
            try:
                await ntfy.notify_download_complete(
                    ntfy_url, ntfy_topic,
                    event.torrent_name,
                    preps[0].metadata.author or "",
                )
            except Exception:
                _log.exception(
                    "per-event notify_download_complete failed (non-fatal)"
                )

        # Fan out: each group becomes one review-queue entry (or
        # one direct sink delivery in legacy review-off mode). The
        # status returned from process_completion is True iff EVERY
        # group succeeded — a partial bundle still surfaces failures
        # in the pipeline-run state for the failed children.
        results: list[bool] = []
        for prep in preps:
            if review_queue_enabled:
                ok = await _stage_for_review(
                    db, event, prep,
                    review_staging_path=review_staging_path,
                    ntfy_url=ntfy_url, ntfy_topic=ntfy_topic,
                    per_event_notifications=per_event_notifications,
                )
            else:
                ok = await _deliver_prepared(
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
                    cwa_min_inter_book_seconds=cwa_min_inter_book_seconds,
                    ntfy_url=ntfy_url,
                    ntfy_topic=ntfy_topic,
                    auto_train_enabled=auto_train_enabled,
                    review_id=None,
                    was_timeout=False,
                )
            results.append(ok)
        return all(results)
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
    """Internal carrier for the outputs of `_prepare_book`.

    v2.7.0: bundle/collection torrents fan out into one `_PreparedBook`
    per detected work. Single-book torrents still produce exactly one
    `_PreparedBook` with `bundle_total=1` and `bundle_index=0` — that
    shape is indistinguishable from the pre-v2.7 single-result. Bundle
    children carry `bundle_total>=2` plus `bundle_parent_grab_id`
    pointing at the parent grab so the review-queue audit trail flows
    through approval into future acquisition link-back.

    `group_files` is the list of staged paths belonging to this
    specific work — used by `_stage_for_review` to copy the right
    sibling files (multi-format variants, audiobook parts) into the
    per-group review staging subdir without dragging in unrelated
    bundle siblings.
    """
    __slots__ = (
        "book_path", "book_filename", "book_format",
        "metadata", "enriched", "announce_author",
        "delivery_source", "temp_dir", "cleanup_temp",
        "group_files", "bundle_index", "bundle_total",
        "bundle_parent_grab_id", "library_slug",
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
        group_files: Optional[list[Path]] = None,
        bundle_index: int = 0,
        bundle_total: int = 1,
        bundle_parent_grab_id: Optional[int] = None,
        library_slug: Optional[str] = None,
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
        # Default group_files to just the primary so single-book
        # callers don't need to think about it.
        self.group_files = group_files if group_files is not None else [book_path]
        self.bundle_index = bundle_index
        self.bundle_total = bundle_total
        self.bundle_parent_grab_id = bundle_parent_grab_id
        self.library_slug = library_slug


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
    ebook_format_priority: Optional[list[str]] = None,
) -> list[_PreparedBook]:
    """Steps 1-4: locate file, optional staging, metadata, patch.

    `torrent_files` is the list of file paths qBit reports for the
    completed torrent (from `list_torrent_files(hash)`), relative to
    `save_path`. When present, it's authoritative: we use those exact
    paths rather than guessing. When absent or empty we fall back to
    the older name-heuristic search (exact/prefix/substring match on
    `torrent_name`) for legacy clients and tests.

    Returns a list of `_PreparedBook` on success — one per detected
    work — or an empty list after recording a failure on the pipeline
    run. Single-book torrents return a 1-element list (the pre-v2.7
    shape after the caller indexes [0]); bundles return N elements.
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
                _apply_ebook_priority,
            )
            after_audio = _apply_audiobook_priority(
                sorted(matched_paths, key=lambda p: p.name.lower()),
                audiobook_format_priority,
            )
            book_files = _apply_ebook_priority(
                after_audio, ebook_format_priority,
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
                return []
            fallback_source = matched
        source = fallback_source
        book_files = await loop.run_in_executor(
            None,
            functools.partial(
                find_book_files, source,
                audiobook_priority=audiobook_format_priority,
                ebook_priority=ebook_format_priority,
            ),
        )

    if not book_files:
        await _fail(db, run_id, event,
                    f"no book files found in {source}",
                    ntfy_url, ntfy_topic)
        return []

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
    book_dir: Path
    if staging_path:
        explicit = book_files if torrent_files else None
        copy_result = await loop.run_in_executor(
            None,
            functools.partial(
                copy_to_staging,
                source, Path(staging_path), event.torrent_name,
                explicit_files=explicit,
                audiobook_priority=audiobook_format_priority,
                ebook_priority=ebook_format_priority,
            ),
        )
        if not copy_result.success:
            await _fail(db, run_id, event,
                        f"staging failed: {copy_result.error}",
                        ntfy_url, ntfy_topic)
            return []
        book_dir = Path(copy_result.staged_path)
        # Primary path resolution: copy_to_staging reports the largest /
        # format-priority winner, but for a bundle that's only the
        # primary of group 0 — every other group's primary needs to be
        # resolved separately below.
        book_filename = copy_result.book_filename or book_filename
        book_format = copy_result.book_format or book_format
        await pipe_storage.set_state(
            db, run_id, pipe_storage.PIPE_EXTRACTED,
            staged_path=str(book_dir),
            book_filename=book_filename,
            book_format=book_format,
        )
    else:
        # No staging — files stay where qBit left them.
        book_dir = primary_book.parent
        await pipe_storage.set_state(
            db, run_id, pipe_storage.PIPE_EXTRACTED,
            staged_path=str(source),
            book_filename=book_filename,
            book_format=book_format,
        )

    # ── Bundle classification ────────────────────────────────────
    # Group the located book files into one or more `BookGroup`s.
    # Single-book and multi-format-same-book and multi-part-audiobook
    # all resolve to ONE group (preserving pre-v2.7 behavior); only
    # actual bundle torrents fan out into multiple groups.
    from app.config import load_settings
    from app.orchestrator.bundle_classifier import classify as classify_bundle
    settings = load_settings()
    bundle_enabled = bool(settings.get("bundle_detection_enabled", True))

    grab = await grabs_storage.get_grab(db, event.grab_id)
    announce_author = grab.author_blob if grab else ""
    announce_title = grab.torrent_name if grab else ""

    # Run the classifier on the original (pre-staging) paths — they
    # share basenames with the staged copies, so the classifier's
    # metadata extraction works regardless of which dir we point at.
    # Resolve each group's primary back to its staged location below.
    groups = classify_bundle(
        book_files,
        enabled=bundle_enabled,
        announce_title=announce_title,
        announce_author=announce_author,
    )
    if not groups:
        await _fail(db, run_id, event,
                    "bundle classifier returned no groups (unexpected)",
                    ntfy_url, ntfy_topic)
        return []
    is_bundle = len(groups) > 1

    if is_bundle:
        _log.info(
            "pipeline: grab_id=%d is a bundle of %d works",
            event.grab_id, len(groups),
        )

    # Read source_metadata once — it's torrent-level, so all groups
    # would see the same bundle-level data. For bundles we DON'T use
    # it because the prebaked metadata describes the bundle as a
    # whole. Single-book grabs use it as before.
    prebaked_raw = await grabs_storage.get_source_metadata(db, event.grab_id)

    prepared: list[_PreparedBook] = []
    for group_idx, group in enumerate(groups):
        # Resolve this group's files to their staged locations. After
        # copy_to_staging every file lives under `book_dir` with its
        # original basename.
        group_staged: list[Path] = []
        for f in group.files:
            staged = book_dir / f.name
            if staged.exists():
                group_staged.append(staged)
            else:
                # File didn't make it through staging — log and skip
                # (rare; the legacy code would silently lose it too).
                _log.warning(
                    "pipeline: group %d primary %s not in staging dir %s",
                    group_idx, f.name, book_dir,
                )
        if not group_staged:
            _log.warning(
                "pipeline: skipping empty group %d for grab_id=%d",
                group_idx, event.grab_id,
            )
            continue
        primary_staged = group_staged[0]
        group_book_filename = primary_staged.name
        group_book_format = primary_staged.suffix.lstrip(".").lower()

        prep = await _prepare_group(
            db, event,
            group=group,
            group_files=group_staged,
            primary_path=primary_staged,
            book_filename=group_book_filename,
            book_format=group_book_format,
            grab=grab,
            announce_author=announce_author,
            announce_title=announce_title,
            prebaked_raw=prebaked_raw if not is_bundle else None,
            metadata_enricher=metadata_enricher,
            is_bundle=is_bundle,
            bundle_index=group_idx,
            bundle_total=len(groups),
            run_id=run_id,
        )
        if prep is not None:
            prepared.append(prep)

    return prepared


async def _prepare_group(
    db: aiosqlite.Connection,
    event: CompletionEvent,
    *,
    group,  # BookGroup, not imported at module level to avoid circularity
    group_files: list[Path],
    primary_path: Path,
    book_filename: str,
    book_format: str,
    grab,
    announce_author: str,
    announce_title: str,
    prebaked_raw: Optional[str],
    metadata_enricher: Optional[MetadataEnricher],
    is_bundle: bool,
    bundle_index: int,
    bundle_total: int,
    run_id: int,
) -> Optional[_PreparedBook]:
    """Build one `_PreparedBook` for a single classified book group.

    Single-book grabs call this once with `is_bundle=False` and the
    semantics match the pre-v2.7 flow exactly. Bundle children call
    this once per group with `is_bundle=True`, which switches the
    metadata-merge seed to use the group's extracted title (not the
    bundle's announce title) and tells the enricher to skip MAM (since
    the parent grab's MAM listing describes the bundle as a whole,
    not the individual child book).
    """

    # Extract metadata from the per-group primary. For single-book
    # callers the classifier already extracted this; reuse it to
    # avoid a redundant file read.
    file_metadata = group.extracted if group.extracted else BookMetadata()
    if not file_metadata.title and primary_path.exists():
        try:
            file_metadata = extract_metadata(primary_path)
        except Exception:
            _log.exception(
                "pipeline: extract failed for %s in group %d",
                primary_path, bundle_index,
            )

    if is_bundle:
        # Bundle child: prefer the per-book extracted title over the
        # bundle's announce title. The announce title is bundle-level
        # ("Mistborn Trilogy") and would mislabel every child.
        metadata = BookMetadata(
            title=file_metadata.title or "",
            author=file_metadata.author or announce_author or "",
            series=file_metadata.series,
            series_index=file_metadata.series_index,
            language=file_metadata.language,
            publisher=file_metadata.publisher,
            description=file_metadata.description,
            isbn=file_metadata.isbn,
            format=file_metadata.format,
        )
    else:
        # Single-book: existing precedence (file > announce for title,
        # announce > file for author — matches pre-v2.7 behavior).
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
    # Short-circuit: if the grab arrived with a pre-baked metadata
    # bundle on `grabs.source_metadata` (from the discovery domain's
    # send-to-pipeline flow or the external grabs endpoint), use that
    # INSTEAD of calling the enricher. Saves 6 outbound scraper
    # requests per book. If the bundle exists but is malformed JSON,
    # fall through to the normal enricher path. Bundle children skip
    # the prebaked path entirely (the prebaked metadata is bundle-
    # level and would mislabel every child).
    enriched: Optional[MetaRecord] = None
    if prebaked_raw and not is_bundle:
        try:
            prebaked = json.loads(prebaked_raw)
            # Surface which discovery-side sources contributed to this
            # record (set by `/discovery/send-to-pipeline` when packing
            # the bundle). Renders on the review card as
            # "via discovery (goodreads, hardcover) · 100%" instead of
            # the opaque "via source_metadata". Falls back to plain
            # "discovery" when the bundle predates the sources_used
            # field or couldn't be populated.
            sources_used = prebaked.get("sources_used")
            if (
                isinstance(sources_used, list)
                and all(isinstance(s, str) for s in sources_used)
                and sources_used
            ):
                source_label = f"discovery ({', '.join(sources_used)})"
            else:
                source_label = "discovery"
            enriched = MetaRecord(
                title=prebaked.get("title") or "",
                authors=[prebaked.get("author")] if prebaked.get("author") else [],
                series=prebaked.get("series_name"),
                series_index=prebaked.get("series_index"),
                isbn=prebaked.get("isbn"),
                language=prebaked.get("language"),
                publisher=prebaked.get("publisher"),
                description=prebaked.get("description"),
                cover_url=prebaked.get("cover_url"),
                page_count=prebaked.get("page_count"),
                source=source_label,
                confidence=1.0,  # submitter vouched for this metadata; trust it
            )
            _log.debug(
                "pipeline: grab_id=%d using pre-baked source_metadata (enricher skipped)",
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
        # v2.13.2: anchor GoodreadsSource's T4/T5 resolver tiers with
        # the author's stored goodreads_id when we have one. Empty
        # string is fine — the resolver tiers no-op on absent anchor.
        from app.metadata.author_lookup import get_goodreads_id_for_author
        author_goodreads_id = await get_goodreads_id_for_author(metadata.author)
        try:
            enriched = await metadata_enricher.enrich(
                title=metadata.title,
                author=metadata.author,
                # v2.17.3: thread the file-embedded ISBN/ASIN into
                # the resolver chain. Without this seed, identifier-
                # gated tiers (Goodreads T2 ISBN, T1 ASIN) only fire
                # when a prior source populates merged.isbn/asin —
                # which fails when the MAM upload form was left
                # blank but the .epub itself has the ISBN. ASIN
                # piggy-backs off `file_metadata` (m4b ASIN tags)
                # since BookMetadata above doesn't currently carry
                # it forward.
                isbn=metadata.isbn or "",
                asin=file_metadata.asin or "",
                # Bundle children: don't pass mam_torrent_id (the
                # bundle's torrent_id describes the bundle as a whole,
                # not the child book). Also flip skip_mam=True so the
                # default-priority MAM source doesn't fall back to a
                # fuzzy text search that would hit the bundle listing.
                mam_torrent_id="" if is_bundle else (grab.mam_torrent_id if grab else ""),
                mam_token="" if is_bundle else _get_mam_token(),
                audiobook=is_audiobook,
                skip_mam=is_bundle,
                author_goodreads_id=author_goodreads_id,
            )
        except Exception:
            _log.exception(
                "pipeline: enricher crashed for grab_id=%d group %d (non-fatal)",
                event.grab_id, bundle_index,
            )
            enriched = None

    if enriched is not None:
        if is_bundle:
            # Bundle children: per the design, MAM data is bundle-
            # level and effectively drops to last priority. Goodreads
            # / Hardcover / Audible win when they have differing data
            # for a given field; only fall back to the seed when no
            # source filled the gap. Implemented as "enriched wins on
            # any non-empty value" (the inverse of the single-book
            # rule, which has metadata winning).
            metadata = BookMetadata(
                title=enriched.title or metadata.title or "",
                author=(
                    ", ".join(enriched.authors)
                    if enriched.authors
                    else metadata.author
                ) or "",
                series=enriched.series or metadata.series,
                series_index=enriched.series_index or metadata.series_index,
                language=enriched.language or metadata.language,
                publisher=enriched.publisher or metadata.publisher,
                description=enriched.description or metadata.description,
                isbn=enriched.isbn or metadata.isbn,
                format=metadata.format,
            )
        else:
            # Single-book: existing precedence (file-embedded wins;
            # enricher fills nulls only) — preserves pre-v2.7 behavior.
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

    # The pipeline_run state stays at PIPE_METADATA_DONE per torrent
    # — only the first group's metadata advances the run state so
    # downstream watchers see a single state-machine transition. The
    # other groups' metadata still lands on each row's review entry.
    if bundle_index == 0:
        await pipe_storage.set_state(
            db, run_id, pipe_storage.PIPE_METADATA_DONE,
            metadata_title=metadata.title or None,
            metadata_author=metadata.author or None,
            metadata_series=metadata.series or None,
            metadata_language=metadata.language or None,
        )

    # Patch metadata into a temp copy of the epub so the seeding
    # original is untouched. Per-group temp_dir so bundle siblings
    # don't share state.
    delivery_source = primary_path
    temp_dir: Optional[Path] = None
    if (
        primary_path.exists()
        and primary_path.suffix.lower() == ".epub"
        and metadata.author
    ):
        temp_dir = Path(tempfile.mkdtemp(prefix="seshat-patch-"))
        try:
            temp_book = temp_dir / primary_path.name
            shutil.copy2(str(primary_path), str(temp_book))
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
                    "pipeline: patched epub metadata for grab_id=%d group %d",
                    event.grab_id, bundle_index,
                )
        except Exception:
            _log.exception(
                "pipeline: failed to patch epub for grab_id=%d group %d, "
                "using original file", event.grab_id, bundle_index,
            )

    return _PreparedBook(
        book_path=primary_path,
        book_filename=book_filename,
        book_format=book_format,
        metadata=metadata,
        announce_author=announce_author,
        delivery_source=delivery_source,
        temp_dir=temp_dir,
        cleanup_temp=True,
        enriched=enriched,
        group_files=group_files,
        bundle_index=bundle_index,
        bundle_total=bundle_total,
        bundle_parent_grab_id=event.grab_id if is_bundle else None,
        library_slug=None,  # reserved for future multi-library routing
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
        # Single-group grabs (the common case) land in `grab-{id}/`
        # for backwards compatibility with in-flight review queues
        # and existing review-staging dirs. Bundle children land in
        # `grab-{id}/group-{i}/` so siblings of different works don't
        # cross-contaminate each other's staging dir.
        if prep.bundle_total > 1:
            target_dir = (
                Path(review_staging_path)
                / f"grab-{event.grab_id}"
                / f"group-{prep.bundle_index}"
            )
        else:
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

        # Multi-file group support: copy every OTHER book-format file
        # in this group's file list (NOT the whole torrent's staging
        # dir — for bundles that would drag in unrelated works). For
        # single-book groups `group_files` contains just the primary
        # so this loop no-ops. For multi-part audiobooks it copies the
        # other 25 m4b parts. For multi-format ebooks (epub+mobi+azw3)
        # it copies the other formats.
        for sibling in prep.group_files:
            if not sibling.is_file():
                continue
            if sibling.name == prep.book_filename:
                continue  # primary already staged via delivery_source
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

    # v2.17.7 — surface owned-book duplicates in the review UI. If the
    # user already owns this book under a row with no confirmed MAM
    # URL, render a banner with a "Claim torrent for owned" action.
    # Same matcher the announce-time claim uses, so what the UI sees
    # is consistent with what dispatch would have caught earlier (the
    # announce hook only catches the no-ambiguity case; this surface
    # additionally helps with multi-owned-row ambiguity).
    try:
        from app.orchestrator.owned_announce_claim import find_owned_matches
        owned_matches = await find_owned_matches(
            title=prep.metadata.title or "",
            author_blob=prep.metadata.author or "",
            category=(grab.category if grab else "") or "",
        )
        if owned_matches:
            metadata_dict["duplicate_of_owned"] = [
                {
                    "library_slug": m.library_slug,
                    "book_id": m.book_id,
                    "title": m.title,
                    "mam_status": m.mam_status,
                    "calibre_id": m.calibre_id,
                }
                for m in owned_matches
            ]
    except Exception:
        _log.exception(
            "pipeline: duplicate-of-owned lookup crashed for grab_id=%d "
            "(non-fatal)", event.grab_id,
        )

    await review_storage.create_entry(
        db,
        grab_id=event.grab_id,
        pipeline_run_id=run_id,
        staged_path=str(target_dir),
        book_filename=dest.name,
        book_format=prep.book_format,
        metadata=metadata_dict,
        cover_path=cover_path_str,
        bundle_group_id=f"grab-{event.grab_id}",
        bundle_index=prep.bundle_index,
        bundle_total=prep.bundle_total,
        library_slug=prep.library_slug,
        bundle_parent_grab_id=prep.bundle_parent_grab_id,
    )
    # Pipeline-run state only advances on the LAST group so that
    # external watchers (review-timeout, dispatcher status) see the
    # awaiting_review transition once per torrent, not per child.
    if prep.bundle_index == prep.bundle_total - 1:
        await pipe_storage.set_state(db, run_id, pipe_storage.PIPE_AWAITING_REVIEW)
        await grabs_storage.set_state(
            db, event.grab_id, grabs_storage.STATE_PROCESSING
        )

    _log.debug(
        "pipeline: staged for review grab_id=%d %s → %s",
        event.grab_id, event.torrent_name, dest,
    )

    # v2.12.0 — gate on `notify_on_review_queued`. Pre-v2.12.0 this
    # fired whenever per_event_notifications was on, with no per-event
    # opt-out. Users who wanted "added to library" pushes but not
    # "ready for review" pushes had no UI knob.
    if ntfy_url and ntfy_topic and ntfy.is_event_enabled("review_queued"):
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
    cwa_min_inter_book_seconds: float,
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
        cwa_min_inter_book_seconds=cwa_min_inter_book_seconds,
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

    # v2.12.0 — gate on `notify_on_library_ingest`. This is the
    # "Added to Calibre / Audiobookshelf / …" push — fires only after
    # the book actually lands in the user's library app. Mark wanted a
    # discrete toggle for this so users could mute the download/grab
    # pushes while keeping the "book is now in your library" signal,
    # or vice versa.
    if ntfy_url and ntfy_topic and ntfy.is_event_enabled("library_ingest"):
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
    cwa_min_inter_book_seconds: float = 10.0,
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
    #
    # SKIP for v2.7.0 bundle children: each group's review staging
    # dir was populated with only THAT group's files at staging time
    # (see `_stage_for_review` `prep.group_files` loop). Running the
    # qBit-side backfill would re-introduce other bundle siblings'
    # audio files into this child's staging dir, since qBit reports
    # every file in the torrent — not just this group's. Detected
    # via `bundle_total > 1` on the review entry.
    if (
        _is_audiobook_grab(entry.book_format, grab.category or "")
        and grab.qbit_hash
        and state.dispatcher is not None
        and entry.bundle_total <= 1
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
        cwa_min_inter_book_seconds=cwa_min_inter_book_seconds,
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
    cwa_min_inter_book_seconds: float = 10.0,
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
        return CWASink(cwa_ingest_path, cwa_min_inter_book_seconds)
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
    # v2.11.1 N1: error notifications go through the same master +
    # per-event gate as the other pipeline events. Pre-fix this call
    # site bypassed both gates entirely, so users who explicitly
    # disabled "Pipeline errors" in Settings still received them.
    if ntfy_url and ntfy_topic and ntfy.is_event_enabled("pipeline_error"):
        try:
            await ntfy.notify_error(ntfy_url, ntfy_topic, event.torrent_name, error)
        except Exception:
            _log.exception("ntfy error notify failed (non-fatal)")
