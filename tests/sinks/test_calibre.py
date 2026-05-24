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


@pytest.fixture
def remove_capable_calibredb(tmp_path, monkeypatch):
    """Fake calibredb that dispatches on argv[1] (list/remove/add) and
    captures every invocation for assertion. `list` emits whatever
    JSON the test set via `FAKE_CALIBREDB_LIST_OUT` (env var pointing
    at a file); `remove` always succeeds; `add` always succeeds.

    Returns (script_path, calls_log_path) so the test can read the
    record of every dispatch made by the code under test.
    """
    script = tmp_path / "calibredb"
    calls_log = tmp_path / "calls.log"
    list_out_default = tmp_path / "list_out_default.json"
    list_out_default.write_text("[]")
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {calls_log}\n'
        'case "$1" in\n'
        "  list)\n"
        f'    out="${{FAKE_CALIBREDB_LIST_OUT:-{list_out_default}}}"\n'
        '    cat "$out"\n'
        "    ;;\n"
        "  remove)\n"
        '    echo "removed: $@"\n'
        "    exit 0\n"
        "    ;;\n"
        "  *)\n"
        '    echo "noop: $@"\n'
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(calibre, "CALIBREDB_CMD", str(script))
    return script, calls_log


@pytest.fixture
def list_fails_calibredb(tmp_path, monkeypatch):
    """Fake calibredb whose `list` subcommand exits non-zero with stderr."""
    script = tmp_path / "calibredb"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  list)\n"
        '    echo "Calibre library path does not exist" >&2\n'
        "    exit 2\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(calibre, "CALIBREDB_CMD", str(script))
    return script


@pytest.fixture
def remove_fails_calibredb(tmp_path, monkeypatch):
    """Fake calibredb that emits IDs on list but fails on remove."""
    script = tmp_path / "calibredb"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  list)\n"
        '    echo \'[{"id": 42}]\'\n'
        "    ;;\n"
        "  remove)\n"
        '    echo "Permission denied" >&2\n'
        "    exit 1\n"
        "    ;;\n"
        "esac\n"
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


class TestCalibreSinkRemove:
    """v2.27.0 Phase 5b — inverse of `deliver()` used by the active
    replacement enact path. Three resolution paths (calibre_book_id
    direct → mam_torrent_id search → file_path sqlite). Idempotency
    matters: an enact can be retried after partial failure, so a
    lookup that returns no rows is a successful no-op, not an error."""

    async def test_remove_by_calibre_book_id_skips_search(
        self, tmp_path, remove_capable_calibredb,
    ):
        """Primary path: caller provides Calibre book id directly, so
        the sink should NOT invoke `calibredb list` at all — just
        `calibredb remove`."""
        script, calls_log = remove_capable_calibredb

        sink = CalibreSink(str(tmp_path / "lib"))
        result = await sink.remove(calibre_book_id=42)

        assert result.success is True
        log_lines = calls_log.read_text().strip().splitlines()
        # No list invocation.
        assert not any(line.startswith("list ") for line in log_lines), log_lines
        # remove was invoked with id 42.
        assert any(
            line.startswith("remove ") and " 42" in line
            for line in log_lines
        ), log_lines

    async def test_remove_by_mam_torrent_id_invokes_search_then_remove(
        self, tmp_path, monkeypatch, remove_capable_calibredb,
    ):
        script, calls_log = remove_capable_calibredb
        list_out = tmp_path / "list_out.json"
        list_out.write_text('[{"id": 17}, {"id": 23}]')
        monkeypatch.setenv("FAKE_CALIBREDB_LIST_OUT", str(list_out))

        sink = CalibreSink(str(tmp_path / "lib"))
        result = await sink.remove(mam_torrent_id=987654)

        assert result.success is True
        assert result.sink_name == "calibre"
        log = calls_log.read_text()
        # Search line uses identifiers:mam_torrent_id:<X>.
        assert "identifiers:mam_torrent_id:987654" in log
        # Remove line includes both ids the search returned.
        log_lines = log.strip().splitlines()
        remove_lines = [l for l in log_lines if l.startswith("remove ")]
        assert remove_lines, log_lines
        assert " 17" in remove_lines[0] and " 23" in remove_lines[0]

    async def test_remove_by_file_path_uses_sqlite(self, tmp_path):
        """file_path resolution opens the Calibre metadata.db directly
        and matches against books.path (relative parent dir). This is
        the only reliable path-based lookup because calibredb's CLI
        search has no `path:` field."""
        import sqlite3

        library = tmp_path / "lib"
        library.mkdir()
        # Build a fake metadata.db that mirrors Calibre's books table
        # shape just enough for the lookup.
        db = library / "metadata.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, path TEXT)")
        con.execute(
            "INSERT INTO books (id, path) VALUES (?, ?)",
            (123, "Brandon Sanderson/The Way of Kings (123)"),
        )
        con.commit()
        con.close()

        # Create the file path that calibredb would have created when
        # adding this book.
        book_dir = library / "Brandon Sanderson" / "The Way of Kings (123)"
        book_dir.mkdir(parents=True)
        book_file = book_dir / "The Way of Kings - Brandon Sanderson.epub"
        book_file.write_bytes(b"x")

        # We don't even need calibredb installed for the sqlite lookup
        # part — but the subsequent `remove` does. Use the no-op fake.
        sink = CalibreSink(str(library))
        # Stub the actual calibredb-remove subprocess so the test stays
        # hermetic. We're verifying lookup-resolution here, not the
        # subprocess call (covered by other tests).
        calls: list = []
        async def fake_create(*args, **kwargs):
            calls.append(args)
            class P:
                returncode = 0
                async def communicate(self):
                    return (b"", b"")
            return P()
        import app.sinks.calibre as cal_mod
        import asyncio as _asyncio
        original = _asyncio.create_subprocess_exec
        _asyncio.create_subprocess_exec = fake_create  # type: ignore[assignment]
        try:
            result = await sink.remove(file_path=str(book_file))
        finally:
            _asyncio.create_subprocess_exec = original  # type: ignore[assignment]

        assert result.success is True
        # The remove subprocess was called with book id 123.
        assert any("123" in str(arg) for arg in calls[0])

    def test_sqlite_lookup_rejects_path_outside_library(self, tmp_path):
        library = tmp_path / "lib"
        library.mkdir()
        (library / "metadata.db").write_bytes(b"")
        elsewhere = tmp_path / "other" / "book.epub"
        elsewhere.parent.mkdir()
        elsewhere.write_bytes(b"")

        sink = CalibreSink(str(library))
        ids, err = sink._sqlite_lookup_by_path(str(elsewhere))
        assert ids == []
        assert err is not None
        assert "not under library_path" in err

    def test_sqlite_lookup_missing_metadata_db(self, tmp_path):
        library = tmp_path / "lib"
        library.mkdir()
        book = library / "Author" / "Title (1)" / "book.epub"
        book.parent.mkdir(parents=True)
        book.write_bytes(b"")
        sink = CalibreSink(str(library))
        ids, err = sink._sqlite_lookup_by_path(str(book))
        assert ids == []
        assert "metadata.db not found" in (err or "")

    async def test_remove_idempotent_when_no_mam_id_match(
        self, tmp_path, remove_capable_calibredb,
    ):
        """Empty mam_torrent_id search → success, not error."""
        script, calls_log = remove_capable_calibredb
        sink = CalibreSink(str(tmp_path / "lib"))
        result = await sink.remove(mam_torrent_id=42)

        assert result.success is True
        assert "no match" in (result.detail or "")
        log_lines = calls_log.read_text().strip().splitlines()
        # No remove invocation.
        assert not any(line.startswith("remove ") for line in log_lines), log_lines

    async def test_remove_idempotent_when_path_lookup_misses(self, tmp_path):
        """file_path that resolves cleanly but has no matching book row
        in metadata.db is also a successful no-op."""
        import sqlite3
        library = tmp_path / "lib"
        library.mkdir()
        db = library / "metadata.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, path TEXT)")
        con.commit()
        con.close()

        book = library / "Author" / "Title (99)" / "book.epub"
        book.parent.mkdir(parents=True)
        book.write_bytes(b"")

        sink = CalibreSink(str(library))
        result = await sink.remove(file_path=str(book))
        assert result.success is True
        assert "no match" in (result.detail or "")

    async def test_remove_no_library_path_fails(self):
        sink = CalibreSink("")
        result = await sink.remove(calibre_book_id=1)
        assert result.success is False
        assert "not configured" in result.error

    async def test_remove_requires_some_identifier(self, tmp_path):
        sink = CalibreSink(str(tmp_path))
        result = await sink.remove()
        assert result.success is False
        assert "requires" in result.error

    async def test_remove_propagates_list_failure(
        self, tmp_path, list_fails_calibredb,
    ):
        """A failed list lookup should surface as a SinkResult error,
        not silently swallow + treat as 'no match' — the latter could
        mask config problems and cause active-replacement to skip
        deletes that were genuinely required."""
        sink = CalibreSink(str(tmp_path / "lib"))
        result = await sink.remove(mam_torrent_id=1)
        assert result.success is False
        assert "exit 2" in (result.error or "")

    async def test_remove_propagates_remove_failure(
        self, tmp_path, remove_fails_calibredb,
    ):
        sink = CalibreSink(str(tmp_path / "lib"))
        result = await sink.remove(calibre_book_id=42)
        assert result.success is False
        assert "exit 1" in (result.error or "")

    async def test_remove_diagnostic_fires_on_gl_failure(
        self, tmp_path, monkeypatch,
    ):
        """The slim-image-missing-libGL diagnostic should mention the
        `remove` action when the failure happens inside the remove
        path, not just `add`."""
        script = tmp_path / "calibredb"
        script.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  remove)\n"
            '    echo "qt.qpa.plugin: Could not load Qt platform plugin" >&2\n'
            "    exit 1\n"
            "    ;;\n"
            "esac\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setattr(calibre, "CALIBREDB_CMD", str(script))

        # Verify the diagnostic helper produces the action="remove" string.
        from app.sinks.calibre import _format_runtime_lib_diagnostic
        out = _format_runtime_lib_diagnostic(
            "qt.qpa.plugin: Could not load Qt platform plugin",
            action="remove",
        )
        assert "calibredb remove" in out

        sink = CalibreSink(str(tmp_path / "lib"))
        result = await sink.remove(calibre_book_id=99)
        assert result.success is False


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
