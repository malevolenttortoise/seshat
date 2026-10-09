"""
Unit tests for the epub metadata writer.
"""
import stat
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from app.metadata.extract import extract
from app.metadata.writer import patch_epub_metadata


def _make_epub(path: Path, title: str = "Original Title", author: str = "Original Author"):
    """Create a minimal valid EPUB for testing."""
    opf = ET.Element("package", xmlns="http://www.idpf.org/2007/opf", version="3.0")
    md = ET.SubElement(opf, "metadata")
    md.set("xmlns:dc", "http://purl.org/dc/elements/1.1/")
    md.set("xmlns:opf", "http://www.idpf.org/2007/opf")

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


class TestPatchEpubMetadata:
    def test_keeps_the_file_mode(self, tmp_path):
        # The rebuilt zip is a NamedTemporaryFile (0600). Moved over the
        # epub as is, a root-owned patched copy was unreadable to CWA, which
        # ingests as a non-root user since v4.0.8 (2026-10-09).
        epub = tmp_path / "book.epub"
        _make_epub(epub, title="Bad Title")
        epub.chmod(0o664)

        assert patch_epub_metadata(epub, title="Good Title") is True

        assert stat.S_IMODE(epub.stat().st_mode) == 0o664
        assert extract(epub).title == "Good Title"

    def test_patches_title(self, tmp_path):
        epub = tmp_path / "book.epub"
        _make_epub(epub, title="Bad Title")

        result = patch_epub_metadata(epub, title="Good Title")

        assert result is True
        meta = extract(epub)
        assert meta.title == "Good Title"

    def test_patches_authors(self, tmp_path):
        epub = tmp_path / "book.epub"
        _make_epub(epub, author="Press, LitForge")

        result = patch_epub_metadata(epub, authors=["Cassius Lange", "Bram Kingsley"])

        assert result is True
        meta = extract(epub)
        assert meta.author == "Cassius Lange"  # extract reads first dc:creator

    def test_patches_series(self, tmp_path):
        epub = tmp_path / "book.epub"
        _make_epub(epub)

        patch_epub_metadata(epub, series="Tapped", series_index="1")

        meta = extract(epub)
        assert meta.series == "Tapped"
        assert meta.series_index == "1"

    def test_preserves_other_content(self, tmp_path):
        epub = tmp_path / "book.epub"
        _make_epub(epub, title="Original")

        patch_epub_metadata(epub, title="Patched")

        # Verify the chapter content is still there.
        with zipfile.ZipFile(str(epub), "r") as zf:
            content = zf.read("chapter1.xhtml")
            assert b"Content" in content

    def test_only_patches_specified_fields(self, tmp_path):
        epub = tmp_path / "book.epub"
        _make_epub(epub, title="Keep This", author="Keep Author")

        # Only patch title, leave author alone.
        patch_epub_metadata(epub, title="New Title")

        meta = extract(epub)
        assert meta.title == "New Title"
        assert meta.author == "Keep Author"

    def test_nonexistent_file_returns_false(self, tmp_path):
        assert patch_epub_metadata(tmp_path / "nope.epub", title="X") is False

    def test_non_epub_returns_false(self, tmp_path):
        txt = tmp_path / "book.txt"
        txt.write_text("not an epub")
        assert patch_epub_metadata(txt, title="X") is False

    def test_no_changes_returns_true(self, tmp_path):
        epub = tmp_path / "book.epub"
        _make_epub(epub, title="Same")

        result = patch_epub_metadata(epub, title="Same")
        assert result is True
