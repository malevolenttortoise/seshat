"""
Tests for the v2.8.0 reingest module — discover already-snatched
torrents on disk / in qBit and feed them to the pipeline without
re-snatching from MAM.

Coverage:
  - `_name_score` tiering (exact / prefix / substring / Jaccard)
  - `find_fs_candidates` with planted files + directories
  - `find_qbit_candidates` against a mock dispatcher.qbit
  - `find_candidates` combining qBit + fs and de-duping overlaps
  - `start_reingest` creates a `grabs` row with `is_reingest=1`,
    a `pipeline_run`, and invokes `process_completion` end-to-end
    landing the book in the review queue
"""
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.clients.base import TorrentInfo
from app.database import get_db
from app.orchestrator.reingest import (
    Candidate,
    _name_score,
    find_candidates,
    find_fs_candidates,
    find_qbit_candidates,
    start_reingest,
)
from app.storage import grabs as grabs_storage
from app.storage import review_queue as review_storage


# ─── Helpers ────────────────────────────────────────────────


def _make_epub(path: Path, title: str = "Test Book", author: str = "Test Author"):
    """Build a minimal valid EPUB so process_completion can extract metadata."""
    opf = ET.Element("package", xmlns="http://www.idpf.org/2007/opf", version="3.0")
    md = ET.SubElement(opf, "metadata")
    md.set("xmlns:dc", "http://purl.org/dc/elements/1.1/")
    ET.SubElement(md, "dc:title").text = title
    ET.SubElement(md, "dc:creator").text = author
    container = (
        '<?xml version="1.0"?>'
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
        '<rootfiles><rootfile full-path="content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(path), "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("content.opf", ET.tostring(opf, encoding="unicode", xml_declaration=True))
        zf.writestr("chapter1.xhtml", "<html><body>Content</body></html>")


def _make_fake_qbit(torrents: list[dict]):
    """Build a mock dispatcher.qbit with list_torrents + list_torrent_files.

    `torrents` is a list of dicts shaped like
    `{name, hash, save_path, files}` — `files` is the relative file
    path list `list_torrent_files` should return for that hash.
    """
    by_hash = {t["hash"]: t for t in torrents}

    def _to_info(t: dict) -> TorrentInfo:
        return TorrentInfo(
            hash=t["hash"], name=t["name"], category=t.get("category", ""),
            state=t.get("state", "uploading"),
            seeding_seconds=t.get("seeding_seconds", 0),
            save_path=t["save_path"],
            added_on=t.get("added_on", 0),
            progress=t.get("progress", 1.0),
            size=t.get("size", 0),
        )

    qbit = SimpleNamespace()
    qbit.list_torrents = AsyncMock(return_value=[_to_info(t) for t in torrents])
    qbit.list_torrent_files = AsyncMock(
        side_effect=lambda h: by_hash.get(h, {"files": []}).get("files", []),
    )
    return qbit


def _make_dispatcher(qbit, **kwargs):
    """Build a stub dispatcher carrying just the attributes
    `find_candidates` + `start_reingest` read."""
    return SimpleNamespace(
        qbit=qbit,
        qbit_path_prefix=kwargs.get("qbit_path_prefix", ""),
        local_path_prefix=kwargs.get("local_path_prefix", ""),
        default_sink=kwargs.get("default_sink", "folder"),
        calibre_library_path=kwargs.get("calibre_library_path", ""),
        folder_sink_path=kwargs.get("folder_sink_path", ""),
        audiobookshelf_library_path=kwargs.get("audiobookshelf_library_path", ""),
        abs_base_url=kwargs.get("abs_base_url", ""),
        abs_api_key=kwargs.get("abs_api_key", ""),
        abs_library_id=kwargs.get("abs_library_id", ""),
        cwa_ingest_path=kwargs.get("cwa_ingest_path", ""),
        category_routing=kwargs.get("category_routing", {}),
        ntfy_url=kwargs.get("ntfy_url", ""),
        ntfy_topic=kwargs.get("ntfy_topic", ""),
        auto_train_enabled=kwargs.get("auto_train_enabled", False),
        per_event_notifications=kwargs.get("per_event_notifications", False),
        metadata_enricher=kwargs.get("metadata_enricher", None),
        staging_path=kwargs.get("staging_path", ""),
    )


# ─── _name_score ────────────────────────────────────────────


class TestNameScore:
    def test_exact_match_top_tier(self):
        assert _name_score("The Final Empire", "The Final Empire") == 100

    def test_exact_match_case_insensitive(self):
        assert _name_score("THE FINAL EMPIRE", "the final empire") == 100

    def test_stem_match_ignores_extension(self):
        assert _name_score("Book.epub", "Book") == 100

    def test_prefix_match(self):
        assert _name_score("Book Title (2024)", "Book Title") == 80

    def test_substring_match(self):
        # Candidate is longer; the target is a substring of the candidate.
        assert _name_score(
            "Long Decorated Book Title Volume 2", "Book Title",
        ) == 60

    def test_jaccard_fallback(self):
        # Strings that share most tokens but neither is a prefix or
        # substring of the other — exercises the Jaccard fallback
        # tier specifically.
        #   a = {alpha, beta, gamma, omega}
        #   b = {alpha, beta, gamma, delta}
        # intersection 3 / union 5 = 0.6 → Jaccard tier (40).
        assert _name_score(
            "omega alpha gamma beta",
            "delta alpha beta gamma",
        ) == 40

    def test_no_match_returns_zero(self):
        assert _name_score("Foundation", "Mistborn Trilogy") == 0

    def test_empty_inputs_return_zero(self):
        assert _name_score("", "anything") == 0
        assert _name_score("anything", "") == 0

    def test_single_char_directory_does_not_match_via_substring(self):
        # v2.9.1 regression: a directory basename of just "2" used to
        # substring-match every target torrent name containing "2".
        # In Mark's library that pulled in a Warhammer Empire Army
        # bundle's `/collection/2/` files as a candidate for
        # "Ghost Academy 2: Fall Term". Guard rejects short
        # candidate names from the substring tier; the Jaccard
        # fallback then correctly drops the score to 0 (single
        # numeric token / no token overlap).
        assert _name_score("2", "Ghost Academy 2: Fall Term") == 0
        assert _name_score("3", "Ghost Academy 3: Winter Term") == 0

    def test_zero_padded_numeric_aligns_with_unpadded(self):
        # v2.9.1: MAM titles use plain numbers ("Ghost Academy 2");
        # on-disk filenames frequently use zero-padded indices
        # ("Ghost Academy 02"). Without normalization the tokens
        # "02" and "2" stayed distinct, dropping the Jaccard below
        # the 0.6 cutoff and breaking the legitimate match.
        score = _name_score(
            "D L Bacon - Ghost Academy  02 - Fall Term",
            "Ghost Academy 2: Fall Term",
        )
        # 5 shared tokens (ghost, academy, 2, fall, term) out of
        # 8 in the union (+d, +l, +bacon) → Jaccard 0.625 ≥ 0.6.
        assert score == 40

    def test_zero_pad_preserves_real_years_and_round_numbers(self):
        # Sanity: normalization must only strip leading zeros, not
        # collapse 2024 → 224 or 100 → 1.
        from app.orchestrator.reingest import _normalize_numeric_padding
        assert _normalize_numeric_padding("Book (2024)") == "Book (2024)"
        assert _normalize_numeric_padding("100") == "100"
        assert _normalize_numeric_padding("2024-02") == "2024-2"
        assert _normalize_numeric_padding("01") == "1"
        assert _normalize_numeric_padding("Vol 03") == "Vol 3"


# ─── find_fs_candidates ─────────────────────────────────────


class TestFsCandidates:
    def test_single_file_match(self, tmp_path):
        _make_epub(tmp_path / "downloads" / "A Tangle of Time.epub")
        cs = find_fs_candidates(
            str(tmp_path / "downloads"),
            mam_torrent_name="A Tangle of Time",
        )
        assert len(cs) == 1
        assert cs[0].source == "fs"
        assert cs[0].book_files == ["A Tangle of Time.epub"]
        assert cs[0].save_path == str(tmp_path / "downloads")

    def test_directory_match_with_multiple_book_files(self, tmp_path):
        torrent_dir = tmp_path / "downloads" / "A Book Bundle"
        _make_epub(torrent_dir / "book-one.epub", title="One", author="A")
        _make_epub(torrent_dir / "book-two.epub", title="Two", author="A")
        cs = find_fs_candidates(
            str(tmp_path / "downloads"),
            mam_torrent_name="A Book Bundle",
        )
        # The directory match should outscore (or de-dupe with) the
        # individual file matches. We expect ONE candidate covering
        # the directory, and its book_files should hold both files.
        dir_matches = [c for c in cs if c.save_path == str(torrent_dir)]
        assert len(dir_matches) == 1
        assert set(dir_matches[0].book_files) == {"book-one.epub", "book-two.epub"}

    def test_no_match_empty_list(self, tmp_path):
        _make_epub(tmp_path / "downloads" / "Some Other Book.epub")
        cs = find_fs_candidates(
            str(tmp_path / "downloads"),
            mam_torrent_name="A Tangle of Time",
        )
        assert cs == []

    def test_missing_root_returns_empty(self, tmp_path):
        cs = find_fs_candidates(
            str(tmp_path / "does-not-exist"),
            mam_torrent_name="Anything",
        )
        assert cs == []

    def test_caps_at_five_candidates(self, tmp_path):
        # Plant 7 plausibly-matching files.
        for i in range(7):
            _make_epub(tmp_path / "downloads" / f"Tangle Variant {i}.epub")
        cs = find_fs_candidates(
            str(tmp_path / "downloads"),
            mam_torrent_name="Tangle Variant",
        )
        assert len(cs) <= 5

    def test_ghost_academy_regression_picks_correct_file(self, tmp_path):
        """v2.9.1 regression for the Ghost Academy reingest mismatch.

        Mark's downloads tree has:
          [mam-complete]/[2026-04]/D L Bacon - Ghost Academy  02 - Fall Term.epub
          [mam-complete]/[2024-02]/collection/2/Warhammer ... Iron Company.epub
          [mam-complete]/[2024-02]/collection/3/Warhammer ... Call to Arms.epub

        Before the fix, reingesting "Ghost Academy 2: Fall Term"
        substring-matched the `2/` directory ("2" is a substring of
        the target), pulled in the Iron Company files as a score-60
        candidate, and beat the legitimate Bacon file (which scored
        0 because the "02" token didn't match "2" and the
        "D L Bacon -" prefix diluted Jaccard below 0.6).

        After: the dir `2` is rejected (length-1 candidate), and
        the Bacon file matches via Jaccard with normalized numeric
        tokens.
        """
        root = tmp_path / "downloads" / "[mam-complete]"
        _make_epub(
            root / "[2026-04]"
            / "D L Bacon - Ghost Academy  02 - Fall Term.epub",
            title="Ghost Academy 2", author="D L Bacon",
        )
        _make_epub(
            root / "[2024-02]" / "collection" / "2"
            / "Warhammer - [Empire Army 02] - Iron Company by Chris Wraight.epub",
            title="Iron Company", author="Chris Wraight",
        )
        _make_epub(
            root / "[2024-02]" / "collection" / "3"
            / "Warhammer - [Empire Army 03] - Call to Arms by Mitchel Scanlon.epub",
            title="Call to Arms", author="Mitchel Scanlon",
        )

        cs = find_fs_candidates(
            str(root), mam_torrent_name="Ghost Academy 2: Fall Term",
        )

        # The Bacon file must appear; the Warhammer Iron Company
        # directory must NOT (its parent dir basename "2" is the
        # false-positive that the v2.9.1 guard rejects).
        bacon_paths = [
            c for c in cs
            if any("Ghost Academy" in f for f in c.book_files)
        ]
        warhammer_paths = [
            c for c in cs
            if any("Iron Company" in f for f in c.book_files)
        ]
        assert len(bacon_paths) == 1, (
            f"expected the Bacon file as a candidate; got {cs}"
        )
        assert warhammer_paths == [], (
            f"Warhammer Iron Company must not be a candidate for "
            f"Ghost Academy 2; got {warhammer_paths}"
        )


# ─── find_qbit_candidates ───────────────────────────────────


class TestQbitCandidates:
    async def test_exact_name_match(self, tmp_path):
        # v2.8.1: qBit candidates now require their book files to
        # actually exist on disk under the (translated) save_path.
        # Plant a real file so the candidate survives validation.
        save_path = tmp_path / "downloads"
        _make_epub(save_path / "A Tangle of Time" / "book.epub")
        qbit = _make_fake_qbit([
            {
                "hash": "abc123",
                "name": "A Tangle of Time",
                "save_path": str(save_path),
                "files": ["A Tangle of Time/book.epub"],
                "size": 1024,
            },
        ])
        dispatcher = _make_dispatcher(qbit)
        cs = await find_qbit_candidates(
            dispatcher, mam_torrent_name="A Tangle of Time",
        )
        assert len(cs) == 1
        assert cs[0].source == "qbit"
        assert cs[0].qbit_hash == "abc123"
        assert cs[0].book_files == ["A Tangle of Time/book.epub"]

    async def test_path_translation(self, tmp_path):
        # Plant the file at the TRANSLATED path (where Seshat would
        # see it), since the existence check runs after translation.
        translated = tmp_path / "mnt-local" / "downloads"
        _make_epub(translated / "book.epub")
        qbit = _make_fake_qbit([
            {
                "hash": "abc123",
                "name": "Tangle",
                "save_path": str(tmp_path / "data" / "downloads"),
                "files": ["book.epub"],
            },
        ])
        dispatcher = _make_dispatcher(
            qbit,
            qbit_path_prefix=str(tmp_path / "data"),
            local_path_prefix=str(tmp_path / "mnt-local"),
        )
        cs = await find_qbit_candidates(
            dispatcher, mam_torrent_name="Tangle",
        )
        assert len(cs) == 1
        assert cs[0].save_path == str(translated)

    async def test_non_book_torrents_skipped(self):
        qbit = _make_fake_qbit([
            {
                "hash": "h1", "name": "A Tangle of Time",
                "save_path": "/data", "files": ["movie.mkv", "subs.srt"],
            },
        ])
        dispatcher = _make_dispatcher(qbit)
        cs = await find_qbit_candidates(
            dispatcher, mam_torrent_name="A Tangle of Time",
        )
        # Matched by name but contains no book files → filtered out.
        assert cs == []

    async def test_no_qbit_returns_empty(self):
        dispatcher = SimpleNamespace(qbit=None)
        cs = await find_qbit_candidates(
            dispatcher, mam_torrent_name="anything",
        )
        assert cs == []


# ─── find_candidates (combined) ─────────────────────────────


class TestCombinedFind:
    async def test_qbit_outranks_fs_at_equal_name_match(self, tmp_path, monkeypatch):
        # Plant an fs candidate AND a matching qBit torrent. The
        # qBit torrent's save_path is different so the v2.8.1
        # absolute-path dedup keeps both — we're testing the ranking
        # tiebreak here, not the dedup.
        _make_epub(tmp_path / "downloads" / "Same Book.epub")
        # v2.8.1: qBit candidate must point at a real file on disk.
        _make_epub(tmp_path / "qbit-data" / "Same Book.epub")
        qbit = _make_fake_qbit([
            {
                "hash": "h1", "name": "Same Book",
                "save_path": str(tmp_path / "qbit-data"),
                "files": ["Same Book.epub"],
            },
        ])
        dispatcher = _make_dispatcher(qbit)
        # Force load_settings to return our tmp download path.
        from app import config as config_module
        original = config_module.load_settings
        monkeypatch.setattr(config_module, "load_settings", lambda: {
            **original(),
            "qbit_download_path": str(tmp_path / "downloads"),
            "qbit_path_prefix": "",
            "local_path_prefix": "",
        })
        cs = await find_candidates(
            dispatcher, mam_torrent_name="Same Book",
        )
        # qBit candidate first.
        assert cs[0].source == "qbit"
        # fs candidate may or may not be present depending on the
        # dedupe rule — but the qBit candidate must outrank.


# ─── start_reingest (end-to-end pipeline) ───────────────────


class TestStartReingest:
    async def test_creates_grab_and_review_row(self, temp_db, tmp_path, monkeypatch):
        """Full reingest path: planted EPUB on disk → start_reingest
        creates a `grabs` row (is_reingest=1, state=downloaded) +
        `pipeline_run` + `book_review_queue` row."""
        downloads = tmp_path / "downloads" / "Found Book"
        _make_epub(downloads / "book.epub", title="Found Book", author="Author")

        candidate = Candidate(
            source="fs",
            display_path=str(downloads),
            save_path=str(downloads),
            book_files=["book.epub"],
            qbit_hash=None,
            mtime=0.0, total_size=0,
            score=100,
        )

        dispatcher = _make_dispatcher(
            qbit=None,
            folder_sink_path=str(tmp_path / "library"),
        )

        # Force settings to point review staging at a per-test dir
        # so the pipeline's _stage_for_review has somewhere to write.
        review_dir = tmp_path / "review-staging"
        from app import config as config_module
        original = config_module.load_settings
        monkeypatch.setattr(config_module, "load_settings", lambda: {
            **original(),
            "review_queue_enabled": True,
            "review_staging_path": str(review_dir),
        })

        db = await get_db()
        try:
            grab_id, run_id, ok = await start_reingest(
                db,
                dispatcher=dispatcher,
                mam_torrent_id="9999",
                mam_torrent_name="Found Book",
                category="ebooks fantasy",
                author_blob="Author",
                candidate=candidate,
            )
            assert grab_id > 0
            assert run_id > 0
            assert ok is True

            # Grabs row carries is_reingest=1. State is `processing`
            # by this point because _stage_for_review already advanced
            # it as part of the synthesized pipeline run; the initial
            # `downloaded` state was only momentarily visible during
            # start_reingest itself.
            row = await (await db.execute(
                "SELECT state, is_reingest, mam_torrent_id FROM grabs WHERE id = ?",
                (grab_id,),
            )).fetchone()
            assert row["state"] == grabs_storage.STATE_PROCESSING
            assert row["is_reingest"] == 1
            assert row["mam_torrent_id"] == "9999"

            # Review queue row was created via process_completion.
            pending = await review_storage.list_pending(db)
            assert len(pending) == 1
            assert pending[0].grab_id == grab_id
            assert pending[0].metadata.get("title") == "Found Book"
        finally:
            await db.close()

    async def test_qbit_candidate_records_hash(self, temp_db, tmp_path):
        """qBit candidates should carry their hash through to the
        grabs row so future link-back / status reconciliation can
        find the live torrent."""
        downloads = tmp_path / "downloads"
        _make_epub(downloads / "book.epub", title="Book", author="A")

        candidate = Candidate(
            source="qbit",
            display_path="qBit: Book → /downloads",
            save_path=str(downloads),
            book_files=["book.epub"],
            qbit_hash="aabbcc112233",
            mtime=0.0, total_size=0,
            score=200,
        )
        dispatcher = _make_dispatcher(
            qbit=None, folder_sink_path=str(tmp_path / "library"),
        )

        db = await get_db()
        try:
            grab_id, _, _ = await start_reingest(
                db, dispatcher=dispatcher,
                mam_torrent_id="1234", mam_torrent_name="Book",
                category="ebooks fantasy", author_blob="A",
                candidate=candidate,
            )
            row = await (await db.execute(
                "SELECT qbit_hash, is_reingest FROM grabs WHERE id = ?",
                (grab_id,),
            )).fetchone()
            assert row["qbit_hash"] == "aabbcc112233"
            assert row["is_reingest"] == 1
        finally:
            await db.close()


# ─── v2.8.1 regression tests ────────────────────────────────


class TestV281QbitFileExistenceValidation:
    """v2.8.1: qBit candidates whose book files don't actually exist
    on disk must get filtered out at probe time. Pre-v2.8.1 they
    would silently auto-start, create grab+pipeline_run rows, then
    fail deep inside process_completion with a misleading "success"
    toast already shown to the user."""

    async def test_candidate_with_missing_files_filtered(self, tmp_path):
        # qBit reports a torrent with a book file, but the file isn't
        # actually on disk under the (translated) save_path. Simulates
        # Mark's Test 8 scenario where the file was moved out while
        # qBit retained the torrent metadata.
        qbit = _make_fake_qbit([
            {
                "hash": "missing-files",
                "name": "A Temperamental Enchantress",
                "save_path": str(tmp_path / "downloads"),
                "files": ["A Temperamental Enchantress/book.epub"],
            },
        ])
        dispatcher = _make_dispatcher(qbit)
        # Note: no _make_epub call — the file does NOT exist.
        cs = await find_qbit_candidates(
            dispatcher,
            mam_torrent_name="A Temperamental Enchantress",
        )
        assert cs == []

    async def test_partial_existence_narrows_book_files(self, tmp_path):
        """When some files exist and others don't, the candidate is
        kept but its book_files list is narrowed to the surviving
        files. Pipeline gets to work with what's actually on disk."""
        save_path = tmp_path / "downloads"
        # Two files in the torrent metadata; only one actually exists.
        _make_epub(save_path / "Bundle" / "alive.epub")
        qbit = _make_fake_qbit([
            {
                "hash": "partial",
                "name": "Bundle",
                "save_path": str(save_path),
                "files": ["Bundle/alive.epub", "Bundle/missing.epub"],
            },
        ])
        dispatcher = _make_dispatcher(qbit)
        cs = await find_qbit_candidates(
            dispatcher, mam_torrent_name="Bundle",
        )
        assert len(cs) == 1
        assert cs[0].book_files == ["Bundle/alive.epub"]


class TestV281AbsolutePathDedup:
    """v2.8.1: qBit and fs candidates pointing at the same physical
    file should collapse to ONE entry in `find_candidates`, even when
    their `save_path` values differ (qBit reports the parent dir,
    fs walks the torrent's own dir). Pre-v2.8.1 the dedup keyed on
    raw save_path and let both pass through, producing the duplicate
    Mark saw in the v2.8.0 picker."""

    async def test_qbit_parent_plus_fs_subdir_collapses(self, tmp_path, monkeypatch):
        downloads = tmp_path / "downloads"
        torrent_dir = downloads / "The Same Book"
        _make_epub(torrent_dir / "book.epub", title="Same", author="A")

        # qBit sees the parent dir + torrent-relative path.
        qbit = _make_fake_qbit([
            {
                "hash": "h", "name": "The Same Book",
                "save_path": str(downloads),
                "files": ["The Same Book/book.epub"],
            },
        ])
        dispatcher = _make_dispatcher(qbit)

        # Force load_settings to point fs scan at the same downloads
        # dir so both resolvers find the file.
        from app import config as config_module
        original = config_module.load_settings
        monkeypatch.setattr(config_module, "load_settings", lambda: {
            **original(),
            "qbit_download_path": str(downloads),
            "qbit_path_prefix": "",
            "local_path_prefix": "",
        })

        cs = await find_candidates(
            dispatcher, mam_torrent_name="The Same Book",
        )
        # Exactly one candidate — the qBit one (outranks fs at equal
        # name-match, plus the fs dupe got filtered by the v2.8.1
        # absolute-path comparison).
        assert len(cs) == 1
        assert cs[0].source == "qbit"


class TestV281AutoStartFailurePropagation:
    """v2.8.1: when `process_completion` fails during the auto-start
    path (e.g. the candidate pointed at a file that's now missing
    between probe and start), `start_reingest` must return ok=False
    so the endpoint can surface a clear error instead of a misleading
    success toast."""

    async def test_missing_file_returns_ok_false(self, temp_db, tmp_path, monkeypatch):
        # Candidate says the file lives at this path, but the file
        # was never created → process_completion fails.
        downloads = tmp_path / "downloads" / "Ghost Book"
        downloads.mkdir(parents=True)
        # NO _make_epub — the candidate points at a nonexistent file.
        candidate = Candidate(
            source="fs",
            display_path=str(downloads),
            save_path=str(downloads),
            book_files=["ghost.epub"],
            qbit_hash=None,
            mtime=0.0, total_size=0,
            score=100,
        )

        dispatcher = _make_dispatcher(
            qbit=None, folder_sink_path=str(tmp_path / "library"),
        )

        # Configure review staging so the pipeline reaches the file-
        # location step (and fails there, since the file is missing).
        review_dir = tmp_path / "review-staging"
        from app import config as config_module
        original = config_module.load_settings
        monkeypatch.setattr(config_module, "load_settings", lambda: {
            **original(),
            "review_queue_enabled": True,
            "review_staging_path": str(review_dir),
        })

        db = await get_db()
        try:
            grab_id, run_id, ok = await start_reingest(
                db,
                dispatcher=dispatcher,
                mam_torrent_id="6666",
                mam_torrent_name="Ghost Book",
                category="ebooks fantasy",
                author_blob="A",
                candidate=candidate,
            )
            # Pipeline failed → ok=False so the endpoint can surface
            # the error to the user.
            assert ok is False
            # Grab + pipeline_run rows still exist as audit trail.
            assert grab_id > 0
            assert run_id > 0
            # No review queue entry was created.
            from app.storage import review_queue as review_storage
            pending = await review_storage.list_pending(db)
            assert pending == []
        finally:
            await db.close()
