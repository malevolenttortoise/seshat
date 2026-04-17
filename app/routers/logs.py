"""
Log viewer endpoint.

    GET /api/v1/logs?lines=200&filter=announce

Reads from Seshat's in-memory log buffer. Uses Python's logging
module to capture recent log records into a bounded deque, so no
file I/O is needed and the endpoint works regardless of how the
container's stdout is configured.

Two views for the UI:
  - "all" — full application log (dispatcher, budget watcher, IRC,
    pipeline, enricher, etc.)
  - "announces" — just the IRC announce events (filtered by logger
    name prefix "seshat.mam.irc" or message content)

The buffer is capped at 5000 records to limit memory usage. Older
records are evicted FIFO. The endpoint returns newest-first so the
UI doesn't need to scroll to the bottom.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/logs", tags=["logs"])


# ─── In-memory log buffer ──────────────────────────────────────

# Ring buffer capacity. Sized to hold roughly a day of steady-state
# log output even during active IRC hours. Overflow evicts FIFO, so
# announces older than the buffer are gone — the `announces` DB
# table is the authoritative long-term record, the buffer is just
# the fast path for the in-app viewer. Bumped from 5000 to 20000
# in a v1.1 hotfix after users reported losing overnight history.
_MAX_RECORDS = 20000
_buffer: deque[dict] = deque(maxlen=_MAX_RECORDS)


class _BufferHandler(logging.Handler):
    """Captures log records into the bounded deque."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": self.format(record).split(" ")[0] + " " + self.format(record).split(" ")[1]
                if " " in self.format(record) else "",
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "is_announce": (
                    record.name.startswith("seshat.orchestrator.dispatch")
                ),
            }
            _buffer.append(entry)
        except Exception:
            pass


_handler_installed = False


def install_log_handler() -> None:
    """Attach the buffer handler to the root seshat logger.

    Called once from main.py's lifespan. Safe to call multiple times
    (idempotent).
    """
    global _handler_installed
    if _handler_installed:
        return
    handler = _BufferHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger("seshat").addHandler(handler)
    _handler_installed = True


# ─── Endpoint ──────────────────────────────────────────────────


class LogEntry(BaseModel):
    ts: str
    level: str
    logger: str
    message: str
    is_announce: bool


class LogsResponse(BaseModel):
    entries: list[LogEntry]
    total_buffered: int


@router.get("", response_model=LogsResponse)
async def get_logs(
    lines: int = Query(200, ge=1, le=5000),
    filter: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
) -> LogsResponse:
    """Return recent log entries, newest first.

    Query params:
      - lines: max number of entries to return (default 200)
      - filter: "announces" to show only announce-related entries,
                or any substring to filter messages
      - category: logger-name-prefix filter for the log viewer's
                  tab system. Supported values:
                    "irc"         — only `seshat.mam.irc.*`
                    "application" — everything NOT under
                                    `seshat.mam.irc` (app-level
                                    events: pipeline, budget,
                                    enricher, etc.)
                  Omit (or pass "all") for the full stream.
                  Combines with `filter` when both are provided.
    """
    entries = list(_buffer)
    entries.reverse()  # newest first

    # Category filter runs first — it's the coarsest slice and
    # narrows the dataset before the message-level filter.
    if category == "irc":
        entries = [e for e in entries if e["logger"].startswith("seshat.mam.irc")]
    elif category == "application":
        entries = [e for e in entries if not e["logger"].startswith("seshat.mam.irc")]
    elif category == "scans":
        entries = [e for e in entries if e["logger"].startswith("seshat.discovery")]
    # "all" / None = no category filter

    if filter == "announces":
        entries = [e for e in entries if e["is_announce"]]
    elif filter:
        needle = filter.lower()
        entries = [
            e for e in entries
            if needle in e["message"].lower()
            or needle in e["logger"].lower()
        ]

    entries = entries[:lines]
    return LogsResponse(
        entries=[LogEntry(**e) for e in entries],
        total_buffered=len(_buffer),
    )
