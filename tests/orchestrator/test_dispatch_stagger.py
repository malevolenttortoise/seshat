"""
Tests for `_stagger_qbit_add()` — the module-level wallclock + lock
that spaces consecutive qBit `add_torrent` calls so MAM's per-IP
tracker throttle isn't tripped by bursts.

The settings the helper reads are live-loaded via `app.config.load_settings`,
so the tests monkeypatch that to inject controlled values rather than
writing a temporary settings.json. The module-level `_last_qbit_add_at`
is reset in a fixture so test order doesn't matter.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.orchestrator import dispatch as dispatch_mod


@pytest.fixture(autouse=True)
def _reset_stagger_state():
    """Reset module-level wallclock state around each test.

    The actual `_last_qbit_add_at` is module-level (not per-Dispatcher)
    because it models the GLOBAL qBit instance's throttle window. In
    production that's fine — there's only one qBit. In tests we need to
    poke it back to zero so a prior test's wallclock doesn't leak into
    the next test's gap calculation.
    """
    dispatch_mod._last_qbit_add_at = 0.0
    yield
    dispatch_mod._last_qbit_add_at = 0.0


def _patch_settings(monkeypatch, *, stagger_s: float, jitter_s: float = 0.0) -> None:
    """Force `load_settings()` to return the stagger pair we want.

    Real settings load is mtime-cached and bound to a JSON file on
    disk; we shortcut both by replacing the function entirely.
    """
    def _fake_load():
        return {
            "qbit_add_stagger_s": stagger_s,
            "qbit_add_stagger_jitter_s": jitter_s,
        }

    # `_stagger_qbit_add()` does `from app.config import load_settings`
    # inside the function body, so patch the source module not the
    # local symbol in `dispatch_mod`.
    monkeypatch.setattr("app.config.load_settings", _fake_load)


@pytest.mark.asyncio
async def test_disabled_when_stagger_zero(monkeypatch):
    """`qbit_add_stagger_s=0` short-circuits before any sleep."""
    _patch_settings(monkeypatch, stagger_s=0.0)
    started = time.monotonic()
    slept = await dispatch_mod._stagger_qbit_add()
    elapsed = time.monotonic() - started
    assert slept == 0.0
    assert elapsed < 0.05, "should not sleep when disabled"


@pytest.mark.asyncio
async def test_first_call_does_not_sleep(monkeypatch):
    """Initial call sees `_last_qbit_add_at=0` → elapsed huge → no sleep.

    The wallclock timer is reset to 0 by the fixture. `time.monotonic()`
    is a large positive number, so `elapsed` is enormous and the gap
    requirement is trivially satisfied.
    """
    _patch_settings(monkeypatch, stagger_s=1.0)
    started = time.monotonic()
    slept = await dispatch_mod._stagger_qbit_add()
    elapsed = time.monotonic() - started
    assert slept == 0.0
    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_second_call_sleeps_remaining_gap(monkeypatch):
    """A second call right after the first must sleep ~stagger_s.

    Jitter is set to 0 so the assertion is deterministic; the jitter
    branch is covered separately.
    """
    _patch_settings(monkeypatch, stagger_s=0.3, jitter_s=0.0)
    # First call to set `_last_qbit_add_at`.
    first_slept = await dispatch_mod._stagger_qbit_add()
    assert first_slept == 0.0

    started = time.monotonic()
    second_slept = await dispatch_mod._stagger_qbit_add()
    elapsed = time.monotonic() - started

    # Should sleep close to the full 0.3s (we just set the timer).
    assert 0.25 <= second_slept <= 0.35, f"unexpected second-call sleep: {second_slept}"
    # Real elapsed lines up with the reported sleep (within scheduler jitter).
    assert elapsed >= second_slept - 0.05


@pytest.mark.asyncio
async def test_concurrent_calls_serialize(monkeypatch):
    """Two concurrent tasks must both wait the configured gap apart.

    Without the lock both would see the same `_last_qbit_add_at`,
    compute the same gap, and resolve simultaneously — defeating the
    purpose of the stagger when the IRC listener fans out grabs.
    """
    _patch_settings(monkeypatch, stagger_s=0.2, jitter_s=0.0)

    # Prime the timer so both calls actually need to sleep.
    await dispatch_mod._stagger_qbit_add()

    start = time.monotonic()
    a, b = await asyncio.gather(
        dispatch_mod._stagger_qbit_add(),
        dispatch_mod._stagger_qbit_add(),
    )
    elapsed = time.monotonic() - start

    # Total wall time should be ~2 * stagger (each call sleeps the gap
    # since they execute back-to-back after the prime). If the lock
    # were missing both would finish in ~stagger_s.
    assert elapsed >= 0.35, f"concurrent calls didn't serialize (elapsed={elapsed:.2f})"
    # Each individual sleep should be near the full gap.
    assert a >= 0.15
    assert b >= 0.15


@pytest.mark.asyncio
async def test_negative_jitter_does_not_underflow(monkeypatch):
    """`stagger_s=0.1, jitter_s=10` could pick a negative target_gap.

    The helper must clamp to >= 0 so we don't pass a negative number
    to `asyncio.sleep` (which raises) or compute a nonsense elapsed
    comparison.
    """
    _patch_settings(monkeypatch, stagger_s=0.1, jitter_s=10.0)
    # 100 calls in a tight loop — with that much jitter at least one
    # iteration is guaranteed to land on a negative pre-clamp value.
    # The function should not raise.
    for _ in range(50):
        slept = await dispatch_mod._stagger_qbit_add()
        assert slept >= 0.0
