"""
Unit tests for the CWA (Calibre-Web-Automated) sink.
"""
from pathlib import Path

import pytest

from app.metadata.extract import BookMetadata
from app.sinks.cwa import CWASink


class TestCWASink:
    async def test_drops_file_flat(self, tmp_path):
        src = tmp_path / "staging" / "book.epub"
        src.parent.mkdir()
        src.write_bytes(b"epub content")
        ingest = tmp_path / "cwa-ingest"

        sink = CWASink(str(ingest))
        result = await sink.deliver(str(src), BookMetadata())

        assert result.success is True
        assert result.sink_name == "cwa"
        # CWA expects flat drops — file should be directly in ingest dir.
        assert (ingest / "book.epub").exists()

    async def test_avoids_overwrite(self, tmp_path):
        src = tmp_path / "book.epub"
        src.write_bytes(b"new")
        ingest = tmp_path / "cwa-ingest"
        ingest.mkdir()
        (ingest / "book.epub").write_bytes(b"pending")

        sink = CWASink(str(ingest))
        result = await sink.deliver(str(src), BookMetadata())

        assert result.success is True
        assert (ingest / "book.epub").read_bytes() == b"pending"
        assert (ingest / "book_1.epub").exists()

    async def test_no_ingest_path_fails(self):
        sink = CWASink("")
        result = await sink.deliver("/some/book.epub", BookMetadata())
        assert result.success is False
        assert "not configured" in result.error

    async def test_missing_file_fails(self, tmp_path):
        sink = CWASink(str(tmp_path))
        result = await sink.deliver("/nope/book.epub", BookMetadata())
        assert result.success is False
        assert "not found" in result.error

    async def test_creates_ingest_dir(self, tmp_path):
        src = tmp_path / "book.epub"
        src.write_bytes(b"content")
        ingest = tmp_path / "deep" / "ingest"

        sink = CWASink(str(ingest))
        result = await sink.deliver(str(src), BookMetadata())

        assert result.success is True
        assert ingest.exists()


class TestCWASinkRemove:
    """v2.27.0 Phase 5b — inverse of deliver. Calls into CWAClient.delete
    via app.discovery.push_back so the wire-level CWA admin flow stays
    in one place. Tests at this level just verify the sink layer's
    config-reading + error translation."""

    def _patch_settings(self, monkeypatch, *, base_url, username, password):
        from app.sinks import cwa as cwa_module
        from app.discovery import push_back as push_back_module

        def fake_load_settings():
            return {
                "cwa_base_url": base_url,
                "cwa_username": username,
            }
        async def fake_get_secret(name):
            return password if name == "cwa_password" else ""

        # Patch the imports at their CWA-sink-side use site. Both are
        # imported lazily inside `remove`, so monkeypatching `app.config`
        # and `app.secrets` directly is the path that survives.
        import app.config
        import app.secrets
        monkeypatch.setattr(app.config, "load_settings", fake_load_settings)
        monkeypatch.setattr(app.secrets, "get_secret", fake_get_secret)

    async def test_remove_calls_cwaclient_delete(self, monkeypatch):
        self._patch_settings(
            monkeypatch,
            base_url="http://cwa:8083",
            username="admin",
            password="hunter2",
        )

        calls: list[int] = []
        class StubCWAClient:
            def __init__(self, base_url, username, password):
                assert base_url == "http://cwa:8083"
                assert username == "admin"
                assert password == "hunter2"
            async def delete(self, book_id):
                calls.append(book_id)

        from app.discovery import push_back
        monkeypatch.setattr(push_back, "CWAClient", StubCWAClient)

        sink = CWASink("/cwa-ingest")
        result = await sink.remove(calibre_book_id=42)

        assert result.success is True
        assert calls == [42]
        assert "42" in (result.detail or "")

    async def test_remove_fails_when_creds_missing(self, monkeypatch):
        self._patch_settings(
            monkeypatch, base_url="", username="", password="",
        )
        sink = CWASink("/cwa-ingest")
        result = await sink.remove(calibre_book_id=42)
        assert result.success is False
        assert "not configured" in (result.error or "")

    async def test_remove_propagates_push_failed(self, monkeypatch):
        self._patch_settings(
            monkeypatch,
            base_url="http://cwa:8083",
            username="admin",
            password="hunter2",
        )

        from app.discovery import push_back
        class StubCWAClient:
            def __init__(self, *args, **kw): pass
            async def delete(self, book_id):
                raise push_back.PushFailed("CWA delete did not remove book")
        monkeypatch.setattr(push_back, "CWAClient", StubCWAClient)

        sink = CWASink("/cwa-ingest")
        result = await sink.remove(calibre_book_id=42)
        assert result.success is False
        assert "did not remove" in (result.error or "")

    async def test_remove_propagates_unavailable(self, monkeypatch):
        self._patch_settings(
            monkeypatch,
            base_url="http://cwa:8083",
            username="admin",
            password="hunter2",
        )

        from app.discovery import push_back
        class StubCWAClient:
            def __init__(self, *args, **kw): pass
            async def delete(self, book_id):
                raise push_back.PushUnavailable("not applicable")
        monkeypatch.setattr(push_back, "CWAClient", StubCWAClient)

        sink = CWASink("/cwa-ingest")
        result = await sink.remove(calibre_book_id=42)
        assert result.success is False
        assert "not applicable" in (result.error or "")

    async def test_remove_swallows_unexpected_exception(self, monkeypatch):
        """An unexpected exception inside CWAClient should surface as a
        failed SinkResult, not raise out of `remove`. The orchestrator
        relies on every sink op returning a SinkResult so it can
        decide whether to rollback the soft-delete or retry."""
        self._patch_settings(
            monkeypatch,
            base_url="http://cwa:8083",
            username="admin",
            password="hunter2",
        )

        from app.discovery import push_back
        class StubCWAClient:
            def __init__(self, *args, **kw): pass
            async def delete(self, book_id):
                raise ConnectionError("CWA unreachable")
        monkeypatch.setattr(push_back, "CWAClient", StubCWAClient)

        sink = CWASink("/cwa-ingest")
        result = await sink.remove(calibre_book_id=42)
        assert result.success is False
        assert "ConnectionError" in (result.error or "")
