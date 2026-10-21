// Books page fixtures (Missing, and Hidden with its owned filter and its
// two tabs, wave B S7).
import { book, orchardBooks, standaloneBooks } from "./common";

const shortStory = book(106, "The Moth Sermon", { owned: 0, page_count: 42 });
const missing = [orchardBooks[1], orchardBooks[2], standaloneBooks[1], shortStory];
const hidden = [{ ...standaloneBooks[0], hidden: 1 as const }];
const filtered = [
  { ...book(107, "Orchard Cycle: The Graphic Novel, Vol. 1", { page_count: 120 }), hidden: 1 as const, hidden_reason: "comic" },
  { ...book(108, "Orchard Cycle Books 1-3", {}), hidden: 1 as const, hidden_reason: "box_set" },
];

export const booksRoutes = {
  "GET /discovery/missing": { books: missing, total: 4 },
  "GET /discovery/books/hidden": { books: hidden, total: 1 },
  "GET /discovery/books/hidden?search=&sort=title&sort_dir=asc&per_page=60&page=1&content_type=all&filtered=true":
    { books: filtered, total: 2 },
  "GET /discovery/books/hidden?search=&sort=title&per_page=60&page=1&content_type=all&filtered=true":
    { books: filtered, total: 2 },
  "GET /discovery/books/hidden/reasons*": { counts: { mine: 1, comic: 1, box_set: 1 } },
  "GET /discovery/mam/status": { enabled: true, validation_ok: true },
  "GET /discovery/mam/scan/status": { running: false, status: "idle" },
};
