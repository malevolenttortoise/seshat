// Dashboard — pipeline control center.
//
// Layout: hero status, stat grid, pipeline health, quick actions with
// context, and tools sidebar.
import { useCallback, useEffect, useState } from "react";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { api } from "../api";
import { useTheme } from "../theme";
import { fmtNum, fmtBytes, fmtRatio, fmtDuration } from "../lib/format";
import { useVisibleInterval } from "../hooks/useVisibleInterval";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import MobilePipelineDashboard from "./MobilePipelineDashboard";

interface DashboardProps { onNav: (page: string) => void; }

interface ReviewListResponse { items: unknown[]; pending_count: number; }
interface TentativeListResponse { items: unknown[]; }
interface HealthResponse { status: string; dispatcher_ready: boolean; }
interface MamStatusResponse {
  cookie_configured: boolean; validation_ok: boolean;
  ratio: number | null; wedges: number | null; seedbonus: number | null;
  username: string | null; classname: string | null;
  uploaded_bytes: number | null; downloaded_bytes: number | null;
  error: string | null;
}
interface AuthorOverviewResponse { counts: Record<string, number>; }
interface DataCounts { [key: string]: number; }
interface SettingsResponse { [key: string]: unknown; }
interface BudgetEntry {
  grab_id: number | null; torrent_name: string; author_blob: string;
  seeding_seconds: number; remaining_seconds: number; source: string;
}
interface BudgetResponse {
  budget_used: number; budget_cap: number; ledger_active: number;
  qbit_extras: number; queue_size: number; seed_seconds_required: number;
  next_release_seconds: number | null; entries: BudgetEntry[];
}

// Module-level cache so dashboard data persists across page navigations.
// The component reads from cache on mount and updates it after each poll.
const _cache: {
  reviewCount: number | null; tentativeCount: number | null;
  health: HealthResponse | null; mam: MamStatusResponse | null;
  authors: AuthorOverviewResponse | null; counts: DataCounts | null;
  recentGrabs: { torrent_name: string; author_blob: string; grabbed_at: string }[];
  budget: BudgetResponse | null;
  settings: SettingsResponse | null;
} = {
  reviewCount: null, tentativeCount: null, health: null, mam: null,
  authors: null, counts: null, recentGrabs: [], budget: null, settings: null,
};

const POLL_INTERVAL = 30;

export default function Dashboard(props: DashboardProps) {
  // Mobile codepath catches phones, iPads, and any touch device.
  const vp = useViewport();
  if (useMobileCodepath(vp)) {
    return <MobilePipelineDashboard {...props} />;
  }
  return <DesktopPipelineDashboard {...props} />;
}

function DesktopPipelineDashboard({ onNav }: DashboardProps) {
  const t = useTheme();
  const [reviewCount, setReviewCount] = useState<number | null>(_cache.reviewCount);
  const [tentativeCount, setTentativeCount] = useState<number | null>(_cache.tentativeCount);
  const [health, setHealth] = useState<HealthResponse | null>(_cache.health);
  const [mam, setMam] = useState<MamStatusResponse | null>(_cache.mam);
  const [authors, setAuthors] = useState<AuthorOverviewResponse | null>(_cache.authors);
  const [counts, setCounts] = useState<DataCounts | null>(_cache.counts);
  const [recentGrabs, setRecentGrabs] = useState(_cache.recentGrabs);
  const [budget, setBudget] = useState<BudgetResponse | null>(_cache.budget);
  const [settings, setSettings] = useState<SettingsResponse | null>(_cache.settings);
  const [error, setError] = useState<string | null>(null);
  const [countdown, setCountdown] = useState(POLL_INTERVAL);
  const [lastPoll, setLastPoll] = useState<Date | null>(null);

  // Lifted out of useEffect so useVisibleInterval can drive it.
  // Stable identity (no deps) — uses setState callbacks and module-
  // level _cache, so the closure has nothing to capture-stale.
  const refresh = useCallback(async () => {
    try {
      const [review, tentative, h, mamS, auth, cnt, recent, budgetR, settingsR] = await Promise.all([
        api.get<ReviewListResponse>("/v1/review"),
        api.get<TentativeListResponse>("/v1/tentative"),
        api.get<HealthResponse>("/health"),
        api.get<MamStatusResponse>("/v1/mam/status").catch(() => null),
        api.get<AuthorOverviewResponse>("/v1/authors").catch(() => null),
        api.get<DataCounts>("/v1/data/counts").catch(() => null),
        api.get<{ grabs: { torrent_name: string; author_blob: string; grabbed_at: string }[] }>("/v1/grabs/recent").catch(() => ({ grabs: [] })),
        api.get<BudgetResponse>("/v1/grabs/budget").catch(() => null),
        api.get<SettingsResponse>("/v1/settings").catch(() => null),
      ]);
      setReviewCount(review.pending_count);
      setTentativeCount(tentative.items.length);
      setHealth(h); setMam(mamS); setAuthors(auth); setCounts(cnt);
      if (recent) setRecentGrabs(recent.grabs);
      if (budgetR) setBudget(budgetR);
      if (settingsR) setSettings(settingsR);
      setError(null);
      // Update module-level cache.
      _cache.reviewCount = review.pending_count;
      _cache.tentativeCount = tentative.items.length;
      _cache.health = h; _cache.mam = mamS; _cache.authors = auth; _cache.counts = cnt;
      if (recent) _cache.recentGrabs = recent.grabs;
      if (budgetR) _cache.budget = budgetR;
      if (settingsR) _cache.settings = settingsR;
      // Reset countdown after successful poll.
      setCountdown(POLL_INTERVAL);
      setLastPoll(new Date());
    } catch (e) { setError(String(e)); }
  }, []);

  // Initial fetch on mount; the visible-interval hooks below take
  // over once the component has rendered.
  useEffect(() => { refresh(); }, [refresh]);

  // Polling cadence — paused when the tab is hidden, fires
  // immediately on visibilitychange-back-to-visible to catch up.
  useVisibleInterval(refresh, POLL_INTERVAL * 1000);

  // Countdown ticker — also visibility-aware so we don't burn
  // re-renders while no one is looking.
  useVisibleInterval(
    () => setCountdown((c) => Math.max(0, c - 1)),
    1000,
  );

  const cwaUrl = (settings?.cwa_web_url as string) || "";
  const calibreUrl = (settings?.calibre_web_url as string) || "";
  const allowed = authors?.counts?.allowed ?? 0;
  const ignored = authors?.counts?.ignored ?? 0;
  const grabs = counts?.grabs ?? 0;
  const calibreAdds = counts?.calibre_additions ?? 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>

      {error && (
        <div style={{ background: t.err + "22", border: `1px solid ${t.err}55`, color: t.err, padding: "10px 14px", borderRadius: 8, fontSize: 13 }}>
          {error}
        </div>
      )}

      {/* ── Hero: Pipeline Status ── */}
      <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 16, padding: "28px 32px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 20 }}>
          <div>
            <h1 style={{ fontSize: 30, fontWeight: 700, color: t.text, margin: 0 }}>Pipeline Status</h1>
            <p style={{ fontSize: 15, color: t.textDim, marginTop: 6 }}>
              {health?.dispatcher_ready
                ? `${fmtNum(grabs)} total grabs · ${fmtNum(calibreAdds)} books added to Calibre`
                : "Starting up…"}
            </p>
            {/* Status pills row */}
            <div style={{ display: "flex", gap: 20, marginTop: 16, flexWrap: "wrap", alignItems: "center" }}>
              <StatusPill label="Dispatcher" ok={health?.dispatcher_ready ?? false} />
              <StatusPill label="IRC Listener" ok={health?.dispatcher_ready ?? false} />
              <StatusPill label="MAM Cookie" ok={mam?.validation_ok ?? false} warn={mam?.cookie_configured === true && !mam?.validation_ok} />
              <StatusPill label="Budget Watcher" ok={health?.dispatcher_ready ?? false} />
              <div style={{
                marginLeft: 12, background: t.bg3, borderRadius: 8,
                padding: "8px 14px", display: "flex", alignItems: "center", gap: 8,
              }}>
                <div style={{ fontSize: 10, color: t.textDim, textTransform: "uppercase", letterSpacing: "0.05em", fontWeight: 600 }}>
                  Next poll
                </div>
                <div style={{ fontSize: 18, fontWeight: 700, color: countdown <= 5 ? t.accent : t.text2, minWidth: 32, textAlign: "center" }}>
                  {countdown > 0 ? `${countdown}s` : "..."}
                </div>
              </div>
            </div>
          </div>

          {/* MAM account summary */}
          {mam?.username && (
            <div style={{ background: t.bg3, borderRadius: 12, padding: "16px 20px", minWidth: 200, textAlign: "right" }}>
              <div style={{ fontSize: 12, color: t.textDim, textTransform: "uppercase", letterSpacing: "0.05em", fontWeight: 600 }}>
                MAM · {mam.username}
              </div>
              {mam.classname && <div style={{ fontSize: 11, color: t.textDim, marginTop: 2 }}>{mam.classname}</div>}
              <div style={{ marginTop: 10, display: "flex", gap: 20, justifyContent: "flex-end" }}>
                {mam.ratio !== null && (
                  <div>
                    <div style={{ fontSize: 28, fontWeight: 700, color: mam.ratio >= 1 ? t.ok : t.warn }}>{fmtRatio(mam.ratio)}</div>
                    <div style={{ fontSize: 11, color: t.textDim }}>Ratio</div>
                  </div>
                )}
                {mam.wedges !== null && (
                  <div>
                    <div style={{ fontSize: 28, fontWeight: 700, color: t.accent }}>{fmtNum(mam.wedges)}</div>
                    <div style={{ fontSize: 11, color: t.textDim }}>Wedges</div>
                  </div>
                )}
              </div>
              {(mam.uploaded_bytes || mam.downloaded_bytes) && (
                <div style={{ fontSize: 11, color: t.textDim, marginTop: 8 }}>
                  ↑ {fmtBytes(mam.uploaded_bytes)} · ↓ {fmtBytes(mam.downloaded_bytes)}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* MAM warning banner */}
      {mam?.cookie_configured && mam?.error && (
        <div style={{ background: t.warn + "18", border: `1px solid ${t.warn}33`, borderRadius: 10, padding: "12px 20px", fontSize: 13, color: t.warn, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span>⚠ MAM: {mam.error}</span>
          <button onClick={() => onNav("mam")} style={{ background: "none", border: "none", color: t.accent, cursor: "pointer", fontWeight: 600, fontSize: 13, textDecoration: "underline" }}>
            Fix →
          </button>
        </div>
      )}

      {/* ── Snatch Budget ── */}
      {budget && (
        <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "20px 24px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 16 }}>
            <div>
              <div style={{ fontSize: 12, fontWeight: 600, color: t.textDim, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 10 }}>
                Snatch Budget
              </div>
              <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                <span style={{ fontSize: 32, fontWeight: 700, color: budget.budget_used >= budget.budget_cap ? t.warn : t.accent }}>
                  {budget.budget_used}
                </span>
                <span style={{ fontSize: 16, color: t.textDim }}>/ {budget.budget_cap}</span>
              </div>
              <div style={{ fontSize: 11, color: t.textDim, marginTop: 4 }}>
                {budget.ledger_active} Seshat + {budget.qbit_extras} manual
                {budget.queue_size > 0 && <span style={{ color: t.warn, marginLeft: 8 }}>{budget.queue_size} queued</span>}
              </div>
              {budget.next_release_seconds !== null && (
                <div style={{ fontSize: 12, color: t.accent, marginTop: 6 }}>
                  Next release in {fmtDuration(budget.next_release_seconds)}
                </div>
              )}
              {lastPoll && (
                <div style={{ fontSize: 10, color: t.textDim, marginTop: 4, opacity: 0.5 }}>
                  Polled {lastPoll.toLocaleTimeString()}
                </div>
              )}
            </div>

            {/* Active entries list */}
            {budget.entries.length > 0 && (
              <div style={{ flex: 1, minWidth: 280, maxWidth: 500 }}>
                <div style={{ maxHeight: 160, overflowY: "auto" }}>
                  {budget.entries.map((e, i) => {
                    const pct = Math.min(100, (e.seeding_seconds / budget.seed_seconds_required) * 100);
                    return (
                      <div key={e.grab_id ?? `ext-${i}`} style={{ display: "flex", alignItems: "center", gap: 10, padding: "5px 0", borderBottom: `1px solid ${t.borderL}`, fontSize: 12 }}>
                        <div style={{ flex: 1, minWidth: 0, display: "flex", alignItems: "center", gap: 6 }}>
                          {e.source === "external" && (
                            <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 4, background: t.textDim + "22", color: t.textDim, fontWeight: 600, flexShrink: 0 }}>EXT</span>
                          )}
                          <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: t.text2 }}>{e.torrent_name}</div>
                        </div>
                        <div style={{ width: 60, height: 4, borderRadius: 2, background: t.bg4, flexShrink: 0 }}>
                          <div style={{ width: `${pct}%`, height: "100%", borderRadius: 2, background: pct >= 100 ? t.ok : t.accent }} />
                        </div>
                        <span style={{ width: 60, textAlign: "right", color: t.textDim, fontSize: 11, flexShrink: 0 }}>
                          {e.remaining_seconds > 0 ? fmtDuration(e.remaining_seconds) : "done"}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Stat Cards ── */}
      {/* 6 tiles at minmax(170px, 1fr) wouldn't quite fit one row
          at the Dashboard's NARROW_WIDTH (1120px) container,
          pushing "Total Grabs" onto its own row and breaking
          symmetry. 150px minmax fits all 6 comfortably on one row
          and still reads fine on mobile (wraps to 2 columns). */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 14 }}>
        <StatCard label="Books to Review" value={reviewCount} icon="📚" color={(reviewCount ?? 0) > 0 ? t.accent : t.textDim} nav={() => onNav("review")} highlight={(reviewCount ?? 0) > 0} />
        <StatCard label="New Authors" value={tentativeCount} icon="🔎" color={(tentativeCount ?? 0) > 0 ? t.warn : t.textDim} nav={() => onNav("tentative")} highlight={(tentativeCount ?? 0) > 0} />
        <StatCard label="Allowed" value={allowed} icon="✅" color={t.ok} nav={() => onNav("authors")} />
        <StatCard label="Ignored" value={ignored} icon="⛔" color={t.textDim} nav={() => onNav("authors")} />
        <StatCard label="To Calibre" value={calibreAdds} icon="📖" color={t.ok} />
        <StatCard label="Total Grabs" value={grabs} icon="📥" color={t.text2} />
      </div>

      {/* ── Three-column widget: Quick Actions | Recent Activity | Tools ── */}
      <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: 24, display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 0, alignItems: "start" }}>

        {/* Left: Quick Actions */}
        <div style={{ paddingRight: 20 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: t.textDim, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 12 }}>
            Quick Actions
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Btn variant="primary" onClick={() => onNav("review")}>
              📚 Review Books {reviewCount ? `(${reviewCount})` : ""}
            </Btn>
            <Btn onClick={() => onNav("tentative")}>
              🔎 New Authors {tentativeCount ? `(${tentativeCount})` : ""}
            </Btn>
            <Btn onClick={() => onNav("ignored-weekly")}>
              📊 Weekly Ignored
            </Btn>
            <Btn onClick={() => onNav("authors")}>
              👤 Author Lists
            </Btn>
            <Btn onClick={() => onNav("filters")}>
              🎯 Filters
            </Btn>
            <Btn onClick={() => onNav("delayed")}>
              ⏳ Delayed
            </Btn>
            {cwaUrl && (
              <Btn onClick={() => window.open(cwaUrl, "_blank")}>
                📕 CWA
              </Btn>
            )}
            {calibreUrl && (
              <Btn onClick={() => window.open(calibreUrl, "_blank")}>
                📗 Calibre
              </Btn>
            )}
          </div>
        </div>

        {/* Middle: Recent Activity */}
        <div style={{ borderLeft: `1px solid ${t.borderL}`, borderRight: `1px solid ${t.borderL}`, paddingLeft: 20, paddingRight: 20 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: t.textDim, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 12 }}>
            Recent Activity
          </div>
          {recentGrabs.length > 0 ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
              {recentGrabs.map((g, i) => (
                <div key={i} style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", fontSize: 12, padding: "6px 0", borderBottom: i < recentGrabs.length - 1 ? `1px solid ${t.borderL}` : "none" }}>
                  <div style={{ minWidth: 0, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    <span style={{ color: t.text2, fontWeight: 500 }}>{g.torrent_name}</span>
                    {g.author_blob && <span style={{ color: t.textDim, marginLeft: 6 }}>— {g.author_blob}</span>}
                  </div>
                  <span style={{ fontSize: 10, color: t.textDim, flexShrink: 0, marginLeft: 10 }}>
                    {new Date(g.grabbed_at + "Z").toLocaleDateString()}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ fontSize: 12, color: t.textDim, fontStyle: "italic" }}>No recent grabs yet.</div>
          )}
        </div>

        {/* Right: Tools */}
        <div style={{ paddingLeft: 20, display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: t.textDim, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>
            Tools
          </div>
          <ToolBtn label="Migration" icon="📦" onClick={() => onNav("migration")} />
          <ToolBtn label="MAM Account" icon="📡" onClick={() => onNav("mam")} />
          <ToolBtn label="Logs" icon="📝" onClick={() => onNav("logs")} />
          <ToolBtn label="Settings" icon="⚙️" onClick={() => onNav("settings")} />
        </div>
      </div>
    </div>
  );
}

function StatusPill({ label, ok, warn }: { label: string; ok: boolean; warn?: boolean }) {
  const t = useTheme();
  const color = ok ? t.ok : warn ? t.warn : t.textDim;
  const text = ok ? "Online" : warn ? "Check" : "Offline";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <div style={{ width: 10, height: 10, borderRadius: "50%", background: color, boxShadow: ok ? `0 0 6px ${color}66` : "none" }} />
      <div>
        <div style={{ fontSize: 13, fontWeight: 600, color: t.text2 }}>{label}</div>
        <div style={{ fontSize: 11, color }}>{text}</div>
      </div>
    </div>
  );
}

function StatCard({ label, value, icon, color, nav, highlight }: {
  label: string; value: number | string | null; icon: string; color: string;
  nav?: () => void; highlight?: boolean;
}) {
  const t = useTheme();
  return (
    <div
      onClick={nav}
      style={{
        background: t.bg2, border: `1px solid ${highlight ? color + "55" : t.border}`,
        borderRadius: 12, padding: "20px 22px",
        cursor: nav ? "pointer" : "default",
        transition: "border-color 0.2s, transform 0.1s",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 26 }}>{icon}</span>
        <span style={{ fontSize: 30, fontWeight: 700, color }}>
          {value === null ? <Spin size={20} /> : typeof value === "number" ? fmtNum(value) : value}
        </span>
      </div>
      <div style={{ fontSize: 13, color: t.textDim, marginTop: 10, fontWeight: 500 }}>{label}</div>
    </div>
  );
}

function ToolBtn({ label, icon, onClick }: { label: string; icon: string; onClick: () => void }) {
  const t = useTheme();
  return (
    <button onClick={onClick} style={{
      display: "flex", alignItems: "center", gap: 8,
      padding: "8px 14px", background: t.bg4, border: `1px solid ${t.border}`,
      borderRadius: 8, cursor: "pointer", fontSize: 13, fontWeight: 500,
      color: t.text2, whiteSpace: "nowrap", transition: "border-color 0.15s",
    }}>
      <span style={{ fontSize: 16 }}>{icon}</span> {label}
    </button>
  );
}
