"""
Unit tests for the Audiobookshelf library app — the API client, the
discover+sync wiring, and the secrets-store read path.
"""
from __future__ import annotations

from typing import Any

import httpx


# ─── AudiobookshelfClient ──────────────────────────────────────

def _mock_transport(responses: dict[str, Any]) -> httpx.MockTransport:
    """Build a MockTransport mapping URL paths → response bodies."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path not in responses:
            return httpx.Response(404, json={"error": f"no mock for {path}"})
        entry = responses[path]
        if callable(entry):
            return entry(request)
        status, body = entry
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


class TestAudiobookshelfClient:
    async def test_list_libraries_filters_to_books(self, monkeypatch):
        from app.library_apps import audiobookshelf as abs_mod

        transport = _mock_transport({
            "/api/libraries": (200, {"libraries": [
                {"id": "lib-book", "name": "audio-library",
                 "mediaType": "book", "folders": [{"fullPath": "/audiobooks"}],
                 "lastUpdate": 1776711480506},
                {"id": "lib-pod", "name": "podcasts", "mediaType": "podcast",
                 "folders": [], "lastUpdate": 1776700000000},
            ]}),
        })

        # MockTransport can't be used with the `with httpx.Client()` block
        # inside the client — monkeypatch the httpx.AsyncClient to build
        # with our transport instead.
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        client = abs_mod.AudiobookshelfClient("http://abs:13378", "tok")
        libs = await client.list_libraries()
        assert len(libs) == 2  # client doesn't filter — discover() does
        assert libs[0]["mediaType"] == "book"
        assert libs[1]["mediaType"] == "podcast"

    async def test_iter_all_items_paginates(self, monkeypatch):
        from app.library_apps import audiobookshelf as abs_mod

        pages = [
            {"results": [{"id": "a"}, {"id": "b"}], "total": 3, "offset": 0, "limit": 2},
            {"results": [{"id": "c"}], "total": 3, "offset": 2, "limit": 2},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", "0"))
            return httpx.Response(200, json=pages[page])

        transport = httpx.MockTransport(handler)
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        client = abs_mod.AudiobookshelfClient("http://abs:13378", "tok")
        ids = []
        async for item in client.iter_all_items("lib-123", page_size=2):
            ids.append(item["id"])
        assert ids == ["a", "b", "c"]

    async def test_trigger_scan_returns_true_on_2xx(self, monkeypatch):
        from app.library_apps import audiobookshelf as abs_mod

        transport = _mock_transport({
            "/api/libraries/lib-x/scan": (200, {}),
        })
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        client = abs_mod.AudiobookshelfClient("http://abs:13378", "tok")
        assert await client.trigger_scan("lib-x") is True

    async def test_trigger_scan_swallows_http_errors(self, monkeypatch):
        from app.library_apps import audiobookshelf as abs_mod

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope", request=request)

        orig = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: orig(transport=httpx.MockTransport(handler),
                              **{k: v for k, v in kw.items() if k != "transport"}),
        )

        client = abs_mod.AudiobookshelfClient("http://abs:13378", "tok")
        assert await client.trigger_scan("lib-x") is False


# ─── Secrets read (sync) ───────────────────────────────────────

def _isolate_data_dir(tmp_path, monkeypatch):
    """Redirect every consumer of `get_data_dir()` at tmp_path.

    `from app.runtime import get_data_dir` binds a local name in each
    importing module, so monkeypatching `app.runtime.get_data_dir` alone
    is not enough — we have to target every caller that did that
    import. Missing even one means tests leak into the user's real
    seshat_auth.db (past incident: `abs_api_key = test-bearer-token`
    was written to the real DB before this helper existed).
    """
    from app import auth_db, auth_secret, runtime
    monkeypatch.setattr(runtime, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(auth_db, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(auth_secret, "_cached_secret", None)


class TestSyncSecretsRead:
    async def test_roundtrip_read(self, tmp_path, monkeypatch):
        """Write a secret via the async store, read via the sync helper."""
        from app import auth_db, secrets as secrets_mod
        from app.library_apps.audiobookshelf import _get_abs_api_key_sync

        _isolate_data_dir(tmp_path, monkeypatch)

        await auth_db.init_auth_db()
        await secrets_mod.init_secrets_table()
        await secrets_mod.set_secret("abs_api_key", "test-bearer-token")

        assert _get_abs_api_key_sync() == "test-bearer-token"

    def test_missing_secret_returns_none(self, tmp_path, monkeypatch):
        from app.library_apps.audiobookshelf import _get_abs_api_key_sync

        _isolate_data_dir(tmp_path, monkeypatch)

        # No auth DB file → must return None cleanly (not raise).
        assert _get_abs_api_key_sync() is None


# ─── AudiobookshelfApp.discover ───────────────────────────────

class TestAudiobookshelfDiscover:
    def test_no_root_path_returns_empty(self):
        from app.library_apps.audiobookshelf import AudiobookshelfApp
        assert AudiobookshelfApp().discover("") == []

    def test_no_api_key_returns_empty(self, monkeypatch):
        from app.library_apps.audiobookshelf import AudiobookshelfApp

        monkeypatch.setattr(
            "app.library_apps.audiobookshelf._get_abs_api_key_sync",
            lambda: None,
        )
        assert AudiobookshelfApp().discover("http://abs:13378") == []

    def test_discover_filters_podcasts(self, monkeypatch):
        from app.library_apps import audiobookshelf as abs_mod

        monkeypatch.setattr(
            abs_mod, "_get_abs_api_key_sync", lambda: "tok",
        )

        def fake_list_libraries_sync(self):
            return [
                {"id": "book-lib", "name": "audio-library", "mediaType": "book",
                 "folders": [{"fullPath": "/audiobooks"}], "lastUpdate": 123},
                {"id": "pod-lib", "name": "podcasts", "mediaType": "podcast",
                 "folders": [], "lastUpdate": 456},
            ]

        monkeypatch.setattr(
            abs_mod.AudiobookshelfClient,
            "list_libraries_sync",
            fake_list_libraries_sync,
        )

        libs = abs_mod.AudiobookshelfApp().discover("http://abs:13378")
        assert len(libs) == 1
        assert libs[0]["abs_library_id"] == "book-lib"
        assert libs[0]["content_type"] == "audiobook"
        assert libs[0]["app_type"] == "audiobookshelf"
        assert libs[0]["library_path"] == "/audiobooks"
        assert libs[0]["abs_last_update"] == 123

    def test_discover_survives_api_error(self, monkeypatch):
        from app.library_apps import audiobookshelf as abs_mod

        monkeypatch.setattr(abs_mod, "_get_abs_api_key_sync", lambda: "tok")

        def broken_list_libraries_sync(self):
            raise httpx.ConnectError("down", request=None)

        monkeypatch.setattr(
            abs_mod.AudiobookshelfClient,
            "list_libraries_sync",
            broken_list_libraries_sync,
        )

        assert abs_mod.AudiobookshelfApp().discover("http://abs:13378") == []

    async def test_get_mtime_fetches_live_value_from_api(self, monkeypatch):
        """The cached `abs_last_update` is frozen at startup — `get_mtime`
        must hit the API on every tick, otherwise scheduled syncs
        short-circuit forever after the first sync (canary: Mark added
        66 audiobooks overnight and saw zero scheduled syncs).

        v2.3.4.1: return is a composite "lastUpdate:numItems" so item-
        count changes trigger sync even when ABS doesn't bump
        lastUpdate.
        """
        from app.library_apps import audiobookshelf as abs_mod

        async def fake_get_key():
            return "tok"
        monkeypatch.setattr(abs_mod, "_get_abs_api_key", fake_get_key)

        transport = _mock_transport({
            "/api/libraries": (200, {"libraries": [
                {"id": "lib-book", "mediaType": "book", "folders": [],
                 "lastUpdate": 9999},
            ]}),
            "/api/libraries/lib-book/items": (200, {
                "results": [], "total": 113, "limit": 0, "page": 0,
            }),
        })
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        lib = {
            "abs_base_url": "http://abs:13378",
            "abs_library_id": "lib-book",
            "abs_last_update": 1234,  # stale cached value
        }
        result = await abs_mod.AudiobookshelfApp().get_mtime(lib)
        # Composite signal: lastUpdate:numItems.
        assert result == "9999.0:113"
        # Cache also refreshed in place (still a float so existing
        # readers don't break).
        assert lib["abs_last_update"] == 9999.0

    async def test_get_mtime_item_count_change_with_stable_lastupdate(
        self, monkeypatch,
    ):
        """The v2.3.4.1 fix's reason for being. ABS's `lastUpdate`
        bumps on settings changes but not reliably on item adds —
        Mark's UAT 2026-05-07: 5 audiobooks added, ABS UI showed
        113 items, but `lastUpdate` was 17 days stale. The composite
        signal sees the item-count delta even when lastUpdate is
        flat."""
        from app.library_apps import audiobookshelf as abs_mod

        async def fake_get_key():
            return "tok"
        monkeypatch.setattr(abs_mod, "_get_abs_api_key", fake_get_key)

        # Two consecutive ticks: lastUpdate stays at 9999, but items
        # grew 108 → 113.
        items_total = {"n": 108}
        def items_handler(request):
            return httpx.Response(200, json={
                "results": [], "total": items_total["n"], "limit": 0, "page": 0,
            })

        transport = _mock_transport({
            "/api/libraries": (200, {"libraries": [
                {"id": "lib-book", "mediaType": "book", "folders": [],
                 "lastUpdate": 9999},
            ]}),
            "/api/libraries/lib-book/items": items_handler,
        })
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        lib = {
            "abs_base_url": "http://abs:13378",
            "abs_library_id": "lib-book",
            "abs_last_update": 9999,
        }
        first = await abs_mod.AudiobookshelfApp().get_mtime(lib)
        items_total["n"] = 113  # five audiobooks added between ticks
        second = await abs_mod.AudiobookshelfApp().get_mtime(lib)
        # lastUpdate identical, item count changed → signals differ.
        assert first != second
        assert first == "9999.0:108"
        assert second == "9999.0:113"

    async def test_get_mtime_item_count_failure_degrades_to_lastupdate(
        self, monkeypatch,
    ):
        """Items endpoint failing should not block the sync. Degrade
        to the pre-v2.3.4.1 lastUpdate-only return — better one
        non-redundant sync than no signal at all."""
        from app.library_apps import audiobookshelf as abs_mod

        async def fake_get_key():
            return "tok"
        monkeypatch.setattr(abs_mod, "_get_abs_api_key", fake_get_key)

        def items_handler(request):
            return httpx.Response(500, json={"error": "kaboom"})

        transport = _mock_transport({
            "/api/libraries": (200, {"libraries": [
                {"id": "lib-book", "mediaType": "book", "folders": [],
                 "lastUpdate": 9999},
            ]}),
            "/api/libraries/lib-book/items": items_handler,
        })
        orig = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        lib = {
            "abs_base_url": "http://abs:13378",
            "abs_library_id": "lib-book",
            "abs_last_update": 1234,
        }
        result = await abs_mod.AudiobookshelfApp().get_mtime(lib)
        # Float-only fallback (no composite) — caller's float==string
        # comparison still works to trigger one re-sync if needed.
        assert result == 9999.0

    async def test_get_mtime_falls_back_to_cache_on_api_failure(self, monkeypatch):
        """Transient ABS outage → return the cached value, not 0.
        Returning 0 would force a full re-sync on the next tick after
        ABS recovers."""
        from app.library_apps import audiobookshelf as abs_mod

        async def fake_get_key():
            return "tok"
        monkeypatch.setattr(abs_mod, "_get_abs_api_key", fake_get_key)

        async def boom(self):
            raise httpx.ConnectError("down", request=None)
        monkeypatch.setattr(abs_mod.AudiobookshelfClient, "list_libraries", boom)

        lib = {
            "abs_base_url": "http://abs:13378",
            "abs_library_id": "lib-book",
            "abs_last_update": 1234,
        }
        assert await abs_mod.AudiobookshelfApp().get_mtime(lib) == 1234.0

    async def test_get_mtime_missing_returns_zero(self):
        from app.library_apps.audiobookshelf import AudiobookshelfApp
        assert await AudiobookshelfApp().get_mtime({}) == 0.0

    async def test_get_mtime_no_api_key_falls_back_to_cache(self, monkeypatch):
        from app.library_apps import audiobookshelf as abs_mod

        async def no_key():
            return None
        monkeypatch.setattr(abs_mod, "_get_abs_api_key", no_key)

        lib = {
            "abs_base_url": "http://abs:13378",
            "abs_library_id": "lib-book",
            "abs_last_update": 1234,
        }
        assert await abs_mod.AudiobookshelfApp().get_mtime(lib) == 1234.0
