// The Dashboard's data and commands, for UnifiedDashboard and
// MobileUnifiedDashboard (wave 5b S15, issue 23 / G140), which carried
// them line for line.
//
// - `useDashboardData`: the 10-endpoint fan-out (each failure falls back
//   on its own), one /stats call per library, and the MAM figures the
//   `mam-stats` SSE event patches in place.
// - `useDashboardCommands`: Sync / Scan Ebooks / Scan Audiobooks / MAM
//   Scan / Data Hygiene and their Stops, each refreshing after. A failure
//   goes to `onFail` with its message (G148 wording); the shells toast it.
// - `useDashboard`: both, polled every 30s (every 3s while a scan or a
//   sync runs) while the tab is visible.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { MamBudgetFields } from "../lib/format";
import type { MamStatusResponse, ScanProgress } from "../types";
import { useVisibleEventSource } from "./useVisibleEventSource";
import { useVisibleInterval } from "./useVisibleInterval";

export const DASHBOARD_POLL_S = 30;
const BUSY_POLL_MS = 3000;

// ─── API response shapes ─────────────────────────────────────
// GET /discovery/stats — same projection the DiscDashboard page consumes.
// Duplicated there rather than shared because the two read different
// overlapping subsets and sharing would balloon the type.
export interface DashboardStats {
  owned_books?: number;
  total_books?: number;
  missing_books?: number;
  new_books?: number;
  upcoming_books?: number;
  total_series?: number;
  authors?: number;
  hidden_books?: number;
  suggestions?: number;
  library_name?: string;
  library_display_name?: string;
  content_type?: string;
  mam?: {
    upload_candidates?: number;
    available_to_download?: number;
    missing_everywhere?: number;
    total_unscanned?: number;
  };
  // Audiobook-specific — only populated on audiobook-library slug stats.
  total_duration_sec?: number;
  narrator_count?: number;
  unabridged_count?: number;
}

export interface HealthResponse {
  dispatcher_ready?: boolean;
}

// MamStatusResponse plus the MAM user/account fields the Dashboard
// renders when the MAM cookie is configured.
export interface MamUserStatus extends MamStatusResponse {
  username?: string;
  classname?: string;
  ratio?: number;
  wedges?: number;
  seedbonus?: number;
  upload_buffer_bytes?: number;
  uploaded_bytes?: number;
  downloaded_bytes?: number;
  cookie_configured?: boolean;
  error?: string;
}

export interface BudgetEntry {
  grab_id?: number;
  torrent_name?: string;
  source?: string;
  seeding_seconds?: number;
  remaining_seconds?: number;
}

export interface BudgetResponse extends MamBudgetFields {
  budget_used?: number;
  budget_cap?: number;
  next_release_seconds?: number;
  ledger_active?: number;
  qbit_extras?: number;
  queue_size?: number;
  seed_seconds_required?: number;
  entries?: BudgetEntry[];
}

export interface CountsResponse {
  authors_allowed?: number;
  authors_ignored?: number;
  grabs?: number;
  calibre_additions?: number;
}

export interface GrabRow {
  torrent_name?: string;
  grabbed_at?: string;
}

export interface SettingsBlob {
  cwa_web_url?: string;
  calibre_web_url?: string;
  abs_web_url?: string;
}

type LibraryScan = ScanProgress & { slug?: string };

export function useDashboardData() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [mam, setMam] = useState<MamUserStatus | null>(null);
  const [budget, setBudget] = useState<BudgetResponse | null>(null);
  const [reviewCount, setReviewCount] = useState(0);
  const [tentativeCount, setTentativeCount] = useState(0);
  const [counts, setCounts] = useState<CountsResponse | null>(null);
  const [grabs, setGrabs] = useState<GrabRow[]>([]);
  const [settings, setSettings] = useState<SettingsBlob | null>(null);
  const [scans, setScans] = useState<ScanProgress[]>([]);
  // Per-library stats keyed by slug, so Calibre AND Audiobookshelf
  // numbers show side by side; `stats` is the active library's.
  const [statsBySlug, setStatsBySlug] = useState<Record<string, DashboardStats>>({});
  // Counts finished refreshes (the desktop's next-refresh countdown
  // restarts on it).
  const [refreshes, setRefreshes] = useState(0);

  const refresh = useCallback(async () => {
    const r = await Promise.all([
      api.get<DashboardStats>("/discovery/stats").catch(() => null),
      api.get<HealthResponse>("/health").catch(() => null),
      api.get<MamUserStatus>("/v1/mam/status").catch(() => null),
      api.get<BudgetResponse>("/v1/grabs/budget").catch(() => null),
      api.get<{ pending_count?: number }>("/v1/review").catch(() => ({ pending_count: 0 })),
      api.get<{ items?: unknown[] }>("/v1/tentative").catch(() => ({ items: [] })),
      api.get<CountsResponse>("/v1/data/counts").catch(() => null),
      api.get<{ grabs?: GrabRow[] }>("/v1/grabs/recent").catch(() => ({ grabs: [] })),
      api.get<SettingsBlob>("/v1/settings").catch(() => null),
      api.get<{ scans?: ScanProgress[] }>("/discovery/scan-status").catch(() => null),
    ]);
    setStats(r[0]);
    setHealth(r[1]);
    setMam(r[2]);
    setBudget(r[3]);
    setReviewCount(r[4]?.pending_count ?? 0);
    setTentativeCount(r[5]?.items?.length ?? 0);
    setCounts(r[6]);
    setGrabs(r[7]?.grabs ?? []);
    setSettings(r[8]);
    // A failed scan-status keeps the last scans shown.
    if (r[9]) setScans(r[9].scans ?? []);
    // Second pass: one /stats call per library (2 in practice), so the
    // Calibre and Audiobookshelf widgets render their own numbers.
    const libs = (r[9]?.scans || []).filter((s) => s.kind === "library");
    if (libs.length > 0) {
      const byPair = await Promise.all(
        libs.map(async (ls) => {
          const slug = (ls as LibraryScan).slug || "";
          const s = await api
            .get<DashboardStats>(`/discovery/stats?slug=${encodeURIComponent(slug)}`)
            .catch(() => null);
          return [slug, s] as const;
        }),
      );
      const map: Record<string, DashboardStats> = {};
      for (const [slug, s] of byPair) {
        if (s && slug) map[slug] = s;
      }
      setStatsBySlug(map);
    }
    setRefreshes((n) => n + 1);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Live MAM figures (ratio / seedbonus / wedges / buffer) between
  // refreshes. `toast` and `client-status` are handled app-level by
  // SseEventsProvider.
  useVisibleEventSource({
    "mam-stats": (e) => {
      setMam((prev) => ({
        enabled: prev?.enabled ?? true,
        validation_ok: prev?.validation_ok,
        stats: prev?.stats,
        ...prev,
        ratio: e.ratio,
        seedbonus: e.seedbonus,
        wedges: e.wedges,
        upload_buffer_bytes: e.upload_buffer_bytes,
      }));
    },
  });

  // Each content type shows the first library of that kind (one ebook +
  // one audiobook library in practice); with no per-library stats yet,
  // the active library's stand in for the ebook side.
  const statsEntries = Object.values(statsBySlug);
  const ebookStats: DashboardStats =
    statsEntries.find((s) => s?.content_type === "ebook") || stats || {};
  const audiobookStats: DashboardStats | undefined = statsEntries.find(
    (s) => s?.content_type === "audiobook",
  );

  return {
    stats, health, mam, budget, reviewCount, tentativeCount, counts, grabs, settings, scans,
    statsBySlug, ebookStats, audiobookStats, refreshes, refresh,
  };
}

const reason = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function useDashboardCommands(
  refresh: () => Promise<void>,
  onFail: (message: string) => void,
) {
  // Per-slug so the clicked Sync button is the one that spins (the
  // server runs one library sync at a time).
  const [syncingSlug, setSyncingSlug] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [mamScanning, setMamScanning] = useState(false);
  // Data Hygiene mutates every library DB (deletes empty authors,
  // merges duplicate books, consolidates series), so it asks first.
  const [showHygieneConfirm, setShowHygieneConfirm] = useState(false);
  const [hygieneStarting, setHygieneStarting] = useState(false);

  /** POST, report a failure as `<what>: <reason>`. */
  const send = async (path: string, what: string) => {
    try {
      await api.post(path);
    } catch (e) {
      onFail(`${what}: ${reason(e)}`);
    }
  };

  const triggerSync = async (slug?: string) => {
    setSyncingSlug(slug || "__active__");
    const qs = slug ? `?slug=${encodeURIComponent(slug)}` : "";
    await send(`/discovery/sync/library${qs}`, "Couldn't start the library sync");
    setSyncingSlug(null);
    refresh();
  };
  // v2.12.0 — explicit scope: each Scan button fans across every
  // library of its content type.
  const triggerEbookSources = async () => {
    setScanning(true);
    await send("/discovery/lookup?content_type=ebook", "Couldn't start the ebook source scan");
    setScanning(false);
    refresh();
  };
  const triggerAudiobookSources = async () => {
    setScanning(true);
    await send("/discovery/lookup?content_type=audiobook", "Couldn't start the audiobook source scan");
    setScanning(false);
    refresh();
  };
  const triggerMam = async () => {
    setMamScanning(true);
    await send("/discovery/mam/scan", "Couldn't start the MAM scan");
    setMamScanning(false);
    refresh();
  };
  const cancelSources = async () => {
    await send("/discovery/lookup/cancel", "Couldn't cancel the source scan");
    refresh();
  };
  const cancelMam = async () => {
    await send("/discovery/mam/scan/cancel", "Couldn't cancel the MAM scan");
    refresh();
  };
  const triggerHygiene = async () => {
    setHygieneStarting(true);
    await send("/discovery/hygiene/run", "Couldn't start Data Hygiene");
    setHygieneStarting(false);
    setShowHygieneConfirm(false);
    refresh();
  };
  const cancelHygiene = async () => {
    await send("/discovery/hygiene/cancel", "Couldn't cancel Data Hygiene");
    refresh();
  };

  return {
    syncingSlug, scanning, mamScanning, showHygieneConfirm, setShowHygieneConfirm, hygieneStarting,
    triggerSync, triggerEbookSources, triggerAudiobookSources, triggerMam,
    cancelSources, cancelMam, triggerHygiene, cancelHygiene,
  };
}

export function useDashboard(onFail: (message: string) => void) {
  const data = useDashboardData();
  const commands = useDashboardCommands(data.refresh, onFail);
  // Poll faster while a scan or a sync is in flight.
  const busy = data.scans.some((s) => s.running) || commands.syncingSlug !== null;
  useVisibleInterval(data.refresh, busy ? BUSY_POLL_MS : DASHBOARD_POLL_S * 1000);
  return { ...data, ...commands };
}
