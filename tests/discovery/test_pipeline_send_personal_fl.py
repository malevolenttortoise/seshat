"""Send-to-pipeline: a successful personal-FL buy marks the grab free.

MAM's search API takes 5-20 min to report `personal_freeleech`, so the
grab can't learn it by re-reading the API; the buy result is passed to
`inject_grab(personal_fl_bought=...)` instead (Manual Grab D16).
"""
from __future__ import annotations

from app import state
from app.discovery.routers import pipeline_send
from app.orchestrator.dispatch import DispatchResult
from tests.orchestrator.test_dispatch import _make_deps


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows


class _LibraryDb:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, sql, params=()):
        return _Cursor(self._rows)

    async def close(self):
        return None


def _book(book_id: int, tid: str) -> dict:
    return {
        "id": book_id, "title": f"Book {book_id}", "mam_url": f"https://www.myanonamouse.net/t/{tid}",
        "mam_status": "found", "mam_torrent_id": tid, "mam_category": "Ebooks - Fantasy",
        "mam_formats": "epub", "source_url": None, "isbn": None, "series_id": None,
        "series_index": None, "cover_url": None, "description": None, "page_count": None,
        "author_name": "Brandon Sanderson", "series_name": None,
    }


async def test_buy_result_reaches_inject_grab(temp_db, monkeypatch):
    from app.routers import inject as inject_mod

    rows = [_book(1, "101"), _book(2, "102")]
    outcomes = iter([True, False])
    seen: list[tuple[str, bool]] = []

    async def fake_library_db():
        return _LibraryDb(rows)

    async def fake_buy(torrent_id, token):
        return next(outcomes)

    async def fake_inject(deps, **kwargs):
        seen.append((kwargs["torrent_id"], kwargs["personal_fl_bought"]))
        return DispatchResult(action="submit", reason="ok", announce_id=1)

    monkeypatch.setattr(pipeline_send, "get_discovery_db", fake_library_db)
    monkeypatch.setattr(pipeline_send, "inject_grab", fake_inject)
    monkeypatch.setattr(inject_mod, "_buy_personal_fl_for_inject", fake_buy)
    state.dispatcher = _make_deps()
    try:
        out = await pipeline_send.send_to_pipeline({"book_ids": [1, 2], "buy_personal_fl": True})
    finally:
        state.dispatcher = None
    assert out["sent"] == 2
    assert seen == [("101", True), ("102", False)]
