"""Cover-image verification (Part C) tests.

Focused unit coverage for `_annotate_candidate_covers` — the helper
that fetches MAM candidate covers, computes pHash distance against
the searched book's cover, and tags each candidate with a signal
(promote/neutral/demote/skipped_bundle/no_data/not_evaluated).

The full `check_book` integration is exercised end-to-end via the
debug-match endpoint and Mark's UAT — see step 5 for that path.
"""
from typing import Optional

import pytest

from app.discovery.sources import mam as mam_mod
from app.discovery.sources.mam import (
    _COVER_DEMOTE_DIST_MIN,
    _COVER_PROMOTE_DIST_MAX,
    _annotate_candidate_covers,
)


def _candidate(
    tid: str,
    *,
    confidence: float = 0.5,
    is_bundle: bool = False,
) -> dict:
    """Minimal candidate dict mirroring `_evaluate_results` output shape."""
    return {
        "torrent_id": tid,
        "mam_title": f"Title {tid}",
        "confidence": confidence,
        "is_bundle": is_bundle,
        "format_str": "epub",
        "match_pct": confidence * 100,
    }


def _patch_fetcher(
    monkeypatch, by_tid: dict[str, Optional[str]]
) -> dict[str, int]:
    """Monkeypatch `fetch_and_hash_mam_cover` to return canned values per
    torrent_id. Returns a call-count dict so tests can assert on fetch
    behavior (cache hit vs. miss).
    """
    calls: dict[str, int] = {}

    async def _fake(tid: str, token: str, *, db=None) -> Optional[str]:
        calls[tid] = calls.get(tid, 0) + 1
        return by_tid.get(tid)

    # cover_hash is imported INSIDE _annotate_candidate_covers, so we
    # have to patch it on the cover_hash module — not on mam.
    from app.mam import cover_hash
    monkeypatch.setattr(cover_hash, "fetch_and_hash_mam_cover", _fake)
    return calls


# Use real imagehash hex strings so distance comparisons aren't fake.
# Same hash → distance 0 (promote band).
# Distance derived from hamming on hex chars: each different bit = 1.
_HASH_A = "0000000000000000"
_HASH_A_NEAR = "0000000000000003"  # 2 bits flipped → distance 2
_HASH_A_DEADBAND = "00000000000000ff"  # 8 bits flipped → distance 8 (still promote)
_HASH_A_FAR = "0000000000000fff"  # 12 bits flipped → distance 12 (deadband)
_HASH_FULL_DIFF = "ffffffffffffffff"  # 64 bits flipped → distance 64 (demote)


# ─── Promote/demote signal classification ────────────────────────


class TestAnnotateCandidateCovers:
    @pytest.mark.asyncio
    async def test_no_op_on_empty_candidates(self, monkeypatch):
        calls = _patch_fetcher(monkeypatch, {})
        await _annotate_candidate_covers([], _HASH_A, "tok", {})
        assert calls == {}

    @pytest.mark.asyncio
    async def test_no_op_when_seshat_phash_empty(self, monkeypatch):
        calls = _patch_fetcher(monkeypatch, {"1": _HASH_A})
        c = [_candidate("1")]
        await _annotate_candidate_covers(c, "", "tok", {})
        # Without a target hash, no fetches and no annotations.
        assert calls == {}
        assert "cover_signal" not in c[0]

    @pytest.mark.asyncio
    async def test_promote_at_distance_zero(self, monkeypatch):
        _patch_fetcher(monkeypatch, {"1": _HASH_A})
        c = [_candidate("1", confidence=0.4)]
        await _annotate_candidate_covers(c, _HASH_A, "tok", {})
        assert c[0]["cover_distance"] == 0
        assert c[0]["cover_signal"] == "promote"
        assert c[0]["mam_cover_phash"] == _HASH_A

    @pytest.mark.asyncio
    async def test_promote_at_threshold_boundary(self, monkeypatch):
        _patch_fetcher(monkeypatch, {"1": _HASH_A_DEADBAND})
        c = [_candidate("1")]
        await _annotate_candidate_covers(c, _HASH_A, "tok", {})
        # Distance 8 is <= _COVER_PROMOTE_DIST_MAX (10) → promote.
        assert c[0]["cover_distance"] <= _COVER_PROMOTE_DIST_MAX
        assert c[0]["cover_signal"] == "promote"

    @pytest.mark.asyncio
    async def test_neutral_in_deadband(self, monkeypatch):
        _patch_fetcher(monkeypatch, {"1": _HASH_A_FAR})
        c = [_candidate("1")]
        await _annotate_candidate_covers(c, _HASH_A, "tok", {})
        # Distance 12 is in deadband (11-21).
        assert _COVER_PROMOTE_DIST_MAX < c[0]["cover_distance"] < _COVER_DEMOTE_DIST_MIN
        assert c[0]["cover_signal"] == "neutral"

    @pytest.mark.asyncio
    async def test_demote_at_full_distance(self, monkeypatch):
        _patch_fetcher(monkeypatch, {"1": _HASH_FULL_DIFF})
        c = [_candidate("1")]
        await _annotate_candidate_covers(c, _HASH_A, "tok", {})
        assert c[0]["cover_distance"] >= _COVER_DEMOTE_DIST_MIN
        assert c[0]["cover_signal"] == "demote"

    @pytest.mark.asyncio
    async def test_no_data_when_fetch_returns_none(self, monkeypatch):
        _patch_fetcher(monkeypatch, {"1": None})
        c = [_candidate("1")]
        await _annotate_candidate_covers(c, _HASH_A, "tok", {})
        assert c[0]["cover_distance"] is None
        assert c[0]["cover_signal"] == "no_data"

    @pytest.mark.asyncio
    async def test_bundles_skipped(self, monkeypatch):
        calls = _patch_fetcher(monkeypatch, {"1": _HASH_A})
        c = [_candidate("1", is_bundle=True)]
        await _annotate_candidate_covers(c, _HASH_A, "tok", {})
        # Bundle gets the skip signal without a fetch — bundle URL
        # verification owns these.
        assert c[0]["cover_signal"] == "skipped_bundle"
        assert calls == {}

    @pytest.mark.asyncio
    async def test_topn_caps_fetches(self, monkeypatch):
        # 8 non-bundle candidates with distinct text confidence;
        # only top-5 should get fetched. Confidence ranks higher = fetched.
        candidates = [
            _candidate(str(i), confidence=0.1 * i)
            for i in range(8)
        ]
        calls = _patch_fetcher(monkeypatch, {str(i): _HASH_A for i in range(8)})
        await _annotate_candidate_covers(candidates, _HASH_A, "tok", {}, topn=5)
        # Top 5 by confidence are tids "7","6","5","4","3" (0.7-0.3).
        assert set(calls.keys()) == {"7", "6", "5", "4", "3"}
        # Below-topn candidates flagged "not_evaluated" so callers can't
        # mistake the absence of a signal for a verified-different cover.
        for c in candidates:
            if c["torrent_id"] in calls:
                assert c["cover_signal"] == "promote"
            else:
                assert c["cover_signal"] == "not_evaluated"
                assert c["cover_distance"] is None

    @pytest.mark.asyncio
    async def test_topn_excludes_bundles_from_pool(self, monkeypatch):
        # 3 high-conf bundles + 2 lower-conf non-bundles.
        # topn=5 — bundles are EXCLUDED from the pool, so all 2 non-bundles
        # get fetched even though they're outside the top-3 by confidence.
        candidates = [
            _candidate("b1", confidence=0.9, is_bundle=True),
            _candidate("b2", confidence=0.85, is_bundle=True),
            _candidate("b3", confidence=0.8, is_bundle=True),
            _candidate("n1", confidence=0.4),
            _candidate("n2", confidence=0.3),
        ]
        calls = _patch_fetcher(
            monkeypatch, {"n1": _HASH_A, "n2": _HASH_A}
        )
        await _annotate_candidate_covers(
            candidates, _HASH_A, "tok", {}, topn=5,
        )
        assert set(calls.keys()) == {"n1", "n2"}
        # All 3 bundles were skipped; both non-bundles were promoted.
        signals = {c["torrent_id"]: c["cover_signal"] for c in candidates}
        assert signals == {
            "b1": "skipped_bundle",
            "b2": "skipped_bundle",
            "b3": "skipped_bundle",
            "n1": "promote",
            "n2": "promote",
        }

    @pytest.mark.asyncio
    async def test_in_memory_cache_dedups_repeat_torrent(self, monkeypatch):
        # Same torrent appears twice in the candidate list (rare but
        # possible if MAM ranks the same torrent under multiple
        # categories — pre-existing dedup happens later). The cache
        # ensures we only fetch once.
        c = [_candidate("99"), _candidate("99")]
        calls = _patch_fetcher(monkeypatch, {"99": _HASH_A})
        cache: dict = {}
        await _annotate_candidate_covers(c, _HASH_A, "tok", cache)
        assert calls.get("99") == 1
        assert "99" in cache

    @pytest.mark.asyncio
    async def test_cache_seeded_from_caller_skips_fetch(self, monkeypatch):
        # Caller pre-populates the cache (e.g. earlier cascade pass
        # already saw this torrent). Helper must respect it — no fetch.
        calls = _patch_fetcher(monkeypatch, {"42": _HASH_FULL_DIFF})
        cache = {"42": _HASH_A}  # pre-seeded with the "right" hash
        c = [_candidate("42")]
        await _annotate_candidate_covers(c, _HASH_A, "tok", cache)
        assert calls.get("42") is None  # never called
        # Used the cached value, which == seshat hash → promote.
        assert c[0]["cover_signal"] == "promote"

    @pytest.mark.asyncio
    async def test_signal_initialization_on_unsuccessful_fetches(
        self, monkeypatch
    ):
        # All top-N fetches fail. Every candidate should still have
        # the cover_signal/cover_distance keys set (initialized to
        # not_evaluated/no_data/skipped_bundle) so downstream code can
        # rely on the keys existing.
        candidates = [
            _candidate("a"),
            _candidate("b", is_bundle=True),
        ]
        _patch_fetcher(monkeypatch, {"a": None})
        await _annotate_candidate_covers(candidates, _HASH_A, "tok", {})
        for c in candidates:
            assert "cover_signal" in c
            assert "cover_distance" in c
            assert "mam_cover_phash" in c


# ─── Production gate: flag-off path is dead code ────────────────


class TestProductionFlags:
    """Pin the production-enabled flag values + thresholds.

    `_COVER_VERIFICATION_ENABLED` and `_COVER_DEMOTION_ENABLED` were
    flipped True in v2.4.0 (2026-05-09) after the A3 UAT validated
    promoter-anchored demotion as safe under the observed Cohort C
    distribution (n=3, range 28-34 — overlaps wrong band). If you
    change these defaults, re-read project_seshat_mam_url_confidence
    memory and the threshold rationale below.
    """

    def test_promotion_enabled(self):
        assert mam_mod._COVER_VERIFICATION_ENABLED is True

    def test_demotion_enabled(self):
        # Demotion is gated SEPARATELY but currently True; gating
        # remains separate so future ops can pause demotion alone if
        # ever needed (without disabling the promote signal).
        assert mam_mod._COVER_DEMOTION_ENABLED is True

    def test_constants_match_validated_thresholds(self):
        # Promote: max(right_band)=6 from A3 UAT, 10 leaves 4-bit margin.
        # Demote: min(wrong_band)=22 from A3 UAT — but only fires when
        # promoter-anchored, so Cohort C at distance 28-34 is safe.
        # TopN: bumped 5→10 after Veil/Raw cases showed text-conf ties
        # could push the right candidate outside top-5.
        assert mam_mod._COVER_PROMOTE_DIST_MAX == 10
        assert mam_mod._COVER_DEMOTE_DIST_MIN == 22
        assert mam_mod._COVER_TOPN_CANDIDATES == 10
