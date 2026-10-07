"""
Manual Grab — the user hands Seshat specific MAM torrents to grab.

Two halves, both behind `app/routers/manual_grab.py`:

  - **Preview.** One row per pasted link or uploaded .torrent: what
    MAM says about the torrent, plus local state (already grabbed?
    owned? in flight?) and the policy tier the grab would get.
    Read-only: no announce or grab rows are written.
  - **Grab all.** The ticked rows go to a background job that grabs
    them one at a time and reports per-row status. The job lives in
    memory (D14): a restart loses the rows it hadn't reached; every row
    it did reach is an ordinary grab.

Every MAM request made here goes through `search_pacer` (lookups and
covers), so a batch never bursts. A paste grab is
`inject_grab` with the preview's metadata filled in, claim-for-owned
and format dedup off (the preview showed the user what they own;
ADR-0023), and the snatch-safety guards of ADR-0022 untouched.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from app import config
from app.filter.gate import Announce
from app.mam import search_pacer
from app.mam.torrent_id import extract_torrent_id
from app.mam.torrent_info import TorrentInfo, TorrentInfoError, TorrentNotFoundError
from app.mam.user_status import UserStatusError, get_user_status
from app.orchestrator.dispatch import (
    DispatcherDeps,
    DispatchResult,
    _build_economic_context,
    grab_uploaded_torrent,
    inject_grab,
)
from app.orchestrator.format_dedup import (
    lookup_dedup_siblings,
    media_type_from_category,
    normalize_dedup_key,
)
from app.policy.engine import evaluate_policy
from app.storage import grabs as grabs_storage

_log = logging.getLogger("seshat.orchestrator.manual_grab")

MAX_BATCH = 30

# Preview statuses. "Blocking" ones can never be ticked; the others
# start unticked and ticking them is the user's decision (D3, D13).
STATUS_READY = "ready"
STATUS_OWNED = "owned"
STATUS_IN_FLIGHT = "in_flight_sibling"
STATUS_SNATCHED = "snatched_on_mam"
STATUS_POLICY = "policy_skip"
STATUS_ALREADY_GRABBED = "already_grabbed"
STATUS_REMOVED = "removed_from_mam"
STATUS_EXCLUDED_UPLOADER = "excluded_uploader"
STATUS_BAD_INPUT = "bad_input"
STATUS_LOOKUP_FAILED = "lookup_failed"
# Upload-only (ADR-0023): the file must be this account's own MAM download.
STATUS_BAD_FILE = "bad_file"
STATUS_NOT_MAM_FILE = "not_mam_file"
STATUS_FOREIGN_FILE = "foreign_file"
STATUS_UID_UNKNOWN = "uid_unknown"

BLOCKING_STATUSES = frozenset({
    STATUS_ALREADY_GRABBED, STATUS_REMOVED, STATUS_EXCLUDED_UPLOADER,
    STATUS_BAD_INPUT, STATUS_LOOKUP_FAILED, STATUS_BAD_FILE,
    STATUS_NOT_MAM_FILE, STATUS_FOREIGN_FILE, STATUS_UID_UNKNOWN,
})

# A real .torrent tops out around 100 KB (largest on the host: 97 KB).
MAX_TORRENT_BYTES = 1_000_000


# ─── Preview ─────────────────────────────────────────────────


@dataclass
class PreviewRow:
    kind: str
    input: str
    status: str
    message: str = ""
    torrent_id: Optional[str] = None
    title: str = ""
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)
    series: list[dict[str, str]] = field(default_factory=list)
    category: str = ""
    filetype: str = ""
    size_bytes: Optional[int] = None
    seeders: Optional[int] = None
    vip: bool = False
    freeleech: bool = False
    personal_freeleech: bool = False
    my_snatched: bool = False
    owned_in: list[dict[str, str]] = field(default_factory=list)
    in_flight: bool = False
    policy_tier: str = ""
    policy_grab: bool = True
    wedge_eligible: bool = False
    grab_id: Optional[int] = None
    cover_url: Optional[str] = None
    info_hash: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _author_blob(info: TorrentInfo) -> str:
    return ", ".join(n.strip() for n in (info.authors or {}).values() if n and n.strip())


def _first_format(filetype: str) -> str:
    """MAM's `filetype` can list several ("m4a mp3"); the grab row wants one."""
    parts = (filetype or "").replace(",", " ").split()
    return parts[0].lower() if parts else ""


def _series_name(info: TorrentInfo) -> str:
    for value in (info.series or {}).values():
        if isinstance(value, list) and value and value[0]:
            return str(value[0])
    return ""


def _fill_from_info(row: PreviewRow, info: TorrentInfo) -> None:
    row.title = info.title
    row.authors = [n for n in (info.authors or {}).values() if n]
    row.narrators = [n for n in (info.narrators or {}).values() if n]
    row.series = [
        {"name": str(v[0]), "index": str(v[1]) if len(v) > 1 else ""}
        for v in (info.series or {}).values()
        if isinstance(v, list) and v and v[0]
    ]
    row.category = info.category
    row.filetype = info.filetype
    row.size_bytes = info.size_bytes
    row.seeders = info.seeders
    row.vip = info.vip
    row.freeleech = info.free or info.fl_vip
    row.personal_freeleech = info.personal_freeleech
    row.my_snatched = info.my_snatched
    row.wedge_eligible = not (
        info.vip or info.free or info.fl_vip or info.personal_freeleech
    )


async def preview_link(deps: DispatcherDeps, value: str) -> PreviewRow:
    """Build the preview row for one pasted MAM link or torrent ID."""
    row = PreviewRow(kind="link", input=value, status=STATUS_READY)
    tid = extract_torrent_id(value)
    if tid is None:
        row.status = STATUS_BAD_INPUT
        row.message = "Not a MAM torrent link or ID."
        return row
    row.torrent_id = tid
    return await _preview_torrent(deps, row)


async def preview_file(deps: DispatcherDeps, name: str, data: bytes) -> PreviewRow:
    """Build the preview row for one uploaded .torrent (ADR-0023).

    The file must be a MAM download by this account; then it previews
    like a link to its MID, except `my_snatched` is expected (the upload
    is the snatch) and a wedge is never offered (it rides on the MAM
    download, which already happened).
    """
    from app.mam.torrent_meta import BencodeError, info_hash, read_mam_comment

    row = PreviewRow(kind="file", input=name, status=STATUS_READY)
    if len(data) > MAX_TORRENT_BYTES:
        row.status = STATUS_BAD_FILE
        row.message = "Too big to be a .torrent file."
        return row
    try:
        row.info_hash = info_hash(data)
        stamp = read_mam_comment(data)
    except BencodeError:
        row.status = STATUS_BAD_FILE
        row.message = "Not a valid .torrent file."
        return row
    if stamp is None:
        row.status = STATUS_NOT_MAM_FILE
        row.message = "Not a MAM .torrent (no MAM torrent ID in it)."
        return row
    row.torrent_id = stamp.torrent_id

    uid = await _paced_account_uid(deps)
    if uid is None:
        row.status = STATUS_UID_UNKNOWN
        row.message = (
            "Can't confirm this .torrent is yours: Seshat can't read your "
            "MAM account right now. Check the cookie and try again."
        )
        return row
    if stamp.uid != uid:
        row.status = STATUS_FOREIGN_FILE
        row.message = (
            "This .torrent was downloaded by another MAM account; "
            "download your own copy from MAM."
        )
        return row
    return await _preview_torrent(deps, row)


async def _paced_account_uid(deps: DispatcherDeps) -> Optional[int]:
    token = deps.live_mam_token()
    if not token:
        return None
    try:
        status = await search_pacer.paced_user_status(token)
    except UserStatusError:
        return None
    return status.uid or None


async def _preview_torrent(deps: DispatcherDeps, row: PreviewRow) -> PreviewRow:
    """The shared half of a preview, once the row has a torrent ID."""
    tid = row.torrent_id or ""
    is_file = row.kind == "file"

    # Local guard first: a torrent Seshat already grabbed costs MAM
    # nothing to report, so it never reaches the pacer.
    db = await deps.db_factory()
    try:
        prior = await grabs_storage.find_blocking_grab(db, tid)
        if prior is None and row.info_hash:
            prior = await grabs_storage.find_grab_by_hash(db, row.info_hash)
    finally:
        await db.close()
    if prior is not None:
        row.status = STATUS_ALREADY_GRABBED
        row.title = prior.torrent_name
        row.grab_id = prior.id
        row.message = (
            f"Seshat already grabbed this (grab #{prior.id}, {prior.state}); "
            "it never downloads the same torrent twice."
        )
        return row

    token = deps.live_mam_token()
    if not token:
        row.status = STATUS_LOOKUP_FAILED
        row.message = "No MAM session cookie is set; add one in Settings."
        return row
    try:
        info = await search_pacer.paced_torrent_info(tid, token)
    except TorrentNotFoundError:
        row.status = STATUS_REMOVED
        row.message = "This torrent is no longer on MAM (removed, deleted or trumped)."
        return row
    except TorrentInfoError as e:
        row.status = STATUS_LOOKUP_FAILED
        row.message = lookup_failure_message(e)
        return row

    _fill_from_info(row, info)
    if is_file:
        # No wedge for an upload: `&fl` would be a second MAM download,
        # and "Buy as FL" is refused via the API (D35).
        row.wedge_eligible = False

    if (
        deps.excluded_uploaders
        and info.uploader_name
        and info.uploader_name.lower() in deps.excluded_uploaders
    ):
        row.status = STATUS_EXCLUDED_UPLOADER
        row.message = f"Uploaded by {info.uploader_name}, who is on your excluded-uploaders list."
        return row

    await _fill_local_state(row, info)
    await _fill_policy(row, deps, info)
    row.cover_url = await cover_url_for(tid, token)

    if row.my_snatched and not is_file:
        row.status = STATUS_SNATCHED
        row.message = "You already snatched this on MAM."
    elif row.owned_in:
        row.status = STATUS_OWNED
        where = ", ".join(
            f"{o['library_slug']}" + (f" ({o['format']})" if o["format"] else "")
            for o in row.owned_in
        )
        row.message = f"Owned in {where}."
    elif row.in_flight:
        row.status = STATUS_IN_FLIGHT
        row.message = "Another copy of this book is already on its way."
    elif not row.policy_grab:
        row.status = STATUS_POLICY
        row.message = _policy_message(row.policy_tier)
    return row


async def _fill_local_state(row: PreviewRow, info: TorrentInfo) -> None:
    media_type = media_type_from_category(info.category or "")
    dedup_key = normalize_dedup_key(info.title or "", _author_blob(info))
    if not media_type or not dedup_key:
        return
    try:
        siblings = await lookup_dedup_siblings(dedup_key=dedup_key, media_type=media_type)
    except Exception:
        _log.exception("manual grab: sibling lookup failed for tid=%s", info.torrent_id)
        return
    for s in siblings:
        if s.where == "owned":
            row.owned_in.append({"library_slug": s.library_slug or "", "format": s.book_format})
        else:
            row.in_flight = True


async def _fill_policy(row: PreviewRow, deps: DispatcherDeps, info: TorrentInfo) -> None:
    announce = Announce(
        torrent_id=info.torrent_id,
        torrent_name=info.title,
        category=info.category,
        author_blob=_author_blob(info),
    )
    try:
        eco = await _build_economic_context(deps, announce)
        decision = evaluate_policy(eco, deps.policy_config)
    except Exception:
        _log.exception("manual grab: policy preview failed for tid=%s", info.torrent_id)
        return
    row.policy_tier = decision.tier
    row.policy_grab = decision.action == "grab"


def lookup_failure_message(e: TorrentInfoError) -> str:
    """What went wrong with a MAM lookup, in words the row can show (D26)."""
    import httpx

    if isinstance(e.__cause__, httpx.TimeoutException):
        return (
            f"MAM didn't answer in time ({type(e.__cause__).__name__}). "
            "Try again in a moment."
        )
    return f"Couldn't reach MAM ({e}). Try again in a moment."


def _policy_message(tier: str) -> str:
    return {
        "buffer_insufficient": (
            "Your upload buffer is too low for this one. Tick Personal FL, "
            "or buy upload credit first."
        ),
        "vip_required": "Your policy only grabs VIP torrents.",
        "free_required": "Your policy only grabs freeleech torrents.",
        "wedge_reserve": "Your policy needs a wedge here and you're at your reserve.",
        "ratio_too_low": "Your ratio is below your policy's floor.",
    }.get(tier, f"Your grab policy would skip this ({tier}).")


# ─── Covers ──────────────────────────────────────────────────


_COVER_DIRNAME = "manual-grab-covers"
_COVER_MAX_AGE_S = 7 * 24 * 3600
_NO_COVER_MARKER = ".none"


def cover_dir() -> Path:
    # Per call so the test suite's DATA_DIR redirect applies.
    return Path(config.DATA_DIR) / _COVER_DIRNAME


def cached_cover_path(torrent_id: str) -> Optional[Path]:
    folder = cover_dir() / str(torrent_id)
    if not folder.is_dir():
        return None
    for p in folder.iterdir():
        if p.is_file() and p.name.startswith("cover-mam"):
            return p
    return None


async def cover_url_for(torrent_id: str, token: str) -> Optional[str]:
    """Thumbnail for a preview row: cached on disk, else one paced CDN fetch (D12)."""
    from app.metadata.covers import fetch_mam_cover

    folder = cover_dir() / str(torrent_id)
    if cached_cover_path(torrent_id) is not None:
        return f"/api/v1/manual-grab/cover/{torrent_id}"
    if (folder / _NO_COVER_MARKER).exists():
        return None
    _prune_covers()
    path = await search_pacer.paced(
        lambda: fetch_mam_cover(
            torrent_id, dest_dir=folder, basename="cover-mam", token=token,
        ),
        label=f"cover tid={torrent_id}",
    )
    if path is None:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / _NO_COVER_MARKER).touch()
        except OSError:
            pass
        return None
    return f"/api/v1/manual-grab/cover/{torrent_id}"


def _prune_covers() -> None:
    root = cover_dir()
    if not root.is_dir():
        return
    cutoff = time.time() - _COVER_MAX_AGE_S
    for entry in root.iterdir():
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


# ─── Wedges (D9) ─────────────────────────────────────────────


@dataclass
class WedgeBudget:
    wedges: int
    reserved: int

    @property
    def spendable(self) -> int:
        return max(0, self.wedges - self.reserved)

    def to_dict(self) -> dict[str, int]:
        return {"wedges": self.wedges, "reserved": self.reserved, "spendable": self.spendable}


async def wedge_budget(deps: DispatcherDeps, *, fresh: bool = False) -> Optional[WedgeBudget]:
    """The account's wedges and the reserve the policy keeps back.

    `fresh=True` re-reads MAM (through the pacer) instead of the cached
    status: Grab all checks the batch against it, and the cached count
    can be minutes old. None when the account can't be read.
    """
    token = deps.live_mam_token()
    if not token:
        return None
    try:
        if fresh:
            status = await search_pacer.paced(
                lambda: get_user_status(token=token, ttl=0), label="user status (fresh)",
            )
        else:
            status = await search_pacer.paced_user_status(token)
    except UserStatusError:
        return None
    return WedgeBudget(
        wedges=int(status.wedges or 0),
        reserved=int(deps.policy_config.min_wedges_reserved or 0),
    )


def wedge_shortfall_message(needed: int, budget: WedgeBudget) -> str:
    return (
        f"Needs {needed} wedge{'s' if needed != 1 else ''}, {budget.spendable} spendable "
        f"({budget.wedges} − {budget.reserved} reserved). "
        "Untick rows or turn wedges off."
    )


# ─── Grab all (background job) ───────────────────────────────


@dataclass
class GrabRequestItem:
    kind: str                     # "link" | "file"
    value: str                    # the link, or the file's name
    override_mam_snatched: bool = False
    data: Optional[bytes] = None  # the .torrent bytes, for "file"
    # Set on each eligible row when the batch "Use wedges" toggle is on
    # (D7). Links only: an app can spend a wedge only with `&fl=1` on the
    # MAM download, and MAM refuses "Buy as FL" via the API (D35).
    use_wedge: bool = False


@dataclass
class JobRow:
    index: int
    kind: str
    input: str
    torrent_id: Optional[str] = None
    status: str = "pending"   # pending | working | submitted | queued | refused | failed
    reason: str = ""
    message: str = ""
    grab_id: Optional[int] = None
    wedge_used: bool = False


@dataclass
class Job:
    id: str
    created_at: float
    rows: list[JobRow]
    done: bool = False
    finished_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "done": self.done,
            "rows": [asdict(r) for r in self.rows],
        }


_JOB_TTL_S = 3600
_jobs: dict[str, Job] = {}
_tasks: set[asyncio.Task] = set()


def get_job(job_id: str) -> Optional[Job]:
    return _jobs.get(job_id)


def _prune_jobs() -> None:
    now = time.time()
    for jid in [
        jid for jid, j in _jobs.items()
        if j.done and j.finished_at and now - j.finished_at > _JOB_TTL_S
    ]:
        _jobs.pop(jid, None)


def start_job(deps: DispatcherDeps, items: list[GrabRequestItem]) -> Job:
    """Create the job and start grabbing in the background."""
    _prune_jobs()
    job = Job(
        id=uuid.uuid4().hex,
        created_at=time.time(),
        rows=[
            JobRow(index=i, kind=item.kind, input=item.value)
            for i, item in enumerate(items)
        ],
    )
    _jobs[job.id] = job
    task = asyncio.create_task(_run_job(deps, job, items))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


async def _run_job(
    deps: DispatcherDeps, job: Job, items: list[GrabRequestItem],
) -> None:
    try:
        for row, item in zip(job.rows, items):
            row.status = "working"
            try:
                if item.kind == "file":
                    await _grab_file(deps, row, item)
                else:
                    await _grab_link(deps, row, item)
            except Exception as e:
                _log.exception("manual grab: row %d crashed", row.index)
                row.status = "failed"
                row.message = f"Unexpected error: {e}"
    finally:
        job.done = True
        job.finished_at = time.time()
        _log.info(
            "manual grab job %s: %s", job.id,
            ", ".join(f"{r.torrent_id or r.input}={r.status}" for r in job.rows),
        )


async def _grab_link(
    deps: DispatcherDeps, row: JobRow, item: GrabRequestItem,
) -> None:
    tid = extract_torrent_id(item.value)
    if tid is None:
        row.status, row.reason = "failed", "bad_input"
        row.message = "Not a MAM torrent link or ID."
        return
    row.torrent_id = tid
    token = deps.live_mam_token()
    if not token:
        row.status, row.reason = "failed", "no_cookie"
        row.message = "No MAM session cookie is set; add one in Settings."
        return

    # Warm the torrent-info cache through the pacer, so the lookups
    # inside inject_grab (guard, auto-train, economics) are cache hits
    # rather than unpaced MAM calls. A lookup that fails here stops the
    # row instead of letting inject_grab retry it unpaced.
    info = await _warm(row, tid, token)
    if info is None:
        return

    # The batch wedge applies only where it buys something: not on a
    # torrent that's free by now (the dispatcher re-checks, D28).
    use_wedge = item.use_wedge and not (
        info.vip or info.free or info.fl_vip or info.personal_freeleech
    )
    result = await inject_grab(
        deps,
        torrent_id=tid,
        torrent_name=info.title,
        category=info.category,
        author_blob=_author_blob(info),
        series_name=_series_name(info),
        book_title=info.title,
        filetype=_first_format(info.filetype),
        raw_line=f"manual_grab:link:{tid}",
        apply_format_dedup=False,
        apply_claim_for_owned=False,
        override_mam_snatched=item.override_mam_snatched,
        force_fl_wedge=use_wedge,
    )
    # A hash means MAM served the bytes, i.e. the &fl=1 download happened.
    row.wedge_used = use_wedge and result.qbit_hash is not None
    _apply_result(row, result)


async def _grab_file(
    deps: DispatcherDeps, row: JobRow, item: GrabRequestItem,
) -> None:
    """Grab an uploaded .torrent: its own bytes, never a MAM download."""
    from app.mam.torrent_meta import BencodeError, read_mam_comment

    data = item.data or b""
    try:
        stamp = read_mam_comment(data)
    except BencodeError:
        stamp = None
    token = deps.live_mam_token()
    if stamp is not None:
        row.torrent_id = stamp.torrent_id
    # Warm what grab_uploaded_torrent reads (account uid, then torrent
    # info) through the pacer, so its own lookups are cache hits. A file
    # that isn't ours never costs a torrent-info lookup: it's refused below.
    if (
        stamp is not None
        and token
        and await _paced_account_uid(deps) == stamp.uid
    ):
        if await _warm(row, stamp.torrent_id, token) is None:
            return
    result = await grab_uploaded_torrent(deps, torrent_bytes=data)
    _apply_result(row, result)


async def _warm(row: JobRow, tid: str, token: str) -> Optional[TorrentInfo]:
    try:
        return await search_pacer.paced_torrent_info(tid, token)
    except TorrentNotFoundError:
        row.status, row.reason = "refused", "torrent_removed_from_mam"
        row.message = "This torrent is no longer on MAM; nothing was downloaded."
    except TorrentInfoError as e:
        row.status, row.reason = "failed", "lookup_failed"
        row.message = f"{lookup_failure_message(e)} Nothing was downloaded."
    return None


def _apply_result(row: JobRow, result: DispatchResult) -> None:
    row.grab_id = result.grab_id
    row.reason = result.reason
    if result.action == "submit" and result.error is None:
        row.status, row.message = "submitted", "Sent to qBittorrent."
    elif result.action == "queue" and result.error is None:
        row.status, row.message = "queued", "Queued: your snatch budget is full."
    elif result.action == "drop":
        row.status = "refused"
        row.message = "Snatch budget and queue are both full; nothing was downloaded."
    elif result.action == "skip":
        row.status = "refused"
        row.message = result.error or _skip_message(result.reason)
    else:
        row.status = "failed"
        row.message = result.error or result.reason


def _skip_message(reason: str) -> str:
    if reason == "lookup_failed":
        return "Couldn't reach MAM; nothing was grabbed. Try again."
    if reason.startswith("policy:"):
        return _policy_message(reason.split(":", 1)[1])
    if reason.startswith("dry_run"):
        return "Dry run is on; nothing was grabbed."
    if reason.startswith("excluded_uploader"):
        return "The uploader is on your excluded-uploaders list."
    return f"Not grabbed ({reason})."
