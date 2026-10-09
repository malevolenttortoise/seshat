"""The weekly Goodreads author-ID job (2026-10 audit wave 4b, G84 / G90 /
G91 / G104) and scans no longer resolving author IDs (call 5)."""
from __future__ import annotations

import uuid

import pytest

from app import state


@pytest.fixture
async def libs(tmp_path, monkeypatch):
    """An ebook and an audiobook library with their own discovery DBs."""
    from app import config as app_config
    from app.discovery import database as disco_db
    from app.discovery import author_identity, goodreads_author_backfill, metadata_cache
    monkeypatch.setattr(author_identity, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(metadata_cache, "DATA_DIR", tmp_path)
    tag = uuid.uuid4().hex[:8]
    ebook, audio = f"ebooks-{tag}", f"audio-{tag}"
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": ebook, "content_type": "ebook"},
        {"slug": audio, "content_type": "audiobook"},
    ])
    monkeypatch.setattr(state, "_source_scan_refs", 0)
    monkeypatch.setattr(state, "_library_sync_in_progress", False)
    monkeypatch.setattr(state, "_hygiene_task", None)
    await disco_db.init_db(ebook)
    await disco_db.init_db(audio)
    await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
    goodreads_author_backfill.reset_attempted_for_tests()
    prev = disco_db.get_active_library()
    disco_db.set_active_library(ebook)
    yield {"ebook": ebook, "audio": audio, "tag": tag}
    disco_db.set_active_library(prev)
    goodreads_author_backfill.reset_attempted_for_tests()


async def _author(slug, name, *, gr=None, books=()):
    """An author in `slug` with `books` = [(title, goodreads_id, isbn)]."""
    from app.discovery.database import get_db
    db = await get_db(slug=slug)
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name, goodreads_id) "
            "VALUES (?,?,?,?)", (name, name, name.lower(), gr),
        )
        aid = cur.lastrowid
        for title, gr_book, isbn in books:
            b = await db.execute(
                "INSERT INTO books (title, goodreads_id, isbn, owned, source) "
                "VALUES (?,?,?,1,'calibre')", (title, gr_book, isbn),
            )
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) VALUES (?,?,0)",
                (b.lastrowid, aid),
            )
        await db.commit()
        return aid
    finally:
        await db.close()


async def _gr_id(slug, aid):
    from app.discovery.database import get_db
    db = await get_db(slug=slug)
    try:
        (v,) = await (await db.execute(
            "SELECT goodreads_id FROM authors WHERE id = ?", (aid,))).fetchone()
        return v
    finally:
        await db.close()


async def test_the_copy_step_fills_a_twin_without_a_request(libs, monkeypatch):
    from app.discovery import author_identity
    from app.discovery.goodreads_author_backfill import copy_goodreads_ids_from_twins
    name = f"Twin Author {libs['tag']}"
    e = await _author(libs["ebook"], name, gr="777")
    a = await _author(libs["audio"], name)
    await author_identity.get_or_create_person(libs["ebook"], e)
    await author_identity.get_or_create_person(libs["audio"], a)
    from app.metadata import goodreads_session as gs

    async def no_session():
        raise AssertionError("the copy step sends nothing")
    monkeypatch.setattr(gs, "get_session", no_session)
    assert await copy_goodreads_ids_from_twins() == 1
    assert await _gr_id(libs["audio"], a) == "777"
    assert await copy_goodreads_ids_from_twins() == 0       # nothing left


async def test_the_weekly_job_works_ebook_authors_with_books_every_run(libs, monkeypatch):
    import app.discovery.goodreads_author_backfill as bf
    from app.discovery.database import get_active_library
    tag = libs["tag"]
    with_id = await _author(libs["ebook"], f"Has Isbn {tag}", books=[("B1", None, "978111")])
    no_ids = await _author(libs["ebook"], f"No Ids {tag}", books=[("B2", None, None)])
    bookless = await _author(libs["ebook"], f"Bookless {tag}")
    audio = await _author(libs["audio"], f"Audio Only {tag}", books=[("B3", None, "978333")])
    p1, p2 = [], []

    async def fake_p1(aid):
        p1.append((get_active_library(), aid))
        return None

    async def fake_p2(aid, name):
        p2.append((get_active_library(), aid))
        return None
    monkeypatch.setattr(bf, "resolve_author_goodreads_id", fake_p1)
    monkeypatch.setattr(bf, "resolve_author_via_calibre_coauthor", fake_p2)

    await bf.weekly_author_id_backfill()
    await bf.weekly_author_id_backfill()              # G91: a fresh set each run
    ebook = libs["ebook"]
    assert p1 == [(ebook, with_id)] * 2
    assert sorted(p2) == sorted([(ebook, with_id), (ebook, no_ids)] * 2)
    # (Author IDs are per library: the exact lists above already leave out
    # the bookless author and the audiobook library's.)
    assert bookless not in [aid for _, aid in p2]
    del audio
    assert get_active_library() == ebook              # restored


async def test_hygiene_keeps_trying_each_author_once_per_process(libs, monkeypatch):
    import app.discovery.goodreads_author_backfill as bf
    a = await _author(libs["ebook"], f"Once {libs['tag']}", books=[("X", "1", None)])
    tried = []

    async def fake_p1(aid):
        tried.append(aid)
        return None

    async def fake_p2(aid, name):
        return None
    monkeypatch.setattr(bf, "resolve_author_goodreads_id", fake_p1)
    monkeypatch.setattr(bf, "resolve_author_via_calibre_coauthor", fake_p2)
    await bf.backfill_missing_author_ids()
    await bf.backfill_missing_author_ids()
    assert tried == [a]


async def test_the_weekly_job_waits_for_a_running_scan(libs, monkeypatch):
    import app.discovery.goodreads_author_backfill as bf
    monkeypatch.setattr(state, "_source_scan_refs", 1)
    monkeypatch.setattr(bf, "_WEEKLY_WAIT_MAX_S", 0.0)

    async def boom(*a, **k):
        raise AssertionError("nothing runs while a scan has the library")
    monkeypatch.setattr(bf, "backfill_missing_author_ids", boom)
    assert await bf.weekly_author_id_backfill() == {"skipped": "a source scan"}


def test_the_weekly_job_is_registered_for_sunday_11():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from app.orchestrator.scheduler import register_goodreads_author_backfill
    scheduler = AsyncIOScheduler()
    register_goodreads_author_backfill(scheduler)
    job = scheduler.get_job("goodreads_author_id_backfill")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["day_of_week"], fields["hour"], fields["minute"]) == ("sun", "11", "0")


async def test_a_scan_no_longer_resolves_a_goodreads_author_id(libs, monkeypatch):
    """Call 5: a scan of an author without a Goodreads ID sends Goodreads
    nothing for it; the weekly job resolves the ID."""
    import app.discovery.goodreads_author_backfill as bf
    from app.discovery import lookup
    from app.discovery.sources.goodreads import GoodreadsSource
    aid = await _author(libs["ebook"], f"No Gr Id {libs['tag']}", books=[("B", "5", None)])

    calls = []

    async def record(*a, **k):        # `_try_source` swallows exceptions
        calls.append(a)
        return "999"
    monkeypatch.setattr(bf, "resolve_author_goodreads_id", record)
    n = await lookup._try_source(
        GoodreadsSource(), "No Gr Id", aid, [], ["English"], "goodreads",
    )
    assert n == 0 and calls == []
