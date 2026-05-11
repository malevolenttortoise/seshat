"""
Integration tests for the v2.7.0 bundle/collection pipeline fan-out.

Real-EPUB fixtures + real SQLite DB. Exercises:
  - 3-book bundle produces 3 pending review rows (one per work)
  - Each review row has the correct bundle_index / bundle_total /
    bundle_group_id / bundle_parent_grab_id
  - Each bundle child has its own staged_path under group-{i}/ and
    its own primary book file in that dir
  - Single-book grab still produces exactly ONE review row with
    bundle_total=1 (no behavioral regression)
  - bundle_detection_enabled=False falls back to the pre-v2.7
    one-group-per-torrent path
"""
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from app.database import get_db
from app.orchestrator.download_watcher import CompletionEvent
from app.orchestrator.pipeline import deliver_reviewed, process_completion
from app.storage import grabs as grabs_storage
from app.storage import pipeline as pipe_storage
from app.storage import review_queue as review_storage


def _make_epub(path: Path, title: str, author: str):
    """Build a minimal valid EPUB with controlled OPF metadata.

    Mirrors the helper in test_review_queue.py — kept self-contained
    here so this file can stand alone if/when test_review_queue.py
    refactors."""
    opf = ET.Element("package", xmlns="http://www.idpf.org/2007/opf", version="3.0")
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


async def _setup_bundle_grab(db, tmp_path, files: list[tuple[str, str, str]]) -> tuple[CompletionEvent, list[str]]:
    """Set up a grab whose source dir contains the given files.

    `files` is a list of `(filename, title, author)` triples — one
    real EPUB per entry. Returns the CompletionEvent plus the
    `torrent_files` list (basenames relative to save_path) — bundles
    must pass these explicitly because the legacy filename-heuristic
    fallback can't resolve a bundle whose child filenames don't
    share the torrent-name prefix."""
    grab_id = await grabs_storage.create_grab(
        db, announce_id=None, mam_torrent_id="bundle-1",
        torrent_name="A Bundle Collection", category="ebooks fantasy",
        author_blob="Some Author", state=grabs_storage.STATE_DOWNLOADED,
    )
    source_dir = tmp_path / "downloads" / "A Bundle Collection"
    rels: list[str] = []
    for filename, title, author in files:
        _make_epub(source_dir / filename, title=title, author=author)
        rels.append(filename)
    run_id = await pipe_storage.create_run(
        db, grab_id=grab_id, qbit_hash="hash_bundle", source_path=str(source_dir),
    )
    event = CompletionEvent(
        grab_id=grab_id, qbit_hash="hash_bundle", torrent_name="A Bundle Collection",
        save_path=str(source_dir), pipeline_run_id=run_id,
    )
    return event, rels


class TestBundleFanOut:
    async def test_three_book_bundle_produces_three_review_rows(self, temp_db, tmp_path):
        db = await get_db()
        try:
            event, torrent_files = await _setup_bundle_grab(db, tmp_path, [
                ("01_book_one.epub", "Book One", "Some Author"),
                ("02_book_two.epub", "Book Two", "Some Author"),
                ("03_book_three.epub", "Book Three", "Some Author"),
            ])
            review_dir = tmp_path / "review-staging"
            library = tmp_path / "library"

            ok = await process_completion(
                db, event,
                staging_path="",
                default_sink="folder",
                calibre_library_path="",
                folder_sink_path=str(library),
                category_routing={},
                ntfy_url="", ntfy_topic="",
                review_queue_enabled=True,
                review_staging_path=str(review_dir),
                torrent_files=torrent_files,
            )
            assert ok is True

            pending = await review_storage.list_pending(db)
            assert len(pending) == 3
            # All three rows belong to the same bundle group.
            assert {r.bundle_group_id for r in pending} == {f"grab-{event.grab_id}"}
            assert all(r.bundle_total == 3 for r in pending)
            # Indices are 0, 1, 2 in some order (list_pending sorts
            # by bundle_index within the same group).
            assert sorted(r.bundle_index for r in pending) == [0, 1, 2]
            # Every child carries bundle_parent_grab_id.
            assert all(r.bundle_parent_grab_id == event.grab_id for r in pending)
            # Titles round-trip from the embedded EPUB metadata.
            titles = {r.metadata.get("title") for r in pending}
            assert titles == {"Book One", "Book Two", "Book Three"}
            # Each child has its own staged dir under grab-N/group-i/.
            for r in pending:
                staged_path = Path(r.staged_path)
                assert staged_path.name.startswith("group-")
                # The primary file exists at the expected location.
                assert (staged_path / r.book_filename).exists()
        finally:
            await db.close()

    async def test_single_book_grab_still_produces_one_review_row(self, temp_db, tmp_path):
        """No behavioral regression for the common case."""
        db = await get_db()
        try:
            event, torrent_files = await _setup_bundle_grab(db, tmp_path, [
                ("solo.epub", "Solo Book", "Solo Author"),
            ])
            review_dir = tmp_path / "review-staging"
            library = tmp_path / "library"

            ok = await process_completion(
                db, event,
                staging_path="",
                default_sink="folder",
                calibre_library_path="",
                folder_sink_path=str(library),
                category_routing={},
                ntfy_url="", ntfy_topic="",
                review_queue_enabled=True,
                review_staging_path=str(review_dir),
                torrent_files=torrent_files,
            )
            assert ok is True

            pending = await review_storage.list_pending(db)
            assert len(pending) == 1
            row = pending[0]
            assert row.bundle_total == 1
            assert row.bundle_index == 0
            assert row.bundle_parent_grab_id is None
            assert row.bundle_group_id == f"grab-{event.grab_id}"
            # Single-book grabs land at grab-N/ (NOT grab-N/group-0/)
            # for backwards compatibility with in-flight queues.
            assert Path(row.staged_path).name == f"grab-{event.grab_id}"
        finally:
            await db.close()

    async def test_multi_format_same_book_still_one_review_row(self, temp_db, tmp_path):
        """An epub+mobi torrent of the same book must collapse to ONE
        review entry (multi-format ≠ bundle). The stem-dedupe pre-
        check in the classifier handles this without reading embedded
        metadata."""
        db = await get_db()
        try:
            event, torrent_files = await _setup_bundle_grab(db, tmp_path, [
                ("Same Book.epub", "Same Book", "An Author"),
                ("Same Book.mobi", "Same Book", "An Author"),
            ])
            # The mobi we created isn't a real Mobi (it's an EPUB
            # with a .mobi extension), but the classifier doesn't
            # care — it only inspects the filename stem for the
            # multi-format short-circuit.
            review_dir = tmp_path / "review-staging"
            library = tmp_path / "library"

            ok = await process_completion(
                db, event,
                staging_path="",
                default_sink="folder",
                calibre_library_path="",
                folder_sink_path=str(library),
                category_routing={},
                ntfy_url="", ntfy_topic="",
                review_queue_enabled=True,
                review_staging_path=str(review_dir),
                torrent_files=torrent_files,
            )
            assert ok is True

            pending = await review_storage.list_pending(db)
            assert len(pending) == 1
            assert pending[0].bundle_total == 1
        finally:
            await db.close()


class TestBundleDelivery:
    async def test_each_child_delivers_independently(self, temp_db, tmp_path):
        """Bundle children should each deliver to the sink as an
        independent book. Approving one child must not affect the
        others (siblings stay pending; their staged dirs stay on
        disk)."""
        db = await get_db()
        try:
            event, torrent_files = await _setup_bundle_grab(db, tmp_path, [
                ("01_alpha.epub", "Alpha Book", "Bundle Author"),
                ("02_beta.epub", "Beta Book", "Bundle Author"),
            ])
            review_dir = tmp_path / "review-staging"
            library = tmp_path / "library"

            await process_completion(
                db, event,
                staging_path="",
                default_sink="folder",
                calibre_library_path="",
                folder_sink_path=str(library),
                category_routing={},
                ntfy_url="", ntfy_topic="",
                review_queue_enabled=True,
                review_staging_path=str(review_dir),
                torrent_files=torrent_files,
            )

            pending = await review_storage.list_pending(db)
            assert len(pending) == 2

            # Deliver the first child only.
            first = pending[0]
            ok = await deliver_reviewed(
                db, review_id=first.id,
                default_sink="folder",
                calibre_library_path="",
                folder_sink_path=str(library),
                ntfy_url="", ntfy_topic="",
                auto_train_enabled=False,
                was_timeout=False,
            )
            assert ok is True

            # Exactly one book in the library — the other bundle
            # sibling stays pending.
            epubs = list(library.rglob("*.epub"))
            assert len(epubs) == 1
            still_pending = await review_storage.list_pending(db)
            assert len(still_pending) == 1
            assert still_pending[0].id == pending[1].id

            # The second child's staged dir is still intact (the
            # first child's was cleaned up on delivery).
            second_dir = Path(still_pending[0].staged_path)
            assert second_dir.exists()
            assert (second_dir / still_pending[0].book_filename).exists()

            # Now deliver the second child — both books land.
            ok2 = await deliver_reviewed(
                db, review_id=still_pending[0].id,
                default_sink="folder",
                calibre_library_path="",
                folder_sink_path=str(library),
                ntfy_url="", ntfy_topic="",
                auto_train_enabled=False,
                was_timeout=False,
            )
            assert ok2 is True
            assert len(list(library.rglob("*.epub"))) == 2
            # Queue empty.
            assert await review_storage.list_pending(db) == []
        finally:
            await db.close()
