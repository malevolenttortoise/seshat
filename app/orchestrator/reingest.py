"""
Reingest already-snatched torrents from disk into the Seshat pipeline.

The "Send to pipeline" button on Discovery's MAM-matched books hides
when MAM reports `my_snatched=true` — the user already pulled that
torrent at some point (often long before Seshat existed). For those
cases the file already lives on disk (or in qBit) and the user wants
to flow it through enrichment + manual review WITHOUT re-snatching
from MAM (snatch-safety rule: never re-download the same .torrent).

This module discovers where the existing snatch lives and hands the
result to the same pipeline path a fresh grab would take — minus
the MAM .torrent fetch and qBit submit steps. Two resolvers, in
priority order:

  1. **qBit candidates** — walk qBit's `list_torrents()` and match by
     torrent name (exact, then prefix/substring fallback). For each
     match we know the save_path + file list authoritatively without
     touching the disk. Preferred because qBit is the source of truth
     for in-flight seeding torrents.
  2. **Filesystem candidates** — recursively walk the configured
     `qbit_download_path` (translated to Seshat's mount via
     `qbit_path_prefix`/`local_path_prefix`) looking for files or
     directories whose name matches the MAM torrent name. Used for
     "grandfather'd" snatches that predate Seshat: there's no qBit
     hash recorded anywhere, but the file may still be on disk.

A single match → start the pipeline immediately. Multiple matches →
return candidate list for user disambiguation. Zero matches →
return `found=False` so the UI can surface a "not found anywhere"
error (per the v2.8.0 design: option (a), no auto-fallback to a
re-snatch).
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import aiosqlite

# NOTE: `load_settings` is imported lazily inside `find_candidates`
# and `start_reingest` (rather than at module scope) so test-time
# `monkeypatch.setattr(app.config, "load_settings", ...)` actually
# takes effect. With a top-level `from app.config import load_settings`
# the local name binds at import time and ignores patches applied
# afterwards.
from app.orchestrator.download_folders import translate_path
from app.storage import grabs as grabs_storage
from app.storage import pipeline as pipe_storage

_log = logging.getLogger("seshat.orchestrator.reingest")


# Book file extensions used by the fs scanner. Mirrors
# `app.orchestrator.file_copier.BOOK_EXTENSIONS`.
_BOOK_EXTS = frozenset({
    "epub", "mobi", "azw", "azw3", "pdf",
    "m4b", "m4a", "mp3", "aax", "aa",
    "cbz", "cbr",
    "lit", "fb2", "djvu",
})

# Max candidates returned to the user for disambiguation. Per the
# v2.8.0 design Mark asked for "top 3-5 picks" so we cap at 5.
_MAX_CANDIDATES = 5

# Word-token regex for filename-similarity scoring on the fs side.
_TOKEN_RX = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class Candidate:
    """One discovered reingest source.

    `source` is "qbit" when the torrent is loaded in the client (and
    we have the infohash + authoritative file list) or "fs" when the
    files were located by directory walk only.

    `save_path` + `book_files` (relative basenames) match what
    `process_completion` already accepts via its `torrent_files`
    kwarg, so the downstream pipeline runs without any code changes.

    `display_path` is what the UI shows in the disambiguation modal.
    For qBit candidates it's the torrent name + save_path; for fs
    candidates it's the discovered directory or file path.

    `score` is an internal ranking number (higher = better) — exact
    name matches outrank substring matches; qBit candidates outrank
    fs candidates of equal name-score. Used to pick the auto-start
    candidate when exactly one outranks the rest, and to sort the
    list shown to the user when multiple tie.
    """

    source: str                       # "qbit" | "fs"
    display_path: str
    save_path: str
    book_files: list[str] = field(default_factory=list)
    qbit_hash: Optional[str] = None
    mtime: float = 0.0
    total_size: int = 0
    score: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        # Drop the score from the API surface — it's a ranking
        # internal, not something the UI should render.
        d.pop("score", None)
        return d


# ─── qBit-side resolution ────────────────────────────────────


async def find_qbit_candidates(
    dispatcher,
    *,
    mam_torrent_name: str,
) -> list[Candidate]:
    """List candidates from the live qBit client.

    Matches by torrent name (case-insensitive). Exact match outranks
    prefix outranks substring. Returns `[]` when qBit is unreachable
    or no name match exists — callers fall back to fs search.

    Per-candidate file list comes from `list_torrent_files()` so the
    pipeline knows the authoritative basenames + multi-file shape
    (no glob needed downstream). Save path is translated through
    `qbit_path_prefix` / `local_path_prefix` so Unraid-style setups
    where qBit and Seshat see the share at different mount points
    resolve correctly.
    """
    if dispatcher is None or not hasattr(dispatcher, "qbit") or dispatcher.qbit is None:
        return []
    if not mam_torrent_name:
        return []

    try:
        torrents = await dispatcher.qbit.list_torrents()
    except Exception:
        _log.exception("reingest: qbit list_torrents crashed")
        return []

    qbit_prefix = getattr(dispatcher, "qbit_path_prefix", "") or ""
    local_prefix = getattr(dispatcher, "local_path_prefix", "") or ""

    target = mam_torrent_name.strip().lower()
    candidates: list[Candidate] = []
    for t in torrents:
        score = _name_score(t.name, mam_torrent_name)
        if score <= 0:
            continue
        try:
            files = await dispatcher.qbit.list_torrent_files(t.hash)
        except Exception:
            _log.exception(
                "reingest: list_torrent_files crashed for hash=%s", t.hash,
            )
            files = []
        # Filter to book-format files only — qBit returns ancillary
        # files (.nfo, .jpg, .txt) too and the pipeline doesn't want
        # to see them in its `torrent_files` input.
        book_files = [
            f for f in files
            if Path(f).suffix.lstrip(".").lower() in _BOOK_EXTS
        ]
        if not book_files:
            # Torrent matches by name but contains no book files.
            # Skip — almost certainly a non-MAM torrent that happens
            # to share a name fragment.
            continue
        translated = translate_path(t.save_path, qbit_prefix, local_prefix)
        # qBit candidates rank above fs by +100 so a tie on name-score
        # always picks qBit (it has authoritative file list + hash).
        candidates.append(Candidate(
            source="qbit",
            display_path=f"qBit: {t.name} → {translated}",
            save_path=translated,
            book_files=book_files,
            qbit_hash=t.hash,
            mtime=float(t.added_on or 0),
            total_size=int(t.size or 0),
            score=score + 100,
        ))
    candidates.sort(key=lambda c: -c.score)
    return candidates[:_MAX_CANDIDATES]


# ─── Filesystem-side resolution ──────────────────────────────


def find_fs_candidates(
    download_root: str,
    *,
    mam_torrent_name: str,
    max_depth: int = 6,
) -> list[Candidate]:
    """Recursively search `download_root` for files/dirs matching the name.

    Two shapes a MAM snatch can take on disk:
      - Single-file torrent: a book file named like the torrent
        (or close — MAM filenames don't always match torrent names
        verbatim).
      - Multi-file torrent: a directory whose name matches the
        torrent name, containing the book files inside.

    Both shapes are tried. For directory matches we collect every
    book-format file inside via `rglob`. For file matches we wrap
    the lone file as a single-element book list. Results are scored
    by name similarity and capped at the top `_MAX_CANDIDATES`.

    `max_depth` caps recursion. With `[mam-complete]/[YYYY-MM]/`
    structure the depth from `download_root` is usually 2; we set
    the cap at 6 to handle deeper user-customized templates.
    """
    if not download_root or not mam_torrent_name:
        return []

    root = Path(download_root)
    if not root.exists() or not root.is_dir():
        _log.info(
            "reingest: fs root %r does not exist or is not a directory",
            download_root,
        )
        return []

    candidates: list[Candidate] = []
    target = mam_torrent_name.strip()

    # Walk: every entry in the tree up to max_depth. We capture both
    # files and directories; the matcher inspects both.
    for entry in _iter_tree(root, max_depth):
        score = _name_score(entry.name, target)
        if score <= 0:
            continue
        if entry.is_file():
            ext = entry.suffix.lstrip(".").lower()
            if ext not in _BOOK_EXTS:
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            candidates.append(Candidate(
                source="fs",
                display_path=str(entry),
                save_path=str(entry.parent),
                book_files=[entry.name],
                qbit_hash=None,
                mtime=stat.st_mtime,
                total_size=stat.st_size,
                score=score,
            ))
        elif entry.is_dir():
            # Gather every book file inside this directory.
            books: list[Path] = []
            try:
                for inner in entry.rglob("*"):
                    if not inner.is_file():
                        continue
                    ext = inner.suffix.lstrip(".").lower()
                    if ext in _BOOK_EXTS:
                        books.append(inner)
            except OSError:
                continue
            if not books:
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            # All files must live under `entry` so their rel-paths
            # are simple basenames the pipeline can resolve.
            book_files = [p.name for p in books]
            total = sum(b.stat().st_size for b in books if b.exists())
            candidates.append(Candidate(
                source="fs",
                display_path=str(entry),
                save_path=str(entry),
                book_files=book_files,
                qbit_hash=None,
                mtime=stat.st_mtime,
                total_size=int(total),
                score=score,
            ))

    # Dedupe — a directory match + its inner file match can both
    # land in the list. Prefer the directory (it carries more files)
    # so the user sees the multi-file shape if present.
    deduped: dict[str, Candidate] = {}
    for c in candidates:
        # Key on the directory containing the candidate's primary
        # so file-match-inside-dir-match collapses to the dir entry.
        key = c.save_path
        existing = deduped.get(key)
        if existing is None or len(c.book_files) > len(existing.book_files):
            deduped[key] = c

    final = sorted(deduped.values(), key=lambda c: -c.score)
    return final[:_MAX_CANDIDATES]


def _iter_tree(root: Path, max_depth: int):
    """Yield Path entries under `root` up to `max_depth` levels deep.

    Symlink loops are guarded against by sticking to `iterdir()` (no
    `follow_symlinks` traversal). Permission errors on individual
    subdirectories are logged and skipped, not raised.
    """
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        parent, depth = stack.pop()
        try:
            entries = list(parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            yield entry
            if entry.is_dir() and depth < max_depth:
                stack.append((entry, depth + 1))


# ─── Name-similarity scoring ────────────────────────────────


def _name_score(candidate_name: str, target_name: str) -> int:
    """Score how well `candidate_name` matches `target_name`.

    Returns a positive integer when there's a match (higher = better)
    or 0 when there's no usable similarity. Used to rank qBit list
    entries and filesystem nodes against the MAM torrent name.

    Tiers (descending):
      100  — exact case-insensitive match on the full name or its stem
       80  — candidate stem starts with the target (or vice versa)
       60  — target is a substring of the candidate (or vice versa)
       40  — token-set Jaccard ≥ 0.6 on word tokens
        0  — no plausible match

    The scoring is intentionally permissive: MAM torrent names
    sometimes include a year suffix or `(Unabridged)` decoration
    that the on-disk filename drops, and Audible rips strip the
    bracketed series tag. We prefer false-positive candidates the
    user can ignore over a false-negative "not found anywhere"
    when the file is in fact on disk.
    """
    if not candidate_name or not target_name:
        return 0
    a = _strip_ext(candidate_name).strip().lower()
    b = _strip_ext(target_name).strip().lower()
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a.startswith(b) or b.startswith(a):
        return 80
    if b in a or a in b:
        return 60
    a_tokens = set(_TOKEN_RX.findall(a))
    b_tokens = set(_TOKEN_RX.findall(b))
    if not a_tokens or not b_tokens:
        return 0
    inter = a_tokens & b_tokens
    union = a_tokens | b_tokens
    jaccard = len(inter) / len(union) if union else 0.0
    if jaccard >= 0.6:
        return 40
    return 0


def _strip_ext(name: str) -> str:
    """Strip a recognized book extension from `name` for comparison."""
    p = Path(name)
    if p.suffix.lstrip(".").lower() in _BOOK_EXTS:
        return p.stem
    return name


# ─── Combined resolver + pipeline kickoff ────────────────────


async def find_candidates(
    dispatcher,
    *,
    mam_torrent_name: str,
) -> list[Candidate]:
    """qBit + fs candidates, qBit listed first.

    qBit candidates always come back ranked above fs candidates of
    equal name-similarity (see `find_qbit_candidates` for the +100
    score bias). This means a single-result auto-start picks the
    qBit candidate when both sides match — the right default since
    qBit has authoritative save_path + file list and the user is
    almost always still seeding the torrent.
    """
    qbit = await find_qbit_candidates(
        dispatcher, mam_torrent_name=mam_torrent_name,
    )
    from app.config import load_settings
    settings = load_settings()
    qbit_root = settings.get("qbit_download_path", "") or ""
    qbit_prefix = settings.get("qbit_path_prefix", "") or ""
    local_prefix = settings.get("local_path_prefix", "") or ""
    local_root = translate_path(qbit_root, qbit_prefix, local_prefix)
    fs = find_fs_candidates(
        local_root, mam_torrent_name=mam_torrent_name,
    )
    # Dedupe: if a qBit candidate's save_path is the same dir as an
    # fs candidate, drop the fs duplicate.
    qbit_paths = {c.save_path for c in qbit}
    fs_unique = [c for c in fs if c.save_path not in qbit_paths]
    combined = qbit + fs_unique
    combined.sort(key=lambda c: -c.score)
    return combined[:_MAX_CANDIDATES]


async def start_reingest(
    db: aiosqlite.Connection,
    *,
    dispatcher,
    mam_torrent_id: str,
    mam_torrent_name: str,
    category: str,
    author_blob: str,
    candidate: Candidate,
) -> tuple[int, int]:
    """Create the grab + pipeline_run rows and kick off the pipeline.

    Returns `(grab_id, pipeline_run_id)`. The caller (the HTTP
    endpoint) typically returns those so the UI can poll the
    pipeline status if it wants to.

    Side effects:
      - Inserts a `grabs` row with `state=STATE_DOWNLOADED`,
        `is_reingest=1`, and the qBit hash if known. NO MAM .torrent
        fetch occurs — the .torrent never enters the snatch_ledger
        or counts against the budget. NO qBit submit occurs — the
        torrent is either already loaded (qBit candidate) or only
        exists as files on disk (fs candidate).
      - Inserts a `pipeline_runs` row in the standard initial state
        so the budget watcher / pipeline state machine see a
        complete-shaped run from the moment it's reingested.
      - Calls `process_completion` directly with `torrent_files`
        populated from the candidate, bypassing the qBit "download
        finished" detection step that would otherwise wait for a
        non-existent submission to complete.
    """
    # Late import to avoid orchestrator → reingest → orchestrator
    # cycle at module load. process_completion is the entrypoint
    # that runs steps 1-9 of the post-download pipeline.
    from app.orchestrator.download_watcher import CompletionEvent
    from app.orchestrator.pipeline import process_completion

    grab_id = await grabs_storage.create_grab(
        db,
        announce_id=None,
        mam_torrent_id=mam_torrent_id,
        torrent_name=mam_torrent_name,
        category=category,
        author_blob=author_blob,
        state=grabs_storage.STATE_DOWNLOADED,
        qbit_hash=candidate.qbit_hash,
        is_reingest=True,
    )

    pipeline_run_id = await pipe_storage.create_run(
        db,
        grab_id=grab_id,
        qbit_hash=candidate.qbit_hash or "",
        source_path=candidate.save_path,
    )

    event = CompletionEvent(
        grab_id=grab_id,
        qbit_hash=candidate.qbit_hash or "",
        torrent_name=mam_torrent_name,
        save_path=candidate.save_path,
        pipeline_run_id=pipeline_run_id,
    )

    deps = dispatcher
    from app.config import load_settings
    settings = load_settings()
    metadata_enricher = getattr(deps, "metadata_enricher", None)

    ok = await process_completion(
        db, event,
        staging_path=getattr(deps, "staging_path", "") or settings.get("staging_path", ""),
        default_sink=getattr(deps, "default_sink", ""),
        calibre_library_path=getattr(deps, "calibre_library_path", ""),
        folder_sink_path=getattr(deps, "folder_sink_path", ""),
        audiobookshelf_library_path=getattr(deps, "audiobookshelf_library_path", ""),
        abs_base_url=getattr(deps, "abs_base_url", ""),
        abs_api_key=getattr(deps, "abs_api_key", ""),
        abs_library_id=getattr(deps, "abs_library_id", ""),
        cwa_ingest_path=getattr(deps, "cwa_ingest_path", ""),
        category_routing=getattr(deps, "category_routing", None) or {},
        ntfy_url=getattr(deps, "ntfy_url", ""),
        ntfy_topic=getattr(deps, "ntfy_topic", ""),
        auto_train_enabled=getattr(deps, "auto_train_enabled", True),
        review_queue_enabled=bool(settings.get("review_queue_enabled", True)),
        review_staging_path=settings.get("review_staging_path", "") or "",
        per_event_notifications=getattr(deps, "per_event_notifications", False),
        metadata_enricher=metadata_enricher,
        torrent_files=candidate.book_files,
        audiobook_format_priority=settings.get("audiobook_format_priority") or None,
        ebook_format_priority=settings.get("ebook_format_priority") or None,
    )

    _log.info(
        "reingest: grab_id=%d run_id=%d ok=%s source=%s files=%d for mam_id=%s",
        grab_id, pipeline_run_id, ok, candidate.source,
        len(candidate.book_files), mam_torrent_id,
    )
    return grab_id, pipeline_run_id
