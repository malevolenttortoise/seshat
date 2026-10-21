"""Discovery fix pass S20 (G203 / G204): co-authors train when the book is
delivered, not at grab.

The dispatcher saves MAM's co-author list on the grab and trains nobody; a
pending or rejected review trains nobody; delivery (an approved review, or
no review) trains the saved list; a book credited to 4+ authors trains
nobody. The 2026-08-29 case: a namesake match on Kobold Press's "Deep Magic
Volume 1" trained three RPG writers at grab, and 16 RPG grabs later the
allow list held 56 such names, some from grabs Mark rejected in review.
"""
from __future__ import annotations

from app.database import get_db
from app.orchestrator.download_watcher import CompletionEvent
from app.orchestrator.pipeline import deliver_reviewed, process_completion
from app.storage import grabs as grabs_storage
from app.storage import pipeline as pipe_storage
from app.storage import review_queue as review_storage
from tests.orchestrator.test_review_queue import _make_epub
from tests.orchestrator.test_manual_grab import mam_search  # noqa: F401 (fixture)
from tests.orchestrator.test_manual_grab_upload import me  # noqa: F401 (fixture)


async def _allowed(db) -> dict[str, str]:
    rows = await (await db.execute("SELECT name, source FROM authors_allowed")).fetchall()
    return {r["name"]: r["source"] for r in rows}


async def _grab(db, tmp_path, coauthors, author_blob="Test Author") -> CompletionEvent:
    grab_id = await grabs_storage.create_grab(
        db, announce_id=None, mam_torrent_id="12345",
        torrent_name="Test Book", category="ebooks fantasy",
        author_blob=author_blob, state=grabs_storage.STATE_DOWNLOADED,
        coauthors=coauthors,
    )
    source_dir = tmp_path / "downloads" / "Test Book"
    _make_epub(source_dir / "Test Book.epub")
    run_id = await pipe_storage.create_run(
        db, grab_id=grab_id, qbit_hash="hash_abc", source_path=str(source_dir),
    )
    return CompletionEvent(
        grab_id=grab_id, qbit_hash="hash_abc", torrent_name="Test Book",
        save_path=str(source_dir), pipeline_run_id=run_id,
    )


async def _process(db, event, tmp_path, *, review: bool):
    return await process_completion(
        db, event, staging_path="", default_sink="folder", calibre_library_path="",
        folder_sink_path=str(tmp_path / "library"), category_routing={},
        ntfy_url="", ntfy_topic="",
        review_queue_enabled=review, review_staging_path=str(tmp_path / "review-staging"),
    )


async def test_the_grab_keeps_its_coauthor_list(temp_db):
    db = await get_db()
    try:
        gid = await grabs_storage.create_grab(
            db, announce_id=None, mam_torrent_id="1", torrent_name="T", category="c",
            author_blob="A", state=grabs_storage.STATE_FETCHED, coauthors=["Ann One", "Bo Two"],
        )
        assert await grabs_storage.grab_coauthors(db, gid) == ["Ann One", "Bo Two"]
        gid2 = await grabs_storage.create_grab(
            db, announce_id=None, mam_torrent_id="2", torrent_name="T", category="c",
            author_blob="A", state=grabs_storage.STATE_FETCHED,
        )
        assert await grabs_storage.grab_coauthors(db, gid2) == []
    finally:
        await db.close()


async def test_a_book_under_review_and_then_rejected_trains_nobody(temp_db, tmp_path):
    from app.routers.review import RejectRequest, reject
    db = await get_db()
    try:
        event = await _grab(db, tmp_path, ["Ann One", "Bo Two"])
        assert await _process(db, event, tmp_path, review=True) is True
        assert await _allowed(db) == {}
        review_id = (await review_storage.list_pending(db))[0].id
    finally:
        await db.close()
    res = await reject(review_id, RejectRequest(note="not mine"))
    assert res.ok
    db = await get_db()
    try:
        assert await _allowed(db) == {}
    finally:
        await db.close()


async def test_an_approved_book_trains_its_coauthors_on_delivery(temp_db, tmp_path):
    db = await get_db()
    try:
        event = await _grab(db, tmp_path, ["Ann One", "Bo Two"])
        await _process(db, event, tmp_path, review=True)
        review_id = (await review_storage.list_pending(db))[0].id
        assert await deliver_reviewed(
            db, review_id=review_id, default_sink="folder", calibre_library_path="",
            folder_sink_path=str(tmp_path / "library"), ntfy_url="", ntfy_topic="",
            auto_train_enabled=True, was_timeout=False,
        )
        assert await _allowed(db) == {
            "Test Author": "auto_train", "Ann One": "coauthor_train", "Bo Two": "coauthor_train",
        }
    finally:
        await db.close()


async def test_without_review_delivery_trains_at_once(temp_db, tmp_path):
    db = await get_db()
    try:
        event = await _grab(db, tmp_path, ["Ann One"])
        assert await _process(db, event, tmp_path, review=False) is True
        assert (await _allowed(db))["Ann One"] == "coauthor_train"
    finally:
        await db.close()


async def test_a_book_with_four_or_more_authors_trains_nobody(temp_db, tmp_path):
    """Deep Magic Volume 1: your Chris Harris + three RPG writers."""
    db = await get_db()
    try:
        names = ["Chris Harris", "Wolfgang Baur", "Jeff Lee", "Dan Dillon"]
        event = await _grab(db, tmp_path, names, author_blob=", ".join(names))
        assert await _process(db, event, tmp_path, review=False) is True
        assert await _allowed(db) == {}
    finally:
        await db.close()


# ─── The dispatcher saves, never trains ─────────────────────


async def test_dispatch_saves_the_coauthors_and_trains_nobody(temp_db, monkeypatch):
    from app.orchestrator import dispatch
    from tests.orchestrator.test_dispatch import _make_announce, _make_deps, _make_filter_config

    async def _names(*_a, **_k):
        return ["Brandon Sanderson", "Co Writer"]

    monkeypatch.setattr(dispatch, "coauthor_names", _names)
    deps = _make_deps(filter_config=_make_filter_config(allowed=["Brandon Sanderson"]))
    result = await dispatch.handle_announce(deps, _make_announce())
    assert result.action == "submit"
    db = await get_db()
    try:
        assert await grabs_storage.grab_coauthors(db, result.grab_id) == ["Brandon Sanderson", "Co Writer"]
        assert await _allowed(db) == {}
    finally:
        await db.close()


async def test_a_user_grab_saves_them_too(temp_db, monkeypatch):
    """inject / tentative approve / hold release / delayed / send to
    pipeline: the skip-filter grabs, same rule."""
    from app.orchestrator import dispatch
    from tests.orchestrator.test_dispatch import _make_deps, _make_filter_config

    async def _names(*_a, **_k):
        return ["Ann One", "Bo Two"]

    monkeypatch.setattr(dispatch, "coauthor_names", _names)
    deps = _make_deps(filter_config=_make_filter_config(allowed=[]))
    result = await dispatch.inject_grab(deps, torrent_id="9999", torrent_name="A Requested Book")
    assert result.action == "submit"
    db = await get_db()
    try:
        assert await grabs_storage.grab_coauthors(db, result.grab_id) == ["Ann One", "Bo Two"]
        assert await _allowed(db) == {}
    finally:
        await db.close()


async def test_an_announce_the_filter_skips_reads_no_coauthors(temp_db, monkeypatch):
    """Only an author match (or a user grab) keeps a co-author list, as
    before; a skipped announce costs no torrent_info call."""
    from app.orchestrator import dispatch
    from tests.orchestrator.test_dispatch import _make_announce, _make_deps, _make_filter_config

    called = []

    async def _names(*_a, **_k):
        called.append(1)
        return ["X"]

    monkeypatch.setattr(dispatch, "coauthor_names", _names)
    deps = _make_deps(filter_config=_make_filter_config(allowed=["Someone Else"]))
    result = await dispatch.handle_announce(deps, _make_announce())
    assert result.action == "skip" and called == []


async def test_an_uploaded_torrent_saves_them_too(temp_db, mam_search, me, monkeypatch):
    """Manual Grab's upload path (`grab_uploaded_torrent`), same rule."""
    from app.orchestrator import dispatch
    from tests.orchestrator.test_dispatch import _make_deps
    from tests.orchestrator.test_manual_grab_upload import _torrent

    async def _names(*_a, **_k):
        return ["Brandon Sanderson", "Co Writer"]

    monkeypatch.setattr(dispatch, "coauthor_names", _names)
    result = await dispatch.grab_uploaded_torrent(_make_deps(), torrent_bytes=_torrent())
    assert result.action == "submit", result
    db = await get_db()
    try:
        assert await grabs_storage.grab_coauthors(db, result.grab_id) == ["Brandon Sanderson", "Co Writer"]
        assert await _allowed(db) == {}
    finally:
        await db.close()
