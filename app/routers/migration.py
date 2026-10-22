"""
Migration wizard endpoints — v3 with server-side background processing.

    GET  /api/v1/migration/preview       — scan + compute targets
    POST /api/v1/migration/start         — kick off background migration job
    GET  /api/v1/migration/status        — poll progress of running job
    POST /api/v1/migration/cancel        — abort a running migration
    POST /api/v1/migration/resume-all    — resume all stopped torrents
    GET  /api/v1/migration/empty-folders — list empty subdirectories
    POST /api/v1/migration/cleanup       — delete selected empty folders

The migration runs entirely server-side so the user can navigate away
from the page (or close the browser) without losing progress. The
frontend polls /status every few seconds to update the progress bar.

After migration + resume, the cleanup step scans the download root for
empty subdirectories left behind (e.g. [2026-03-15], [Random Seeding])
and offers to delete them.

Path matching logic: a torrent is "already correct" ONLY if its
save_path ends with a folder that EXACTLY matches the target
pattern. Everything else is fair game for migration.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import state
from app.clients.qbittorrent import QbitClient
from app.config import load_settings, ui_error
from app.orchestrator.download_folders import (
    ensure_folder_exists,
    translate_path,
)

_log = logging.getLogger("seshat.routers.migration")

router = APIRouter(prefix="/api/v1/migration", tags=["migration"])

# Regex patterns for "already in the right structure" checks.
_MONTHLY_RX = re.compile(r"^.*\[(\d{4}-\d{2})\]$")  # [2026-04]
_YEARLY_RX = re.compile(r"^.*\[(\d{4})\]$")          # [2026]

BATCH_LIMIT = 50


# ─── Models ──────────────────────────────────────────────────

class PreviewItem(BaseModel):
    hash: str
    name: str
    current_path: str
    current_folder: str
    target_folder: Optional[str]
    target_path: Optional[str]
    needs_move: bool
    file_mtime: Optional[str]


class PreviewResponse(BaseModel):
    items: list[PreviewItem]
    need_move_count: int
    already_ok_count: int
    total: int


class StartRequest(BaseModel):
    hashes: list[str] = Field(..., min_length=1)
    dry_run: bool = False


class StatusResponse(BaseModel):
    running: bool
    done: int
    total: int
    succeeded: int
    failed: int
    finished: bool
    dry_run: bool
    results: list[dict]


# ─── Helpers ─────────────────────────────────────────────────

def _target_folder_for_mtime(ts: float, structure: str) -> str:
    """Compute the target folder name based on the folder structure setting."""
    dt = datetime.fromtimestamp(ts)
    if structure == "yearly":
        return f"[{dt.strftime('%Y')}]"
    elif structure == "flat":
        return ""
    else:  # monthly (default)
        return f"[{dt.strftime('%Y-%m')}]"


def _is_already_correct(save_path: str, target_folder: str, structure: str) -> bool:
    """Check if the torrent's save_path already ends with the exact target folder."""
    if structure == "flat":
        last = save_path.rstrip("/").rsplit("/", 1)[-1]
        return not last.startswith("[")
    if not target_folder:
        return False
    normalized = save_path.rstrip("/")
    return normalized.endswith(f"/{target_folder}") or normalized == target_folder


def _find_primary_mtime(local_dir: Path) -> Optional[float]:
    """Walk a download directory and return the mtime of the largest file.

    Only inspects the given directory — never scans parent directories.
    If the path is a regular file (single-file torrent), returns its mtime.
    Falls back to the directory's own mtime only if the directory exists
    but contains no regular files (unlikely for a completed download).
    """
    if not local_dir.exists():
        return None

    # Single-file torrent: local_dir is actually a file.
    if local_dir.is_file():
        try:
            return local_dir.stat().st_mtime
        except OSError:
            return None

    best_size = 0
    best_mtime: Optional[float] = None
    try:
        for f in local_dir.rglob("*"):
            if f.is_file():
                sz = f.stat().st_size
                if sz > best_size:
                    best_size = sz
                    best_mtime = f.stat().st_mtime
    except OSError:
        pass
    if best_mtime is not None:
        return best_mtime
    try:
        return local_dir.stat().st_mtime
    except OSError:
        return None


def _last_folder(path: str) -> str:
    """Extract the last path component."""
    return path.rstrip("/").rsplit("/", 1)[-1] if path else ""


# ─── Preview ─────────────────────────────────────────────────

@router.get("/preview", response_model=PreviewResponse)
async def preview() -> PreviewResponse:
    if state.dispatcher is None:
        raise HTTPException(503, "dispatcher not initialized")

    deps = state.dispatcher
    settings = load_settings()
    qbit_download_path = settings.get("qbit_download_path", "") or ""
    structure = settings.get("download_folder_structure", "monthly") or "monthly"
    if not qbit_download_path:
        raise HTTPException(400, "qbit_download_path not configured")

    torrents = await deps.qbit.list_torrents(category=deps.qbit_category)

    items: list[PreviewItem] = []
    need_move = 0
    already_ok = 0

    for t in torrents:
        local_save = translate_path(
            t.save_path, deps.qbit_path_prefix, deps.local_path_prefix
        )
        local_dir = Path(local_save) / t.name if t.name else Path(local_save)

        # Only inspect the specific torrent directory — never scan the
        # parent, which would pick up another torrent's file dates.
        mtime = _find_primary_mtime(local_dir)

        if mtime is not None:
            target_folder = _target_folder_for_mtime(mtime, structure)
            target_qbit = f"{qbit_download_path}/{target_folder}" if target_folder else qbit_download_path
        else:
            target_folder = None
            target_qbit = None

        correct = _is_already_correct(t.save_path, target_folder or "", structure) if target_folder is not None else False

        items.append(PreviewItem(
            hash=t.hash,
            name=t.name,
            current_path=t.save_path,
            current_folder=_last_folder(t.save_path),
            target_folder=target_folder,
            target_path=target_qbit,
            needs_move=not correct and target_qbit is not None,
            file_mtime=datetime.fromtimestamp(mtime).isoformat() if mtime else None,
        ))
        if correct:
            already_ok += 1
        elif target_qbit:
            need_move += 1

    return PreviewResponse(
        items=items,
        need_move_count=need_move,
        already_ok_count=already_ok,
        total=len(torrents),
    )


# ─── Background migration job ───────────────────────────────

@router.post("/start")
async def start_migration(body: StartRequest):
    """Kick off a background migration job. Returns immediately."""
    if state.dispatcher is None:
        raise HTTPException(503, "dispatcher not initialized")
    if state._migration_task is not None and not state._migration_task.done():
        raise HTTPException(409, "migration already running")

    # Reset status.
    state._migration_status = {
        "running": True,
        "done": 0,
        "total": len(body.hashes),
        "succeeded": 0,
        "failed": 0,
        "results": [],
        "finished": False,
        "dry_run": body.dry_run,
    }

    state._migration_task = asyncio.create_task(
        _run_migration(body.hashes, body.dry_run),
        name="migration-job",
    )
    return {"ok": True, "total": len(body.hashes)}


@router.get("/status", response_model=StatusResponse)
async def migration_status():
    """Poll the current migration job's progress."""
    s = state._migration_status
    return StatusResponse(
        running=s["running"],
        done=s["done"],
        total=s["total"],
        succeeded=s["succeeded"],
        failed=s["failed"],
        finished=s["finished"],
        dry_run=s["dry_run"],
        results=s["results"],
    )


@router.post("/cancel")
async def cancel_migration():
    """Cancel a running migration. Already-processed items stay moved."""
    if state._migration_task is None or state._migration_task.done():
        return {"ok": True, "was_running": False}
    state._migration_task.cancel()
    try:
        await state._migration_task
    except asyncio.CancelledError:
        pass
    state._migration_status["running"] = False
    state._migration_status["finished"] = True
    return {"ok": True, "was_running": True}


async def _run_migration(hashes: list[str], dry_run: bool) -> None:
    """Background coroutine that processes all hashes sequentially."""
    deps = state.dispatcher
    settings = load_settings()
    qbit_download_path = settings.get("qbit_download_path", "") or ""
    structure = settings.get("download_folder_structure", "monthly") or "monthly"
    qbit: QbitClient = deps.qbit  # type: ignore
    status = state._migration_status

    for idx, h in enumerate(hashes):
        if asyncio.current_task().cancelled():
            break

        # Re-fetch each torrent individually for fresh save_path.
        t = await deps.qbit.get_torrent(h)
        if t is None:
            status["results"].append({"hash": h, "name": "?", "ok": False, "error": "not found", "action": None})
            status["failed"] += 1
            status["done"] = idx + 1
            continue

        local_save = translate_path(
            t.save_path, deps.qbit_path_prefix, deps.local_path_prefix
        )
        local_dir = Path(local_save) / t.name if t.name else Path(local_save)
        mtime = _find_primary_mtime(local_dir)

        if mtime is None:
            status["results"].append({"hash": h, "name": t.name, "ok": False, "error": "could not determine mtime", "action": None})
            status["failed"] += 1
            status["done"] = idx + 1
            continue

        target_folder = _target_folder_for_mtime(mtime, structure)
        target_qbit = f"{qbit_download_path}/{target_folder}" if target_folder else qbit_download_path

        if _is_already_correct(t.save_path, target_folder, structure):
            status["results"].append({"hash": h, "name": t.name, "ok": True, "error": None, "action": "already correct"})
            status["succeeded"] += 1
            status["done"] = idx + 1
            continue

        action_desc = f"{_last_folder(t.save_path)} -> {target_folder or 'root'}"

        if dry_run:
            src_exists = local_dir.exists() or Path(local_save).exists()
            status["results"].append({
                "hash": h, "name": t.name, "ok": True,
                "action": f"DRY RUN: would move {action_desc}",
                "error": None if src_exists else "WARNING: source not found on disk",
            })
            status["succeeded"] += 1
            status["done"] = idx + 1
            continue

        # Pre-create the target folder.
        local_target = translate_path(target_qbit, deps.qbit_path_prefix, deps.local_path_prefix)
        ensure_folder_exists(local_target)

        try:
            ok = await _migrate_one(qbit, h, target_qbit)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("migration failed for %s", h)
            ok = False

        if ok:
            status["results"].append({"hash": h, "name": t.name, "ok": True, "error": None, "action": action_desc})
            status["succeeded"] += 1
            _log.info("migrated %s: %s", t.name, action_desc)
        else:
            status["results"].append({"hash": h, "name": t.name, "ok": False, "error": "move/recheck failed", "action": action_desc})
            status["failed"] += 1

        status["done"] = idx + 1

    status["running"] = False
    status["finished"] = True
    _log.info("migration job finished: %d succeeded, %d failed out of %d",
              status["succeeded"], status["failed"], status["total"])


# ─── Resume all ──────────────────────────────────────────────

@router.post("/resume-all")
async def resume_all():
    """Resume all stopped torrents in the watched category."""
    if state.dispatcher is None:
        raise HTTPException(503, "dispatcher not initialized")
    deps = state.dispatcher
    qbit: QbitClient = deps.qbit  # type: ignore
    torrents = await deps.qbit.list_torrents(category=deps.qbit_category)
    resumed = 0
    for t in torrents:
        if t.state.lower() in ("pausedup", "pauseddl", "stoppedup", "stoppeddl", "stopped"):
            ok = await qbit.resume_torrent(t.hash)
            if ok:
                resumed += 1
    return {"ok": True, "resumed": resumed, "total": len(torrents)}


# ─── Empty folder cleanup ────────────────────────────────────

class EmptyFolderItem(BaseModel):
    name: str
    path: str


class EmptyFoldersResponse(BaseModel):
    folders: list[EmptyFolderItem]
    root: str


class CleanupRequest(BaseModel):
    folders: list[str] = Field(..., min_length=1)


class CleanupResponse(BaseModel):
    deleted: int
    failed: int
    errors: list[str]


@router.get("/empty-folders", response_model=EmptyFoldersResponse)
async def list_empty_folders():
    """Scan the download root for completely empty subdirectories."""
    if state.dispatcher is None:
        raise HTTPException(503, "dispatcher not initialized")

    deps = state.dispatcher
    settings = load_settings()
    qbit_download_path = settings.get("qbit_download_path", "") or ""
    if not qbit_download_path:
        raise HTTPException(400, "qbit_download_path not configured")

    local_root = translate_path(
        qbit_download_path, deps.qbit_path_prefix, deps.local_path_prefix
    )
    root_path = Path(local_root)
    if not root_path.is_dir():
        return EmptyFoldersResponse(folders=[], root=local_root)

    empty: list[EmptyFolderItem] = []
    try:
        for child in sorted(root_path.iterdir()):
            if not child.is_dir():
                continue
            # A folder is "empty" if it contains zero regular files
            # (recursively). We don't count other empty subdirs.
            has_files = False
            try:
                for f in child.rglob("*"):
                    if f.is_file():
                        has_files = True
                        break
            except OSError:
                continue
            if not has_files:
                empty.append(EmptyFolderItem(
                    name=child.name,
                    path=str(child),
                ))
    except OSError:
        _log.exception("failed to scan %s for empty folders", local_root)

    return EmptyFoldersResponse(folders=empty, root=local_root)


@router.post("/cleanup", response_model=CleanupResponse)
async def cleanup_empty_folders(body: CleanupRequest):
    """Delete the specified empty folders."""
    if state.dispatcher is None:
        raise HTTPException(503, "dispatcher not initialized")

    deps = state.dispatcher
    settings = load_settings()
    qbit_download_path = settings.get("qbit_download_path", "") or ""
    local_root = translate_path(
        qbit_download_path, deps.qbit_path_prefix, deps.local_path_prefix
    )

    deleted = 0
    fail = 0
    errors: list[str] = []

    for folder_path in body.folders:
        p = Path(folder_path)
        # Safety: only delete folders that are direct children of the
        # download root to prevent path traversal.
        try:
            if p.parent != Path(local_root):
                errors.append(f"{p.name}: not a direct child of download root")
                fail += 1
                continue
            if not p.is_dir():
                errors.append(f"{p.name}: not found or not a directory")
                fail += 1
                continue
            # Verify still empty before deleting.
            has_files = any(f.is_file() for f in p.rglob("*"))
            if has_files:
                errors.append(f"{p.name}: no longer empty, skipped")
                fail += 1
                continue
            # rmtree to handle nested empty subdirs.
            import shutil
            shutil.rmtree(str(p))
            deleted += 1
            _log.info("cleaned up empty folder: %s", p)
        except Exception as e:
            errors.append(f"{p.name}: {ui_error(e)}")
            fail += 1

    return CleanupResponse(deleted=deleted, failed=fail, errors=errors)


# ─── Single torrent move ────────────────────────────────────

async def _migrate_one(qbit: QbitClient, torrent_hash: str, target_path: str) -> bool:
    """Relocate one torrent: [pause if active] -> setSavePath -> verify -> recheck -> [resume].

    After calling setSavePath we verify that qBit actually updated the
    torrent's save_path to the target. qBit v5's setSavePath can return
    200 without doing anything if the target directory doesn't exist or
    isn't writable — the 200 is NOT a reliable success signal.
    """
    info = await qbit.get_torrent(torrent_hash)
    if info is None:
        return False

    was_active = info.state.lower() not in (
        "pausedup", "pauseddl", "stoppedup", "stoppeddl", "stopped",
    )

    if was_active:
        if not await qbit.pause_torrent(torrent_hash):
            return False
        await asyncio.sleep(1)

    if not await qbit.set_location(torrent_hash, target_path):
        if was_active:
            await qbit.resume_torrent(torrent_hash)
        return False

    # Give qBit time to process the move, then verify it took effect.
    await asyncio.sleep(2)

    verify = await qbit.get_torrent(torrent_hash)
    if verify is None:
        _log.warning("torrent %s disappeared after setSavePath", torrent_hash)
        return False

    actual = verify.save_path.rstrip("/")
    expected = target_path.rstrip("/")
    if actual != expected:
        _log.warning(
            "setSavePath for %s did NOT take effect: expected %r, got %r",
            torrent_hash, expected, actual,
        )
        if was_active:
            await qbit.resume_torrent(torrent_hash)
        return False

    if not await qbit.recheck_torrent(torrent_hash):
        if was_active:
            await qbit.resume_torrent(torrent_hash)
        return False

    # Poll until recheck completes.
    for _ in range(120):
        await asyncio.sleep(2)
        check_info = await qbit.get_torrent(torrent_hash)
        if check_info is None:
            break
        if "checking" not in check_info.state.lower():
            break

    if was_active:
        await qbit.resume_torrent(torrent_hash)

    return True
