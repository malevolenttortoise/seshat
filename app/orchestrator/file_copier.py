"""
File copier: extract book files from qBit download dir to staging.

When a torrent finishes downloading, qBit leaves the files in its
download directory (e.g. `/downloads/[mam-reseed]/Book Name/`). The
copier scans that directory for book files (epub, m4b, pdf, cbz, etc.),
copies them to the staging directory, and updates the pipeline_run row
with the staged path and detected format.

Design choices:
  - COPY, not move. The original stays in place for seeding.
  - Only recognized book formats are copied. Ancillary files (.nfo,
    .txt, .jpg) are left behind.
  - If multiple book files exist in the torrent (e.g., a series pack),
    each gets its own copy. The pipeline_run row tracks the "primary"
    file (largest by size).
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_log = logging.getLogger("seshat.orchestrator.file_copier")

# Recognized book file extensions (lowercase, no dot).
BOOK_EXTENSIONS = frozenset({
    "epub", "mobi", "azw", "azw3", "pdf",
    "m4b", "mp3", "m4a",    # audiobook formats
    "cbz", "cbr",           # comics
    "lit", "fb2", "djvu",
})

# Audiobook extensions the priority list can reorder. Anything not
# in this set is untouched by priority-sorting.
_AUDIOBOOK_EXTENSIONS = frozenset({"m4b", "m4a", "mp3", "aax", "aa"})

# Ebook extensions the ebook priority list can reorder. Mirrors the
# audiobook set above — `_apply_ebook_priority` is gated on whether
# the torrent contains any ebook file at all so mixed audiobook-only
# releases don't get touched.
_EBOOK_EXTENSIONS = frozenset({
    "epub", "mobi", "azw", "azw3", "pdf", "lit", "fb2", "djvu",
    "cbz", "cbr",
})


def _apply_audiobook_priority(
    files: list[Path],
    audiobook_priority: Optional[list[str]] = None,
) -> list[Path]:
    """Re-rank a file list so the preferred audiobook format lands first.

    Baseline sort (largest-first) is preserved for non-audiobook
    files and for files within the same audiobook format. Only the
    between-format ordering changes.

    When `audiobook_priority` is None or empty, returns `files`
    unchanged. When a torrent contains no audiobook files at all,
    this is a no-op regardless of the priority list — ebook-only
    releases don't care about m4b/mp3 ordering.

    Implementation: stable sort with a key that gives audiobook
    files a (priority_rank, -size) pair, and non-audiobook files
    a (huge_rank, -size) pair. `sorted` is stable so files within
    the same format keep their original largest-first order.
    """
    if not audiobook_priority or not files:
        return files
    priority = [
        str(p).lstrip(".").lower() for p in audiobook_priority if p
    ]
    rank_of = {ext: i for i, ext in enumerate(priority)}
    has_audio = any(
        f.suffix.lstrip(".").lower() in _AUDIOBOOK_EXTENSIONS for f in files
    )
    if not has_audio:
        return files
    # Sentinel rank for extensions missing from the priority list —
    # they land after every ranked format but before non-audiobook
    # files (which keep the huge sentinel below).
    unranked_audio = len(priority)
    non_audio = unranked_audio + 1

    def _key(p: Path) -> tuple[int, int]:
        ext = p.suffix.lstrip(".").lower()
        if ext in rank_of:
            return (rank_of[ext], 0)
        if ext in _AUDIOBOOK_EXTENSIONS:
            return (unranked_audio, 0)
        return (non_audio, 0)

    return sorted(files, key=_key)


def _apply_ebook_priority(
    files: list[Path],
    ebook_priority: Optional[list[str]] = None,
) -> list[Path]:
    """Re-rank a file list so the preferred ebook format lands first.

    Mirror of `_apply_audiobook_priority` for the ebook side. UAT
    canary 2026-05-11: Mark grabbed "Methodology of Secrets" by
    Elliot Freeman from a torrent containing both EPUB and PDF;
    file_copier picked the PDF (largest-first baseline) even
    though `mam_format_priority` listed `epub` first. Without an
    ebook-priority pass the largest file wins regardless of
    extension, so a smaller preferred-format file gets passed over.

    Baseline sort (largest-first) is preserved for non-ebook files
    and for files within the same ebook format. Only the
    between-format ordering changes. No-op when:
      - `ebook_priority` is None or empty
      - the torrent contains no ebook files at all (audiobook-only
        releases stay in their original largest-first order)

    Implementation matches `_apply_audiobook_priority` exactly:
    stable sort with a key that gives ebook files a (priority_rank, 0)
    pair, non-ranked-ebook files a (sentinel, 0) pair.
    """
    if not ebook_priority or not files:
        return files
    priority = [
        str(p).lstrip(".").lower() for p in ebook_priority if p
    ]
    rank_of = {ext: i for i, ext in enumerate(priority)}
    has_ebook = any(
        f.suffix.lstrip(".").lower() in _EBOOK_EXTENSIONS for f in files
    )
    if not has_ebook:
        return files
    unranked_ebook = len(priority)
    non_ebook = unranked_ebook + 1

    def _key(p: Path) -> tuple[int, int]:
        ext = p.suffix.lstrip(".").lower()
        if ext in rank_of:
            return (rank_of[ext], 0)
        if ext in _EBOOK_EXTENSIONS:
            return (unranked_ebook, 0)
        return (non_ebook, 0)

    return sorted(files, key=_key)


@dataclass(frozen=True)
class CopyResult:
    """Outcome of a file copy operation."""

    success: bool
    staged_path: Optional[str] = None
    book_filename: Optional[str] = None
    book_format: Optional[str] = None
    files_copied: int = 0
    error: Optional[str] = None


def find_book_files(
    source_dir: Path,
    *,
    audiobook_priority: Optional[list[str]] = None,
    ebook_priority: Optional[list[str]] = None,
) -> list[Path]:
    """Find all book files in a directory tree.

    Primary sort is size descending (largest-first). When
    `audiobook_priority` is provided and the torrent contains
    audiobook files, a second pass reorders so the user's preferred
    audiobook format lands first. Same for `ebook_priority` on the
    ebook side. Both can be applied independently — they touch
    different file extensions and won't interfere.

    Covers mixed-format bundles where Seshat would otherwise pick
    whichever single file happened to be largest regardless of
    extension. UAT canary 2026-05-11: an EPUB+PDF release where
    PDF was larger had the PDF picked despite `mam_format_priority`
    listing `epub` first.
    """
    if not source_dir.exists():
        return []

    if source_dir.is_file():
        ext = source_dir.suffix.lstrip(".").lower()
        return [source_dir] if ext in BOOK_EXTENSIONS else []

    found: list[Path] = []
    for path in source_dir.rglob("*"):
        if path.is_file():
            ext = path.suffix.lstrip(".").lower()
            if ext in BOOK_EXTENSIONS:
                found.append(path)

    by_size = sorted(found, key=lambda p: p.stat().st_size, reverse=True)
    by_audio = _apply_audiobook_priority(by_size, audiobook_priority)
    return _apply_ebook_priority(by_audio, ebook_priority)


def copy_to_staging(
    source_dir: Path,
    staging_dir: Path,
    torrent_name: str,
    *,
    explicit_files: Optional[list[Path]] = None,
    audiobook_priority: Optional[list[str]] = None,
    ebook_priority: Optional[list[str]] = None,
) -> CopyResult:
    """Copy book files from source to staging.

    When `explicit_files` is provided the copier uses exactly that
    list — typically populated from qBit's `/torrents/files` response
    so we copy only what belongs to this specific torrent, even when
    the save_path also contains files from other torrents. Without
    it, `source_dir` is scanned recursively (legacy behavior used
    when the client can't report its file list).

    `audiobook_priority` / `ebook_priority` (optional) let mixed-
    format torrents pick a preferred extension for the primary file
    — the baseline largest-first sort runs first, then stable
    re-ranks promote files whose extension appears earlier in each
    respective priority list. The two priorities operate on disjoint
    extension sets and don't interfere.

    Creates a subdirectory under staging_dir named after the torrent.
    Returns info about the primary (largest) book file.

    This is a synchronous function because file I/O on local disk is
    fast and shutil.copy2 doesn't have an async variant. The caller
    should run it in a thread pool if needed.
    """
    source_dir = Path(source_dir)
    staging_str = str(staging_dir).strip() if staging_dir else ""
    if not staging_str or staging_str == ".":
        return CopyResult(success=False, error="staging directory not configured")
    staging_dir = Path(staging_str)

    try:
        if explicit_files is not None:
            # Filter the explicit list to existing book-format files
            # and sort largest-first so the primary selection matches
            # the find_book_files ordering.
            by_size = sorted(
                [p for p in explicit_files
                 if p.is_file() and p.suffix.lstrip(".").lower() in BOOK_EXTENSIONS],
                key=lambda p: p.stat().st_size, reverse=True,
            )
            by_audio = _apply_audiobook_priority(by_size, audiobook_priority)
            book_files = _apply_ebook_priority(by_audio, ebook_priority)
        else:
            book_files = find_book_files(
                source_dir,
                audiobook_priority=audiobook_priority,
                ebook_priority=ebook_priority,
            )
        if not book_files:
            return CopyResult(
                success=False,
                error=f"no book files found in {source_dir}",
            )

        # Create a staging subdirectory for this torrent.
        dest_dir = staging_dir / _safe_dirname(torrent_name)
        dest_dir.mkdir(parents=True, exist_ok=True)

        copied = 0
        primary_dest: Optional[Path] = None

        for src_file in book_files:
            dest_file = dest_dir / src_file.name
            # Avoid overwriting if two files have the same name.
            if dest_file.exists():
                stem = dest_file.stem
                suffix = dest_file.suffix
                counter = 1
                while dest_file.exists():
                    dest_file = dest_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

            shutil.copy2(str(src_file), str(dest_file))
            copied += 1

            if primary_dest is None:
                primary_dest = dest_file

        primary = primary_dest or dest_dir
        fmt = primary.suffix.lstrip(".").lower() if primary.is_file() else ""

        _log.info(
            "copied %d book file(s) to staging: %s → %s",
            copied, source_dir, dest_dir,
        )

        return CopyResult(
            success=True,
            staged_path=str(dest_dir),
            book_filename=primary.name if primary.is_file() else None,
            book_format=fmt or None,
            files_copied=copied,
        )
    except Exception as e:
        _log.exception("file copy failed: %s → %s", source_dir, staging_dir)
        return CopyResult(success=False, error=f"{type(e).__name__}: {e}")


def _safe_dirname(name: str) -> str:
    """Sanitize a torrent name for use as a directory name."""
    # Replace filesystem-unsafe characters.
    unsafe = '<>:"/\\|?*'
    result = name
    for ch in unsafe:
        result = result.replace(ch, "_")
    return result.strip(". ") or "unnamed"
