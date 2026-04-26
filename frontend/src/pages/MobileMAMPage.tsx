// Mobile-native MAM page. Three tabs (upload / download / missing
// everywhere), per-library scoping when multi-lib, search + sort,
// manual scan controls (collapsed), and live scan progress.
//
// Features intentionally dropped from the mobile surface:
//   - View toggle (always card list)
//   - Bulk-select mode (admin-y; revisit in Phase 6)
// Send-to-pipeline is still available per-card via MobileBookCard.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { usePersist } from "../hooks/usePersist";
import { BookSidebar } from "../components/BookSidebar";
import { Ic } from "../icons";
import {
  MobileInput,
  MobileChip,
  MobileBookCard,
  MobilePagination,
  MobileSection,
  MobileBtn,
  MobileSheet,
  MobileRow,
  MobileBackButton,
} from "../components/mobile";
import type {
  Book,
  BookAction,
  MamStatusResponse,
  NavFn,
  SendToPipelineFn,
} from "../types";

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

interface ScanStatusRow {
  kind?: string;
  slug?: string;
  content_type?: string;
  label?: string;
}

interface LibraryOption {
  slug: string;
  content_type: string;
  label: string;
}

interface MamBooksResponse {
  books?: Book[];
  total?: number;
}

interface PipelineStatusResponse {
  configured?: boolean;
  reachable?: boolean;
}

interface SendToPipelineResponse {
  sent?: number;
  skipped?: number;
  message?: string;
}

interface StartScanResponse {
  error?: string;
  total?: number;
}

const TAB_OPTIONS: { value: string; label: string; icon: string }[] = [
  { value: "upload", label: "Upload", icon: "↑" },
  { value: "download", label: "Available", icon: "↓" },
  { value: "missing_everywhere", label: "Missing", icon: "∅" },
];

const SORT_OPTIONS: { value: string; label: string }[] = [
  { value: "title", label: "Title" },
  { value: "author", label: "Author" },
  { value: "series", label: "Series" },
  { value: "pub_date", label: "Pub Date" },
];

export default function MobileMAMPage({ onNav }: { onNav: NavFn }) {
  const t = useTheme();
  void onNav;

  const [tab, setTab] = usePersist<string>("mam_tab", "upload");
  const [libSlug, setLibSlug] = usePersist<string | null>("mam_slug", null);
  const [libs, setLibs] = useState<LibraryOption[]>([]);
  const [books, setBooks] = useState<Book[]>([]);
  const [total, setTotal] = useState(0);
  const [pg, setPg] = useState(1);
  const [q, setQ] = useState("");
  const [sort, setSort] = usePersist("mam_sort", "title");
  const [ld, setLd] = useState(true);
  const [counts, setCounts] = useState({
    upload: 0,
    download: 0,
    missing: 0,
    unscanned: 0,
  });
  const [scanLimit, setScanLimit] = useState<number>(100);
  const [scanStarting, setScanStarting] = useState(false);
  const [mamScan, setMamScan] = useState<MamScanStatus | null>(null);
  const [sb, setSb] = useState<Book | null>(null);
  const [sbClosing, setSbClosing] = useState(false);
  const [sortSheet, setSortSheet] = useState(false);
  const [pipelineReady, setPipelineReady] = useState(false);

  const perPage = 50;

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

  useEffect(() => {
    refreshCounts();
    api
      .get<MamScanStatus>("/discovery/mam/scan/status")
      .then((r) => {
        if (r.running) setMamScan(r);
      })
      .catch(() => {});
    api
      .get<{ scans?: ScanStatusRow[] }>("/discovery/scan-status")
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
  }, []);

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

  // Poll while a scan is running.
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
        total: r.total || scanLimit,
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
    } catch { /* ignore */ }
  };

  const closeSb = () => {
    if (!sb) return;
    setSbClosing(true);
    setTimeout(() => {
      setSb(null);
      setSbClosing(false);
    }, 200);
  };

  const onAction = async (act: BookAction, id: number) => {
    if (act === "hide") await api.post(`/discovery/books/${id}/hide`);
    if (act === "dismiss") await api.post(`/discovery/books/${id}/dismiss`);
    await load(pg);
  };

  const sendToPipeline: SendToPipelineFn = async (bookIds) => {
    if (!bookIds || !bookIds.length) return;
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
    } catch (e) {
      alert(`Send failed: ${(e as Error).message || e}`);
    }
  };

  const sortLabel =
    SORT_OPTIONS.find((o) => o.value === sort)?.label || "Title";

  const tabCount = (v: string): number => {
    if (v === "upload") return counts.upload;
    if (v === "download") return counts.download;
    if (v === "missing_everywhere") return counts.missing;
    return 0;
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />
      {/* Page title */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>
          MAM Search
        </h1>
        <span style={{ fontSize: 13, color: t.td }}>
          {ld ? "…" : `${total.toLocaleString()} in tab`}
        </span>
      </div>

      {/* Library selector — only when multi-library */}
      {libs.length > 1 && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 6,
          }}
        >
          <MobileChip
            active={libSlug === null}
            onClick={() => setLibSlug(null)}
          >
            All
          </MobileChip>
          {libs.map((lib) => (
            <MobileChip
              key={lib.slug}
              active={libSlug === lib.slug}
              onClick={() => setLibSlug(lib.slug)}
            >
              {lib.content_type === "audiobook" ? "🎧 " : "📖 "}
              {lib.label}
            </MobileChip>
          ))}
        </div>
      )}

      {/* Tab chips */}
      <div
        style={{
          display: "flex",
          gap: 6,
          overflowX: "auto",
          scrollbarWidth: "none",
        }}
      >
        {TAB_OPTIONS.map((opt) => (
          <MobileChip
            key={opt.value}
            active={tab === opt.value}
            onClick={() => switchTab(opt.value)}
          >
            {opt.icon} {opt.label} ({tabCount(opt.value).toLocaleString()})
          </MobileChip>
        ))}
      </div>

      {/* Search */}
      <MobileInput
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search title or author"
        leadingIcon={Ic.search}
        trailing={
          q ? (
            <button
              onClick={() => setQ("")}
              style={{
                background: "none",
                border: "none",
                cursor: "pointer",
                color: t.tg,
                padding: 4,
                display: "flex",
                width: 32,
                height: 32,
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              {Ic.x}
            </button>
          ) : undefined
        }
      />

      {/* Sort chip */}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        <MobileChip onClick={() => setSortSheet(true)} leadingIcon="↕">
          Sort: {sortLabel}
        </MobileChip>
        {counts.unscanned > 0 && (
          <MobileChip>
            {counts.unscanned.toLocaleString()} unscanned
          </MobileChip>
        )}
      </div>

      {/* Manual scan section — collapsed by default */}
      <MobileSection
        title="Manual Scan"
        subtitle={
          mamScan?.running
            ? `Scanning… ${mamScan.scanned ?? 0}/${mamScan.total ?? "?"}`
            : "Scan unscanned books against MAM"
        }
        defaultOpen={!!mamScan?.running}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {mamScan?.running ? (
            <>
              <div
                style={{
                  height: 6,
                  background: t.bg3,
                  borderRadius: 999,
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    width: `${
                      mamScan.total
                        ? Math.min(
                            100,
                            ((mamScan.scanned ?? 0) / mamScan.total) * 100,
                          )
                        : 0
                    }%`,
                    height: "100%",
                    background: t.accent,
                    transition: "width 0.3s",
                  }}
                />
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(4, 1fr)",
                  gap: 6,
                  fontSize: 12,
                }}
              >
                <div style={{ color: t.grn }}>✓ {mamScan.found ?? 0}</div>
                <div style={{ color: t.ylw }}>? {mamScan.possible ?? 0}</div>
                <div style={{ color: t.red }}>✗ {mamScan.not_found ?? 0}</div>
                <div style={{ color: t.tg }}>! {mamScan.errors ?? 0}</div>
              </div>
              <MobileBtn variant="ghost" onClick={cancelScan}>
                Cancel scan
              </MobileBtn>
            </>
          ) : (
            <>
              <label
                style={{
                  fontSize: 13,
                  color: t.td,
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                }}
              >
                Limit
                <select
                  value={scanLimit}
                  onChange={(e) => setScanLimit(Number(e.target.value))}
                  style={{
                    flex: 1,
                    minHeight: 44,
                    padding: "0 12px",
                    background: t.inp,
                    color: t.text,
                    border: `1px solid ${t.border}`,
                    borderRadius: 10,
                    fontSize: 16,
                  }}
                >
                  <option value={50}>50 books</option>
                  <option value={100}>100 books</option>
                  <option value={250}>250 books</option>
                  <option value={500}>500 books</option>
                  <option value={1000}>1,000 books</option>
                </select>
              </label>
              <MobileBtn
                variant="primary"
                primary
                fullWidth
                onClick={startScan}
                disabled={scanStarting || counts.unscanned === 0}
              >
                {scanStarting
                  ? "Starting…"
                  : counts.unscanned === 0
                    ? "All scanned"
                    : `Scan ${Math.min(scanLimit, counts.unscanned)} books`}
              </MobileBtn>
            </>
          )}
        </div>
      </MobileSection>

      {/* Book list */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 360px), 1fr))",
          gap: 8,
        }}
      >
        {books.map((b) => (
          <MobileBookCard
            key={b.id}
            book={b}
            onClick={() => setSb(b)}
            showAuthor
            showMamLink
            onSendToPipeline={pipelineReady ? sendToPipeline : undefined}
          />
        ))}
      </div>

      {!ld && books.length === 0 && (
        <div
          style={{
            padding: 24,
            textAlign: "center",
            color: t.tg,
            fontSize: 14,
            background: t.bg2,
            border: `1px solid ${t.borderL}`,
            borderRadius: 12,
          }}
        >
          {q ? "No books match your search." : "Nothing in this tab."}
        </div>
      )}

      <MobilePagination
        page={pg}
        totalPages={totalPages}
        onPrev={() => load(pg - 1)}
        onNext={() => load(pg + 1)}
      />

      <MobileSheet
        open={sortSheet}
        onClose={() => setSortSheet(false)}
        title="Sort by"
        height="auto"
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {SORT_OPTIONS.map((opt) => (
            <MobileRow
              key={opt.value}
              title={opt.label}
              active={sort === opt.value}
              hideChevron
              onClick={() => {
                setSort(opt.value);
                setSortSheet(false);
              }}
            />
          ))}
        </div>
      </MobileSheet>

      {sb && (
        <BookSidebar
          book={sb}
          closing={sbClosing}
          onClose={closeSb}
          onAction={onAction}
          onEdit={() => load(pg)}
        />
      )}
    </div>
  );
}
