// Shared types for AthenaScout frontend.
//
// Kept intentionally lean — only the shapes that cross component
// boundaries or come back from the API live here. Single-component
// internal shapes stay inline next to their use site.

import type { CSSProperties, ReactNode } from "react";

// ─── Theme ──────────────────────────────────────────────────
// Mirrors the palette object in theme.ts. Every key that components
// read off `useTheme()` must appear here so consumers get IDE
// completion and miss-typed colors fail compilation.
export interface Theme {
  name: string;
  bg: string; bg2: string; bg3: string; bg4: string;
  border: string; borderH: string; borderL: string;
  text: string; text2: string;
  tm: string; td: string; tf: string; tg: string; ti: string;
  accent: string; abg: string; abr: string;
  grn: string; grnt: string; grnb: string;
  red: string; redt: string; redb: string;
  ylw: string; ylwt: string; ylwb: string;
  pur: string; purt: string; purb: string;
  cyan: string; cyant: string; cyanb: string;
  inp: string;
}

export type ThemeName = "dark" | "dim" | "light";

// ─── Common UI prop shapes ──────────────────────────────────
export interface ChildrenProps {
  children?: ReactNode;
}

export interface StyleProps {
  style?: CSSProperties;
  className?: string;
}

// ─── App-level callback shapes ──────────────────────────────
// `NavFn` is the page-router callback that App.tsx hands down to
// every page. The arg is page-specific — author detail uses an
// author id (number), other pages either don't pass anything or
// pass a string discriminator.
export type NavFn = (page: string, arg?: number | string | null) => void;

// `BookAction` enumerates the per-row actions BookSidebar /
// BookViews emit upward. Centralized so a renamed/added action is
// caught at every consumer instead of silently ignored.
export type BookAction = "hide" | "unhide" | "dismiss" | "delete";
export type BookActionHandler = (action: BookAction, bookId: number) => void | Promise<void>;

// `SendToHermeece` is the bulk-send callback used by the MAM page
// + book sidebar. Returns void; errors surface as toasts.
export type SendToHermeeceFn = (bookIds: number[]) => void | Promise<void>;

// ─── API response shapes ────────────────────────────────────
// One per high-traffic endpoint. Use as the generic on api.get<T>()
// at the call site. Add new ones here as endpoints are typed —
// don't sprinkle inline anonymous types at call sites.

export interface AuthorsResponse {
  authors: Author[];
}

export interface BooksResponse {
  books: Book[];
  total: number;
}

export interface PenNamesResponse {
  links: PenNameLink[];
}

export interface MamStatusResponse {
  enabled: boolean;
  validation_ok?: boolean;
  stats?: {
    upload_candidates?: number;
    available_to_download?: number;
    missing_everywhere?: number;
    total_unscanned?: number;
  };
}

export interface ScanStatusResponse {
  scans: ScanProgress[];
}

export interface LibrariesResponse {
  libraries: Library[];
}

export interface AuthCheckResponse {
  authenticated: boolean;
  first_run?: boolean;
}

export interface SeriesSuggestionCountResponse {
  pending: number;
}

// ─── API entity shapes ──────────────────────────────────────
// Tracks the JSON shape the FastAPI routers actually return. Fields
// not consumed by the UI are intentionally omitted to keep the
// surface area small — add them as the UI starts using them.
export interface Author {
  id: number;
  name: string;
  sort_name?: string | null;
  bio?: string | null;
  image_url?: string | null;
  total_books?: number;
  owned_count?: number;
  missing_count?: number;
  new_count?: number;
  series_count?: number;
  link_count?: number;
  last_lookup_at?: number | null;
  // Stamped by run_across_libraries when the authors endpoint is
  // called with `content_type`. The Authors page uses it to build
  // the "slug:id" nav arg so cross-library click-throughs resolve
  // in the right library.
  library_slug?: string;
}

export interface Book {
  id: number;
  title: string;
  author_id?: number;
  author_name?: string;
  series_id?: number | null;
  series_name?: string | null;
  series_index?: number | null;
  series_total?: number;
  mainline_total?: number;
  owned?: 0 | 1;
  hidden?: 0 | 1;
  is_unreleased?: 0 | 1;
  is_omnibus?: 0 | 1;
  is_new?: 0 | 1;
  expected_date?: string | null;
  pub_date?: string | null;
  publisher?: string | null;
  language?: string | null;
  isbn?: string | null;
  description?: string | null;
  cover_url?: string | null;
  cover_path?: string | null;
  page_count?: number | null;
  source?: string | null;
  source_url?: string | null;
  goodreads_id?: string | null;
  hardcover_id?: string | null;
  kobo_id?: string | null;
  amazon_id?: string | null;
  ibdb_id?: string | null;
  google_books_id?: string | null;
  // MAM
  mam_url?: string | null;
  mam_status?: "found" | "possible" | "not_found" | null;
  mam_formats?: string | null;
  mam_torrent_id?: string | null;
  mam_has_multiple?: 0 | 1;
  mam_my_snatched?: 0 | 1;
  // Calibre linkage
  calibre_id?: number | null;
  // Audiobook-specific — populated when the book came from an ABS
  // library; null / undefined for ebook rows.
  audiobookshelf_id?: string | null;
  asin?: string | null;
  narrator?: string | null;
  duration_sec?: number | null;
  abridged?: 0 | 1;
  audio_formats?: string | null;
  // Cross-library metadata — set by the aggregation helper in
  // `app/discovery/cross_library.py` when the books endpoint is
  // called with a `content_type` query param.
  library_slug?: string;
  library_name?: string;
  content_type?: "ebook" | "audiobook" | string;
  // Cross-library "work" linkage stamped by the authors + series
  // endpoints when `get_siblings_for_books` finds paired rows in
  // another library. `work_siblings` is a non-empty array for books
  // whose work has at least one member in a different library; the
  // entries describe the sibling's library + content_type so the UI
  // can render the "also available as audiobook" hint.
  work_id?: string;
  work_siblings?: WorkSibling[];
  // Catalog extras carried through from the source library. Strings
  // rather than enums because Calibre tags + format lists are free-
  // form — the UI renders them as pill chips.
  tags?: string | null;
  formats?: string | null;
  rating?: number | null;
}

export interface WorkSibling {
  library_slug: string;
  book_id: number;
  content_type: string;
}

export interface Series {
  id: number;
  name: string;
  book_count?: number;
  author_book_count?: number;
  owned_count?: number;
  missing_count?: number;
  multi_author?: 0 | 1;
}

export interface Library {
  slug: string;
  name: string;
  display_name?: string;
  app_type?: string;
  content_type?: string;  // "ebook" | "audiobook" | etc. — used for the nav-bar emoji prefix
  source_db_path?: string;
  library_path?: string;
  active?: boolean;
}

export type LinkType = "pen_name" | "co_author";

export interface PenNameLink {
  id: number;
  canonical_author_id: number;
  alias_author_id: number;
  canonical_name: string;
  alias_name: string;
  link_type: LinkType;
}

// Unified scan-status entry (one per kind: lookup / mam / library).
// `extra` is a grab-bag of counters the scan emits for progress /
// summary lines (found, possible, not_found, books_new, etc.) plus
// the nested `source_timeouts` map keyed by source name → author
// count. Typed as `unknown` so callers narrow per-field rather than
// claiming every value is a number.
export interface ScanProgress {
  kind: "lookup" | "mam" | "library";
  type: string;
  label: string;
  running: boolean;
  current: number;
  total: number;
  current_label?: string | null;
  current_book?: string | null;
  status: string;
  completed_at?: number | null;
  extra?: Record<string, unknown>;
}

// ─── App-level event names ──────────────────────────────────
// Exhaustive list of CustomEvent names dispatched on `window`.
// Use `window.addEventListener(EVT.X, ...)` to keep typos out.
export const EVT = {
  AuthRequired:        "seshat:auth-required",
  ScansUpdated:        "seshat:scans-updated",
  ScanCompleted:       "seshat:scan-completed",
  ScanStarted:         "seshat:scan-started",
  MamStateChanged:     "seshat:mam-state-changed",
  SuggestionsChanged:  "seshat:suggestions-changed",
  Toast:               "seshat:toast",
} as const;
