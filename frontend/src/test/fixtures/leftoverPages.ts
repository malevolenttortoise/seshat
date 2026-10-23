// Fixtures for the pages wave 5b S18 moves onto the shared sidebar and
// MAM-flag hooks (G156): Series detail, Hidden, Authors.
import { AUDIO_SLUG, EBOOK_SLUG, book, orchardBooks } from "./common";
import { sidebarCommonRoutes } from "./bookSidebar";

export const seriesDetail = {
  id: 7,
  name: "Orchard Cycle",
  author_mode: "multi_author",
  books: orchardBooks.map((b, i) =>
    i === 1
      ? { ...b, contributors: [...(b.contributors ?? []), { author_id: 12, name: "Bram Okafor", position: 1, role: null }] }
      : b,
  ),
  contributing_authors: [
    { author_id: 11, name: "Ada Quill", book_count: 3, is_owner: true },
    { author_id: 12, name: "Bram Okafor", book_count: 1, is_owner: false },
  ],
};

export const seriesDetailRoutes = {
  [`GET /discovery/series/7?slug=${EBOOK_SLUG}`]: seriesDetail,
};

export const seriesDetailSidebarRoutes = {
  ...seriesDetailRoutes,
  ...sidebarCommonRoutes,
  "GET /discovery/series-suggestions/by-book/102": { suggestion: null },
};

export const hiddenBooks = [
  book(201, "The Winter Graft", { hidden: 1, owned: 0, mam_status: "not_found" }),
  book(202, "A Ledger of Bees", {
    hidden: 1, owned: 0, library_slug: AUDIO_SLUG, library_name: "Audiobookshelf", content_type: "audiobook",
  }),
];

export const hiddenRoutes = {
  "GET /discovery/books/hidden": { books: hiddenBooks, total: hiddenBooks.length },
};

export const hiddenSidebarRoutes = {
  ...hiddenRoutes,
  ...sidebarCommonRoutes,
  "GET /discovery/series-suggestions/by-book/201": { suggestion: null },
};

export const authorsList = [
  {
    id: 11, name: "Ada Quill", sort_name: "Quill, Ada", total_books: 6, owned_count: 3, missing_count: 3,
    new_count: 1, series_count: 1, library_slug: EBOOK_SLUG, library_slugs: [EBOOK_SLUG, AUDIO_SLUG],
    content_types: ["ebook", "audiobook"], last_lookup_at: Date.parse("2026-10-08T09:00:00Z") / 1000,
  },
  {
    id: 12, name: "Bram Okafor", sort_name: "Okafor, Bram", total_books: 2, owned_count: 0, missing_count: 2,
    new_count: 0, series_count: 0, library_slug: EBOOK_SLUG, library_slugs: [EBOOK_SLUG],
    content_types: ["ebook"], last_lookup_at: null,
  },
];

export const authorsRoutes = {
  "GET /discovery/authors?*": { authors: authorsList },
  // Authors a source scan flagged for a look (desktop's review badge).
  "GET /discovery/authors/source-review": { author_keys: [`${EBOOK_SLUG}:12`] },
  "GET /discovery/mam/status": { enabled: true, validation_ok: true },
};
