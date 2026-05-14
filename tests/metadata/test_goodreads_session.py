"""
Tests for `app.metadata.goodreads_session` — the centralized Goodreads
HTTP plumbing for Cloudflare bypass + soft-block tracking.

Scope:
  - Soft-block detection (HTTP 202 vs empty 2xx vs real 200)
  - Runtime-state flag transitions (unknown → active → soft_blocked → active)
  - `is_soft_blocked()` dispatcher helper accuracy
  - `GoodreadsSession.get()` end-to-end with a stubbed HTTP layer:
      - 202 → state flips to soft_blocked, last_status recorded
      - 200 with body → state flips to active
      - 404 / 503 → state unchanged (real responses, not fingerprint blocks)
  - Phase B injection hook returns None today (regression guard for the
    "Phase A doesn't accidentally ship cookie injection" invariant)
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def gr_session_module(monkeypatch, tmp_path):
    """Reload goodreads_session with a clean DATA_DIR + reset singleton."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.config
    importlib.reload(app.config)
    import app.metadata.goodreads_session as gs
    importlib.reload(gs)
    gs.reset_session_for_tests()
    return gs


def _make_resp(status: int, body: bytes = b"") -> SimpleNamespace:
    """Minimal duck-typed response — only `.status_code` + `.content` matter."""
    return SimpleNamespace(status_code=status, content=body, text=body.decode("utf-8", "ignore"))


class TestSoftBlockDetection:
    """`is_cloudflare_soft_block` must classify responses correctly."""

    def test_202_is_soft_block(self, gr_session_module):
        assert gr_session_module.is_cloudflare_soft_block(_make_resp(202))

    def test_200_with_empty_body_is_soft_block(self, gr_session_module):
        assert gr_session_module.is_cloudflare_soft_block(_make_resp(200, b""))

    def test_200_with_body_is_not_soft_block(self, gr_session_module):
        assert not gr_session_module.is_cloudflare_soft_block(
            _make_resp(200, b"<html>real page</html>")
        )

    def test_404_is_not_soft_block(self, gr_session_module):
        # Real 404 from Goodreads (book deleted, etc.) — not a fingerprint
        # problem. Must not trip the soft-block flag.
        assert not gr_session_module.is_cloudflare_soft_block(_make_resp(404))

    def test_503_is_not_soft_block(self, gr_session_module):
        # Transient server issue — not a Cloudflare gate. Treat as
        # retryable failure, not a session-state problem.
        assert not gr_session_module.is_cloudflare_soft_block(_make_resp(503))

    def test_none_is_not_soft_block(self, gr_session_module):
        # Defensive: caller may pass None on transport exceptions.
        assert not gr_session_module.is_cloudflare_soft_block(None)


class TestRuntimeStateFlag:
    """The three flat keys must persist across load/save round-trips."""

    def test_default_state_is_unknown(self, gr_session_module):
        state = gr_session_module.get_session_state()
        assert state["state"] == "unknown"
        assert state["since"] is None
        assert state["last_status"] is None

    def test_mark_soft_blocked_persists(self, gr_session_module):
        gr_session_module.mark_soft_blocked(last_status=202)
        state = gr_session_module.get_session_state()
        assert state["state"] == "soft_blocked"
        assert state["since"] is not None
        assert state["last_status"] == 202

    def test_mark_active_persists(self, gr_session_module):
        gr_session_module.mark_active(last_status=200)
        state = gr_session_module.get_session_state()
        assert state["state"] == "active"
        assert state["last_status"] == 200

    def test_is_soft_blocked_helper_tracks_state(self, gr_session_module):
        assert not gr_session_module.is_soft_blocked()
        gr_session_module.mark_soft_blocked(last_status=202)
        assert gr_session_module.is_soft_blocked()
        gr_session_module.mark_active(last_status=200)
        assert not gr_session_module.is_soft_blocked()

    def test_since_timestamp_updates_only_on_state_flip(self, gr_session_module):
        """Repeated mark_soft_blocked() must not bump `since` — once
        we know we're soft-blocked, the "since" timestamp records the
        moment of first detection, not the last probe."""
        gr_session_module.mark_soft_blocked(last_status=202)
        first_since = gr_session_module.get_session_state()["since"]
        # Same state again — since should NOT change
        gr_session_module.mark_soft_blocked(last_status=202)
        second_since = gr_session_module.get_session_state()["since"]
        assert first_since == second_since


class TestSessionGetIntegration:
    """`GoodreadsSession.get()` updates the runtime-state flag correctly."""

    @pytest.mark.asyncio
    async def test_200_response_marks_active(self, gr_session_module, monkeypatch):
        session = gr_session_module.GoodreadsSession(rate_limit=0)
        # Force the httpx fallback path (no curl_cffi).
        monkeypatch.setattr(session, "_get_curl", lambda: None)

        class FakeClient:
            async def get(self, url, **kwargs):
                return _make_resp(200, b"<html>book detail</html>")

        monkeypatch.setattr(session, "_get_httpx", lambda: FakeClient())

        resp = await session.get("https://www.goodreads.com/book/show/3")
        assert resp.status_code == 200
        assert gr_session_module.get_session_state()["state"] == "active"

    @pytest.mark.asyncio
    async def test_202_response_marks_soft_blocked(self, gr_session_module, monkeypatch):
        session = gr_session_module.GoodreadsSession(rate_limit=0)
        monkeypatch.setattr(session, "_get_curl", lambda: None)

        class FakeClient:
            async def get(self, url, **kwargs):
                return _make_resp(202, b"")

        monkeypatch.setattr(session, "_get_httpx", lambda: FakeClient())

        resp = await session.get("https://www.goodreads.com/book/show/3")
        assert resp.status_code == 202
        assert gr_session_module.get_session_state()["state"] == "soft_blocked"
        assert gr_session_module.get_session_state()["last_status"] == 202

    @pytest.mark.asyncio
    async def test_404_response_leaves_state_unchanged(self, gr_session_module, monkeypatch):
        # Pre-set to active so we can detect a regression that flips
        # state on legitimate 404s.
        gr_session_module.mark_active(last_status=200)

        session = gr_session_module.GoodreadsSession(rate_limit=0)
        monkeypatch.setattr(session, "_get_curl", lambda: None)

        class FakeClient:
            async def get(self, url, **kwargs):
                return _make_resp(404, b"<html>not found</html>")

        monkeypatch.setattr(session, "_get_httpx", lambda: FakeClient())

        await session.get("https://www.goodreads.com/book/show/99999999")
        state = gr_session_module.get_session_state()
        assert state["state"] == "active"  # unchanged
        assert state["last_status"] == 404  # but last_status updated


class TestPhaseBHook:
    """Regression guard: cookie injection must stay OFF in Phase A.

    Flipping `_build_cookie_header()` on without going through a tagged
    Phase B release would leak cookie state into Phase A scans before
    the encrypted-store secrets path is wired. This test fails loudly
    if someone enables injection prematurely.
    """

    def test_cookie_header_is_none_in_phase_a(self, gr_session_module):
        assert gr_session_module._build_cookie_header() is None
