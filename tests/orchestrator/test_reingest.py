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


# ─── find_qbit_candidates ───────────────────────────────────


class TestQbitCandidates:
    async def test_exact_name_match(self):
        qbit = _make_fake_qbit([
            {
                "hash": "abc123",
                "name": "A Tangle of Time",
                "save_path": "/data/downloads/[mam-complete]",
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

    async def test_path_translation(self):
        qbit = _make_fake_qbit([
            {
                "hash": "abc123",
                "name": "Tangle",
                "save_path": "/data/downloads/[mam-complete]",
                "files": ["book.epub"],
            },
        ])
        dispatcher = _make_dispatcher(
            qbit, qbit_path_prefix="/data", local_path_prefix="/mnt/local",
        )
        cs = await find_qbit_candidates(
            dispatcher, mam_torrent_name="Tangle",
        )
        assert len(cs) == 1
        assert cs[0].save_path == "/mnt/local/downloads/[mam-complete]"

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
        # Plant an fs candidate AND a matching qBit torrent.
        _make_epub(tmp_path / "downloads" / "Same Book.epub")
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
            grab_id, run_id = await start_reingest(
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
            grab_id, _ = await start_reingest(
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
