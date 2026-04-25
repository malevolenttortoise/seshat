"""
Unit tests for monthly download folder management.
"""
from datetime import datetime, timezone
from pathlib import Path

from app.orchestrator.download_folders import (
    compute_download_folder,
    current_month_folder,
    ensure_folder_exists,
    translate_path,
    _render_template,
)


class TestCurrentMonthFolder:
    def test_basic_path(self):
        dt = datetime(2026, 4, 10, tzinfo=timezone.utc)
        result = current_month_folder("/downloads/[mam-complete]", now=dt)
        assert result == "/downloads/[mam-complete]/[2026-04]"

    def test_january(self):
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = current_month_folder("/downloads/[mam-complete]", now=dt)
        assert result == "/downloads/[mam-complete]/[2026-01]"

    def test_december(self):
        dt = datetime(2026, 12, 31, tzinfo=timezone.utc)
        result = current_month_folder("/downloads/[mam-complete]", now=dt)
        assert result == "/downloads/[mam-complete]/[2026-12]"

    def test_empty_base_returns_empty(self):
        assert current_month_folder("") == ""

    def test_trailing_slash_handled(self):
        dt = datetime(2026, 4, 10, tzinfo=timezone.utc)
        result = current_month_folder("/downloads/[mam-complete]/", now=dt)
        assert result == "/downloads/[mam-complete]/[2026-04]"

    def test_uses_current_time_by_default(self):
        # Just verify it doesn't crash without a `now` argument.
        result = current_month_folder("/downloads/test")
        assert "[20" in result  # sanity check for year prefix


class TestEnsureFolderExists:
    def test_creates_folder(self, tmp_path):
        target = str(tmp_path / "[2026-04]")
        assert ensure_folder_exists(target) is True
        assert Path(target).is_dir()

    def test_existing_folder_ok(self, tmp_path):
        target = tmp_path / "[2026-04]"
        target.mkdir()
        assert ensure_folder_exists(str(target)) is True

    def test_nested_creation(self, tmp_path):
        target = str(tmp_path / "deep" / "nested" / "[2026-04]")
        assert ensure_folder_exists(target) is True
        assert Path(target).is_dir()

    def test_empty_path_returns_false(self):
        assert ensure_folder_exists("") is False


class TestTranslatePath:
    def test_qbit_to_local(self):
        result = translate_path("/data/[mam-complete]/book", "/data", "/downloads")
        assert result == "/downloads/[mam-complete]/book"

    def test_local_to_qbit(self):
        result = translate_path("/downloads/[mam-complete]/book", "/downloads", "/data")
        assert result == "/data/[mam-complete]/book"

    def test_no_match_returns_unchanged(self):
        result = translate_path("/other/path/book", "/data", "/downloads")
        assert result == "/other/path/book"

    def test_exact_prefix_match(self):
        result = translate_path("/data", "/data", "/downloads")
        assert result == "/downloads"

    def test_trailing_slashes_handled(self):
        result = translate_path("/data/[mam-complete]", "/data/", "/downloads/")
        assert result == "/downloads/[mam-complete]"

    def test_empty_path(self):
        assert translate_path("", "/data", "/downloads") == ""

    def test_empty_prefix(self):
        assert translate_path("/data/book", "", "/downloads") == "/data/book"


class TestRenderTemplate:
    def test_author_only(self):
        assert _render_template("{author}", author="Brandon Sanderson") == "Brandon Sanderson"

    def test_author_series_title(self):
        result = _render_template(
            "{author}/{series}/{title}",
            author="Brandon Sanderson",
            series="Mistborn",
            title="The Final Empire",
        )
        assert result == "Brandon Sanderson/Mistborn/The Final Empire"

    def test_standalone_drops_series_segment(self):
        # Standalone book → empty series → that segment is dropped
        # rather than producing an empty-named directory.
        result = _render_template(
            "{author}/{series}/{title}",
            author="Project Hail Mary",
            series="",
            title="Project Hail Mary",
        )
        assert result == "Project Hail Mary/Project Hail Mary"

    def test_all_empty_falls_back_to_author(self):
        # Pathological: everything empty. Avoid returning "" which
        # would dump the torrent in the bare root.
        result = _render_template(
            "{author}/{series}/{title}",
            author="",
            series="",
            title="",
        )
        assert result == "_Unknown"

    def test_normalizes_author_dots(self):
        # "William D. Arand" and "William D Arand" should land
        # together — same canonicalization the legacy author mode
        # used.
        a = _render_template("{author}", author="William D. Arand")
        b = _render_template("{author}", author="William D Arand")
        assert a == b == "William D Arand"

    def test_strips_filesystem_unsafe_chars(self):
        result = _render_template(
            "{author}/{title}",
            author="Author<>",
            title="Title?*|\"",
        )
        assert result == "Author/Title"

    def test_unknown_token_drops_segment(self):
        # `{publisher}` isn't supported; the segment containing it
        # should be skipped rather than crashing the submission.
        result = _render_template(
            "{author}/{publisher}/{title}",
            author="A",
            title="T",
        )
        assert result == "A/T"

    def test_extra_slashes_collapse(self):
        # Defensive: doubled-up separators in the user template
        # shouldn't produce empty segments in the output.
        result = _render_template(
            "{author}//{title}",
            author="A",
            title="T",
        )
        assert result == "A/T"

    def test_series_only_template(self):
        result = _render_template(
            "{series}",
            author="A",
            series="My Series",
            title="T",
        )
        assert result == "My Series"


class TestComputeDownloadFolderTemplate:
    def test_template_mode_full(self):
        result = compute_download_folder(
            "/downloads/[mam-complete]",
            "template",
            author_name="Brandon Sanderson",
            series_name="Mistborn",
            book_title="The Final Empire",
            template="{author}/{series}/{title}",
        )
        assert result == "/downloads/[mam-complete]/Brandon Sanderson/Mistborn/The Final Empire"

    def test_template_mode_standalone(self):
        result = compute_download_folder(
            "/downloads/[mam-complete]",
            "template",
            author_name="Andy Weir",
            series_name="",
            book_title="Project Hail Mary",
            template="{author}/{series}/{title}",
        )
        assert result == "/downloads/[mam-complete]/Andy Weir/Project Hail Mary"

    def test_template_mode_empty_template_defaults_to_author(self):
        # Empty template should match legacy "author" mode exactly.
        result = compute_download_folder(
            "/downloads/[mam-complete]",
            "template",
            author_name="Brandon Sanderson",
            template="",
        )
        assert result == "/downloads/[mam-complete]/Brandon Sanderson"

    def test_legacy_author_mode_unchanged(self):
        # Regression check — the existing "author" mode keeps its
        # exact pre-Phase-5 behavior.
        result = compute_download_folder(
            "/downloads/[mam-complete]",
            "author",
            author_name="Brandon Sanderson",
        )
        assert result == "/downloads/[mam-complete]/Brandon Sanderson"
