// Review queue fixtures. Synthetic books; no real torrent IDs or MAM names.

const enrichedPending = {
  id: 501,
  grab_id: 9101,
  staged_path: "/review-staging/grab-9101",
  book_filename: "The Glass Orchard - Ada Quill.epub",
  book_format: "epub",
  metadata: {
    title: "The Glass Orchard",
    author: "Ada Quill",
    series: "Orchard Cycle",
    series_index: 2,
    enriched: {
      title: "The Glass Orchard",
      authors: ["Ada Quill"],
      description: "A botanist inherits a greenhouse that grows memories.",
      series: "Orchard Cycle",
      series_index: 2,
      isbn: "9780000000002",
      publisher: "Lantern House",
      pub_date: "2025-03-11",
      page_count: 412,
      cover_url: "https://example.invalid/covers/glass-orchard.jpg",
      source: "hardcover",
      source_url: "https://example.invalid/books/glass-orchard",
      confidence: 0.94,
      source_log: [
        { source: "goodreads", confidence: null, status: "skipped" },
        { source: "hardcover", confidence: 0.94, status: "matched" },
      ],
    },
    duplicate_of_owned: [
      {
        library_slug: "calibre-library",
        book_id: 77,
        title: "The Glass Orchard",
        mam_status: "found",
        calibre_id: 1203,
      },
    ],
  },
  cover_path: "/review-staging/grab-9101/cover.jpg",
  status: "pending",
  created_at: "2026-10-09 14:20:00",
  categories: ["Fantasy", "Mystery"],
};

const plainPending = {
  id: 502,
  grab_id: 9102,
  staged_path: "/review-staging/grab-9102",
  book_filename: "Saltwind - Bram Okafor.m4b",
  book_format: "m4b",
  bundle_group_id: null,
  metadata: {
    title: "Saltwind",
    author: "Bram Okafor",
    enriched: {
      title: "Saltwind",
      authors: ["Bram Okafor"],
      narrator: "Rhea Vance",
      duration_sec: 41_520,
      asin: "B000000002",
      abridged: false,
      source: "audible",
      confidence: 0.81,
    },
  },
  cover_path: null,
  status: "pending",
  created_at: "2026-10-09 15:05:00",
  categories: null,
};

const importFailed = {
  id: 503,
  grab_id: 9103,
  staged_path: "/review-staging/grab-9103",
  book_filename: "Ninefold Tide - Ada Quill.epub",
  book_format: "epub",
  metadata: { title: "Ninefold Tide", author: "Ada Quill" },
  cover_path: null,
  status: "import_failed",
  created_at: "2026-10-09 13:40:00",
  decision_note: "CWA took the file but no book appeared in Calibre",
  categories: ["Science Fiction"],
};

export const reviewPendingOnly = {
  "GET /v1/review": { items: [enrichedPending, plainPending], pending_count: 2 },
};

export const reviewWithImportFailure = {
  "GET /v1/review": { items: [importFailed, enrichedPending, plainPending], pending_count: 2 },
};

// Bulk approve where one of the two fails (wave 5b S6a: the phone shows
// the partial failure, as desktop does).
export const reviewBulkPartialRoutes = {
  ...reviewPendingOnly,
  "POST /v1/review/bulk/approve": { processed: 1, failed: 1, errors: ["Saltwind: sink unreachable"] },
};

// Re-drop of the import failure fails (its message must stay on screen).
export const reviewRedropFailRoutes = {
  ...reviewWithImportFailure,
  "POST /v1/review/503/redrop": { ok: false, error: "CWA ingest folder not writable" },
};
