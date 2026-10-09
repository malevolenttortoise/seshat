// Settings page fixtures. `settingsBlob.json` = the backend's DEFAULT_SETTINGS
// as GET /v1/settings returns it (secrets → `<key>_configured`), plus a
// few URLs so the page renders a configured install. (Not named
// `settings.json`: .gitignore drops every file of that name.)
import settings from "./settingsBlob.json";
import { cacheStatusCooldownRoutes, cacheStatusRoutes } from "./cacheStatus";
import { metadataSourcesRoutes } from "./metadataSources";
import { AUDIO_SLUG, EBOOK_SLUG } from "./common";

export const settingsRoutes: Record<string, unknown> = {
  "GET /v1/settings": settings,
  "GET /v1/credentials": {
    items: [
      { key: "mam_session_id", label: "MAM session", configured: true },
      { key: "qbit_password", label: "qBittorrent password", configured: true },
      { key: "hardcover_api_key", label: "Hardcover API key", configured: true },
      { key: "cwa_password", label: "CWA password", configured: false },
    ],
  },
  "GET /version": { short_sha: "98d6075" },
};

export { cacheStatusCooldownRoutes, cacheStatusRoutes };

// What the desktop sections' own panels load when opened (Quality,
// Active Replacement, Library, Data Management, Notifications, Metadata
// Sources), on top of the page's own requests.
export const settingsSectionRoutes: Record<string, unknown> = {
  ...settingsRoutes,
  ...metadataSourcesRoutes,
  "GET /quality/stats": {
    linked_torrents: 1210,
    extracted: 1104,
    missing: 106,
    by_source: { mediainfo: 640, description: 301, tags: 98, mixed: 65, none: 80, unavailable: 26 },
  },
  "GET /quality/backfill/status": {
    running: false,
    started_at: Date.parse("2026-10-08T02:00:00Z") / 1000,
    finished_at: Date.parse("2026-10-08T02:40:00Z") / 1000,
    processed: 1104,
    skipped: 80,
    failed: 26,
    total_at_start: 1210,
    current_torrent_id: null,
    last_error: null,
    cancel_requested: false,
  },
  "GET /quality/library-safety": {
    libraries: [
      {
        slug: EBOOK_SLUG, name: "Calibre Library", content_type: "ebook", library_path: "/calibre",
        safety: "safe", enabled: true, effective: true, auto_enact: false, auto_enact_effective: false,
      },
      {
        slug: AUDIO_SLUG, name: "Audiobookshelf", content_type: "audiobook", library_path: "/audiobooks",
        safety: "overlap", enabled: false, effective: false, auto_enact: false, auto_enact_effective: false,
      },
    ],
  },
  "GET /quality/replacement-opportunities/counts": { detected: 14, enacted: 3, dismissed: 2 },
  "GET /discovery/libraries": {
    libraries: [
      { slug: EBOOK_SLUG, name: "Calibre Library", display_name: "Calibre Library", content_type: "ebook" },
      { slug: AUDIO_SLUG, name: "Audiobookshelf", display_name: "Audiobookshelf", content_type: "audiobook" },
    ],
    active: EBOOK_SLUG,
  },
  "GET /discovery/mam/status": { enabled: true, validation_ok: true },
  "GET /v1/data/counts": {
    authors_allowed: 412, authors_ignored: 38, grabs: 4673, calibre_additions: 1851,
    announces: 9120, tentative_torrents: 54, review_queue: 2,
  },
  "GET /v1/notifications/events": {
    events: [
      {
        name: "grab.submitted", description: "A grab was sent to the download client", default_priority: 3,
        default_tags: ["inbox_tray"], suppressible_during_quiet_hours: true,
        legacy_setting_key: "notify_on_grab", legacy_requires_master: true, legacy_default_enabled: true,
      },
      {
        name: "pipeline.import_failed", description: "CWA didn't import a delivered ebook", default_priority: 4,
        default_tags: ["warning"], suppressible_during_quiet_hours: false,
        legacy_setting_key: null, legacy_requires_master: true, legacy_default_enabled: true,
      },
    ],
  },
};
