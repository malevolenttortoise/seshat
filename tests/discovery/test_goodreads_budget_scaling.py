"""
v2.20.3 — Goodreads list-page-size-aware budget scaling.

Sanderson stress test 2026-05-22 revealed GR silently drops ~37% of
books for ≥200-book authors due to time-budget exhaustion across the
per-source 300s cap × retry cycle. These helpers scale the per-retry
timeout AND the per-author wall-clock budget after GR has reported
its list-page count via `_partial_state["total"]`.

These tests cover the pure-function tier math (200+ / 100+ / default).
The retry-loop wiring itself is exercised by the live discovery
integration tests; this file just pins the cutoffs.
"""
from __future__ import annotations

from app.discovery.lookup import (
    PER_AUTHOR_BUDGET_SEC,
    _scaled_goodreads_retry_timeout,
    _scaled_per_author_budget,
)


class TestScaledGoodreadsRetryTimeout:
    def test_small_author_keeps_base_timeout(self):
        # < 100 books → base (300s) cap unchanged.
        assert _scaled_goodreads_retry_timeout(300.0, 50) == 300.0
        assert _scaled_goodreads_retry_timeout(300.0, 99) == 300.0

    def test_medium_author_bumps_to_600(self):
        # 100+ → 600s tier.
        assert _scaled_goodreads_retry_timeout(300.0, 100) == 600.0
        assert _scaled_goodreads_retry_timeout(300.0, 150) == 600.0
        assert _scaled_goodreads_retry_timeout(300.0, 199) == 600.0

    def test_big_author_bumps_to_900(self):
        # 200+ → 900s tier (the Sanderson case).
        assert _scaled_goodreads_retry_timeout(300.0, 200) == 900.0
        assert _scaled_goodreads_retry_timeout(300.0, 399) == 900.0
        assert _scaled_goodreads_retry_timeout(300.0, 999) == 900.0

    def test_zero_book_count_keeps_base(self):
        # Before the first list-page parse we don't know the count; the
        # caller passes 0 and the helper must NO-OP rather than
        # accidentally cap to a stale tier.
        assert _scaled_goodreads_retry_timeout(300.0, 0) == 300.0

    def test_helper_never_shrinks_user_timeout(self):
        # If an operator has manually raised `spec.timeout_sec` (e.g. to
        # 1200s for their own profile), the tier cap must not silently
        # shrink it back down.
        assert _scaled_goodreads_retry_timeout(1200.0, 250) == 1200.0
        assert _scaled_goodreads_retry_timeout(1200.0, 150) == 1200.0


class TestScaledPerAuthorBudget:
    def test_small_author_keeps_default(self):
        assert _scaled_per_author_budget(PER_AUTHOR_BUDGET_SEC, 50) == (
            PER_AUTHOR_BUDGET_SEC
        )

    def test_medium_author_bumps_to_30_minutes(self):
        # 100+ → 30 min ceiling.
        assert _scaled_per_author_budget(PER_AUTHOR_BUDGET_SEC, 100) == 30 * 60
        assert _scaled_per_author_budget(PER_AUTHOR_BUDGET_SEC, 199) == 30 * 60

    def test_big_author_bumps_to_40_minutes(self):
        # 200+ → 40 min ceiling.
        assert _scaled_per_author_budget(PER_AUTHOR_BUDGET_SEC, 200) == 40 * 60
        assert _scaled_per_author_budget(PER_AUTHOR_BUDGET_SEC, 399) == 40 * 60

    def test_zero_book_count_keeps_default(self):
        assert _scaled_per_author_budget(PER_AUTHOR_BUDGET_SEC, 0) == (
            PER_AUTHOR_BUDGET_SEC
        )

    def test_helper_never_shrinks_user_budget(self):
        # If an operator has manually raised PER_AUTHOR_BUDGET_SEC past
        # the tier cap, the helper must not silently shrink it.
        assert _scaled_per_author_budget(60 * 60.0, 250) == 60 * 60.0
        assert _scaled_per_author_budget(45 * 60.0, 150) == 45 * 60.0
