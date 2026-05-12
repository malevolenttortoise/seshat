"""
Integration tests for the v2.9.0 format-priority dedup gate as wired
into the dispatcher.

The pure decision logic lives in `tests/orchestrator/test_format_dedup.py`.
These tests exercise the live `handle_announce` / `inject_grab` paths
with the new dedup gate plumbed in — verifying that:

  - announce row + decision text reflect the dedup outcome
  - a held grab leaves a `pending_holds` row and NO grab row
  - a preempted hold gets marked dropped synchronously
  - the manual-inject override bypasses dedup cleanly
  - `format_priority={}` short-circuits and old behavior is preserved
  - the `hold_release` tick releases due holds correctly

The Delves / Duchy real-world incident is used as the canary fixture
to keep the wired behavior tied to actual production data.
"""
from __future__ import annotations

from typing import Optional

import pytest

from app import state
from app.clients.base import AddResult, TorrentClient, TorrentInfo
from app.database import get_db
from app.filter.gate import Announce, FilterConfig
from app.filter.normalize import normalize_author, normalize_category
from app.mam.grab import GrabResult
from app.orchestrator.dispatch import (
    DispatcherDeps,
    handle_announce,
    inject_grab,
)
from app.orchestrator.hold_release import tick as hold_release_tick
from app.storage import grabs as grabs_storage
from app.storage import holds as holds_storage
from tests.fake_mam import MINIMAL_BENCODED_TORRENT


# ─── Helpers ─────────────────────────────────────────────────


EBOOK_PRIORITY = [
    {"fmt": "epub", "enabled": True},
    {"fmt": "azw3", "enabled": False},
    {"fmt": "mobi", "enabled": False},
    {"fmt": "pdf",  "enabled": False},
]
AUDIOBOOK_PRIORITY = [
    {"fmt": "m4b", "enabled": True},
    {"fmt": "mp3", "enabled": False},
]
DEFAULT_PRIORITY = {"ebook": EBOOK_PRIORITY, "audiobook": AUDIOBOOK_PRIORITY}


class _FakeQbit:
    def __init__(self, *, add_result: Optional[AddResult] = None):
        self.add_result = add_result or AddResult(success=True)
        self.add_calls: list[dict] = []

    async def login(self) -> bool:
        return True

    async def add_torrent(
        self, torrent_bytes: bytes,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> AddResult:
        self.add_calls.append({"size": len(torrent_bytes)})
        return self.add_result

    async def list_torrents(
        self, category: Optional[str] = None,
    ) -> list[TorrentInfo]:
        return []

    async def get_torrent(self, torrent_hash: str) -> Optional[TorrentInfo]:
        return None

    async def aclose(self) -> None:
        return None


def _make_fetch(result: GrabResult):
    async def fake_fetch(torrent_id, token, **kwargs):
        return result
    return fake_fetch


def _make_filter_config(allowed: list[str] = None) -> FilterConfig:
    return FilterConfig(
        allowed_categories=frozenset(
            normalize_category(c) for c in [
                "Ebooks - Fantasy", "Audiobooks - Fantasy",
            ]
        ),
        allowed_authors=frozenset(
            normalize_author(a) for a in (allowed or ["Keleros"])
        ),
    )


def _make_deps(
    *,
    format_priority: dict = None,
    format_dedup_hold_seconds: int = 600,
    fetch_result: Optional[GrabResult] = None,
    qbit: Optional[TorrentClient] = None,
) -> DispatcherDeps:
    return DispatcherDeps(
        filter_config=_make_filter_config(),
        mam_token="good_token",
        qbit_category="mam-complete",
        budget_cap=200,
        queue_max=100,
        queue_mode_enabled=True,
        seed_seconds_required=72 * 3600,
        db_factory=get_db,
        fetch_torrent=_make_fetch(
            fetch_result or GrabResult(
                success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT,
            )
        ),
        qbit=qbit or _FakeQbit(),
        format_priority=(
            DEFAULT_PRIORITY if format_priority is None else format_priority
        ),
        format_dedup_hold_seconds=format_dedup_hold_seconds,
    )


def _delves(
    *, filetype: str, torrent_id: str = "1240987",
) -> Announce:
    return Announce(
        torrent_id=torrent_id,
        torrent_name="The Delves",
        category="Ebooks - Fantasy",
        author_blob="Keleros",
        title="The Delves",
        filetype=filetype,
    )


def _duchy(
    *, filetype: str, torrent_id: str = "1240992",
) -> Announce:
    return Announce(
        torrent_id=torrent_id,
        torrent_name="The Duchy",
        category="Ebooks - Fantasy",
        author_blob="Keleros",
        title="The Duchy",
        filetype=filetype,
    )


async def _fetch_announce_row(announce_id: int) -> dict:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT decision, decision_reason, filetype "
            "FROM announces WHERE id = ?", (announce_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else {}
    finally:
        await db.close()


async def _fetch_grabs() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, mam_torrent_id, book_format, dedup_key, state "
            "FROM grabs ORDER BY id"
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _fetch_holds() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, dedup_key, book_format, state, resolution_reason "
            "FROM pending_holds ORDER BY id"
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


@pytest.fixture
async def isolated_libs(monkeypatch, temp_db):
    """Empty `_discovered_libraries` so the owned-side lookup short-
    circuits without trying to open per-library DBs."""
    monkeypatch.setattr(state, "_discovered_libraries", [])
    yield


# ═══════════════════════════════════════════════════════════════
# handle_announce — wired dedup flow
# ═══════════════════════════════════════════════════════════════


class TestEnabledFormatGrabs:
    async def test_lone_epub_grabs_with_dedup_metadata(self, isolated_libs):
        deps = _make_deps()
        result = await handle_announce(deps, _delves(filetype="epub"))

        assert result.action == "submit"
        assert result.grab_id is not None

        grabs = await _fetch_grabs()
        assert len(grabs) == 1
        assert grabs[0]["book_format"] == "epub"
        assert grabs[0]["dedup_key"]  # non-empty
        assert "delves" in grabs[0]["dedup_key"]
        assert "keleros" in grabs[0]["dedup_key"]

        ann = await _fetch_announce_row(result.announce_id)
        assert ann["decision"] == "allow"
        assert ann["filetype"] == "epub"


class TestDisabledFormatHolds:
    async def test_lone_azw3_creates_hold_no_grab(self, isolated_libs):
        deps = _make_deps()
        result = await handle_announce(deps, _delves(filetype="azw3"))

        # Skip outcome from the dispatcher's perspective — no grab.
        assert result.action == "skip"
        assert result.reason == "format_dedup_hold"
        assert result.grab_id is None

        # No grab row was created.
        grabs = await _fetch_grabs()
        assert grabs == []

        # A pending hold IS created with the right metadata.
        holds = await _fetch_holds()
        assert len(holds) == 1
        assert holds[0]["book_format"] == "azw3"
        assert holds[0]["state"] == "pending"

        # The announce row was UPDATED to reflect the dedup decision.
        ann = await _fetch_announce_row(result.announce_id)
        assert ann["decision"] == "hold"
        assert ann["decision_reason"] == "format_dedup_hold"


class TestDuchyCaseInflightSkip:
    """The real-world incident: EPUB lands first, AZW3 lands 29s later
    and finds EPUB in-flight at higher priority. AZW3 must be skipped
    without becoming a hold."""

    async def test_azw3_skipped_when_epub_inflight(self, isolated_libs):
        deps = _make_deps()
        # First announce: EPUB grabs.
        first = await handle_announce(deps, _duchy(filetype="epub"))
        assert first.action == "submit"
        assert first.grab_id is not None

        # Second announce: AZW3 sees the in-flight EPUB.
        second = await handle_announce(
            deps, _duchy(filetype="azw3", torrent_id="1240993"),
        )
        assert second.action == "skip"
        assert second.reason == "format_dedup_higher_priority_inflight"
        assert second.grab_id is None

        # Only one grab row total; no holds.
        grabs = await _fetch_grabs()
        assert len(grabs) == 1
        assert grabs[0]["book_format"] == "epub"
        assert await _fetch_holds() == []


class TestDelvesPreemptCase:
    """AZW3 arrives first (held), EPUB arrives later — EPUB grabs and
    drops the AZW3 hold synchronously."""

    async def test_epub_preempts_held_azw3(self, isolated_libs):
        deps = _make_deps()
        # First: AZW3 lands in the hold queue.
        first = await handle_announce(deps, _delves(filetype="azw3"))
        assert first.action == "skip"
        assert first.reason == "format_dedup_hold"
        holds = await _fetch_holds()
        assert len(holds) == 1
        assert holds[0]["state"] == "pending"
        held_id = holds[0]["id"]

        # Second: EPUB lands — grabs and preempts.
        second = await handle_announce(
            deps, _delves(filetype="epub", torrent_id="1240990"),
        )
        assert second.action == "submit"
        assert second.grab_id is not None

        # The AZW3 hold is now dropped.
        holds = await _fetch_holds()
        assert len(holds) == 1
        assert holds[0]["id"] == held_id
        assert holds[0]["state"] == "dropped"
        assert "preempted_by_format_dedup_enabled_grab" in holds[0]["resolution_reason"]


class TestEmptyFormatPriorityBypassesGate:
    """When the user hasn't configured format_priority (empty dict),
    the dedup gate short-circuits entirely — preserves pre-v2.9.0
    behavior. Two formats of the same book both get grabbed."""

    async def test_both_formats_grab_when_priority_empty(self, isolated_libs):
        deps = _make_deps(format_priority={})
        await handle_announce(deps, _delves(filetype="azw3"))
        await handle_announce(deps, _delves(filetype="epub", torrent_id="1240988"))

        grabs = await _fetch_grabs()
        assert len(grabs) == 2
        assert await _fetch_holds() == []


class TestUnknownMediaTypeFallsThrough:
    """Comics-category announces don't have a priority list in v2.9.0
    defaults — the gate falls through to allow."""

    async def test_comic_announce_grabs(self, isolated_libs):
        deps = _make_deps()
        ann = Announce(
            torrent_id="9999",
            torrent_name="Some Manga Vol 1",
            category="Comics/Graphic novels - Manga",
            author_blob="Keleros",
            filetype="cbz",
        )
        # Need to allow that category in the filter for the test.
        deps.filter_config = FilterConfig(
            allowed_categories=frozenset(
                normalize_category(c) for c in [
                    "Ebooks - Fantasy", "Comics/Graphic novels - Manga",
                ]
            ),
            allowed_authors=frozenset([normalize_author("Keleros")]),
        )
        result = await handle_announce(deps, ann)
        assert result.action == "submit"


# ═══════════════════════════════════════════════════════════════
# inject_grab — manual override
# ═══════════════════════════════════════════════════════════════


class TestInjectGrabDedup:
    async def test_inject_default_applies_dedup(self, isolated_libs):
        deps = _make_deps()
        # AZW3 manual inject with default apply_format_dedup=True →
        # should hold just like an IRC announce would.
        result = await inject_grab(
            deps, torrent_id="1240987",
            torrent_name="The Delves",
            category="Ebooks - Fantasy",
            author_blob="Keleros",
            filetype="azw3",
        )
        assert result.action == "skip"
        assert result.reason == "format_dedup_hold"
        assert await _fetch_grabs() == []
        assert len(await _fetch_holds()) == 1

    async def test_inject_override_bypasses_dedup(self, isolated_libs):
        deps = _make_deps()
        result = await inject_grab(
            deps, torrent_id="1240987",
            torrent_name="The Delves",
            category="Ebooks - Fantasy",
            author_blob="Keleros",
            filetype="azw3",
            apply_format_dedup=False,
        )
        # Override → grab proceeds even though AZW3 is disabled.
        assert result.action == "submit"
        assert result.grab_id is not None
        # No hold created.
        assert await _fetch_holds() == []
        grabs = await _fetch_grabs()
        assert len(grabs) == 1
        assert grabs[0]["book_format"] == "azw3"


# ═══════════════════════════════════════════════════════════════
# hold_release scheduler tick
# ═══════════════════════════════════════════════════════════════


class TestHoldRelease:
    async def test_tick_with_no_holds_is_noop(self, isolated_libs):
        deps = _make_deps()
        assert await hold_release_tick(deps) == 0

    async def test_due_hold_with_no_siblings_releases(
        self, isolated_libs,
    ):
        deps = _make_deps()
        # Plant a hold directly with a release_at in the past.
        db = await get_db()
        try:
            from app.storage.holds import create_hold
            from app.orchestrator.format_dedup import normalize_dedup_key
            key = normalize_dedup_key("The Delves", "Keleros")
            hold_id = await create_hold(
                db,
                announce_id=None,
                dedup_key=key,
                media_type="ebook",
                book_format="azw3",
                torrent_id="1240987",
                torrent_name="The Delves",
                category="Ebooks - Fantasy",
                author_blob="Keleros",
                hold_seconds=-60,  # already-fired
            )
        finally:
            await db.close()

        # Tick should release the hold by injecting a grab.
        resolved = await hold_release_tick(deps)
        assert resolved == 1

        holds = await _fetch_holds()
        assert len(holds) == 1
        assert holds[0]["id"] == hold_id
        assert holds[0]["state"] == "released"

        grabs = await _fetch_grabs()
        assert len(grabs) == 1
        assert grabs[0]["book_format"] == "azw3"
        assert grabs[0]["dedup_key"]

    async def test_due_hold_blocked_by_new_inflight_sibling_dropped(
        self, isolated_libs,
    ):
        """Slow uploader uploads AZW3 (held). 10 min later, an EPUB
        for the same book arrives and grabs. The AZW3 hold's timer
        fires after that — and the release tick must see the EPUB
        in-flight and drop the AZW3 hold instead of grabbing it."""
        deps = _make_deps()

        # Plant a held AZW3 with expired release_at.
        from app.orchestrator.format_dedup import normalize_dedup_key
        from app.storage.holds import create_hold
        key = normalize_dedup_key("The Delves", "Keleros")
        db = await get_db()
        try:
            hold_id = await create_hold(
                db,
                announce_id=None, dedup_key=key, media_type="ebook",
                book_format="azw3", torrent_id="1240987",
                torrent_name="The Delves", category="Ebooks - Fantasy",
                author_blob="Keleros",
                hold_seconds=-60,
            )
            # Also plant an in-flight EPUB grab with the same dedup_key.
            await grabs_storage.create_grab(
                db,
                announce_id=None, mam_torrent_id="1240990",
                torrent_name="The Delves", category="Ebooks - Fantasy",
                author_blob="Keleros",
                state=grabs_storage.STATE_FETCHED,
                book_format="epub", dedup_key=key,
            )
        finally:
            await db.close()

        resolved = await hold_release_tick(deps)
        assert resolved == 1

        holds = await _fetch_holds()
        assert holds[0]["id"] == hold_id
        assert holds[0]["state"] == "dropped"
        assert "blocked_by_sibling" in holds[0]["resolution_reason"]

        # No new grab was created — only the planted EPUB exists.
        grabs = await _fetch_grabs()
        assert len(grabs) == 1
        assert grabs[0]["book_format"] == "epub"

    async def test_not_yet_due_holds_left_alone(self, isolated_libs):
        deps = _make_deps()
        from app.orchestrator.format_dedup import normalize_dedup_key
        from app.storage.holds import create_hold
        key = normalize_dedup_key("The Delves", "Keleros")
        db = await get_db()
        try:
            await create_hold(
                db,
                announce_id=None, dedup_key=key, media_type="ebook",
                book_format="azw3", torrent_id="1240987",
                torrent_name="The Delves", category="Ebooks - Fantasy",
                author_blob="Keleros",
                hold_seconds=3600,  # 1 hour from now — not yet due
            )
        finally:
            await db.close()

        resolved = await hold_release_tick(deps)
        assert resolved == 0
        holds = await _fetch_holds()
        assert holds[0]["state"] == "pending"
