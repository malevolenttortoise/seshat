"""
Reingest endpoints — pull an already-snatched torrent from disk into
the Seshat pipeline without re-snatching from MAM.

When MAM reports a book as `my_snatched=true` (the user already
downloaded that torrent at some point, often before Seshat existed)
the discovery row gets flagged and the standard "Send to pipeline"
button is hidden — Seshat must never re-download a snatched torrent
(MAM duplicate-snatch penalty per the snatch-safety rule).

These endpoints expose a separate "Reingest from disk" path that:
  1. Probes qBit + the configured download folder for the existing
     files.
  2. Returns up to 5 candidates for the user to pick from (or
     auto-starts if exactly one is found).
  3. Hands the chosen candidate to the orchestrator's `start_reingest`
     helper, which synthesizes a `grabs` row (`is_reingest=1`,
     `state=STATE_DOWNLOADED`), creates a `pipeline_run`, and calls
     `process_completion` directly — skipping the MAM .torrent fetch
     and qBit submit phases that a normal grab would run.

Slug correctness: both endpoints require `?slug=` per
`feedback_seshat_multi_library_slug.md` since the books table is
per-library. Without it a numeric id collision across libraries
would let one library's reingest mutate another library's book.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app import state
from app.database import get_db as get_pipeline_db
from app.discovery.database import get_db as get_discovery_db
from app.mam.cookie import get_current_token as _get_mam_token
from app.mam.torrent_info import TorrentInfoError, get_torrent_info
from app.orchestrator.reingest import (
    Candidate,
    find_candidates,
    start_reingest,
)

_log = logging.getLogger("seshat.discovery.reingest")

router = APIRouter(prefix="/api/discovery", tags=["reingest"])


# ─── Response models ─────────────────────────────────────────


class CandidateModel(BaseModel):
    """Wire-shape of `Candidate` for the probe response.

    Carries the same fields the user picks from in the disambiguation
    modal. `score` is dropped from the API surface — see
    `Candidate.to_dict()` in the orchestrator module.
    """
    source: str
    display_path: str
    save_path: str
    book_files: list[str]
    qbit_hash: Optional[str] = None
    mtime: float = 0.0
    total_size: int = 0


class ProbeResponse(BaseModel):
    found: bool
    candidates: list[CandidateModel] = []
    # When the probe finds exactly one candidate AND the pipeline
    # ran cleanly, `auto_started=True` tells the UI to skip the
    # picker and show a success toast. When the pipeline ran but
    # failed mid-flight, `auto_started=False` + `error` is set so
    # the UI can show a clear error toast (not a misleading
    # success). The grab/run ids stay populated either way so the
    # audit trail row is reachable from the UI.
    auto_started: bool = False
    grab_id: Optional[int] = None
    pipeline_run_id: Optional[int] = None
    error: Optional[str] = None
    # When `found=False` the searched-sources list lets the UI explain
    # which paths were checked so the user can investigate (drive
    # unmounted, custom download dir, etc.).
    searched: list[str] = []
    mam_torrent_name: Optional[str] = None


class StartRequest(BaseModel):
    candidate: CandidateModel


class StartResponse(BaseModel):
    ok: bool
    grab_id: int
    pipeline_run_id: int
    error: Optional[str] = None


# ─── Helpers ─────────────────────────────────────────────────


async def _load_book_or_404(slug: Optional[str], book_id: int) -> dict[str, Any]:
    """Return the books row + joined author/series names, or 404.

    Validates the book is in the "Already Snatched" state — i.e. the
    state the UI's Reingest button surfaces on. Any other state
    (not found, possible, owned, no MAM URL) returns a clear 409 so
    the UI can surface why the action isn't available.
    """
    db = await get_discovery_db(slug=slug)
    try:
        row = await (await db.execute(
            "SELECT b.id, b.title, b.mam_torrent_id, b.mam_status, "
            "b.mam_my_snatched, b.owned, b.mam_category, "
            "a.name AS author_name "
            "FROM books b "
            "JOIN book_authors bpa ON bpa.book_id = b.id AND bpa.position = 0 "
            "JOIN authors a ON a.id = bpa.author_id "
            "WHERE b.id = ?",
            (book_id,),
        )).fetchone()
    finally:
        await db.close()

    if row is None:
        raise HTTPException(404, "book not found")
    if row["owned"]:
        raise HTTPException(409, "book is already owned — no reingest needed")
    if row["mam_status"] != "found" or not row["mam_torrent_id"]:
        raise HTTPException(
            409,
            f"book has mam_status={row['mam_status']!r}; reingest requires "
            "a confirmed MAM match (found + mam_torrent_id)",
        )
    if not row["mam_my_snatched"]:
        raise HTTPException(
            409,
            "book is not flagged as snatched on MAM; use the standard "
            "Send to pipeline button instead",
        )
    return dict(row)


async def _resolve_torrent_name(mam_torrent_id: str, fallback: str) -> str:
    """Fetch the canonical torrent name from MAM's search API.

    NOT a snatch — `torrent_info.php` returns metadata only. Falls
    back to the book's title when MAM is unreachable so the search
    can still try its best (the title often matches the filename
    closely enough that the fuzzy resolver finds something).
    """
    token = _get_mam_token()
    if not token:
        return fallback or ""
    try:
        info = await get_torrent_info(mam_torrent_id, token=token)
        return (info.title or "").strip() or fallback or ""
    except TorrentInfoError as exc:
        _log.warning(
            "reingest: get_torrent_info(%s) failed: %s — falling back to title",
            mam_torrent_id, exc,
        )
        return fallback or ""


def _candidate_to_wire(c: Candidate) -> CandidateModel:
    return CandidateModel(
        source=c.source,
        display_path=c.display_path,
        save_path=c.save_path,
        book_files=list(c.book_files),
        qbit_hash=c.qbit_hash,
        mtime=c.mtime,
        total_size=c.total_size,
    )


def _wire_to_candidate(c: CandidateModel) -> Candidate:
    return Candidate(
        source=c.source,
        display_path=c.display_path,
        save_path=c.save_path,
        book_files=list(c.book_files),
        qbit_hash=c.qbit_hash,
        mtime=c.mtime,
        total_size=c.total_size,
    )


# ─── Endpoints ──────────────────────────────────────────────


@router.post(
    "/books/{book_id}/reingest/probe",
    response_model=ProbeResponse,
)
async def probe_reingest(
    book_id: int,
    slug: Optional[str] = Query(None),
) -> ProbeResponse:
    """Search qBit + the configured download folder for the existing snatch.

    Returns up to 5 candidates ranked by name-match quality. If
    exactly ONE candidate is found, this endpoint auto-starts the
    pipeline (single-result auto-pick = the natural default) and
    returns `auto_started=True` with the grab/run ids. The UI can
    skip the disambiguation modal in that case.

    For multi-candidate results the user picks one via
    `/reingest/start`.
    """
    if state.dispatcher is None:
        raise HTTPException(503, "dispatcher not initialized")
    book = await _load_book_or_404(slug, book_id)

    mam_torrent_id = str(book["mam_torrent_id"]).strip()
    torrent_name = await _resolve_torrent_name(
        mam_torrent_id, fallback=book["title"] or "",
    )
    if not torrent_name:
        raise HTTPException(
            502,
            "could not resolve a torrent name for the reingest search "
            "(MAM unreachable and book title was empty)",
        )

    candidates = await find_candidates(
        state.dispatcher, mam_torrent_name=torrent_name,
    )
    # Surface which sources we actually searched so the UI can render
    # a clear "not found anywhere" message instead of a bare empty
    # state.
    searched: list[str] = []
    if state.dispatcher is not None and getattr(state.dispatcher, "qbit", None) is not None:
        searched.append("qBit (live torrent list)")
    from app.config import load_settings
    from app.orchestrator.download_folders import translate_path
    settings = load_settings()
    qbit_root = settings.get("qbit_download_path", "") or ""
    local_root = translate_path(
        qbit_root,
        settings.get("qbit_path_prefix", "") or "",
        settings.get("local_path_prefix", "") or "",
    )
    if local_root:
        searched.append(f"filesystem: {local_root}")

    if not candidates:
        return ProbeResponse(
            found=False, candidates=[], searched=searched,
            mam_torrent_name=torrent_name,
        )

    # Single-candidate auto-pick: start the pipeline immediately.
    if len(candidates) == 1:
        chosen = candidates[0]
        pdb = await get_pipeline_db()
        try:
            grab_id, pipeline_run_id, ok = await start_reingest(
                pdb,
                dispatcher=state.dispatcher,
                mam_torrent_id=mam_torrent_id,
                mam_torrent_name=torrent_name,
                category=book["mam_category"] or "",
                author_blob=book["author_name"] or "",
                candidate=chosen,
            )
        finally:
            await pdb.close()
        # v2.8.1: surface mid-pipeline failures from the auto-start
        # path. Pre-v2.8.1 we returned `auto_started=True` regardless
        # of `ok`, which made the UI show a misleading success toast
        # when (for example) qBit reported a file that wasn't
        # actually on disk and `process_completion` failed deep
        # inside staging. The grab/run rows still exist (audit
        # trail), but the user needs to know.
        return ProbeResponse(
            found=True,
            candidates=[_candidate_to_wire(chosen)],
            auto_started=bool(ok),
            grab_id=grab_id,
            pipeline_run_id=pipeline_run_id,
            error=None if ok else (
                f"reingest auto-start failed: pipeline_run "
                f"#{pipeline_run_id} did not complete — check "
                f"the pipeline_runs table for the recorded error."
            ),
            searched=searched,
            mam_torrent_name=torrent_name,
        )

    return ProbeResponse(
        found=True,
        candidates=[_candidate_to_wire(c) for c in candidates],
        auto_started=False,
        searched=searched,
        mam_torrent_name=torrent_name,
    )


@router.post(
    "/books/{book_id}/reingest/start",
    response_model=StartResponse,
)
async def start_reingest_endpoint(
    book_id: int,
    body: StartRequest,
    slug: Optional[str] = Query(None),
) -> StartResponse:
    """Commit a user-chosen candidate from a previous probe.

    The candidate object echoed back from the probe response is sent
    in the request body. The endpoint re-validates the book row's
    state (some other process could have changed it between probe
    and start), then calls `start_reingest()` to create the grab +
    pipeline_run and kick off `process_completion`.
    """
    if state.dispatcher is None:
        raise HTTPException(503, "dispatcher not initialized")
    book = await _load_book_or_404(slug, book_id)

    mam_torrent_id = str(book["mam_torrent_id"]).strip()
    # Re-resolve the torrent name from MAM. Using the wire candidate's
    # display_path here would let a malicious caller mismatch the
    # name vs the recorded grab — the canonical name comes from MAM.
    torrent_name = await _resolve_torrent_name(
        mam_torrent_id, fallback=book["title"] or "",
    )

    pdb = await get_pipeline_db()
    try:
        grab_id, pipeline_run_id, ok = await start_reingest(
            pdb,
            dispatcher=state.dispatcher,
            mam_torrent_id=mam_torrent_id,
            mam_torrent_name=torrent_name,
            category=book["mam_category"] or "",
            author_blob=book["author_name"] or "",
            candidate=_wire_to_candidate(body.candidate),
        )
    finally:
        await pdb.close()

    return StartResponse(
        ok=bool(ok),
        grab_id=grab_id,
        pipeline_run_id=pipeline_run_id,
        error=None if ok else (
            f"reingest pipeline_run #{pipeline_run_id} did not "
            "complete — check the pipeline_runs table for the "
            "recorded error."
        ),
    )
