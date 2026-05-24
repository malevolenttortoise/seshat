"""
Calibre sink: add books via `calibredb add`.

Uses the `calibredb` CLI rather than Calibre's content server API
because:
  1. calibredb is always available in any Calibre installation
  2. It handles duplicate detection, format conversion, and metadata
     enrichment automatically
  3. No auth setup needed (it talks to the library directory directly)

The library path must be configured in settings.json or via the
CALIBRE_LIBRARY_PATH environment variable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from app.metadata.extract import BookMetadata
from app.sinks.base import SinkResult

_log = logging.getLogger("seshat.sinks")

# calibredb binary name. Can be overridden for testing.
CALIBREDB_CMD = "calibredb"

# Patterns in calibredb's stderr that indicate the bundled-Calibre
# image is missing a system library Qt's platform plugin loader needs.
# The default Seshat image deliberately omits the OpenGL/Mesa stack
# (libgl1, libegl1, libopengl0) because headless `calibredb add` and
# `calibredb list` don't exercise GL paths in any test we've run —
# but if a real-world ebook conversion route does pull a GL symbol,
# we want a clear diagnostic instead of a cryptic Qt traceback.
#
# When any of these match, `_detect_runtime_lib_failure` returns
# True and the caller emits a structured error pointing the user at
# the GitHub issue tracker so we can collect data on which Calibre
# operations actually need GL.
_RX_QT_PLUGIN_FAILURE = re.compile(
    r"(?i)("
    r"could not load the qt platform plugin"
    r"|no qt platform plugin could be initialized"
    r"|qt\.qpa\.plugin"
    r"|error while loading shared libraries"
    # Library names in either form: "libGL.so.1: cannot open ..." or
    # "... cannot open shared object file: ... libGL.so.1". Bare name
    # mentions are common in Qt's "xcb-cursor0 is needed" prompt too,
    # so we match the un-prefixed name as well.
    r"|lib(gl|egl|opengl|xcb-cursor|fontconfig|xrender)\S*\.so"
    r"|\bxcb-cursor0?\b"
    r")"
)


def _detect_runtime_lib_failure(stderr: str) -> bool:
    """True when calibredb's stderr looks like a missing-system-library
    failure rather than an ordinary Calibre error (bad library path,
    metadata clash, etc.).

    Match list is intentionally permissive — false positives are cheap
    (one extra log line pointing the user at the issue tracker) but
    false negatives mean a confused user with no actionable signal.
    """
    return bool(stderr and _RX_QT_PLUGIN_FAILURE.search(stderr))


def _format_runtime_lib_diagnostic(stderr: str, *, action: str) -> str:
    """Build a multi-line diagnostic block users can paste into a
    GitHub issue. Includes the matching stderr snippet, the calibredb
    action that failed, and a hint about the slim apt-deps tradeoff.
    """
    snippet = (stderr or "").strip()[:600]
    return (
        f"calibredb {action} failed with what looks like a missing "
        f"system library. The Seshat image ships a trimmed apt set "
        f"(no libgl1/libegl1/libopengl0 — they pull ~170MB of LLVM/Mesa "
        f"that headless calibredb usually doesn't need).\n"
        f"\n"
        f"If you're hitting this, please open an issue at "
        f"https://github.com/malevolenttortoise/seshat/issues with this block:\n"
        f"---\n"
        f"action: calibredb {action}\n"
        f"image: ghcr.io/malevolenttortoise/seshat:latest (full Calibre)\n"
        f"stderr:\n{snippet}\n"
        f"---\n"
        f"Workaround: switch to a custom image that adds `libgl1 "
        f"libegl1 libopengl0` back to the apt install, or use the "
        f"file-folder sink + CWA/ABS to ingest instead of the direct "
        f"Calibre sink."
    )


class CalibreSink:
    """Delivers book files to a Calibre library via calibredb."""

    name = "calibre"

    def __init__(self, library_path: str):
        self.library_path = library_path

    async def deliver(
        self,
        file_path: str,
        metadata: BookMetadata,
    ) -> SinkResult:
        """Add a book file to the Calibre library.

        Uses `calibredb add --library-path <path> <file>`.
        Optionally sets title/author if metadata is available.
        """
        if not self.library_path:
            return SinkResult(
                success=False,
                sink_name=self.name,
                error="Calibre library path not configured",
            )

        path = Path(file_path)
        if not path.exists():
            return SinkResult(
                success=False,
                sink_name=self.name,
                error=f"file not found: {file_path}",
            )

        cmd = [
            CALIBREDB_CMD, "add",
            "--library-path", self.library_path,
        ]

        # Set metadata if available.
        if metadata.title:
            cmd.extend(["--title", metadata.title])
        if metadata.author:
            cmd.extend(["--authors", metadata.author])
        if metadata.series:
            cmd.extend(["--series", metadata.series])
        if metadata.series_index:
            cmd.extend(["--series-index", metadata.series_index])
        if metadata.isbn:
            cmd.extend(["--isbn", metadata.isbn])

        cmd.append(str(path))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=60
            )

            output = stdout.decode("utf-8", errors="replace").strip()
            err_output = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                _log.info("calibredb add succeeded: %s", output or path.name)
                return SinkResult(
                    success=True,
                    sink_name=self.name,
                    detail=output or f"added {path.name}",
                )

            full_error = f"exit {proc.returncode}: {err_output or output}"
            if _detect_runtime_lib_failure(err_output):
                _log.error(
                    "%s",
                    _format_runtime_lib_diagnostic(err_output, action="add"),
                )
            else:
                _log.warning("calibredb add failed: %s", full_error)
            return SinkResult(
                success=False,
                sink_name=self.name,
                error=full_error,
            )
        except FileNotFoundError:
            return SinkResult(
                success=False,
                sink_name=self.name,
                error="calibredb not found — is Calibre installed?",
            )
        except asyncio.TimeoutError:
            return SinkResult(
                success=False,
                sink_name=self.name,
                error="calibredb timed out after 60s",
            )
        except Exception as e:
            _log.exception("calibredb add raised")
            return SinkResult(
                success=False,
                sink_name=self.name,
                error=f"{type(e).__name__}: {e}",
            )

    async def remove(
        self,
        *,
        calibre_book_id: Optional[int] = None,
        mam_torrent_id: Optional[int] = None,
        file_path: Optional[str] = None,
    ) -> SinkResult:
        """Remove a book from the Calibre library.

        Resolution order (first non-None wins):
          1. `calibre_book_id` — used directly with `calibredb remove`.
             This is the path Phase 3's orchestrator hands in; Seshat's
             `books.calibre_id` is populated by the calibre_sync
             backfill for every owned book.
          2. `mam_torrent_id` — searches Calibre identifiers via
             `calibredb list --search "identifiers:mam_torrent_id:<X>"`.
             Useful for advanced libraries that have manually stamped
             this identifier (Seshat itself does not stamp at delivery
             time per Phase 5b Decision 3).
          3. `file_path` — true pre-Seshat fallback. Queries
             `<library>/metadata.db` directly via sqlite for any book
             whose `path` column matches the file's parent directory
             relative to the library root. This is the only reliable
             path-based lookup because Calibre's CLI search syntax
             does not expose `path:` as a queryable field.

        Then runs `calibredb remove --library-path <path> <book_id>`.

        Idempotent: a lookup that returns no rows is treated as
        success (book already absent — that's the desired terminal
        state, mirroring the soft-delete-then-sink-remove flow where
        sink-remove may be retried after a partial failure).

        At least one of `calibre_book_id`, `mam_torrent_id`, or
        `file_path` must be given.
        """
        if not self.library_path:
            return SinkResult(
                success=False,
                sink_name=self.name,
                error="Calibre library path not configured",
            )
        if calibre_book_id is None and mam_torrent_id is None and not file_path:
            return SinkResult(
                success=False,
                sink_name=self.name,
                error="remove requires calibre_book_id, mam_torrent_id, or file_path",
            )

        # Phase 1 — resolve to a list of Calibre internal book ids.
        if calibre_book_id is not None:
            book_ids: list[int] = [int(calibre_book_id)]
            lookup_descr = f"calibre_book_id={calibre_book_id}"
        elif mam_torrent_id is not None:
            search_expr = f"identifiers:mam_torrent_id:{int(mam_torrent_id)}"
            book_ids, search_err = await self._calibredb_search_ids(search_expr)
            if search_err is not None:
                return SinkResult(
                    success=False,
                    sink_name=self.name,
                    error=search_err,
                )
            lookup_descr = search_expr
        else:
            book_ids, lookup_err = self._sqlite_lookup_by_path(file_path)  # type: ignore[arg-type]
            if lookup_err is not None:
                return SinkResult(
                    success=False,
                    sink_name=self.name,
                    error=lookup_err,
                )
            lookup_descr = f"file_path={file_path!r}"

        if not book_ids:
            # Idempotency: nothing to remove.
            _log.info(
                "calibredb remove: no rows matched %s — treating as success",
                lookup_descr,
            )
            return SinkResult(
                success=True,
                sink_name=self.name,
                detail=f"no match for {lookup_descr}",
            )

        # Phase 2 — `calibredb remove --library-path <lib> <id> [<id>...]`.
        cmd = [
            CALIBREDB_CMD, "remove",
            "--library-path", self.library_path,
            *[str(i) for i in book_ids],
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=60
            )
            output = stdout.decode("utf-8", errors="replace").strip()
            err_output = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                _log.info(
                    "calibredb remove succeeded for ids=%s (%s)",
                    book_ids, output or "no stdout",
                )
                return SinkResult(
                    success=True,
                    sink_name=self.name,
                    detail=f"removed {book_ids}",
                )

            full_error = f"exit {proc.returncode}: {err_output or output}"
            if _detect_runtime_lib_failure(err_output):
                _log.error(
                    "%s",
                    _format_runtime_lib_diagnostic(err_output, action="remove"),
                )
            else:
                _log.warning("calibredb remove failed: %s", full_error)
            return SinkResult(
                success=False,
                sink_name=self.name,
                error=full_error,
            )
        except FileNotFoundError:
            return SinkResult(
                success=False,
                sink_name=self.name,
                error="calibredb not found — is Calibre installed?",
            )
        except asyncio.TimeoutError:
            return SinkResult(
                success=False,
                sink_name=self.name,
                error="calibredb timed out after 60s",
            )
        except Exception as e:
            _log.exception("calibredb remove raised")
            return SinkResult(
                success=False,
                sink_name=self.name,
                error=f"{type(e).__name__}: {e}",
            )

    async def _calibredb_search_ids(
        self, search_expr: str,
    ) -> tuple[list[int], Optional[str]]:
        """Run `calibredb list --search <expr> --for-machine` and return
        the matched book ids.

        `--for-machine` emits a JSON array of objects; we only need
        `id` from each. A search that matches nothing returns an empty
        list with no error. Returns (ids, error) — error is None on
        successful empty results.
        """
        cmd = [
            CALIBREDB_CMD, "list",
            "--library-path", self.library_path,
            "--for-machine",
            "--fields", "id",
            "--search", search_expr,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30
            )
            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                if _detect_runtime_lib_failure(err):
                    _log.error(
                        "%s",
                        _format_runtime_lib_diagnostic(err, action="list"),
                    )
                return [], f"calibredb list exit {proc.returncode}: {err or out}"
            try:
                rows = json.loads(out or "[]")
            except json.JSONDecodeError as e:
                return [], f"calibredb list JSON decode failed: {e}"
            ids: list[int] = []
            for row in rows or []:
                rid = row.get("id") if isinstance(row, dict) else None
                if isinstance(rid, int):
                    ids.append(rid)
            return ids, None
        except FileNotFoundError:
            return [], "calibredb not found — is Calibre installed?"
        except asyncio.TimeoutError:
            return [], "calibredb list timed out after 30s"
        except Exception as e:
            _log.exception("calibredb list raised")
            return [], f"{type(e).__name__}: {e}"

    def _sqlite_lookup_by_path(
        self, file_path: str,
    ) -> tuple[list[int], Optional[str]]:
        """Resolve a filesystem path to Calibre book id(s) via direct
        sqlite query on `<library>/metadata.db`.

        Calibre stores `books.path` as a relative directory like
        `<Author>/<Title> (<id>)`. Given a file path under the library
        root, the relative parent directory IS this value, so an
        equality match returns the owning book row.

        Returns (ids, error) — error is None on successful empty
        results. We keep this synchronous since sqlite reads are fast
        enough that wrapping in a thread isn't worth the complexity at
        the call site (one short read per enact).

        The metadata.db is opened read-only so a concurrent CWA writer
        cannot deadlock us; we never mutate via this path (the
        `calibredb remove` step performs the actual delete).
        """
        import os
        import sqlite3

        lib_root = Path(self.library_path)
        target = Path(file_path)
        try:
            rel_parent = target.parent.resolve().relative_to(lib_root.resolve())
        except ValueError:
            return [], (
                f"file_path {file_path!r} is not under library_path "
                f"{self.library_path!r}; cannot resolve to a Calibre book id"
            )
        except OSError as e:
            return [], f"could not resolve path: {type(e).__name__}: {e}"

        db_path = lib_root / "metadata.db"
        if not db_path.exists():
            return [], f"Calibre metadata.db not found at {db_path}"

        try:
            con = sqlite3.connect(
                f"file:{os.fspath(db_path)}?mode=ro", uri=True,
            )
            try:
                rows = con.execute(
                    "SELECT id FROM books WHERE path = ?",
                    (str(rel_parent),),
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error as e:
            return [], f"sqlite read failed: {type(e).__name__}: {e}"

        return [int(r[0]) for r in rows], None
