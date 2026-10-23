"""
Goodreads session diagnostic endpoints (v2.13.0 Stage 6 Phase A).

    GET  /api/v1/metadata/goodreads/state         — current session state
    POST /api/v1/metadata/goodreads/test          — probe Goodreads (single or burst)
    POST /api/v1/metadata/goodreads/mark-active   — manually clear the soft-block flag

State is the {state, since, last_status} tuple managed by
`app.metadata.goodreads_session`. The probe endpoint fires real
HTTP through the production session module so the user's wire-level
result matches whatever the live scan would see.

Probe mode `single` does ONE GET to a known-good Goodreads book and
returns a per-request report. Mode `burst` does N consecutive GETs
(default 10) against a canonical pool of books drawn from Mark's
production library (selected 2026-05-14 for v2.13.0 baseline). Each
burst request honors the production rate limit + jitter, so the
total wall time approximates a real-world scan slice — if the
burst result matches a real scan's per-book latency, the bypass is
working under realistic conditions.

Both modes share the same code path as the per-book scan, so
nothing about probe responses is "lab-only" — same TLS, same
headers, same rate limit, same soft-block detection.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.metadata import goodreads_session as gr
from app.metadata import source_gate

_log = logging.getLogger("seshat.routers.goodreads_session")

router = APIRouter(prefix="/api/v1/metadata/goodreads", tags=["goodreads-session"])


# Canonical probe pool. Selected 2026-05-14 from Mark's production
# library (seshat_calibre-library.db, books table) as the v2.13.0
# Phase-A baseline. Mix of recent (200M+ IDs) + older (30M-50M) entries
# across mainstream + indie authors so the burst test exercises a
# representative cross-section of Goodreads' catalog cardinality.
#
# These books all have populated `goodreads_id` columns at the time
# of selection — confirms the probe's "did we get real content?"
# check has a baseline of known-resolvable rows.
_DEFAULT_PROBE_POOL: list[dict[str, Any]] = [
    {"goodreads_id": "237832459", "title": "The Devil's Peak", "author": "Greig Beck"},
    {"goodreads_id": "213076829", "title": "Returner's Defiance", "author": "Bruce Sentar"},
    {"goodreads_id": "60548283",  "title": "Survivors 3: A Lost World Harem", "author": "Jack Porter"},
    {"goodreads_id": "34403860",  "title": "Sufficiently Advanced Magic", "author": "Andrew Rowe"},
    {"goodreads_id": "228713175", "title": "Phoenix Trials (Bloodline of the Phoenix Book 4)", "author": "S. D. McKittrick"},
    {"goodreads_id": "40581053",  "title": "Earth Unrelenting", "author": "M. R. Forbes"},
    {"goodreads_id": "237894285", "title": "These Heroines Are So High Maintenance", "author": "Virgil Knightley"},
    {"goodreads_id": "243255290", "title": "Trailer Park Bikini Vampires 2", "author": "Virgil Knightley"},
    {"goodreads_id": "48593270",  "title": "Metal Mage 8", "author": "Eric Vall"},
    {"goodreads_id": "35583546",  "title": "Defiance", "author": "Joel Shepherd"},
]


# ─── Request/response models ──────────────────────────────────────────


class ProbeRequest(BaseModel):
    mode: str = Field("single", pattern=r"^(single|burst)$")
    # Override the default pool. Each entry is just a goodreads_id
    # string. Burst-mode N defaults to len(book_ids); single picks
    # the first.
    book_ids: Optional[list[str]] = None


class ProbeResult(BaseModel):
    """One request's outcome. Returned bare for `mode=single`, or as a
    list inside the burst summary."""
    goodreads_id: str
    status: int
    body_size_kb: float
    wall_ms: int
    soft_blocked: bool


class BurstSummary(BaseModel):
    """Aggregate stats across a burst. Frontend renders this directly."""
    requests: int
    status_distribution: dict[int, int]
    soft_blocks: int
    total_wall_s: float
    mean_body_kb: float
    per_request: list[ProbeResult]


class ProbeResponse(BaseModel):
    mode: str
    state_after: dict  # whatever get_session_state() returns
    single: Optional[ProbeResult] = None
    burst: Optional[BurstSummary] = None


class StateResponse(BaseModel):
    state: str
    since: Optional[float]
    last_status: Optional[int]
    # Per request kind (book_page / list_page / autocomplete / other):
    # {"level", "until", "last_block_at", "backing_off"} (G66).
    backoff: dict = {}


class MarkActiveResponse(BaseModel):
    ok: bool
    state_after: dict


# ─── Probe primitive ─────────────────────────────────────────────────


async def _probe_one(book_id: str) -> ProbeResult:
    """Single Goodreads /book/show/{id} probe via the prod session.

    Reads the session state side-effect for free (mark_active /
    mark_soft_blocked happen inside `session.get()`). We re-check
    `is_soft_block()` here only to populate the per-request
    `soft_blocked` field in the response.
    """
    session = await gr.get_session()
    started = time.monotonic()
    try:
        # The probe exists to test whether a backoff is over, so it
        # goes out even while book pages are backing off.
        with source_gate.caller(source_gate.CALLER_PROBE):
            resp = await session.get(
                f"https://www.goodreads.com/book/show/{book_id}",
                ignore_backoff=True,
            )
    except Exception as e:
        # Transport-layer failure (timeout, DNS, TLS) — still report as
        # a probe outcome so the user sees something actionable.
        _log.warning("goodreads probe: transport error on book_id=%s: %s", book_id, e)
        wall_ms = int((time.monotonic() - started) * 1000)
        return ProbeResult(
            goodreads_id=book_id,
            status=0,
            body_size_kb=0.0,
            wall_ms=wall_ms,
            soft_blocked=False,
        )
    wall_ms = int((time.monotonic() - started) * 1000)
    body = getattr(resp, "content", b"") or b""
    return ProbeResult(
        goodreads_id=book_id,
        status=int(getattr(resp, "status_code", 0)),
        body_size_kb=round(len(body) / 1024.0, 2),
        wall_ms=wall_ms,
        soft_blocked=gr.is_soft_block(resp),
    )


# ─── Endpoints ───────────────────────────────────────────────────────


@router.get("/state", response_model=StateResponse)
async def get_state():
    """Current Goodreads session state. Frontend polls this for the
    status pill on the GoodreadsStatusCard."""
    s = gr.get_session_state()
    return StateResponse(
        state=s.get("state", "unknown"),
        since=s.get("since"),
        last_status=s.get("last_status"),
        backoff=s.get("backoff") or {},
    )


@router.post("/test", response_model=ProbeResponse)
async def probe(body: ProbeRequest):
    """Run a Phase-A probe against Goodreads.

    `mode=single` — one GET to the first book in `book_ids` (or the
    default pool's first book). Cheap diagnostic; takes one rate-limit
    interval (5s + jitter by default).

    `mode=burst` — N consecutive GETs against the full pool. Each
    request honors the production rate-limit / jitter, so the total
    wall time approximates an N-book slice of a real scan. This is
    the realistic-load test that catches density-based 202s.
    """
    pool = body.book_ids or [b["goodreads_id"] for b in _DEFAULT_PROBE_POOL]
    if not pool:
        raise HTTPException(400, "Empty probe pool")

    if body.mode == "single":
        result = await _probe_one(pool[0])
        return ProbeResponse(
            mode="single",
            state_after=gr.get_session_state(),
            single=result,
        )

    # Burst mode.
    started = time.monotonic()
    per_request: list[ProbeResult] = []
    for bid in pool:
        per_request.append(await _probe_one(bid))
    total_wall_s = round(time.monotonic() - started, 2)
    status_dist: dict[int, int] = {}
    soft_blocks = 0
    body_kb_total = 0.0
    for r in per_request:
        status_dist[r.status] = status_dist.get(r.status, 0) + 1
        if r.soft_blocked:
            soft_blocks += 1
        body_kb_total += r.body_size_kb
    mean_kb = round(body_kb_total / len(per_request), 2) if per_request else 0.0
    return ProbeResponse(
        mode="burst",
        state_after=gr.get_session_state(),
        burst=BurstSummary(
            requests=len(per_request),
            status_distribution=status_dist,
            soft_blocks=soft_blocks,
            total_wall_s=total_wall_s,
            mean_body_kb=mean_kb,
            per_request=per_request,
        ),
    )


@router.post("/mark-active", response_model=MarkActiveResponse)
async def mark_active():
    """End every request kind's backoff and flip the state to active.

    For when the user would rather not wait out a backoff. Doesn't
    probe; run one afterwards to see whether Goodreads answers.
    """
    gr.clear_all()
    return MarkActiveResponse(ok=True, state_after=gr.get_session_state())
