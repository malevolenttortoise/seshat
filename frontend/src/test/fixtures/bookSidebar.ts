// BookSidebar fixtures: a missing book MAM has (Send to pipeline + the
// "Use wedge" tick) and an owned book with a pending series suggestion.
import { book, orchardBooks } from "./common";
import { economyConfig } from "./economy";

export const missingOnMam = orchardBooks[1];

export const ownedWithSuggestion = book(104, "Ninefold Tide", {
  owned: 1,
  calibre_id: 1204,
  mam_status: "possible",
  description: "A tide that comes in nine times a day.",
  publisher: "Lantern House",
  isbn: "9780000000104",
  page_count: 288,
  source: "hardcover",
});

const common = {
  "GET /discovery/settings": { calibre_web_url: "https://books.example.invalid", abs_web_url: "https://abs.example.invalid" },
  "GET /discovery/pipeline/status": { configured: true, reachable: true },
  "GET /v1/mam/economy/config": economyConfig,
  "GET /discovery/mam/status": { enabled: true, validation_ok: true },
};

export const sidebarMissingRoutes = {
  ...common,
  "GET /discovery/series-suggestions/by-book/102": { suggestion: null },
};

export const sidebarOwnedRoutes = {
  ...common,
  "GET /discovery/series-suggestions/by-book/104": {
    suggestion: {
      id: 12,
      status: "pending",
      current_series_name: null,
      current_series_index: null,
      suggested_series_name: "Tidewater",
      suggested_series_index: 1,
      sources_agreeing: ["hardcover", "goodreads"],
    },
  },
};

// A missing book MAM says you already snatched: "Reingest from disk"
// instead of Send to pipeline.
export const snatchedOnMam = book(106, "The Pruning Hook", {
  owned: 0,
  mam_status: "found",
  mam_url: "https://example.invalid/t/900006",
  mam_torrent_id: "900006",
  mam_formats: "epub",
  mam_my_snatched: 1,
});

const candidate = (path: string, files: string[], size: number) => ({
  source: "qbit",
  display_path: path,
  save_path: `/downloads/${path}`,
  book_files: files,
  qbit_hash: null,
  mtime: Date.parse("2026-09-30T10:00:00Z") / 1000,
  total_size: size,
});

export const sidebarSnatchedRoutes = {
  ...common,
  "GET /discovery/series-suggestions/by-book/106": { suggestion: null },
};

// The probe found the snatch in two places: the picker.
export const reingestTwoCandidates = {
  found: true,
  candidates: [
    candidate("The Pruning Hook [epub]", ["The Pruning Hook.epub"], 1_800_000),
    candidate("Orchard Cycle 1-3", ["The Seed Vault.epub", "The Pruning Hook.epub"], 5_200_000),
  ],
  auto_started: false,
  grab_id: null,
  pipeline_run_id: null,
  error: null,
  searched: ["qBittorrent", "/downloads"],
  mam_torrent_name: "The Pruning Hook",
};

// The probe found nothing anywhere: the error line.
export const reingestNotFound = {
  found: false,
  candidates: [],
  auto_started: false,
  grab_id: null,
  pipeline_run_id: null,
  error: null,
  searched: ["qBittorrent", "/downloads"],
  mam_torrent_name: "The Pruning Hook",
};

// Not enough upload buffer for the torrent: the buffer banner.
export const preflightShort = {
  size_gb: 2.4,
  buffer_gb: 1.1,
  safety_margin_gb: 5,
  sufficient: false,
  shortfall_gb: 6.3,
  recommended_buy_gb: 10,
  recommended_buy_cost_bp: 5000,
};

// Two authors on one book: removable chips.
export const coauthored = book(107, "Two Hands Make Rope", {
  owned: 1,
  calibre_id: 1207,
  contributors: [
    { author_id: 11, name: "Ada Quill", position: 0, role: null },
    { author_id: 12, name: "Bram Okafor", position: 1, role: null },
  ],
});

export const sidebarCoauthoredRoutes = {
  ...common,
  "GET /discovery/series-suggestions/by-book/107": { suggestion: null },
};

export const compareNinefold = {
  book_id: 104,
  user_edited_fields: ["description"],
  calibre_synced_at: Date.parse("2026-10-08T22:00:00Z") / 1000,
  abs_synced_at: null,
  fields: [
    {
      field: "title", label: "Title", seshat: "Ninefold Tide", calibre: "Ninefold Tide", abs: null,
      calibre_diff: false, abs_diff: false, user_edited: false,
    },
    {
      field: "description", label: "Description", seshat: "A tide that comes in nine times a day.",
      calibre: "A tide.", abs: null, calibre_diff: true, abs_diff: false, user_edited: true,
    },
    {
      field: "publisher", label: "Publisher", seshat: "Lantern House", calibre: "Lantern Press", abs: null,
      calibre_diff: true, abs_diff: false, user_edited: false,
    },
  ],
};
