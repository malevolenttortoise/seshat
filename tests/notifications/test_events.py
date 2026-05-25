"""Shape tests for the notification event registry."""
from __future__ import annotations

from app.notifications import events


class TestRegistryShape:
    def test_all_names_are_dotted(self):
        for name in events.REGISTRY:
            assert "." in name, f"event name {name!r} is not dotted"

    def test_names_unique(self):
        names = list(events.REGISTRY.keys())
        assert len(names) == len(set(names))

    def test_legacy_setting_keys_unique(self):
        keys = [
            meta.legacy_setting_key
            for meta in events.REGISTRY.values()
            if meta.legacy_setting_key is not None
        ]
        assert len(keys) == len(set(keys)), f"duplicate legacy keys: {keys}"

    def test_registry_keys_match_meta_names(self):
        for key, meta in events.REGISTRY.items():
            assert key == meta.name

    def test_priorities_in_range(self):
        for meta in events.REGISTRY.values():
            assert 1 <= meta.default_priority <= 5

    def test_error_events_not_suppressible(self):
        """Errors must wake the operator even during quiet hours."""
        must_fire = {
            events.PIPELINE_ERROR,
            events.GRAB_BUFFER_BLOCKED,
            events.SOURCE_GOODREADS_CANARY_FAILED,
            events.SOURCE_METADATA_CACHE_ERROR,
        }
        for name in must_fire:
            meta = events.REGISTRY[name]
            assert meta.suppressible_during_quiet_hours is False, (
                f"{name} must not be suppressible during quiet hours"
            )

    def test_routine_events_are_suppressible(self):
        """Routine successes should fall under the quiet-hours gate."""
        routine = {
            events.GRAB_SUCCESS,
            events.PIPELINE_DOWNLOAD_COMPLETE,
            events.PIPELINE_LIBRARY_INGEST,
            events.DISCOVERY_SCAN_COMPLETE,
            events.SYNC_MAM_COOKIE_ROTATED,
        }
        for name in routine:
            meta = events.REGISTRY[name]
            assert meta.suppressible_during_quiet_hours is True

    def test_orchestrator_events_require_master(self):
        """Pre-v2.28.0, orchestrator events were gated by
        per_event_notifications. The compatibility layer must
        preserve that."""
        require_master = {
            events.GRAB_SUCCESS,
            events.GRAB_BUFFER_BLOCKED,
            events.PIPELINE_DOWNLOAD_COMPLETE,
            events.PIPELINE_REVIEW_QUEUED,
            events.PIPELINE_LIBRARY_INGEST,
            events.PIPELINE_ERROR,
            events.SOURCE_GOODREADS_CANARY_FAILED,
            events.SOURCE_METADATA_CACHE_ERROR,
            events.SOURCE_METADATA_CACHE_WARNING,
            events.SOURCE_METADATA_CACHE_DAILY_SUMMARY,
            events.SOURCE_METADATA_CACHE_NEW_BOOK,
        }
        for name in require_master:
            meta = events.REGISTRY[name]
            assert meta.legacy_requires_master is True, (
                f"{name} should require legacy master toggle"
            )

    def test_discovery_events_do_not_require_master(self):
        no_master = {
            events.DISCOVERY_SCAN_COMPLETE,
            events.DISCOVERY_NEW_BOOKS,
            events.DISCOVERY_MAM_COMPLETE,
            events.DISCOVERY_PIPELINE_SENT,
            events.SYNC_LIBRARY,
            events.SYNC_MAM_COOKIE_ROTATED,
            events.DIGEST_DAILY_ACCEPTED,
            events.DIGEST_DAILY_TENTATIVE,
            events.DIGEST_DAILY_IGNORED,
            events.DIGEST_WEEKLY,
        }
        for name in no_master:
            meta = events.REGISTRY[name]
            assert meta.legacy_requires_master is False


class TestRegistryLookups:
    def test_get_known(self):
        meta = events.get(events.GRAB_SUCCESS)
        assert meta is not None
        assert meta.name == events.GRAB_SUCCESS

    def test_get_unknown(self):
        assert events.get("does.not.exist") is None

    def test_all_event_names(self):
        names = events.all_event_names()
        assert events.GRAB_SUCCESS in names
        assert events.DIGEST_WEEKLY in names

    def test_by_prefix_grab(self):
        grab_events = events.by_prefix("grab")
        names = {e.name for e in grab_events}
        assert names == {events.GRAB_SUCCESS, events.GRAB_BUFFER_BLOCKED}

    def test_by_prefix_with_trailing_dot(self):
        a = {e.name for e in events.by_prefix("grab")}
        b = {e.name for e in events.by_prefix("grab.")}
        assert a == b

    def test_by_prefix_unknown_returns_empty(self):
        assert events.by_prefix("nope") == []

    def test_by_prefix_does_not_match_partial_segment(self):
        """``grabby`` must not match ``grab.*``."""
        assert all(
            not e.name.startswith("grabby")
            for e in events.by_prefix("grab")
        )
