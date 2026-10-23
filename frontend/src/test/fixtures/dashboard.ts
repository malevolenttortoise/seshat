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

// "Clean up missing books" (discovery fix pass S8): the dialog's states.
// Times are seconds before the tests' fixed clock (FIXED_NOW).
const NOW_S = Date.parse("2026-10-09T16:00:00Z") / 1000;

const cleanupStep = (
  key: string, name: string, action: string, count: number,
  sample: Record<string, unknown>[] = [], skipped_count = 0,
) => ({ key, name, action, count, sample, skipped_count });

const cleanupReport = (mode: "preview" | "apply", id: string, finishedAgoS: number) => ({
  id,
  mode,
  started_at: NOW_S - finishedAgoS - 13,
  finished_at: NOW_S - finishedAgoS,
  ...(mode === "apply" ? { preview_id: "p1" } : {}),
  errors: [],
  steps: [
    cleanupStep("no_eligible_author", "No author you scan", "remove", 667, [
      { library: EBOOK_SLUG, id: 101, title: "Morganville 3", authors: ["Rachel Caine"], detail: "" },
    ]),
    cleanupStep("openlibrary_only", "Found only by OpenLibrary", "remove", 0),
    cleanupStep("filtered", "Comics, box sets, anthologies and other non-books", "hide", 1354, [
      { library: EBOOK_SLUG, id: 102, title: "Lucifer (2015-2017) #1", authors: ["Holly Black"], reason: "comic", stays_shown: false },
      { library: EBOOK_SLUG, id: 103, title: "Refugio (Spanish Edition)", authors: ["Ann Aguirre"], reason: "not_english", stays_shown: true },
    ]),
    cleanupStep("owned_copy", "Copies of books you own", "fold", 9, [
      { library: EBOOK_SLUG, id: 104, title: "Truthwitch: The Witchlands", authors: ["Susan Dennard"], into_id: 81, into_title: "Truthwitch" },
    ], 22),
    cleanupStep("series_in_title", "Series named in the title", "file", 72, [
      { library: EBOOK_SLUG, id: 105, title: "The Rainy Season (The Ghosts Trilogy Book 3)", authors: ["Tobias S. Buckell"], detail: "The Ghosts Trilogy #3 (new series)" },
    ]),
    cleanupStep("cache_remerge", "Amazon and Goodreads books merged again", "merge", 1158, [
      { library: EBOOK_SLUG, id: 50139, title: "Slate Star Codex Essays: Volume II", authors: ["Scott Alexander"], hidden: false },
      { library: EBOOK_SLUG, id: 50140, title: "X-Factor (2005-2013) #23", authors: ["Peter David"], hidden: true, hidden_reason: "comic" },
    ], 85),
  ],
});

const cleanupStatus = (over: Record<string, unknown> = {}) => ({
  running: false, mode: null, library: "", library_name: "", step: "", step_name: "",
  preview_ttl_s: 3600, last_preview: null, last_apply: null, ...over,
});

export const cleanupNeverRun = cleanupStatus();
export const cleanupPreviewed = cleanupStatus({ last_preview: cleanupReport("preview", "p1", 300) });
export const cleanupStale = cleanupStatus({ last_preview: cleanupReport("preview", "p1", 4000) });
export const cleanupRunning = cleanupStatus({
  running: true, mode: "preview", library: EBOOK_SLUG, library_name: "Calibre Library",
  step: "filtered", step_name: "Comics, box sets, anthologies and other non-books",
});
export const cleanupApplied = cleanupStatus({
  last_preview: cleanupReport("preview", "p1", 300),
  last_apply: cleanupReport("apply", "a1", 60),
});
