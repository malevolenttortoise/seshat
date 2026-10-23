// What the book lists filter on (discovery fix pass S7, ADR-0029).
//
// "Short" (F11 / C8): under 100 pages, computed on read from `page_count`
// (the backend's `filters.SHORT_UNDER_PAGES` is the same number). A book
// with no page count can't be badged.
//
// The Hidden page's "Filtered" tab (F19): the books Seshat hid, by the
// reason it hid them; one chip may cover several reasons.
import type { Book } from "../types";

export const SHORT_UNDER_PAGES = 100;

export function isShort(book: Pick<Book, "page_count">): boolean {
  const n = book.page_count ?? 0;
  return n > 0 && n < SHORT_UNDER_PAGES;
}

export const HIDDEN_PATH = "/discovery/books/hidden";

/** Lists the "Hide short books" filter applies to. */
export const SHORT_FILTER_PATHS = ["/discovery/missing", "/discovery/books"];

export interface ReasonGroup {
  id: string;
  label: string;
  reasons: string[];
}

export const REASON_GROUPS: ReasonGroup[] = [
  { id: "comic", label: "Comic", reasons: ["comic"] },
  { id: "box_set", label: "Box set", reasons: ["box_set"] },
  { id: "anthology", label: "Anthology", reasons: ["anthology"] },
  { id: "not_english", label: "Not English", reasons: ["not_english"] },
  { id: "split", label: "Split / sampler / listing", reasons: ["split", "sampler", "listing"] },
  { id: "not_a_book", label: "Not a book", reasons: ["not_a_book", "study_guide", "junk"] },
  { id: "author_off", label: "Author off", reasons: ["author_off"] },
];

/** `/books/hidden/reasons`'s counts → per tab and per chip. */
export function hiddenCounts(counts: Record<string, number>) {
  const byGroup: Record<string, number> = {};
  let filtered = 0;
  for (const g of REASON_GROUPS) {
    byGroup[g.id] = g.reasons.reduce((n, r) => n + (counts[r] ?? 0), 0);
    filtered += byGroup[g.id];
  }
  return { mine: counts.mine ?? 0, filtered, byGroup };
}
