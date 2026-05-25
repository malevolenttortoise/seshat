"""Unit tests for the per-event routing resolver."""
from __future__ import annotations

from app.notifications import routing


# ─── resolve_event_field ─────────────────────────────────────


class TestResolveEventField:
    def test_exact_match_wins(self):
        s = {"notifications": {"events": {
            "grab.success": {"topic": "t-exact"},
            "grab.*": {"topic": "t-wildcard"},
            "*": {"topic": "t-universal"},
        }}}
        assert routing.resolve_event_field("grab.success", s, "topic") == "t-exact"

    def test_wildcard_match(self):
        s = {"notifications": {"events": {
            "grab.*": {"topic": "t-wildcard"},
        }}}
        assert routing.resolve_event_field("grab.success", s, "topic") == "t-wildcard"
        assert routing.resolve_event_field("grab.buffer_blocked", s, "topic") == "t-wildcard"

    def test_wildcard_matches_prefix_itself(self):
        """``grab.*`` matches the bare ``grab`` event name too."""
        s = {"notifications": {"events": {
            "grab.*": {"topic": "t"},
        }}}
        assert routing.resolve_event_field("grab", s, "topic") == "t"

    def test_wildcard_does_not_match_partial_segment(self):
        """``grab.*`` must NOT match ``grabby.something``."""
        s = {"notifications": {"events": {
            "grab.*": {"topic": "t"},
        }}}
        assert routing.resolve_event_field("grabby", s, "topic", default="d") == "d"
        assert routing.resolve_event_field("grabby.thing", s, "topic", default="d") == "d"

    def test_longest_prefix_wins(self):
        s = {"notifications": {"events": {
            "discovery.*": {"topic": "t-discovery"},
            "discovery.mam.*": {"topic": "t-mam"},
            "*": {"topic": "t-universal"},
        }}}
        assert routing.resolve_event_field(
            "discovery.mam.scan_complete", s, "topic"
        ) == "t-mam"

    def test_universal_fallback(self):
        s = {"notifications": {"events": {
            "*": {"topic": "t-universal"},
        }}}
        assert routing.resolve_event_field("anything.goes", s, "topic") == "t-universal"

    def test_universal_loses_to_prefix_wildcard(self):
        s = {"notifications": {"events": {
            "grab.*": {"topic": "t-grab"},
            "*": {"topic": "t-universal"},
        }}}
        assert routing.resolve_event_field("grab.success", s, "topic") == "t-grab"
        assert routing.resolve_event_field("other.event", s, "topic") == "t-universal"

    def test_no_match_returns_default(self):
        s = {"notifications": {"events": {}}}
        assert routing.resolve_event_field("anything", s, "topic", default="X") == "X"

    def test_missing_notifications_returns_default(self):
        assert routing.resolve_event_field("x", {}, "topic", default="D") == "D"
        assert routing.resolve_event_field("x", {"notifications": None}, "topic", default="D") == "D"

    def test_non_dict_event_value_ignored(self):
        """Garbage entries (a string instead of a dict) don't crash
        resolution — they just don't supply the field."""
        s = {"notifications": {"events": {
            "grab.success": "not a dict",
            "grab.*": {"topic": "t-wildcard"},
        }}}
        assert routing.resolve_event_field("grab.success", s, "topic") == "t-wildcard"

    def test_none_field_value_falls_through(self):
        """An entry whose field is ``None`` is treated as "no opinion"."""
        s = {"notifications": {"events": {
            "grab.success": {"topic": None},
            "grab.*": {"topic": "t-wildcard"},
        }}}
        assert routing.resolve_event_field("grab.success", s, "topic") == "t-wildcard"

    def test_field_absent_falls_through(self):
        """An entry that doesn't mention the field falls through to
        the wildcard."""
        s = {"notifications": {"events": {
            "grab.success": {"enabled": True},  # no topic
            "grab.*": {"topic": "t-wildcard"},
        }}}
        assert routing.resolve_event_field("grab.success", s, "topic") == "t-wildcard"

    def test_dot_star_is_universal(self):
        """``.*`` is treated as universal, not an empty-prefix match-
        everything monstrosity that would accidentally match other
        wildcards' suffixes."""
        s = {"notifications": {"events": {
            ".*": {"topic": "t-universal"},
        }}}
        assert routing.resolve_event_field("anything", s, "topic") == "t-universal"


# ─── resolve_topic ───────────────────────────────────────────


class TestResolveTopic:
    def test_returns_routed_when_present(self):
        s = {"notifications": {"events": {"grab.success": {"topic": "routed"}}}}
        assert routing.resolve_topic("grab.success", s, "default") == "routed"

    def test_falls_back_to_default(self):
        assert routing.resolve_topic("grab.success", {}, "default") == "default"

    def test_empty_routed_topic_uses_default(self):
        """An empty-string topic in routing config is ignored (treated
        as "no override")."""
        s = {"notifications": {"events": {"grab.success": {"topic": ""}}}}
        assert routing.resolve_topic("grab.success", s, "default") == "default"


# ─── resolve_enabled ─────────────────────────────────────────


class TestResolveEnabled:
    def test_exact_true(self):
        s = {"notifications": {"events": {"grab.success": {"enabled": True}}}}
        assert routing.resolve_enabled("grab.success", s) is True

    def test_exact_false(self):
        s = {"notifications": {"events": {"grab.success": {"enabled": False}}}}
        assert routing.resolve_enabled("grab.success", s) is False

    def test_wildcard_disables_all(self):
        s = {"notifications": {"events": {"grab.*": {"enabled": False}}}}
        assert routing.resolve_enabled("grab.success", s) is False
        assert routing.resolve_enabled("grab.buffer_blocked", s) is False

    def test_no_entry_returns_default(self):
        assert routing.resolve_enabled("grab.success", {}) is None
        assert routing.resolve_enabled("grab.success", {}, default=True) is True

    def test_exact_overrides_wildcard(self):
        s = {"notifications": {"events": {
            "grab.success": {"enabled": True},
            "grab.*": {"enabled": False},
        }}}
        assert routing.resolve_enabled("grab.success", s) is True
        assert routing.resolve_enabled("grab.buffer_blocked", s) is False


# ─── resolve_url_and_topic ───────────────────────────────────


class TestResolveUrlAndTopic:
    def test_basic_url_plus_topic_setting(self):
        s = {"ntfy_url": "https://ntfy.example.com", "ntfy_topic": "seshat"}
        assert routing.resolve_url_and_topic("grab.success", s) == (
            "https://ntfy.example.com", "seshat",
        )

    def test_topic_in_url_path_split_out(self):
        """When ntfy_url has a path, split it into bare URL + topic so
        an explicit routing override can win."""
        s = {"ntfy_url": "https://ntfy.example.com/seshat", "ntfy_topic": ""}
        url, topic = routing.resolve_url_and_topic("grab.success", s)
        assert url == "https://ntfy.example.com"
        assert topic == "seshat"

    def test_routing_override_wins_over_url_path(self):
        """The key bug Phase 2 is fixing: a URL with embedded topic
        used to silently swallow routing overrides."""
        s = {
            "ntfy_url": "https://ntfy.example.com/seshat",
            "notifications": {"events": {"grab.success": {"topic": "seshat-grabs"}}},
        }
        url, topic = routing.resolve_url_and_topic("grab.success", s)
        assert url == "https://ntfy.example.com"
        assert topic == "seshat-grabs"

    def test_routing_override_wins_over_topic_setting(self):
        s = {
            "ntfy_url": "https://ntfy.example.com",
            "ntfy_topic": "seshat",
            "notifications": {"events": {"grab.*": {"topic": "seshat-grabs"}}},
        }
        url, topic = routing.resolve_url_and_topic("grab.success", s)
        assert url == "https://ntfy.example.com"
        assert topic == "seshat-grabs"

    def test_no_url_returns_empty(self):
        url, topic = routing.resolve_url_and_topic("grab.success", {})
        assert url == ""
        assert topic == ""

    def test_url_without_scheme(self):
        """``ntfy.example.com/seshat`` (no scheme) — the splitter still
        recognizes the path. The bare URL is returned untouched
        for the no-path case to preserve existing ntfy._resolve_endpoint
        behavior."""
        s = {"ntfy_url": "ntfy.example.com/seshat", "ntfy_topic": ""}
        url, topic = routing.resolve_url_and_topic("grab.success", s)
        # bare URL gets the scheme prepended via urlparse normalization.
        assert url.endswith("ntfy.example.com")
        assert topic == "seshat"

    def test_url_with_trailing_slash_no_topic(self):
        s = {"ntfy_url": "https://ntfy.example.com/", "ntfy_topic": "seshat"}
        url, topic = routing.resolve_url_and_topic("grab.success", s)
        assert url == "https://ntfy.example.com/"
        assert topic == "seshat"
