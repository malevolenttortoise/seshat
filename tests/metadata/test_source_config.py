"""
Tests for the Phase 7 unified metadata source configuration.

Exercises `migrate_legacy_settings`, `derive_enrich_priority`,
`derive_scan_priority`, and `sync_legacy_keys` against a range of
settings shapes: fresh install, legacy settings, partial legacy,
and already-migrated (idempotent).
"""
from app.metadata.source_config import (
    KNOWN_SOURCES,
    derive_enrich_priority,
    derive_scan_priority,
    get_source_rate_limit,
    migrate_legacy_settings,
    sync_legacy_keys,
)


class TestMigration:
    def test_fresh_install_seeds_defaults(self):
        """No legacy keys → ship-with defaults, every known source
        present, MAM pinned first in both priority lists."""
        settings: dict = {}
        assert migrate_legacy_settings(settings) is True

        sources = settings["metadata_sources"]
        priority = settings["metadata_priority"]

        # Every known source has an entry.
        assert set(sources.keys()) == set(KNOWN_SOURCES.keys())

        # MAM pinned first in both content types.
        assert priority["ebook"][0] == "mam"
        assert priority["audiobook"][0] == "mam"

        # Shipped defaults: Audible on for audiobook, off for ebook.
        assert sources["audible"]["audiobook_enrich"] is True
        assert sources["audible"]["ebook_enrich"] is False

        # Google Books off for audiobook enrich (rate-limited, no
        # audiobook fields).
        assert sources["google_books"]["audiobook_enrich"] is False
        assert sources["google_books"]["ebook_enrich"] is True

    def test_legacy_priority_preserved(self):
        """When the legacy priority list is populated, migration
        preserves its order — MAM gets prepended if absent.
        Retired source names (audnexus) are scrubbed."""
        settings = {
            "metadata_provider_priority": ["goodreads", "hardcover", "amazon"],
            # Legacy list that includes the retired audnexus entry —
            # scrub should drop it but preserve the rest.
            "metadata_audiobook_priority": ["audible", "audnexus", "hardcover"],
        }
        migrate_legacy_settings(settings)
        assert settings["metadata_priority"]["ebook"] == [
            "mam", "goodreads", "hardcover", "amazon",
        ]
        assert settings["metadata_priority"]["audiobook"] == [
            "mam", "audible", "hardcover",
        ]

    def test_legacy_enabled_flags_respected(self):
        """`<name>_enabled` → per-source scan toggles honour availability."""
        settings = {
            "goodreads_enabled": True,
            "kobo_enabled": False,
            "audible_enabled": True,  # ebook-unavailable — should not flip ebook_scan
        }
        migrate_legacy_settings(settings)
        sources = settings["metadata_sources"]

        # Goodreads available for both, scan flipped on both surfaces.
        assert sources["goodreads"]["ebook_scan"] is True
        assert sources["goodreads"]["audiobook_scan"] is True

        # Kobo ebook-only, scan explicitly off.
        assert sources["kobo"]["ebook_scan"] is False
        assert sources["kobo"]["audiobook_scan"] is False

        # Audible audiobook-only — legacy bool was True, but
        # availability restricts ebook_scan to False regardless.
        assert sources["audible"]["ebook_scan"] is False
        assert sources["audible"]["audiobook_scan"] is True

    def test_source_without_legacy_key_keeps_shipped_defaults(self):
        """Sources that never had a legacy `*_enabled` key should
        inherit the per-surface ship-with default, not collapse to
        False via a single-bool fallback.

        Originally written against audnexus (now retired); still
        valuable as a regression guard using audible instead, since
        it exhibits the same "new audiobook-surface source with no
        legacy bool" shape.
        """
        settings = {
            # Plausible legacy state: goodreads & hardcover mentioned,
            # audible completely absent.
            "goodreads_enabled": True,
            "hardcover_enabled": True,
        }
        migrate_legacy_settings(settings)
        audible = settings["metadata_sources"]["audible"]
        # Ship-with default has audiobook_scan=True; preserved for
        # sources the user hadn't explicitly touched.
        assert audible["audiobook_scan"] is True
        # audible's ebook_scan default is False (audiobook-only
        # `available_for`) — stays at the ship-with default.
        assert audible["ebook_scan"] is False

    def test_legacy_rate_limits_carried_over(self):
        settings = {
            "rate_goodreads": 1.5,
            "rate_hardcover": 0.5,
            "rate_audible": 0.25,
        }
        migrate_legacy_settings(settings)
        assert settings["metadata_sources"]["goodreads"]["rate_limit"] == 1.5
        assert settings["metadata_sources"]["hardcover"]["rate_limit"] == 0.5
        assert settings["metadata_sources"]["audible"]["rate_limit"] == 0.25
        # Unspecified sources fall back to KNOWN_SOURCES defaults.
        assert settings["metadata_sources"]["kobo"]["rate_limit"] == 3.0

    def test_idempotent_when_already_migrated(self):
        """Running twice is a no-op."""
        settings: dict = {}
        assert migrate_legacy_settings(settings) is True
        snapshot_sources = dict(settings["metadata_sources"])
        assert migrate_legacy_settings(settings) is False
        assert settings["metadata_sources"] == snapshot_sources

    def test_retired_sources_scrubbed_from_existing_settings(self):
        """Settings.json from an older build that still carries
        `audnexus` in both the unified shape and the legacy priority
        lists should come out clean on the next migration pass."""
        settings = {
            "metadata_sources": {
                "audnexus": {"rate_limit": 1.0, "audiobook_enrich": True},
                "audible": {"rate_limit": 0.5, "audiobook_enrich": True},
            },
            "metadata_priority": {
                "ebook": ["mam", "audnexus", "goodreads"],
                "audiobook": ["mam", "audible", "audnexus"],
            },
            # A user whose legacy list still named audnexus, forcing
            # the seeder to scrub there too.
            "metadata_audiobook_priority": ["audible", "audnexus"],
        }
        migrate_legacy_settings(settings)
        sources = settings["metadata_sources"]
        priority = settings["metadata_priority"]
        assert "audnexus" not in sources
        assert "audnexus" not in priority["ebook"]
        assert "audnexus" not in priority["audiobook"]
        # Non-retired entries preserved.
        assert "audible" in sources
        assert "audible" in priority["audiobook"]

    def test_migration_enabled_means_in_priority_list(self):
        """A source present in the legacy priority list gets
        `ebook_enrich=True` automatically."""
        settings = {
            "metadata_provider_priority": ["goodreads", "amazon"],
            "metadata_audiobook_priority": ["audible"],
        }
        migrate_legacy_settings(settings)
        sources = settings["metadata_sources"]
        # Goodreads was in the ebook priority list → enrich on.
        assert sources["goodreads"]["ebook_enrich"] is True
        # Amazon was in the ebook priority list → enrich on.
        assert sources["amazon"]["ebook_enrich"] is True
        # Hardcover was NOT in either list → enrich off.
        assert sources["hardcover"]["ebook_enrich"] is False
        assert sources["hardcover"]["audiobook_enrich"] is False
        # Audible in audiobook priority → audiobook_enrich on, ebook off.
        assert sources["audible"]["ebook_enrich"] is False
        assert sources["audible"]["audiobook_enrich"] is True


class TestDerivation:
    def _seeded_settings(self) -> dict:
        settings: dict = {}
        migrate_legacy_settings(settings)
        return settings

    def test_enrich_priority_filters_by_toggle(self):
        """Sources with `*_enrich=False` drop out of the live list."""
        settings = self._seeded_settings()
        settings["metadata_sources"]["goodreads"]["ebook_enrich"] = False
        result = derive_enrich_priority(settings, audiobook=False)
        assert "goodreads" not in result
        # mam still there (enrich default true).
        assert result[0] == "mam"

    def test_enrich_priority_preserves_order(self):
        """List order mirrors `metadata_priority` ranking."""
        settings = self._seeded_settings()
        settings["metadata_priority"]["ebook"] = [
            "hardcover", "mam", "goodreads",
        ]
        # Every row enabled — result matches the new order.
        for name in ("hardcover", "mam", "goodreads"):
            settings["metadata_sources"][name]["ebook_enrich"] = True
        result = derive_enrich_priority(settings, audiobook=False)
        assert result[:3] == ["hardcover", "mam", "goodreads"]

    def test_audiobook_vs_ebook_lists_independent(self):
        settings = self._seeded_settings()
        # Audible off for ebook enrich (already default), on for audiobook.
        assert "audible" not in derive_enrich_priority(
            settings, audiobook=False,
        )
        assert "audible" in derive_enrich_priority(
            settings, audiobook=True,
        )

    def test_scan_priority_uses_scan_flags(self):
        """Scan and enrich share the priority order but filter
        on different per-source flags."""
        settings = self._seeded_settings()
        # Turn goodreads scan off but leave enrich on.
        settings["metadata_sources"]["goodreads"]["ebook_scan"] = False
        assert "goodreads" in derive_enrich_priority(
            settings, audiobook=False,
        )
        assert "goodreads" not in derive_scan_priority(
            settings, audiobook=False,
        )

    def test_missing_source_entry_excluded(self):
        """A name in the priority list without a matching source
        entry is silently dropped from the derived list."""
        settings = self._seeded_settings()
        settings["metadata_priority"]["ebook"] = [
            "mam", "goodreads", "bogus_source",
        ]
        result = derive_enrich_priority(settings, audiobook=False)
        assert "bogus_source" not in result

    def test_rate_limit_lookup(self):
        settings = self._seeded_settings()
        settings["metadata_sources"]["goodreads"]["rate_limit"] = 7.5
        assert get_source_rate_limit(settings, "goodreads") == 7.5
        # Unknown name falls back to 1.0.
        assert get_source_rate_limit(settings, "unknown") == 1.0


class TestSyncLegacy:
    """Post-Phase-7 sync_legacy_keys is narrowed to one responsibility:
    mirror the MAM rate_limit from the unified `metadata_sources`
    shape onto the standalone `rate_mam` key, since `rate_mam` still
    has ~7 non-metadata-source call sites that weren't migrated.
    Every other legacy mirror (goodreads_enabled, rate_goodreads,
    metadata_provider_priority, etc.) was retired alongside the
    DEFAULT_SETTINGS cleanup."""

    def test_mirrors_rate_mam_from_panel(self):
        settings: dict = {}
        migrate_legacy_settings(settings)
        settings["metadata_sources"]["mam"]["rate_limit"] = 3.5
        sync_legacy_keys(settings)
        assert settings["rate_mam"] == 3.5

    def test_no_op_when_mam_rate_missing(self):
        """Missing MAM rate_limit shouldn't stamp a default onto
        settings — leave rate_mam to its own default elsewhere."""
        settings: dict = {}
        migrate_legacy_settings(settings)
        # Explicitly delete rate_limit to confirm we don't write.
        settings["metadata_sources"]["mam"].pop("rate_limit", None)
        settings.pop("rate_mam", None)
        sync_legacy_keys(settings)
        assert "rate_mam" not in settings

    def test_mam_enabled_is_not_clobbered(self):
        """`mam_enabled` is a wider-scope toggle (IRC listener gate),
        not just a source toggle. sync_legacy_keys must NOT touch it."""
        settings = {"mam_enabled": False}
        migrate_legacy_settings(settings)
        settings["metadata_sources"]["mam"]["ebook_scan"] = True
        sync_legacy_keys(settings)
        # The wider IRC gate stays False.
        assert settings["mam_enabled"] is False
