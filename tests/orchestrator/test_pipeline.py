"""
Integration tests for the pipeline orchestrator.

Exercises the full pipeline in both modes:
  - Direct mode (no staging): find book in download dir → metadata → sink
  - Staging mode: copy to staging → metadata → sink

Uses tmp_path for real file I/O and temp_db for database state.
"""
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from app.database import get_db
from app.orchestrator.download_watcher import CompletionEvent
from app.orchestrator.pipeline import process_completion
from app.storage import grabs as grabs_storage
from app.storage import pipeline as pipe_storage


def _make_epub(path: Path, title: str = "Test Book", author: str = "Test Author"):
    """Create a minimal valid EPUB at the given path."""
    opf = ET.Element(
        "package",
        xmlns="http://www.idpf.org/2007/opf",
        version="3.0",
    )
    md = ET.SubElement(opf, "metadata")
    md.set("xmlns:dc", "http://purl.org/dc/elements/1.1/")

    dc_title = ET.SubElement(md, "dc:title")
    dc_title.text = title
    dc_creator = ET.SubElement(md, "dc:creator")
    dc_creator.text = author

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


async def _setup_grab_and_event(db, tmp_path) -> CompletionEvent:
    """Create a grab + pipeline run + epub file, return a CompletionEvent."""
    grab_id = await grabs_storage.create_grab(
        db,
        announce_id=None,
        mam_torrent_id="12345",
        torrent_name="Test Book",
        category="ebooks fantasy",
        author_blob="Test Author",
        state=grabs_storage.STATE_DOWNLOADED,
    )

    source_dir = tmp_path / "downloads" / "Test Book"
    _make_epub(source_dir / "Test Book.epub", "Test Book", "Test Author")

    run_id = await pipe_storage.create_run(
        db, grab_id=grab_id, qbit_hash="hash_abc", source_path=str(source_dir)
    )

    return CompletionEvent(
        grab_id=grab_id,
        qbit_hash="hash_abc",
        torrent_name="Test Book",
        save_path=str(source_dir),
        pipeline_run_id=run_id,
    )


class TestDirectMode:
    """Pipeline with no staging — delivers directly from download dir."""

    async def test_direct_to_folder_sink(self, temp_db, tmp_path):
        db = await get_db()
        try:
            event = await _setup_grab_and_event(db, tmp_path)
            library = tmp_path / "library"

            result = await process_completion(
                db, event,
                staging_path="",  # no staging = direct mode
                default_sink="folder",
                calibre_library_path="",
                folder_sink_path=str(library),
                category_routing={},
                ntfy_url="",
                ntfy_topic="",
            )

            assert result is True

            run = await pipe_storage.get_run(db, event.pipeline_run_id)
            assert run.state == pipe_storage.PIPE_COMPLETE
            assert run.book_format == "epub"
            assert run.metadata_title == "Test Book"
            assert run.metadata_author == "Test Author"
            assert run.sink_name == "folder"

            # Verify book delivered to library.
            epub_files = list(library.rglob("*.epub"))
            assert len(epub_files) == 1

            # Verify grab is complete.
            grab = await grabs_storage.get_grab(db, event.grab_id)
            assert grab.state == grabs_storage.STATE_COMPLETE

            # Verify auto-train.
            cursor = await db.execute(
                "SELECT normalized FROM authors_allowed"
            )
            row = await cursor.fetchone()
            assert row["normalized"] == "test author"
        finally:
            await db.close()

    async def test_original_file_preserved(self, temp_db, tmp_path):
        """Direct mode delivers from download dir — original must survive for seeding."""
        db = await get_db()
        try:
            event = await _setup_grab_and_event(db, tmp_path)
            library = tmp_path / "library"

            await process_completion(
                db, event,
                staging_path="",
                default_sink="folder",
                calibre_library_path="",
                folder_sink_path=str(library),
                category_routing={},
                ntfy_url="",
                ntfy_topic="",
            )

            # Original file in download dir must still exist.
            original = tmp_path / "downloads" / "Test Book" / "Test Book.epub"
            assert original.exists()
        finally:
            await db.close()


class TestStagingMode:
    """Pipeline with staging — copies to staging first, then delivers."""

    async def test_staging_then_folder_sink(self, temp_db, tmp_path):
        db = await get_db()
        try:
            event = await _setup_grab_and_event(db, tmp_path)
            staging = tmp_path / "staging"
            library = tmp_path / "library"

            result = await process_completion(
                db, event,
                staging_path=str(staging),
                default_sink="folder",
                calibre_library_path="",
                folder_sink_path=str(library),
                category_routing={},
                ntfy_url="",
                ntfy_topic="",
            )

            assert result is True

            run = await pipe_storage.get_run(db, event.pipeline_run_id)
            assert run.state == pipe_storage.PIPE_COMPLETE
            # Staged path should be under the staging directory.
            assert str(staging) in run.staged_path
        finally:
            await db.close()


class TestPipelineErrors:
    async def test_no_book_files_fails(self, temp_db, tmp_path):
        db = await get_db()
        try:
            grab_id = await grabs_storage.create_grab(
                db,
                announce_id=None,
                mam_torrent_id="99999",
                torrent_name="Empty Torrent",
                category="ebooks fantasy",
                author_blob="Nobody",
                state=grabs_storage.STATE_DOWNLOADED,
            )
            # Create source dir with only a .nfo file (no book files).
            source = tmp_path / "downloads" / "Empty Torrent"
            source.mkdir(parents=True)
            (source / "info.nfo").write_text("no books here")

            run_id = await pipe_storage.create_run(
                db, grab_id=grab_id, source_path=str(source)
            )

            event = CompletionEvent(
                grab_id=grab_id,
                qbit_hash="hash_empty",
                torrent_name="Empty Torrent",
                save_path=str(source),
                pipeline_run_id=run_id,
            )

            result = await process_completion(
                db, event,
                staging_path="",
                default_sink="folder",
                calibre_library_path="",
                folder_sink_path=str(tmp_path / "lib"),
                category_routing={},
                ntfy_url="",
                ntfy_topic="",
            )

            assert result is False
            run = await pipe_storage.get_run(db, run_id)
            assert run.state == pipe_storage.PIPE_FAILED
            assert "no file matching" in run.error or "no book files" in run.error
        finally:
            await db.close()

    async def test_auto_train_disabled(self, temp_db, tmp_path):
        db = await get_db()
        try:
            event = await _setup_grab_and_event(db, tmp_path)
            library = tmp_path / "library"

            await process_completion(
                db, event,
                staging_path="",
                default_sink="folder",
                calibre_library_path="",
                folder_sink_path=str(library),
                category_routing={},
                ntfy_url="",
                ntfy_topic="",
                auto_train_enabled=False,
            )

            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM authors_allowed"
            )
            row = await cursor.fetchone()
            assert row["cnt"] == 0
        finally:
            await db.close()


class TestAudiobookRouting:
    """Phase 6: audiobook format detection + sink routing."""

    def test_is_audiobook_grab_by_extension(self):
        from app.orchestrator.pipeline import _is_audiobook_grab
        assert _is_audiobook_grab("m4b", "")
        assert _is_audiobook_grab("mp3", "")
        assert _is_audiobook_grab("m4a", "")
        assert _is_audiobook_grab(".m4b", "")  # tolerates leading dot
        assert not _is_audiobook_grab("epub", "")
        assert not _is_audiobook_grab("", "")

    def test_is_audiobook_grab_by_category(self):
        from app.orchestrator.pipeline import _is_audiobook_grab
        assert _is_audiobook_grab("", "AudioBooks - Fantasy")
        assert _is_audiobook_grab("", "audiobooks - science fiction")
        assert not _is_audiobook_grab("", "Ebooks - Fantasy")
        assert not _is_audiobook_grab("", "")

    def test_pick_sink_routes_audiobook_to_abs_when_configured(self):
        from app.orchestrator.pipeline import _pick_sink
        from app.sinks.audiobookshelf import AudiobookshelfSink
        from app.sinks.calibre import CalibreSink

        # Audiobook grab + ABS configured → ABS sink, even with
        # default_sink="calibre".
        sink = _pick_sink(
            default_sink="calibre",
            calibre_library_path="/calibre",
            folder_sink_path="",
            audiobookshelf_library_path="/audiobooks",
            cwa_ingest_path="",
            book_format="m4b",
        )
        assert isinstance(sink, AudiobookshelfSink)

        # Audiobook grab but ABS NOT configured → falls back to default.
        sink = _pick_sink(
            default_sink="calibre",
            calibre_library_path="/calibre",
            folder_sink_path="",
            audiobookshelf_library_path="",
            cwa_ingest_path="",
            book_format="m4b",
        )
        assert isinstance(sink, CalibreSink)

        # Ebook grab + ABS configured → still Calibre (default_sink
        # respected; ABS routing is audiobook-only).
        sink = _pick_sink(
            default_sink="calibre",
            calibre_library_path="/calibre",
            folder_sink_path="",
            audiobookshelf_library_path="/audiobooks",
            cwa_ingest_path="",
            book_format="epub",
        )
        assert isinstance(sink, CalibreSink)


class TestAudiobookWithCompanionFile:
    """2026-10-12: three Stormlight audiobook torrents (m4b + illustrations
    PDF) had the PDF picked as the primary — the ebook-priority pass ranks
    every ebook file ahead of every audio file — so the PDF went to CWA and
    the m4b never reached Audiobookshelf. The grab's MAM category decides
    which kind leads."""

    async def _run(self, db, tmp_path, category: str):
        grab_id = await grabs_storage.create_grab(
            db,
            announce_id=None,
            mam_torrent_id="1275961",
            torrent_name="Rhythm of War",
            category=category,
            author_blob="Brandon Sanderson",
            state=grabs_storage.STATE_DOWNLOADED,
        )
        save_path = tmp_path / "downloads"
        folder = save_path / "Rhythm of War - Brandon Sanderson"
        folder.mkdir(parents=True)
        (folder / "Rhythm of War.m4b").write_bytes(b"\x00" * 5000)
        (folder / "Rhythm of War.pdf").write_bytes(b"%PDF-1.4" + b"\x00" * 100)
        run_id = await pipe_storage.create_run(
            db, grab_id=grab_id, qbit_hash="hash_row", source_path=str(save_path),
        )
        event = CompletionEvent(
            grab_id=grab_id, qbit_hash="hash_row", torrent_name="Rhythm of War",
            save_path=str(save_path), pipeline_run_id=run_id,
        )
        library = tmp_path / "library"
        abs_library = tmp_path / "abs"
        ok = await process_completion(
            db, event,
            staging_path=str(tmp_path / "staging"),
            default_sink="folder",
            calibre_library_path="",
            folder_sink_path=str(library),
            audiobookshelf_library_path=str(abs_library),
            category_routing={},
            ntfy_url="", ntfy_topic="",
            torrent_files=[
                "Rhythm of War - Brandon Sanderson/Rhythm of War.m4b",
                "Rhythm of War - Brandon Sanderson/Rhythm of War.pdf",
            ],
            audiobook_format_priority=["m4b", "m4a", "mp3"],
            ebook_format_priority=["epub", "azw3", "mobi", "pdf"],
        )
        assert ok is True
        run = await pipe_storage.get_run(db, run_id)
        return run, library, abs_library

    async def test_audiobook_grab_delivers_the_m4b_to_abs(self, temp_db, tmp_path):
        db = await get_db()
        try:
            run, library, abs_library = await self._run(
                db, tmp_path, "Audiobooks - Fantasy",
            )
            assert run.book_format == "m4b"
            assert run.book_filename == "Rhythm of War.m4b"
            assert run.sink_name == "audiobookshelf"
            assert [p.name for p in abs_library.rglob("*.m4b")] == ["Rhythm of War.m4b"]
            # The PDF goes nowhere (Mark, 2026-10-12): not into ABS, not to
            # the ebook sink.
            assert list(abs_library.rglob("*.pdf")) == []
            assert not library.exists() or list(library.rglob("*")) == []
        finally:
            await db.close()

    async def test_ebook_grab_still_leads_with_the_ebook(self, temp_db, tmp_path):
        db = await get_db()
        try:
            run, library, abs_library = await self._run(
                db, tmp_path, "Ebooks - Fantasy",
            )
            assert run.book_format == "pdf"
            assert run.sink_name == "folder"
            assert not abs_library.exists() or list(abs_library.rglob("*")) == []
        finally:
            await db.close()
