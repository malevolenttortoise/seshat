// Global search bar (v2.15.0 #B).
//
// Persistent input mounted in the top nav. Live results dropdown
// shows a categorized list: Pages, Settings, Authors, Series, Books.
// Pages + Settings entries are client-side indexed (instant, no
// API call); Authors / Series / Books come from /api/v1/search
// (debounced to 300ms so typing doesn't hammer the endpoint).
//
// Click a result OR press Enter on the selected row → invokes
// onNavigate with a structured target. The host (App.tsx) decides
// how to route: page IDs go to nav(pageId), Settings entries pass
// a {focus} arg, Authors get {authorId, librarySlug}, etc.

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { Ic } from "../icons";

// ── Client-side index entries ───────────────────────────────
// Pages + Settings sections are small (<50 entries) so they live
// in-memory. The author/series/book entries come from the server.

interface PageEntry {
  kind: "page";
  label: string;
  page_id: string;
  section: "discovery" | "pipeline" | "shared";
  icon: string;
  keywords?: string[];  // extra match strings (e.g. "settings" → "preferences config")
}

interface SettingsSectionEntry {
  kind: "settings-section";
  label: string;
  section_id: string;
  group: "Pipeline" | "Discovery" | "Shared";
  keywords?: string[];
}

// Page + nav surfaces. Mirrors DISCOVERY_NAV / PIPELINE_NAV /
// RIGHT_ICONS in App.tsx — kept in sync by hand for now since
// extracting to a shared module would require restructuring App's
// component layout.
const PAGE_INDEX: PageEntry[] = [
  { kind: "page", label: "Dashboard",        page_id: "dashboard",         section: "shared",    icon: "🏠" },
  // Discovery
  { kind: "page", label: "Library",          page_id: "disc-library",      section: "discovery", icon: "📖",
    keywords: ["books", "owned"] },
  { kind: "page", label: "Authors",          page_id: "disc-authors",      section: "discovery", icon: "◉" },
  { kind: "page", label: "Missing",          page_id: "disc-missing",      section: "discovery", icon: "◌",
    keywords: ["unowned"] },
  { kind: "page", label: "Upcoming",         page_id: "disc-upcoming",     section: "discovery", icon: "📅",
    keywords: ["releases", "expected"] },
  { kind: "page", label: "Works",            page_id: "disc-works",        section: "discovery", icon: "🔗" },
  { kind: "page", label: "MAM Search",       page_id: "disc-mam",          section: "discovery", icon: "🔍" },
  { kind: "page", label: "Metadata",         page_id: "disc-metadata",     section: "discovery", icon: "📋" },
  { kind: "page", label: "Series",           page_id: "disc-series",       section: "discovery", icon: "🗂️" },
  { kind: "page", label: "Hidden",           page_id: "disc-hidden",       section: "discovery", icon: "🚫" },
  // Pipeline
  { kind: "page", label: "Review",           page_id: "pipe-review",       section: "pipeline",  icon: "📚" },
  { kind: "page", label: "New Authors",      page_id: "pipe-tentative",    section: "pipeline",  icon: "🔎",
    keywords: ["tentative"] },
  { kind: "page", label: "Weekly Ignored",   page_id: "pipe-ignored",      section: "pipeline",  icon: "📊" },
  { kind: "page", label: "Author Lists",     page_id: "pipe-authors",      section: "pipeline",  icon: "👤",
    keywords: ["allowed", "ignored"] },
  { kind: "page", label: "Delayed",          page_id: "pipe-delayed",      section: "pipeline",  icon: "⏳" },
  { kind: "page", label: "Filters",          page_id: "filters",           section: "pipeline",  icon: "🎯" },
  // Shared right-rail
  { kind: "page", label: "Import / Export",  page_id: "disc-importexport", section: "discovery", icon: "📦",
    keywords: ["backup", "export", "import"] },
  { kind: "page", label: "MAM Status",       page_id: "pipe-mam",          section: "pipeline",  icon: "📡",
    keywords: ["session", "vip", "ratio"] },
  { kind: "page", label: "Logs",             page_id: "logs",              section: "shared",    icon: "📋" },
  { kind: "page", label: "Database",         page_id: "database",          section: "shared",    icon: "🗄️",
    keywords: ["sqlite", "table", "rows"] },
  { kind: "page", label: "Settings",         page_id: "settings",          section: "shared",    icon: "⚙️",
    keywords: ["preferences", "config", "options"] },
];

// Mirrors SettingsPage SECTIONS. Surfaced as their own category so
// users typing "notifications" or "metadata sources" land on the
// right Settings tab.
const SETTINGS_SECTIONS_INDEX: SettingsSectionEntry[] = [
  { kind: "settings-section", label: "Pipeline",          section_id: "pipeline",       group: "Pipeline" },
  { kind: "settings-section", label: "Review & Enrichment", section_id: "review",       group: "Pipeline",
    keywords: ["enrichment", "metadata"] },
  { kind: "settings-section", label: "Grab Policy",       section_id: "policy",         group: "Pipeline",
    keywords: ["grab", "ratio", "wedge"] },
  { kind: "settings-section", label: "Snatch Budget",     section_id: "budget",         group: "Pipeline",
    keywords: ["budget", "cap", "queue"] },
  { kind: "settings-section", label: "MyAnonamouse",      section_id: "mam",            group: "Pipeline",
    keywords: ["mam", "session", "irc"] },
  { kind: "settings-section", label: "Download Client",   section_id: "client",         group: "Pipeline",
    keywords: ["qbittorrent", "qbit", "transmission"] },
  { kind: "settings-section", label: "Sinks & Delivery",  section_id: "sinks",          group: "Pipeline",
    keywords: ["calibre", "cwa", "audiobookshelf", "abs", "delivery"] },
  { kind: "settings-section", label: "Notifications",     section_id: "notifications",  group: "Pipeline",
    keywords: ["ntfy", "notify", "alerts"] },
  { kind: "settings-section", label: "Metadata Sources",  section_id: "sources",        group: "Discovery",
    keywords: ["goodreads", "hardcover", "amazon", "kobo", "openlibrary", "audible", "ibdb"] },
  { kind: "settings-section", label: "Author Scanning",   section_id: "scanning",       group: "Discovery",
    keywords: ["scan", "discover"] },
  { kind: "settings-section", label: "Library Management", section_id: "library",       group: "Discovery",
    keywords: ["libraries", "calibre", "abs"] },
  { kind: "settings-section", label: "Audiobookshelf",    section_id: "audiobookshelf", group: "Discovery",
    keywords: ["abs", "audio"] },
  { kind: "settings-section", label: "Discovery MAM",     section_id: "discmam",        group: "Discovery",
    keywords: ["mam"] },
  { kind: "settings-section", label: "Operational",       section_id: "operational",    group: "Shared",
    keywords: ["logging", "verbose", "theme"] },
  { kind: "settings-section", label: "Data Management",   section_id: "data",           group: "Shared",
    keywords: ["clear", "wipe", "delete"] },
];

// ── Server response types ──────────────────────────────────

interface BookHit {
  id: number;
  title: string;
  author_name?: string | null;
  author_id?: number | null;
  series_name?: string | null;
  library_slug?: string | null;
  library_name?: string | null;
  content_type?: string | null;
  owned?: number | null;
}

interface AuthorHit {
  id: number;
  name: string;
  library_slug?: string | null;
  library_name?: string | null;
  content_type?: string | null;
  book_count?: number | null;
}

interface SeriesHit {
  id: number;
  name: string;
  author_name?: string | null;
  author_id?: number | null;
  library_slug?: string | null;
  library_name?: string | null;
}

interface SearchResponse {
  q: string;
  books: BookHit[];
  authors: AuthorHit[];
  series: SeriesHit[];
}

// ── Public navigation target ───────────────────────────────

export type SearchNavTarget =
  | { kind: "page"; page_id: string; section?: "discovery" | "pipeline" }
  | { kind: "settings-section"; section_id: string }
  | { kind: "author"; author_id: number; library_slug?: string | null }
  | { kind: "series"; series_id: number; library_slug?: string | null; author_id?: number | null }
  | { kind: "book"; book_id: number; library_slug?: string | null; author_id?: number | null };

interface GlobalSearchBarProps {
  onNavigate: (target: SearchNavTarget) => void;
  // Optional: shrink to icon-only on narrow viewports. Caller decides.
  compact?: boolean;
  // Mount-time focus. Used by the mobile overlay so the user lands
  // ready to type without an extra tap.
  autoFocus?: boolean;
  // Full-width mode: stretches to container width instead of the
  // default fixed 280px. Used by the mobile overlay.
  fullWidth?: boolean;
}

// ── Client-side match helpers ──────────────────────────────

function clientMatch(q: string, entry: PageEntry | SettingsSectionEntry): boolean {
  const needle = q.toLowerCase();
  if (entry.label.toLowerCase().includes(needle)) return true;
  if (entry.keywords?.some((k) => k.toLowerCase().includes(needle))) return true;
  return false;
}

// Rank-by-prefix: exact-prefix label matches rank above mid-string
// matches above keyword-only matches.
function clientRank(q: string, entry: PageEntry | SettingsSectionEntry): number {
  const needle = q.toLowerCase();
  const label = entry.label.toLowerCase();
  if (label.startsWith(needle)) return 0;
  if (label.includes(needle)) return 1;
  return 2;
}

// ── Flat row model for the dropdown ─────────────────────────

interface DropdownRow {
  group: "Pages" | "Settings" | "Authors" | "Series" | "Books";
  primary: string;
  secondary?: string;
  icon?: string;
  target: SearchNavTarget;
}

function buildRows(
  q: string,
  serverHits: SearchResponse | null,
): DropdownRow[] {
  const rows: DropdownRow[] = [];

  // Pages
  const pageHits = PAGE_INDEX
    .filter((p) => clientMatch(q, p))
    .sort((a, b) => clientRank(q, a) - clientRank(q, b))
    .slice(0, 6);
  for (const p of pageHits) {
    rows.push({
      group: "Pages",
      primary: p.label,
      icon: p.icon,
      target: { kind: "page", page_id: p.page_id,
                section: p.section === "shared" ? undefined : p.section },
    });
  }

  // Settings sections
  const settingsHits = SETTINGS_SECTIONS_INDEX
    .filter((s) => clientMatch(q, s))
    .sort((a, b) => clientRank(q, a) - clientRank(q, b))
    .slice(0, 6);
  for (const s of settingsHits) {
    rows.push({
      group: "Settings",
      primary: s.label,
      secondary: s.group,
      icon: "⚙️",
      target: { kind: "settings-section", section_id: s.section_id },
    });
  }

  // Server hits
  if (serverHits) {
    for (const a of serverHits.authors) {
      rows.push({
        group: "Authors",
        primary: a.name,
        secondary: a.book_count != null ? `${a.book_count} book${a.book_count === 1 ? "" : "s"}` : undefined,
        icon: "◉",
        target: { kind: "author", author_id: a.id, library_slug: a.library_slug },
      });
    }
    for (const s of serverHits.series) {
      rows.push({
        group: "Series",
        primary: s.name,
        secondary: s.author_name ? `by ${s.author_name}` : undefined,
        icon: "🗂️",
        target: {
          kind: "series",
          series_id: s.id,
          author_id: s.author_id ?? null,
          library_slug: s.library_slug,
        },
      });
    }
    for (const b of serverHits.books) {
      const tail: string[] = [];
      if (b.author_name) tail.push(`by ${b.author_name}`);
      if (b.library_name) tail.push(b.library_name);
      rows.push({
        group: "Books",
        primary: b.title,
        secondary: tail.join(" · ") || undefined,
        icon: b.owned ? "📖" : "◌",
        target: {
          kind: "book",
          book_id: b.id,
          author_id: b.author_id ?? null,
          library_slug: b.library_slug,
        },
      });
    }
  }

  return rows;
}

// ── Component ───────────────────────────────────────────────

export function GlobalSearchBar({ onNavigate, compact, autoFocus, fullWidth }: GlobalSearchBarProps) {
  const t = useTheme();
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [serverHits, setServerHits] = useState<SearchResponse | null>(null);
  const [open, setOpen] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const [loading, setLoading] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);

  // Debounce: 300ms after the user stops typing, mirror q → debouncedQ.
  // Empty / short queries clear immediately so the dropdown collapses
  // without waiting for the timeout.
  useEffect(() => {
    if (q.length < 2) { setDebouncedQ(""); return; }
    const id = setTimeout(() => setDebouncedQ(q), 300);
    return () => clearTimeout(id);
  }, [q]);

  // Server fetch on debouncedQ change.
  useEffect(() => {
    if (debouncedQ.length < 2) { setServerHits(null); return; }
    let active = true;
    setLoading(true);
    api.get<SearchResponse>(`/v1/search?q=${encodeURIComponent(debouncedQ)}&limit=6`)
      .then((r) => { if (active) { setServerHits(r); setLoading(false); } })
      .catch(() => { if (active) { setServerHits(null); setLoading(false); } });
    return () => { active = false; };
  }, [debouncedQ]);

  // Click-outside-to-close.
  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (!wrapRef.current) return;
      if (e.target instanceof Node && wrapRef.current.contains(e.target)) return;
      setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  // Cmd+K / Ctrl+K shortcut to focus the input.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
        setOpen(true);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const rows = useMemo(
    () => (q.length >= 2 ? buildRows(q, serverHits) : []),
    [q, serverHits],
  );

  // Reset active index when results change so keyboard nav doesn't
  // point at a row that disappeared.
  useEffect(() => { setActiveIdx(0); }, [rows.length]);

  function activate(idx: number) {
    const row = rows[idx];
    if (!row) return;
    setOpen(false);
    setQ("");
    setDebouncedQ("");
    setServerHits(null);
    onNavigate(row.target);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Escape") {
      e.currentTarget.blur();
      setOpen(false);
      return;
    }
    if (!open || rows.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => Math.min(rows.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      activate(activeIdx);
    }
  }

  // Group rows by group name for rendering.
  const groupedRows = useMemo(() => {
    const groups: { name: DropdownRow["group"]; rows: { row: DropdownRow; idx: number }[] }[] = [];
    rows.forEach((row, idx) => {
      const last = groups[groups.length - 1];
      if (last && last.name === row.group) last.rows.push({ row, idx });
      else groups.push({ name: row.group, rows: [{ row, idx }] });
    });
    return groups;
  }, [rows]);

  const inputWidth = compact ? 200 : 280;

  return (
    <div ref={wrapRef} style={{ position: "relative", width: fullWidth ? "100%" : undefined }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 6,
        background: t.bg, border: `1px solid ${t.border}`,
        borderRadius: 6, padding: "4px 8px",
        width: fullWidth ? "100%" : inputWidth,
      }}>
        <span style={{ color: t.tf, display: "flex", alignItems: "center" }}>
          {Ic.search}
        </span>
        <input
          ref={inputRef}
          type="search"
          value={q}
          onChange={(e) => { setQ(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
          placeholder="Search… (⌘K)"
          style={{
            flex: 1, minWidth: 0,
            background: "transparent", border: "none", outline: "none",
            color: t.text, fontSize: 13, fontFamily: "inherit",
          }}
        />
        {loading && (
          <span style={{ fontSize: 11, color: t.tf }}>…</span>
        )}
      </div>

      {open && q.length >= 2 && (
        <div
          role="listbox"
          style={{
            position: "absolute", top: "calc(100% + 4px)", left: 0,
            background: t.bg2, border: `1px solid ${t.borderL}`,
            borderRadius: 8, boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
            minWidth: fullWidth ? "100%" : inputWidth + 80,
            maxWidth: fullWidth ? "100%" : 460,
            width: fullWidth ? "100%" : undefined,
            maxHeight: 480, overflowY: "auto",
            zIndex: 200,
          }}
        >
          {rows.length === 0 ? (
            <div style={{ padding: "12px 14px", color: t.tf, fontSize: 13 }}>
              {loading ? "Searching…" : "No matches."}
            </div>
          ) : (
            groupedRows.map((g) => (
              <div key={g.name}>
                <div style={{
                  fontSize: 10, fontWeight: 700, color: t.tf,
                  textTransform: "uppercase", letterSpacing: "0.06em",
                  padding: "8px 12px 4px",
                }}>
                  {g.name}
                </div>
                {g.rows.map(({ row, idx }) => {
                  const active = idx === activeIdx;
                  return (
                    <div
                      key={`${g.name}-${idx}`}
                      role="option"
                      aria-selected={active}
                      onMouseEnter={() => setActiveIdx(idx)}
                      onMouseDown={(e) => { e.preventDefault(); activate(idx); }}
                      style={{
                        display: "flex", alignItems: "center", gap: 10,
                        padding: "8px 12px",
                        cursor: "pointer",
                        background: active ? t.abg : "transparent",
                        color: active ? t.accent : t.text2,
                      }}
                    >
                      <span style={{ fontSize: 14, flexShrink: 0 }}>{row.icon}</span>
                      <span style={{
                        flex: 1, minWidth: 0,
                        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                      }}>
                        <span style={{ fontSize: 13, fontWeight: 500 }}>{row.primary}</span>
                        {row.secondary && (
                          <span style={{
                            marginLeft: 8, fontSize: 11, color: t.tf,
                          }}>
                            {row.secondary}
                          </span>
                        )}
                      </span>
                    </div>
                  );
                })}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
