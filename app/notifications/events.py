"""Event taxonomy + registry for Seshat notifications (Bundle B.2).

Every notification the app can emit is catalogued here as an
``EventMeta`` entry in ``REGISTRY``. The bus reads this registry to
resolve defaults (priority, tags, quiet-hours suppressibility) and to
gate sends against legacy ``notify_on_*`` / ``ntfy_on_*`` settings
during the v2.28.0 migration window.

Names are hierarchical-dotted (``grab.success``,
``discovery.scan_complete``) so routing rules can match a whole prefix
(``grab.*``) in Phase 2. Add a new event by:

  1. Declaring a dotted-name constant below.
  2. Adding an ``EventMeta`` to ``_REGISTRY_ENTRIES``.
  3. Calling ``bus.emit(<name>, ...)`` from the producing call site.

``legacy_setting_key`` maps the new event back to its pre-v2.28.0
settings key, so the bus can keep honouring user choices made before
the new Settings UI ships. ``legacy_requires_master`` marks the
orchestrator-side events that historically also required the
``per_event_notifications`` master toggle — discovery, sync, and
digest events never required it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ─── Event name constants ────────────────────────────────────

# Grab events (autograb / IRC pickup pipeline).
GRAB_SUCCESS = "grab.success"
GRAB_BUFFER_BLOCKED = "grab.buffer_blocked"

# Pipeline events (post-download, pre-library).
PIPELINE_DOWNLOAD_COMPLETE = "pipeline.download_complete"
PIPELINE_REVIEW_QUEUED = "pipeline.review_queued"
PIPELINE_LIBRARY_INGEST = "pipeline.library_ingest"
PIPELINE_ERROR = "pipeline.error"

# Discovery events (source scanning, MAM matching).
DISCOVERY_SCAN_COMPLETE = "discovery.scan_complete"
DISCOVERY_NEW_BOOKS = "discovery.new_books"
DISCOVERY_MAM_COMPLETE = "discovery.mam_complete"
DISCOVERY_PIPELINE_SENT = "discovery.pipeline_sent"

# Sync events (library / cookie maintenance).
SYNC_LIBRARY = "sync.library"
SYNC_MAM_COOKIE_ROTATED = "sync.mam_cookie_rotated"

# Source-health events (Goodreads canary, metadata-cache worker).
SOURCE_GOODREADS_CANARY_FAILED = "source.goodreads_canary_failed"
SOURCE_METADATA_CACHE_ERROR = "source.metadata_cache_error"
SOURCE_METADATA_CACHE_WARNING = "source.metadata_cache_warning"
SOURCE_METADATA_CACHE_DAILY_SUMMARY = "source.metadata_cache_daily_summary"
SOURCE_METADATA_CACHE_NEW_BOOK = "source.metadata_cache_new_book"

# Digest summaries (scheduled, not event-driven, but catalogued so the
# Phase 5 Settings UI can toggle them through the same surface).
DIGEST_DAILY_ACCEPTED = "digest.daily_accepted"
DIGEST_DAILY_TENTATIVE = "digest.daily_tentative"
DIGEST_DAILY_IGNORED = "digest.daily_ignored"
DIGEST_WEEKLY = "digest.weekly"


@dataclass(frozen=True)
class EventMeta:
    """Static metadata for one notification event type.

    Held in ``REGISTRY`` keyed by ``name``. Read at send time to
    resolve default priority + tags, decide whether the event is
    suppressible during quiet hours (Phase 3), and route legacy
    backwards-compatibility gating (the bus consults
    ``legacy_setting_key`` when the user has not yet migrated to the
    new ``notifications.events.<name>`` shape).
    """
    name: str
    description: str
    default_priority: int = 3
    default_tags: tuple[str, ...] = ()
    suppressible_during_quiet_hours: bool = True
    legacy_setting_key: Optional[str] = None
    legacy_requires_master: bool = False
    legacy_default_enabled: bool = True


_REGISTRY_ENTRIES: tuple[EventMeta, ...] = (
    # ── Grab ────────────────────────────────────────────────
    EventMeta(
        name=GRAB_SUCCESS,
        description="A torrent was grabbed (autograb or manual).",
        default_tags=("books",),
        legacy_setting_key="notify_on_grab",
        legacy_requires_master=True,
    ),
    EventMeta(
        name=GRAB_BUFFER_BLOCKED,
        description="An autograb was refused by the buffer gate.",
        default_priority=4,
        default_tags=("no_entry_sign",),
        suppressible_during_quiet_hours=False,
        legacy_setting_key="notify_on_buffer_gate_block",
        legacy_requires_master=True,
    ),
    # ── Pipeline ────────────────────────────────────────────
    EventMeta(
        name=PIPELINE_DOWNLOAD_COMPLETE,
        description="A torrent download finished.",
        default_tags=("white_check_mark",),
        legacy_setting_key="notify_on_download_complete",
        legacy_requires_master=True,
    ),
    EventMeta(
        name=PIPELINE_REVIEW_QUEUED,
        description="A downloaded book entered the review queue.",
        default_tags=("books", "white_check_mark"),
        legacy_setting_key="notify_on_review_queued",
        legacy_requires_master=True,
    ),
    EventMeta(
        name=PIPELINE_LIBRARY_INGEST,
        description="A book landed in a library (Calibre / CWA / Audiobookshelf).",
        default_tags=("books", "white_check_mark"),
        legacy_setting_key="notify_on_library_ingest",
        legacy_requires_master=True,
    ),
    EventMeta(
        name=PIPELINE_ERROR,
        description="The post-download pipeline hit a fatal error.",
        default_priority=4,
        default_tags=("warning",),
        suppressible_during_quiet_hours=False,
        legacy_setting_key="notify_on_pipeline_error",
        legacy_requires_master=True,
    ),
    # ── Discovery ───────────────────────────────────────────
    EventMeta(
        name=DISCOVERY_SCAN_COMPLETE,
        description="A source or bulk scan finished.",
        default_tags=("books", "mag"),
        legacy_setting_key="ntfy_on_scan_complete",
    ),
    EventMeta(
        name=DISCOVERY_NEW_BOOKS,
        description="Per-author new-books summary inside a bulk scan.",
        default_tags=("books", "sparkles"),
        legacy_setting_key="ntfy_on_new_books",
    ),
    EventMeta(
        name=DISCOVERY_MAM_COMPLETE,
        description="A MAM scan finished (found / possible / not-found summary).",
        default_tags=("mag",),
        legacy_setting_key="ntfy_on_mam_complete",
    ),
    EventMeta(
        name=DISCOVERY_PIPELINE_SENT,
        description="Books were sent from discovery to the pipeline.",
        default_tags=("arrow_down", "books"),
        legacy_setting_key="ntfy_on_pipeline_sent",
    ),
    # ── Sync ────────────────────────────────────────────────
    EventMeta(
        name=SYNC_LIBRARY,
        description="A library finished syncing (Calibre / Audiobookshelf).",
        default_tags=("books",),
        legacy_setting_key="ntfy_on_library_sync",
        legacy_default_enabled=False,
    ),
    EventMeta(
        name=SYNC_MAM_COOKIE_ROTATED,
        description="The MAM session cookie was automatically refreshed.",
        default_priority=2,
        default_tags=("key",),
        legacy_setting_key="ntfy_on_mam_cookie_rotated",
        legacy_default_enabled=False,
    ),
    # ── Source health ───────────────────────────────────────
    EventMeta(
        name=SOURCE_GOODREADS_CANARY_FAILED,
        description="The weekly Goodreads canary detected a Cloudflare soft-block.",
        default_priority=4,
        default_tags=("warning",),
        suppressible_during_quiet_hours=False,
        legacy_setting_key="notify_on_goodreads_canary_failed",
        legacy_requires_master=True,
    ),
    EventMeta(
        name=SOURCE_METADATA_CACHE_ERROR,
        description="The metadata-cache worker hit a fatal error.",
        default_priority=4,
        default_tags=("warning",),
        suppressible_during_quiet_hours=False,
        legacy_setting_key="notify_on_metadata_cache_error",
        legacy_requires_master=True,
    ),
    EventMeta(
        name=SOURCE_METADATA_CACHE_WARNING,
        description="The metadata-cache worker logged a recoverable warning.",
        default_tags=("warning",),
        legacy_setting_key="notify_on_metadata_cache_warning",
        legacy_requires_master=True,
    ),
    EventMeta(
        name=SOURCE_METADATA_CACHE_DAILY_SUMMARY,
        description="Daily summary of the metadata-cache worker's activity.",
        default_tags=("books", "calendar"),
        legacy_setting_key="notify_on_metadata_cache_daily_summary",
        legacy_requires_master=True,
        legacy_default_enabled=False,
    ),
    EventMeta(
        name=SOURCE_METADATA_CACHE_NEW_BOOK,
        description="The metadata-cache worker discovered a previously-unseen book.",
        default_tags=("books", "sparkles"),
        legacy_setting_key="notify_on_metadata_cache_new_book",
        legacy_requires_master=True,
        legacy_default_enabled=False,
    ),
    # ── Digests ─────────────────────────────────────────────
    EventMeta(
        name=DIGEST_DAILY_ACCEPTED,
        description="Daily digest of accepted books.",
        default_tags=("books",),
        legacy_setting_key="notify_daily_accepted",
    ),
    EventMeta(
        name=DIGEST_DAILY_TENTATIVE,
        description="Daily digest of books awaiting tentative-review approval.",
        default_tags=("books", "mag"),
        legacy_setting_key="notify_daily_tentative",
    ),
    EventMeta(
        name=DIGEST_DAILY_IGNORED,
        description="Daily digest of ignored torrents.",
        default_tags=("books",),
        legacy_setting_key="notify_daily_ignored",
    ),
    EventMeta(
        name=DIGEST_WEEKLY,
        description="Weekly digest (author promotions + Calibre summary).",
        default_tags=("books", "calendar"),
        legacy_setting_key="notify_weekly_digest",
    ),
)


REGISTRY: dict[str, EventMeta] = {e.name: e for e in _REGISTRY_ENTRIES}


def get(event_type: str) -> Optional[EventMeta]:
    """Look up an ``EventMeta`` by event name. ``None`` if unknown."""
    return REGISTRY.get(event_type)


def all_event_names() -> list[str]:
    """All registered event names in declaration order."""
    return list(REGISTRY.keys())


def by_prefix(prefix: str) -> list[EventMeta]:
    """All events whose dotted-name starts with ``prefix.``

    ``by_prefix("grab")`` returns ``[grab.success, grab.buffer_blocked]``.
    A trailing ``.`` on the input is tolerated and stripped.
    """
    pfx = prefix.rstrip(".") + "."
    return [meta for name, meta in REGISTRY.items() if name.startswith(pfx)]
