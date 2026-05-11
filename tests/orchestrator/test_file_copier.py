"""
Unit tests for the file copier.

Uses pytest's tmp_path fixture for real filesystem operations.
"""
from pathlib import Path

from app.orchestrator.file_copier import (
    BOOK_EXTENSIONS,
    CopyResult,
    copy_to_staging,
    find_book_files,
)


def _create_file(path: Path, size: int = 100) -> Path:
    """Create a file with the given size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)
    return path


class TestFindBookFiles:
    def test_finds_epub(self, tmp_path):
        _create_file(tmp_path / "book.epub")
        assert len(find_book_files(tmp_path)) == 1

    def test_finds_m4b(self, tmp_path):
        _create_file(tmp_path / "audiobook.m4b")
        assert len(find_book_files(tmp_path)) == 1

    def test_ignores_nfo(self, tmp_path):
        _create_file(tmp_path / "info.nfo")
        assert len(find_book_files(tmp_path)) == 0

    def test_ignores_jpg(self, tmp_path):
        _create_file(tmp_path / "cover.jpg")
        assert len(find_book_files(tmp_path)) == 0

    def test_sorted_by_size_descending(self, tmp_path):
        _create_file(tmp_path / "small.epub", size=50)
        _create_file(tmp_path / "large.epub", size=500)
        files = find_book_files(tmp_path)
        assert files[0].name == "large.epub"
        assert files[1].name == "small.epub"

    def test_recursive_search(self, tmp_path):
        _create_file(tmp_path / "subdir" / "nested.epub")
        files = find_book_files(tmp_path)
        assert len(files) == 1
        assert files[0].name == "nested.epub"

    def test_nonexistent_dir(self, tmp_path):
        assert find_book_files(tmp_path / "nope") == []

    def test_single_file_as_source(self, tmp_path):
        f = _create_file(tmp_path / "book.epub")
        files = find_book_files(f)
        assert len(files) == 1

    def test_single_non_book_file(self, tmp_path):
        f = _create_file(tmp_path / "readme.txt")
        files = find_book_files(f)
        assert len(files) == 0


class TestCopyToStaging:
    def test_copies_epub_to_staging(self, tmp_path):
        source = tmp_path / "downloads" / "My Book"
        staging = tmp_path / "staging"
        _create_file(source / "My Book.epub", size=200)

        result = copy_to_staging(source, staging, "My Book")

        assert result.success is True
        assert result.files_copied == 1
        assert result.book_format == "epub"
        assert result.book_filename == "My Book.epub"
        assert Path(result.staged_path).exists()
        assert (Path(result.staged_path) / "My Book.epub").exists()

    def test_copies_multiple_files(self, tmp_path):
        source = tmp_path / "downloads" / "Series Pack"
        staging = tmp_path / "staging"
        _create_file(source / "book1.epub", size=100)
        _create_file(source / "book2.epub", size=200)
        _create_file(source / "cover.jpg", size=50)  # ignored

        result = copy_to_staging(source, staging, "Series Pack")

        assert result.success is True
        assert result.files_copied == 2
        # Primary file should be the largest.
        assert result.book_filename == "book2.epub"

    def test_no_book_files_fails(self, tmp_path):
        source = tmp_path / "downloads" / "Empty"
        staging = tmp_path / "staging"
        _create_file(source / "readme.txt")

        result = copy_to_staging(source, staging, "Empty")

        assert result.success is False
        assert "no book files" in result.error

    def test_staging_not_configured(self, tmp_path):
        source = tmp_path / "downloads"
        result = copy_to_staging(source, Path(""), "Book")
        assert result.success is False
        assert "not configured" in result.error

    def test_creates_staging_subdir(self, tmp_path):
        source = tmp_path / "dl"
        staging = tmp_path / "staging"
        _create_file(source / "book.epub")

        copy_to_staging(source, staging, "My Great Book")

        assert (staging / "My Great Book").is_dir()

    def test_sanitizes_dirname(self, tmp_path):
        source = tmp_path / "dl"
        staging = tmp_path / "staging"
        _create_file(source / "book.epub")

        copy_to_staging(source, staging, 'Book: A "Title" With <Bad> Chars')

        # Should have replaced unsafe chars.
        subdirs = list(staging.iterdir())
        assert len(subdirs) == 1
        assert ":" not in subdirs[0].name
        assert '"' not in subdirs[0].name

    def test_original_file_preserved(self, tmp_path):
        source = tmp_path / "dl"
        staging = tmp_path / "staging"
        original = _create_file(source / "book.epub", size=300)

        copy_to_staging(source, staging, "Book")

        # Original file must still exist (for seeding).
        assert original.exists()
        assert original.stat().st_size == 300


class TestAudiobookFormatPriority:
    """Phase 7: primary-file selection honours the user's audiobook
    format preference when a torrent contains multiple formats."""

    def test_m4b_preferred_over_mp3_when_ranked_first(self, tmp_path):
        _create_file(tmp_path / "audiobook.mp3", size=500)
        _create_file(tmp_path / "audiobook.m4b", size=100)
        files = find_book_files(
            tmp_path, audiobook_priority=["m4b", "m4a", "mp3"],
        )
        # m4b wins despite being smaller — priority overrides size.
        assert files[0].name == "audiobook.m4b"
        assert files[1].name == "audiobook.mp3"

    def test_no_priority_falls_back_to_size(self, tmp_path):
        _create_file(tmp_path / "audiobook.mp3", size=500)
        _create_file(tmp_path / "audiobook.m4b", size=100)
        files = find_book_files(tmp_path)
        assert files[0].name == "audiobook.mp3"

    def test_empty_priority_falls_back_to_size(self, tmp_path):
        _create_file(tmp_path / "audiobook.mp3", size=500)
        _create_file(tmp_path / "audiobook.m4b", size=100)
        files = find_book_files(tmp_path, audiobook_priority=[])
        assert files[0].name == "audiobook.mp3"

    def test_largest_file_wins_within_same_format(self, tmp_path):
        _create_file(tmp_path / "part01.mp3", size=100)
        _create_file(tmp_path / "part05.mp3", size=500)
        _create_file(tmp_path / "part03.mp3", size=300)
        files = find_book_files(
            tmp_path, audiobook_priority=["m4b", "m4a", "mp3"],
        )
        assert files[0].name == "part05.mp3"

    def test_noop_for_pure_ebook_torrent(self, tmp_path):
        """A folder of only epubs doesn't care about audiobook priority."""
        _create_file(tmp_path / "small.epub", size=100)
        _create_file(tmp_path / "large.epub", size=500)
        files = find_book_files(
            tmp_path, audiobook_priority=["m4b", "m4a", "mp3"],
        )
        assert files[0].name == "large.epub"

    def test_audiobook_ext_missing_from_priority_ranked_after(self, tmp_path):
        """An audiobook file whose extension isn't in the priority
        list lands after ranked formats but before non-audio."""
        _create_file(tmp_path / "book.mp3", size=100)
        _create_file(tmp_path / "book.m4a", size=500)
        # Priority only mentions m4b + mp3 — m4a unranked.
        files = find_book_files(
            tmp_path, audiobook_priority=["m4b", "mp3"],
        )
        assert files[0].name == "book.mp3"    # ranked in priority
        assert files[1].name == "book.m4a"    # unranked audio

    def test_m4a_preferred_when_listed_first(self, tmp_path):
        _create_file(tmp_path / "book.mp3", size=500)
        _create_file(tmp_path / "book.m4a", size=100)
        files = find_book_files(
            tmp_path, audiobook_priority=["m4a", "m4b", "mp3"],
        )
        assert files[0].name == "book.m4a"

    def test_copy_to_staging_honours_priority(self, tmp_path):
        source = tmp_path / "src"
        staging = tmp_path / "stage"
        _create_file(source / "book.mp3", size=500)
        _create_file(source / "book.m4b", size=100)
        result = copy_to_staging(
            source, staging, "Book",
            audiobook_priority=["m4b", "mp3"],
        )
        assert result.success
        # Primary (the file returned as book_filename) should be
        # the m4b even though it's smaller.
        assert result.book_filename == "book.m4b"


class TestEbookFormatPriority:
    """UAT 2026-05-11: ebook-side counterpart to TestAudiobookFormatPriority.
    Mark grabbed "Methodology of Secrets" by Elliot Freeman from a torrent
    containing both EPUB and PDF; the PDF was larger, so file_copier
    picked it for enrichment + CWA ingest despite EPUB being the user's
    preferred ebook format. `_apply_ebook_priority` mirrors the
    audiobook re-rank for ebook files."""

    def test_epub_preferred_over_pdf_when_ranked_first(self, tmp_path):
        _create_file(tmp_path / "book.pdf", size=500)
        _create_file(tmp_path / "book.epub", size=100)
        files = find_book_files(
            tmp_path, ebook_priority=["epub", "mobi", "azw3", "pdf"],
        )
        # EPUB wins despite being smaller — the canary fix.
        assert files[0].name == "book.epub"
        assert files[1].name == "book.pdf"

    def test_no_ebook_priority_falls_back_to_size(self, tmp_path):
        _create_file(tmp_path / "book.pdf", size=500)
        _create_file(tmp_path / "book.epub", size=100)
        files = find_book_files(tmp_path)
        assert files[0].name == "book.pdf"

    def test_empty_ebook_priority_falls_back_to_size(self, tmp_path):
        _create_file(tmp_path / "book.pdf", size=500)
        _create_file(tmp_path / "book.epub", size=100)
        files = find_book_files(tmp_path, ebook_priority=[])
        assert files[0].name == "book.pdf"

    def test_largest_file_wins_within_same_format(self, tmp_path):
        _create_file(tmp_path / "small.epub", size=100)
        _create_file(tmp_path / "big.epub", size=500)
        files = find_book_files(
            tmp_path, ebook_priority=["epub", "pdf"],
        )
        assert files[0].name == "big.epub"

    def test_noop_for_pure_audiobook_torrent(self, tmp_path):
        """A folder of only m4b doesn't care about ebook priority."""
        _create_file(tmp_path / "small.m4b", size=100)
        _create_file(tmp_path / "large.m4b", size=500)
        files = find_book_files(
            tmp_path, ebook_priority=["epub", "pdf"],
        )
        assert files[0].name == "large.m4b"

    def test_ebook_ext_missing_from_priority_ranked_after(self, tmp_path):
        """An ebook file whose extension isn't in the priority list
        lands after ranked formats but before non-ebook files."""
        _create_file(tmp_path / "book.pdf", size=100)
        _create_file(tmp_path / "book.azw3", size=500)
        # Priority only mentions epub + pdf — azw3 unranked.
        files = find_book_files(
            tmp_path, ebook_priority=["epub", "pdf"],
        )
        assert files[0].name == "book.pdf"    # ranked
        assert files[1].name == "book.azw3"   # unranked ebook

    def test_audiobook_and_ebook_priorities_compose(self, tmp_path):
        """Mixed-format release with BOTH audiobook + ebook priorities
        applied. Each priority operates on its own extension set."""
        _create_file(tmp_path / "book.pdf", size=500)
        _create_file(tmp_path / "book.epub", size=100)
        _create_file(tmp_path / "book.mp3", size=400)
        _create_file(tmp_path / "book.m4b", size=200)
        files = find_book_files(
            tmp_path,
            audiobook_priority=["m4b", "mp3"],
            ebook_priority=["epub", "pdf"],
        )
        # Ebook-priority pass runs after audiobook-priority. With both
        # extension sets ranked, the EPUB ends up first overall (the
        # ebook re-rank walks all files; mp3/m4b are in the audiobook
        # set so they get the unranked-ebook sentinel from the ebook
        # pass, landing AFTER ranked ebook files).
        assert files[0].name == "book.epub"
        # m4b should still beat mp3 within the audiobook subset.
        m4b_idx = next(i for i, f in enumerate(files) if f.name == "book.m4b")
        mp3_idx = next(i for i, f in enumerate(files) if f.name == "book.mp3")
        assert m4b_idx < mp3_idx

    def test_copy_to_staging_honours_ebook_priority(self, tmp_path):
        source = tmp_path / "src"
        staging = tmp_path / "stage"
        _create_file(source / "book.pdf", size=500)
        _create_file(source / "book.epub", size=100)
        result = copy_to_staging(
            source, staging, "Book",
            ebook_priority=["epub", "pdf"],
        )
        assert result.success
        # Primary should be EPUB despite PDF being larger — the
        # canary scenario from Mark's "Methodology of Secrets" grab.
        assert result.book_filename == "book.epub"
