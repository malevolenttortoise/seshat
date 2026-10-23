// Shared fixture builders for the render snapshots. Synthetic library:
// two libraries (Calibre ebooks + Audiobookshelf), a handful of made-up
// authors and books. No real torrent IDs, no MAM usernames.
import type { Book, ScanProgress, Series } from "../../types";

export const EBOOK_SLUG = "calibre-library";
export const AUDIO_SLUG = "audiobookshelf";

export function book(id: number, title: string, extra: Partial<Book> = {}): Book {
  return {
    id,
    title,
    author_id: 11,
    author_name: "Ada Quill",
    contributors: [{ author_id: 11, name: "Ada Quill", position: 0, role: null }],
    series_id: null,
    series_name: null,
    series_index: null,
    owned: 0,
    hidden: 0,
    is_unreleased: 0,
    is_omnibus: 0,
    is_new: 0,
    pub_date: "2024-05-14",
    cover_url: null,
    cover_path: null,
    mam_status: null,
    library_slug: EBOOK_SLUG,
    library_name: "Calibre Library",
    content_type: "ebook",
    ...extra,
  };
}

export const orchardBooks: Book[] = [
  book(101, "The Seed Vault", {
    series_id: 7, series_name: "Orchard Cycle", series_index: 1, owned: 1, calibre_id: 1201,
    mam_status: "found", mam_url: "https://example.invalid/t/900001", mam_formats: "epub",
  }),
  book(102, "The Glass Orchard", {
    series_id: 7, series_name: "Orchard Cycle", series_index: 2, owned: 0,
    mam_status: "found", mam_url: "https://example.invalid/t/900002", mam_formats: "epub mobi",
    mam_torrent_id: "900002",
  }),
  book(103, "Rootbound", {
    series_id: 7, series_name: "Orchard Cycle", series_index: 3, owned: 0, is_unreleased: 1,
    expected_date: "2027-02-02", mam_status: "not_found",
  }),
];

export const standaloneBooks: Book[] = [
  book(104, "Ninefold Tide", { owned: 1, calibre_id: 1204, mam_status: "possible" }),
  book(105, "A Lantern for Wolves", { owned: 0, is_new: 1, mam_status: null }),
];

export const orchardSeries: Series = {
  id: 7,
  name: "Orchard Cycle",
  book_count: 3,
  author_book_count: 3,
  owned_count: 1,
  missing_count: 2,
  multi_author: 0,
  author_mode: "per_author",
  is_owner: true,
};

export function scan(kind: ScanProgress["kind"], extra: Partial<ScanProgress> = {}): ScanProgress {
  return {
    kind,
    type: kind,
    label: kind,
    running: false,
    current: 0,
    total: 0,
    status: "idle",
    completed_at: Date.parse("2026-10-09T10:00:00Z") / 1000,
    ...extra,
  };
}

export const idleScans = {
  scans: [
    scan("library", { slug: EBOOK_SLUG, app_type: "calibre", content_type: "ebook", label: "Calibre Library" }),
    scan("library", { slug: AUDIO_SLUG, app_type: "audiobookshelf", content_type: "audiobook", label: "Audiobookshelf" }),
    scan("lookup", { label: "Source scan" }),
    scan("mam", { label: "MAM scan" }),
  ],
};

export const runningScans = {
  scans: [
    scan("library", { slug: EBOOK_SLUG, app_type: "calibre", content_type: "ebook", label: "Calibre Library" }),
    scan("library", { slug: AUDIO_SLUG, app_type: "audiobookshelf", content_type: "audiobook", label: "Audiobookshelf" }),
    scan("lookup", {
      label: "Source scan", running: true, status: "running", current: 12, total: 40,
      current_label: "Ada Quill", extra: { books_new: 3, source_timeouts: { goodreads: 1 } },
    }),
    scan("mam", {
      label: "MAM scan", running: true, status: "running", current: 5, total: 60,
      current_book: "The Glass Orchard", extra: { found: 2, possible: 1, not_found: 2 },
    }),
  ],
};
