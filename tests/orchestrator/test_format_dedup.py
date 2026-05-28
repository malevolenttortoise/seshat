"""
Unit + integration tests for the v2.9.0 format-priority dedup gate.

The four user-facing scenarios from the design plan each get a dedicated
test. Plus edge cases (unknown media type, unknown filetype, empty key)
and the two preempt cases (Delves-style enabled-preempts-held and
disabled-replaces-lower-priority-hold). The lookup integration tests
exercise real grabs/pending_holds/per-library books DBs to catch any
SQL drift.

Real-world fixtures: the Keleros "The Delves" / "The Duchy" announces
that triggered the v2.9.0 release are baked into the test data, so the
gate's behavior on Mark's actual incident data is regression-pinned.
"""
from __future__ import annotations

import pytest

from app import state
from app.discovery import database as disco_db
from app.filter.gate import Announce
from app.orchestrator.format_dedup import (
    FormatDedupDecision,
    SiblingMatch,
    evaluate_format_dedup,
    lookup_dedup_siblings,
    media_type_from_category,
    normalize_dedup_key,
)


# ─── Test fixtures: priority lists matching the v2.9.0 defaults ──

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
HOLD_SECONDS = 600


def _announce(
    *,
    title: str = "The Delves",
    author: str = "Keleros",
    category: str = "Ebooks - Fantasy",
    filetype: str = "epub",
    torrent_id: str = "1240987",
) -> Announce:
    """Build a realistic IRC-shaped Announce for the tests."""
    return Announce(
        torrent_id=torrent_id,
        torrent_name=title,
        category=category,
        author_blob=author,
        title=title,
        filetype=filetype,
    )


# ═══════════════════════════════════════════════════════════════
# Pure-helper unit tests
# ═══════════════════════════════════════════════════════════════


class TestMediaTypeFromCategory:
    def test_ebook_prefix(self):
        assert media_type_from_category("Ebooks - Fantasy") == "ebook"

    def test_audiobook_prefix(self):
        assert media_type_from_category("Audiobooks - Sci-Fi") == "audiobook"

    def test_unknown_prefix(self):
        # Comics has no priority rules in v2.9.0 — falls through to allow.
        assert media_type_from_category("Comics/Graphic novels - Manga") is None

    def test_empty_category(self):
        assert media_type_from_category("") is None

    def test_no_separator(self):
        # Bare "Ebooks" with no " - " suffix still matches the prefix.
        assert media_type_from_category("Ebooks") == "ebook"

    def test_case_insensitive(self):
        assert media_type_from_category("EBOOKS - Fantasy") == "ebook"


class TestNormalizeDedupKey:
    def test_delves_canonical(self):
        # Mark's real announce — what we expect from the live DB.
        assert normalize_dedup_key("The Delves", "Keleros") \
            == normalize_dedup_key("The Delves", "Keleros")
        # Sanity: same author + title via different paths collapses.
        assert normalize_dedup_key("The Delves", "Keleros") == \
               normalize_dedup_key("The Delves (Unabridged)", "Keleros")

    def test_both_keleros_books_yield_different_keys(self):
        # Title is the distinguishing field — Delves and Duchy must
        # NOT collide (different books by the same author).
        delves = normalize_dedup_key("The Delves", "Keleros")
        duchy = normalize_dedup_key("The Duchy", "Keleros")
        assert delves and duchy and delves != duchy

    def test_strips_leading_article(self):
        assert normalize_dedup_key("The Delves", "Keleros") == \
               normalize_dedup_key("Delves", "Keleros")

    def test_first_author_only(self):
        # MAM author_blob is comma-separated; we key on the first.
        assert normalize_dedup_key("Foo", "First Author, Second Author") \
            == normalize_dedup_key("Foo", "First Author")

    def test_empty_inputs_yield_empty_key(self):
        assert normalize_dedup_key("", "Keleros") == ""
        assert normalize_dedup_key("The Delves", "") == ""
        assert normalize_dedup_key("", "") == ""

    def test_format_paren_stripped(self):
        # The works/normalize matcher already handles this — we just
        # pin that the dedup key inherits the behavior.
        assert normalize_dedup_key("Foo (Unabridged)", "Author X") == \
               normalize_dedup_key("Foo", "Author X")
        assert normalize_dedup_key("Foo [Audiobook]", "Author X") == \
               normalize_dedup_key("Foo", "Author X")


# Note: the format-rank helper that powered the old `_format_index`
# inline function now lives in app/quality/scoring.py; its coverage is
# in tests/quality/test_scoring.py::test_format_rank_*.


# ═══════════════════════════════════════════════════════════════
# Gate decision tests — the four v2.9.0 scenarios + edge cases
# ═══════════════════════════════════════════════════════════════


class TestScenario1_EnabledNoSiblings:
    """Scenario 1: EPUB arrives, no other version torrent races. Grab."""

    def test_epub_grab(self):
        ann = _announce(filetype="epub")
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[],
        )
        assert d.action == "allow"
        assert d.reason == "format_dedup_enabled_grab"
        assert d.preempt_hold_ids == ()
        assert d.media_type == "ebook"
        assert d.book_format == "epub"

    def test_m4b_grab(self):
        ann = _announce(
            title="A Tangle of Time", author="Sarah Lin",
            category="Audiobooks - Fantasy", filetype="m4b",
        )
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[],
        )
        assert d.action == "allow"
        assert d.media_type == "audiobook"


class TestScenario2_DuchyCase:
    """Scenario 2: EPUB first (in-flight), AZW3 arrives 29s later. Skip."""

    def test_azw3_blocked_by_inflight_epub(self):
        key = normalize_dedup_key("The Duchy", "Keleros")
        epub_inflight = SiblingMatch(
            where="in_flight", book_format="epub", grab_id=2945,
        )
        ann = _announce(title="The Duchy", filetype="azw3", torrent_id="1240993")
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[epub_inflight],
        )
        assert d.action == "skip"
        assert d.reason == "format_dedup_higher_priority_inflight"
        assert d.dedup_key == key

    def test_azw3_blocked_by_held_epub(self):
        # Variant: EPUB is in the hold queue (not yet released).
        # AZW3 arriving sees a higher-priority hold — also skip.
        epub_held = SiblingMatch(
            where="held", book_format="epub", hold_id=42,
        )
        ann = _announce(title="The Duchy", filetype="azw3")
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[epub_held],
        )
        assert d.action == "skip"
        assert d.reason == "format_dedup_higher_priority_inflight"


class TestScenario3_DisabledNoSibling:
    """Scenario 3: only AZW3 arrives. Hold for 10 min."""

    def test_lone_azw3_held(self):
        ann = _announce(filetype="azw3")
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[],
        )
        assert d.action == "hold"
        assert d.reason == "format_dedup_hold"
        assert d.hold_seconds == HOLD_SECONDS
        assert d.preempt_hold_ids == ()
        assert d.book_format == "azw3"

    def test_lone_mp3_held(self):
        ann = _announce(
            title="A Tangle of Time", author="Sarah Lin",
            category="Audiobooks - Fantasy", filetype="mp3",
        )
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[],
        )
        assert d.action == "hold"
        assert d.book_format == "mp3"


class TestScenario3_5_EnabledLaterOverridesOwned:
    """Scenario 3.5: AZW3 was already grabbed/Owned (lone Scenario 3
    happy-path long ago), EPUB arrives later. EPUB grabs — Calibre
    stores both formats per book row."""

    def test_epub_grabs_over_owned_azw3(self):
        # Owned sibling at lower priority — does NOT block enabled grab.
        owned_azw3 = SiblingMatch(
            where="owned", book_format="azw3", library_slug="calibre-main",
        )
        ann = _announce(filetype="epub")
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[owned_azw3],
        )
        assert d.action == "allow"
        assert d.reason == "format_dedup_enabled_grab"


class TestDelvesPreempt:
    """The headline v2.9.0 case: AZW3 arrives first, held for 10 min.
    EPUB arrives 57s later — enabled, drops the AZW3 hold, grabs."""

    def test_epub_preempts_held_azw3(self):
        azw3_held = SiblingMatch(
            where="held", book_format="azw3", hold_id=99,
        )
        ann = _announce(filetype="epub")
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[azw3_held],
        )
        assert d.action == "allow"
        assert d.reason == "format_dedup_enabled_grab"
        # The held AZW3 must be marked dropped.
        assert d.preempt_hold_ids == (99,)

    def test_enabled_does_not_preempt_higher_priority_held(self):
        # Defensive: if for some reason a higher-priority sibling is held
        # (e.g., user added a new higher format mid-window), the new
        # enabled arrival shouldn't drop it — we just grab alongside.
        # In practice priority order means this is unusual, but the
        # gate should still behave correctly.
        ann = _announce(filetype="azw3")  # azw3 disabled but conceptually
        # Force azw3 as enabled for this test by tweaking priority.
        priority = {
            "ebook": [
                {"fmt": "epub", "enabled": False},  # higher prio, held
                {"fmt": "azw3", "enabled": True},   # incoming, enabled
            ],
            "audiobook": AUDIOBOOK_PRIORITY,
        }
        epub_held = SiblingMatch(
            where="held", book_format="epub", hold_id=42,
        )
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=priority,
            hold_seconds=HOLD_SECONDS,
            siblings=[epub_held],
        )
        # AZW3 is enabled, grabs. EPUB hold is HIGHER priority — not
        # preempted by a LOWER-priority enabled arrival.
        assert d.action == "allow"
        assert d.preempt_hold_ids == ()


class TestDisabledOwnedBlock:
    """Any owned sibling blocks a disabled arrival, regardless of which
    format is owned."""

    def test_owned_same_format_blocks(self):
        owned = SiblingMatch(where="owned", book_format="azw3")
        ann = _announce(filetype="azw3")
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[owned],
        )
        assert d.action == "skip"
        assert d.reason == "format_dedup_owned_sibling"

    def test_owned_higher_priority_blocks_disabled(self):
        owned = SiblingMatch(where="owned", book_format="epub")
        ann = _announce(filetype="azw3")
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[owned],
        )
        assert d.action == "skip"
        assert d.reason == "format_dedup_owned_sibling"


class TestDisabledLowerPriorityHoldReplacement:
    """A higher-priority disabled arrival replaces an existing lower-
    priority hold — invariant: at most one active hold per dedup_key."""

    def test_azw3_replaces_pdf_hold(self):
        pdf_held = SiblingMatch(
            where="held", book_format="pdf", hold_id=77,
        )
        ann = _announce(filetype="azw3")
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[pdf_held],
        )
        # AZW3 is higher priority than PDF (index 1 < index 3) — both
        # disabled — replace.
        assert d.action == "hold"
        assert d.preempt_hold_ids == (77,)


class TestFallThroughCases:
    """When the gate can't apply rules (unknown media type / format /
    missing data), it returns allow so the existing flow proceeds
    unchanged."""

    def test_unknown_media_type_allows(self):
        ann = _announce(
            title="X", author="Y",
            category="Comics/Graphic novels - Manga", filetype="cbz",
        )
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[],
        )
        assert d.action == "allow"
        assert d.reason == "format_dedup_no_media_type_rule"
        assert d.media_type is None

    def test_unknown_filetype_allows(self):
        ann = _announce(filetype="fb2")
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[],
        )
        assert d.action == "allow"
        assert d.reason == "format_dedup_unknown_fmt"

    def test_empty_filetype_allows(self):
        ann = _announce(filetype="")
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[],
        )
        assert d.action == "allow"
        assert d.reason == "format_dedup_no_filetype"

    def test_empty_title_allows(self):
        ann = _announce(title="", author="Some Author", filetype="epub")
        # Author present but title empty → no match key.
        d = evaluate_format_dedup(
            announce=ann,
            format_priority=DEFAULT_PRIORITY,
            hold_seconds=HOLD_SECONDS,
            siblings=[],
        )
        assert d.action == "allow"
        assert d.reason == "format_dedup_no_match_key"

    def test_missing_priority_list_allows(self):
        # User explicitly removed the ebook priority list (e.g., set to
        # empty dict). Gate falls through to allow.
        d = evaluate_format_dedup(
            announce=_announce(filetype="epub"),
            format_priority={"audiobook": AUDIOBOOK_PRIORITY},  # no ebook
            hold_seconds=HOLD_SECONDS,
            siblings=[],
        )
        assert d.action == "allow"
        assert d.reason == "format_dedup_no_media_type_rule"

    def test_empty_priority_list_allows(self):
        d = evaluate_format_dedup(
            announce=_announce(filetype="epub"),
            format_priority={"ebook": [], "audiobook": AUDIOBOOK_PRIORITY},
            hold_seconds=HOLD_SECONDS,
            siblings=[],
        )
        assert d.action == "allow"
        assert d.reason == "format_dedup_empty_priority_list"


# ═══════════════════════════════════════════════════════════════
# v2.26.0 — multi-axis quality scoring at the dedup gate
# ═══════════════════════════════════════════════════════════════


# Mirrors DEFAULT_QUALITY_AXES — kept local so a settings refactor
# can't silently change the dedup behavior these tests pin.
AUDIOBOOK_AXES = [
    {
        "axis": "audio_bitrate_kbps",
        "tiers": [
            {"label": "320+ kbps", "min_value": 320},
            {"label": "192+ kbps", "min_value": 192},
            {"label": "128+ kbps", "min_value": 128},
            {"label": "64+ kbps",  "min_value": 64},
            {"label": "<64 kbps",  "min_value": 0},
        ],
    },
    {
        "axis": "audio_channels",
        "tiers": [
            {"label": "Stereo+", "min_value": 2},
            {"label": "Mono",    "min_value": 1},
        ],
    },
]
QUALITY_AXES = {"audiobook": AUDIOBOOK_AXES, "ebook": []}


def _audio_snapshot(*, bitrate=None, channels=None, fmt="AAC"):
    from app.quality.extract import QualitySnapshot
    return QualitySnapshot(
        mam_torrent_id="1",
        source="mediainfo",
        audio_format=fmt,
        audio_bitrate_kbps=bitrate,
        audio_channels=channels,
    )


class TestQualityAxesAtDedupGate:
    """Numeric-axis tiebreakers fire when both candidate and sibling
    have populated quality snapshots. When either side lacks data, the
    gate falls back to format-only comparison (v2.9.0 behavior)."""

    def _audio(self, **kw):
        defaults = dict(
            title="Red Rising", author="Pierce Brown",
            category="Audiobooks - Sci-Fi", filetype="m4b",
        )
        defaults.update(kw)
        return _announce(**defaults)

    def test_better_bitrate_preempts_held_same_format(self):
        """m4b@320 arriving preempts m4b@64 sitting on a hold.

        Today's v2.9.0 same-format dedup never preempts; the new
        multi-axis path fires when both sides have quality data.
        """
        held_sib = SiblingMatch(
            where="held", book_format="m4b", hold_id=42,
            quality_snapshot=_audio_snapshot(bitrate=64, channels=1),
        )
        d = evaluate_format_dedup(
            announce=self._audio(),
            format_priority=DEFAULT_PRIORITY,
            quality_axes=QUALITY_AXES,
            hold_seconds=HOLD_SECONDS,
            siblings=[held_sib],
            candidate_snapshot=_audio_snapshot(bitrate=320, channels=2),
        )
        assert d.action == "allow"
        assert d.preempt_hold_ids == (42,)

    def test_higher_quality_inflight_sibling_blocks_disabled(self):
        """A disabled-format candidate (mp3) is blocked by a higher-
        scoring in-flight m4b sibling — the v2.9.0 rule, untouched.
        Quality-axis data isn't needed here because format already
        differentiates them."""
        inflight = SiblingMatch(
            where="in_flight", book_format="m4b", grab_id=7,
            quality_snapshot=_audio_snapshot(bitrate=128, channels=2),
        )
        d = evaluate_format_dedup(
            announce=self._audio(filetype="mp3"),
            format_priority=DEFAULT_PRIORITY,
            quality_axes=QUALITY_AXES,
            hold_seconds=HOLD_SECONDS,
            siblings=[inflight],
        )
        assert d.action == "skip"
        assert d.reason == "format_dedup_higher_priority_inflight"

    def test_no_preempt_when_same_quality(self):
        """m4b@128 arriving while m4b@128 is held — same score, no
        preempt (we don't want a tied candidate stealing a hold)."""
        held = SiblingMatch(
            where="held", book_format="m4b", hold_id=99,
            quality_snapshot=_audio_snapshot(bitrate=128, channels=2),
        )
        d = evaluate_format_dedup(
            announce=self._audio(),
            format_priority=DEFAULT_PRIORITY,
            quality_axes=QUALITY_AXES,
            hold_seconds=HOLD_SECONDS,
            siblings=[held],
            candidate_snapshot=_audio_snapshot(bitrate=128, channels=2),
        )
        assert d.action == "allow"
        assert d.preempt_hold_ids == ()

    def test_no_preempt_when_candidate_worse(self):
        """m4b@64 arriving while m4b@320 is held — worse score, no
        preempt. Held sibling is the better option; let it ride out
        its window."""
        held = SiblingMatch(
            where="held", book_format="m4b", hold_id=99,
            quality_snapshot=_audio_snapshot(bitrate=320, channels=2),
        )
        d = evaluate_format_dedup(
            announce=self._audio(),
            format_priority=DEFAULT_PRIORITY,
            quality_axes=QUALITY_AXES,
            hold_seconds=HOLD_SECONDS,
            siblings=[held],
            candidate_snapshot=_audio_snapshot(bitrate=64, channels=2),
        )
        assert d.action == "allow"
        assert d.preempt_hold_ids == ()

    def test_v290_behavior_preserved_when_no_snapshots(self):
        """Announce-time path: no candidate_snapshot, no per-sibling
        snapshots. Quality axes are present in config but every numeric
        axis ranks as 'unknown' on both sides — comparison collapses
        to format-only, identical to v2.9.0."""
        held = SiblingMatch(
            where="held", book_format="mp3", hold_id=55,
        )
        d = evaluate_format_dedup(
            announce=self._audio(filetype="m4b"),
            format_priority=DEFAULT_PRIORITY,
            quality_axes=QUALITY_AXES,
            hold_seconds=HOLD_SECONDS,
            siblings=[held],
        )
        # m4b is enabled + higher-priority than mp3 → allow + preempt.
        assert d.action == "allow"
        assert d.preempt_hold_ids == (55,)


# ═══════════════════════════════════════════════════════════════
# Integration: lookup_dedup_siblings against real SQLite DBs
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
async def ebook_library(tmp_path, monkeypatch, temp_db):
    """One ebook library, isolated DB. Returns the library dict."""
    from app import config as app_config

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    await disco_db.init_db("calibre-main")

    lib = {"slug": "calibre-main", "content_type": "ebook", "app_type": "calibre"}
    monkeypatch.setattr(state, "_discovered_libraries", [lib])
    yield lib


async def _insert_grab(
    mam_torrent_id: str, torrent_name: str, dedup_key: str,
    book_format: str, state: str = "submitted",
) -> int:
    """Insert a grab row into the global DB; return its id."""
    from app.database import get_db as get_app_db

    db = await get_app_db()
    try:
        cur = await db.execute(
            "INSERT INTO grabs (mam_torrent_id, torrent_name, state, "
            "                   book_format, dedup_key) "
            "VALUES (?, ?, ?, ?, ?)",
            (mam_torrent_id, torrent_name, state, book_format, dedup_key),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _insert_hold(
    dedup_key: str, media_type: str, book_format: str,
    torrent_id: str, torrent_name: str,
    state: str = "pending", release_at: str = "2099-01-01 00:00:00",
) -> int:
    from app.database import get_db as get_app_db

    db = await get_app_db()
    try:
        cur = await db.execute(
            "INSERT INTO pending_holds (dedup_key, media_type, book_format, "
            "  torrent_id, torrent_name, release_at, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dedup_key, media_type, book_format, torrent_id, torrent_name,
             release_at, state),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _insert_owned_book(
    slug: str, title: str, author: str, formats: str = "EPUB",
) -> None:
    db = await disco_db.get_db(slug)
    try:
        existing = await (await db.execute(
            "SELECT id FROM authors WHERE name = ?", (author,),
        )).fetchone()
        if existing:
            author_id = existing["id"]
        else:
            cur = await db.execute(
                "INSERT INTO authors (name, sort_name) VALUES (?, ?)",
                (author, author),
            )
            author_id = cur.lastrowid
        # v3.0.0: books.author_id dropped; seed book_authors instead.
        cur = await db.execute(
            "INSERT INTO books (title, source, owned, formats, hidden) "
            "VALUES (?, 'calibre', 1, ?, 0)",
            (title, formats),
        )
        book_id = cur.lastrowid
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (?, ?, 0)",
            (book_id, author_id),
        )
        await db.commit()
    finally:
        await db.close()


class TestLookupDedupSiblings:
    async def test_empty_key_returns_empty(self, ebook_library):
        result = await lookup_dedup_siblings(
            dedup_key="", media_type="ebook",
        )
        assert result == []

    async def test_finds_inflight_grab(self, ebook_library):
        key = normalize_dedup_key("The Delves", "Keleros")
        grab_id = await _insert_grab(
            mam_torrent_id="1240987", torrent_name="The Delves",
            dedup_key=key, book_format="azw3",
        )

        result = await lookup_dedup_siblings(
            dedup_key=key, media_type="ebook",
        )
        in_flight = [s for s in result if s.where == "in_flight"]
        assert len(in_flight) == 1
        assert in_flight[0].book_format == "azw3"
        assert in_flight[0].grab_id == grab_id

    async def test_terminal_grab_states_ignored(self, ebook_library):
        # A `complete` grab is no longer competing; same for any failed_*.
        key = normalize_dedup_key("The Delves", "Keleros")
        await _insert_grab(
            mam_torrent_id="1240987", torrent_name="The Delves",
            dedup_key=key, book_format="azw3", state="complete",
        )
        await _insert_grab(
            mam_torrent_id="1240988", torrent_name="The Delves",
            dedup_key=key, book_format="azw3", state="failed_unknown",
        )

        result = await lookup_dedup_siblings(
            dedup_key=key, media_type="ebook",
        )
        assert [s for s in result if s.where == "in_flight"] == []

    async def test_finds_pending_hold(self, ebook_library):
        key = normalize_dedup_key("The Delves", "Keleros")
        hold_id = await _insert_hold(
            dedup_key=key, media_type="ebook", book_format="azw3",
            torrent_id="1240987", torrent_name="The Delves",
        )

        result = await lookup_dedup_siblings(
            dedup_key=key, media_type="ebook",
        )
        held = [s for s in result if s.where == "held"]
        assert len(held) == 1
        assert held[0].book_format == "azw3"
        assert held[0].hold_id == hold_id

    async def test_resolved_hold_ignored(self, ebook_library):
        key = normalize_dedup_key("The Delves", "Keleros")
        await _insert_hold(
            dedup_key=key, media_type="ebook", book_format="azw3",
            torrent_id="1240987", torrent_name="The Delves",
            state="released",
        )
        await _insert_hold(
            dedup_key=key, media_type="ebook", book_format="mobi",
            torrent_id="1240988", torrent_name="The Delves",
            state="dropped",
        )

        result = await lookup_dedup_siblings(
            dedup_key=key, media_type="ebook",
        )
        assert [s for s in result if s.where == "held"] == []

    async def test_finds_owned_book(self, ebook_library):
        await _insert_owned_book(
            "calibre-main", "The Delves", "Keleros", formats="EPUB,AZW3",
        )
        key = normalize_dedup_key("The Delves", "Keleros")

        result = await lookup_dedup_siblings(
            dedup_key=key, media_type="ebook",
        )
        owned = [s for s in result if s.where == "owned"]
        assert len(owned) == 1
        # First listed format is what surfaces — see SiblingMatch docs.
        assert owned[0].book_format == "epub"
        assert owned[0].library_slug == "calibre-main"

    async def test_owned_filtered_by_content_type(
        self, tmp_path, monkeypatch, temp_db,
    ):
        """An ebook announce only checks ebook libraries for owned.
        An audiobook library's owned copy is NOT a block — the user
        wants both formats."""
        from app import config as app_config

        monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
        await disco_db.init_db("calibre-main")
        await disco_db.init_db("abs-audio")

        libs = [
            {"slug": "calibre-main", "content_type": "ebook"},
            {"slug": "abs-audio", "content_type": "audiobook"},
        ]
        monkeypatch.setattr(state, "_discovered_libraries", libs)

        # Audiobook of "The Delves" is owned; an ebook announce should
        # still NOT see it as a block.
        await _insert_owned_book(
            "abs-audio", "The Delves", "Keleros", formats="M4B",
        )
        key = normalize_dedup_key("The Delves", "Keleros")

        result = await lookup_dedup_siblings(
            dedup_key=key, media_type="ebook",
        )
        assert [s for s in result if s.where == "owned"] == []

    async def test_combined_three_way_match(self, ebook_library):
        """In-flight + held + owned for the same key — all three surface."""
        key = normalize_dedup_key("The Delves", "Keleros")
        await _insert_grab(
            mam_torrent_id="1240987", torrent_name="The Delves",
            dedup_key=key, book_format="azw3",
        )
        await _insert_hold(
            dedup_key=key, media_type="ebook", book_format="mobi",
            torrent_id="1240988", torrent_name="The Delves",
        )
        await _insert_owned_book(
            "calibre-main", "The Delves", "Keleros", formats="PDF",
        )

        result = await lookup_dedup_siblings(
            dedup_key=key, media_type="ebook",
        )
        wheres = sorted(s.where for s in result)
        assert wheres == ["held", "in_flight", "owned"]
