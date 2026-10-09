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
