// Unified Dashboard v6 — Athena | Hermes | MAM Activity | Command Center | Stats.
//
// Single-screen overview: two Discovery library sections on the left (Athena),
// pipeline health + snatch budget + recent activity + seeding in the middle
// (Hermes, absorbing the old MAM Activity row), Quick Actions + Tools across
// the bottom, and a stats rail on the right that wraps under the actions bar
// on narrow viewports.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import type { Theme } from "../theme";
import { Spin } from "../components/Spin";
import { fmtBytes, fmtDuration, fmtNum, fmtRatio, pct } from "../lib/format";
import { useVisibleInterval } from "../hooks/useVisibleInterval";
import { useVisibleEventSource } from "../hooks/useVisibleEventSource";
import { useSseEvents } from "../providers/SseEventsProvider";
import type { MamStatusResponse, NavFn, ScanProgress } from "../types";

interface Props {
  onNav: NavFn;
}

const POLL = 30;

// ─── API response shapes ─────────────────────────────────────
// GET /discovery/stats — same projection the DiscDashboard page consumes.
// Duplicated here rather than shared because the two files read different
// overlapping subsets and sharing would balloon the type.
interface DashboardStats {
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

interface HealthResponse {
  dispatcher_ready?: boolean;
}

// Extends MamStatusResponse with the MAM user/account fields the
// Hermes block renders when the MAM cookie is configured.
interface MamUserStatus extends MamStatusResponse {
  username?: string;
  classname?: string;
  ratio?: number;
  wedges?: number;
  seedbonus?: number;
  upload_buffer_bytes?: number;
  uploaded_bytes?: number;
  downloaded_bytes?: number;
  cookie_configured?: boolean;
}

interface BudgetEntry {
  grab_id?: number;
  torrent_name?: string;
  source?: string;
  seeding_seconds?: number;
  remaining_seconds?: number;
}

interface BudgetResponse {
  budget_used?: number;
  budget_cap?: number;
  next_release_seconds?: number;
  ledger_active?: number;
  qbit_extras?: number;
  queue_size?: number;
  seed_seconds_required?: number;
  entries?: BudgetEntry[];
}

interface ReviewResponse {
  pending_count?: number;
}

interface TentativeResponse {
  items?: unknown[];
}

interface CountsResponse {
  authors_allowed?: number;
  authors_ignored?: number;
  grabs?: number;
  calibre_additions?: number;
}

interface GrabRow {
  torrent_name?: string;
  grabbed_at?: string;
}

interface GrabsResponse {
  grabs?: GrabRow[];
}

interface SettingsBlob {
  cwa_web_url?: string;
  calibre_web_url?: string;
  abs_web_url?: string;
}

interface ScanStatusResponse {
  scans?: ScanProgress[];
}

// ─── Library-link panel helper shape ─────────────────────────
interface LibraryLink {
  label: string;
  color: string;
  href: string;
}

export default function UnifiedDashboard({ onNav }: Props) {
  const t = useTheme();
  const [d, setD] = useState<DashboardStats | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [mam, setMam] = useState<MamUserStatus | null>(null);
  const [budget, setBudget] = useState<BudgetResponse | null>(null);
  const [reviewCount, setReviewCount] = useState(0);
  const [tentativeCount, setTentativeCount] = useState(0);
  const [counts, setCounts] = useState<CountsResponse | null>(null);
  const [grabs, setGrabs] = useState<GrabRow[]>([]);
  const [settings, setSettings] = useState<SettingsBlob | null>(null);
  const [cd, setCd] = useState(POLL);
  const [scanStatus, setScanStatus] = useState<ScanStatusResponse | null>(null);
  // Per-slug syncing spinner state. Serialized server-side (only one
  // library sync runs at a time via _library_sync_in_progress), but
  // the UI tracks per-slug so the clicked button is the one that
  // spins — not every Sync button at once.
  const [syncingSlug, setSyncingSlug] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [mamScanning, setMamScanning] = useState(false);

  // Per-library stats map keyed by slug. Populated after the first
  // refresh tick so the Athena widget and Seshat Stats row can show
  // Calibre AND Audiobookshelf numbers simultaneously instead of only
  // the active library. `d` (the active-library stats) is kept for
  // back-compat with the existing Hermes/Pipeline consumers that
  // don't care which library is active.
  const [statsBySlug, setStatsBySlug] = useState<Record<string, DashboardStats>>({});

  const refresh = useCallback(async () => {
    const r = await Promise.all([
      api.get<DashboardStats>("/discovery/stats").catch(() => null),
      api.get<HealthResponse>("/health").catch(() => null),
      api.get<MamUserStatus>("/v1/mam/status").catch(() => null),
      api.get<BudgetResponse>("/v1/grabs/budget").catch(() => null),
      api.get<ReviewResponse>("/v1/review").catch(() => ({ pending_count: 0 })),
      api.get<TentativeResponse>("/v1/tentative").catch(() => ({ items: [] })),
      api.get<CountsResponse>("/v1/data/counts").catch(() => null),
      api.get<GrabsResponse>("/v1/grabs/recent").catch(() => ({ grabs: [] })),
      api.get<SettingsBlob>("/v1/settings").catch(() => null),
      api.get<ScanStatusResponse>("/discovery/scan-status").catch(() => null),
    ]);
    setD(r[0]);
    setHealth(r[1]);
    setMam(r[2]);
    setBudget(r[3]);
    setReviewCount(r[4]?.pending_count ?? 0);
    setTentativeCount(r[5]?.items?.length ?? 0);
    setCounts(r[6]);
    setGrabs(r[7]?.grabs ?? []);
    setSettings(r[8]);
    if (r[9]) setScanStatus(r[9]);
    // Second pass: fan out one /stats call per discovered library so
    // Calibre + ABS widgets render with their own numbers. Bounded by
    // the number of libraries (2 in practice); the parallel fetch
    // adds one network round-trip to the 30s poll loop.
    const libs = (r[9]?.scans || []).filter((s) => s.kind === "library");
    if (libs.length > 0) {
      const byPair = await Promise.all(
        libs.map(async (ls) => {
          const s = await api
            .get<DashboardStats>(
              `/discovery/stats?slug=${encodeURIComponent((ls as ScanProgress & { slug?: string }).slug || "")}`,
            )
            .catch(() => null);
          return [(ls as ScanProgress & { slug?: string }).slug || "", s] as const;
        }),
      );
      const map: Record<string, DashboardStats> = {};
      for (const [slug, stats] of byPair) {
        if (stats && slug) map[slug] = stats;
      }
      setStatsBySlug(map);
    }
    setCd(POLL);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const ds: DashboardStats = d || {};
  const b = budget || ({} as BudgetResponse);
  const allowed = counts?.authors_allowed ?? 0;
  const ignored = counts?.authors_ignored ?? 0;
  const totalGrabs = counts?.grabs ?? 0;
  const calibreAdds = counts?.calibre_additions ?? 0;
  // Calibre-Web quick-launch URL. Prefer the primary setting
  // (`cwa_web_url`) and fall back to the legacy `calibre_web_url`
  // so upgraded installs that still have the old key populated
  // don't lose their Dashboard button on the settings reorg.
  const calibreWebUrl = settings?.cwa_web_url || settings?.calibre_web_url || "";
  const absWebUrl = settings?.abs_web_url || "";

  // Per-library stats split. Each content type picks the first library
  // of that kind from statsBySlug — matches the current 1-ebook +
  // 1-audiobook setup. Multi-library-per-kind users fall back to the
  // first discovered; a proper per-library tab can come later.
  const statsEntries: DashboardStats[] = Object.values(statsBySlug);
  const ebookStats: DashboardStats =
    statsEntries.find((s) => s?.content_type === "ebook") || ds;
  const audiobookStats: DashboardStats | undefined = statsEntries.find(
    (s) => s?.content_type === "audiobook",
  );
  const authors = ebookStats?.authors ?? 0;
  const series = ebookStats?.total_series ?? 0;
  const newBooks = ebookStats?.new_books ?? 0;
  // Seshat Stats row tiles summarize the ebook library (historical
  // behavior before ABS existed). Audiobook-specific tiles are added
  // separately from `audiobookStats`.
  const owned = ebookStats?.owned_books ?? 0;
  const total = ebookStats?.total_books ?? 0;
  const missing = ebookStats?.missing_books ?? 0;
  const upcoming = ebookStats?.upcoming_books ?? 0;

  // Scan progress — the /scan-status response returns one entry per
  // kind ({kind: "lookup"}, {kind: "mam"}, {kind: "library", slug: ...}).
  // Library entries are per-slug: Calibre and Audiobookshelf each get
  // their own entry so the Command Center shows dedicated rows with
  // independent in-flight progress + "(Last Sync: ...)" timestamps.
  const scansArr: ScanProgress[] = scanStatus?.scans || [];
  const libScans: (ScanProgress & { slug?: string })[] = scansArr.filter(
    (s) => s.kind === "library",
  );
  const srcScan: ScanProgress | Record<string, never> =
    scansArr.find((s) => s.kind === "lookup") || {};
  const mamScan: ScanProgress | Record<string, never> =
    scansArr.find((s) => s.kind === "mam") || {};

  const triggerSync = async (slug?: string) => {
    setSyncingSlug(slug || "__active__");
    try {
      const qs = slug ? `?slug=${encodeURIComponent(slug)}` : "";
      await api.post(`/discovery/sync/library${qs}`);
    } catch {
      /* ignore — poll loop will surface errors */
    }
    setSyncingSlug(null);
    refresh();
  };
  const triggerSources = async () => {
    setScanning(true);
    try {
      await api.post("/discovery/lookup");
    } catch {
      /* ignore */
    }
    setScanning(false);
    refresh();
  };
  const triggerAudiobookSources = async () => {
    setScanning(true);
    try {
      await api.post("/discovery/lookup?content_type=audiobook");
    } catch {
      /* ignore */
    }
    setScanning(false);
    refresh();
  };
  const triggerMam = async () => {
    setMamScanning(true);
    try {
      await api.post("/discovery/mam/scan");
    } catch {
      /* ignore */
    }
    setMamScanning(false);
    refresh();
  };
  const cancelSources = async () => {
    try {
      await api.post("/discovery/lookup/cancel");
    } catch {
      /* ignore */
    }
    refresh();
  };
  const cancelMam = async () => {
    try {
      await api.post("/discovery/mam/scan/cancel");
    } catch {
      /* ignore */
    }
    refresh();
  };

  const anyLibRunning = libScans.some((s) => s.running);
  const anyRunning =
    anyLibRunning ||
    ("running" in srcScan && srcScan.running) ||
    ("running" in mamScan && mamScan.running) ||
    syncingSlug !== null;
  const pollMs = anyRunning ? 3000 : POLL * 1000;
  useVisibleInterval(refresh, pollMs);

  // Dashboard-local SSE subscription — patches `mam-stats` into the
  // rendered MAM block in place so ratio/seedbonus/wedges update
  // without waiting for the 30s refresh cycle. `toast` and
  // `client-status` are handled app-level by SseEventsProvider.
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
  const { clientReachable } = useSseEvents();
  useVisibleInterval(() => setCd((c) => Math.max(0, c - 1)), 1000);

  // Responsive dashboard grid. Above 1500px the Seshat Stats column
  // pins to the right as a narrow full-height rail; below, it wraps
  // underneath the Quick Actions bar so we still fit without a
  // horizontal scrollbar on laptop-sized screens.
  const [viewport, setViewport] = useState(
    typeof window !== "undefined" ? window.innerWidth : 1800,
  );
  useEffect(() => {
    const onResize = () => setViewport(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  const wideMode = viewport >= 1500;
  // Phone mode collapses the multi-column dashboard to a single
  // stacked column. Drives the grid-area selection below.
  const mobileMode = viewport <= 700;

  const hdr = (color?: string): React.CSSProperties => ({
    fontSize: 15,
    fontWeight: 700,
    color: color || t.accent,
    textTransform: "uppercase",
    letterSpacing: "0.05em",
  });

  // Grid area names: left (Athena+CC stacked), middle (Hermes with
  // absorbed MAM Activity), stats (narrow right rail), actions
  // (full-width bottom bar). Wide mode pins stats to a right column
  // spanning both the content and actions rows; narrow mode wraps
  // stats below the actions bar.
  const gridStyle: React.CSSProperties = mobileMode
    ? {
        display: "grid",
        gridTemplateColumns: "1fr",
        gridTemplateAreas: `"left" "middle" "actions" "stats"`,
        gap: 10,
        alignItems: "start",
      }
    : wideMode
    ? {
        display: "grid",
        gridTemplateColumns: "1fr 1fr 380px",
        gridTemplateAreas: `"left middle stats" "actions actions stats"`,
        gap: 10,
        alignItems: "start",
      }
    : {
        display: "grid",
        gridTemplateColumns: "1fr 1fr",
        gridTemplateAreas: `"left middle" "actions actions" "stats stats"`,
        gap: 10,
        alignItems: "start",
      };

  const sectionHdr: React.CSSProperties = {
    fontSize: 12,
    fontWeight: 600,
    color: t.td,
    textTransform: "uppercase",
    marginBottom: 6,
    letterSpacing: "0.04em",
  };
  const hsep: React.CSSProperties = {
    height: 1,
    background: t.borderL,
    margin: "10px 0",
  };

  // Narrowed accessor for the source-scan "extras" grab-bag. ScanProgress.extra
  // is Record<string, unknown> so we pull the two keys we actually read
  // (`new_books` + `source_timeouts`) behind typed helpers.
  const srcExtra = "extra" in srcScan ? srcScan.extra : undefined;
  const srcNewBooks = (() => {
    const v = srcExtra?.new_books;
    return typeof v === "number" ? v : null;
  })();
  const srcTimeouts: Record<string, number> = (() => {
    const v = srcExtra?.source_timeouts;
    return v && typeof v === "object" ? (v as Record<string, number>) : {};
  })();
  const hasTimeouts = Object.keys(srcTimeouts).length > 0;

  return (
    <div style={gridStyle}>
      {/* ══════ LEFT COLUMN: Athena + Command Center ══════ */}
      <div
        style={{
          gridArea: "left",
          display: "flex",
          flexDirection: "column",
          gap: 10,
        }}
      >
        {/* ATHENA — ebook and audiobook sections stacked. Each has
            its own ownership percentage, progress bar, MAM metrics,
            and external-link column so the user can see both libraries'
            state simultaneously. */}
        <div
          style={{
            background: t.bg2,
            border: `1px solid ${t.border}`,
            borderRadius: 12,
            padding: "12px 16px",
          }}
        >
          <div style={{ ...hdr(), marginBottom: 8 }}>
            <Dot color={t.accent} /> Athena
          </div>
          <LibrarySection
            stats={ebookStats}
            color={t.jade}
            accent={t.accent}
            links={[
              calibreWebUrl
                ? { label: "Calibre-Web", color: t.jade, href: calibreWebUrl }
                : null,
            ].filter((x): x is LibraryLink => x !== null)}
            onNavMam={() => onNav("disc-mam")}
            t={t}
          />
          {audiobookStats && (
            <>
              <div style={hsep} />
              <LibrarySection
                stats={audiobookStats}
                color={t.pur}
                accent={t.accent}
                links={[
                  absWebUrl
                    ? { label: "Audiobookshelf", color: t.pur, href: absWebUrl }
                    : null,
                ].filter((x): x is LibraryLink => x !== null)}
                onNavMam={() => onNav("disc-mam")}
                t={t}
              />
            </>
          )}
        </div>

        {/* COMMAND CENTER — triggers on top, progress rows below. */}
        <div
          style={{
            background: t.bg2,
            border: `1px solid ${t.border}`,
            borderRadius: 12,
            padding: "12px 16px",
          }}
        >
          <div style={{ ...hdr(), marginBottom: 8 }}>
            <Dot color={t.accent} /> Command Center
          </div>
          {/* Buttons row */}
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: 6,
              marginBottom: 10,
            }}
          >
            {libScans.map((ls) => {
              const color =
                (ls as ScanProgress & { content_type?: string }).content_type === "audiobook"
                  ? t.pur
                  : t.jade;
              const shortLabel =
                (ls as ScanProgress & { label?: string }).label?.replace(/\s*Sync$/, "") ||
                ls.slug ||
                "";
              return (
                <CmdBtn
                  key={ls.slug}
                  label={
                    <>
                      <Dot color={color} /> Sync {shortLabel}
                    </>
                  }
                  busy={syncingSlug === ls.slug || ls.running}
                  onClick={() => triggerSync(ls.slug)}
                />
              );
            })}
            <CmdBtn
              label={
                <>
                  <Dot color={t.cyan} /> Scan Sources
                </>
              }
              busy={scanning || ("running" in srcScan && !!srcScan.running)}
              onClick={triggerSources}
            />
            {libScans.some(
              (ls) =>
                (ls as ScanProgress & { content_type?: string }).content_type ===
                "audiobook",
            ) ? (
              <CmdBtn
                label={
                  <>
                    <Dot color={t.pur} /> Scan Audiobooks
                  </>
                }
                busy={scanning || ("running" in srcScan && !!srcScan.running)}
                onClick={triggerAudiobookSources}
              />
            ) : null}
            <CmdBtn
              label={
                <>
                  <Dot color={t.ylw} /> MAM Scan
                </>
              }
              busy={mamScanning || ("running" in mamScan && !!mamScan.running)}
              onClick={triggerMam}
            />
            <CmdBtn
              label={`Review ${reviewCount ? `(${reviewCount})` : ""}`}
              highlight
              onClick={() => onNav("pipe-review")}
            />
            <CmdBtn
              label={`New Authors ${tentativeCount ? `(${tentativeCount})` : ""}`}
              onClick={() => onNav("pipe-tentative")}
            />
          </div>
          {/* Progress below */}
          <div style={sectionHdr}>Progress</div>
          {libScans.map((ls) => (
            <ProgressRow
              key={ls.slug}
              label={ls.label || "Library Sync"}
              scan={ls}
              t={t}
            />
          ))}
          <ProgressRow
            label="Source Scan"
            scan={srcScan}
            t={t}
            onCancel={
              "running" in srcScan && srcScan.running ? cancelSources : undefined
            }
          />
          <ProgressRow
            label="MAM Scan"
            scan={mamScan}
            t={t}
            onCancel={
              "running" in mamScan && mamScan.running ? cancelMam : undefined
            }
          />
          {/* Last-scan summary inline, below the progress rows */}
          {srcNewBooks != null || hasTimeouts ? (
            <div
              style={{
                marginTop: 8,
                paddingTop: 6,
                borderTop: `1px solid ${t.borderL}`,
              }}
            >
              {"status" in srcScan &&
              srcScan.status === "complete" &&
              hasTimeouts ? (
                <div style={{ fontSize: 13, color: t.warn }}>
                  {Object.entries(srcTimeouts).map(([src, sec]) => (
                    <div key={src}>
                      {src}: timed out ({sec}s)
                    </div>
                  ))}
                </div>
              ) : (
                <div style={{ fontSize: 13, color: t.text2 }}>
                  <span style={{ color: t.td }}>Last source scan: </span>
                  {("current" in srcScan ? srcScan.current : 0) ?? 0} authors
                  checked ·{" "}
                  <span style={{ color: t.jade, fontWeight: 600 }}>
                    {srcNewBooks ?? 0} new books
                  </span>
                </div>
              )}
            </div>
          ) : null}
        </div>
      </div>

      {/* ══════ MIDDLE COLUMN: Hermes (expanded) ══════ */}
      <div
        style={{
          gridArea: "middle",
          background: t.bg2,
          border: `1px solid ${t.border}`,
          borderRadius: 12,
          padding: "12px 16px",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            gap: 12,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={hdr(t.jade)}>
              <Dot color={t.jade} /> Hermes
            </div>
            <div style={{ fontSize: 13, color: t.td, marginTop: 2 }}>
              {health?.dispatcher_ready
                ? `${fmtNum(totalGrabs)} grabs · ${fmtNum(calibreAdds)} to Calibre`
                : "Starting…"}
            </div>
            <div
              style={{
                display: "flex",
                gap: 14,
                marginTop: 10,
                flexWrap: "wrap",
                alignItems: "center",
              }}
            >
              <Pill label="Dispatcher" ok={health?.dispatcher_ready} />
              <Pill label="IRC" ok={health?.dispatcher_ready} />
              <Pill
                label="Cookie"
                ok={mam?.validation_ok}
                warn={mam?.cookie_configured && !mam?.validation_ok}
              />
              <Pill label="Watcher" ok={health?.dispatcher_ready} />
              {/* Downloader pill tracks qBit reachability via the SSE
                  client-status event, not the Watcher loop's liveness.
                  The Watcher can be happily looping even when qBit is
                  down — it just returns [] from every list_torrents
                  call — so we need a dedicated indicator for the
                  user to see "the downloader side of the world is
                  actually talking to qBit right now". undefined until
                  the first SSE event arrives (shows neutral). */}
              <Pill
                label="Downloader"
                ok={clientReachable === null ? undefined : clientReachable}
              />
              <div
                style={{
                  background: t.bg3,
                  borderRadius: 8,
                  padding: "6px 12px",
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <span
                  style={{
                    fontSize: 11,
                    color: t.td,
                    textTransform: "uppercase",
                    fontWeight: 600,
                  }}
                >
                  Poll
                </span>
                <span
                  style={{
                    fontSize: 17,
                    fontWeight: 700,
                    color: cd <= 5 ? t.accent : t.text2,
                  }}
                >
                  {cd}s
                </span>
              </div>
            </div>
          </div>
          {mam?.username && (
            <div
              style={{
                background: t.bg3,
                borderRadius: 10,
                padding: "10px 14px",
                textAlign: "right",
                minWidth: 170,
              }}
            >
              <div
                style={{
                  fontSize: 12,
                  color: t.td,
                  textTransform: "uppercase",
                  fontWeight: 600,
                }}
              >
                {mam.username}
              </div>
              {mam.classname && (
                <div style={{ fontSize: 11, color: t.tf }}>{mam.classname}</div>
              )}
              <div
                style={{
                  display: "flex",
                  gap: 14,
                  justifyContent: "flex-end",
                  marginTop: 5,
                }}
              >
                {mam.ratio != null && (
                  <div>
                    <div
                      style={{
                        fontSize: 22,
                        fontWeight: 700,
                        color: mam.ratio >= 1 ? t.ok : t.warn,
                      }}
                    >
                      {fmtRatio(mam.ratio)}
                    </div>
                    <div style={{ fontSize: 11, color: t.td }}>Ratio</div>
                  </div>
                )}
                {mam.wedges != null && (
                  <div>
                    <div
                      style={{
                        fontSize: 22,
                        fontWeight: 700,
                        color: t.accent,
                      }}
                    >
                      {fmtNum(mam.wedges)}
                    </div>
                    <div style={{ fontSize: 11, color: t.td }}>Wedges</div>
                  </div>
                )}
              </div>
              {(mam.uploaded_bytes || mam.downloaded_bytes) && (
                <div style={{ fontSize: 11, color: t.tf, marginTop: 4 }}>
                  ↑ {fmtBytes(mam.uploaded_bytes)} · ↓{" "}
                  {fmtBytes(mam.downloaded_bytes)}
                </div>
              )}
            </div>
          )}
        </div>

        <div style={hsep} />

        {/* Snatch Budget */}
        <div>
          <div style={sectionHdr}>Snatch Budget</div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
            <span
              style={{
                fontSize: 26,
                fontWeight: 700,
                color:
                  (b.budget_used ?? 0) >= (b.budget_cap ?? 1) ? t.warn : t.accent,
              }}
            >
              {b.budget_used ?? 0}
            </span>
            <span style={{ fontSize: 14, color: t.td }}>
              / {b.budget_cap ?? 0}
            </span>
            {(b.next_release_seconds || 0) > 0 && (
              <span
                style={{ fontSize: 12, color: t.accent, marginLeft: 12 }}
              >
                Next release in {fmtDuration(b.next_release_seconds || 0)}
              </span>
            )}
          </div>
          <div style={{ fontSize: 12, color: t.td, marginTop: 2 }}>
            {b.ledger_active ?? 0} active + {b.qbit_extras ?? 0} manual
            {(b.queue_size ?? 0) > 0 && (
              <span style={{ color: t.warn }}> · {b.queue_size} queued</span>
            )}
          </div>
        </div>

        <div style={hsep} />

        {/* Recent Activity */}
        <div>
          <div style={sectionHdr}>Recent Activity</div>
          {grabs.length > 0 ? (
            grabs.slice(0, 5).map((g, i) => (
              <div
                key={i}
                style={{
                  display: "flex",
                  fontSize: 13,
                  padding: "4px 0",
                  borderBottom: i < 4 ? `1px solid ${t.borderL}` : "none",
                  overflow: "hidden",
                }}
              >
                <span
                  style={{
                    color: t.text2,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                    flex: 1,
                    minWidth: 0,
                  }}
                >
                  {g.torrent_name}
                </span>
                <span
                  style={{
                    color: t.tf,
                    marginLeft: 10,
                    flexShrink: 0,
                    fontSize: 12,
                  }}
                >
                  {g.grabbed_at
                    ? new Date(g.grabbed_at + "Z").toLocaleDateString()
                    : ""}
                </span>
              </div>
            ))
          ) : (
            <div style={{ fontSize: 13, color: t.tf, fontStyle: "italic" }}>
              No recent grabs
            </div>
          )}
        </div>

        <div style={hsep} />

        {/* Seeding Progress */}
        <div>
          <div style={sectionHdr}>Seeding Progress</div>
          {(b.entries?.length ?? 0) > 0 ? (
            <div style={{ maxHeight: 180, overflowY: "auto" }}>
              {(b.entries || []).map((e, i) => {
                const sp = Math.min(
                  100,
                  ((e.seeding_seconds || 0) /
                    (b.seed_seconds_required || 1)) *
                    100,
                );
                return (
                  <div
                    key={e.grab_id ?? i}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      padding: "4px 0",
                      borderBottom: `1px solid ${t.borderL}`,
                      fontSize: 12,
                    }}
                  >
                    {e.source === "external" && (
                      <span
                        style={{
                          fontSize: 9,
                          padding: "1px 5px",
                          borderRadius: 3,
                          background: t.td + "22",
                          color: t.td,
                          fontWeight: 600,
                        }}
                      >
                        EXT
                      </span>
                    )}
                    <span
                      style={{
                        flex: 1,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                        color: t.text2,
                        minWidth: 0,
                      }}
                    >
                      {e.torrent_name}
                    </span>
                    <div
                      style={{
                        width: 80,
                        height: 4,
                        borderRadius: 2,
                        background: t.bg4,
                        flexShrink: 0,
                      }}
                    >
                      <div
                        style={{
                          width: `${sp}%`,
                          height: "100%",
                          borderRadius: 2,
                          background: sp >= 100 ? t.ok : t.accent,
                        }}
                      />
                    </div>
                    <span
                      style={{
                        width: 55,
                        textAlign: "right",
                        color: t.tf,
                        fontSize: 11,
                        flexShrink: 0,
                      }}
                    >
                      {(e.remaining_seconds || 0) > 0
                        ? fmtDuration(e.remaining_seconds || 0)
                        : "done"}
                    </span>
                  </div>
                );
              })}
            </div>
          ) : (
            <div style={{ fontSize: 13, color: t.tf, fontStyle: "italic" }}>
              No active seeds
            </div>
          )}
        </div>
      </div>

      {/* ══════ ACTIONS BAR: Quick Actions + Tools (full-width) ══════ */}
      <div
        style={{
          gridArea: "actions",
          background: t.bg2,
          border: `1px solid ${t.border}`,
          borderRadius: 12,
          padding: "12px 16px",
        }}
      >
        <div
          style={{
            display: "flex",
            gap: 20,
            alignItems: "flex-start",
            flexWrap: "wrap",
          }}
        >
          <div style={{ flex: 2, minWidth: 280 }}>
            <div style={{ ...sectionHdr }}>Discovery</div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <QBtn
                label={<><Dot color={t.accent} /> Library</>}
                onClick={() => onNav("disc-library")}
              />
              <QBtn
                label={<><Dot color={t.accent} /> Authors</>}
                onClick={() => onNav("disc-authors")}
              />
              <QBtn
                label={<><Dot color={t.ylw} /> Missing</>}
                onClick={() => onNav("disc-missing")}
              />
              <QBtn
                label={<><Dot color={t.cyan} /> Upcoming</>}
                onClick={() => onNav("disc-upcoming")}
              />
              <QBtn
                label={<><Dot color={t.jade} /> MAM Search</>}
                onClick={() => onNav("disc-mam")}
              />
              <QBtn
                label={<><Dot color={t.pur} /> Suggestions</>}
                onClick={() => onNav("disc-suggestions")}
              />
              <QBtn
                label={<><Dot color={t.accent} /> Works</>}
                onClick={() => onNav("works")}
              />
            </div>
          </div>
          <div
            style={{
              flex: 2,
              minWidth: 280,
              borderLeft: wideMode ? `1px solid ${t.border}` : "none",
              paddingLeft: wideMode ? 20 : 0,
            }}
          >
            <div style={sectionHdr}>Pipeline</div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <QBtn
                label={`Review ${reviewCount ? `(${reviewCount})` : ""}`}
                primary
                onClick={() => onNav("pipe-review")}
              />
              <QBtn
                label={<><Dot color={t.warn} /> New Authors</>}
                onClick={() => onNav("pipe-tentative")}
              />
              <QBtn
                label={<><Dot color={t.td} /> Weekly Ignored</>}
                onClick={() => onNav("pipe-ignored")}
              />
              <QBtn
                label={<><Dot color={t.td} /> Author Lists</>}
                onClick={() => onNav("pipe-authors")}
              />
              <QBtn
                label={<><Dot color={t.td} /> Filters</>}
                onClick={() => onNav("filters")}
              />
              <QBtn
                label={<><Dot color={t.td} /> Delayed</>}
                onClick={() => onNav("pipe-delayed")}
              />
            </div>
          </div>
          <div
            style={{
              flex: 1,
              minWidth: 240,
              borderLeft: `1px solid ${t.border}`,
              paddingLeft: 20,
            }}
          >
            <div style={sectionHdr}>Tools</div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <TBtn
                icon={<Bar color={t.ylw} />}
                label="Migration"
                onClick={() => onNav("pipe-migration")}
              />
              <TBtn
                icon={<Bar color={t.cyan} />}
                label="MAM"
                onClick={() => onNav("pipe-mam")}
              />
              <TBtn
                icon={<Bar color={t.tf} />}
                label="Logs"
                onClick={() => onNav("logs")}
              />
              <TBtn
                icon={<Bar color={t.td} />}
                label="Settings"
                onClick={() => onNav("settings")}
              />
            </div>
          </div>
        </div>
      </div>

      {/* ══════ SESHAT STATS: right rail on wide, wraps below on narrow ══════ */}
      <div
        style={{
          gridArea: "stats",
          background: t.bg2,
          border: `1px solid ${t.border}`,
          borderRadius: 12,
          padding: "12px 16px",
        }}
      >
        <div style={{ ...hdr(), marginBottom: 12 }}>
          <Dot color={t.accent} /> Seshat Stats
        </div>

        <div style={sectionHdr}>Ebook</div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: mobileMode ? "repeat(2, 1fr)" : wideMode ? "repeat(2, 1fr)" : "repeat(4, 1fr)",
            gap: 8,
            marginBottom: 14,
          }}
        >
          <Tile
            label="Owned"
            value={fmtNum(owned)}
            color={t.accent}
            onClick={() => onNav("disc-library")}
          />
          <Tile
            label="Missing"
            value={fmtNum(missing)}
            color={t.ylw}
            onClick={() => onNav("disc-missing")}
          />
          <Tile label="New" value={fmtNum(newBooks)} color={t.jade} />
          <Tile
            label="Upcoming"
            value={fmtNum(upcoming)}
            color={t.cyan}
            onClick={() => onNav("disc-upcoming")}
          />
          <Tile
            label="Authors"
            value={fmtNum(authors)}
            onClick={() => onNav("disc-authors")}
          />
          <Tile label="Series" value={fmtNum(series)} />
          <Tile
            label="Suggestions"
            value={fmtNum(ds.suggestions ?? 0)}
            color={t.pur}
            onClick={() => onNav("disc-suggestions")}
          />
        </div>

        {audiobookStats && (
          <>
            <div style={sectionHdr}>Audiobook</div>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: mobileMode ? "repeat(2, 1fr)" : wideMode ? "repeat(2, 1fr)" : "repeat(4, 1fr)",
                gap: 8,
                marginBottom: 14,
              }}
            >
              <Tile
                label="Owned"
                value={fmtNum(audiobookStats.owned_books ?? 0)}
                color={t.pur}
                onClick={() => onNav("disc-library")}
              />
              <Tile
                label="Hours"
                value={fmtNum(
                  Math.round((audiobookStats.total_duration_sec ?? 0) / 3600),
                )}
                color={t.pur}
              />
              <Tile
                label="Narrators"
                value={fmtNum(audiobookStats.narrator_count ?? 0)}
                color={t.pur}
              />
              <Tile
                label="Unabridged"
                value={fmtNum(audiobookStats.unabridged_count ?? 0)}
                color={t.jade}
              />
            </div>
          </>
        )}

        <div style={sectionHdr}>Pipeline</div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: mobileMode ? "repeat(2, 1fr)" : wideMode ? "repeat(2, 1fr)" : "repeat(4, 1fr)",
            gap: 8,
          }}
        >
          <Tile
            label="To Review"
            value={reviewCount}
            color={(reviewCount ?? 0) > 0 ? t.accent : t.td}
            onClick={() => onNav("pipe-review")}
          />
          <Tile
            label="New Authors"
            value={tentativeCount}
            color={(tentativeCount ?? 0) > 0 ? t.warn : t.td}
            onClick={() => onNav("pipe-tentative")}
          />
          <Tile
            label="Allowed"
            value={fmtNum(allowed)}
            color={t.ok}
            onClick={() => onNav("pipe-authors")}
          />
          <Tile
            label="Ignored"
            value={fmtNum(ignored)}
            color={t.red}
            onClick={() => onNav("pipe-authors")}
          />
          <Tile label="To Calibre" value={fmtNum(calibreAdds)} color={t.ok} />
          <Tile label="Total Grabs" value={fmtNum(totalGrabs)} />
        </div>
      </div>
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────

interface LibrarySectionProps {
  stats: DashboardStats;
  color: string;
  accent: string;
  links: LibraryLink[];
  onNavMam: () => void;
  t: Theme;
}

function LibrarySection({
  stats,
  color,
  accent,
  links,
  onNavMam,
  t,
}: LibrarySectionProps) {
  const owned = stats?.owned_books ?? 0;
  const total = stats?.total_books ?? 0;
  const comp = total > 0 ? pct(owned, total) : 0;
  const mam = stats?.mam || {};
  const available = mam.available_to_download ?? 0;
  const upload = mam.upload_candidates ?? 0;
  const missing = mam.missing_everywhere ?? 0;
  const name =
    stats?.library_display_name || stats?.library_name || "Library";
  return (
    <div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 6,
        }}
      >
        <div>
          <div style={{ fontSize: 14, fontWeight: 600, color: t.text2 }}>
            <Dot color={color} /> {name}
          </div>
          <div style={{ fontSize: 13, color: t.td, marginTop: 2 }}>
            {fmtNum(owned)} of {fmtNum(total)}{" "}
            {stats?.content_type === "audiobook" ? "audiobooks" : "books"} owned
          </div>
        </div>
        <div
          style={{
            fontSize: 28,
            fontWeight: 800,
            color: accent,
            lineHeight: 1,
          }}
        >
          {comp}%
        </div>
      </div>
      <div
        style={{
          height: 5,
          background: t.bg4,
          borderRadius: 3,
          overflow: "hidden",
          marginBottom: 8,
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${Math.min(comp, 100)}%`,
            background: `linear-gradient(90deg, ${color}, ${accent})`,
            borderRadius: 3,
          }}
        />
      </div>
      <div style={{ display: "flex", gap: 10, alignItems: "stretch" }}>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr 1fr",
            gap: 8,
            flex: 1,
          }}
        >
          <MiniBox
            value={fmtNum(available)}
            label="Available on MAM"
            color={color}
            onClick={onNavMam}
          />
          <MiniBox
            value={fmtNum(upload)}
            label="Upload Candidates"
            color={t.ylw}
            onClick={onNavMam}
          />
          <MiniBox
            value={fmtNum(missing)}
            label="Missing Everywhere"
            color={t.red}
          />
        </div>
        <div
          style={{
            borderLeft: `1px solid ${t.border}`,
            paddingLeft: 10,
            display: "flex",
            flexDirection: "column",
            gap: 5,
            justifyContent: "center",
            minWidth: 120,
          }}
        >
          {links.length > 0 ? (
            links.map((l) => (
              <TBtn
                key={l.label}
                icon={<Bar color={l.color} />}
                label={l.label}
                onClick={() => window.open(l.href, "_blank")}
              />
            ))
          ) : (
            <span style={{ fontSize: 12, color: t.tf }}>No links</span>
          )}
        </div>
      </div>
    </div>
  );
}

interface MiniBoxProps {
  value: React.ReactNode;
  label: string;
  color?: string;
  onClick?: () => void;
}

function MiniBox({ value, label, color, onClick }: MiniBoxProps) {
  const t = useTheme();
  return (
    <div
      onClick={onClick}
      style={{
        background: t.bg3,
        borderRadius: 8,
        padding: "10px 8px",
        cursor: onClick ? "pointer" : "default",
        textAlign: "center",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <div
        style={{
          fontSize: 22,
          fontWeight: 700,
          color: color || t.text,
          lineHeight: 1.1,
        }}
      >
        {value}
      </div>
      <div style={{ fontSize: 11, color: t.td, marginTop: 3 }}>{label}</div>
    </div>
  );
}

interface PillProps {
  label: string;
  ok?: boolean;
  warn?: boolean;
}

function Pill({ label, ok, warn }: PillProps) {
  const t = useTheme();
  const color = ok ? t.ok : warn ? t.warn : t.td;
  const text = ok ? "Online" : warn ? "Check" : "Offline";
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "2px 0",
      }}
    >
      <div
        style={{
          width: 9,
          height: 9,
          borderRadius: "50%",
          background: color,
          boxShadow: ok ? `0 0 6px ${color}66` : "none",
        }}
      />
      <div>
        <div style={{ fontSize: 13, fontWeight: 600, color: t.text2 }}>
          {label}
        </div>
        <div style={{ fontSize: 11, color }}>{text}</div>
      </div>
    </div>
  );
}

interface TileProps {
  label: string;
  value: React.ReactNode;
  color?: string;
  sub?: string;
  onClick?: () => void;
}

function Tile({ label, value, color, sub, onClick }: TileProps) {
  const t = useTheme();
  return (
    <div
      onClick={onClick}
      style={{
        background: t.bg3,
        borderRadius: 8,
        padding: "12px 14px",
        cursor: onClick ? "pointer" : "default",
      }}
    >
      <div
        style={{
          fontSize: 24,
          fontWeight: 700,
          color: color || t.text,
          lineHeight: 1.1,
        }}
      >
        {value === null ? <Spin size={16} /> : value}
      </div>
      <div style={{ fontSize: 13, color: t.td, marginTop: 4 }}>{label}</div>
      {sub && (
        <div
          style={{
            fontSize: 10,
            color: t.tf,
            marginTop: 2,
            textTransform: "uppercase",
            letterSpacing: "0.04em",
          }}
        >
          {sub}
        </div>
      )}
    </div>
  );
}

function formatAgo(ts: number | null | undefined): string | null {
  if (!ts) return null;
  const secs = Math.max(0, Date.now() / 1000 - ts);
  if (secs < 60) return "Just Now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    return `${h}h ${m}m ago`;
  }
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  return `${d}d ${h}h ago`;
}

interface ProgressRowProps {
  label: string;
  scan: ScanProgress | Record<string, never>;
  t: Theme;
  onCancel?: () => void;
}

function ProgressRow({ label, scan, t, onCancel }: ProgressRowProps) {
  const running = "running" in scan ? scan.running : false;
  const status = "status" in scan ? scan.status || "idle" : "idle";
  const authorName = "current_label" in scan ? scan.current_label || "" : "";
  const bookName = "current_book" in scan ? scan.current_book || "" : "";
  const checked = "current" in scan ? scan.current ?? 0 : 0;
  const total = "total" in scan ? scan.total ?? 0 : 0;
  const pctDone = total > 0 ? Math.floor((checked / total) * 100) : 0;
  const completedAt = "completed_at" in scan ? scan.completed_at : null;
  const ago = !running ? formatAgo(completedAt) : null;
  // "Library Sync" → "Sync"; "Source Scan" / "MAM Scan" → "Scan".
  const kind = label.split(" ").pop();
  const statusText =
    status === "complete"
      ? "Done"
      : status === "cancelled"
      ? "Cancelled"
      : "Idle";
  return (
    <div style={{ padding: "5px 0", borderBottom: `1px solid ${t.borderL}` }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <span
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: running ? t.accent : t.td,
          }}
        >
          {label}
        </span>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 13, color: running ? t.text2 : t.tf }}>
            {running ? (
              `${checked}/${total} (${pctDone}%)`
            ) : (
              <>
                {ago && (
                  <span style={{ fontStyle: "italic" }}>
                    (Last {kind}: {ago}){" "}
                  </span>
                )}
                {statusText}
              </>
            )}
          </span>
          {running && onCancel && (
            <button
              onClick={onCancel}
              style={{
                padding: "2px 8px",
                fontSize: 10,
                fontWeight: 600,
                borderRadius: 4,
                background: t.red + "22",
                color: t.red,
                border: `1px solid ${t.red}44`,
                cursor: "pointer",
              }}
            >
              Stop
            </button>
          )}
        </div>
      </div>
      {running && (
        <>
          <div
            style={{
              height: 4,
              background: t.bg4,
              borderRadius: 2,
              marginTop: 3,
              overflow: "hidden",
            }}
          >
            <div
              style={{
                height: "100%",
                width: `${pctDone}%`,
                background: t.accent,
                borderRadius: 2,
                transition: "width 0.3s",
              }}
            />
          </div>
          {(authorName || bookName) && (
            <div
              style={{
                fontSize: 12,
                color: t.td,
                marginTop: 3,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {authorName && (
                <span style={{ fontWeight: 600 }}>{authorName}</span>
              )}
              {authorName && bookName && (
                <span style={{ color: t.tf }}> — </span>
              )}
              {bookName && <span style={{ color: t.tf }}>{bookName}</span>}
            </div>
          )}
        </>
      )}
    </div>
  );
}

interface CmdBtnProps {
  label: React.ReactNode;
  busy?: boolean;
  highlight?: boolean;
  onClick: () => void;
}

function CmdBtn({ label, busy, highlight, onClick }: CmdBtnProps) {
  const t = useTheme();
  return (
    <button
      onClick={onClick}
      disabled={busy}
      style={{
        padding: "7px 12px",
        borderRadius: 6,
        fontSize: 12,
        fontWeight: 600,
        background: highlight ? t.accent : t.bg4,
        color: highlight ? t.bg : t.text2,
        border: `1px solid ${highlight ? t.accent : t.border}`,
        cursor: busy ? "wait" : "pointer",
        opacity: busy ? 0.6 : 1,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 5,
        whiteSpace: "nowrap",
      }}
    >
      {busy ? <Spin size={12} /> : null}
      {label}
    </button>
  );
}

interface QBtnProps {
  label: React.ReactNode;
  primary?: boolean;
  onClick: () => void;
}

function QBtn({ label, primary, onClick }: QBtnProps) {
  const t = useTheme();
  return (
    <button
      onClick={onClick}
      style={{
        padding: "7px 10px",
        borderRadius: 5,
        fontSize: 13,
        fontWeight: 500,
        background: primary ? t.accent : t.bg4,
        color: primary ? t.bg : t.text2,
        border: `1px solid ${primary ? t.accent : t.border}`,
        cursor: "pointer",
        textAlign: "center",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 5,
      }}
    >
      {label}
    </button>
  );
}

interface TBtnProps {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
}

function TBtn({ icon, label, onClick }: TBtnProps) {
  const t = useTheme();
  return (
    <button
      onClick={onClick}
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 8,
        padding: "9px 18px",
        background: t.bg4,
        border: `1px solid ${t.border}`,
        borderRadius: 6,
        cursor: "pointer",
        fontSize: 13,
        fontWeight: 500,
        color: t.text2,
      }}
    >
      {icon} {label}
    </button>
  );
}

function Dot({ color }: { color: string }) {
  return (
    <span
      style={{
        display: "inline-block",
        width: 9,
        height: 9,
        borderRadius: "50%",
        background: color,
        marginRight: 5,
        verticalAlign: "middle",
        boxShadow: `0 0 4px ${color}44`,
      }}
    />
  );
}

function Bar({ color }: { color: string }) {
  return (
    <span
      style={{
        display: "inline-block",
        width: 3,
        height: 14,
        borderRadius: 2,
        background: color,
        verticalAlign: "middle",
      }}
    />
  );
}
