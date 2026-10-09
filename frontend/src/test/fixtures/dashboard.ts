// Unified Dashboard fixtures: both libraries, a healthy pipeline, a
// part-used snatch budget. MAM identity is the repo's placeholder.
import { AUDIO_SLUG, EBOOK_SLUG, idleScans, runningScans } from "./common";
import { cacheStatusRoutes } from "./cacheStatus";

const ebookStats = {
  owned_books: 1840,
  total_books: 2310,
  missing_books: 412,
  new_books: 23,
  upcoming_books: 35,
  total_series: 388,
  authors: 640,
  hidden_books: 19,
  suggestions: 4,
  library_name: "Calibre Library",
  library_display_name: "Calibre Library",
  content_type: "ebook",
  mam: { upload_candidates: 7, available_to_download: 160, missing_everywhere: 210, total_unscanned: 42 },
};

const audioStats = {
  owned_books: 830,
  total_books: 960,
  missing_books: 120,
  new_books: 6,
  upcoming_books: 4,
  total_series: 140,
  authors: 210,
  library_name: "Audiobookshelf",
  library_display_name: "Audiobookshelf",
  content_type: "audiobook",
  total_duration_sec: 31_000_000,
  narrator_count: 310,
  unabridged_count: 815,
};

export const dashboardRoutes = {
  "GET /discovery/stats": ebookStats,
  [`GET /discovery/stats?slug=${EBOOK_SLUG}`]: ebookStats,
  [`GET /discovery/stats?slug=${AUDIO_SLUG}`]: audioStats,
  "GET /health": { dispatcher_ready: true },
  "GET /v1/mam/status": {
    enabled: true,
    validation_ok: true,
    username: "ExampleMouse",
    classname: "Power User",
    ratio: 4.21,
    wedges: 12,
    seedbonus: 48_210,
    upload_buffer_bytes: 512 * 1024 ** 3,
    uploaded_bytes: 2_100 * 1024 ** 3,
    downloaded_bytes: 499 * 1024 ** 3,
    cookie_configured: true,
  },
  "GET /v1/grabs/budget": {
    budget_used: 38,
    budget_cap: 200,
    next_release_seconds: 5400,
    ledger_active: 38,
    qbit_extras: 1,
    queue_size: 0,
    seed_seconds_required: 259_200,
    entries: [
      { grab_id: 9101, torrent_name: "The Glass Orchard", source: "irc", seeding_seconds: 86_400, remaining_seconds: 172_800 },
    ],
  },
  "GET /v1/review": { pending_count: 2, items: [] },
  "GET /v1/tentative": { items: [{ id: 61 }, { id: 62 }] },
  "GET /v1/data/counts": { authors_allowed: 412, authors_ignored: 38, grabs: 4673, calibre_additions: 1851 },
  "GET /v1/grabs/recent": {
    grabs: [
      { torrent_name: "The Glass Orchard", grabbed_at: "2026-10-09 14:51:23" },
      { torrent_name: "Saltwind", grabbed_at: "2026-10-09 12:46:02" },
    ],
  },
  "GET /v1/settings": {
    cwa_web_url: "https://books.example.invalid",
    abs_web_url: "https://abs.example.invalid",
  },
  "GET /discovery/scan-status": idleScans,
  ...cacheStatusRoutes,
};

export const dashboardScanningRoutes = {
  ...dashboardRoutes,
  "GET /discovery/scan-status": runningScans,
};
