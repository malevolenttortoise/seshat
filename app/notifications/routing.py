"""Per-event routing resolution (Bundle B.2 — Phase 2).

Settings shape:

    {
      "notifications": {
        "events": {
          "grab.success":   {"enabled": true,  "topic": "seshat-grabs"},
          "grab.*":         {"topic": "seshat-grabs", "priority": 4},
          "discovery.*":    {"topic": "seshat-scans"},
          "*":              {"topic": "seshat-misc"}
        }
      }
    }

Field resolution for any per-event field (``enabled``, ``topic``,
``priority``, ...) follows the same precedence:

  1. Exact key match (``grab.success``) — most specific, always wins.
  2. Longest matching prefix wildcard (``grab.*`` over ``*``).
  3. Universal ``*`` key (lowest specificity).
  4. Caller-supplied default.

Wildcards are restricted to the ``prefix.*`` and ``*`` forms — no
mid-name or suffix wildcards. A trailing ``.*`` matches the prefix
itself AND any deeper dotted name beneath it (``grab.*`` matches
``grab.success`` and a hypothetical ``grab.success.retry``).

The ``resolve_url_and_topic`` helper additionally normalizes
``ntfy_url`` + ``ntfy_topic`` so the URL is always bare (no path
component) and the topic is always explicit — that way a routing
override actually wins even when the user has the topic baked into
the URL path.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse, urlunparse


_SENTINEL = object()


def resolve_event_field(
    event_type: str,
    settings: dict,
    field: str,
    default: Any = None,
) -> Any:
    """Resolve a per-event field from the new ``notifications.events``
    shape.

    Returns ``default`` when no exact / wildcard / universal entry
    supplies the field.

    Wildcard matching:
      - ``"<prefix>.*"`` matches ``event_type`` iff it equals
        ``<prefix>`` or begins with ``<prefix>.``
      - ``"*"`` is the universal fallback
      - Most-specific (longest prefix) wins; ``"*"`` only applies when
        no prefix wildcard matched
    """
    events_cfg = (settings.get("notifications") or {}).get("events") or {}
    if not isinstance(events_cfg, dict):
        return default

    # 1. Exact match wins.
    exact = events_cfg.get(event_type)
    if isinstance(exact, dict) and field in exact and exact[field] is not None:
        return exact[field]

    # 2. Longest-prefix wildcard.
    best_value: Any = _SENTINEL
    best_prefix_len = -1
    universal: Any = _SENTINEL

    for key, cfg in events_cfg.items():
        if not isinstance(cfg, dict):
            continue
        if field not in cfg or cfg[field] is None:
            continue
        if key == "*":
            universal = cfg[field]
            continue
        if not key.endswith(".*"):
            continue
        prefix = key[:-2]
        if not prefix:
            # Treat ``.*`` like ``*`` rather than matching everything
            # via empty-prefix arithmetic.
            universal = cfg[field]
            continue
        if event_type == prefix or event_type.startswith(prefix + "."):
            if len(prefix) > best_prefix_len:
                best_prefix_len = len(prefix)
                best_value = cfg[field]

    if best_value is not _SENTINEL:
        return best_value
    if universal is not _SENTINEL:
        return universal
    return default


def resolve_topic(
    event_type: str,
    settings: dict,
    default_topic: str,
) -> str:
    """Resolve the ntfy topic for ``event_type``.

    Falls back to ``default_topic`` when no routing rule applies.
    """
    routed = resolve_event_field(event_type, settings, "topic", default=None)
    if routed:
        return str(routed)
    return default_topic


def _split_url_topic(url: str) -> tuple[str, str]:
    """Split ``ntfy_url`` into ``(bare_url, embedded_topic)``.

    When the URL has a path component (e.g.
    ``https://ntfy.example.com/seshat``), the path's first segment is
    treated as an embedded topic and stripped from the URL. Returning
    a bare URL lets the bus pass an explicit topic to ``ntfy.send()``
    so a routing override always takes effect, even when the user has
    the default topic baked into the URL path.
    """
    if not url:
        return "", ""
    raw = url.strip()
    if not raw:
        return "", ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.path or parsed.path == "/":
        return url, ""
    embedded = parsed.path.strip("/").split("/", 1)[0]
    bare = urlunparse(parsed._replace(path=""))
    return bare, embedded


def resolve_url_and_topic(
    event_type: str,
    settings: dict,
) -> tuple[str, str]:
    """Resolve the ``(url, topic)`` pair to use when sending
    ``event_type``.

    Always returns a bare server URL (no embedded topic) and an
    explicit topic string. The topic is, in order of precedence:

      1. The per-event routing override (exact / wildcard / universal).
      2. Any topic embedded in ``ntfy_url``'s path
         (backwards compatibility with pre-v2.28.0 single-topic
         configurations).
      3. The ``ntfy_topic`` setting.
      4. Empty string (caller must treat this as "no destination
         configured").
    """
    notif_url = settings.get("ntfy_url") or ""
    notif_topic = settings.get("ntfy_topic") or ""

    bare_url, embedded_topic = _split_url_topic(notif_url)
    default_topic = embedded_topic or notif_topic

    routed = resolve_event_field(event_type, settings, "topic", default=None)
    if routed:
        return bare_url, str(routed)
    return bare_url, default_topic


def resolve_enabled(
    event_type: str,
    settings: dict,
    default: Optional[bool] = None,
) -> Optional[bool]:
    """Resolve a per-event ``enabled`` flag from the new
    ``notifications.events`` shape.

    Returns ``None`` (or the supplied ``default``) when no entry
    supplies an explicit ``enabled`` — the caller then drops to the
    legacy ``notify_on_*`` / ``ntfy_on_*`` fallback.
    """
    value = resolve_event_field(event_type, settings, "enabled", default=_SENTINEL)
    if value is _SENTINEL:
        return default
    return bool(value)
