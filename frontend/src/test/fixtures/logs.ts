// Logs page fixtures: a few application / IRC lines and the Announces
// audit with one row per decision. MAM identity is the placeholder.
const entries = [
  { ts: "2026-10-09 15:58:01", level: "INFO", logger: "seshat.mam.irc", message: "Announce: The Glass Orchard by Ada Quill [Ebooks - Fantasy]", is_announce: true },
  { ts: "2026-10-09 15:58:02", level: "INFO", logger: "seshat.dispatcher", message: "allow: The Glass Orchard (author on allow list)", is_announce: false },
  { ts: "2026-10-09 15:59:10", level: "WARNING", logger: "seshat.metadata.goodreads", message: "book page blocked; next in 600s", is_announce: false },
  { ts: "2026-10-09 15:59:44", level: "ERROR", logger: "seshat.orchestrator.import_check", message: "import check: 'Ninefold Tide' not in Calibre after 15 min", is_announce: false },
];

const rows = [
  { id: 40416, seen_at: "2026-10-09 15:58:01", torrent_name: "The Glass Orchard", author_blob: "Ada Quill", category: "Ebooks - Fantasy", filetype: "epub", decision: "allow", decision_reason: "author_allowed", matched_author: "Ada Quill", categories: ["Fantasy", "Mystery"] },
  { id: 40415, seen_at: "2026-10-09 15:40:12", torrent_name: "Red Lantern", author_blob: "C. Doyle", category: "Ebooks - Crime", filetype: "epub", decision: "skip", decision_reason: "category_not_allowed", matched_author: "", categories: ["Crime", "Thriller/Suspense"] },
  { id: 40414, seen_at: "2026-10-09 15:21:24", torrent_name: "Saltwind", author_blob: "Bram Okafor", category: "Audiobooks - Science Fiction", filetype: "m4b", decision: "hold", decision_reason: "tentative_new_author", matched_author: "", categories: null },
];

export const logsRoutes = {
  "GET /v1/logs": { entries, total_buffered: 4 },
  "GET /v1/announces": { rows, total_matched: 3, decision_counts: { allow: 1, skip: 1, hold: 1 } },
};
