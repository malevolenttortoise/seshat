// Mobile-native pipeline dashboard. The desktop Dashboard branches
// to this when useMobileCodepath() is true. Stack order: pipeline
// health pills, MAM warning banner (if any), MAM account, snatch
// budget, stats grid, action rows, recent activity, tools.
//
// Polling cadence + module-level cache mirror the desktop component
// so dashboard data persists across page navigations.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { fmtNum } from "../lib/format";
import { useVisibleInterval } from "../hooks/useVisibleInterval";
import {
  MobileSection,
  MobileRow,
  MobileBtn,
  MobileBackButton,
} from "../components/mobile";
import {
  MobileHealthPill,
  MobileMamAccount,
  MobileSnatchBudget,
  MobileStatTile,
  MobileRecentActivity,
} from "../components/mobile/dashboard";

interface DashboardProps {
  onNav: (page: string) => void;
}

interface ReviewListResponse {
  items: unknown[];
  pending_count: number;
}
interface TentativeListResponse {
  items: unknown[];
}
interface HealthResponse {
  status: string;
  dispatcher_ready: boolean;
}
interface MamStatusResponse {
  cookie_configured: boolean;
  validation_ok: boolean;
  ratio: number | null;
  wedges: number | null;
  seedbonus: number | null;
  username: string | null;
  classname: string | null;
  uploaded_bytes: number | null;
  downloaded_bytes: number | null;
  error: string | null;
}
interface AuthorOverviewResponse {
  counts: Record<string, number>;
}
interface DataCounts {
  [key: string]: number;
}
interface SettingsResponse {
  [key: string]: unknown;
}
interface BudgetEntry {
  grab_id: number | null;
  torrent_name: string;
  author_blob: string;
  seeding_seconds: number;
  remaining_seconds: number;
  source: string;
}
interface BudgetResponse {
  budget_used: number;
  budget_cap: number;
  ledger_active: number;
  qbit_extras: number;
  queue_size: number;
  seed_seconds_required: number;
  next_release_seconds: number | null;
  entries: BudgetEntry[];
}

const POLL_INTERVAL = 30;

export default function MobilePipelineDashboard({ onNav }: DashboardProps) {
  const t = useTheme();
  const [reviewCount, setReviewCount] = useState<number | null>(null);
  const [tentativeCount, setTentativeCount] = useState<number | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [mam, setMam] = useState<MamStatusResponse | null>(null);
  const [authors, setAuthors] = useState<AuthorOverviewResponse | null>(null);
  const [counts, setCounts] = useState<DataCounts | null>(null);
  const [recentGrabs, setRecentGrabs] = useState<
    { torrent_name: string; author_blob: string; grabbed_at: string }[]
  >([]);
  const [budget, setBudget] = useState<BudgetResponse | null>(null);
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [review, tentative, h, mamS, auth, cnt, recent, budgetR, settingsR] =
        await Promise.all([
          api.get<ReviewListResponse>("/v1/review"),
          api.get<TentativeListResponse>("/v1/tentative"),
          api.get<HealthResponse>("/health"),
          api.get<MamStatusResponse>("/v1/mam/status").catch(() => null),
          api.get<AuthorOverviewResponse>("/v1/authors").catch(() => null),
          api.get<DataCounts>("/v1/data/counts").catch(() => null),
          api
            .get<{
              grabs: { torrent_name: string; author_blob: string; grabbed_at: string }[];
            }>("/v1/grabs/recent")
            .catch(() => ({ grabs: [] })),
          api.get<BudgetResponse>("/v1/grabs/budget").catch(() => null),
          api.get<SettingsResponse>("/v1/settings").catch(() => null),
        ]);
      setReviewCount(review.pending_count);
      setTentativeCount(tentative.items.length);
      setHealth(h);
      setMam(mamS);
      setAuthors(auth);
      setCounts(cnt);
      if (recent) setRecentGrabs(recent.grabs);
      if (budgetR) setBudget(budgetR);
      if (settingsR) setSettings(settingsR);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useVisibleInterval(refresh, POLL_INTERVAL * 1000);

  const cwaUrl = (settings?.cwa_web_url as string) || "";
  const calibreUrl = (settings?.calibre_web_url as string) || "";
  const allowed = authors?.counts?.allowed ?? 0;
  const ignored = authors?.counts?.ignored ?? 0;
  const grabs = counts?.grabs ?? 0;
  const calibreAdds = counts?.calibre_additions ?? 0;

  // Pipeline health derivations
  const dispatcherOk = !!health?.dispatcher_ready;
  const mamCookieOk = !!mam?.cookie_configured && !mam?.error;
  const mamValid = !!mam?.validation_ok;
  const budgetUsed = budget?.budget_used ?? 0;
  const budgetCap = budget?.budget_cap ?? 1;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />
      {error && (
        <div
          style={{
            padding: "10px 14px",
            background: t.redb,
            border: `1px solid ${t.redt}`,
            borderRadius: 10,
            color: t.red,
            fontSize: 14,
          }}
        >
          {error}
        </div>
      )}

      {/* Health pills */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 6,
          padding: "4px 2px",
        }}
      >
        <MobileHealthPill label="Dispatcher" ok={dispatcherOk} />
        <MobileHealthPill label="MAM" ok={mamCookieOk && mamValid} warn={mamCookieOk && !mamValid} />
        <MobileHealthPill
          label="Budget"
          ok={budgetUsed < budgetCap}
          warn={budgetUsed >= budgetCap * 0.9}
        />
      </div>

      {/* MAM cookie warning — when configured but throwing errors */}
      {mam?.cookie_configured && mam?.error && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 8,
            padding: "10px 14px",
            background: t.ylwb,
            border: `1px solid ${t.ylwt}`,
            borderRadius: 10,
            color: t.ylw,
            fontSize: 14,
          }}
        >
          <span>{mam.error}</span>
          <MobileBtn
            variant="ghost"
            onClick={() => onNav("settings")}
            style={{ minHeight: 36, fontSize: 13 }}
          >
            Fix
          </MobileBtn>
        </div>
      )}

      {/* MAM account card */}
      {mam?.username && (
        <MobileMamAccount
          mam={{
            username: mam.username || undefined,
            classname: mam.classname || undefined,
            ratio: mam.ratio ?? undefined,
            wedges: mam.wedges ?? undefined,
            seedbonus: mam.seedbonus ?? undefined,
            uploaded_bytes: mam.uploaded_bytes ?? undefined,
            downloaded_bytes: mam.downloaded_bytes ?? undefined,
          }}
          onClick={() => onNav("pipe-mam")}
        />
      )}

      {/* Snatch budget */}
      {budget && (
        <MobileSection title="Snatch Budget" defaultOpen={true}>
          <MobileSnatchBudget budget={budget} />
        </MobileSection>
      )}

      {/* Stats grid */}
      <MobileSection title="Pipeline Stats" defaultOpen={true}>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 8,
          }}
        >
          <MobileStatTile
            label="To Review"
            value={fmtNum(reviewCount ?? 0)}
            color={(reviewCount ?? 0) > 0 ? t.accent : undefined}
            highlight={(reviewCount ?? 0) > 0}
            onClick={() => onNav("pipe-review")}
          />
          <MobileStatTile
            label="New Authors"
            value={fmtNum(tentativeCount ?? 0)}
            color={(tentativeCount ?? 0) > 0 ? t.accent : undefined}
            highlight={(tentativeCount ?? 0) > 0}
            onClick={() => onNav("pipe-tentative")}
          />
          <MobileStatTile
            label="Allowed"
            value={fmtNum(allowed)}
            color={t.grn}
          />
          <MobileStatTile
            label="Ignored"
            value={fmtNum(ignored)}
            color={t.red}
          />
          <MobileStatTile
            label="To Calibre"
            value={fmtNum(calibreAdds)}
            color={t.cyan}
          />
          <MobileStatTile
            label="Total Grabs"
            value={fmtNum(grabs)}
          />
        </div>
      </MobileSection>

      {/* Pipeline action rows */}
      <MobileSection title="Pipeline" defaultOpen={true}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <MobileRow
            title="Review"
            subtitle={
              (reviewCount ?? 0) > 0 ? `${reviewCount} pending` : undefined
            }
            leadingIcon="📚"
            onClick={() => onNav("pipe-review")}
          />
          <MobileRow
            title="New Authors"
            subtitle={
              (tentativeCount ?? 0) > 0
                ? `${tentativeCount} pending`
                : undefined
            }
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

      {/* Recent activity */}
      <MobileSection title="Recent Activity" defaultOpen={true}>
        <MobileRecentActivity grabs={recentGrabs} max={5} />
      </MobileSection>

      {/* Tools */}
      <MobileSection title="Tools" defaultOpen={false}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {cwaUrl && (
            <MobileRow
              title="Calibre-Web"
              leadingIcon="🌐"
              onClick={() => window.open(cwaUrl, "_blank", "noopener")}
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
    </div>
  );
}
