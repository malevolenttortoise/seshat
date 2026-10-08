"""Grab record-keeping (2026-10 audit issue 11; Mark's G9, G37, G46).

- The IRC line an announce came from is stored in `announces.raw`.
- The policy tier a grab went through is stored in `grabs.policy_tier`.
- A rejected review ends its grab: `rejected` (or `complete` when another
  book of the bundle was delivered), the pipeline run closed. The torrent
  ID stays blocked (ADR-0022); format dedup no longer sees it in flight.
- The appended migration settles grabs stuck before the fix.
"""
from __future__ import annotations

import httpx
from fastapi import FastAPI

from app import main, state
from app.database import get_db
from app.mam.irc import IrcClient, IrcConfig
from app.orchestrator import format_dedup
from app.storage import grabs as grabs_storage
from app.storage import pipeline as pipe_storage
from app.storage import review_queue as review_storage
from tests.orchestrator.test_live_dispatcher import (
    TID,
    _make_announce,
    _sanderson_deps,
)
from tests.orchestrator.test_manual_grab import mam_search  # noqa: F401  (fixture)


# ─── The IRC line (G9) ───────────────────────────────────────


async def test_the_irc_client_hands_over_the_line(monkeypatch):
    line = (
        ":MouseBot!mouse@bot PRIVMSG #announce :New Torrent: "
        "placeholder line, parsed by a stub"
    )
    got: list[tuple] = []

    async def on_announce(announce, raw_line=""):
        got.append((announce, raw_line))

    client = IrcClient(
        IrcConfig(server="fake", nick="n", channel="#announce", announcer_nick="MouseBot"),
        on_announce,
    )
    from app.mam import irc as irc_mod

    parsed = _make_announce(torrent_id=TID)
    monkeypatch.setattr(irc_mod, "parse_announce", lambda text: parsed)
    msg = irc_mod.parse_irc_line(line)
    await client._handle_privmsg(msg)
    assert got == [(parsed, msg.trailing)]
    assert msg.trailing.startswith("New Torrent:")


async def test_the_bridge_stores_the_line_on_the_announce_row(temp_db, mam_search):
    state.dispatcher = _sanderson_deps()
    await main._on_irc_announce(_make_announce(torrent_id=TID), "the raw IRC line")

    db = await get_db()
    try:
        row = await (await db.execute("SELECT raw FROM announces")).fetchone()
    finally:
        await db.close()
    assert row[0] == "the raw IRC line"


# ─── The policy tier ─────────────────────────────────────────


async def test_a_grab_records_its_policy_tier(temp_db, mam_search):
    deps = _sanderson_deps()
    state.dispatcher = deps
    from app.orchestrator.dispatch import inject_grab

    result = await inject_grab(
        deps, torrent_id=TID, torrent_name="Test Book",
        author_blob="Brandon Sanderson",
    )
    assert result.grab_id

    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT policy_tier FROM grabs WHERE id = ?", (result.grab_id,),
        )).fetchone()
    finally:
        await db.close()
    assert row[0] in {"vip", "free", "wedge", "normal"}


# ─── Rejected reviews end their grab ─────────────────────────


async def _staged_grab(db, *, tid: str, reviews: int) -> tuple[int, int, list[int]]:
    """A grab the pipeline left awaiting review, with `reviews` books."""
    grab_id = await grabs_storage.create_grab(
        db, announce_id=None, mam_torrent_id=tid, torrent_name=f"Book {tid}",
        category="Ebooks - Fantasy", author_blob="Placeholder Author",
        state=grabs_storage.STATE_PROCESSING, qbit_hash="a" * 40,
        dedup_key=f"book {tid}|author",
    )
    run_id = await pipe_storage.create_run(
        db, grab_id=grab_id, state=pipe_storage.PIPE_AWAITING_REVIEW,
    )
    review_ids = []
    for i in range(reviews):
        review_ids.append(await review_storage.create_entry(
            db, grab_id=grab_id, pipeline_run_id=run_id,
            staged_path=f"/nonexistent/grab-{grab_id}-{i}",
            book_filename="book.epub", book_format="epub", metadata={},
            bundle_index=i, bundle_total=reviews,
        ))
    return grab_id, run_id, review_ids


def _review_app() -> httpx.AsyncClient:
    from app.routers.review import router as review_router

    app = FastAPI()
    app.include_router(review_router)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )


async def _states(grab_id: int, run_id: int) -> tuple[str, str]:
    db = await get_db()
    try:
        g = await (await db.execute("SELECT state FROM grabs WHERE id=?", (grab_id,))).fetchone()
        r = await (await db.execute("SELECT state FROM pipeline_runs WHERE id=?", (run_id,))).fetchone()
        return g[0], r[0]
    finally:
        await db.close()


async def _reject(review_id: int) -> None:
    from app.routers.review import router as review_router

    prefix = review_router.prefix
    async with _review_app() as c:
        r = await c.post(f"{prefix}/{review_id}/reject", json={})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


async def test_reject_ends_the_grab_and_closes_the_run(temp_db):
    db = await get_db()
    try:
        grab_id, run_id, (rid,) = await _staged_grab(db, tid="5001", reviews=1)
    finally:
        await db.close()

    await _reject(rid)

    assert await _states(grab_id, run_id) == ("rejected", "rejected")
    db = await get_db()
    try:
        # MAM served the bytes: the torrent ID stays blocked (ADR-0022) ...
        assert await grabs_storage.find_blocking_grab(db, "5001") is not None
        # ... but it's no longer an in-flight sibling for format dedup (G37).
        siblings = await format_dedup.lookup_dedup_siblings(
            dedup_key="book 5001|author", media_type="Ebook", libraries=[],
        )
        assert siblings == []
    finally:
        await db.close()


async def test_a_book_still_under_review_keeps_the_grab_open(temp_db):
    db = await get_db()
    try:
        grab_id, run_id, (r1, _r2) = await _staged_grab(db, tid="5002", reviews=2)
    finally:
        await db.close()

    await _reject(r1)
    assert await _states(grab_id, run_id) == ("processing", "awaiting_review")


async def test_a_bundle_with_a_delivered_book_ends_complete(temp_db):
    db = await get_db()
    try:
        grab_id, run_id, (r1, r2) = await _staged_grab(db, tid="5003", reviews=2)
        await review_storage.set_status(db, r1, review_storage.STATUS_DELIVERED)
    finally:
        await db.close()

    await _reject(r2)
    assert await _states(grab_id, run_id) == ("complete", "complete")


# ─── The backfill migration (G46) ────────────────────────────


async def test_the_migration_settles_grabs_stuck_before_the_fix(temp_db):
    from app.database import MIGRATIONS, init_db

    db = await get_db()
    try:
        rej, rej_run, (r1,) = await _staged_grab(db, tid="6001", reviews=1)
        await review_storage.set_status(db, r1, review_storage.STATUS_REJECTED)
        mixed, mixed_run, (m1, m2) = await _staged_grab(db, tid="6002", reviews=2)
        await review_storage.set_status(db, m1, review_storage.STATUS_DELIVERED)
        await review_storage.set_status(db, m2, review_storage.STATUS_REJECTED)
        open_, open_run, (o1, _o2) = await _staged_grab(db, tid="6003", reviews=2)
        await review_storage.set_status(db, o1, review_storage.STATUS_REJECTED)
        # Pretend this DB predates the issue-11 migrations.
        first_issue_11 = MIGRATIONS.index("ALTER TABLE grabs ADD COLUMN policy_tier TEXT")
        await db.execute(f"PRAGMA user_version = {first_issue_11}")
        await db.commit()
    finally:
        await db.close()

    await init_db()

    assert await _states(rej, rej_run) == ("rejected", "rejected")
    assert await _states(mixed, mixed_run) == ("complete", "complete")
    assert await _states(open_, open_run) == ("processing", "awaiting_review")


async def test_a_delivered_book_with_another_still_open_keeps_the_grab_open(temp_db):
    db = await get_db()
    try:
        grab_id, run_id, (r1, _r2, r3) = await _staged_grab(db, tid="5004", reviews=3)
        await review_storage.set_status(db, r1, review_storage.STATUS_DELIVERED)
    finally:
        await db.close()

    await _reject(r3)
    assert await _states(grab_id, run_id) == ("processing", "awaiting_review")


async def test_a_rejected_grab_blocks_its_torrent_id_even_without_a_hash(temp_db):
    db = await get_db()
    try:
        await grabs_storage.create_grab(
            db, announce_id=None, mam_torrent_id="5005", torrent_name="x",
            category="Ebooks - Fantasy", author_blob="y",
            state=grabs_storage.STATE_REJECTED,
        )
        assert await grabs_storage.find_blocking_grab(db, "5005") is not None
    finally:
        await db.close()
