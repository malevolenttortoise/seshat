"""
In-memory background jobs: start, then poll.

Manual Grab's Grab all introduced this (D14): the request starts the job
and returns at once, and the page polls for per-row status. Since every
MAM request is paced (issue 05), a batch that grabs from MAM takes
minutes, past the ~60-100s a reverse proxy waits for one request (a
31-row tentative approve already ran ~2 minutes before pacing). The
batch grab paths from Discovery (send to pipeline) and the Tentative page
(bulk approve) run as jobs too (ADR-0024).

Jobs live in memory. A restart loses the rows a job hadn't reached;
every row it did reach is an ordinary grab, in the grab history. A
finished job is kept for an hour so a slow poller still sees the end.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, Optional

_log = logging.getLogger("seshat.orchestrator.jobs")

JOB_TTL_S = 3600

# A poll for a job this process doesn't know: the 404 detail.
GONE_MESSAGE = (
    "No such batch. Seshat may have restarted; anything it had "
    "already grabbed is in the grab history."
)


class JobRegistry:
    """Jobs by id, plus strong refs to their tasks (so none is collected
    mid-run). Each job type keeps its own registry."""

    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}
        self.tasks: set[asyncio.Task] = set()

    def prune(self) -> None:
        now = time.time()
        for jid in [
            jid for jid, j in self.jobs.items()
            if j.done and j.finished_at and now - j.finished_at > JOB_TTL_S
        ]:
            self.jobs.pop(jid, None)

    def add(self, job: Any, run: Optional[Coroutine[Any, Any, None]] = None) -> None:
        """Register `job`; run `run` in the background if given."""
        self.prune()
        self.jobs[job.id] = job
        if run is not None:
            task = asyncio.create_task(run)
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    def get(self, job_id: str) -> Optional[Any]:
        return self.jobs.get(job_id)


@dataclass
class BatchJob:
    """A batch grab running in the background (ADR-0024).

    `rows` carry per-row status (`pending` → `working` → `done`) for
    progress; `result` is, once `done`, exactly what the endpoint used to
    return in one response.
    """

    id: str
    kind: str
    created_at: float
    rows: list[dict[str, Any]]
    done: bool = False
    finished_at: Optional[float] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "done": self.done,
            "total": len(self.rows),
            "completed": sum(1 for r in self.rows if r.get("status") == "done"),
            "rows": self.rows,
            "result": self.result,
            "error": self.error,
        }


batch_jobs = JobRegistry()


def start_batch_job(
    kind: str,
    rows: list[dict[str, Any]],
    run: Callable[[BatchJob], Awaitable[dict[str, Any]]],
) -> BatchJob:
    """Start `run(job)` in the background; its return value is the result."""
    job = BatchJob(id=uuid.uuid4().hex, kind=kind, created_at=time.time(), rows=rows)

    async def _run() -> None:
        try:
            job.result = await run(job)
        except Exception as e:
            _log.exception("%s job %s crashed", kind, job.id)
            job.error = f"Unexpected error: {e}"
        finally:
            job.done = True
            job.finished_at = time.time()

    batch_jobs.add(job, _run())
    return job


def finished_batch_job(kind: str, result: dict[str, Any]) -> BatchJob:
    """A job with nothing to do: done at once, `result` already known."""
    now = time.time()
    job = BatchJob(
        id=uuid.uuid4().hex, kind=kind, created_at=now, rows=[],
        done=True, finished_at=now, result=result,
    )
    batch_jobs.add(job)
    return job


def get_batch_job(job_id: str, kind: str) -> Optional[BatchJob]:
    job = batch_jobs.get(job_id)
    if job is None or job.kind != kind:
        return None
    return job
