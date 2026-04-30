// MAM page — three-tab view of MAM scan results.
//
//   - upload:             owned books that aren't on MAM (potential uploads)
//   - download:           unowned books MAM has (find / possible matches)
//   - missing_everywhere: unowned books MAM also doesn't have
//
// Tabs are paginated and searchable. The page is a thin shell over
// /api/discovery/mam/books — the heavy lifting happens server-side.
// The unified scan widget on the Dashboard is what shows live scan
// progress; this page just displays the latest results.
//
// Found-on-MAM rows show a "Send to pipeline" button that POSTs to
// /api/discovery/send-to-pipeline, which calls inject_grab() on the
// pipeline dispatcher directly (no HTTP round-trip — same process).
import { useCallback, useEffect, useState } from "react";
import { useTheme } from "../theme";
import type { Theme } from "../theme";
import { api } from "../api";
import { usePersist } from "../hooks/usePersist";
import { Btn } from "../components/Btn";
import { Load } from "../components/Load";
import { BookGridSkeleton } from "../components/Skeleton";
import { VT, type ViewMode } from "../components/VT";
import { SearchBar } from "../components/SearchBar";
import { BGrid, BList } from "../components/BookViews";
import { BookSidebar } from "../components/BookSidebar";
import { ClearMenu } from "../components/ClearMenu";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import MobileMAMPage from "./MobileMAMPage";
import type {
  Book,
  BookAction,
  MamStatusResponse,
  NavFn,
  SendToPipelineFn,
} from "../types";

// The shape of a MAM scan progress snapshot returned by
// /discovery/mam/scan/status. Mirrors state._mam_scan_progress on the
// server — a loose dict of counters the Manual Scan Card renders.
interface MamScanStatus {
  running?: boolean;
  scanned?: number;
  total?: number;
  found?: number;
  possible?: number;
  not_found?: number;
  errors?: number;
  status?: string;
  type?: string;
  progress_pct?: number;
}

// GET /discovery/scan-status — the unified feed. We only peek at the
// library-kind rows here (the MAM page's library selector uses them
// to discover slugs + content_type), so every other field is optional.
interface ScanStatusRow {
  kind?: string;
  slug?: string;
  content_type?: string;
  label?: string;
}
interface ScanStatusResponse {
  scans?: ScanStatusRow[];
}

// Typed envelope for a library selector entry, derived from ScanStatusRow.
interface LibraryOption {
  slug: string;
  content_type: string;
  label: string;
}

// GET /discovery/mam/books — tabbed list of scan results.
interface MamBooksResponse {
  books?: Book[];
  total?: number;
}

// POST /discovery/mam/scan (limit-bounded run).
interface StartScanResponse {
  error?: string;
  total?: number;
}

// POST /discovery/books/clear-scan-data.
interface ClearScanDataResponse {
  error?: string;
  status?: string;
  books_cleared?: number;
  books_deleted?: number;
}

// POST /discovery/books/scan-mam (bulk).
interface BulkScanMamResponse {
  error?: string;
  scanned?: number;
  found?: number;
  possible?: number;
  not_found?: number;
  errors?: number;
}

// POST /discovery/books/scan-sources (bulk).
interface BulkScanSourcesResponse {
  error?: string;
  authors_scanned?: number;
  new_books?: number;
  errors?: number;
}

// POST /discovery/send-to-pipeline
interface SendToPipelineResponse {
  sent?: number;
  skipped?: number;
  message?: string;
}
interface PipelineStatusResponse {
  configured?: boolean;
  reachable?: boolean;
}

type ClearType = "source" | "mam" | "both";

interface TabDef {
  id: "upload" | "download" | "missing_everywhere";
  label: string;
  color: string;
  icon: string;
  desc: string;
}

export default function MAMPage(props: { onNav: NavFn }) {
  // Mobile codepath catches phones, iPads, and any touch device.
  const vp = useViewport();
  if (useMobileCodepath(vp)) {
    return <MobileMAMPage {...props} />;
  }
  return <DesktopMAMPage {...props} />;
}

function DesktopMAMPage({ onNav }: { onNav: NavFn }) {
  const t = useTheme();
  void onNav;

  // Tab + section data
  const [tab, setTab] = usePersist<string>("mam_tab", "upload");
  // Per-library selector. `null` means use the active library
  // (back-compat, single-library installs). When multi-library is
  // discovered we flip to the discovered slugs — user can toggle
  // between ebook + audiobook MAM data. Persisted so returning to
  // the page reopens the same view.
  const [libSlug, setLibSlug] = usePersist<string | null>("mam_slug", null);
  const [libs, setLibs] = useState<LibraryOption[]>([]);
  const [books, setBooks] = useState<Book[]>([]);
  const [total, setTotal] = useState(0);
  const [pg, setPg] = useState(1);
  const [q, setQ] = useState("");
  const [sort, setSort] = usePersist("mam_sort", "title");
  const [vm, setVm] = usePersist<ViewMode>("mam_vm", "list");
  const [ld, setLd] = useState(true);
  const perPage = 50;

  // Counts
  const [counts, setCounts] = useState({
    upload: 0,
    download: 0,
    missing: 0,
    unscanned: 0,
  });

  // Scan
  const [scanLimit, setScanLimit] = useState<number | "">(100);
  const [scanStarting, setScanStarting] = useState(false);
  const [mamScan, setMamScan] = useState<MamScanStatus | null>(null);

  // Sidebar
  const [sb, setSb] = useState<Book | null>(null);
  const [sbClosing, setSbClosing] = useState(false);

  // Multi-select
  const [selMode, setSelMode] = useState(false);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);

  // Pipeline reachability — drives Send-to-pipeline button visibility.
  const [pipelineReady, setPipelineReady] = useState(false);

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
    setSel((p) => new Set([...p, ...books.map((b) => b.id)]));

  // Helper: refresh the three section counts + unscanned from the
  // /discovery/mam/status endpoint. Called on mount and after every
  // action that could reshape the counts (scan, clear, bulk-scan).
  const refreshCounts = () =>
    api
      .get<MamStatusResponse>("/discovery/mam/status")
      .then((r) => {
        if (r.stats)
          setCounts({
            upload: r.stats.upload_candidates || 0,
            download: r.stats.available_to_download || 0,
            missing: r.stats.missing_everywhere || 0,
            unscanned: r.stats.total_unscanned || 0,
          });
      })
      .catch(() => {});

  // Load counts + check running scan on mount
  useEffect(() => {
    refreshCounts();
    api
      .get<MamScanStatus>("/discovery/mam/scan/status")
      .then((r) => {
        if (r.running) setMamScan(r);
      })
      .catch(() => {});
    // Discovered libraries, for the library-selector tab bar. Pulled
    // from scan-status which already lists every library with
    // slug/content_type/label. Single-library installs collapse the
    // tabs; multi-library installs get one tab per library.
    api
      .get<ScanStatusResponse>("/discovery/scan-status")
      .then((r) => {
        const rows = (r?.scans || []).filter((s) => s.kind === "library");
        setLibs(
          rows.map((s) => ({
            slug: s.slug || "",
            content_type: s.content_type || "ebook",
            label: s.label?.replace(/\s*Sync$/, "") || s.slug || "",
          })),
        );
      })
      .catch(() => {});
    api
      .get<PipelineStatusResponse>("/discovery/pipeline/status")
      .then((r) => setPipelineReady(!!r.configured && !!r.reachable))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load section data
  const load = useCallback(
    (page: number = 1, signal?: AbortSignal) => {
      setLd(true);
      const p = new URLSearchParams({
        section: tab,
        search: q,
        sort,
        page: String(page),
        per_page: String(perPage),
      });
      if (libSlug) p.set("slug", libSlug);
      return api
        .get<MamBooksResponse>(`/discovery/mam/books?${p}`, signal)
        .then((d) => {
          setBooks(d.books || []);
          setTotal(d.total || 0);
          setPg(page);
          setLd(false);
        })
        .catch((e) => {
          if (!api.isAbort(e)) setLd(false);
        });
    },
    [tab, q, sort, libSlug],
  );

  useEffect(() => {
    const c = new AbortController();
    load(1, c.signal);
    return () => c.abort();
  }, [load]);

  // Scan polling
  useEffect(() => {
    if (!mamScan?.running) return;
    const iv = setInterval(() => {
      api
        .get<MamScanStatus>("/discovery/mam/scan/status")
        .then((r) => {
          setMamScan(r);
          if (!r.running) {
            clearInterval(iv);
            refreshCounts();
            load(1);
          }
        })
        .catch(() => {});
    }, 5000);
    return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mamScan?.running]);

  const totalPages = Math.max(1, Math.ceil(total / perPage));
  const switchTab = (tb: string) => {
    setTab(tb);
    setQ("");
    setSort("title");
    setPg(1);
  };

  const startScan = async () => {
    setScanStarting(true);
    try {
      const r = await api.post<StartScanResponse>(
        `/discovery/mam/scan?limit=${scanLimit}`,
      );
      if (r.error) {
        alert(r.error);
        setScanStarting(false);
        return;
      }
      setMamScan({
        running: true,
        scanned: 0,
        total: r.total || (typeof scanLimit === "number" ? scanLimit : 100),
        found: 0,
        possible: 0,
        not_found: 0,
        errors: 0,
        status: "scanning",
        type: "manual",
      });
    } catch {
      alert("Failed to start scan");
    }
    setScanStarting(false);
  };

  const cancelScan = async () => {
    try {
      await api.post("/discovery/mam/scan/cancel");
    } catch {
      /* ignore — user sees no change */
    }
  };

  const closeSb = () => {
    if (!sb) return;
    setSbClosing(true);
    setTimeout(() => {
      setSb(null);
      setSbClosing(false);
    }, 200);
  };
  const toggleSb = (b: Book) => {
    if (sb && sb.id === b.id) closeSb();
    else {
      setSbClosing(false);
      setSb(b);
    }
  };

  const onAction = async (act: BookAction, id: number) => {
    const scrollY = window.scrollY;
    if (act === "hide") await api.post(`/discovery/books/${id}/hide`);
    if (act === "dismiss") await api.post(`/discovery/books/${id}/dismiss`);
    await load(pg);
    setTimeout(() => window.scrollTo(0, scrollY), 100);
  };

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
        alert(`Error: ${r.error}`);
      } else {
        setSel(new Set());
        setSelMode(false);
        load(pg);
        refreshCounts();
      }
    } catch (e) {
      alert(`Error: ${(e as Error).message || e}`);
    }
    setBusy(false);
  };

  const scanSelected = async () => {
    if (
      !confirm(
        `Run a MAM scan against ${sel.size} selected book(s)? This will re-scan even already-scanned books.`,
      )
    )
      return;
    setBusy(true);
    try {
      const r = await api.post<BulkScanMamResponse>(
        "/discovery/books/scan-mam",
        { book_ids: [...sel] },
      );
      if (r.error) {
        alert(`MAM scan failed: ${r.error}`);
      } else {
        alert(
          `MAM scan complete: ${r.scanned || 0} scanned, ${r.found || 0} found, ${r.possible || 0} possible, ${r.not_found || 0} not on MAM` +
            (r.errors ? `, ${r.errors} errors` : ""),
        );
        setSel(new Set());
        setSelMode(false);
        load(pg);
        refreshCounts();
      }
    } catch (e) {
      alert(`MAM scan failed: ${(e as Error).message || e}`);
    }
    setBusy(false);
  };

  const sendToPipeline: SendToPipelineFn = async (bookIds) => {
    if (!bookIds || !bookIds.length) return;
    setBusy(true);
    try {
      const r = await api.post<SendToPipelineResponse>(
        "/discovery/send-to-pipeline",
        { book_ids: bookIds },
      );
      if ((r.sent || 0) > 0) {
        alert(
          `Sent ${r.sent} book(s) to pipeline!${r.skipped ? ` (${r.skipped} skipped — not Found)` : ""}`,
        );
      } else {
        alert(r.message || "No books sent");
      }
      setSel(new Set());
      setSelMode(false);
    } catch (e) {
      alert(`Send failed: ${(e as Error).message || e}`);
    }
    setBusy(false);
  };

  const scanSelectedSources = async () => {
    if (
      !confirm(
        `Run a source-plugin scan for the unique authors of ${sel.size} selected book(s)?\n\nNote: source plugins look up by author, so this will scan the WHOLE author for each unique author in your selection — not just the selected books.`,
      )
    )
      return;
    setBusy(true);
    try {
      const r = await api.post<BulkScanSourcesResponse>(
        "/discovery/books/scan-sources",
        { book_ids: [...sel] },
      );
      if (r.error) {
        alert(`Source scan failed: ${r.error}`);
      } else {
        alert(
          `Source scan complete: ${r.authors_scanned || 0} author(s) scanned, ${r.new_books || 0} new books found` +
            (r.errors ? `, ${r.errors} errors` : ""),
        );
        setSel(new Set());
        setSelMode(false);
        load(pg);
      }
    } catch (e) {
      alert(`Source scan failed: ${(e as Error).message || e}`);
    }
    setBusy(false);
  };

  const tabDefs: TabDef[] = [
    {
      id: "upload",
      label: "Upload Candidates",
      color: t.grnt,
      icon: "↑",
      desc: "Books you own that aren't on MAM — potential uploads",
    },
    {
      id: "download",
      label: "Available on MAM",
      color: t.cyant || t.cyan,
      icon: "↓",
      desc: "Missing books found on MAM — ready to grab",
    },
    {
      id: "missing_everywhere",
      label: "Missing Everywhere",
      color: t.tg,
      icon: "∅",
      desc: "Neither you nor MAM have these books",
    },
  ];
  const activeTab = tabDefs.find((x) => x.id === tab) || tabDefs[0];
  const countFor = (id: string) =>
    id === "upload"
      ? counts.upload
      : id === "download"
      ? counts.download
      : counts.missing;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Header */}
      <div>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: t.text, margin: 0 }}>
          MyAnonamouse
        </h1>
        <p style={{ fontSize: 13, color: t.td, marginTop: 4 }}>
          {counts.unscanned > 0
            ? `${counts.unscanned} books not yet scanned`
            : "All books scanned"}
        </p>
      </div>

      {/* Manual Scan Card */}
      <div
        style={{
          background: t.bg2,
          border: `1px solid ${t.border}`,
          borderRadius: 12,
          padding: 20,
        }}
      >
        <div
          style={{
            fontSize: 12,
            fontWeight: 600,
            color: t.tm,
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            marginBottom: 12,
          }}
        >
          Manual Scan
        </div>

        {mamScan?.running ? (
          <div>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                fontSize: 12,
                color: t.td,
                marginBottom: 6,
              }}
            >
              <span>
                {mamScan.status === "paused"
                  ? "Paused (5 min between batches)"
                  : mamScan.status === "waiting (author scan running)"
                  ? "Waiting for author scan..."
                  : "Scanning..."}{" "}
                {mamScan.scanned || 0} of {mamScan.total ?? "?"}
              </span>
            </div>
            <div
              style={{
                height: 8,
                borderRadius: 4,
                background: t.bg4,
                overflow: "hidden",
                marginBottom: 8,
              }}
            >
              <div
                style={{
                  width: `${mamScan.total ? Math.round(((mamScan.scanned || 0) / mamScan.total) * 100) : 0}%`,
                  height: "100%",
                  borderRadius: 4,
                  background: t.accent,
                  transition: "width 0.5s",
                }}
              />
            </div>
            <div
              style={{ display: "flex", gap: 14, fontSize: 12, marginBottom: 10 }}
            >
              <span style={{ color: t.grnt }}>
                Found: <b>{mamScan.found || 0}</b>
              </span>
              <span style={{ color: t.ylwt }}>
                Possible: <b>{mamScan.possible || 0}</b>
              </span>
              <span style={{ color: t.redt }}>
                Not found: <b>{mamScan.not_found || 0}</b>
              </span>
              {(mamScan.errors || 0) > 0 ? (
                <span style={{ color: t.red }}>
                  Errors: <b>{mamScan.errors}</b>
                </span>
              ) : null}
            </div>
            <Btn
              size="sm"
              onClick={cancelScan}
              style={{
                background: t.red + "22",
                color: t.redt,
                border: `1px solid ${t.red}44`,
              }}
            >
              Cancel scan
            </Btn>
          </div>
        ) : mamScan?.status === "complete" ? (
          <div>
            <div
              style={{
                display: "flex",
                gap: 14,
                fontSize: 13,
                color: t.text2,
                padding: "8px 12px",
                background: t.grn + "15",
                borderRadius: 8,
                border: `1px solid ${t.grn}33`,
                marginBottom: 10,
              }}
            >
              <span>✓ Complete — {mamScan.scanned || 0} scanned:</span>
              <span style={{ color: t.grnt }}>{mamScan.found || 0} found</span>
              <span style={{ color: t.ylwt }}>
                {mamScan.possible || 0} possible
              </span>
              <span style={{ color: t.redt }}>
                {mamScan.not_found || 0} not found
              </span>
            </div>
            <ScanLimitRow
              scanLimit={scanLimit}
              setScanLimit={setScanLimit}
              scanStarting={scanStarting}
              startScan={startScan}
              unscanned={counts.unscanned}
              t={t}
            />
          </div>
        ) : (
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <ScanLimitRow
              scanLimit={scanLimit}
              setScanLimit={setScanLimit}
              scanStarting={scanStarting}
              startScan={startScan}
              unscanned={counts.unscanned}
              t={t}
            />
            {counts.unscanned === 0 ? (
              <span style={{ fontSize: 12, color: t.grnt }}>✓ All scanned</span>
            ) : null}
          </div>
        )}
      </div>

      {/* Library selector — hidden on single-library installs. The
          Audiobook tab serves whatever ABS data the DB currently has;
          until the MAM search path is extended to accept audiobook
          main_cat IDs, audiobook entries render mostly as "not
          scanned". See post-Phase-7 notes. */}
      {libs.length > 1 ? (
        <div style={{ display: "flex", gap: 4, marginBottom: 4 }}>
          {libs.map((l) => {
            const active = (libSlug ?? libs[0]?.slug) === l.slug;
            const color =
              l.content_type === "audiobook" ? t.pur || t.accent : t.accent;
            return (
              <button
                key={l.slug}
                onClick={() => {
                  setLibSlug(l.slug);
                  setPg(1);
                }}
                style={{
                  padding: "6px 14px",
                  background: active ? color + "22" : "transparent",
                  color: active ? color : t.tm,
                  border: `1px solid ${active ? color + "66" : "transparent"}`,
                  borderRadius: 6,
                  fontSize: 13,
                  fontWeight: active ? 600 : 500,
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  gap: 5,
                }}
              >
                {l.content_type === "audiobook" ? "🎧" : "📖"}{" "}
                <span>{l.label}</span>
              </button>
            );
          })}
        </div>
      ) : null}

      {/* Tab Bar */}
      <div
        style={{
          display: "flex",
          gap: 0,
          borderBottom: `2px solid ${t.borderL}`,
          overflowX: "auto",
        }}
      >
        {tabDefs.map((tb) => (
          <button
            key={tb.id}
            onClick={() => switchTab(tb.id)}
            style={{
              padding: "10px 16px",
              background: "none",
              border: "none",
              borderBottom:
                tab === tb.id
                  ? `2px solid ${tb.color}`
                  : "2px solid transparent",
              marginBottom: -2,
              cursor: "pointer",
              display: "flex",
              alignItems: "center",
              gap: 6,
              fontSize: 13,
              fontWeight: tab === tb.id ? 600 : 400,
              color: tab === tb.id ? tb.color : t.tg,
              transition: "color 0.15s",
              whiteSpace: "nowrap",
              flexShrink: 0,
            }}
          >
            <span>{tb.icon}</span>
            <span>{tb.label}</span>
            <span
              style={{
                background: tab === tb.id ? tb.color + "22" : t.bg4,
                color: tab === tb.id ? tb.color : t.tg,
                padding: "1px 6px",
                borderRadius: 10,
                fontSize: 11,
                fontWeight: 600,
              }}
            >
              {countFor(tb.id)}
            </span>
          </button>
        ))}
      </div>

      {/* Section description + Upload button */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: 8,
        }}
      >
        <p
          style={{
            fontSize: 12,
            color: t.tg,
            fontStyle: "italic",
            margin: 0,
          }}
        >
          {activeTab.desc}
        </p>
        {tab === "upload" ? (
          <a
            href="https://www.myanonamouse.net/tor/upload.php"
            target="_blank"
            rel="noopener noreferrer"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              padding: "6px 14px",
              borderRadius: 6,
              fontSize: 12,
              fontWeight: 600,
              textDecoration: "none",
              background: t.grn + "22",
              color: t.grnt,
              border: `1px solid ${t.grn}44`,
            }}
          >
            Upload to MAM ↗
          </a>
        ) : null}
      </div>

      {/* Controls — sticky */}
      <div
        style={{
          position: "sticky",
          top: 56,
          zIndex: 20,
          background: t.bg + "ee",
          backdropFilter: "blur(8px)",
          padding: "8px 0",
        }}
      >
        <div
          className="bp-controls page-header-row"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 8,
          }}
        >
          <div style={{ fontSize: 14, fontWeight: 600, color: t.td }}>
            {total} books
          </div>
          <div
            className="bp-right page-header-controls"
            style={{
              display: "flex",
              gap: 8,
              alignItems: "center",
              flexWrap: "wrap",
            }}
          >
            <SearchBar
              value={q}
              onChange={(v) => {
                setQ(v);
                setPg(1);
              }}
            />
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
              <option value="date">Sort: Date</option>
              <option value="series">Sort: Series</option>
            </select>
            <VT mode={vm} setMode={setVm} />
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
        {totalPages > 1 && !ld && (
          <MamPager
            pg={pg}
            totalPages={totalPages}
            onPage={(p) => load(p)}
            t={t}
          />
        )}
      </div>

      {selMode ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "10px 14px",
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
                onClick={scanSelectedSources}
                disabled={busy}
                title="Scans the unique authors of the selected books — note that source plugins lookup by author, not by individual book"
                style={{
                  background: t.grn + "22",
                  color: t.grnt,
                  border: `1px solid ${t.grn}44`,
                }}
              >
                {busy ? "…" : ""} Scan Sources
              </Btn>
              <Btn
                size="sm"
                onClick={scanSelected}
                disabled={busy}
                style={{
                  background: t.accent + "22",
                  color: t.accent,
                  border: `1px solid ${t.accent}44`,
                }}
              >
                {busy ? "…" : ""} Scan MAM
              </Btn>
              {pipelineReady && tab === "download" ? (
                <Btn
                  size="sm"
                  onClick={() => sendToPipeline([...sel])}
                  disabled={busy}
                  style={{
                    background: t.pur + "22",
                    color: t.purt,
                    border: `1px solid ${t.pur}44`,
                  }}
                >
                  {busy ? "…" : "⬇"} Send to pipeline
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
                  {
                    label: "Clear MAM Data",
                    onClick: () => clearData("mam"),
                  },
                  {
                    label: "Clear Both (Source + MAM)",
                    variant: "danger",
                    divider: true,
                    onClick: () => clearData("both"),
                  },
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

      {/* Book list */}
      {ld ? (
        vm === "grid" ? <BookGridSkeleton /> : <Load />
      ) : books.length === 0 ? (
        <div style={{ textAlign: "center", padding: 40, color: t.tg }}>
          No books in this section
        </div>
      ) : vm === "list" ? (
        <BList
          books={books}
          onAction={onAction}
          onBookClick={toggleSb}
          showAuthor={true}
          showMamLink={tab === "download"}
          onSendToPipeline={
            pipelineReady && tab === "download" ? sendToPipeline : undefined
          }
          selMode={selMode}
          sel={sel}
          onToggleSel={toggleSel}
        />
      ) : (
        <BGrid
          books={books}
          onAction={onAction}
          onBookClick={toggleSb}
          showAuthor={true}
          showMamLink={tab === "download"}
          onSendToPipeline={
            pipelineReady && tab === "download" ? sendToPipeline : undefined
          }
          selMode={selMode}
          sel={sel}
          onToggleSel={toggleSel}
        />
      )}

      {/* Pagination */}
      {totalPages > 1 && !ld ? (
        <div
          style={{
            display: "flex",
            justifyContent: "center",
            gap: 6,
            paddingTop: 8,
            alignItems: "center",
          }}
        >
          <Btn size="sm" variant="ghost" onClick={() => load(1)} disabled={pg <= 1}>
            «
          </Btn>
          <Btn
            size="sm"
            variant="ghost"
            onClick={() => load(pg - 1)}
            disabled={pg <= 1}
          >
            ‹ Prev
          </Btn>
          <span
            style={{
              fontSize: 13,
              color: t.tg,
              padding: "4px 6px",
              fontWeight: 500,
            }}
          >
            Page {pg} of {totalPages}
          </span>
          <Btn
            size="sm"
            variant="ghost"
            onClick={() => load(pg + 1)}
            disabled={pg >= totalPages}
          >
            Next ›
          </Btn>
          <Btn
            size="sm"
            variant="ghost"
            onClick={() => load(totalPages)}
            disabled={pg >= totalPages}
          >
            »
          </Btn>
        </div>
      ) : null}

      {/* Sidebar */}
      {sb ? (
        <BookSidebar
          book={sb}
          closing={sbClosing}
          onClose={closeSb}
          onAction={onAction}
          onEdit={() => load(pg)}
        />
      ) : null}
    </div>
  );
}

// Shared scan-input row rendered in both the idle and post-complete
// states of the Manual Scan Card. Pulled out because the two branches
// were 95% identical markup with only the surrounding copy differing.
function ScanLimitRow({
  scanLimit,
  setScanLimit,
  scanStarting,
  startScan,
  unscanned,
  t,
}: {
  scanLimit: number | "";
  setScanLimit: (v: number | "") => void;
  scanStarting: boolean;
  startScan: () => void;
  unscanned: number;
  t: Theme;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <span style={{ fontSize: 12, color: t.tg }}>Scan</span>
      <input
        type="number"
        value={scanLimit}
        onChange={(e) => setScanLimit(parseInt(e.target.value) || "")}
        onBlur={() => {
          if (!scanLimit || (typeof scanLimit === "number" && scanLimit < 1))
            setScanLimit(100);
        }}
        style={{
          width: 70,
          padding: "6px 8px",
          background: t.inp,
          border: `1px solid ${t.border}`,
          borderRadius: 6,
          color: t.text2,
          fontSize: 13,
          textAlign: "center",
        }}
      />
      <span style={{ fontSize: 12, color: t.tg }}>books</span>
      <Btn
        size="sm"
        variant="accent"
        onClick={startScan}
        disabled={scanStarting || unscanned === 0}
      >
        {scanStarting ? "Starting..." : "Start Scan"}
      </Btn>
    </div>
  );
}

function MamPager({
  pg,
  totalPages,
  onPage,
  t,
}: {
  pg: number;
  totalPages: number;
  onPage: (p: number) => void;
  t: Theme;
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
        gap: 6,
        padding: "4px 0",
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
        style={{
          fontSize: 13,
          color: t.td,
          fontWeight: 500,
          padding: "0 4px",
        }}
      >
        Page {pg} of {totalPages}
      </span>
      <Btn size="sm" disabled={pg >= totalPages} onClick={() => onPage(pg + 1)}>
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
