"""Wave C S8 — the missing-books cleanup (F13 / F14, G209–G212): the fix
pass's rules applied to missing books listed before them, Preview → Apply.
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from app import state


@pytest.fixture
async def env(tmp_path, monkeypatch):
    from app import config as app_config
    from app import database as pipeline_database
    from app.discovery import database as disco_db
    from app.discovery import metadata_cache, missing_cleanup, roster, source_blacklist

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    # Step 6 reads the source caches: keep them off the real DATA_DIR.
    monkeypatch.setattr(metadata_cache, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(pipeline_database, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "test", "name": "Books", "content_type": "ebook"},
        {"slug": "audio", "name": "Audio", "content_type": "audiobook"},
    ])
    settings = {"languages": ["English"],
                "metadata_sources": {"amazon": {"ebook_scan": True, "audiobook_scan": True}}}
    monkeypatch.setattr(missing_cleanup, "load_settings", lambda: dict(settings))
    await pipeline_database.init_db()
    await disco_db.init_db("test")
    await disco_db.init_db("audio")
    await metadata_cache.init_db(metadata_cache.SOURCE_AMAZON)
    await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
    monkeypatch.setitem(missing_cleanup._last, "preview", None)
    monkeypatch.setitem(missing_cleanup._last, "apply", None)
    roster.invalidate()
    source_blacklist.invalidate()
    yield {"tmp": tmp_path, "settings": settings}
    roster.invalidate()
    source_blacklist.invalidate()


async def _q(sql, args=(), slug="test"):
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        cur = await db.execute(sql, args)
        if sql.lstrip().upper().startswith("SELECT"):
            return [tuple(r) for r in await cur.fetchall()]
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _pq(sql, args=(), *, fk=True):
    from app.database import get_db
    db = await get_db()
    try:
        if not fk:
            await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute(sql, args)
        await db.commit()
    finally:
        await db.close()


async def _allow(name, source="autobrr_import"):
    from app.filter.normalize import normalize_author
    await _pq("INSERT INTO authors_allowed (name, normalized, source) VALUES (?,?,?)",
              (name, normalize_author(name), source))


async def _author(name, *, slug="test", list_missing=1):
    from app.metadata.author_names import normalize_author_name
    return await _q(
        "INSERT INTO authors (name, sort_name, normalized_name, list_missing) VALUES (?,?,?,?)",
        (name, name, normalize_author_name(name), list_missing), slug)


async def _book(title, author_ids, *, slug="test", owned=0, source="hardcover", calibre_id=None,
                abs_id=None, hidden=0, reason=None, series_id=None, index=None, **cols):
    names = ["title", "owned", "source", "discovered_by", "calibre_id", "audiobookshelf_id",
             "hidden", "hidden_reason", "series_id", "series_index", *cols]
    vals = [title, owned, "calibre" if calibre_id else source, None if calibre_id else source,
            calibre_id, abs_id, hidden, reason, series_id, index, *cols.values()]
    bid = await _q(f"INSERT INTO books ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                   vals, slug)
    for pos, aid in enumerate(author_ids):
        await _q("INSERT INTO book_authors (book_id, author_id, position) VALUES (?,?,?)",
                 (bid, aid, pos), slug)
    return bid


async def _exists(bid, slug="test"):
    return bool(await _q("SELECT 1 FROM books WHERE id = ?", (bid,), slug))


async def _apply():
    from app.discovery import missing_cleanup
    p = await missing_cleanup.preview()
    return p, await missing_cleanup.apply(p["id"])


def _count(report, key):
    return next(s for s in report["steps"] if s["key"] == key)["count"]


# ─── Step 1: no author you scan (D1 / F15, G212) ─────────────


async def test_a_book_no_scanned_author_wrote_goes(env):
    mine = await _author("Ann Owner")
    await _book("Owned One", [mine], owned=1, calibre_id=1)
    trained = await _author("Rachel Caine")
    await _allow("Rachel Caine", "coauthor_train")
    picked = await _author("Picked Person")
    await _allow("Picked Person", "tentative_approve")

    gone = await _book("Morganville 3", [trained])
    by_owner = await _book("Owner's Next", [mine])
    by_pick = await _book("Picked Book", [picked])
    shared = await _book("Co-written", [trained, mine])
    nobody = await _book("Nobody's", [])
    _, report = await _apply()
    assert not await _exists(gone) and not await _exists(nobody)
    assert await _exists(by_owner) and await _exists(by_pick) and await _exists(shared)
    assert _count(report, "no_eligible_author") == 2


async def test_rows_you_hid_showed_or_switched_off_and_grab_links_stay(env):
    trained = await _author("Rachel Caine")
    await _allow("Rachel Caine", "coauthor_train")
    yours = await _book("Hidden By You", [trained], hidden=1)
    shown = await _book("Shown By You", [trained], reason="shown")
    off = await _book("Author Off", [trained], hidden=1, reason="author_off")
    linked = await _book("Grabbed", [trained])
    await _pq("INSERT INTO book_grab_links (grab_id, library_slug, book_id) VALUES (1, 'test', ?)",
              (linked,), fk=False)
    await _apply()
    for bid in (yours, shown, off, linked):
        assert await _exists(bid)


async def test_a_switched_off_author_you_picked_still_counts(env):
    """The List-missing switch doesn't make an author unscanned for the
    cleanup: their books are `author_off`, and visible ones of theirs stay."""
    clancy = await _author("Tom Clancy", list_missing=0)
    await _allow("Tom Clancy", "tentative_approve")
    bid = await _book("Net Force 7", [clancy])
    await _apply()
    assert await _exists(bid)


async def test_owning_books_in_the_other_library_doesnt_count(env):
    """G212: Matt Dinniman owns audiobooks only; his ebook missing rows go,
    as the ebook scan gate already skips him."""
    dinniman = await _author("Matt Dinniman")
    abs_dinniman = await _author("Matt Dinniman", slug="audio")
    await _book("Dungeon Crawler Carl", [abs_dinniman], slug="audio", owned=1, abs_id="abs-1")
    ebook = await _book("Dungeon Crawler Carl", [dinniman])
    await _apply()
    assert not await _exists(ebook)
    assert await _exists(1, "audio")


# ─── Step 2: found only by OpenLibrary (D11, G210) ───────────


async def test_an_openlibrary_only_book_goes_unless_another_source_or_mam_knows_it(env):
    mine = await _author("Lin Carter")
    await _allow("Lin Carter")
    only = await _book("Tara of the Twilight", [mine], source="openlibrary")
    gr = await _book("The Black Pharaoh", [mine], source="openlibrary", goodreads_id="77")
    mam = await _book("Sky Pirates of Callisto", [mine], source="openlibrary", mam_torrent_id=293191,
                      mam_url="https://www.myanonamouse.net/t/293191")
    url = await _book("Time War", [mine], source="openlibrary",
                      source_url='{"openlibrary": "x", "amazon": "https://www.amazon.com/dp/B0"}')
    search = await _book("Star Rogue", [mine], source="openlibrary",
                         mam_url="https://www.myanonamouse.net/tor/browse.php?tor[text]=Star+Rogue")
    _, report = await _apply()
    assert not await _exists(only) and not await _exists(search)
    assert await _exists(gr) and await _exists(mam) and await _exists(url)
    assert _count(report, "openlibrary_only") == 2


# ─── Step 3: filtered families (ADR-0029) ────────────────────


async def test_a_stored_comic_is_hidden_with_its_reason(env):
    mine = await _author("Holly Black")
    await _allow("Holly Black")
    comic = await _book("Lucifer (2015-2017) #1", [mine])
    blurb = await _book("Dead Man's Hand", [mine], description="An anthology of the weird west.")
    novel = await _book("The Cruel Prince", [mine])
    shown = await _book("Lucifer (2015-2017) #2", [mine], reason="shown")
    _, report = await _apply()
    rows = dict((r[0], r[1:]) for r in await _q("SELECT id, hidden, hidden_reason FROM books"))
    assert rows[comic] == (1, "comic")
    page = {r["id"]: (r["reason"], r["stays_shown"])
            for r in next(s for s in report["steps"] if s["key"] == "filtered")["rows"]}
    assert page == {comic: ("comic", False), blurb: ("anthology", False)}
    assert rows[blurb] == (1, "anthology")
    assert rows[novel] == (0, None)
    assert rows[shown] == (0, "shown")


async def test_a_family_whose_setting_is_off_keeps_its_reason_and_shows(env):
    env["settings"]["hide_comics"] = False
    mine = await _author("Holly Black")
    await _allow("Holly Black")
    comic = await _book("Lucifer (2015-2017) #1", [mine])
    _, report = await _apply()
    assert await _q("SELECT hidden, hidden_reason FROM books WHERE id = ?", (comic,)) == [(0, "comic")]
    assert next(s for s in report["steps"] if s["key"] == "filtered")["rows"][0]["stays_shown"] is True


async def test_a_foreign_title_stays_when_english_isnt_read(env):
    env["settings"]["languages"] = ["German"]
    mine = await _author("Kevin J. Anderson")
    await _allow("Kevin J. Anderson")
    bid = await _book("Das Imperium", [mine])
    await _apply()
    assert await _q("SELECT hidden, hidden_reason FROM books WHERE id = ?", (bid,)) == [(0, None)]


# ─── Step 4: copies of an owned book (ADR-0028 + F10, G211) ──


async def test_a_missing_copy_folds_into_the_owned_book_which_keeps_its_byline(env):
    mine = await _author("Susan Dennard")
    co = await _author("Some Editor")
    await _allow("Susan Dennard")
    sid = await _q("INSERT INTO series (name, author_id) VALUES ('The Witchlands', ?)", (mine,))
    owned = await _book("Truthwitch", [mine], owned=1, calibre_id=81, series_id=sid, index=1.0)
    copy = await _book("Truthwitch: The Witchlands", [mine, co], source="amazon", amazon_id="B01",
                       series_id=sid)
    _, report = await _apply()
    assert not await _exists(copy)
    assert await _q("SELECT amazon_id FROM books WHERE id = ?", (owned,)) == [("B01",)]
    names = [r[0] for r in await _q(
        "SELECT a.name FROM book_authors ba JOIN authors a ON a.id = ba.author_id "
        "WHERE ba.book_id = ? ORDER BY ba.position", (owned,))]
    assert names == ["Susan Dennard"]
    assert await _q("SELECT reason FROM book_merges WHERE loser_id = ?", (copy,)) == [
        ("missing_cleanup_owned_copy",)]
    assert _count(report, "owned_copy") == 1
    row = next(s for s in report["steps"] if s["key"] == "owned_copy")["rows"][0]
    assert (row["into_id"], row["into_title"]) == (owned, "Truthwitch")


async def test_matches_that_arent_one_book_fold_nothing(env):
    mine = await _author("Kevin J. Anderson")
    await _allow("Kevin J. Anderson")
    sid = await _q("INSERT INTO series (name, author_id) VALUES ('Dune: House Corrino', ?)", (mine,))
    await _q("INSERT INTO series (name, author_id) VALUES ('Prelude to Dune', ?)", (mine,))
    await _book("Dune: House Corrino", [mine], owned=1, calibre_id=1292,
                series_id=sid + 1, index=3.0)
    v1 = await _book("Dune: House Corrino, Vol. 1", [mine], series_id=sid, index=1.0)
    v2 = await _book("Dune: House Corrino, Vol. 2", [mine], series_id=sid, index=2.0)
    _, report = await _apply()
    assert await _exists(v1) and await _exists(v2)
    step = next(s for s in report["steps"] if s["key"] == "owned_copy")
    assert step["count"] == 0 and {r["id"] for r in step["skipped"]} == {v1, v2}


async def test_an_owned_row_with_no_library_item_folds_into_the_calibre_book(env):
    """S6's flips leave owned rows with no Calibre id beside the Calibre book."""
    mine = await _author("Jim Butcher")
    await _allow("Jim Butcher")
    sid = await _q("INSERT INTO series (name, author_id) VALUES ('The Dresden Files', ?)", (mine,))
    owned = await _book("Small Favor", [mine], owned=1, calibre_id=515, series_id=sid, index=10.0)
    flipped = await _book("Book 10.0 - Small Favor", [mine], owned=1)
    await _apply()
    assert await _exists(owned) and not await _exists(flipped)


async def test_a_copy_folds_into_an_audiobookshelf_book_too(env):
    mine = await _author("Shirtaloon", slug="audio")
    await _allow("Shirtaloon")
    owned = await _book("He Who Fights With Monsters 3", [mine], slug="audio", owned=1, abs_id="a3")
    copy = await _book("He Who Fights With Monsters, Book 3: Hero", [mine], slug="audio")
    await _apply()
    assert await _exists(owned, "audio") and not await _exists(copy, "audio")


# ─── Step 5: the series a title names (F14 / G171) ───────────


async def test_a_standalone_titled_with_its_series_is_filed_there(env):
    mine = await _author("Tobias S. Buckell")
    await _allow("Tobias S. Buckell")
    existing = await _q("INSERT INTO series (name, author_id) VALUES ('Xenowealth', ?)", (mine,))
    new = await _book("The Rainy Season (The Ghosts Trilogy Book 3)", [mine])
    reused = await _book("Arctic Rising (Xenowealth Book 5)", [mine])
    plain = await _book("Hurricane Fever", [mine])
    await _apply()
    rows = dict((r[0], r[1:]) for r in await _q(
        "SELECT b.id, s.name, b.series_index FROM books b LEFT JOIN series s ON s.id = b.series_id"))
    assert rows[new] == ("The Ghosts Trilogy", 3.0)
    assert rows[reused] == ("Xenowealth", 5.0)
    assert rows[plain] == (None, None)
    assert await _q("SELECT series_id FROM books WHERE id = ?", (reused,)) == [(existing,)]


# ─── Step 6: the caches merged again (F13) ───────────────────


async def _amazon_cache(asin, books, slug="test"):
    import json
    import time
    from app.discovery import metadata_cache
    db = await metadata_cache.get_db(metadata_cache.SOURCE_AMAZON)
    try:
        await db.execute(
            f"INSERT INTO {metadata_cache.state_table()} (author_id, library_slug, last_scanned_at, "
            f"last_outcome, book_count) VALUES (?,?,?,?,?)", (asin, slug, time.time(), "ok", len(books)))
        for i, (title, series, pos) in enumerate(books):
            await db.execute(
                f"INSERT INTO {metadata_cache.books_table()} (author_id, library_slug, book_asin, title, "
                f"series_name, series_pos, format, language, raw_json, cached_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (asin, slug, f"{asin}-{i}", title, series, pos, "kindle_edition", "English", json.dumps({}),
                 time.time()))
        await db.commit()
    finally:
        await db.close()


async def _scanned(aid, asin=None, gr=None):
    await _q("UPDATE authors SET amazon_id = ?, goodreads_id = ?, last_lookup_at = 1.0 WHERE id = ?",
             (asin, gr, aid))


async def _gr_candidate(gr_author, aid, book_id, title, *, series=None, index=None, author="",
                        slug="test"):
    """A list entry the old merge called "existing", with its stored page."""
    import json
    import time
    from app.discovery import metadata_cache
    db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
    try:
        await db.execute(
            f"INSERT OR IGNORE INTO {metadata_cache.candidate_authors_table()} (author_id, library_slug, "
            f"seshat_author_id, seeded_at) VALUES (?,?,?,?)", (gr_author, slug, aid, time.time()))
        await db.execute(
            f"INSERT INTO {metadata_cache.candidates_table()} (author_id, library_slug, book_id, title, "
            f"list_json, state, reason, first_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (gr_author, slug, book_id, title, json.dumps({"title": title}), "created", "existing",
             time.time(), time.time()))
        await db.execute(
            f"INSERT INTO {metadata_cache.books_table(metadata_cache.SOURCE_GOODREADS)} (book_id, "
            f"page_fetched_at, page_json) VALUES (?,?,?)",
            (book_id, time.time(), json.dumps({
                "series_name": series, "series_index": index, "language": "English", "genres": [],
                "contributors": [{"name": author, "role": None}]})))
        await db.commit()
    finally:
        await db.close()


async def _titles(slug="test"):
    return sorted(r[0] for r in await _q("SELECT title FROM books", (), slug))


@pytest.fixture
def no_network(monkeypatch):
    """Every socket connect and curl_cffi request recorded and refused."""
    import socket
    tried = []

    def refuse(*a, **k):
        tried.append(a)
        raise OSError("no requests in the cleanup")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    try:
        import curl_cffi.requests as cr
        monkeypatch.setattr(cr.AsyncSession, "request", refuse)
        monkeypatch.setattr(cr.Session, "request", refuse)
    except ImportError:
        pass
    return tried


async def test_a_book_amazon_cached_for_a_scanned_author_comes_back(env, no_network):
    mine = await _author("Susan Dennard")
    await _scanned(mine, asin="B00DENNARD")
    await _book("Truthwitch", [mine], owned=1, calibre_id=81)
    await _amazon_cache("B00DENNARD", [("Truthwitch", "The Witchlands", 1.0),
                                       ("Windwitch", "The Witchlands", 2.0)])
    # Cached too, but never scanned: its first scan is the re-scans' job.
    other = await _author("Rachel Caine")
    await _q("UPDATE authors SET amazon_id = 'B00CAINE' WHERE id = ?", (other,))
    await _book("Glass Houses", [other], owned=1, calibre_id=82)
    await _amazon_cache("B00CAINE", [("Glass Houses", None, None), ("The Dead Girls' Dance", None, None)])
    before = await _q("SELECT id, name, amazon_id, goodreads_id, last_lookup_at FROM authors ORDER BY id")
    preview, report = await _apply()
    assert await _titles() == ["Glass Houses", "Truthwitch", "Windwitch"]
    assert _count(preview, "cache_remerge") == _count(report, "cache_remerge") == 1
    # No author-level writes, no requests.
    assert await _q("SELECT id, name, amazon_id, goodreads_id, last_lookup_at FROM authors ORDER BY id") == before
    assert no_network == []


async def test_a_cached_catalogue_sharing_no_owned_title_is_left_alone(env):
    mine = await _author("Edward Ashton")
    await _scanned(mine, asin="B00ASHTON")
    await _book("Mickey7", [mine], owned=1, calibre_id=81)
    await _amazon_cache("B00ASHTON", [("Of Bells and Clocks", None, None), ("Church Towers", None, None)])
    _, report = await _apply()
    assert await _titles() == ["Mickey7"]
    step = next(s for s in report["steps"] if s["key"] == "cache_remerge")
    assert [s["title"] for s in step["skipped"]] == ["Edward Ashton"]


async def test_an_author_a_scan_wouldnt_visit_gets_nothing_back(env):
    # Grab training added her as a co-author: linked, never scanned.
    trained = await _author("Rachel Caine")
    await _allow("Rachel Caine", "coauthor_train")
    await _scanned(trained, asin="B00CAINE", gr="999")
    await _amazon_cache("B00CAINE", [("Glass Houses", None, None)])
    await _gr_candidate("999", trained, "123", "The Dead Girls' Dance", author="Rachel Caine")
    await _apply()
    assert await _titles() == []


@pytest.mark.parametrize("stop", ["blacklisted", "amazon_not_asked", "library_only"])
async def test_what_stops_a_scan_stops_the_amazon_remerge(env, stop):
    from app.discovery import source_blacklist
    mine = await _author("Jay Aury")
    await _scanned(mine, asin="B079NLYFCQ")
    await _book("Beyond the Borderlands", [mine], owned=1, calibre_id=81)
    await _amazon_cache("B079NLYFCQ", [("Beyond the Borderlands", None, None), ("Station SS9", None, None)])
    if stop == "blacklisted":
        await source_blacklist.add("amazon", "B079NLYFCQ", author_name="Jay Aury")
    elif stop == "amazon_not_asked":
        env["settings"]["metadata_sources"] = {"amazon": {"ebook_scan": False, "audiobook_scan": True}}
    else:
        env["settings"]["author_scan_owned_only"] = True
    await _apply()
    assert await _titles() == ["Beyond the Borderlands"]


async def test_a_goodreads_book_the_old_merge_folded_comes_back_unless_a_row_holds_its_id(env, no_network):
    from app.discovery import source_blacklist
    mine = await _author("C. Y. Croc")
    await _scanned(mine, gr="555")
    sid = await _q("INSERT INTO series (name, author_id) VALUES ('Perinqual Galaxy Hybrids', ?)", (mine,))
    await _book("Xander Alien Hybrid", [mine], owned=1, calibre_id=81, series_id=sid, index=1.0)
    # Renamed on Amazon; the old merge put Goodreads' id on it.
    await _book("Drix Alien Hybrid", [mine], series_id=sid, index=2.0, source="amazon",
                goodreads_id="55867801")
    await _gr_candidate("555", mine, "55867801", "Drix Alien HalfBreed (Perinqual Galaxy Hybrids #2)",
                        series="Perinqual Galaxy Hybrids", index=2.0, author="C. Y. Croc")
    # Folded into book 1 before; its own book now.
    await _gr_candidate("555", mine, "57323658", "Saaxx Alien Hybrid (Perinqual Galaxy Hybrids #3)",
                        series="Perinqual Galaxy Hybrids", index=3.0, author="C. Y. Croc")
    # Another author's record, blacklisted.
    theirs = await _author("Edward Ashton")
    await _scanned(theirs, gr="777")
    await _book("Mickey7", [theirs], owned=1, calibre_id=82)
    await _gr_candidate("777", theirs, "120169016", "Of Bells and Clocks", author="Edward Ashton")
    await source_blacklist.add("goodreads", "777", author_name="Edward Ashton")
    _, report = await _apply()
    assert await _titles() == ["Drix Alien Hybrid", "Mickey7", "Saaxx Alien Hybrid (Perinqual Galaxy Hybrids #3)",
                               "Xander Alien Hybrid"]
    assert await _q("SELECT s.name, b.series_index, b.source, b.goodreads_id FROM books b "
                    "JOIN series s ON s.id = b.series_id WHERE b.title LIKE 'Saaxx%'") == [
        ("Perinqual Galaxy Hybrids", 3.0, "goodreads", "57323658")]
    step = next(s for s in report["steps"] if s["key"] == "cache_remerge")
    assert sorted(s["detail"] for s in step["skipped"]) == [
        "Goodreads author record blacklisted", "a row already holds this Goodreads book's id"]
    assert no_network == []


# ─── Preview → Apply (G209, C27) ─────────────────────────────


def _digest(tmp):
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(tmp.glob("*.db"))}


async def test_a_preview_changes_nothing_and_apply_does_what_it_said(env):
    from app.discovery import missing_cleanup
    mine = await _author("Susan Dennard")
    await _allow("Susan Dennard")
    trained = await _author("Rachel Caine")
    sid = await _q("INSERT INTO series (name, author_id) VALUES ('The Witchlands', ?)", (mine,))
    await _book("Truthwitch", [mine], owned=1, calibre_id=81, series_id=sid, index=1.0)
    copy = await _book("Truthwitch: The Witchlands", [mine], series_id=sid)
    gone = await _book("Morganville 3", [trained])
    before = _digest(env["tmp"])
    p = await missing_cleanup.preview()
    assert _digest(env["tmp"]) == before
    assert await _exists(copy) and await _exists(gone)
    a = await missing_cleanup.apply(p["id"])
    assert [(s["key"], s["count"]) for s in a["steps"]] == [(s["key"], s["count"]) for s in p["steps"]]
    assert not await _exists(copy) and not await _exists(gone)


async def test_apply_needs_the_last_preview(env):
    from app.discovery import missing_cleanup
    with pytest.raises(missing_cleanup.PreviewNeeded):
        await missing_cleanup.apply("nope")
    p = await missing_cleanup.preview()
    p["finished_at"] -= missing_cleanup.PREVIEW_TTL_S + 1
    with pytest.raises(missing_cleanup.PreviewNeeded):
        await missing_cleanup.apply(p["id"])
    # One preview, one Apply: after it the library isn't what it described.
    p = await missing_cleanup.preview()
    await missing_cleanup.apply(p["id"])
    with pytest.raises(missing_cleanup.PreviewNeeded):
        await missing_cleanup.apply(p["id"])


async def test_the_status_says_where_a_run_is(env, monkeypatch):
    from app.discovery import missing_cleanup
    seen = []

    async def look(lib, report):
        st = missing_cleanup.status()
        seen.append(tuple(st[k] for k in ("running", "mode", "library_name", "step", "step_name")))
    funcs = list(missing_cleanup._STEP_FUNCS)
    funcs[2] = look
    monkeypatch.setattr(missing_cleanup, "_STEP_FUNCS", tuple(funcs))
    await missing_cleanup.run("preview")
    assert seen == [
        (True, "preview", "Books", "filtered", "Comics, box sets, anthologies and other non-books"),
        (True, "preview", "Audio", "filtered", "Comics, box sets, anthologies and other non-books"),
    ]
    st = missing_cleanup.status()
    assert (st["running"], st["library_name"], st["step_name"]) == (False, "", "")


async def test_the_copies_are_this_tasks_alone(tmp_path):
    """C27: inside `using_copies` this task and the tasks it starts open the
    copy; a task started elsewhere keeps the real library."""
    from app.discovery import database as disco_db
    real = disco_db.get_db_path("test")
    outside = asyncio.Event()

    async def elsewhere():
        await outside.wait()
        return disco_db.get_db_path("test")

    other = asyncio.create_task(elsewhere())
    with disco_db.using_copies({"test": tmp_path / "copy.db"}, active="test"):
        assert disco_db.get_db_path("test") == tmp_path / "copy.db"
        assert disco_db.get_active_library() == "test"
        child = asyncio.create_task(asyncio.sleep(0, result=disco_db.get_db_path("test")))
        assert await child == tmp_path / "copy.db"
        outside.set()
        assert await asyncio.wait_for(other, 5) == real
    assert disco_db.get_db_path("test") == real


async def test_the_routes_preview_then_apply(env, monkeypatch):
    from fastapi import HTTPException
    from app.discovery import missing_cleanup
    from app.discovery.routers import missing_cleanup as routes
    trained = await _author("Rachel Caine")
    gone = await _book("Morganville 3", [trained])

    async def _wait():
        await asyncio.wait_for(state._missing_cleanup_task, 10)

    with pytest.raises(HTTPException) as e:
        await routes.start_apply(preview_id="x")
    assert e.value.status_code == 409
    assert (await routes.start_preview())["status"] == "started"
    await _wait()
    status = await routes.cleanup_status()
    assert (status["running"], status["preview_ttl_s"]) == (False, missing_cleanup.PREVIEW_TTL_S)
    assert status["last_preview"]["steps"][0]["count"] == 1
    assert status["last_preview"]["steps"][0]["sample"][0]["title"] == "Morganville 3"
    assert await _exists(gone)
    assert (await routes.start_apply(preview_id=status["last_preview"]["id"]))["status"] == "started"
    await _wait()
    assert not await _exists(gone)
    assert (await routes.cleanup_report(mode="apply"))["steps"][0]["count"] == 1
    with pytest.raises(HTTPException) as e:
        await routes.start_apply(preview_id=status["last_preview"]["id"])
    assert e.value.detail == "That preview was applied: run a new one before applying again."


async def test_a_scan_or_sync_in_progress_holds_it(env, monkeypatch):
    from fastapi import HTTPException
    from app.discovery.routers import missing_cleanup as routes
    monkeypatch.setattr(state, "_library_sync_in_progress", True)
    before = state._missing_cleanup_task
    with pytest.raises(HTTPException) as e:
        await routes.start_preview()
    assert (e.value.status_code, e.value.detail) == (409, "a library sync is running")
    assert state._missing_cleanup_task is before


async def test_the_goodreads_worker_waits_for_it(env, monkeypatch):
    from app.discovery import goodreads_candidates
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    monkeypatch.setattr(state, "_missing_cleanup_task", task)
    try:
        assert goodreads_candidates.merge_blocked_by() == "the missing-books cleanup"
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    assert goodreads_candidates.merge_blocked_by() is None


async def test_the_scheduled_library_sync_waits_for_it(env, monkeypatch, caplog):
    """A sync's own folds and flips in the middle of the cleanup's would race it."""
    from app.discovery import missing_cleanup, scheduled_jobs
    monkeypatch.setattr(missing_cleanup, "is_running", lambda: True)

    def _no_app(*_a, **_k):
        raise AssertionError("a library was synced while the cleanup ran")
    monkeypatch.setattr(scheduled_jobs, "get_app", _no_app)
    with caplog.at_level("INFO", logger="seshat.discovery.scheduled"):
        await scheduled_jobs.sync_all_libraries()
    assert "the missing-books cleanup is running" in caplog.text
    assert state._library_sync_in_progress is False


async def test_an_owned_book_is_never_removed(env):
    """Hard rule: an owned row with no Calibre item that only OpenLibrary
    knows (a flip of an OpenLibrary row) and no author you scan still stays."""
    stranger = await _author("Somebody Else")
    bid = await _book("Kept Forever", [stranger], owned=1, source="openlibrary")
    await _apply()
    assert await _exists(bid)


async def test_a_row_you_hid_is_never_given_a_family(env):
    mine = await _author("Holly Black")
    await _allow("Holly Black")
    bid = await _book("Lucifer (2015-2017) #1", [mine], hidden=1)
    await _apply()
    assert await _q("SELECT hidden, hidden_reason FROM books WHERE id = ?", (bid,)) == [(1, None)]


async def test_a_copy_matching_two_owned_books_folds_into_neither(env):
    mine = await _author("Susan Dennard")
    await _allow("Susan Dennard")
    sid = await _q("INSERT INTO series (name, author_id) VALUES ('The Witchlands', ?)", (mine,))
    await _book("Truthwitch", [mine], owned=1, calibre_id=81, series_id=sid, index=1.0)
    await _book("Truthwitch", [mine], owned=1, calibre_id=82, series_id=sid, index=1.0)
    copy = await _book("Truthwitch: The Witchlands", [mine], series_id=sid)
    _, report = await _apply()
    assert await _exists(copy)
    step = next(s for s in report["steps"] if s["key"] == "owned_copy")
    assert [r["id"] for r in step["skipped"]] == [copy]


async def test_two_audiobookshelf_items_of_one_book_never_merge(env):
    mine = await _author("Shirtaloon", slug="audio")
    await _allow("Shirtaloon")
    a = await _book("He Who Fights With Monsters 3", [mine], slug="audio", owned=1, abs_id="a3")
    b = await _book("He Who Fights With Monsters, Book 3: Hero", [mine], slug="audio",
                    owned=1, abs_id="a3-dup")
    await _apply()
    assert await _exists(a, "audio") and await _exists(b, "audio")
