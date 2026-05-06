"""
Unit tests for the Calibre sink.

Tests use a fake calibredb script that echoes its arguments so we
can verify the correct CLI invocation without needing Calibre installed.
"""
import os
import stat
from pathlib import Path

import pytest

from app.metadata.extract import BookMetadata
from app.sinks import calibre
from app.sinks.calibre import CalibreSink


@pytest.fixture
def fake_calibredb(tmp_path, monkeypatch):
    """Create a fake calibredb script that logs its args and exits 0."""
    script = tmp_path / "calibredb"
    script.write_text(
        '#!/bin/sh\necho "Added book: $@"\nexit 0\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(calibre, "CALIBREDB_CMD", str(script))
    return script


@pytest.fixture
def failing_calibredb(tmp_path, monkeypatch):
    """Create a fake calibredb that exits with error."""
    script = tmp_path / "calibredb"
    script.write_text(
        '#!/bin/sh\necho "Error: duplicate book" >&2\nexit 1\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(calibre, "CALIBREDB_CMD", str(script))
    return script


class TestCalibreSink:
    async def test_successful_add(self, tmp_path, fake_calibredb):
        book = tmp_path / "book.epub"
        book.write_bytes(b"epub content")
        library = tmp_path / "calibre_lib"
        library.mkdir()

        sink = CalibreSink(str(library))
        result = await sink.deliver(str(book), BookMetadata(title="Test"))

        assert result.success is True
        assert result.sink_name == "calibre"

    async def test_passes_metadata_flags(self, tmp_path, fake_calibredb):
        book = tmp_path / "book.epub"
        book.write_bytes(b"content")
        library = tmp_path / "lib"
        library.mkdir()

        meta = BookMetadata(
            title="The Way of Kings",
            author="Brandon Sanderson",
            series="Stormlight Archive",
            series_index="1",
            isbn="9780765326355",
        )
        sink = CalibreSink(str(library))
        result = await sink.deliver(str(book), meta)

        assert result.success is True
        # The fake script echoes all args, so the detail contains them.
        assert "The Way of Kings" in result.detail
        assert "Brandon Sanderson" in result.detail

    async def test_failed_add(self, tmp_path, failing_calibredb):
        book = tmp_path / "book.epub"
        book.write_bytes(b"content")

        sink = CalibreSink(str(tmp_path))
        result = await sink.deliver(str(book), BookMetadata())

        assert result.success is False
        assert "exit 1" in result.error

    async def test_no_library_path(self, tmp_path):
        sink = CalibreSink("")
        result = await sink.deliver(str(tmp_path / "book.epub"), BookMetadata())
        assert result.success is False
        assert "not configured" in result.error

    async def test_missing_file(self, tmp_path, fake_calibredb):
        sink = CalibreSink(str(tmp_path))
        result = await sink.deliver("/nope/book.epub", BookMetadata())
        assert result.success is False
        assert "not found" in result.error

    async def test_calibredb_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(calibre, "CALIBREDB_CMD", "/nonexistent/calibredb")
        book = tmp_path / "book.epub"
        book.write_bytes(b"content")

        sink = CalibreSink(str(tmp_path))
        result = await sink.deliver(str(book), BookMetadata())

        assert result.success is False
        assert "not found" in result.error


class TestRuntimeLibFailureDetection:
    """The trimmed apt-deps image is missing libgl1/libegl1/libopengl0
    on the assumption headless calibredb won't pull GL symbols. If
    that assumption ever breaks, we want a structured diagnostic in
    the logs so users can file an actionable issue."""

    def test_qt_plugin_load_failure_matches(self):
        from app.sinks.calibre import _detect_runtime_lib_failure
        stderr = (
            "qt.qpa.plugin: Could not load the Qt platform plugin "
            "\"xcb\" in \"\" even though it was found."
        )
        assert _detect_runtime_lib_failure(stderr) is True

    def test_libgl_missing_matches(self):
        from app.sinks.calibre import _detect_runtime_lib_failure
        stderr = (
            "calibredb: error while loading shared libraries: "
            "libGL.so.1: cannot open shared object file: No such file"
        )
        assert _detect_runtime_lib_failure(stderr) is True

    def test_libegl_missing_matches(self):
        from app.sinks.calibre import _detect_runtime_lib_failure
        stderr = "libEGL.so.1: cannot open shared object file"
        assert _detect_runtime_lib_failure(stderr) is True

    def test_libxcb_cursor_missing_matches(self):
        from app.sinks.calibre import _detect_runtime_lib_failure
        stderr = "From 6.5.0, xcb-cursor0 or libxcb-cursor0 is needed"
        assert _detect_runtime_lib_failure(stderr) is True

    def test_ordinary_calibre_error_does_not_match(self):
        """Bad library path / duplicate book / etc. shouldn't trigger
        the GL diagnostic — they have nothing to do with system libs."""
        from app.sinks.calibre import _detect_runtime_lib_failure
        assert _detect_runtime_lib_failure(
            "Calibre library path does not exist"
        ) is False
        assert _detect_runtime_lib_failure(
            "Error: book is already in the library"
        ) is False
        assert _detect_runtime_lib_failure("") is False

    def test_diagnostic_block_includes_action_and_stderr(self):
        from app.sinks.calibre import _format_runtime_lib_diagnostic
        out = _format_runtime_lib_diagnostic(
            "qt.qpa.plugin: Could not load Qt platform plugin",
            action="add",
        )
        assert "calibredb add" in out
        assert "qt.qpa.plugin" in out
        assert "github.com/malevolenttortoise/seshat/issues" in out
        # Hint about the libgl1 trade-off should be there so users
        # know how to escape if they're hitting it.
        assert "libgl1" in out
