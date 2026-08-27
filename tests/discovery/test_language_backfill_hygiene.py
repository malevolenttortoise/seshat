"""v3.10.0 — Hygiene Jobs 15/16: language backfill + foreign sweep.

`4cedbfa` persists language going FORWARD only, which left 99.3% of
unowned rows on the reference install with language NULL and made the
sweep inert (it measured 0 rows). Job 15 backfills from the cheapest
bulk endpoint each source offers; Job 16 hides what parses as non-English.

The properties that matter:
  - fill-if-empty (never overwrite a source-reported value),
  - values are normalized on the way in, so the backfill doesn't
    reintroduce the "en"/"eng"/"English" split it exists to resolve,
  - ADR-0005 attempted-set: a source that has no language for a row must
    not cause that row to be re-asked in the same run,
  - the sweep HIDES rather than deletes, never touches owned or
    already-hidden rows, and fires only on `is_foreign`.
"""
from __future__ import annotations

import aiosqlite
import pytest

from app import config as app_config
from app.discovery import database as disco_db
from app.discovery import hygiene


@pytest.fixture
async def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


def _stats():
    return {
        "language_backfilled": 0, "language_backfill_attempted": 0,
        "language_backfill_skipped": 0, "foreign_books_hidden": 0,
        "errors": [],
    }


async def _book(db, title, *, owned=0, source="hardcover", language=None,
                hardcover_id=None, openlibrary_id=None, hidden=0,
                author_id=None):
    cur = await db.execute(
        "INSERT INTO books (title, owned, source, language, hardcover_id, "
        "openlibrary_id, hidden) VALUES (?,?,?,?,?,?,?)",
        (title, owned, source, language, hardcover_id, openlibrary_id, hidden),
    )
    bid = cur.lastrowid
    if author_id is not None:
        await db.execute(
            "INSERT INTO book_authors (book_id, author_id, position) "
            "VALUES (?,?,0)", (bid, author_id))
    return bid


async def _author(db, aid, name, *, openlibrary_id=None):
    await db.execute(
        "INSERT INTO authors (id, name, sort_name, normalized_name, "
        "openlibrary_id) VALUES (?,?,?,?,?)",
        (aid, name, name, name.lower(), openlibrary_id))


async def _read(db, bid):
    row = await (await db.execute(
        "SELECT language, hidden FROM books WHERE id = ?", (bid,))).fetchone()
    return dict(row)


# ─── Job 15 — Hardcover batch path ────────────────────────────


async def test_backfill_fills_from_hardcover_and_normalizes(lib, monkeypatch):
    """Hardcover emits 'English' / raw ISO 639-2. Stored value must be
    the normalized code, not the source's spelling."""
    db = await disco_db.get_db("test")
    try:
        b_en = await _book(db, "English One", hardcover_id="111")
        b_de = await _book(db, "German One", hardcover_id="222")
        await db.commit()
    finally:
        await db.close()

    monkeypatch.setattr(hygiene, "load_settings", lambda: {
        "hardcover_api_key": "k"})

    async def fake(src, ids):
        assert set(ids) == {"111", "222"}
        return {"111": "en", "222": "de"}
    monkeypatch.setattr(hygiene, "_hc_languages_for", fake)

    st = _stats()
    await hygiene.job_language_backfill("test", st)
    assert st["errors"] == []
    assert st["language_backfilled"] == 2

    db = await disco_db.get_db("test")
    try:
        assert (await _read(db, b_en))["language"] == "en"
        assert (await _read(db, b_de))["language"] == "de"
    finally:
        await db.close()


async def test_backfill_never_overwrites_an_existing_language(lib, monkeypatch):
    db = await disco_db.get_db("test")
    try:
        kept = await _book(db, "Already Known", hardcover_id="111",
                           language="de")
        await db.commit()
    finally:
        await db.close()

    monkeypatch.setattr(hygiene, "load_settings", lambda: {
        "hardcover_api_key": "k"})

    async def fake(src, ids):
        raise AssertionError("row with a language must not be queried")
    monkeypatch.setattr(hygiene, "_hc_languages_for", fake)

    st = _stats()
    await hygiene.job_language_backfill("test", st)
    assert st["language_backfilled"] == 0

    db = await disco_db.get_db("test")
    try:
        assert (await _read(db, kept))["language"] == "de"
    finally:
        await db.close()


async def test_owned_rows_are_not_backfilled(lib, monkeypatch):
    db = await disco_db.get_db("test")
    try:
        await _book(db, "Owned", owned=1, source="calibre", hardcover_id="111")
        await db.commit()
    finally:
        await db.close()

    monkeypatch.setattr(hygiene, "load_settings", lambda: {
        "hardcover_api_key": "k"})

    async def fake(src, ids):
        raise AssertionError("owned rows are out of scope")
    monkeypatch.setattr(hygiene, "_hc_languages_for", fake)

    st = _stats()
    await hygiene.job_language_backfill("test", st)
    assert st["language_backfilled"] == 0


async def test_source_with_no_language_leaves_row_null_and_doesnt_retry(
        lib, monkeypatch):
    """ADR-0005: a row the source has no language for is marked attempted,
    so it can't be re-asked. This is the shape that burned ~5,800 calls."""
    db = await disco_db.get_db("test")
    try:
        b = await _book(db, "No Language", hardcover_id="111")
        await db.commit()
    finally:
        await db.close()

    monkeypatch.setattr(hygiene, "load_settings", lambda: {
        "hardcover_api_key": "k"})
    calls = []

    async def fake(src, ids):
        calls.append(list(ids))
        return {}
    monkeypatch.setattr(hygiene, "_hc_languages_for", fake)

    st = _stats()
    await hygiene.job_language_backfill("test", st)
    assert len(calls) == 1
    assert st["language_backfilled"] == 0
    assert st["language_backfill_attempted"] == 1

    db = await disco_db.get_db("test")
    try:
        assert (await _read(db, b))["language"] is None
    finally:
        await db.close()


async def test_batch_failure_is_non_fatal(lib, monkeypatch):
    db = await disco_db.get_db("test")
    try:
        b = await _book(db, "Boom", hardcover_id="111")
        await db.commit()
    finally:
        await db.close()

    monkeypatch.setattr(hygiene, "load_settings", lambda: {
        "hardcover_api_key": "k"})

    async def fake(src, ids):
        raise RuntimeError("hardcover down")
    monkeypatch.setattr(hygiene, "_hc_languages_for", fake)

    st = _stats()
    await hygiene.job_language_backfill("test", st)
    assert st["errors"] == []          # swallowed, logged, not fatal
    assert st["language_backfilled"] == 0

    db = await disco_db.get_db("test")
    try:
        assert (await _read(db, b))["language"] is None
    finally:
        await db.close()


async def test_no_api_key_skips_hardcover_cleanly(lib, monkeypatch):
    db = await disco_db.get_db("test")
    try:
        await _book(db, "Needs Key", hardcover_id="111")
        await db.commit()
    finally:
        await db.close()

    monkeypatch.setattr(hygiene, "load_settings", lambda: {})

    async def no_secret(_k):
        return ""
    import app.secrets as secrets_mod
    monkeypatch.setattr(secrets_mod, "get_secret", no_secret)

    st = _stats()
    await hygiene.job_language_backfill("test", st)
    assert st["errors"] == []
    assert st["language_backfilled"] == 0


# ─── Job 15 — OpenLibrary bulk-per-author path ────────────────


async def test_backfill_fills_from_openlibrary_in_bulk_per_author(
        lib, monkeypatch):
    """OL resolves a whole author in 1-2 calls, so rows are grouped by
    the author's OL key rather than queried per book."""
    db = await disco_db.get_db("test")
    try:
        await _author(db, 1, "Holly Black", openlibrary_id="OL12345A")
        b1 = await _book(db, "The Cruel Prince", source="openlibrary",
                         openlibrary_id="OL111W", author_id=1)
        b2 = await _book(db, "El rey malvado", source="openlibrary",
                         openlibrary_id="OL222W", author_id=1)
        await db.commit()
    finally:
        await db.close()

    seen = []

    class FakeOL:
        async def _fetch_work_languages(self, key):
            seen.append(key)
            # English-among-many counts as English.
            return {"OL111W": ["eng", "dut", "heb"], "OL222W": ["spa"]}

    import app.discovery.sources.openlibrary as ol_mod
    monkeypatch.setattr(ol_mod, "OpenLibrarySource", lambda *a, **k: FakeOL())
    monkeypatch.setattr(hygiene, "load_settings", lambda: {})

    st = _stats()
    await hygiene.job_language_backfill("test", st)
    assert seen == ["OL12345A"], "one bulk call for the whole author"
    assert st["language_backfilled"] == 2

    db = await disco_db.get_db("test")
    try:
        assert (await _read(db, b1))["language"] == "en"
        assert (await _read(db, b2))["language"] == "es"
    finally:
        await db.close()


# ─── Job 16 — the sweep ───────────────────────────────────────


async def test_sweep_hides_foreign_and_leaves_english_and_unknown(lib):
    db = await disco_db.get_db("test")
    try:
        de = await _book(db, "Die Spiderwick Geheimnisse", language="de")
        en = await _book(db, "The Cruel Prince", language="en")
        unk = await _book(db, "Mystery Language", language=None)
        junk = await _book(db, "Unparseable", language="Klingon")
        await db.commit()
    finally:
        await db.close()

    st = _stats()
    await hygiene.job_foreign_language_sweep("test", st)
    assert st["foreign_books_hidden"] == 1

    db = await disco_db.get_db("test")
    try:
        assert (await _read(db, de))["hidden"] == 1
        assert (await _read(db, en))["hidden"] == 0
        assert (await _read(db, unk))["hidden"] == 0
        assert (await _read(db, junk))["hidden"] == 0, (
            "an unparseable language must never be swept")
    finally:
        await db.close()


async def test_sweep_never_touches_owned_books(lib):
    db = await disco_db.get_db("test")
    try:
        owned = await _book(db, "Owned German", owned=1, source="calibre",
                            language="de")
        await db.commit()
    finally:
        await db.close()

    st = _stats()
    await hygiene.job_foreign_language_sweep("test", st)
    assert st["foreign_books_hidden"] == 0

    db = await disco_db.get_db("test")
    try:
        assert (await _read(db, owned))["hidden"] == 0
    finally:
        await db.close()


async def test_sweep_does_not_rehide_an_unhidden_row(lib):
    """The operator's un-hide has to stick across runs."""
    db = await disco_db.get_db("test")
    try:
        b = await _book(db, "Unhidden By Operator", language="de", hidden=0)
        await db.commit()
    finally:
        await db.close()

    st = _stats()
    await hygiene.job_foreign_language_sweep("test", st)
    assert st["foreign_books_hidden"] == 1

    # operator un-hides
    db = await disco_db.get_db("test")
    try:
        await db.execute("UPDATE books SET hidden = 0 WHERE id = ?", (b,))
        await db.commit()
    finally:
        await db.close()

    # The `language_swept_at` marker is what makes the un-hide stick.
    # Without it the predicate is stateless and the next run re-hides
    # the book the operator just said they wanted.
    st2 = _stats()
    await hygiene.job_foreign_language_sweep("test", st2)
    assert st2["foreign_books_hidden"] == 0

    db = await disco_db.get_db("test")
    try:
        assert (await _read(db, b))["hidden"] == 0
    finally:
        await db.close()


async def test_sweep_is_idempotent_while_untouched(lib):
    db = await disco_db.get_db("test")
    try:
        await _book(db, "Foreign", language="ja")
        await db.commit()
    finally:
        await db.close()

    first, second = _stats(), _stats()
    await hygiene.job_foreign_language_sweep("test", first)
    await hygiene.job_foreign_language_sweep("test", second)
    assert first["foreign_books_hidden"] == 1
    assert second["foreign_books_hidden"] == 0
