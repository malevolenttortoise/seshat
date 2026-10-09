// Author detail fixtures: Ada Quill, linked to a person, with an
// ebook block (Calibre) and an audiobook block (Audiobookshelf).
import { reply } from "../render";
import {
  AUDIO_SLUG,
  EBOOK_SLUG,
  book,
  idleScans,
  orchardBooks,
  orchardSeries,
  runningScans,
  standaloneBooks,
} from "./common";

const ebookAuthor = {
  id: 11,
  name: "Ada Quill",
  sort_name: "Quill, Ada",
  bio: "Ada Quill writes botanical mysteries.",
  image_url: null,
  total_books: 5,
  owned_count: 2,
  missing_count: 3,
  new_count: 1,
  series_count: 1,
  last_lookup_at: Date.parse("2026-10-08T09:00:00Z") / 1000,
  series: [orchardSeries],
  standalone_books: standaloneBooks,
};

const audioAuthor = {
  id: 41,
  name: "Ada Quill",
  total_books: 1,
  owned_count: 1,
  missing_count: 0,
  series_count: 0,
  series: [],
  standalone_books: [
    book(301, "Ninefold Tide", {
      owned: 1, library_slug: AUDIO_SLUG, library_name: "Audiobookshelf", content_type: "audiobook",
      narrator: "Rhea Vance", duration_sec: 39_600, audiobookshelf_id: "li_0001",
    }),
  ],
};

const legacy = {
  ...ebookAuthor,
  person_id: 5,
  active_library_slug: EBOOK_SLUG,
  active_content_type: "ebook",
};

const person = {
  person_id: 5,
  canonical_name: "Ada Quill",
  display_name: "Ada Quill",
  normalized_name: "ada quill",
  display_name_override: null,
  bio: "Ada Quill writes botanical mysteries.",
  image_url: null,
  source_ids: { goodreads: "g-1001", hardcover: "ada-quill", amazon: null },
  libraries: [
    { library_slug: EBOOK_SLUG, library_name: "Calibre Library", content_type: "ebook", app_type: "calibre", author_id: 11, author: ebookAuthor },
    { library_slug: AUDIO_SLUG, library_name: "Audiobookshelf", content_type: "audiobook", app_type: "audiobookshelf", author_id: 41, author: audioAuthor },
  ],
  pen_names: [],
  global_stats: { owned: 3, missing: 3, total: 6, series_count: 1 },
  low_confidence: false,
};

const seriesBooks = { books: orchardBooks };

export const authorDetailRoutes = {
  "GET /discovery/authors/11": legacy,
  "GET /discovery/persons/5": person,
  "GET /discovery/scan-status": idleScans,
  "GET /discovery/authors/11/pen-names": { links: [] },
  "GET /discovery/mam/status": { enabled: true, validation_ok: true },
  "GET /discovery/series/7*": seriesBooks,
  "GET /discovery/authors/11/source-breakdown": {
    author_id: 11,
    author_name: "Ada Quill",
    slug: EBOOK_SLUG,
    owned_books: 2,
    sources: [
      { source: "hardcover", books: 4, corroborated: 3, sample_titles: ["The Glass Orchard", "Rootbound"], disambiguating: false, source_author_id: "ada-quill", flag: "none", note: "", blacklisted: false },
      { source: "goodreads", books: 2, corroborated: 0, sample_titles: ["A Lantern for Wolves"], disambiguating: true, source_author_id: "g-1001", flag: "review", note: "Nothing else lists these.", blacklisted: false },
    ],
  },
  "GET /v1/metadata-cache/goodreads/author/g-1001": {
    source: "goodreads",
    author_id: "g-1001",
    amazon_author_id: "",
    libraries: [
      {
        library_slug: EBOOK_SLUG,
        state: { last_scanned_at: Date.parse("2026-10-07T08:00:00Z") / 1000, last_outcome: "ok", book_count: 6 },
        queue: { status: "pending", priority: 1, next_scan_due_at: Date.parse("2026-10-14T08:00:00Z") / 1000, consecutive_failures: 0 },
        list_pages: [{ page_num: 1, fetched_at: Date.parse("2026-10-07T08:00:00Z") / 1000, book_count: 6 }],
      },
    ],
    cooldown: { blocked: false, remaining_s: 0 },
  },
};

export const authorDetailScanningRoutes = {
  ...authorDetailRoutes,
  "GET /discovery/persons/5": {
    ...person,
    pen_names: [
      { link_id: 3, person_id: 9, canonical_name: "A. Q. Thorne", display_name: "A. Q. Thorne", link_type: "pen_name", direction: "alias_of_this" },
    ],
  },
  "GET /discovery/authors/11/pen-names": {
    links: [
      { id: 3, canonical_author_id: 11, alias_author_id: 19, canonical_name: "Ada Quill", alias_name: "A. Q. Thorne", link_type: "pen_name" },
    ],
  },
  "GET /discovery/scan-status": runningScans,
};

// The author request fails (wave 5b S2a: the page says so instead of a
// spinner that never stops / a blank page).
export const authorDetailFailedRoutes = {
  ...authorDetailRoutes,
  "GET /discovery/authors/11": reply(500, { detail: "database is locked" }),
};
