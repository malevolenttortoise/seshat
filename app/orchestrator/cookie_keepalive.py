"""
MAM cookie keep-alive loop.

MAM's session cookie has a sliding-window expiration: every API call
the backend receives RESETS the timer (and rotates the cookie value
via the Set-Cookie response header). As long as Seshat makes at
least one API call within a 15-day window, the cookie stays alive
indefinitely. Stop calling MAM for 15+ days and the cookie expires
silently — the next grab attempt will then fail with the
`cookie_expired` failure mode and the user has to manually paste a
new cookie.

The keep-alive loop exists to make 15-day idle periods impossible.
On a fixed interval (default 7 days = half the safety window) it
hits MAM's search endpoint with a tiny throwaway payload. The call
itself returns nothing useful — what matters is that MAM's cookie
rotation fires on the response, which:

  1. Resets MAM's expiration clock
  2. Updates Seshat's in-memory token via the rotation handler
  3. Schedules the debounced settings.json persist

Same shape as the budget watcher: a `tick()` function that does one
cycle, plus a `run_loop()` wrapper that supervises it. Tests target
`tick()` for fast in-process verification; the lifespan supervises
`run_loop()` in production.

Why 7 days and not e.g. 1 day:

  - Every grab and every IRC announce already triggers rotation, so
    in practice the cookie is rotating constantly during normal
    operation. Keep-alive only matters when ALL of those have been
    silent — which is rare.
  - Hitting MAM more often than necessary is bad citizenship and
    eats into MAM's API allowance.
  - 7 days is comfortably under the 15-day window even with ample
    margin for crashes, restarts, and clock skew.

Adjustable via `cookie_keepalive_interval_hours` in settings.json.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from app.mam.cookie import verify_session
from app.config import ui_error

_log = logging.getLogger("seshat.orchestrator.cookie_keepalive")


@dataclass(frozen=True)
class KeepaliveResult:
    """Outcome of one keep-alive cycle.

    Returned by `tick()` so the supervised loop can log appropriately
    and the dashboard can surface "last keep-alive" timestamps in a
    later phase.
    """

    success: bool
    message: str


async def tick() -> KeepaliveResult:
    """Run one keep-alive cycle: call MAM, let rotation fire.

    The actual rotation happens inside `verify_session` -> `_do_post`
    -> `_handle_response_cookie` — this function doesn't manipulate
    the token directly. We just need any successful HTTP call to MAM.

    `verify_session` is the right endpoint because:
      - It's already used by the validation flow (one less code
        path to maintain)
      - It returns a small JSON body (cheap)
      - MAM's response sets the rotated cookie header on success
      - It uses `_do_post` which goes through the rotation handler
      - We pass `token=""` so the in-memory current token (kept
        fresh by previous rotations) is used automatically

    Never raises — caller can rely on the returned `KeepaliveResult`
    to drive logging without exception handling.
    """
    try:
        # Pass empty token so `_resolve_token` falls back to the
        # in-memory `_current_token` that the cookie module keeps
        # updated. This is the production-correct path: keep-alive
        # uses whatever cookie was last rotated, NOT a stale value
        # we read at startup.
        result = await verify_session("")
    except Exception as e:
        _log.exception("cookie keep-alive tick raised")
        return KeepaliveResult(
            success=False,
            message=f"{type(e).__name__}: {ui_error(e)}",
        )

    if result["success"]:
        return KeepaliveResult(
            success=True,
            message=result.get("message", "ok"),
        )

    return KeepaliveResult(
        success=False,
        message=result.get("message", "unknown failure"),
    )


async def run_loop(
    *,
    interval_seconds: float,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Long-running supervised loop that fires `tick()` on schedule.

    Wrapped in `state.supervised_task()` from main.py's lifespan.
    Cancellation propagates cleanly via `asyncio.CancelledError`
    re-raise. Optional `stop_event` is for the smoke test pattern
    that wants exactly N ticks then a clean exit; the lifespan
    doesn't pass one and lets the surrounding asyncio task be
    cancelled at shutdown instead.

    Logging strategy: every successful tick logs at INFO so the user
    can watch the cookie staying alive in `docker compose logs`.
    Failures log at WARNING. Long quiet periods between ticks (7
    days by default) means the noise level stays low even on INFO.
    """
    _log.info(
        f"cookie keep-alive started "
        f"(interval={interval_seconds:.0f}s = "
        f"{interval_seconds / 3600:.1f}h)"
    )
    while True:
        result = await tick()
        if result.success:
            _log.info(f"cookie keep-alive tick OK: {result.message}")
        else:
            _log.warning(f"cookie keep-alive tick FAILED: {result.message}")

        if stop_event is not None and stop_event.is_set():
            _log.info("cookie keep-alive stop_event signaled, exiting loop")
            return

        try:
            if stop_event is not None:
                # Wait for either the interval OR a stop signal —
                # mirrors the budget watcher and IRC client manual-
                # stop guards so a shutdown doesn't have to wait
                # through a multi-day sleep.
                await asyncio.wait_for(
                    stop_event.wait(), timeout=interval_seconds
                )
                _log.info("cookie keep-alive stop_event during sleep, exiting loop")
                return
            else:
                await asyncio.sleep(interval_seconds)
        except asyncio.TimeoutError:
            continue  # interval elapsed normally; tick again
