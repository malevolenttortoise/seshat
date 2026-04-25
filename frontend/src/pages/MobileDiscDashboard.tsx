// Mobile-native discovery dashboard. The desktop DiscDashboard
// branches to this when useMobileCodepath() is true. Renders a
// vertical stack: optional library switcher, library hero (with
// MAM tiles), stat grid, action buttons, active scan progress,
// quick-nav rows.
//
// Data fetching mirrors the desktop component — initial /stats fetch,
// then refresh on the `seshat:scans-updated` event when a scan
// transitions running→idle. No extra polling: the App-level scan
// poller already drives the event.
import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { fmtNum, pct } from "../lib/format";
import type {
  Library,
  NavFn,
  ScanProgress,
  SeriesSuggestionCountResponse,
} from "../types";
import {
  MobileBtn,
  MobileSection,
  MobileRow,
} from "../components/mobile";
import {
  MobileLibraryHero,
  MobileStatTile,
  MobileScanProgress,
} from "../components/mobile/dashboard";

interface DashboardProps {
  onNav: NavFn;
  libs?: Library[];
  activeLib?: string;
  switchLib?: (slug: string) => void | Promise<void>;
}

interface MamStats {
  upload_candidates?: number;
  available_to_download?: number;
  missing_everywhere?: number;
  total_unscanned?: number;
}

interface DashboardStats {
  owned_books: number;
  total_books: number;
  missing_books: number;
  new_books: number;
  upcoming_books?: number;
  total_series: number;
  authors: number;
  hidden_books?: number;
  mam_enabled?: boolean;
  mam_scanning_enabled?: boolean;
  mam?: MamStats;
  calibre_web_url?: string;
  calibre_url?: string;
  library_display_name?: string;
}

interface ScansUpdatedDetail {
  scans?: ScanProgress[];
}

export default function MobileDiscDashboard({
  onNav,
  libs = [],
  activeLib = "",
  switchLib,
}: DashboardProps) {
  const t = useTheme();
  const [d, setD] = useState<DashboardStats | null>(null);
  const [scans, setScans] = useState<ScanProgress[]>([]);
  const [sugCount, setSugCount] = useState(0);
  const [syncing, setSyncing] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [mamScanning, setMamScanning] = useState(false);

  useEffect(() => {
    api.get<DashboardStats>("/discovery/stats").then(setD).catch(() => {});
  }, []);

  useEffect(() => {
    const refresh = () =>
      api
        .get<SeriesSuggestionCountResponse>("/discovery/series-suggestions/count")
        .then((r) => setSugCount(r.pending || 0))
        .catch(() => {});
    refresh();
    window.addEventListener("seshat:suggestions-changed", refresh);
    return () =>
      window.removeEventListener("seshat:suggestions-changed", refresh);
  }, []);

  useEffect(() => {
    const onUpdate = (e: Event) => {
      const detail = (e as CustomEvent<ScansUpdatedDetail>).detail;
      const next = (detail && detail.scans) || [];
      setScans((prev) => {
        const someJustFinished =
          prev.some((p) => p.running) && !next.some((s) => s.running);
        if (someJustFinished) {
          api.get<DashboardStats>("/discovery/stats").then(setD).catch(() => {});
        }
        return next;
      });
    };
    window.addEventListener("seshat:scans-updated", onUpdate);
    return () =>
      window.removeEventListener("seshat:scans-updated", onUpdate);
  }, []);

  if (!d) return null;

  const triggerSync = async () => {
    setSyncing(true);
    try {
      await api.post("/discovery/sync/library");
    } catch { /* ignore */ }
    setSyncing(false);
    api.get<DashboardStats>("/discovery/stats").then(setD).catch(() => {});
  };
  const triggerSources = async () => {
    setScanning(true);
    try { await api.post("/discovery/lookup"); } catch { /* ignore */ }
    setScanning(false);
  };
  const triggerMam = async () => {
    setMamScanning(true);
    try { await api.post("/discovery/mam/scan"); } catch { /* ignore */ }
    setMamScanning(false);
  };

  const lookupScan = scans.find((s) => s.kind === "lookup");
  const mamScan = scans.find((s) => s.kind === "mam");
  const libScans = scans.filter((s) => s.kind === "library");
  const activeScans = scans.filter((s) => s.running);

  const calibreWebUrl = d.calibre_web_url || "";
  const calibreUrl = d.calibre_url || "";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/* Library switcher — only shown when there's more than one library. */}
      {libs.length > 1 && switchLib && (
        <select
          value={activeLib}
          onChange={(e) => switchLib(e.target.value)}
          style={{
            width: "100%",
            minHeight: 44,
            padding: "0 12px",
            background: t.inp,
            color: t.text,
            border: `1px solid ${t.border}`,
            borderRadius: 10,
            fontSize: 16,
          }}
        >
          {libs.map((lib) => (
            <option key={lib.slug} value={lib.slug}>
              {lib.content_type === "audiobook" ? "🎧 " : "📖 "}
              {lib.display_name || lib.name}
            </option>
          ))}
        </select>
      )}

      {/* Library hero with MAM tiles */}
      <MobileLibraryHero
        title={d.library_display_name || "Your Library"}
        icon={
          libs.find((l) => l.slug === activeLib)?.content_type === "audiobook"
            ? "🎧"
            : "📖"
        }
        color={t.jade}
        stats={{
          owned_books: d.owned_books,
          total_books: d.total_books,
          missing_books: d.missing_books,
          new_books: d.new_books,
          upcoming_books: d.upcoming_books,
          authors: d.authors,
          total_series: d.total_series,
          mam: d.mam,
        }}
        onMamClick={() => onNav("disc-mam")}
      />

      {/* Quick action buttons — Sync / Scan / MAM Scan */}
      <MobileSection title="Actions" defaultOpen={true}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <MobileBtn
            variant="primary"
            primary
            fullWidth
            onClick={triggerSync}
            disabled={syncing}
          >
            {syncing ? "Syncing…" : "Sync Library"}
          </MobileBtn>
          <MobileBtn
            variant="secondary"
            fullWidth
            onClick={triggerSources}
            disabled={scanning || lookupScan?.running}
          >
            {scanning || lookupScan?.running ? "Scanning…" : "Scan Sources"}
          </MobileBtn>
          {d.mam_enabled && (
            <MobileBtn
              variant="secondary"
              fullWidth
              onClick={triggerMam}
              disabled={mamScanning || mamScan?.running}
            >
              {mamScanning || mamScan?.running ? "MAM Scan…" : "MAM Scan"}
            </MobileBtn>
          )}
        </div>
        {activeScans.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 12 }}>
            {libScans.map((s) => (
              <MobileScanProgress
                key={`lib-${(s as ScanProgress & { slug?: string }).slug || ""}`}
                scan={s}
                label={(s as ScanProgress & { slug?: string }).slug ? `Library: ${(s as ScanProgress & { slug?: string }).slug}` : s.label}
              />
            ))}
            {lookupScan?.running && (
              <MobileScanProgress scan={lookupScan} label="Sources Scan" />
            )}
            {mamScan?.running && (
              <MobileScanProgress scan={mamScan} label="MAM Scan" />
            )}
          </div>
        )}
      </MobileSection>

      {/* Stats grid */}
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
            value={fmtNum(d.owned_books)}
            color={t.jade}
            onClick={() => onNav("disc-library")}
          />
          <MobileStatTile
            label="Missing"
            value={fmtNum(d.missing_books)}
            color={t.red}
            onClick={() => onNav("disc-missing")}
          />
          <MobileStatTile
            label="New Finds"
            value={fmtNum(d.new_books)}
            color={t.cyan}
          />
          <MobileStatTile
            label="Authors"
            value={fmtNum(d.authors)}
            color={t.pur}
            onClick={() => onNav("disc-authors")}
          />
          <MobileStatTile
            label="Series"
            value={fmtNum(d.total_series)}
          />
          <MobileStatTile
            label="Upcoming"
            value={fmtNum(d.upcoming_books ?? 0)}
            color={t.cyan}
            onClick={() => onNav("disc-upcoming")}
          />
          {sugCount > 0 && (
            <MobileStatTile
              label="Suggestions"
              value={fmtNum(sugCount)}
              color={t.accent}
              highlight
              onClick={() => onNav("disc-suggestions")}
            />
          )}
        </div>
      </MobileSection>

      {/* Quick nav rows */}
      <MobileSection title="Browse" defaultOpen={true}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <MobileRow title="Library" leadingIcon="📖" onClick={() => onNav("disc-library")} />
          <MobileRow title="Authors" leadingIcon="◉" onClick={() => onNav("disc-authors")} />
          <MobileRow title="Missing" leadingIcon="◌" onClick={() => onNav("disc-missing")} />
          <MobileRow title="Upcoming" leadingIcon="📅" onClick={() => onNav("disc-upcoming")} />
          {d.mam_enabled && (
            <MobileRow title="MAM Search" leadingIcon="🔍" onClick={() => onNav("disc-mam")} />
          )}
          {sugCount > 0 && (
            <MobileRow title="Suggestions" leadingIcon="💡" onClick={() => onNav("disc-suggestions")} />
          )}
          <MobileRow title="Hidden" leadingIcon="🚫" onClick={() => onNav("disc-hidden")} />
        </div>
      </MobileSection>

      {/* External tools — Calibre / etc. */}
      {(calibreWebUrl || calibreUrl) && (
        <MobileSection title="Tools" defaultOpen={false}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {calibreWebUrl && (
              <MobileRow
                title="Calibre-Web"
                leadingIcon="🌐"
                onClick={() => window.open(calibreWebUrl, "_blank", "noopener")}
              />
            )}
            {calibreUrl && (
              <MobileRow
                title="Calibre Library"
                leadingIcon="📚"
                onClick={() => window.open(calibreUrl, "_blank", "noopener")}
              />
            )}
            <MobileRow
              title="Settings"
              leadingIcon="⚙️"
              onClick={() => onNav("settings")}
            />
          </div>
        </MobileSection>
      )}
    </div>
  );
}
