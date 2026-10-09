// Books page fixtures (Missing, and Hidden with its owned filter).
import { orchardBooks, standaloneBooks } from "./common";

const missing = [orchardBooks[1], orchardBooks[2], standaloneBooks[1]];
const hidden = [{ ...standaloneBooks[0], hidden: 1 as const }];

export const booksRoutes = {
  "GET /discovery/missing": { books: missing, total: 3 },
  "GET /discovery/books/hidden": { books: hidden, total: 1 },
  "GET /discovery/mam/status": { enabled: true, validation_ok: true },
  "GET /discovery/mam/scan/status": { running: false, status: "idle" },
};
