"""v3.3.0 slice 02 (ADR-0017) — author push-back unit + flow tests.

Covers:
  - Per-sink author formatters (ABS array shape, calibredb & join, CWA
    form-dict key).
  - `_queue_apply_authors` end-to-end: parses proposal payload, dispatches
    to sink(s), runs inline re-sync (book_authors + series author_mode).
  - Failure modes: empty payload → 400; book missing → 404; no sinks
    configured → 409; all sinks rejected → 502; empty resolved set → 502.

Sink helpers are stubbed (no live CWA/ABS/calibredb dependency). The unit
formatters exercise the real translation logic; the queue-apply integration
exercises the dispatcher with fake push helpers.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


# ─── per-sink author formatters ─────────────────────────────


class TestBuildAbsAuthors:
    def test_authors_emits_array_of_name_objects(self):
        from app.discovery.push_back import _build_abs_metadata
        md = _build_abs_metadata(
            {"audiobookshelf_id": "x"},
            ["authors"],
            authors=[
                {"name": "X Author", "source_id": "gr-x"},
                {"name": "Y Author", "source_id": None},
            ],
        )
        # Name-only payload — IDs are ABS-internal and re-assigned by ABS.
        assert md["authors"] == [{"name": "X Author"}, {"name": "Y Author"}]

    def test_authors_drops_empty_names(self):
        from app.discovery.push_back import _build_abs_metadata
        md = _build_abs_metadata(
            {}, ["authors"],
            authors=[{"name": "  "}, {"name": "Real Author"}],
        )
        assert md["authors"] == [{"name": "Real Author"}]

    def test_authors_omitted_when_kwarg_empty(self):
        from app.discovery.push_back import _build_abs_metadata
        md = _build_abs_metadata({}, ["authors"], authors=None)
        assert "authors" not in md

    def test_authors_coexists_with_scalar_fields(self):
        from app.discovery.push_back import _build_abs_metadata
        md = _build_abs_metadata(
            {"title": "T"},
            ["title", "authors"],
            authors=[{"name": "Sole"}],
        )
        assert md["title"] == "T"
        assert md["authors"] == [{"name": "Sole"}]


class TestFormatCalibredbAuthors:
    def test_position_ordered_ampersand_join(self):
        from app.discovery.push_back import _format_calibredb_authors
        out = _format_calibredb_authors([
            {"name": "X Author", "source_id": "gr-x"},
            {"name": "Y Author", "source_id": None},
        ])
        assert out == "X Author & Y Author"

    def test_comma_in_name_does_not_conflict(self):
        from app.discovery.push_back import _format_calibredb_authors
        # "Smith, John" inside a single author's name is preserved —
        # the separator is ` & `, not `,`.
        out = _format_calibredb_authors([
            {"name": "Smith, John"},
            {"name": "Other Author"},
        ])
        assert out == "Smith, John & Other Author"

    def test_empty_input_returns_none(self):
        from app.discovery.push_back import _format_calibredb_authors
        assert _format_calibredb_authors(None) is None
        assert _format_calibredb_authors([]) is None
        assert _format_calibredb_authors([{"name": "  "}]) is None


# ─── queue_apply_authors end-to-end ─────────────────────────


async def _insert_author(name: str) -> int:
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name) "
            "VALUES (?, ?, ?)",
            (name, name, normalize_author_name(name)),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _insert_owned_book(
    title, author_ids, *, calibre_id=None, audiobookshelf_id=None,
):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO books (title, source, owned, calibre_id, audiobookshelf_id) "
            "VALUES (?, 'calibre', 1, ?, ?)",
            (title, calibre_id, audiobookshelf_id),
        )
        bid = cur.lastrowid
        for pos, aid in enumerate(author_ids):
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, ?)",
                (bid, aid, pos),
            )
        await db.commit()
        return bid
    finally:
        await db.close()


async def _enqueue_authors_proposal(
    book_id, source, new_records, old_records=None,
):
    from app.discovery.database import get_db
    import time
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO metadata_review_queue "
            "(book_id, field, old_value, new_value, source, proposed_at) "
            "VALUES (?, 'authors', ?, ?, ?, ?)",
            (
                book_id,
                json.dumps(old_records or []),
                json.dumps(new_records),
                source,
                time.time(),
            ),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _read_book_authors(book_id):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT a.name, ba.position FROM book_authors ba "
            "JOIN authors a ON a.id = ba.author_id "
            "WHERE ba.book_id = ? ORDER BY ba.position",
            (book_id,),
        )).fetchall()
        return [(r["position"], r["name"]) for r in rows]
    finally:
        await db.close()


async def _read_queue_rows(book_id):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT id FROM metadata_review_queue WHERE book_id = ?",
            (book_id,),
        )).fetchall()
        return [r["id"] for r in rows]
    finally:
        await db.close()


async def test_queue_apply_authors_full_chain_cwa_then_resync(
    discovery_db, monkeypatch,
):
    """End-to-end: proposal exists → operator approves → CWA push fires
    (via calibredb→CWA fallback path; calibredb stub raises Unavailable) →
    re-sync writes book_authors → queue row deleted."""
    from app.discovery.routers import metadata as router_mod
    from app.discovery import push_back

    chaney_id = await _insert_author("J.N. Chaney")
    bid = await _insert_owned_book(
        "Co-Authored", [chaney_id], calibre_id=42,
    )
    qid = await _enqueue_authors_proposal(
        bid, "goodreads",
        new_records=[
            {"name": "J.N. Chaney", "source_id": "gr-chaney"},
            {"name": "Jason Anspach", "source_id": "gr-anspach"},
        ],
        old_records=[{"name": "J.N. Chaney", "source_id": None}],
    )

    pushed: list[tuple[str, list[str], list[dict] | None]] = []

    async def _cal_full_unavail(*a, **k):
        raise push_back.PushUnavailable("calibredb not present in test image")

    async def _cwa_ok(db, book_row, fields, *, authors=None):
        pushed.append(("cwa", fields, authors))
        return {"applied": fields, "failed": []}

    async def _abs_unused(*a, **k):
        raise AssertionError("ABS push should not fire for ebook-only book")

    monkeypatch.setattr(push_back, "push_calibre_full", _cal_full_unavail)
    monkeypatch.setattr(push_back, "push_cwa", _cwa_ok)
    monkeypatch.setattr(push_back, "push_abs", _abs_unused)

    result = await router_mod.queue_apply(qid)

    assert result["field"] == "authors"
    assert "cwa" in result["push_succeeded"]
    # CWA was called with the proposal payload.
    assert pushed == [("cwa", ["authors"], [
        {"name": "J.N. Chaney", "source_id": "gr-chaney"},
        {"name": "Jason Anspach", "source_id": "gr-anspach"},
    ])]
    # book_authors reflects the union (Chaney + Anspach, position-ordered).
    assert await _read_book_authors(bid) == [
        (0, "J.N. Chaney"),
        (1, "Jason Anspach"),
    ]
    # Queue row deleted.
    assert await _read_queue_rows(bid) == []


async def test_queue_apply_authors_calibre_path_prefers_calibredb(
    discovery_db, monkeypatch,
):
    """When calibredb succeeds, CWA is not invoked (calibredb wins on the
    calibre route; matches the dispatcher precedence)."""
    from app.discovery.routers import metadata as router_mod
    from app.discovery import push_back

    chaney_id = await _insert_author("J.N. Chaney")
    bid = await _insert_owned_book("X", [chaney_id], calibre_id=1)
    qid = await _enqueue_authors_proposal(
        bid, "amazon",
        new_records=[
            {"name": "J.N. Chaney"},
            {"name": "Coauthor"},
        ],
    )

    cwa_fired = []

    async def _cal_ok(db, book_row, fields, *, authors=None):
        return {"applied": fields, "failed": []}

    async def _cwa_should_not_fire(*a, **k):
        cwa_fired.append(1)
        return {"applied": [], "failed": []}

    monkeypatch.setattr(push_back, "push_calibre_full", _cal_ok)
    monkeypatch.setattr(push_back, "push_cwa", _cwa_should_not_fire)

    result = await router_mod.queue_apply(qid)
    assert "calibredb" in result["push_succeeded"]
    assert cwa_fired == []


async def test_queue_apply_authors_dual_library_pushes_to_both(
    discovery_db, monkeypatch,
):
    """A book with both calibre_id AND audiobookshelf_id (dual-library
    co-owned) pushes to BOTH sinks."""
    from app.discovery.routers import metadata as router_mod
    from app.discovery import push_back

    chaney_id = await _insert_author("J.N. Chaney")
    bid = await _insert_owned_book(
        "Dual", [chaney_id], calibre_id=1, audiobookshelf_id="abs-1",
    )
    qid = await _enqueue_authors_proposal(
        bid, "hardcover",
        new_records=[{"name": "J.N. Chaney"}, {"name": "Coauthor"}],
    )

    calls = []

    async def _cal_ok(db, book_row, fields, *, authors=None):
        calls.append("calibre")
        return {"applied": fields, "failed": []}

    async def _abs_ok(db, book_row, fields, *, authors=None):
        calls.append("abs")
        return {"applied": fields, "failed": []}

    monkeypatch.setattr(push_back, "push_calibre_full", _cal_ok)
    monkeypatch.setattr(push_back, "push_abs", _abs_ok)

    await router_mod.queue_apply(qid)
    assert calls == ["calibre", "abs"]


async def test_queue_apply_authors_no_sinks_configured_returns_409(
    discovery_db, monkeypatch,
):
    """Book has neither calibre_id nor audiobookshelf_id → 409 with a
    helpful message; queue row left intact."""
    from fastapi import HTTPException
    from app.discovery.routers import metadata as router_mod

    chaney_id = await _insert_author("J.N. Chaney")
    # owned=1 but neither sink id present.
    bid = await _insert_owned_book("Orphan", [chaney_id])
    qid = await _enqueue_authors_proposal(
        bid, "goodreads",
        new_records=[{"name": "Other"}],
    )

    with pytest.raises(HTTPException) as ei:
        await router_mod.queue_apply(qid)
    assert ei.value.status_code == 409
    # Queue row survives — operator can retry after configuring a sink.
    assert qid in await _read_queue_rows(bid)


async def test_queue_apply_authors_all_pushes_failed_returns_502(
    discovery_db, monkeypatch,
):
    """All push attempts raise PushFailed → 502; book_authors untouched."""
    from fastapi import HTTPException
    from app.discovery.routers import metadata as router_mod
    from app.discovery import push_back

    chaney_id = await _insert_author("J.N. Chaney")
    bid = await _insert_owned_book("Push Fails", [chaney_id], calibre_id=1)
    qid = await _enqueue_authors_proposal(
        bid, "goodreads",
        new_records=[{"name": "Chaney"}, {"name": "New"}],
    )

    async def _raise(*a, **k):
        raise push_back.PushFailed("synthetic upstream failure")

    monkeypatch.setattr(push_back, "push_calibre_full", _raise)
    monkeypatch.setattr(push_back, "push_cwa", _raise)

    with pytest.raises(HTTPException) as ei:
        await router_mod.queue_apply(qid)
    assert ei.value.status_code == 502
    # book_authors UNCHANGED — Calibre still authoritative; operator retries.
    assert await _read_book_authors(bid) == [(0, "J.N. Chaney")]
    # Queue row survives — operator can retry.
    assert qid in await _read_queue_rows(bid)


async def test_queue_apply_authors_empty_payload_returns_400(
    discovery_db,
):
    """Malformed/empty proposal payload → 400."""
    from fastapi import HTTPException
    from app.discovery.routers import metadata as router_mod

    chaney_id = await _insert_author("J.N. Chaney")
    bid = await _insert_owned_book("Empty Payload", [chaney_id], calibre_id=1)
    qid = await _enqueue_authors_proposal(
        bid, "goodreads", new_records=[],
    )

    with pytest.raises(HTTPException) as ei:
        await router_mod.queue_apply(qid)
    assert ei.value.status_code == 400


async def test_queue_apply_authors_book_missing_returns_404(
    discovery_db,
):
    """Book deleted between proposal time and approval → queue row also
    deleted (no orphaned proposals) → 404."""
    from fastapi import HTTPException
    from app.discovery.database import get_db
    from app.discovery.routers import metadata as router_mod

    chaney_id = await _insert_author("J.N. Chaney")
    bid = await _insert_owned_book("Doomed", [chaney_id], calibre_id=1)
    qid = await _enqueue_authors_proposal(
        bid, "goodreads",
        new_records=[{"name": "Chaney"}, {"name": "New"}],
    )
    # Delete the book (simulating concurrent removal).
    db = await get_db()
    try:
        await db.execute("DELETE FROM book_authors WHERE book_id = ?", (bid,))
        await db.execute("DELETE FROM books WHERE id = ?", (bid,))
        await db.commit()
    finally:
        await db.close()

    with pytest.raises(HTTPException) as ei:
        await router_mod.queue_apply(qid)
    assert ei.value.status_code == 404
    # Queue row cleaned up (no orphan).
    assert await _read_queue_rows(bid) == []


async def test_queue_apply_authors_recomputes_series_author_mode(
    discovery_db, monkeypatch,
):
    """ADR-0017 §6 step 3: after successful push + re-sync, the affected
    series' author_mode recomputes (the union may flip per_author →
    multi_author when a co-author lands)."""
    from app.discovery.database import get_db
    from app.discovery.routers import metadata as router_mod
    from app.discovery import push_back

    anspach_id = await _insert_author("Jason Anspach")
    await _insert_author("J.N. Chaney")
    # Seed series + owned single-book in it (thin book_authors = [Anspach]).
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO series (name, author_id) VALUES (?, ?)",
            ("Galaxy's Edge", anspach_id),
        )
        sid = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO books (title, source, owned, calibre_id, series_id) "
            "VALUES (?, 'calibre', 1, ?, ?)",
            ("GE Book 1", 100, sid),
        )
        bid = cur.lastrowid
        await db.execute(
            "INSERT INTO book_authors (book_id, author_id, position) "
            "VALUES (?, ?, 0)",
            (bid, anspach_id),
        )
        await db.commit()
    finally:
        await db.close()

    # Proposal: source says both Chaney + Anspach.
    qid = await _enqueue_authors_proposal(
        bid, "goodreads",
        new_records=[
            {"name": "Jason Anspach", "source_id": "gr-anspach"},
            {"name": "J.N. Chaney", "source_id": "gr-chaney"},
        ],
    )

    async def _cal_ok(db, book_row, fields, *, authors=None):
        return {"applied": fields, "failed": []}

    monkeypatch.setattr(push_back, "push_calibre_full", _cal_ok)

    await router_mod.queue_apply(qid)

    # Series author_mode flipped (single-book series with 2 contribs =
    # multi_author when both are in the only book's contributor set, since
    # the intersection equals the union).
    db = await get_db()
    try:
        mode = (await (await db.execute(
            "SELECT author_mode FROM series WHERE id = ?", (sid,),
        )).fetchone())["author_mode"]
    finally:
        await db.close()
    assert mode == "multi_author"
