// Books browse page.
//
// Reused as the renderer for "My Library", "Missing Books", and
// "Upcoming Books" — App.tsx instantiates BooksPage with different
// `apiPath` and `extraParams` props for each. The page hosts the
// shared search/sort/grouping/view-mode controls, the BookSidebar
// drawer for inspecting a single book, and the bulk-select bar for
// running scans against a chosen subset.
import { useEffect, useMemo, useState } from "react";
import { useTheme } from "../theme";
import type { Theme } from "../theme";
import { api } from "../api";
import { usePersist } from "../hooks/usePersist";
import { Btn } from "../components/Btn";
import { Load } from "../components/Load";
import { BookGridSkeleton } from "../components/Skeleton";
import { VT, type ViewMode } from "../components/VT";
import { SearchBar } from "../components/SearchBar";
import { Section } from "../components/Section";
import { BGrid, BList } from "../components/BookViews";
import { BookSidebar } from "../components/BookSidebar";
import { ClearMenu } from "../components/ClearMenu";
import { toast } from "../lib/toast";
import { ExportModal } from "../components/ExportModal";
import { useViewport } from "../hooks/useViewport";
import { useBookSidebar } from "../hooks/useBookSidebar";
import { useMamEnabled } from "../hooks/useMamEnabled";
import { useBooksList } from "../hooks/useBooksList";
import { useMobileCodepath } from "../components/mobile";
import MobileBooksPage from "./MobileBooksPage";
import type {
  Book,
  BookAction,
} from "../types";

interface BooksPageProps {
  title: string;
  subtitle?: string;
  apiPath?: string;
  extraParams?: Record<string, string | number | boolean>;
  showAuthor?: boolean;
  exportFilter?: string;
  // When truthy, renders an Ebooks/Audiobooks/All tab row. The
  // selected tab is persisted per page-title and translates into a
  // `content_type` query param on every fetch. Omit for pages that
  // deliberately stay active-library-scoped (e.g. Hidden).
  showFormatTabs?: boolean;
  // v2.3.4.3: when truthy, renders an All / Owned / Discovered tab
  // row that injects `owned=true|false` into the API params. Used by
  // the Hidden page so users can find accidentally-hidden owned
  // books (Mark's UAT canary: 19 owned books bulk-hidden by mistake,
  // no way to surface them without scrolling past discovered misses).
  showOwnedFilter?: boolean;
}

// Bulk-action response shapes. All of them may carry an `error` string
// instead of the success keys, which is why each field is optional.
interface ClearScanDataResponse {
  error?: string;
  status?: string;
  books_cleared?: number;
  books_deleted?: number;
}
interface ScanMamResponse {
  // v2.4.x: scan now runs as a background task; this response shape
  // is the immediate ack after the POST returns. Live progress is
  // tracked via state._mam_scan_progress (polled separately).
  error?: string;
  status?: string;       // "started" or "complete" (zero-book case)
  total?: number;
}
interface ScanSourcesResponse {
  error?: string;
  status?: string;
  total?: number;
  message?: string;
}

type ClearType = "source" | "mam" | "both";

export default function BooksPage(props: BooksPageProps) {
  // Mobile codepath catches phones, iPads, and any touch device.
  const vp = useViewport();
  if (useMobileCodepath(vp)) {
    return <MobileBooksPage {...props} />;
  }
  return <DesktopBooksPage {...props} />;
}

function DesktopBooksPage({
  title,
  apiPath = "/books",
  extraParams = {},
  showAuthor = true,
  exportFilter,
  showFormatTabs = true,
  showOwnedFilter = false,
}: BooksPageProps) {
  const t = useTheme();
  const [vm, setVm] = usePersist<ViewMode>(`bp_${title}_vm`, "grid");
  const [grp, setGrp] = usePersist<string>(`bp_${title}_grp`, "all");
  // v2.11.1 N3: sort direction is a separate state. Persisted alongside
  // the sort field so toggling between fields keeps the user's chosen
  // direction. Defaults asc; the chevron toggle below flips it.
  const [sortDir, setSortDir] = usePersist<string>(`bp_${title}_sort_dir`, "asc");
  const isGrouped = grp !== "all";
  // The list, its filters and actions (shared with the phone page);
  // grouping fetches everything and sorts by the group.
  const {
    bks, total, totalPages, pg, setPg, ld, load, onAction: onBookAction,
    q, setQ, sort, setSort, fmt, setFmt, mamFilter, setMamFilter, ownedFilter, setOwnedFilter,
  } = useBooksList({
    title, apiPath, extraParams, showFormatTabs, showOwnedFilter, sortDir,
    sortOverride: grp === "author" ? "author" : grp === "series" ? "series" : undefined,
    perPage: isGrouped ? 5000 : 60,
  });
  const { sb, sbClosing, closeSb, openSb, toggleSb } = useBookSidebar();
  const [allCollapsed, setAllCollapsed] = useState(false);
  const [showExp, setShowExp] = useState(false);
  const mamOn = useMamEnabled();
  const [selMode, setSelMode] = useState(false);
  const [sel, setSel] = useState<Set<number>>(new Set());
  // v2.4.x: live MAM scan progress for the in-page banner. Polled
  // from /discovery/mam/scan/status when a scan is running. The
  // banner gives UAT visibility into bulk scans kicked off from
  // the multi-select bar (which previously left the page silent).
  const [mamScan, setMamScan] = useState<{
    running?: boolean;
    scanned?: number;
    total?: number;
    found?: number;
    possible?: number;
    not_found?: number;
    errors?: number;
    current_book?: string;
    status?: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);

  const toggleSel = (id: number) =>
    setSel((p) => {
      const n = new Set(p);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  // Adds the currently-visible page slice to the selection without
  // wiping cross-page selections — click on each page to accumulate.
  const selectAllVisible = () =>
    setSel((p) => new Set([...p, ...bks.map((b) => b.id)]));


  // v2.15.1 — listen for `seshat:focus` events with kind=book from
  // the global navbar search. When a user clicks a book result in
  // the dropdown, App routes to disc-library and dispatches this
  // event with the book id + library_slug. We fetch the full Book
  // row from the new GET /discovery/books/{bid}?slug= endpoint and
  // open BookSidebar with it.
  useEffect(() => {
    function onFocus(e: Event) {
      const detail = (e as CustomEvent<{
        kind?: string; book_id?: number; library_slug?: string | null;
      }>).detail;
      if (detail?.kind !== "book" || !detail.book_id) return;
      const qs = detail.library_slug ? `?slug=${encodeURIComponent(detail.library_slug)}` : "";
      api
        .get<Book>(`/discovery/books/${detail.book_id}${qs}`)
        .then((book) => {
          openSb(book);
        })
        .catch(() => { /* book not reachable — silently skip */ });
    }
    window.addEventListener("seshat:focus", onFocus);
    return () => window.removeEventListener("seshat:focus", onFocus);
  }, []);

  // Live MAM scan progress poller — fetches /discovery/mam/scan/status
  // every 3 seconds and updates the in-page banner. Auto-clears the
  // banner state when the scan transitions running→done so the banner
  // disappears after completion. Refreshes the books list once on
  // that transition so users see the new mam_status values land
  // without manually reloading.
  useEffect(() => {
    let active = true;
    let prevRunning = false;
    const tick = async () => {
      try {
        const r = await api.get<typeof mamScan & object>(
          "/discovery/mam/scan/status",
        );
        if (!active) return;
        const isRunning = !!r?.running;
        if (isRunning) {
          setMamScan(r);
        } else if (prevRunning) {
          // Just finished — show the final summary briefly, then clear.
          setMamScan(r);
          setTimeout(() => active && setMamScan(null), 4000);
          load(pg);
        } else {
          setMamScan(null);
        }
        prevRunning = isRunning;
      } catch {
        /* ignore — scan-status is non-critical */
      }
    };
    tick();
    const id = window.setInterval(tick, 3000);
    return () => {
      active = false;
      window.clearInterval(id);
    };
  }, [load, pg]);


  const onAction = async (act: BookAction, id: number, slug?: string) => {
    const scrollY = window.scrollY;
    await onBookAction(act, id, slug);
    setTimeout(() => window.scrollTo(0, scrollY), 100);
  };

  const dismissable = bks.filter((b) => !!b.is_new).length;

  const clearData = async (type: ClearType) => {
    const labels: Record<ClearType, string> = {
      source: "source scan",
      mam: "MAM scan",
      both: "all scan",
    };
    if (
      !confirm(
        `Clear ${labels[type]} data for ${sel.size} book(s)? ${
          type === "source" || type === "both"
            ? "Discovered (non-Calibre) selected books will be DELETED."
            : "MAM status will be reset and books will need re-scanning."
        }`,
      )
    )
      return;
    setBusy(true);
    try {
      const r = await api.post<ClearScanDataResponse>(
        "/discovery/books/clear-scan-data",
        {
          book_ids: [...sel],
          clear_source: type === "source" || type === "both",
          clear_mam: type === "mam" || type === "both",
        },
      );
      if (r.error) {
        toast.error(r.error);
      } else {
        toast.success(`Cleared ${labels[type]} data for ${sel.size} book(s)`);
        setSel(new Set());
        setSelMode(false);
        load(pg);
      }
    } catch (e) {
      toast.error((e as Error).message || "Error clearing data");
    }
    setBusy(false);
  };

  // v2.17.0 Feat C — bulk Hide / Delete on book-listing pages.
  // Behavior parity with the per-row context menu actions: Hide is
  // soft (sets books.hidden=1, recomputes series authority), Delete
  // is hard (removes the row, with Calibre-synced rows automatically
  // skipped server-side).
  const bulkHide = async () => {
    if (!confirm(`Hide ${sel.size} book(s)? They'll move to the Hidden page.`)) return;
    setBusy(true);
    try {
      const r = await api.post<{ status?: string; count?: number; error?: string }>(
        "/discovery/books/bulk-hide",
        { book_ids: [...sel] },
      );
      if (r.error) toast.error(r.error);
      else {
        toast.success(`Hid ${r.count ?? sel.size} book(s)`);
        setSel(new Set());
        setSelMode(false);
        load(pg);
      }
    } catch (e) {
      toast.error((e as Error).message || "Error hiding books");
    }
    setBusy(false);
  };

  const bulkDelete = async () => {
    if (!confirm(
      `Delete ${sel.size} book(s)? Calibre / Audiobookshelf-synced books will be skipped.`,
    )) return;
    setBusy(true);
    try {
      const r = await api.post<{ deleted?: number; skipped?: number; error?: string }>(
        "/discovery/books/bulk-delete",
        { book_ids: [...sel] },
      );
      if (r.error) toast.error(r.error);
      else {
        const parts = [`Deleted ${r.deleted ?? 0}`];
        if (r.skipped) parts.push(`skipped ${r.skipped} library-synced`);
        toast.success(parts.join(", "));
        setSel(new Set());
        setSelMode(false);
        load(pg);
      }
    } catch (e) {
      toast.error((e as Error).message || "Error deleting books");
    }
    setBusy(false);
  };

  const scanMam = async () => {
    if (
      !confirm(
        `Run a MAM scan against ${sel.size} selected book(s)? This will re-scan even already-scanned books.`,
      )
    )
      return;
    setBusy(true);
    try {
      const selected = sel.size;
      const r = await api.post<ScanMamResponse>(
        "/discovery/books/scan-mam",
        { book_ids: [...sel] },
      );
      if (r.error) {
        toast.error(r.error);
      } else {
        // Backend now spawns the scan as a background asyncio task and
        // returns immediately. Surface this with an immediate "started"
        // toast + dispatch the global scan-started event so any global
        // poller (or this page's own banner effect) picks it up.
        toast.info(
          `MAM scan started — ${r.total ?? selected} book${(r.total ?? selected) === 1 ? "" : "s"}. Track progress in the banner above.`,
        );
        setSel(new Set());
        setSelMode(false);
        window.dispatchEvent(new CustomEvent("seshat:scan-started"));
      }
    } catch (e) {
      toast.error((e as Error).message || "MAM scan failed");
    }
    setBusy(false);
  };

  const scanSources = async (scope?: "ebook" | "audiobook") => {
    const scopeLabel = scope
      ? `\n\nScope: ${scope === "audiobook" ? "every audiobook library" : "every ebook library"}.`
      : "";
    if (
      !confirm(
        `Run a source-plugin scan for the unique authors of ${sel.size} selected book(s)?\n\nNote: source plugins look up by author, so this will scan the WHOLE author for each unique author in your selection — not just the selected books.${scopeLabel}`,
      )
    )
      return;
    setBusy(true);
    try {
      // For cross-library scope, send pre-resolved author_names so the
      // backend skips the book_id→author SQL JOIN. Same rationale as
      // the Authors page: cross-library merged book IDs aren't valid
      // active-library IDs, so the JOIN can pick up the wrong author
      // (or none). Resolve names client-side from `bks` instead.
      const author_names = scope
        ? [
            ...new Set(
              [...sel]
                .map((bid) => bks.find((b) => b.id === bid)?.author_name)
                .filter((n): n is string => Boolean(n)),
            ),
          ]
        : undefined;
      const r = await api.post<ScanSourcesResponse>(
        "/discovery/books/scan-sources",
        {
          book_ids: [...sel],
          ...(author_names ? { author_names } : {}),
          ...(scope ? { content_type: scope } : {}),
        },
      );
      // v2.12.0 — gate scan-started toast on r.total > 0; backend
      // returns total=0 with a {message} when nothing matched.
      // v2.12.1 #3 — plain English with scope label + author-count.
      if ((r.total ?? 0) > 0) {
        const scopeWord = scope === "audiobook" ? "audiobook" : "ebook";
        toast.info(
          `Scanning ${scopeWord} sources for ${r.total} author(s). Track progress on the Dashboard.`,
        );
        window.dispatchEvent(new CustomEvent("seshat:scan-started"));
      } else {
        toast.warn(r.message || "Nothing to scan — no matching authors.");
      }
      setSel(new Set());
      setSelMode(false);
    } catch (e) {
      toast.error((e as Error).message || "Source scan failed to start");
    }
    setBusy(false);
  };

  // Memoize the expensive grouping+sort pass. The JSX on the outside is
  // cheap and re-runs every render; what's costly is the forEach bucketing
  // + Object.entries + localeCompare sort on thousands of books, which
  // would otherwise re-run on every keystroke during search, theme change,
  // etc. Scoping deps to [bks, grp] means grouping only recomputes when
  // the book list or grouping mode actually change.
  const groupedEntries = useMemo<[string, Book[]][] | null>(() => {
    if (grp === "author" && bks.length > 0) {
      const g: Record<string, Book[]> = {};
      bks.forEach((b) => {
        const k = b.author_name || "Unknown";
        if (!g[k]) g[k] = [];
        g[k].push(b);
      });
      return Object.entries(g).sort(([a], [b]) => a.localeCompare(b));
    }
    if (grp === "series" && bks.length > 0) {
      const g: Record<string, Book[]> = {};
      bks.forEach((b) => {
        const k = b.series_name || "Standalone";
        if (!g[k]) g[k] = [];
        g[k].push(b);
      });
      return Object.entries(g).sort(([a], [b]) =>
        a === "Standalone" ? 1 : b === "Standalone" ? -1 : a.localeCompare(b),
      );
    }
    return null;
  }, [bks, grp]);

  const viewProps = { selMode, sel, onToggleSel: toggleSel };
  let content: React.ReactNode;
  if (grp === "author" && groupedEntries) {
    content = groupedEntries.map(([name, books]) => (
      <Section key={name} title={name} count={books.length} defaultOpen={!allCollapsed}>
        {vm === "list" ? (
          <BList books={books} onAction={onAction} onBookClick={toggleSb} showAuthor={false} {...viewProps} />
        ) : (
          <BGrid books={books} onAction={onAction} onBookClick={toggleSb} showAuthor={false} {...viewProps} />
        )}
      </Section>
    ));
  } else if (grp === "series" && groupedEntries) {
    content = groupedEntries.map(([name, books]) => (
      <Section key={name} title={name} count={books.length} defaultOpen={!allCollapsed}>
        {vm === "list" ? (
          <BList books={books} onAction={onAction} onBookClick={toggleSb} showAuthor={showAuthor} {...viewProps} />
        ) : (
          <BGrid books={books} onAction={onAction} onBookClick={toggleSb} showAuthor={showAuthor} {...viewProps} />
        )}
      </Section>
    ));
  } else {
    content =
      vm === "list" ? (
        <BList books={bks} onAction={onAction} onBookClick={toggleSb} showAuthor={showAuthor} {...viewProps} />
      ) : (
        // v2.11.1 N3: pass showAuthor to BGrid so the author-name
        // caption renders on tile cards in grid view. Without this
        // BGrid's default-undefined `showAuthor` falls through to
        // BCard's `showAuthor && book.author_name` truthy-check
        // and the caption is silently skipped.
        <BGrid books={bks} onAction={onAction} onBookClick={toggleSb} showAuthor={showAuthor} {...viewProps} />
      );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* MAM scan progress banner — visible whenever a scan (kicked
          off from this page or anywhere else) is in flight. Surfaces
          the same data the Dashboard scan widget shows so the user
          isn't blind to the work happening in the background. */}
      {mamScan && (mamScan.running || mamScan.status === "complete") ? (
        <div
          style={{
            background: t.bg2,
            border: `1px solid ${t.accent}`,
            borderLeft: `4px solid ${t.accent}`,
            borderRadius: 6,
            padding: "10px 14px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            fontSize: 14,
          }}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <div style={{ fontWeight: 600, color: t.accent }}>
              {mamScan.running
                ? `MAM scan in progress — ${mamScan.scanned ?? 0} / ${mamScan.total ?? 0}`
                : `MAM scan complete — ${mamScan.scanned ?? 0} / ${mamScan.total ?? 0}`}
            </div>
            {mamScan.running && mamScan.current_book ? (
              <div style={{ fontSize: 12, color: t.td, fontStyle: "italic" }}>
                Currently scanning: {mamScan.current_book}
              </div>
            ) : null}
            <div style={{ fontSize: 12, color: t.td }}>
              {mamScan.found ?? 0} found · {mamScan.possible ?? 0} possible · {mamScan.not_found ?? 0} not on MAM
              {(mamScan.errors ?? 0) > 0 ? ` · ${mamScan.errors} error${mamScan.errors === 1 ? "" : "s"}` : ""}
            </div>
          </div>
        </div>
      ) : null}
      {/* Sticky sub-header — two rows */}
      <div
        className="bp-sticky"
        style={{
          position: "sticky",
          top: 56,
          zIndex: 40,
          background: t.bg + "ee",
          backdropFilter: "blur(8px)",
          padding: "8px 0",
          marginTop: -12,
        }}
      >
        {showFormatTabs ? (
          <FormatTabs
            fmt={fmt}
            setFmt={(v) => {
              setFmt(v);
              setPg(1);
            }}
          />
        ) : null}
        {showOwnedFilter ? (
          <OwnedFilterTabs
            value={ownedFilter}
            onChange={(v) => {
              setOwnedFilter(v);
              setPg(1);
            }}
          />
        ) : null}

        {/* Row 1: Title + Search/Sort/Filters */}
        <div
          className="page-header-row"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            marginBottom: 6,
          }}
        >
          <h1
            style={{
              fontSize: 24,
              fontWeight: 800,
              color: t.accent,
              margin: 0,
              flexShrink: 0,
            }}
          >
            {title}{" "}
            <span style={{ fontSize: 15, fontWeight: 600, color: t.td, marginLeft: 6 }}>
              {total.toLocaleString()}{" "}
              {fmt === "audiobook"
                ? "audiobooks"
                : fmt === "ebook"
                ? "ebooks"
                : "books"}
            </span>
          </h1>
          <div className="page-header-controls" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <SearchBar
              value={q}
              onChange={(v) => {
                setQ(v);
                setPg(1);
              }}
            />
            {!isGrouped && (
              <>
                <select
                  value={sort}
                  onChange={(e) => {
                    setSort(e.target.value);
                    setPg(1);
                  }}
                  style={{
                    padding: "6px 10px",
                    borderRadius: 6,
                    border: `1px solid ${t.border}`,
                    background: t.inp,
                    color: t.text2,
                    fontSize: 12,
                  }}
                >
                  <option value="title">Sort: Title</option>
                  <option value="author">Sort: Author</option>
                  <option value="series">Sort: Series</option>
                  <option value="date">Sort: Publication Date</option>
                  <option value="added">Sort: Date Added</option>
                </select>
                {/* v2.11.1 N3: sort direction toggle. Chevron flips
                    based on direction; click swaps and refetches. */}
                <button
                  onClick={() => {
                    setSortDir(sortDir === "asc" ? "desc" : "asc");
                    setPg(1);
                  }}
                  title={
                    sortDir === "asc"
                      ? "Ascending — click for descending"
                      : "Descending — click for ascending"
                  }
                  style={{
                    padding: "6px 10px",
                    borderRadius: 6,
                    border: `1px solid ${t.border}`,
                    background: t.inp,
                    color: t.text2,
                    fontSize: 12,
                    fontWeight: 700,
                    cursor: "pointer",
                    minWidth: 28,
                  }}
                >
                  {sortDir === "asc" ? "↑" : "↓"}
                </button>
              </>
            )}
            {mamOn ? (
              <select
                value={mamFilter}
                onChange={(e) => {
                  setMamFilter(e.target.value);
                  setPg(1);
                }}
                style={{
                  padding: "6px 10px",
                  borderRadius: 6,
                  border: `1px solid ${t.border}`,
                  background: mamFilter ? t.accent + "22" : t.inp,
                  color: mamFilter ? t.accent : t.text2,
                  fontSize: 12,
                }}
              >
                <option value="">MAM: All</option>
                <option value="found">MAM: Found</option>
                <option value="possible">MAM: Possible</option>
                <option value="not_found">MAM: Not Found</option>
                <option value="not_applicable">MAM: N/A</option>
                <option value="unscanned">MAM: Unscanned</option>
              </select>
            ) : null}
            <select
              value={grp}
              onChange={(e) => {
                setGrp(e.target.value);
                setPg(1);
              }}
              style={{
                padding: "6px 10px",
                borderRadius: 6,
                border: `1px solid ${t.border}`,
                background: t.inp,
                color: t.text2,
                fontSize: 12,
              }}
            >
              <option value="all">All</option>
              <option value="author">Group: Author</option>
              <option value="series">Group: Series</option>
            </select>
            <VT mode={vm} setMode={setVm} />
          </div>
        </div>

        {/* Row 2: Pagination + Actions */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 8,
          }}
        >
          <div style={{ flex: 1 }}>
            {!isGrouped && totalPages > 1 ? (
              <Pager
                pg={pg}
                totalPages={totalPages}
                onPage={(p) => {
                  load(p);
                  window.scrollTo(0, 0);
                }}
                t={t}
                compact
              />
            ) : (
              <div />
            )}
          </div>
          <div style={{ display: "flex", gap: 6, alignItems: "center", flexShrink: 0 }}>
            {isGrouped && (
              <Btn size="sm" variant="ghost" onClick={() => setAllCollapsed(!allCollapsed)}>
                {allCollapsed ? "Expand" : "Collapse"} All
              </Btn>
            )}
            {dismissable > 0 ? (
              <Btn
                size="sm"
                variant="ghost"
                onClick={async () => {
                  await api.post("/discovery/books/dismiss-all");
                  load(pg);
                }}
              >
                Dismiss ({dismissable})
              </Btn>
            ) : null}
            {exportFilter ? (
              <Btn size="sm" variant="ghost" onClick={() => setShowExp(true)}>
                Export
              </Btn>
            ) : null}
            <Btn
              size="sm"
              variant={selMode ? "accent" : "default"}
              onClick={() => {
                setSelMode(!selMode);
                if (selMode) setSel(new Set());
              }}
            >
              {selMode ? "Cancel" : "Select"}
            </Btn>
          </div>
        </div>

        {selMode ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "10px 14px",
              marginTop: 8,
              background: t.bg2,
              border: `1px solid ${t.border}`,
              borderRadius: 8,
              flexWrap: "wrap",
            }}
          >
            <span style={{ fontSize: 13, fontWeight: 600, color: t.text2 }}>
              {sel.size} book{sel.size === 1 ? "" : "s"} selected
            </span>
            {sel.size > 0 ? (
              <>
                <Btn
                  size="sm"
                  onClick={() => scanSources("ebook")}
                  disabled={busy}
                  title="Scans the unique authors of the selected books across every ebook library"
                  style={{
                    background: t.grn + "22",
                    color: t.grnt,
                    border: `1px solid ${t.grn}44`,
                  }}
                >
                  {busy ? "…" : ""} Scan Ebooks
                </Btn>
                <Btn
                  size="sm"
                  onClick={() => scanSources("audiobook")}
                  disabled={busy}
                  title="Scan these books' authors across every audiobook library"
                  style={{
                    background: t.pur + "22",
                    color: t.purt,
                    border: `1px solid ${t.pur}44`,
                  }}
                >
                  Scan Audiobooks
                </Btn>
                {mamOn ? (
                  <Btn
                    size="sm"
                    onClick={scanMam}
                    disabled={busy}
                    style={{
                      background: t.accent + "22",
                      color: t.accent,
                      border: `1px solid ${t.accent}44`,
                    }}
                  >
                    {busy ? "…" : ""} Scan MAM
                  </Btn>
                ) : null}
                <span
                  style={{
                    width: 1,
                    height: 20,
                    background: t.border,
                    margin: "0 4px",
                  }}
                />
                <ClearMenu
                  disabled={busy}
                  options={[
                    {
                      label: "Clear Source Data",
                      onClick: () => clearData("source"),
                    },
                    ...(mamOn
                      ? [
                          {
                            label: "Clear MAM Data",
                            onClick: () => clearData("mam"),
                          },
                          {
                            label: "Clear Both (Source + MAM)",
                            variant: "danger" as const,
                            divider: true,
                            onClick: () => clearData("both"),
                          },
                        ]
                      : []),
                  ]}
                />
                <span
                  style={{
                    width: 1,
                    height: 20,
                    background: t.border,
                    margin: "0 4px",
                  }}
                />
                {/* v2.17.0 Feat C — bulk Hide + Delete. Hide is soft
                    (books move to Hidden page); Delete is hard but
                    server-side-skips library-synced rows so a mixed
                    selection still does what it can. */}
                <Btn
                  size="sm"
                  onClick={bulkHide}
                  disabled={busy}
                  title="Hide selected books (moves them to the Hidden page)"
                  style={{
                    background: t.ylw + "22",
                    color: t.ylwt,
                    border: `1px solid ${t.ylw}44`,
                  }}
                >
                  Hide
                </Btn>
                <Btn
                  size="sm"
                  onClick={bulkDelete}
                  disabled={busy}
                  title="Delete selected books (Calibre / ABS-synced skipped)"
                  style={{
                    background: t.red + "22",
                    color: t.red,
                    border: `1px solid ${t.red}44`,
                  }}
                >
                  Delete
                </Btn>
                <span
                  style={{
                    width: 1,
                    height: 20,
                    background: t.border,
                    margin: "0 4px",
                  }}
                />
              </>
            ) : null}
            <Btn size="sm" onClick={selectAllVisible} disabled={busy}>
              Select All on Page
            </Btn>
            {sel.size > 0 ? (
              <Btn size="sm" onClick={() => setSel(new Set())} disabled={busy}>
                Deselect All
              </Btn>
            ) : null}
          </div>
        ) : null}
      </div>

      {ld ? (
        // Grid view shows BookCardSkeleton stand-ins; list view falls
        // back to the spinner since the table rows aren't worth a
        // bespoke skeleton (single column of dense text wouldn't read
        // as "still loading" the way card placeholders do).
        vm === "grid" ? <BookGridSkeleton /> : <Load />
      ) : (
        <>
          {content}
          {!isGrouped && totalPages > 1 && (
            <Pager
              pg={pg}
              totalPages={totalPages}
              onPage={(p) => {
                load(p);
                window.scrollTo(0, 0);
              }}
              t={t}
            />
          )}
        </>
      )}

      {sb && (
        <BookSidebar
          book={sb}
          closing={sbClosing}
          onClose={closeSb}
          onAction={onAction}
          onEdit={() => load(pg)}
        />
      )}
      {showExp && exportFilter ? (
        <ExportModal onClose={() => setShowExp(false)} defaultFilter={exportFilter} />
      ) : null}
    </div>
  );
}

// ─── Format Tabs (Ebooks / Audiobooks / All) ───────────────
// Renders above the sticky header on Library / Missing / Upcoming.
// The selected tab feeds `content_type` on the API call — "all" maps
// to a cross-library union across every library regardless of type,
// "ebook" / "audiobook" narrow to libraries of that type. A user
// with ABS-only still sees everything via the Audiobooks or All
// tab; the Ebooks tab in that setup will return an empty list.
function FormatTabs({
  fmt,
  setFmt,
}: {
  fmt: string;
  setFmt: (v: string) => void;
}) {
  const t = useTheme();
  const tabs: { id: string; label: string; icon: string }[] = [
    { id: "all", label: "All", icon: "" },
    { id: "ebook", label: "Ebooks", icon: "📖" },
    { id: "audiobook", label: "Audiobooks", icon: "🎧" },
  ];
  return (
    <div style={{ display: "flex", gap: 4, marginBottom: 8 }}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          onClick={() => setFmt(tab.id)}
          style={{
            background: fmt === tab.id ? t.abg : "transparent",
            color: fmt === tab.id ? t.accent : t.tm,
            border: `1px solid ${fmt === tab.id ? t.abr : "transparent"}`,
            borderRadius: 6,
            padding: "4px 12px",
            fontSize: 13,
            fontWeight: fmt === tab.id ? 600 : 500,
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: 5,
          }}
        >
          {tab.icon ? <span>{tab.icon}</span> : null}
          <span>{tab.label}</span>
        </button>
      ))}
    </div>
  );
}

// ─── Owned Filter Tabs (All / Owned / Discovered) ──────────
// v2.3.4.3 — used by the Hidden page to surface accidentally-hidden
// owned books. "owned" hides discovered/unowned rows; "discovered"
// hides owned rows. "all" is the default and matches pre-v2.3.4.3
// behavior.
function OwnedFilterTabs({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const t = useTheme();
  const tabs: { id: string; label: string; icon: string }[] = [
    { id: "all", label: "All", icon: "" },
    { id: "owned", label: "Owned only", icon: "✓" },
    { id: "discovered", label: "Discovered only", icon: "○" },
  ];
  return (
    <div style={{ display: "flex", gap: 4, marginBottom: 8 }}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          onClick={() => onChange(tab.id)}
          style={{
            background: value === tab.id ? t.abg : "transparent",
            color: value === tab.id ? t.accent : t.tm,
            border: `1px solid ${value === tab.id ? t.abr : "transparent"}`,
            borderRadius: 6,
            padding: "4px 12px",
            fontSize: 13,
            fontWeight: value === tab.id ? 600 : 500,
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: 5,
          }}
        >
          {tab.icon ? <span>{tab.icon}</span> : null}
          <span>{tab.label}</span>
        </button>
      ))}
    </div>
  );
}

function Pager({
  pg,
  totalPages,
  onPage,
  t,
  compact,
}: {
  pg: number;
  totalPages: number;
  onPage: (p: number) => void;
  t: Theme;
  compact?: boolean;
}) {
  const [jumpVal, setJumpVal] = useState("");
  const doJump = () => {
    const n = parseInt(jumpVal);
    if (n >= 1 && n <= totalPages) {
      onPage(n);
      setJumpVal("");
    }
  };
  return (
    <div
      style={{
        display: "flex",
        justifyContent: compact ? "flex-start" : "center",
        gap: 6,
        padding: compact ? "2px 0" : "12px 0",
        alignItems: "center",
      }}
    >
      <Btn size="sm" disabled={pg <= 1} onClick={() => onPage(1)}>
        «
      </Btn>
      <Btn size="sm" disabled={pg <= 1} onClick={() => onPage(pg - 1)}>
        ‹ Prev
      </Btn>
      <span
        style={{ fontSize: 13, color: t.td, fontWeight: 500, padding: "0 4px" }}
      >
        Page {pg} of {totalPages}
      </span>
      <Btn
        size="sm"
        disabled={pg >= totalPages}
        onClick={() => onPage(pg + 1)}
      >
        Next ›
      </Btn>
      <Btn
        size="sm"
        disabled={pg >= totalPages}
        onClick={() => onPage(totalPages)}
      >
        »
      </Btn>
      <span
        style={{
          width: 1,
          height: 16,
          background: t.border,
          margin: "0 2px",
        }}
      />
      <input
        value={jumpVal}
        onChange={(e) => setJumpVal(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") doJump();
        }}
        placeholder="#"
        style={{
          width: 50,
          padding: "4px 6px",
          borderRadius: 5,
          border: `1px solid ${t.border}`,
          background: t.inp,
          color: t.text2,
          fontSize: 12,
          textAlign: "center",
          outline: "none",
        }}
      />
      <Btn size="sm" variant="ghost" onClick={doJump}>
        Go
      </Btn>
    </div>
  );
}
