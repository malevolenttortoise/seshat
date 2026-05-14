"""
Tests for the metadata-sources router
(v2.11.0 Stage 5++ commit 6/6).

Targets the Stage 5++ additions: Amazon's `format` + `language`
config strings round-trip cleanly through GET + PUT
`/api/v1/metadata-sources`. Other sources' rows never carry these
fields (they're Amazon-private).
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI

from app import config
from app.routers.metadata_sources import router as metadata_sources_router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(metadata_sources_router)
    return app


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point config.SETTINGS_PATH at a tmp_path settings.json,
    pre-migrated via source_config.migrate_legacy_settings so
    metadata_sources is populated like a real running install."""
    from app.metadata.source_config import migrate_legacy_settings
    p = tmp_path / "settings.json"
    seed = dict(config.DEFAULT_SETTINGS)
    migrate_legacy_settings(seed)
    p.write_text(json.dumps(seed))
    monkeypatch.setattr(config, "SETTINGS_PATH", p)
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = object()
    yield p
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = object()


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


class TestAmazonFormatLanguageRoundTrip:
    """Stage 5++ — Amazon row carries `format` + `language` strings
    that the frontend dropdowns drive."""

    async def test_get_returns_amazon_defaults(self, isolated_settings):
        """Fresh install seeds amazon.format='kindle' +
        language='English' via `_DEFAULT_NEW_INSTALL_STATE` (commit 1).
        The GET endpoint should reflect that."""
        async with await _client(_make_app()) as ac:
            resp = await ac.get("/api/v1/metadata-sources")
        assert resp.status_code == 200
        payload = resp.json()
        amazon = payload["state"]["sources"]["amazon"]
        assert amazon["format"] == "kindle"
        assert amazon["language"] == "English"

    async def test_get_returns_null_for_non_amazon_sources(
        self, isolated_settings,
    ):
        """format/language are Amazon-only — every other source's
        row should have them as None."""
        async with await _client(_make_app()) as ac:
            resp = await ac.get("/api/v1/metadata-sources")
        payload = resp.json()
        for name, entry in payload["state"]["sources"].items():
            if name == "amazon":
                continue
            assert entry.get("format") is None, (
                f"{name!r} should not carry Amazon-specific format"
            )
            assert entry.get("language") is None, (
                f"{name!r} should not carry Amazon-specific language"
            )

    async def test_put_persists_amazon_format(self, isolated_settings):
        """User picks Paperback in the UI → PUT round-trips through
        the settings store and a subsequent GET returns the new
        value."""
        async with await _client(_make_app()) as ac:
            resp = await ac.get("/api/v1/metadata-sources")
            state = resp.json()["state"]
            state["sources"]["amazon"]["format"] = "paperback"
            state["sources"]["amazon"]["language"] = "Spanish"
            put = await ac.put("/api/v1/metadata-sources", json=state)
            assert put.status_code == 200, put.text
            resp2 = await ac.get("/api/v1/metadata-sources")
        amazon = resp2.json()["state"]["sources"]["amazon"]
        assert amazon["format"] == "paperback"
        assert amazon["language"] == "Spanish"

    async def test_put_accepts_all_formats(self, isolated_settings):
        """All four supported formats + the unfiltered allFormats
        value should be accepted by the PUT validator."""
        async with await _client(_make_app()) as ac:
            resp = await ac.get("/api/v1/metadata-sources")
            state = resp.json()["state"]
            for fmt in ("kindle", "paperback", "hardcover",
                        "mass_market", "allFormats"):
                state["sources"]["amazon"]["format"] = fmt
                put = await ac.put("/api/v1/metadata-sources", json=state)
                assert put.status_code == 200, (
                    f"PUT with format={fmt!r} unexpectedly rejected: "
                    f"{put.text}"
                )

    async def test_put_preserves_other_fields(self, isolated_settings):
        """Setting format/language must not corrupt the other
        per-source toggles."""
        async with await _client(_make_app()) as ac:
            resp = await ac.get("/api/v1/metadata-sources")
            state = resp.json()["state"]
            state["sources"]["amazon"]["format"] = "hardcover"
            state["sources"]["amazon"]["ebook_scan"] = False
            state["sources"]["amazon"]["rate_limit"] = 45.0
            put = await ac.put("/api/v1/metadata-sources", json=state)
            assert put.status_code == 200, put.text
            resp2 = await ac.get("/api/v1/metadata-sources")
        amazon = resp2.json()["state"]["sources"]["amazon"]
        assert amazon["format"] == "hardcover"
        assert amazon["ebook_scan"] is False
        assert amazon["rate_limit"] == 45.0


class TestPutReloadsDiscoverySources:
    """v2.11.1 N9 — the PUT handler must call
    `app.discovery.lookup.reload_sources()` so a Rate field change
    actually reaches the running discovery scanner without a
    container restart.

    UAT 2026-05-13: Mark bumped Amazon Rate 3 → 30 via the panel,
    saved, then triggered a Sanderson scan. The scan's first
    Amazon request fired in ~4 seconds (rate=3 + jitter), not the
    expected ~30s, because the running `amazon` singleton retained
    its startup-time rate. The dispatcher rebuild only covered the
    enricher path; the discovery-side singletons in lookup.py were
    untouched."""

    async def test_put_propagates_amazon_rate_to_singleton(
        self, isolated_settings,
    ):
        from app.discovery import lookup as lookup_module

        # Pre-PUT: rebuild from settings so the test starts from a
        # known-good baseline matching what app startup would do.
        lookup_module.reload_sources()
        baseline_rate = lookup_module.amazon.rate_limit

        async with await _client(_make_app()) as ac:
            resp = await ac.get("/api/v1/metadata-sources")
            state = resp.json()["state"]
            target_rate = baseline_rate + 27.0  # any distinct value
            state["sources"]["amazon"]["rate_limit"] = target_rate
            put = await ac.put("/api/v1/metadata-sources", json=state)
            assert put.status_code == 200, put.text

        # The PUT handler should have re-instantiated the discovery
        # singletons; the live `amazon` instance now carries the new
        # rate.
        assert lookup_module.amazon.rate_limit == target_rate, (
            f"reload_sources didn't propagate the saved rate_limit. "
            f"Settings: {target_rate}, runtime: "
            f"{lookup_module.amazon.rate_limit}"
        )

    async def test_put_propagates_amazon_format_to_singleton(
        self, isolated_settings,
    ):
        """The format/language config from Stage 5++ also flows
        through reload_sources — bumping the Format dropdown should
        affect the next scan."""
        from app.discovery import lookup as lookup_module
        lookup_module.reload_sources()

        async with await _client(_make_app()) as ac:
            resp = await ac.get("/api/v1/metadata-sources")
            state = resp.json()["state"]
            state["sources"]["amazon"]["format"] = "paperback"
            put = await ac.put("/api/v1/metadata-sources", json=state)
            assert put.status_code == 200, put.text

        assert lookup_module.amazon.format_filter == "paperback"


class TestKoboConcurrency:
    """v2.11.1 N5 — Kobo's parallel detail-fetch worker count is
    exposed via `metadata_sources.kobo.concurrency`. Round-trips
    GET/PUT and flows through reload_sources to the live
    `lookup.kobo` singleton without a container restart."""

    async def test_get_returns_kobo_concurrency_default(
        self, isolated_settings,
    ):
        """Fresh install seeds kobo.concurrency=4 via
        `_DEFAULT_NEW_INSTALL_STATE`."""
        async with await _client(_make_app()) as ac:
            resp = await ac.get("/api/v1/metadata-sources")
        kobo = resp.json()["state"]["sources"]["kobo"]
        assert kobo["concurrency"] == 4

    async def test_concurrency_null_for_other_sources(
        self, isolated_settings,
    ):
        """concurrency is Kobo-specific — every other source's row
        should have it as None."""
        async with await _client(_make_app()) as ac:
            resp = await ac.get("/api/v1/metadata-sources")
        for name, entry in resp.json()["state"]["sources"].items():
            if name == "kobo":
                continue
            assert entry.get("concurrency") is None, (
                f"{name!r} should not carry Kobo-specific concurrency"
            )

    async def test_put_persists_concurrency(self, isolated_settings):
        """User bumps Kobo concurrency 4 → 8 via the panel; PUT
        round-trips through settings + a subsequent GET reports
        the new value."""
        async with await _client(_make_app()) as ac:
            resp = await ac.get("/api/v1/metadata-sources")
            state = resp.json()["state"]
            state["sources"]["kobo"]["concurrency"] = 8
            put = await ac.put("/api/v1/metadata-sources", json=state)
            assert put.status_code == 200, put.text
            resp2 = await ac.get("/api/v1/metadata-sources")
        assert resp2.json()["state"]["sources"]["kobo"]["concurrency"] == 8

    async def test_put_propagates_concurrency_to_singleton(
        self, isolated_settings,
    ):
        """N9 + N5 interplay: after a concurrency PUT, the live
        `lookup.kobo` singleton's `concurrency` attribute reflects
        the saved value — proves the reload chain works for the new
        config dimension."""
        from app.discovery import lookup as lookup_module
        lookup_module.reload_sources()
        assert lookup_module.kobo.concurrency == 4  # baseline

        async with await _client(_make_app()) as ac:
            resp = await ac.get("/api/v1/metadata-sources")
            state = resp.json()["state"]
            state["sources"]["kobo"]["concurrency"] = 6
            put = await ac.put("/api/v1/metadata-sources", json=state)
            assert put.status_code == 200, put.text

        assert lookup_module.kobo.concurrency == 6
