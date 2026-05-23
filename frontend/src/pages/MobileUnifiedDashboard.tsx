// Mobile-native unified dashboard. The desktop UnifiedDashboard
// branches to this component when useMobileCodepath() is true (phone
// or iPad). Renders a vertical stack of sections instead of the
// 2-3 column grid the desktop uses.
//
// Data-fetching is duplicated from the desktop component for now —
// extracting a shared `useUnifiedDashboardData` hook is a follow-up
// after this design lands. Keeping the duplication keeps the desktop
// code untouched while we iterate on mobile UX.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { fmtNum } from "../lib/format";
import { useVisibleInterval } from "../hooks/useVisibleInterval";
import { useVisibleEventSource } from "../hooks/useVisibleEventSource";
import type { MamStatusResponse, NavFn, ScanProgress } from "../types";
import {
  MobileBtn,
  MobileSection,
  MobileRow,
} from "../components/mobile";
import {
  MobileLibraryHero,
  MobileHealthPill,
  MobileMamAccount,
  MobileSnatchBudget,
  MobileScanProgress,
  MobileRecentActivity,
  MobileStatTile,
} from "../components/mobile/dashboard";

interface Props {
  onNav: NavFn;
}

const POLL = 30;

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
  total_duration_sec?: number;
  narrator_count?: number;
  unabridged_count?: number;
}

interface HealthResponse {
  dispatcher_ready?: boolean;
}

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
  error?: string;
}

interface BudgetResponse {
  budget_used?: number;
  budget_cap?: number;
  next_release_seconds?: number;
  ledger_active?: number;
  qbit_extras?: number;
  queue_size?: number;
  seed_seconds_required?: number;
  entries?: {
    grab_id?: number;
    torrent_name?: string;
    source?: string;
    seeding_seconds?: number;
    remaining_seconds?: number;
  }[];
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

interface SettingsBlob {
  cwa_web_url?: string;
  calibre_web_url?: string;
  abs_web_url?: string;
}

export default function MobileUnifiedDashboard({ onNav }: Props) {
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
  const [scanStatus, setScanStatus] = useState<ScanProgress[]>([]);
  const [statsBySlug, setStatsBySlug] = useState<Record<string, DashboardStats>>({});
  const [syncingSlug, setSyncingSlug] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [mamScanning, setMamScanning] = useState(false);

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
    setD(r[0]);
    setHealth(r[1]);
    setMam(r[2]);
    setBudget(r[3]);
    setReviewCount(r[4]?.pending_count ?? 0);
    setTentativeCount(r[5]?.items?.length ?? 0);
    setCounts(r[6]);
    setGrabs(r[7]?.grabs ?? []);
    setSettings(r[8]);
    if (r[9]?.scans) setScanStatus(r[9].scans);
    const libs = (r[9]?.scans || []).filter((s) => s.kind === "library");
    if (libs.length > 0) {
      const byPair = await Promise.all(
        libs.map(async (ls) => {
          const slug = (ls as ScanProgress & { slug?: string }).slug || "";
          const s = await api
            .get<DashboardStats>(`/discovery/stats?slug=${encodeURIComponent(slug)}`)
            .catch(() => null);
          return [slug, s] as const;
        }),
      );
      const map: Record<string, DashboardStats> = {};
      for (const [slug, stats] of byPair) {
        if (stats && slug) map[slug] = stats;
      }
      setStatsBySlug(map);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Poll faster while a scan is in flight, slower while idle.
  const anyRunning =
    scanStatus.some((s) => s.running) || syncingSlug !== null;
  const pollMs = anyRunning ? 3000 : POLL * 1000;
  useVisibleInterval(refresh, pollMs);

  // Live MAM stat patches (ratio/wedges/seedbonus) come over SSE.
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

  // Per-library splits — first ebook lib + first audiobook lib.
  const statsEntries: DashboardStats[] = Object.values(statsBySlug);
  const ebookStats: DashboardStats =
    statsEntries.find((s) => s?.content_type === "ebook") || (d ?? {});
  const audiobookStats: DashboardStats | undefined = statsEntries.find(
    (s) => s?.content_type === "audiobook",
  );

  // Pipeline health derivations
  const dispatcherOk = !!health?.dispatcher_ready;
  const mamCookieOk = !!mam?.cookie_configured && !mam?.error;
  const ircOk = !!mam?.username; // proxy: if MAM stats are flowing, IRC + MAM are reachable

  // Commands
  const triggerSync = async (slug?: string) => {
    setSyncingSlug(slug || "__active__");
    try {
      const qs = slug ? `?slug=${encodeURIComponent(slug)}` : "";
      await api.post(`/discovery/sync/library${qs}`);
    } catch { /* ignore */ }
    setSyncingSlug(null);
    refresh();
  };
  // v2.12.0 — paired ebook/audiobook triggers, each cross-library.
  const triggerEbookSources = async () => {
    setScanning(true);
    try { await api.post("/discovery/lookup?content_type=ebook"); } catch { /* ignore */ }
    setScanning(false);
    refresh();
  };
  const triggerAudiobookSources = async () => {
    setScanning(true);
    try { await api.post("/discovery/lookup?content_type=audiobook"); } catch { /* ignore */ }
    setScanning(false);
    refresh();
  };
  const triggerMam = async () => {
    setMamScanning(true);
    try { await api.post("/discovery/mam/scan"); } catch { /* ignore */ }
    setMamScanning(false);
    refresh();
  };
  const cancelSources = async () => {
    try { await api.post("/discovery/lookup/cancel"); } catch { /* ignore */ }
    refresh();
  };
  const cancelMam = async () => {
    try { await api.post("/discovery/mam/scan/cancel"); } catch { /* ignore */ }
    refresh();
  };

  // v2.16.0 — Data Hygiene chain mirror of the desktop flow.
  // Confirmation gate before kicking off; busy state derives from
  // the in-flight scan-status entry.
  const [showHygieneConfirm, setShowHygieneConfirm] = useState(false);
  const [hygieneStarting, setHygieneStarting] = useState(false);
  const triggerHygiene = async () => {
    setHygieneStarting(true);
    try { await api.post("/discovery/hygiene/run"); } catch { /* ignore */ }
    setHygieneStarting(false);
    setShowHygieneConfirm(false);
    refresh();
  };
  const cancelHygiene = async () => {
    try { await api.post("/discovery/hygiene/cancel"); } catch { /* ignore */ }
    refresh();
  };

  const calibreWebUrl = settings?.cwa_web_url || settings?.calibre_web_url || "";
  const absWebUrl = settings?.abs_web_url || "";
  const allowed = counts?.authors_allowed ?? 0;
  const ignored = counts?.authors_ignored ?? 0;
  const totalGrabs = counts?.grabs ?? 0;
  const calibreAdds = counts?.calibre_additions ?? 0;

  const lookupScan = scanStatus.find((s) => s.kind === "lookup");
  const mamScan = scanStatus.find((s) => s.kind === "mam");
  const hygieneScan = scanStatus.find((s) => s.kind === "hygiene");
  const libScans = scanStatus.filter((s) => s.kind === "library");
  const activeScans = scanStatus.filter((s) => s.running);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 12,
      }}
    >
      {/* ─── Pipeline health pills ─────────────────────────── */}
      {/* Wrap to a second line on narrow phones — all four statuses
          should be visible at a glance, not hidden behind a horizontal
          scroll the user has to discover. */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 6,
          padding: "4px 2px",
        }}
      >
        <MobileHealthPill label="Dispatcher" ok={dispatcherOk} />
        <MobileHealthPill label="MAM" ok={mamCookieOk} />
        <MobileHealthPill label="IRC" ok={ircOk} warn={!ircOk} />
        <MobileHealthPill
          label="Budget"
          ok={(budget?.budget_used ?? 0) < (budget?.budget_cap ?? 1)}
          warn={(budget?.budget_used ?? 0) >= (budget?.budget_cap ?? 1) * 0.9}
        />
      </div>

      {/* ─── Athena: library heroes ────────────────────────── */}
      <MobileLibraryHero
        title={ebookStats.library_display_name || "Library"}
        icon="📖"
        color={t.jade}
        stats={ebookStats}
        onMamClick={() => onNav("disc-mam")}
      />
      {audiobookStats && (
        <MobileLibraryHero
          title={audiobookStats.library_display_name || "Audiobooks"}
          icon="🎧"
          color={t.cyan}
          stats={audiobookStats}
          onMamClick={() => onNav("disc-mam")}
        />
      )}

      {/* ─── Command Center: triggers + active scans ───────── */}
      <MobileSection title="Command Center" defaultOpen={true}>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            <MobileBtn
              variant="secondary"
              fullWidth
              onClick={() => triggerSync()}
              disabled={syncingSlug !== null}
            >
              {syncingSlug ? "Syncing…" : "Sync Library"}
            </MobileBtn>
            <MobileBtn
              variant="secondary"
              fullWidth
              onClick={triggerEbookSources}
              disabled={scanning}
            >
              {scanning ? "Scanning…" : "Scan Ebooks"}
            </MobileBtn>
            <MobileBtn
              variant="secondary"
              fullWidth
              onClick={triggerAudiobookSources}
              disabled={scanning}
            >
              {scanning ? "Scanning…" : "Scan Audiobooks"}
            </MobileBtn>
            <MobileBtn
              variant="secondary"
              fullWidth
              onClick={triggerMam}
              disabled={mamScanning}
            >
              {mamScanning ? "MAM Scanning…" : "MAM Scan"}
            </MobileBtn>
            <MobileBtn
              variant="secondary"
              fullWidth
              onClick={() => setShowHygieneConfirm(true)}
              disabled={hygieneStarting || !!hygieneScan?.running}
            >
              {hygieneScan?.running ? "Hygiene…" : "Data Hygiene"}
            </MobileBtn>
            <MobileBtn
              variant="primary"
              fullWidth
              onClick={() => onNav("pipe-review")}
              primary
            >
              Review {reviewCount > 0 ? `(${reviewCount})` : ""}
            </MobileBtn>
          </div>
          {activeScans.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 4 }}>
              {libScans.map((s) => (
                <MobileScanProgress
                  key={`${s.kind}-${(s as ScanProgress & { slug?: string }).slug || ""}`}
                  scan={s}
                  label={(s as ScanProgress & { slug?: string }).slug ? `Library: ${(s as ScanProgress & { slug?: string }).slug}` : s.label}
                />
              ))}
              {lookupScan && lookupScan.running && (
                <MobileScanProgress
                  scan={lookupScan}
                  label="Sources Scan"
                  onCancel={cancelSources}
                />
              )}
              {mamScan && mamScan.running && (
                <MobileScanProgress
                  scan={mamScan}
                  label="MAM Scan"
                  onCancel={cancelMam}
                />
              )}
              {hygieneScan && hygieneScan.running && (
                <MobileScanProgress
                  scan={hygieneScan}
                  label="Data Hygiene"
                  onCancel={cancelHygiene}
                />
              )}
            </div>
          )}
        </div>
      </MobileSection>

      {/* ─── Hermes: MAM + budget + recent activity ────────── */}
      <MobileSection title="Hermes" subtitle="Pipeline detail" defaultOpen={true}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {mam?.username && (
            <MobileMamAccount
              mam={mam}
              onClick={() => onNav("pipe-mam")}
            />
          )}
          {budget && <MobileSnatchBudget budget={budget} />}
          <div>
            <div
              style={{
                fontSize: 12,
                color: t.tg,
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.04em",
                marginBottom: 6,
              }}
            >
              Recent Activity
            </div>
            <MobileRecentActivity grabs={grabs} max={5} />
          </div>
        </div>
      </MobileSection>

      {/* ─── Discovery quick actions ───────────────────────── */}
      <MobileSection title="Discovery" defaultOpen={true}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <MobileRow
            title="Library"
            leadingIcon="📖"
            onClick={() => onNav("disc-library")}
          />
          <MobileRow
            title="Authors"
            leadingIcon="◉"
            onClick={() => onNav("disc-authors")}
          />
          <MobileRow
            title="Missing"
            leadingIcon="◌"
            onClick={() => onNav("disc-missing")}
          />
          <MobileRow
            title="Upcoming"
            leadingIcon="📅"
            onClick={() => onNav("disc-upcoming")}
          />
          <MobileRow
            title="MAM Search"
            leadingIcon="🔍"
            onClick={() => onNav("disc-mam")}
          />
          <MobileRow
            title="Metadata"
            leadingIcon="💡"
            onClick={() => onNav("disc-metadata")}
          />
          <MobileRow
            title="Works"
            leadingIcon="🔗"
            onClick={() => onNav("disc-works")}
          />
          <MobileRow
            title="Hidden"
            leadingIcon="🚫"
            onClick={() => onNav("disc-hidden")}
          />
        </div>
      </MobileSection>

      {/* ─── Pipeline quick actions ────────────────────────── */}
      <MobileSection title="Pipeline" defaultOpen={true}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <MobileRow
            title="Review"
            subtitle={reviewCount > 0 ? `${reviewCount} pending` : undefined}
            leadingIcon="📚"
            onClick={() => onNav("pipe-review")}
          />
          <MobileRow
            title="New Authors"
            subtitle={tentativeCount > 0 ? `${tentativeCount} pending` : undefined}
            leadingIcon="🔎"
            onClick={() => onNav("pipe-tentative")}
          />
          <MobileRow
            title="Weekly Ignored"
            leadingIcon="📊"
            onClick={() => onNav("pipe-ignored")}
          />
          <MobileRow
            title="Author Lists"
            leadingIcon="👤"
            onClick={() => onNav("pipe-authors")}
          />
          <MobileRow
            title="Filters"
            leadingIcon="🎯"
            onClick={() => onNav("filters")}
          />
          <MobileRow
            title="Delayed"
            leadingIcon="⏳"
            onClick={() => onNav("pipe-delayed")}
          />
        </div>
      </MobileSection>

      {/* ─── Tools ─────────────────────────────────────────── */}
      <MobileSection title="Tools" defaultOpen={false}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {calibreWebUrl && (
            <MobileRow
              title="Calibre-Web"
              leadingIcon="🌐"
              onClick={() => window.open(calibreWebUrl, "_blank", "noopener")}
            />
          )}
          {absWebUrl && (
            <MobileRow
              title="Audiobookshelf"
              leadingIcon="🎧"
              onClick={() => window.open(absWebUrl, "_blank", "noopener")}
            />
          )}
          <MobileRow
            title="Import / Export"
            leadingIcon="📦"
            onClick={() => onNav("disc-importexport")}
          />
          <MobileRow
            title="MAM Status"
            leadingIcon="📡"
            onClick={() => onNav("pipe-mam")}
          />
          <MobileRow
            title="Logs"
            leadingIcon="📋"
            onClick={() => onNav("logs")}
          />
          <MobileRow
            title="Database"
            leadingIcon="🗄️"
            onClick={() => onNav("database")}
          />
          <MobileRow
            title="Settings"
            leadingIcon="⚙️"
            onClick={() => onNav("settings")}
          />
        </div>
      </MobileSection>

      {/* ─── Stats grid ────────────────────────────────────── */}
      <MobileSection title="Stats" defaultOpen={true}>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 8,
          }}
        >
          <MobileStatTile
            label="Owned"
            value={fmtNum(ebookStats.owned_books ?? 0)}
            color={t.jade}
            onClick={() => onNav("disc-library")}
          />
          <MobileStatTile
            label="Missing"
            value={fmtNum(ebookStats.missing_books ?? 0)}
            color={t.red}
            onClick={() => onNav("disc-missing")}
          />
          <MobileStatTile
            label="New"
            value={fmtNum(ebookStats.new_books ?? 0)}
            color={t.cyan}
            onClick={() => onNav("disc-library")}
          />
          <MobileStatTile
            label="Upcoming"
            value={fmtNum(ebookStats.upcoming_books ?? 0)}
            color={t.pur}
            onClick={() => onNav("disc-upcoming")}
          />
          <MobileStatTile
            label="Authors"
            value={fmtNum(ebookStats.authors ?? 0)}
            onClick={() => onNav("disc-authors")}
          />
          <MobileStatTile
            label="Series"
            value={fmtNum(ebookStats.total_series ?? 0)}
          />
          {audiobookStats && (
            <>
              <MobileStatTile
                label="🎧 Owned"
                value={fmtNum(audiobookStats.owned_books ?? 0)}
                color={t.cyan}
              />
              <MobileStatTile
                label="🎧 Hours"
                value={fmtNum(Math.round((audiobookStats.total_duration_sec ?? 0) / 3600))}
                color={t.cyan}
              />
            </>
          )}
          <MobileStatTile
            label="To Review"
            value={fmtNum(reviewCount)}
            color={reviewCount > 0 ? t.accent : undefined}
            highlight={reviewCount > 0}
            onClick={() => onNav("pipe-review")}
          />
          <MobileStatTile
            label="New Authors"
            value={fmtNum(tentativeCount)}
            color={tentativeCount > 0 ? t.accent : undefined}
            highlight={tentativeCount > 0}
            onClick={() => onNav("pipe-tentative")}
          />
          <MobileStatTile
            label="Allowed"
            value={fmtNum(allowed)}
          />
          <MobileStatTile
            label="Ignored"
            value={fmtNum(ignored)}
          />
          <MobileStatTile
            label="To Calibre"
            value={fmtNum(calibreAdds)}
          />
          <MobileStatTile
            label="Total Grabs"
            value={fmtNum(totalGrabs)}
          />
        </div>
      </MobileSection>
      {showHygieneConfirm && (
        <MobileHygieneConfirm
          starting={hygieneStarting}
          onConfirm={triggerHygiene}
          onCancel={() => setShowHygieneConfirm(false)}
        />
      )}
    </div>
  );
}

const MOBILE_HYGIENE_JOBS = [
  "Empty author + series cleanup",
  "Hardcover identifier backfill",
  "Phase-2 author goodreads_id backfill",
  "Book deduplication",
  "Series consolidation",
  "ABS author cross-stamp",
  "Orphan author retrolink",
  "Cross-library person backfill",
  "Prune orphan author_links",
];

interface MobileHygieneConfirmProps {
  starting: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

function MobileHygieneConfirm({
  starting,
  onConfirm,
  onCancel,
}: MobileHygieneConfirmProps) {
  const t = useTheme();
  return (
    <div
      onClick={onCancel}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.6)",
        display: "flex",
        alignItems: "flex-end",
        justifyContent: "center",
        zIndex: 200,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%",
          maxHeight: "85vh",
          overflowY: "auto",
          background: t.bg2,
          borderTop: `1px solid ${t.border}`,
          borderTopLeftRadius: 12,
          borderTopRightRadius: 12,
          padding: 16,
          color: t.text,
        }}
      >
        <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 6 }}>
          Run Data Hygiene?
        </div>
        <div style={{ fontSize: 12, color: t.text2, marginBottom: 10 }}>
          Fans 9 jobs across every library, in order. Re-running is idempotent.
        </div>
        <ol style={{ paddingLeft: 22, margin: 0, marginBottom: 14 }}>
          {MOBILE_HYGIENE_JOBS.map((name) => (
            <li
              key={name}
              style={{ fontSize: 12, color: t.text2, marginBottom: 4 }}
            >
              {name}
            </li>
          ))}
        </ol>
        <div style={{ display: "flex", gap: 8 }}>
          <MobileBtn variant="secondary" fullWidth onClick={onCancel}>
            Cancel
          </MobileBtn>
          <MobileBtn
            variant="primary"
            fullWidth
            onClick={onConfirm}
            disabled={starting}
            primary
          >
            {starting ? "Starting…" : "Run Hygiene"}
          </MobileBtn>
        </div>
      </div>
    </div>
  );
}
