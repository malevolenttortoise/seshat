"""v3.10.0 — foreign-edition filtering + language persistence.

Two gaps found while auditing the live "missing books" list:

  1. `_RX_FOREIGN_UNICODE` covered Cyrillic / CJK / Arabic / Hangul but NOT
     Greek or Hebrew, so "DUNE: Οίκος των Ατρειδών" and "שישה עורבים"
     reached the missing list (13 rows on the reference install, from
     openlibrary + hardcover).
  2. Every source emits `BookResult.language`, but the discovery INSERT
     never persisted it — `books.language` was NULL on all 4,971 unowned
     rows, so nothing downstream could reason about language at all.
"""
from __future__ import annotations

import pytest

from app.discovery.lookup import _looks_foreign, _lang_ok


# ─── 1. non-Latin script detection ───────────────────────────


@pytest.mark.parametrize("title,script", [
    ("DUNE: Οίκος των Ατρειδών, Tόμος Β'", "Greek"),
    ("Dune: Ιστορίες της Αρρακήν", "Greek"),
    ("שישה עורבים", "Hebrew"),
    ("הגמביט האחרון", "Hebrew"),
    ("Пробуждение Левиафана", "Cyrillic"),
    ("三体", "CJK"),
    ("ハリー・ポッター", "Kana"),
    ("해리 포터", "Hangul"),
    ("كثيب", "Arabic"),
    ("दून", "Devanagari"),
    ("ดูน", "Thai"),
])
def test_non_latin_scripts_are_foreign(title, script):
    assert _looks_foreign(title) is True, f"{script} not detected"


@pytest.mark.parametrize("title", [
    "Dune",
    "The Way of Kings",
    "Ahead Full (The Kurtherian Gambit Book 19)",
    # ⚠️ regression guards for the Latin-script heuristics: these are
    # ENGLISH titles that a naive foreign-word rule would wrongly reject.
    "Kneel Or Die",
    "Press Die to Continue",
    "La Brea",
    "El Paso Days",
    "Die Trying",
])
def test_english_titles_are_not_foreign(title):
    assert _looks_foreign(title) is False


def test_diacritics_still_detected():
    """Pre-existing behavior must survive the regex rewrite."""
    assert _looks_foreign("Der Kön der Träume") is True
    assert _looks_foreign("L'Étranger") is True


def test_known_foreign_edition_markers_still_detected():
    for t in ("Coffret Dune", "Dune Gesamtausgabe", "Edizione Speciale",
              "Wydanie Rozszerzone"):
        assert _looks_foreign(t) is True


# ─── 2. _lang_ok semantics (unchanged, pinned) ───────────────


def test_lang_ok_is_permissive_on_unknown():
    """None language must PASS — we can't prove foreign, and rejecting
    unknowns would drop most of the catalogue."""
    assert _lang_ok(None, ["English"]) is True
    assert _lang_ok("", ["English"]) is True


def test_lang_ok_rejects_known_mismatch():
    assert _lang_ok("Greek", ["English"]) is False
    assert _lang_ok("German", ["English"]) is False
    assert _lang_ok("English", ["English"]) is True


# ─── 3. language persistence through the real merge ──────────


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    from app import config as app_config
    from app.discovery import database as disco_db
    from app.discovery import roster as roster_mod

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    roster_mod.invalidate()
    yield tmp_path
    roster_mod.invalidate()
    disco_db.set_active_library(None)


async def _mk_author(name="Frank Herbert"):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name) VALUES (?,?,?)",
            (name, name, name.lower()),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _lang_of(title):
    from app.discovery.database import get_db
    db = await get_db()
    try:
        r = await (await db.execute(
            "SELECT language FROM books WHERE title=?", (title,))).fetchone()
        return r[0] if r else "<missing>"
    finally:
        await db.close()


async def test_language_persisted_on_standalone_insert(discovery_db, monkeypatch):
    from app.discovery import roster as roster_mod
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult

    aid = await _mk_author()
    monkeypatch.setattr(roster_mod, "load_roster",
                        lambda *a, **k: _AllowAll())
    await _merge_result(
        author_id=aid,
        result=AuthorResult(name="x", external_id="e1", books=[
            BookResult(title="Dune", source="hardcover", language="English"),
        ]),
        source_name="hardcover", languages=["English"],
    )
    assert await _lang_of("Dune") == "English"


async def test_language_persisted_on_series_insert(discovery_db, monkeypatch):
    from app.discovery import roster as roster_mod
    from app.discovery.lookup import _merge_result
    from app.discovery.sources.base import AuthorResult, BookResult, SeriesResult

    aid = await _mk_author()
    monkeypatch.setattr(roster_mod, "load_roster", lambda *a, **k: _AllowAll())
    await _merge_result(
        author_id=aid,
        result=AuthorResult(name="x", external_id="e1", books=[], series=[
            SeriesResult(name="Dune Saga", books=[
                BookResult(title="Dune Messiah", source="hardcover",
                           language="English", series_index=2),
            ]),
        ]),
        source_name="hardcover", languages=["English"],
    )
    assert await _lang_of("Dune Messiah") == "English"


class _AllowAll:
    """Roster stub that admits everything, so these tests isolate the
    language behavior from ADR-0021's gate."""
    def may_mint(self, name): return True
    def admits(self, name, aid): return True
    def is_scan_eligible(self, name, aid): return True

    def __await__(self):
        async def _self(): return self
        return _self().__await__()


# ─── 4. OpenLibrary language resolution (v3.10.0) ────────────


def test_ol_extract_prefers_english_among_many():
    """OL lists EVERY language a work was published in. The Cruel Prince
    is ['eng','dut','heb'] — picking "the first code" would reject it
    whenever OL happened to order Dutch first."""
    from app.discovery.sources.openlibrary import _extract_language
    work = {"key": "/works/OL17850410W"}
    lang_map = {"OL17850410W": ["dut", "heb", "eng"]}
    assert _extract_language(work, lang_map) == "en"


def test_ol_extract_flags_translation_only_work():
    """El rey malvado is filed as its own work with ['spa'] and no English
    edition — that IS a translation and must be filterable."""
    from app.discovery.sources.openlibrary import _extract_language
    from app.discovery.lookup import _lang_ok
    work = {"key": "/works/OL24208644W"}
    lang = _extract_language(work, {"OL24208644W": ["spa"]})
    assert lang == "es"
    assert _lang_ok(lang, ["English"]) is False


def test_ol_extract_falls_back_to_bulk_map():
    """⚠️ The regression: /authors/{key}/works.json carries NO languages
    field (0 of 60 measured), so the bulk map is the ONLY source on the
    discovery path."""
    from app.discovery.sources.openlibrary import _extract_language
    work = {"key": "/works/OL1W"}          # no 'languages' key at all
    assert _extract_language(work, None) is None
    assert _extract_language(work, {"OL1W": ["ger"]}) == "de"


def test_ol_extract_still_reads_inline_languages():
    """The works.json shape must keep working where it IS present."""
    from app.discovery.sources.openlibrary import _extract_language
    work = {"key": "/works/OL2W", "languages": [{"key": "/languages/fre"}]}
    assert _extract_language(work) == "fr"


def test_ol_unknown_language_stays_permissive():
    """Unknown/unmappable must return None, which _lang_ok lets through —
    a language lookup failure must never silently drop a catalogue."""
    from app.discovery.sources.openlibrary import _extract_language
    from app.discovery.lookup import _lang_ok
    assert _extract_language({"key": "/works/OL3W"}, {"OL3W": ["xyz"]}) is None
    assert _lang_ok(None, ["English"]) is True


def test_english_code_passes_lang_ok():
    """Guard the substring semantics of _lang_ok against the 2-letter
    codes _OL_LANGUAGE_MAP emits."""
    from app.discovery.lookup import _lang_ok
    assert _lang_ok("en", ["English"]) is True
    for code in ("de", "es", "fr", "it", "nl", "pt", "ru", "ja", "zh", "pl"):
        assert _lang_ok(code, ["English"]) is False, code
