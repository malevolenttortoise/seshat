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
