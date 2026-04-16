// @ts-nocheck
// Unified Dashboard — single-screen overview of both domains.
// Designed for minimal scrolling on 1080p.
import { useEffect, useState, useCallback } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { Spin } from "../components/Spin";
import { fmtNum, fmtBytes, fmtRatio, fmtDuration } from "../lib/format";
import { pct, timeAgo } from "../lib/format";
import { useVisibleInterval } from "../hooks/useVisibleInterval";

interface Props {
  onNav: (page: string, arg?: string | number | null) => void;
}

// ── Stat card ────────────────────────────────────────────────

function Stat({ label, value, color, sub, onClick }: {
  label: string; value: string | number; color?: string; sub?: string;
  onClick?: () => void;
}) {
  const t = useTheme();
  return (
    <div onClick={onClick} style={{
      background: t.bg2, border: `1px solid ${t.borderL}`, borderRadius: 10,
      padding: "14px 16px", cursor: onClick ? "pointer" : "default",
      transition: "border-color 0.15s",
    }}
      onMouseEnter={e => onClick && (e.currentTarget.style.borderColor = t.accent)}
      onMouseLeave={e => onClick && (e.currentTarget.style.borderColor = t.borderL)}
    >
      <div style={{ fontSize: 22, fontWeight: 700, color: color || t.text }}>{value}</div>
      <div style={{ fontSize: 12, color: t.td, marginTop: 2 }}>{label}</div>
      {sub && <div style={{ fontSize: 11, color: t.tf, marginTop: 1 }}>{sub}</div>}
    </div>
  );
}

// ── Status pill ──────────────────────────────────────────────

function Pill({ label, ok, detail }: { label: string; ok: boolean; detail?: string }) {
  const t = useTheme();
  return (
    <span title={detail} style={{
      display: "inline-flex", alignItems: "center", gap: 5,
      padding: "3px 10px", borderRadius: 20,
      fontSize: 11, fontWeight: 600,
      background: ok ? t.grnb : t.redb,
      color: ok ? t.grn : t.red,
      border: `1px solid ${ok ? t.grnt : t.redt}`,
    }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: ok ? t.grn : t.red }} />
      {label}
    </span>
  );
}

// ── Main Dashboard ───────────────────────────────────────────

export default function UnifiedDashboard({ onNav }: Props) {
  const t = useTheme();
  const [disc, setDisc] = useState<any>(null);
  const [pipe, setPipe] = useState<any>(null);
  const [health, setHealth] = useState<any>(null);
  const [mam, setMam] = useState<any>(null);
  const [budget, setBudget] = useState<any>(null);
  const [review, setReview] = useState<any>(null);
  const [tentative, setTentative] = useState<any>(null);
  const [counts, setCounts] = useState<any>(null);
  const [recentGrabs, setRecentGrabs] = useState<any[]>([]);

  const poll = useCallback(() => {
    // Discovery stats
    api.get("/discovery/stats").then(setDisc).catch(() => {});
    // Pipeline stats
    api.get("/health").then(setHealth).catch(() => {});
    api.get("/v1/mam/status").then(setMam).catch(() => {});
    api.get("/v1/budget").then(setBudget).catch(() => {});
    api.get("/v1/review/queue?limit=0").then(r => setReview(r)).catch(() => {});
    api.get("/v1/tentative?limit=0").then(r => setTentative(r)).catch(() => {});
    api.get("/v1/data/counts").then(setCounts).catch(() => {});
    api.get("/v1/grabs/recent?limit=5").then(r => setRecentGrabs(r.grabs || [])).catch(() => {});
  }, []);

  useEffect(() => { poll(); }, [poll]);
  useVisibleInterval(poll, 30_000);

  const d = disc || {};
  const b = budget || {};
  const reviewCount = review?.pending_count ?? 0;
  const tentativeCount = tentative?.items?.length ?? 0;
  const allowed = counts?.authors_allowed ?? 0;
  const ignored = counts?.authors_ignored ?? 0;
  const totalGrabs = counts?.grabs ?? 0;
  const calibreAdds = counts?.calibre_additions ?? 0;

  // Discovery stats
  const owned = d.owned ?? 0;
  const totalBooks = d.total ?? 0;
  const missing = d.missing ?? 0;
  const upcoming = d.upcoming ?? 0;
  const authors = d.authors ?? 0;
  const series = d.series ?? 0;
  const newBooks = d.new_books ?? 0;
  const mamFound = d.mam_found ?? 0;
  const completion = totalBooks > 0 ? pct(owned, totalBooks) : 0;

  return (
    <div>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20 }}>
        <h1 style={{ fontSize: 24, fontWeight: 800, color: t.accent, margin: 0 }}>𓋹 Dashboard</h1>
        {health && <Pill label="API" ok={health.status === "ok"} />}
        {health && <Pill label="Dispatcher" ok={health.dispatcher_ready} />}
        {mam && <Pill label="IRC" ok={!!mam.username} detail={mam.username || "disconnected"} />}
        {mam && <Pill label="MAM Cookie" ok={mam.validation_ok} />}
      </div>

      {/* Row 1: Discovery overview + MAM account + Snatch budget */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 16, marginBottom: 16 }}>

        {/* Discovery overview */}
        <div style={{ background: t.bg2, border: `1px solid ${t.borderL}`, borderRadius: 12, padding: 20 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <div>
              <div style={{ fontSize: 14, fontWeight: 700, color: t.accent, textTransform: "uppercase", letterSpacing: "0.04em" }}>Library</div>
              <div style={{ fontSize: 12, color: t.td }}>{fmtNum(owned)} of {fmtNum(totalBooks)} books owned</div>
            </div>
            <div style={{ fontSize: 28, fontWeight: 800, color: t.accent }}>{completion}%</div>
          </div>
          <div style={{ height: 6, background: t.bg4, borderRadius: 3, overflow: "hidden" }}>
            <div style={{ height: "100%", width: `${Math.min(completion, 100)}%`, background: t.accent, borderRadius: 3 }} />
          </div>
        </div>

        {/* MAM account */}
        <div style={{ background: t.bg2, border: `1px solid ${t.borderL}`, borderRadius: 12, padding: 20 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: t.jade, textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 8 }}>MAM Account</div>
          {mam?.username ? (
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 16px", fontSize: 13 }}>
              <div><span style={{ color: t.td }}>User:</span> <span style={{ color: t.text }}>{mam.username}</span></div>
              <div><span style={{ color: t.td }}>Class:</span> <span style={{ color: t.text }}>{mam.classname || "—"}</span></div>
              <div><span style={{ color: t.td }}>Ratio:</span> <span style={{ color: t.text }}>{fmtRatio(mam.ratio)}</span></div>
              <div><span style={{ color: t.td }}>Wedges:</span> <span style={{ color: t.text }}>{fmtNum(mam.wedges)}</span></div>
              <div><span style={{ color: t.td }}>Up:</span> <span style={{ color: t.text }}>{fmtBytes(mam.uploaded_bytes)}</span></div>
              <div><span style={{ color: t.td }}>Down:</span> <span style={{ color: t.text }}>{fmtBytes(mam.downloaded_bytes)}</span></div>
            </div>
          ) : (
            <div style={{ fontSize: 13, color: t.td }}>Not connected</div>
          )}
        </div>

        {/* Snatch budget */}
        <div style={{ background: t.bg2, border: `1px solid ${t.borderL}`, borderRadius: 12, padding: 20 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: t.jade, textTransform: "uppercase", letterSpacing: "0.04em" }}>Snatch Budget</div>
            <div style={{ fontSize: 20, fontWeight: 700, color: t.text }}>{b.budget_used ?? 0}<span style={{ color: t.td, fontWeight: 400 }}>/{b.budget_cap ?? 0}</span></div>
          </div>
          <div style={{ height: 6, background: t.bg4, borderRadius: 3, overflow: "hidden", marginBottom: 8 }}>
            <div style={{ height: "100%", width: `${b.budget_cap ? Math.min((b.budget_used || 0) / b.budget_cap * 100, 100) : 0}%`, background: t.jade, borderRadius: 3 }} />
          </div>
          <div style={{ fontSize: 12, color: t.td }}>
            {b.queue_size ? `${b.queue_size} queued` : "No queue"}{b.next_release_seconds ? ` · next release ${fmtDuration(b.next_release_seconds)}` : ""}
          </div>
        </div>
      </div>

      {/* Row 2: Stat cards grid */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(8, 1fr)", gap: 12, marginBottom: 16 }}>
        <Stat label="Owned" value={fmtNum(owned)} color={t.accent} onClick={() => onNav("disc-library")} />
        <Stat label="Missing" value={fmtNum(missing)} color={t.ylw} onClick={() => onNav("disc-missing")} />
        <Stat label="New" value={fmtNum(newBooks)} color={t.jade} onClick={() => onNav("disc-library")} />
        <Stat label="Upcoming" value={fmtNum(upcoming)} color={t.cyan} onClick={() => onNav("disc-upcoming")} />
        <Stat label="To Review" value={reviewCount} color={t.accent} onClick={() => onNav("pipe-review")} />
        <Stat label="New Authors" value={tentativeCount} color={t.ylw} onClick={() => onNav("pipe-tentative")} />
        <Stat label="Allowed" value={fmtNum(allowed)} color={t.jade} onClick={() => onNav("pipe-authors")} />
        <Stat label="Ignored" value={fmtNum(ignored)} color={t.red} onClick={() => onNav("pipe-authors")} />
      </div>

      {/* Row 3: More stats + Recent activity */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr 2fr", gap: 16 }}>
        <Stat label="Authors" value={fmtNum(authors)} onClick={() => onNav("disc-authors")} />
        <Stat label="Series" value={fmtNum(series)} />
        <Stat label="MAM Found" value={fmtNum(mamFound)} color={t.jade} onClick={() => onNav("disc-mam")} />
        <Stat label="Total Grabs" value={fmtNum(totalGrabs)} />

        {/* Recent grabs */}
        <div style={{ background: t.bg2, border: `1px solid ${t.borderL}`, borderRadius: 10, padding: "14px 16px" }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: t.td, textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 8 }}>Recent Grabs</div>
          {recentGrabs.length === 0 ? (
            <div style={{ fontSize: 12, color: t.tf }}>No recent grabs</div>
          ) : (
            recentGrabs.slice(0, 5).map((g, i) => (
              <div key={i} style={{ fontSize: 12, padding: "3px 0", borderBottom: i < 4 ? `1px solid ${t.borderL}` : "none", display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: t.text2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "70%" }}>{g.torrent_name || "—"}</span>
                <span style={{ color: t.tf, fontSize: 11 }}>{g.grabbed_at ? timeAgo(new Date(g.grabbed_at).getTime() / 1000) : ""}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
