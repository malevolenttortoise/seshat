"""
Calibre-Web-Automated (CWA) sink.

Delivers book files by dropping them into CWA's watched ingest
directory. CWA's built-in file watcher picks up new files, imports
them into the Calibre library, handles database locking correctly,
and applies its own metadata enrichment pipeline.

This is the safest Calibre integration method:
  - No direct metadata.db writes (avoids cache invalidation issues
    with the Calibre GUI/content server)
  - No Docker socket access needed
  - CWA handles duplicate detection, format conversion, and metadata
  - Just a shared volume mount — zero attack surface

The ingest directory path is configured in settings.json as
`cwa_ingest_path`. In Docker, this is typically mounted from the
same host directory that CWA watches (e.g. /mnt/user/.../cwa-import).
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.metadata.extract import BookMetadata
from app.sinks._cwa_throttle import throttle
from app.sinks.base import SinkResult

_log = logging.getLogger("seshat.sinks")


class CWASink:
    """Delivers book files to CWA's ingest directory."""

    name = "cwa"

    def __init__(self, ingest_path: str, min_gap_seconds: float = 10.0):
        self.ingest_path = ingest_path
        self.min_gap_seconds = min_gap_seconds

    async def deliver(
        self,
        file_path: str,
        metadata: BookMetadata,
    ) -> SinkResult:
        """Copy a book file into CWA's ingest directory.

        CWA expects flat file drops — no subdirectory structure needed.
        It handles author/title organization internally during import.
        """
        if not self.ingest_path:
            return SinkResult(
                success=False,
                sink_name=self.name,
                error="CWA ingest path not configured",
            )

        src = Path(file_path)
        if not src.exists():
            return SinkResult(
                success=False,
                sink_name=self.name,
                error=f"file not found: {file_path}",
            )

        target_dir = Path(self.ingest_path)
        # Throttle the write so consecutive deliveries to the same CWA
        # ingest path don't overlap and wedge cps's HTTP listener — see
        # _cwa_throttle.py for the full failure-mode write-up.
        async with throttle(self.ingest_path, self.min_gap_seconds):
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                dest = target_dir / src.name

                # Avoid overwriting if a file with the same name is pending.
                if dest.exists():
                    stem = dest.stem
                    suffix = dest.suffix
                    counter = 1
                    while dest.exists():
                        dest = target_dir / f"{stem}_{counter}{suffix}"
                        counter += 1

                # Atomic write: copy to a hidden temp file in the same dir,
                # then rename to the final name. CWA's inotify watcher only
                # fires on the rename (close_write event on the final name),
                # so it never sees a partial file. The temp filename starts
                # with a dot so CWA's "ignored/temporary file" filter skips it.
                tmp_dest = target_dir / f".seshat-tmp-{dest.name}"
                shutil.copy2(str(src), str(tmp_dest))
                tmp_dest.replace(dest)  # atomic rename on the same filesystem

                _log.info("cwa sink: dropped %s → %s", src.name, dest)
                return SinkResult(
                    success=True,
                    sink_name=self.name,
                    detail=str(dest),
                )
            except Exception as e:
                _log.exception("cwa sink copy failed")
                return SinkResult(
                    success=False,
                    sink_name=self.name,
                    error=f"{type(e).__name__}: {e}",
                )

    async def remove(self, *, calibre_book_id: int) -> SinkResult:
        """Remove a book from the CWA-managed library via its admin API.

        Inverse of `deliver()` for the v2.27.0 active-replacement
        path. Reuses the CWAClient login + CSRF machinery in
        `app/discovery/push_back.py` (the natural home for CWA HTTP
        interactions) rather than duplicating session handling here.

        Reads CWA admin creds (`cwa_base_url`, `cwa_username`,
        `cwa_password`) from settings at call time — matches the
        pattern in `push_back.push_cwa`. The slim-image Calibre user
        keeps a single CWA credential set; we don't store anything
        sink-local.
        """
        if calibre_book_id is None:
            return SinkResult(
                success=False,
                sink_name=self.name,
                error="remove requires calibre_book_id",
            )

        # Lazy import to keep sinks/cwa.py free of bs4/httpx imports
        # at module load when only the deliver path is exercised.
        from app.config import load_settings
        from app.discovery.push_back import (
            CWAClient,
            PushFailed,
            PushUnavailable,
        )
        from app.secrets import get_secret

        settings = load_settings()
        base_url = (settings.get("cwa_base_url") or "").rstrip("/")
        username = settings.get("cwa_username") or ""
        password = await get_secret("cwa_password") or ""
        if not (base_url and username and password):
            return SinkResult(
                success=False,
                sink_name=self.name,
                error=(
                    "CWA admin API not configured. Set cwa_base_url, "
                    "cwa_username, and cwa_password in Settings → Sinks."
                ),
            )

        client = CWAClient(base_url, username, password)
        try:
            await client.delete(int(calibre_book_id))
        except PushUnavailable as e:
            return SinkResult(
                success=False,
                sink_name=self.name,
                error=str(e),
            )
        except PushFailed as e:
            _log.warning(
                "cwa sink remove: book_id=%s failed: %s", calibre_book_id, e,
            )
            return SinkResult(
                success=False,
                sink_name=self.name,
                error=str(e),
            )
        except Exception as e:
            _log.exception("cwa sink remove raised")
            return SinkResult(
                success=False,
                sink_name=self.name,
                error=f"{type(e).__name__}: {e}",
            )

        _log.info("cwa sink: deleted book_id=%s", calibre_book_id)
        return SinkResult(
            success=True,
            sink_name=self.name,
            detail=f"deleted calibre_book_id={calibre_book_id}",
        )
