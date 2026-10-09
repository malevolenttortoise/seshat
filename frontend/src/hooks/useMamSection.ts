// The Discovery → MAM page's data: the section tabs (upload / download /
// missing everywhere) and their counts, the library tabs, the paged
// book list, and the MAM scan with its progress poll.
//
// DiscMAMPage + MobileMAMPage each carried this verbatim (wave 5b S9).
// The desktop page's extras (multi-select, clear data, bulk scans, view
// mode) stay in it; each shell keeps its scan-limit field, its alerts
// and (desktop) the scroll position kept across an action.
import { useCallback, useEffect, useState } from "react";
import { api, slugQuery } from "../api";
import { usePersist } from "./usePersist";
import type { Book, BookAction, MamStatusResponse } from "../types";

export interface MamScanStatus {
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

export interface LibraryOption {
  slug: string;
  content_type: string;
  label: string;
}

interface ScanStatusRow {
  kind?: string;
  slug?: string;
  content_type?: string;
  label?: string;
}

interface MamBooksResponse {
  books?: Book[];
  total?: number;
}

interface PipelineStatusResponse {
  configured?: boolean;
  reachable?: boolean;
}

interface StartScanResponse {
  error?: string;
  total?: number;
}

export const MAM_PER_PAGE = 50;
const SCAN_POLL_MS = 5000;

export function useMamSection() {
  const [tab, setTab] = usePersist<string>("mam_tab", "upload");
  // Per-library selector. `null` means use the active library
  // (back-compat, single-library installs). When multi-library is
  // discovered the user can toggle between ebook + audiobook MAM data.
  // Persisted so returning to the page reopens the same view.
  const [libSlug, setLibSlug] = usePersist<string | null>("mam_slug", null);
  const [libs, setLibs] = useState<LibraryOption[]>([]);
  const [books, setBooks] = useState<Book[]>([]);
  const [total, setTotal] = useState(0);
  const [pg, setPg] = useState(1);
  const [q, setQ] = useState("");
  const [sort, setSort] = usePersist("mam_sort", "title");
  const [ld, setLd] = useState(true);
  const [counts, setCounts] = useState({ upload: 0, download: 0, missing: 0, unscanned: 0 });
  const [scanStarting, setScanStarting] = useState(false);
  const [mamScan, setMamScan] = useState<MamScanStatus | null>(null);
  // Pipeline reachability — drives Send-to-pipeline button visibility.
  const [pipelineReady, setPipelineReady] = useState(false);

  // The three section counts + unscanned, from /discovery/mam/status.
  // On mount and after every action that could reshape them.
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

  // On mount: counts, a scan already running, the libraries, the pipeline.
  useEffect(() => {
    refreshCounts();
    api
      .get<MamScanStatus>("/discovery/mam/scan/status")
      .then((r) => {
        if (r.running) setMamScan(r);
      })
      .catch(() => {});
    // Discovered libraries, for the library tab bar: scan-status already
    // lists every library with slug / content_type / label. A single
    // library collapses the tabs.
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const load = useCallback(
    (page: number = 1, signal?: AbortSignal) => {
      setLd(true);
      const p = new URLSearchParams({
        section: tab,
        search: q,
        sort,
        page: String(page),
        per_page: String(MAM_PER_PAGE),
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

  // Poll while a scan is running; when it ends, refresh counts + list.
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
    }, SCAN_POLL_MS);
    return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mamScan?.running]);

  const totalPages = Math.max(1, Math.ceil(total / MAM_PER_PAGE));

  const switchTab = (tb: string) => {
    setTab(tb);
    setQ("");
    setSort("title");
    setPg(1);
  };

  /** Start a MAM scan of up to `limit` books; resolves to an error to show, or null. */
  const startScan = async (limit: number | ""): Promise<string | null> => {
    setScanStarting(true);
    try {
      const r = await api.post<StartScanResponse>(`/discovery/mam/scan?limit=${limit}`);
      if (r.error) return r.error;
      setMamScan({
        running: true,
        scanned: 0,
        total: r.total || (typeof limit === "number" ? limit : 100),
        found: 0,
        possible: 0,
        not_found: 0,
        errors: 0,
        status: "scanning",
        type: "manual",
      });
      return null;
    } catch {
      return "Failed to start scan";
    } finally {
      setScanStarting(false);
    }
  };

  const cancelScan = async () => {
    try {
      await api.post("/discovery/mam/scan/cancel");
    } catch {
      /* ignore — user sees no change */
    }
  };

  /** Hide / dismiss a book, then reload the current page. */
  const onAction = async (act: BookAction, id: number, slug?: string) => {
    if (act === "hide") await api.post(`/discovery/books/${id}/hide${slugQuery(slug)}`);
    if (act === "dismiss") await api.post(`/discovery/books/${id}/dismiss${slugQuery(slug)}`);
    await load(pg);
  };

  return {
    tab, switchTab, libSlug, setLibSlug, libs, books, total, totalPages, pg, setPg, q, setQ, sort, setSort,
    ld, counts, refreshCounts, load, scanStarting, mamScan, startScan, cancelScan, pipelineReady, onAction,
  };
}
