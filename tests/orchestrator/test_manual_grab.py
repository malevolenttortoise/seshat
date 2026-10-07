"""Manual Grab, slice A — paste a MAM link, preview it, grab it.

MAM's search API is faked at the HTTP seam (`torrent_info._do_post`), so
the real torrent-info cache and the real pacer run; every search call is
counted. Refusals are proven by asserting `fetch_torrent` was NOT
called — never by fetching anything twice (ADR-0022, ADR-0023).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.database import get_db
from app.mam import search_pacer, torrent_info
from app.mam.torrent_id import extract_torrent_id
from app.orchestrator import dispatch, manual_grab
from app.orchestrator.dispatch import inject_grab
from app.orchestrator.format_dedup import SiblingMatch
from app.orchestrator.owned_announce_claim import OwnedClaimResult
from app.policy.engine import PolicyConfig
from app.storage import grabs as grabs_storage
from tests.orchestrator.test_dispatch import _make_deps

TID = "1274788"


def _item(tid: str = TID, **overrides) -> dict:
    item = {
        "id": tid,
        "title": "The Way of Kings",
        "author_info": json.dumps({"1": "Brandon Sanderson"}),
        "narrator_info": json.dumps({"7": "Michael Kramer", "8": "Kate Reading"}),
        "series_info": json.dumps({"5": ["The Stormlight Archive", "1"]}),
        "catname": "Ebooks - Fantasy",
        "size": "2000000",
        "filetype": "epub",
        "vip": 0, "free": 0, "fl_vip": 0, "personal_freeleech": 0,
        "my_snatched": 0,
        "seeders": 12,
    }
    item.update(overrides)
    return item


class _Resp:
    def __init__(self, data):
        self.status_code = 200
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


@pytest.fixture
def mam_search(monkeypatch):
    """MAM's search API by torrent ID. `items[tid]` = the result row, an
    exception to raise, or absent (= not found). `calls` logs every
    real search call (cache hits never reach it)."""
    box: dict = {"items": {TID: _item()}, "calls": []}

    async def fake_post(url, token=None, payload=None, timeout=15):
        tid = json.loads(payload)["tor"]["id"]
        box["calls"].append(tid)
        answer = box["items"].get(tid)
        if isinstance(answer, Exception):
            raise answer
        return _Resp({"data": [answer] if answer else []})

    monkeypatch.setattr(torrent_info, "_do_post", fake_post)
    return box


@pytest.fixture
def fake_clock(monkeypatch):
    """The pacer's clock: sleeping advances it instead of waiting."""
    clock = {"now": 1000.0, "sleeps": [], "starts": []}

    async def fake_sleep(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(search_pacer, "_clock", lambda: clock["now"])
    monkeypatch.setattr(search_pacer, "_sleep", fake_sleep)
    return clock


@pytest.fixture(autouse=True)
def _own_cover_cache(tmp_path, monkeypatch):
    """The cover cache sits under DATA_DIR, which the suite shares."""
    monkeypatch.setattr(manual_grab, "cover_dir", lambda: tmp_path / "covers")


@pytest.fixture(autouse=True)
def _no_leftover_jobs():
    yield
    manual_grab._jobs.clear()


@pytest.fixture
def covers(monkeypatch):
    """MAM's cover CDN: writes a small file, logs the call."""
    from app.metadata import covers as covers_mod

    calls: list[str] = []

    async def fake_fetch(torrent_id, *, dest_dir, basename="cover-mam", token=""):
        calls.append(torrent_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{basename}.jpg"
        path.write_bytes(b"\xff\xd8" + b"0" * 200)
        return path

    monkeypatch.setattr(covers_mod, "fetch_mam_cover", fake_fetch)
    return calls


async def _seed_grab(state: str, *, tid: str = TID, qbit_hash: str | None = None) -> int:
    db = await get_db()
    try:
        return await grabs_storage.create_grab(
            db, announce_id=None, mam_torrent_id=tid,
            torrent_name="The Way of Kings", category="Ebooks - Fantasy",
            author_blob="Brandon Sanderson", state=state, qbit_hash=qbit_hash,
        )
    finally:
        await db.close()


async def _grabs(tid: str = TID) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM grabs WHERE mam_torrent_id = ? ORDER BY id", (tid,),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _count(table: str) -> int:
    db = await get_db()
    try:
        cur = await db.execute(f"SELECT COUNT(*) AS n FROM {table}")
        return int((await cur.fetchone())["n"])
    finally:
        await db.close()


async def _run_job(deps, *items: manual_grab.GrabRequestItem) -> manual_grab.Job:
    job = manual_grab.start_job(deps, list(items))
    await asyncio.wait_for(asyncio.gather(*list(manual_grab._tasks)), timeout=10)
    assert job.done
    return job


def _link(value: str = TID, **kw) -> manual_grab.GrabRequestItem:
    return manual_grab.GrabRequestItem(kind="link", value=value, **kw)


# ─── Parsing a pasted link ───────────────────────────────────


class TestExtractTorrentId:
    @pytest.mark.parametrize("value", [
        "1274788",
        "  1274788 ",
        "01274788",
        "https://www.myanonamouse.net/t/1274788",
        "https://www.myanonamouse.net/t/1274788#torDetMainCon",
        "https://www.myanonamouse.net/tor/download.php?tid=1274788",
    ])
    def test_accepts(self, value):
        assert extract_torrent_id(value) == "1274788"

    @pytest.mark.parametrize("value", [
        "", "abc", "1274788&fl=1", "https://www.goodreads.com/book/show/7235533",
    ])
    def test_rejects(self, value):
        assert extract_torrent_id(value) is None


# ─── The pacer ───────────────────────────────────────────────


class TestPacer:
    async def test_spaces_calls_by_the_gap(self, fake_clock, monkeypatch):
        monkeypatch.setattr(search_pacer, "gap_seconds", lambda: 2.0)

        async def call():
            fake_clock["starts"].append(fake_clock["now"])

        for _ in range(4):
            await search_pacer.paced(call)
        starts = fake_clock["starts"]
        assert [b - a for a, b in zip(starts, starts[1:])] == [2.0, 2.0, 2.0]

    async def test_concurrent_callers_are_serialized(self, fake_clock, monkeypatch):
        monkeypatch.setattr(search_pacer, "gap_seconds", lambda: 2.0)

        async def call():
            fake_clock["starts"].append(fake_clock["now"])

        await asyncio.gather(*(search_pacer.paced(call) for _ in range(5)))
        starts = sorted(fake_clock["starts"])
        assert all(b - a >= 2.0 for a, b in zip(starts, starts[1:]))

    def test_gap_is_rate_mam_floored_at_one_second(self, monkeypatch):
        monkeypatch.setattr(search_pacer, "load_settings", lambda: {"rate_mam": 0})
        assert search_pacer.gap_seconds() == 1.0
        monkeypatch.setattr(search_pacer, "load_settings", lambda: {"rate_mam": 3})
        assert search_pacer.gap_seconds() == 3.0
        monkeypatch.setattr(search_pacer, "load_settings", lambda: {})
        assert search_pacer.gap_seconds() == 2.0

    async def test_cache_hit_skips_the_pacer(self, mam_search, fake_clock):
        await search_pacer.paced_torrent_info(TID, "tok")
        await search_pacer.paced_torrent_info(TID, "tok")
        assert mam_search["calls"] == [TID]
        assert fake_clock["sleeps"] == []


# ─── Preview ─────────────────────────────────────────────────


class TestPreview:
    async def test_ready_row_carries_the_metadata(self, temp_db, mam_search, fake_clock, covers):
        row = await manual_grab.preview_link(
            _make_deps(), f"https://www.myanonamouse.net/t/{TID}",
        )
        assert row.status == manual_grab.STATUS_READY
        assert row.torrent_id == TID
        assert row.title == "The Way of Kings"
        assert row.authors == ["Brandon Sanderson"]
        assert row.narrators == ["Michael Kramer", "Kate Reading"]
        assert row.series == [{"name": "The Stormlight Archive", "index": "1"}]
        assert row.filetype == "epub" and row.size_bytes == 2000000
        assert row.wedge_eligible is True
        assert row.cover_url == f"/api/v1/manual-grab/cover/{TID}"
        assert covers == [TID]

    async def test_preview_writes_nothing(self, temp_db, mam_search, fake_clock, covers):
        await manual_grab.preview_link(_make_deps(), TID)
        assert await _count("announces") == 0
        assert await _count("grabs") == 0

    async def test_bad_input_never_asks_mam(self, temp_db, mam_search):
        row = await manual_grab.preview_link(_make_deps(), "not a link")
        assert row.status == manual_grab.STATUS_BAD_INPUT
        assert mam_search["calls"] == []

    async def test_already_grabbed_never_asks_mam(self, temp_db, mam_search):
        gid = await _seed_grab(grabs_storage.STATE_SUBMITTED, qbit_hash="a" * 40)
        row = await manual_grab.preview_link(_make_deps(), TID)
        assert row.status == manual_grab.STATUS_ALREADY_GRABBED
        assert row.grab_id == gid
        assert mam_search["calls"] == []

    async def test_removed_from_mam(self, temp_db, mam_search, fake_clock):
        mam_search["items"].pop(TID)
        row = await manual_grab.preview_link(_make_deps(), TID)
        assert row.status == manual_grab.STATUS_REMOVED

    async def test_a_timeout_says_so(self, temp_db, mam_search, fake_clock):
        """The live check's upload row read "Couldn't reach MAM: network
        error:" — a timeout's message is empty. Name it (D26)."""
        import httpx

        mam_search["items"][TID] = httpx.ReadTimeout("")
        row = await manual_grab.preview_link(_make_deps(), TID)
        assert row.status == manual_grab.STATUS_LOOKUP_FAILED
        assert row.message.startswith("MAM didn't answer in time (ReadTimeout)")

    async def test_other_lookup_errors_carry_their_reason(self, temp_db, mam_search, fake_clock):
        import httpx

        mam_search["items"][TID] = httpx.ConnectError("")
        row = await manual_grab.preview_link(_make_deps(), TID)
        assert "ConnectError" in row.message

    async def test_snatched_on_mam(self, temp_db, mam_search, fake_clock, covers):
        mam_search["items"][TID] = _item(my_snatched=1)
        row = await manual_grab.preview_link(_make_deps(), TID)
        assert row.status == manual_grab.STATUS_SNATCHED

    async def test_owned_book_is_flagged_not_blocked(
        self, temp_db, mam_search, fake_clock, covers, monkeypatch,
    ):
        async def siblings(**kw):
            return [SiblingMatch(where="owned", book_format="epub", library_slug="calibre-library")]

        monkeypatch.setattr(manual_grab, "lookup_dedup_siblings", siblings)
        row = await manual_grab.preview_link(_make_deps(), TID)
        assert row.status == manual_grab.STATUS_OWNED
        assert row.owned_in == [{"library_slug": "calibre-library", "format": "epub"}]
        assert row.status not in manual_grab.BLOCKING_STATUSES

    async def test_free_torrent_is_not_wedge_eligible(self, temp_db, mam_search, fake_clock, covers):
        mam_search["items"][TID] = _item(free=1)
        row = await manual_grab.preview_link(_make_deps(), TID)
        assert row.freeleech is True and row.wedge_eligible is False

    async def test_lookup_and_cover_are_paced(self, temp_db, mam_search, fake_clock, covers, monkeypatch):
        monkeypatch.setattr(search_pacer, "gap_seconds", lambda: 2.0)
        mam_search["items"]["2"] = _item("2", title="Words of Radiance")
        await manual_grab.preview_link(_make_deps(), TID)
        await manual_grab.preview_link(_make_deps(), "2")
        # lookup, cover, lookup, cover: three gaps between four MAM calls.
        assert fake_clock["sleeps"] == [2.0, 2.0, 2.0]

    async def test_cover_is_cached_on_disk(self, temp_db, mam_search, fake_clock, covers):
        await manual_grab.preview_link(_make_deps(), TID)
        await manual_grab.preview_link(_make_deps(), TID)
        assert covers == [TID]


# ─── inject_grab: the two new flags ──────────────────────────


class TestInjectFlags:
    async def test_claim_for_owned_is_skipped_for_manual_grabs(
        self, temp_db, mam_search, monkeypatch,
    ):
        consulted: list[str] = []

        async def fake_claim(*, announce):
            consulted.append(announce.torrent_id)
            return OwnedClaimResult(claimed=True, library_slug="calibre-library", book_id=1)

        from app.orchestrator import owned_announce_claim
        monkeypatch.setattr(owned_announce_claim, "try_claim_announce_for_owned", fake_claim)

        deps = _make_deps()
        claimed = await inject_grab(
            deps, torrent_id=TID, torrent_name="The Way of Kings",
            category="Ebooks - Fantasy", author_blob="Brandon Sanderson",
        )
        assert claimed.reason == "claimed_for_owned"

        mam_search["items"]["2"] = _item("2")
        grabbed = await inject_grab(
            deps, torrent_id="2", torrent_name="The Way of Kings",
            category="Ebooks - Fantasy", author_blob="Brandon Sanderson",
            apply_claim_for_owned=False,
        )
        assert grabbed.action == "submit" and grabbed.error is None
        assert consulted == [TID]

    @pytest.fixture
    def broke_account(self, monkeypatch):
        """Zero upload buffer, ten wedges."""
        from app.mam.user_status import UserStatus

        async def status(token=None, ttl=300):
            return UserStatus(
                ratio=2.0, wedges=10, seedbonus=0, classname="", username="",
                uid=1, uploaded_bytes=0, downloaded_bytes=0, upload_buffer_bytes=0,
            )

        monkeypatch.setattr(dispatch, "get_user_status", status)

    @staticmethod
    def _recording_fetch(deps):
        from tests.fake_mam import MINIMAL_BENCODED_TORRENT
        from app.mam.grab import GrabResult

        calls: list[dict] = []

        async def fetch(torrent_id, token, **kwargs):
            calls.append({"tid": torrent_id, **kwargs})
            return GrabResult(success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT)

        deps.fetch_torrent = fetch
        return calls

    async def test_personal_fl_from_the_site_passes_the_buffer_gate(
        self, temp_db, mam_search, broke_account,
    ):
        """Wedged or "Bought as FL" on MAM's site: once the search API
        reports personal_freeleech, the grab is free (D36)."""
        deps = _make_deps()
        deps.policy_config = PolicyConfig(buffer_gate_enabled=True)
        calls = self._recording_fetch(deps)

        gated = await inject_grab(deps, torrent_id=TID)
        assert gated.reason == "policy:buffer_insufficient"
        assert calls == []

        mam_search["items"]["2"] = _item("2", personal_freeleech=1)
        free = await inject_grab(deps, torrent_id="2")
        assert free.action == "submit" and free.error is None
        assert [c["tid"] for c in calls] == ["2"]

    async def test_personal_fl_from_the_site_never_gets_a_wedge_on_top(
        self, temp_db, mam_search, broke_account,
    ):
        deps = _make_deps()
        deps.policy_config = PolicyConfig(use_wedge=True)
        calls = self._recording_fetch(deps)

        await inject_grab(deps, torrent_id=TID)
        mam_search["items"]["2"] = _item("2", personal_freeleech=1)
        await inject_grab(deps, torrent_id="2")
        assert [(c["tid"], c["use_fl_wedge"]) for c in calls] == [(TID, True), ("2", False)]


# ─── Grab all ────────────────────────────────────────────────


class TestGrabJob:
    async def test_happy_path_fills_the_grab_row(self, temp_db, mam_search, fake_clock):
        deps = _make_deps()
        job = await _run_job(deps, _link(f"https://www.myanonamouse.net/t/{TID}"))
        row = job.rows[0]
        assert row.status == "submitted", row.message
        assert len(deps.fetch_torrent.calls) == 1
        grab = (await _grabs())[0]
        assert grab["torrent_name"] == "The Way of Kings"
        assert grab["author_blob"] == "Brandon Sanderson"
        assert grab["category"] == "Ebooks - Fantasy"
        assert grab["book_format"] == "epub"

    async def test_already_grabbed_is_refused_without_a_fetch(self, temp_db, mam_search, fake_clock):
        await _seed_grab(grabs_storage.STATE_SUBMITTED, qbit_hash="a" * 40)
        deps = _make_deps()
        job = await _run_job(deps, _link())
        assert job.rows[0].status == "refused"
        assert job.rows[0].reason == "already_grabbed"
        assert deps.fetch_torrent.calls == []

    async def test_removed_is_refused_without_a_fetch(self, temp_db, mam_search, fake_clock):
        mam_search["items"].pop(TID)
        deps = _make_deps()
        job = await _run_job(deps, _link())
        assert job.rows[0].reason == "torrent_removed_from_mam"
        assert deps.fetch_torrent.calls == []
        assert await _grabs() == []

    async def test_lookup_failure_stops_the_row_without_a_fetch(self, temp_db, mam_search, fake_clock):
        mam_search["items"][TID] = torrent_info.TorrentInfoError("boom")
        deps = _make_deps()
        job = await _run_job(deps, _link())
        assert job.rows[0].status == "failed"
        assert deps.fetch_torrent.calls == []
        assert mam_search["calls"] == [TID]   # never retried unpaced

    async def test_snatched_needs_the_override(self, temp_db, mam_search, fake_clock):
        mam_search["items"][TID] = _item(my_snatched=1)
        deps = _make_deps()
        job = await _run_job(deps, _link())
        assert job.rows[0].reason == "already_snatched_on_mam"
        assert deps.fetch_torrent.calls == []

        job = await _run_job(deps, _link(override_mam_snatched=True))
        assert job.rows[0].status == "submitted"
        assert len(deps.fetch_torrent.calls) == 1

    async def test_owned_book_is_grabbed_when_ticked(self, temp_db, mam_search, fake_clock, monkeypatch):
        # A claim that would succeed if consulted. (Dispatch swallows
        # exceptions from the claim hook, so a raising fake proves nothing.)
        consulted: list[str] = []

        async def fake_claim(*, announce):
            consulted.append(announce.torrent_id)
            return OwnedClaimResult(claimed=True, library_slug="calibre-library", book_id=1)

        from app.orchestrator import owned_announce_claim
        monkeypatch.setattr(owned_announce_claim, "try_claim_announce_for_owned", fake_claim)
        deps = _make_deps()
        job = await _run_job(deps, _link())
        assert job.rows[0].status == "submitted"
        assert consulted == []

    async def test_one_search_call_per_row(self, temp_db, mam_search, fake_clock):
        mam_search["items"]["2"] = _item("2", title="Words of Radiance")
        deps = _make_deps()
        job = await _run_job(deps, _link(), _link("2"))
        assert [r.status for r in job.rows] == ["submitted", "submitted"]
        assert mam_search["calls"] == [TID, "2"]
