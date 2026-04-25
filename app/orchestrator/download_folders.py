"""
Download folder management.

Computes the subfolder path based on the user's chosen structure
(monthly, yearly, author, or flat) and ensures it exists. The qBit
`save_path` parameter is set to this folder when submitting a
torrent, so downloads land directly in the organized structure
without needing a post-download move/copy step.

Supported modes (via settings `download_folder_structure`):
    "monthly"  = [YYYY-MM]/ subfolders           (default)
    "yearly"   = [YYYY]/ subfolders
    "author"   = Author Name/ subfolders
    "flat"     = no subfolder, everything in root
    "template" = user-defined nesting (settings `download_folder_template`)

Template mode tokens (Python str.format_map style):
    {author}  — normalized author name
    {series}  — normalized series name (empty for standalones)
    {title}   — normalized book title

Example templates:
    "{author}"                    — same as "author" mode
    "{author}/{series}"           — group books by series under each author
    "{author}/{series}/{title}"   — full nesting; standalone books skip
                                    the series level automatically
    "{series}"                    — series-first organization
                                    (mixes authors at the top)

Empty segments (e.g. {series} on a standalone book) are dropped,
not left as empty directories. So
"{author}/{series}/{title}" with no series becomes just
"<author>/<title>".

Tokens that can't be filled at submit time (because IRC announces
don't carry rich metadata) resolve to empty and drop out the same
way. Discovery's send-to-pipeline path provides all three.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger("seshat.orchestrator.download_folders")


def current_month_folder(
    base_path: str,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Compute the current month's download folder path.

    Args:
        base_path: The qBit base download directory
                   (e.g. "/downloads/[mam-complete]" or
                   "/mnt/user/downloads/[mam-complete]").
        now: Override for testing. Defaults to UTC now.

    Returns the full path including the month subfolder,
    e.g. "/downloads/[mam-complete]/[2026-04]".
    Returns base_path unchanged if it's empty.
    """
    if not base_path:
        return ""

    dt = now or datetime.now(timezone.utc)
    folder_name = f"[{dt.strftime('%Y-%m')}]"
    return str(Path(base_path) / folder_name)


def _normalize_path_segment(segment: str, *, allow_empty: bool = False) -> str:
    """Normalize a string into a filesystem-safe folder segment.

    Strips leading/trailing whitespace, removes characters illegal
    or annoying in paths (`<>:"/\\|?*`), collapses dots+spaces so
    "William D. Arand" and "William D Arand" land identically.

    `allow_empty=False` (default, used for the legacy "author" mode)
    falls back to "_Unknown" so we never pass an empty string to
    Path(). `allow_empty=True` is what template mode uses — empty
    segments signal "drop this nesting level" rather than "fall back
    to a placeholder".
    """
    import re
    name = (segment or "").strip()
    if not name:
        return "" if allow_empty else "_Unknown"
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.replace('.', ' ')
    name = re.sub(r'\s+', ' ', name).strip()
    if not name:
        return "" if allow_empty else "_Unknown"
    return name


# Backwards-compat alias — `_normalize_author_folder` was the old
# name, kept for any out-of-tree imports while we transition.
_normalize_author_folder = lambda n: _normalize_path_segment(n)


def _render_template(
    template: str,
    *,
    author: str = "",
    series: str = "",
    title: str = "",
) -> str:
    """Render a download-folder template with the given tokens.

    Splits the template by `/`, normalizes each segment, drops segments
    that resolve to empty strings (so standalone books in
    "{author}/{series}/{title}" skip the series level cleanly), and
    rejoins with the OS path separator via Path.

    Falls back to the normalized author segment when the resulting
    path would otherwise be empty (e.g. all tokens are empty) so we
    don't return "" and dump the torrent into the bare root.
    """
    norm_author = _normalize_path_segment(author, allow_empty=True)
    norm_series = _normalize_path_segment(series, allow_empty=True)
    norm_title = _normalize_path_segment(title, allow_empty=True)
    tokens = {
        "author": norm_author,
        "series": norm_series,
        "title": norm_title,
    }

    rendered_segments: list[str] = []
    for raw_segment in (template or "{author}").split("/"):
        segment = raw_segment.strip()
        if not segment:
            continue
        try:
            filled = segment.format_map(tokens).strip()
        except (KeyError, ValueError):
            # Unknown token or malformed format string — skip the
            # segment rather than blowing up the whole submission.
            continue
        if filled:
            rendered_segments.append(filled)

    if not rendered_segments:
        # Template rendered to nothing. Use the legacy "author"
        # behavior so we still produce a safe folder.
        return _normalize_path_segment(author, allow_empty=False)
    return "/".join(rendered_segments)


def compute_download_folder(
    base_path: str,
    structure: str,
    *,
    author_name: str = "",
    series_name: str = "",
    book_title: str = "",
    template: str = "",
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Compute the download subfolder path for the given structure mode.

    Args:
        base_path: qBit base download directory.
        structure: One of "monthly", "yearly", "author", "flat", "template".
        author_name: Used by "author" + "template" modes.
        series_name: Used by "template" mode (`{series}` token).
                     Empty for standalones — the template renderer
                     drops the segment in that case.
        book_title: Used by "template" mode (`{title}` token).
        template: Format string for "template" mode, e.g.
                  "{author}/{series}/{title}". Defaults to "{author}"
                  when mode is "template" but template is empty —
                  matches the legacy "author" mode exactly.
        now: Override for testing. Defaults to UTC now.

    Returns the full subfolder path, or None when no subfolder is
    needed ("flat") or base_path is empty.
    """
    if not base_path:
        return None

    if structure == "flat":
        return None  # caller passes None → qBit uses its default save_path

    if structure == "yearly":
        dt = now or datetime.now(timezone.utc)
        return str(Path(base_path) / f"[{dt.strftime('%Y')}]")

    if structure == "author":
        folder = _normalize_path_segment(author_name)
        return str(Path(base_path) / folder)

    if structure == "template":
        rendered = _render_template(
            template or "{author}",
            author=author_name,
            series=series_name,
            title=book_title,
        )
        # rendered is a "/"-joined relative path; let Path handle the
        # OS-correct separator on join + collapse.
        return str(Path(base_path) / rendered)

    # Default: monthly (also covers unknown/typo values)
    return current_month_folder(base_path, now=now)


def translate_path(
    path: str,
    from_prefix: str,
    to_prefix: str,
) -> str:
    """Translate a path between container mount namespaces.

    E.g. translate_path("/data/[mam-complete]/book", "/data", "/downloads")
         → "/downloads/[mam-complete]/book"

    Returns the path unchanged if it doesn't start with from_prefix.
    """
    if not path or not from_prefix:
        return path
    from_prefix = from_prefix.rstrip("/")
    to_prefix = to_prefix.rstrip("/")
    if path.startswith(from_prefix + "/") or path == from_prefix:
        return to_prefix + path[len(from_prefix):]
    return path


def ensure_folder_exists(path: str) -> bool:
    """Create the folder if it doesn't exist, with world-writable perms.

    Returns True if the folder exists (or was created), False on error.
    This is called before submitting to qBit so the save_path is valid.
    In Docker, the container needs write access to the mounted volume.

    Permissions are set to 0o777 (world-writable) because the download
    client may run as a different user/group than Seshat. Without
    world-writable, qBit v5's setSavePath/setLocation returns
    "403 Cannot write to directory".
    """
    if not path:
        return False
    try:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        try:
            import os
            os.chmod(str(p), 0o777)
        except (OSError, PermissionError):
            pass
        return True
    except Exception:
        _log.exception("failed to create download folder: %s", path)
        return False
