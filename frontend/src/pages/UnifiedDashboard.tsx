// @ts-nocheck
// Unified Dashboard v6 — Athena | Hermes | MAM Activity | Command Center | Stats
import { useEffect, useState, useCallback } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { Spin } from "../components/Spin";
import { fmtNum, fmtBytes, fmtRatio, fmtDuration, pct } from "../lib/format";
import { useVisibleInterval } from "../hooks/useVisibleInterval";

interface Props { onNav: (page: string, arg?: string | number | null) => void; }
const POLL = 30;

export default function UnifiedDashboard({ onNav }: Props) {
  const t = useTheme();
  const [d, setD] = useState<any>(null);
  const [health, setHealth] = useState<any>(null);
  const [mam, setMam] = useState<any>(null);
  const [budget, setBudget] = useState<any>(null);
  const [reviewCount, setReviewCount] = useState(0);
  const [tentativeCount, setTentativeCount] = useState(0);
  const [counts, setCounts] = useState<any>(null);
  const [grabs, setGrabs] = useState<any[]>([]);
  const [settings, setSettings] = useState<any>(null);
  const [cd, setCd] = useState(POLL);
  const [scanStatus, setScanStatus] = useState<any>(null);
  const [syncing, setSyncing] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [mamScanning, setMamScanning] = useState(false);

  const refresh = useCallback(async () => {
    const r = await Promise.all([
      api.get("/discovery/stats").catch(() => null),
      api.get("/health").catch(() => null),
      api.get("/v1/mam/status").catch(() => null),
      api.get("/v1/grabs/budget").catch(() => null),
      api.get("/v1/review").catch(() => ({ pending_count: 0 })),
      api.get("/v1/tentative").catch(() => ({ items: [] })),
      api.get("/v1/data/counts").catch(() => null),
      api.get("/v1/grabs/recent").catch(() => ({ grabs: [] })),
      api.get("/v1/settings").catch(() => null),
      api.get("/discovery/scan-status").catch(() => null),
    ]);
    setD(r[0]); setHealth(r[1]); setMam(r[2]); setBudget(r[3]);
    setReviewCount(r[4]?.pending_count ?? 0);
    setTentativeCount(r[5]?.items?.length ?? 0);
    setCounts(r[6]); setGrabs(r[7]?.grabs ?? []); setSettings(r[8]);
    if (r[9]) setScanStatus(r[9]);
    setCd(POLL);
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const ds = d || {};
  const b = budget || {};
  const owned = ds.owned_books ?? 0, total = ds.total_books ?? 0;
  const missing = ds.missing_books ?? 0, upcoming = ds.upcoming_books ?? 0;
  const authors = ds.authors ?? 0, series = ds.total_series ?? 0;
  const newBooks = ds.new_books ?? 0;
  const mamStats = ds.mam || {};
  const mamFound = mamStats.found ?? 0;
  const mamPossible = mamStats.possible ?? 0, mamNotFound = mamStats.not_found ?? 0;
  const comp = total > 0 ? pct(owned, total) : 0;
  const allowed = counts?.authors_allowed ?? 0;
  const ignored = counts?.authors_ignored ?? 0;
  const totalGrabs = counts?.grabs ?? 0;
  const calibreAdds = counts?.calibre_additions ?? 0;
  const cwaUrl = settings?.cwa_web_url || "";
  const calibreUrl = settings?.calibre_web_url || "";

  // Scan progress — API returns {scans: [{kind: "lookup"}, {kind: "mam"}, {kind: "library"}]}
  const scansArr = scanStatus?.scans || [];
  const libScan = scansArr.find(s => s.kind === "library") || {};
  const srcScan = scansArr.find(s => s.kind === "lookup") || {};
  const mamScan = scansArr.find(s => s.kind === "mam") || {};

  const triggerSync = async () => { setSyncing(true); try { await api.post("/discovery/sync/library"); } catch {} setSyncing(false); refresh(); };
  const triggerSources = async () => { setScanning(true); try { await api.post("/discovery/lookup"); } catch {} setScanning(false); refresh(); };
  const triggerMam = async () => { setMamScanning(true); try { await api.post("/discovery/mam/scan"); } catch {} setMamScanning(false); refresh(); };
  const cancelSources = async () => { try { await api.post("/discovery/lookup/cancel"); } catch {} refresh(); };
  const cancelMam = async () => { try { await api.post("/discovery/mam/scan/cancel"); } catch {} refresh(); };

  const anyRunning = libScan.running || srcScan.running || mamScan.running || syncing;
  const pollMs = anyRunning ? 3000 : POLL * 1000;
  useVisibleInterval(refresh, pollMs);
  useVisibleInterval(() => setCd(c => Math.max(0, c - 1)), 1000);

  const hdr = (color?) => ({ fontSize: 13, fontWeight: 700, color: color || t.accent, textTransform: "uppercase" as const, letterSpacing: "0.05em" });
  const vsep = { borderLeft: `1px solid ${t.border}`, paddingLeft: 20, marginLeft: 4 };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>

      {/* ══════ ROW 1: Athena | Hermes ══════ */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>

        {/* ATHENA */}
        <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "14px 18px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
            <div>
              <div style={hdr()}><Dot color={t.accent} /> Athena</div>
              <div style={{ fontSize: 13, color: t.td, marginTop: 2 }}>{fmtNum(owned)} of {fmtNum(total)} books owned</div>
            </div>
            <div style={{ fontSize: 28, fontWeight: 800, color: t.accent }}>{comp}%</div>
          </div>
          <div style={{ height: 5, background: t.bg4, borderRadius: 3, overflow: "hidden", marginBottom: 12 }}>
            <div style={{ height: "100%", width: `${Math.min(comp, 100)}%`, background: `linear-gradient(90deg, ${t.jade}, ${t.accent})`, borderRadius: 3 }} />
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "stretch" }}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, flex: 1 }}>
              <MiniBox value={fmtNum(mamFound)} label="Available on MAM" color={t.jade} onClick={() => onNav("disc-mam")} />
              <MiniBox value={fmtNum(mamPossible)} label="Upload Candidates" color={t.ylw} onClick={() => onNav("disc-mam")} />
              <MiniBox value={fmtNum(mamNotFound)} label="Missing Everywhere" color={t.red} />
            </div>
            <div style={{ borderLeft: `1px solid ${t.border}`, paddingLeft: 10, display: "flex", flexDirection: "column", gap: 6, justifyContent: "center" }}>
              {cwaUrl && <TBtn icon={<Bar color={t.ylw} />} label="CWA" onClick={() => window.open(cwaUrl, "_blank")} />}
              {calibreUrl && <TBtn icon={<Bar color={t.jade} />} label="Calibre" onClick={() => window.open(calibreUrl, "_blank")} />}
              {!cwaUrl && !calibreUrl && <span style={{ fontSize: 11, color: t.tf }}>No links</span>}
            </div>
          </div>
        </div>

        {/* HERMES */}
        <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "14px 18px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
            <div style={{ flex: 1 }}>
              <div style={hdr(t.jade)}><Dot color={t.jade} /> Hermes</div>
              <div style={{ fontSize: 13, color: t.td, marginTop: 2 }}>
                {health?.dispatcher_ready ? `${fmtNum(totalGrabs)} grabs · ${fmtNum(calibreAdds)} to Calibre` : "Starting…"}
              </div>
              <div style={{ display: "flex", gap: 16, marginTop: 12, flexWrap: "wrap", alignItems: "center" }}>
                <Pill label="Dispatcher" ok={health?.dispatcher_ready} />
                <Pill label="IRC" ok={health?.dispatcher_ready} />
                <Pill label="Cookie" ok={mam?.validation_ok} warn={mam?.cookie_configured && !mam?.validation_ok} />
                <Pill label="Watcher" ok={health?.dispatcher_ready} />
                <div style={{ background: t.bg3, borderRadius: 8, padding: "6px 14px", display: "flex", alignItems: "center", gap: 6 }}>
                  <span style={{ fontSize: 12, color: t.td, textTransform: "uppercase", fontWeight: 600 }}>Poll</span>
                  <span style={{ fontSize: 18, fontWeight: 700, color: cd <= 5 ? t.accent : t.text2 }}>{cd}s</span>
                </div>
              </div>
            </div>
            {mam?.username && (
              <div style={{ background: t.bg3, borderRadius: 10, padding: "12px 16px", textAlign: "right", minWidth: 170 }}>
                <div style={{ fontSize: 12, color: t.td, textTransform: "uppercase", fontWeight: 600 }}>{mam.username}</div>
                {mam.classname && <div style={{ fontSize: 11, color: t.tf }}>{mam.classname}</div>}
                <div style={{ display: "flex", gap: 14, justifyContent: "flex-end", marginTop: 6 }}>
                  {mam.ratio != null && <div><div style={{ fontSize: 22, fontWeight: 700, color: mam.ratio >= 1 ? t.ok : t.warn }}>{fmtRatio(mam.ratio)}</div><div style={{ fontSize: 11, color: t.td }}>Ratio</div></div>}
                  {mam.wedges != null && <div><div style={{ fontSize: 22, fontWeight: 700, color: t.accent }}>{fmtNum(mam.wedges)}</div><div style={{ fontSize: 11, color: t.td }}>Wedges</div></div>}
                </div>
                {(mam.uploaded_bytes || mam.downloaded_bytes) && (
                  <div style={{ fontSize: 11, color: t.tf, marginTop: 4 }}>↑ {fmtBytes(mam.uploaded_bytes)} · ↓ {fmtBytes(mam.downloaded_bytes)}</div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ══════ ROW 2: MAM Activity ══════ */}
      <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "12px 18px" }}>
        <div style={{ ...hdr(), marginBottom: 8 }}><Dot color={t.accent} /> MAM Activity</div>
        <div style={{ display: "grid", gridTemplateColumns: "200px 1fr 1fr", gap: 0 }}>
          {/* Snatch Budget */}
          <div style={{ paddingRight: 16 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: t.td, textTransform: "uppercase", marginBottom: 4 }}>Snatch Budget</div>
            <div style={{ display: "flex", alignItems: "baseline", gap: 5 }}>
              <span style={{ fontSize: 22, fontWeight: 700, color: (b.budget_used ?? 0) >= (b.budget_cap ?? 1) ? t.warn : t.accent }}>{b.budget_used ?? 0}</span>
              <span style={{ fontSize: 12, color: t.td }}>/ {b.budget_cap ?? 0}</span>
            </div>
            <div style={{ fontSize: 11, color: t.td, marginTop: 1 }}>
              {b.ledger_active ?? 0} active + {b.qbit_extras ?? 0} manual
              {(b.queue_size ?? 0) > 0 && <span style={{ color: t.warn }}> · {b.queue_size} queued</span>}
            </div>
            {b.next_release_seconds > 0 && <div style={{ fontSize: 11, color: t.accent, marginTop: 2 }}>Next: {fmtDuration(b.next_release_seconds)}</div>}
          </div>
          {/* Recent Activity */}
          <div style={{ ...vsep, paddingRight: 16, overflow: "hidden" }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: t.td, textTransform: "uppercase", marginBottom: 4 }}>Recent Activity</div>
            {grabs.length > 0 ? grabs.slice(0, 5).map((g, i) => (
              <div key={i} style={{ display: "flex", fontSize: 12, padding: "2px 0", borderBottom: i < 4 ? `1px solid ${t.borderL}` : "none", overflow: "hidden" }}>
                <span style={{ color: t.text2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, minWidth: 0 }}>{g.torrent_name}</span>
                <span style={{ color: t.tf, marginLeft: 8, flexShrink: 0, fontSize: 10 }}>{g.grabbed_at ? new Date(g.grabbed_at + "Z").toLocaleDateString() : ""}</span>
              </div>
            )) : <div style={{ fontSize: 12, color: t.tf, fontStyle: "italic" }}>No recent grabs</div>}
          </div>
          {/* Seeding Progress */}
          <div style={vsep}>
            <div style={{ fontSize: 12, fontWeight: 600, color: t.td, textTransform: "uppercase", marginBottom: 4 }}>Seeding Progress</div>
            {b.entries?.length > 0 ? (
              <div style={{ maxHeight: 100, overflowY: "auto" }}>
                {b.entries.map((e, i) => {
                  const sp = Math.min(100, (e.seeding_seconds / (b.seed_seconds_required || 1)) * 100);
                  return (
                    <div key={e.grab_id ?? i} style={{ display: "flex", alignItems: "center", gap: 6, padding: "2px 0", borderBottom: `1px solid ${t.borderL}`, fontSize: 11 }}>
                      {e.source === "external" && <span style={{ fontSize: 8, padding: "0 3px", borderRadius: 3, background: t.td + "22", color: t.td, fontWeight: 600 }}>EXT</span>}
                      <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: t.text2, minWidth: 0 }}>{e.torrent_name}</span>
                      <div style={{ width: 60, height: 3, borderRadius: 2, background: t.bg4, flexShrink: 0 }}>
                        <div style={{ width: `${sp}%`, height: "100%", borderRadius: 2, background: sp >= 100 ? t.ok : t.accent }} />
                      </div>
                      <span style={{ width: 45, textAlign: "right", color: t.tf, fontSize: 10, flexShrink: 0 }}>{e.remaining_seconds > 0 ? fmtDuration(e.remaining_seconds) : "done"}</span>
                    </div>
                  );
                })}
              </div>
            ) : <div style={{ fontSize: 12, color: t.tf, fontStyle: "italic" }}>No active seeds</div>}
          </div>
        </div>
      </div>

      {/* ══════ ROW 3: Command Center | Quick Actions + Tools ══════ */}
      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 10 }}>

        {/* Command Center */}
        <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "14px 18px" }}>
          <div style={{ ...hdr(), marginBottom: 10 }}><Dot color={t.accent} /> Command Center</div>
          <div style={{ display: "grid", gridTemplateColumns: "auto 1fr auto", gap: 0, alignItems: "stretch" }}>
            {/* Trigger buttons */}
            <div style={{ display: "flex", flexDirection: "column", gap: 6, paddingRight: 16 }}>
              <CmdBtn label={<><Dot color={t.jade} /> Sync Library</>} busy={syncing || libScan.running} onClick={triggerSync} />
              <CmdBtn label={<><Dot color={t.cyan} /> Scan Sources</>} busy={scanning || srcScan.running} onClick={triggerSources} />
              <CmdBtn label={<><Dot color={t.ylw} /> MAM Scan</>} busy={mamScanning || mamScan.running} onClick={triggerMam} />
              <div style={{ borderTop: `1px solid ${t.borderL}`, paddingTop: 6, marginTop: 4, display: "flex", flexDirection: "column", gap: 6 }}>
                <CmdBtn label={`Review ${reviewCount ? `(${reviewCount})` : ""}`} highlight onClick={() => onNav("pipe-review")} />
                <CmdBtn label={`New Authors ${tentativeCount ? `(${tentativeCount})` : ""}`} onClick={() => onNav("pipe-tentative")} />
              </div>
            </div>
            {/* Progress display */}
            <div style={{ ...vsep, paddingRight: 40 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: t.td, textTransform: "uppercase", marginBottom: 8 }}>Progress</div>
              <ProgressRow label="Library Sync" scan={libScan} t={t} />
              <ProgressRow label="Source Scan" scan={srcScan} t={t} onCancel={srcScan.running ? cancelSources : undefined} />
              <ProgressRow label="MAM Scan" scan={mamScan} t={t} onCancel={mamScan.running ? cancelMam : undefined} />
            </div>
            {/* Scan stats summary */}
            <div style={{ ...vsep, minWidth: 200 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: t.td, textTransform: "uppercase", marginBottom: 8 }}>Last Scan</div>
              {srcScan.status === "complete" && srcScan.extra?.source_timeouts && Object.keys(srcScan.extra.source_timeouts).length > 0 ? (
                <div style={{ fontSize: 12, color: t.warn }}>
                  {Object.entries(srcScan.extra.source_timeouts).map(([src, sec]) => (
                    <div key={src}>{src}: timed out ({sec}s)</div>
                  ))}
                </div>
              ) : srcScan.extra?.new_books != null ? (
                <div style={{ fontSize: 14, color: t.text2 }}>
                  <div>{srcScan.current ?? 0} authors checked</div>
                  <div style={{ color: t.jade, fontSize: 16, fontWeight: 600, marginTop: 4 }}>{srcScan.extra.new_books ?? 0} new books</div>
                </div>
              ) : (
                <div style={{ fontSize: 12, color: t.tf, fontStyle: "italic" }}>No recent scan</div>
              )}
            </div>
          </div>
        </div>

        {/* Quick Actions + Tools */}
        <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "10px 16px", display: "flex", flexDirection: "column" }}>
          <div style={{ ...hdr(), marginBottom: 6 }}><Dot color={t.accent} /> Quick Actions</div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, flex: 1 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <div style={{ fontSize: 10, fontWeight: 600, color: t.tf, textTransform: "uppercase", marginBottom: 1 }}>Discovery</div>
              <QBtn label=<><Dot color={t.accent} /> Library</> onClick={() => onNav("disc-library")} />
              <QBtn label=<><Dot color={t.accent} /> Authors</> onClick={() => onNav("disc-authors")} />
              <QBtn label=<><Dot color={t.ylw} /> Missing</> onClick={() => onNav("disc-missing")} />
              <QBtn label=<><Dot color={t.cyan} /> Upcoming</> onClick={() => onNav("disc-upcoming")} />
              <QBtn label=<><Dot color={t.jade} /> MAM Search</> onClick={() => onNav("disc-mam")} />
              <QBtn label=<><Dot color={t.pur} /> Suggestions</> onClick={() => onNav("disc-suggestions")} />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <div style={{ fontSize: 10, fontWeight: 600, color: t.tf, textTransform: "uppercase", marginBottom: 1 }}>Pipeline</div>
              <QBtn label={`Review ${reviewCount ? `(${reviewCount})` : ""}`} primary onClick={() => onNav("pipe-review")} />
              <QBtn label=<><Dot color={t.warn} /> New Authors</> onClick={() => onNav("pipe-tentative")} />
              <QBtn label=<><Dot color={t.td} /> Weekly Ignored</> onClick={() => onNav("pipe-ignored")} />
              <QBtn label=<><Dot color={t.td} /> Author Lists</> onClick={() => onNav("pipe-authors")} />
              <QBtn label=<><Dot color={t.td} /> Filters</> onClick={() => onNav("filters")} />
              <QBtn label=<><Dot color={t.td} /> Delayed</> onClick={() => onNav("pipe-delayed")} />
            </div>
          </div>
          <div style={{ borderTop: `1px solid ${t.borderL}`, paddingTop: 6, marginTop: 6 }}>
            <div style={{ fontSize: 10, fontWeight: 600, color: t.td, textTransform: "uppercase", marginBottom: 5, textAlign: "center" }}>Tools</div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "center" }}>
              <TBtn icon={<Bar color={t.ylw} />} label="Migration" onClick={() => onNav("pipe-migration")} />
              <TBtn icon={<Bar color={t.cyan} />} label="MAM" onClick={() => onNav("pipe-mam")} />
              <TBtn icon={<Bar color={t.tf} />} label="Logs" onClick={() => onNav("logs")} />
              <TBtn icon={<Bar color={t.td} />} label="Settings" onClick={() => onNav("settings")} />
            </div>
          </div>
        </div>
      </div>

      {/* ══════ ROW 4: Seshat Stats (full width) ══════ */}
      <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "12px 18px" }}>
        <div style={{ ...hdr(), marginBottom: 8 }}><Dot color={t.accent} /> Seshat Stats</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 8 }}>
          <Tile label="Owned Books" value={fmtNum(owned)} color={t.accent} sub="Discovery" onClick={() => onNav("disc-library")} />
          <Tile label="Missing Books" value={fmtNum(missing)} color={t.ylw} sub="Discovery" onClick={() => onNav("disc-missing")} />
          <Tile label="New Books" value={fmtNum(newBooks)} color={t.jade} sub="Discovery" />
          <Tile label="Upcoming" value={fmtNum(upcoming)} color={t.cyan} sub="Discovery" onClick={() => onNav("disc-upcoming")} />
          <Tile label="Library Authors" value={fmtNum(authors)} sub="Discovery" onClick={() => onNav("disc-authors")} />
          <Tile label="Series" value={fmtNum(series)} sub="Discovery" />
          <Tile label="Suggestions" value={fmtNum(ds.suggestions ?? 0)} color={t.pur} sub="Discovery" onClick={() => onNav("disc-suggestions")} />
          <Tile label="MAM Found" value={fmtNum(mamFound)} color={t.jade} sub="Discovery" onClick={() => onNav("disc-mam")} />
          <Tile label="To Review" value={reviewCount} color={(reviewCount ?? 0) > 0 ? t.accent : t.td} sub="Pipeline" onClick={() => onNav("pipe-review")} />
          <Tile label="New Authors" value={tentativeCount} color={(tentativeCount ?? 0) > 0 ? t.warn : t.td} sub="Pipeline" onClick={() => onNav("pipe-tentative")} />
          <Tile label="Allowed Authors" value={fmtNum(allowed)} color={t.ok} sub="Pipeline" onClick={() => onNav("pipe-authors")} />
          <Tile label="Ignored Authors" value={fmtNum(ignored)} color={t.red} sub="Pipeline" onClick={() => onNav("pipe-authors")} />
          <Tile label="To Calibre" value={fmtNum(calibreAdds)} color={t.ok} sub="Pipeline" />
          <Tile label="Total Grabs" value={fmtNum(totalGrabs)} sub="Pipeline" />
        </div>
      </div>
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────

function MiniBox({ value, label, color, onClick }) {
  const t = useTheme();
  return (
    <div onClick={onClick} style={{
      background: t.bg3, borderRadius: 8, padding: "12px 10px",
      cursor: onClick ? "pointer" : "default", textAlign: "center",
      display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
    }}>
      <div style={{ fontSize: 22, fontWeight: 700, color: color || t.text }}>{value}</div>
      <div style={{ fontSize: 11, color: t.td, marginTop: 3 }}>{label}</div>
    </div>
  );
}

function Pill({ label, ok, warn }) {
  const t = useTheme();
  const color = ok ? t.ok : warn ? t.warn : t.td;
  const text = ok ? "Online" : warn ? "Check" : "Offline";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "4px 0" }}>
      <div style={{ width: 10, height: 10, borderRadius: "50%", background: color, boxShadow: ok ? `0 0 6px ${color}66` : "none" }} />
      <div>
        <div style={{ fontSize: 14, fontWeight: 600, color: t.text2 }}>{label}</div>
        <div style={{ fontSize: 12, color }}>{text}</div>
      </div>
    </div>
  );
}

function Tile({ label, value, color, sub, onClick }) {
  const t = useTheme();
  return (
    <div onClick={onClick} style={{ background: t.bg3, borderRadius: 8, padding: "10px 12px", cursor: onClick ? "pointer" : "default" }}>
      <div style={{ fontSize: 20, fontWeight: 700, color: color || t.text }}>{value === null ? <Spin size={14} /> : value}</div>
      <div style={{ fontSize: 12, color: t.td, marginTop: 2 }}>{label}</div>
      {sub && <div style={{ fontSize: 9, color: t.tf, marginTop: 1, textTransform: "uppercase", letterSpacing: "0.04em" }}>{sub}</div>}
    </div>
  );
}

function formatAgo(ts) {
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

function ProgressRow({ label, scan, t, onCancel }) {
  const running = scan?.running;
  const status = scan?.status || "idle";
  const authorName = scan?.current_label || "";
  const bookName = scan?.current_book || "";
  const checked = scan?.current ?? 0;
  const total = scan?.total ?? 0;
  const pctDone = total > 0 ? Math.floor((checked / total) * 100) : 0;
  const ago = !running ? formatAgo(scan?.completed_at) : null;
  // "Library Sync" → "Sync"; "Source Scan" / "MAM Scan" → "Scan".
  const kind = label.split(" ").pop();
  const statusText = status === "complete" ? "Done" : status === "cancelled" ? "Cancelled" : "Idle";
  return (
    <div style={{ padding: "6px 0", borderBottom: `1px solid ${t.borderL}` }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: running ? t.accent : t.td }}>{label}</span>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 13, color: running ? t.text2 : t.tf }}>
            {running ? `${checked}/${total} (${pctDone}%)` : (
              <>
                {ago && <span style={{ fontStyle: "italic" }}>(Last {kind}: {ago}) </span>}
                {statusText}
              </>
            )}
          </span>
          {running && onCancel && (
            <button onClick={onCancel} style={{
              padding: "2px 8px", fontSize: 10, fontWeight: 600, borderRadius: 4,
              background: t.red + "22", color: t.red, border: `1px solid ${t.red}44`,
              cursor: "pointer",
            }}>Stop</button>
          )}
        </div>
      </div>
      {running && (
        <>
          <div style={{ height: 4, background: t.bg4, borderRadius: 2, marginTop: 4, overflow: "hidden" }}>
            <div style={{ height: "100%", width: `${pctDone}%`, background: t.accent, borderRadius: 2, transition: "width 0.3s" }} />
          </div>
          {(authorName || bookName) && (
            <div style={{ fontSize: 12, color: t.td, marginTop: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {authorName && <span style={{ fontWeight: 600 }}>{authorName}</span>}
              {authorName && bookName && <span style={{ color: t.tf }}> — </span>}
              {bookName && <span style={{ color: t.tf }}>{bookName}</span>}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function CmdBtn({ label, busy, highlight, onClick }) {
  const t = useTheme();
  return (
    <button onClick={onClick} disabled={busy} style={{
      padding: "6px 12px", borderRadius: 6, fontSize: 11, fontWeight: 600,
      background: highlight ? t.accent : t.bg4, color: highlight ? t.bg : t.text2,
      border: `1px solid ${highlight ? t.accent : t.border}`, cursor: busy ? "wait" : "pointer",
      opacity: busy ? 0.6 : 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 4,
      whiteSpace: "nowrap",
    }}>{busy ? <Spin size={12} /> : null}{label}</button>
  );
}

function QBtn({ label, primary, onClick }) {
  const t = useTheme();
  return (
    <button onClick={onClick} style={{
      padding: "5px 8px", borderRadius: 5, fontSize: 11, fontWeight: 500,
      background: primary ? t.accent : t.bg4, color: primary ? t.bg : t.text2,
      border: `1px solid ${primary ? t.accent : t.border}`, cursor: "pointer",
      textAlign: "center", display: "flex", alignItems: "center", justifyContent: "center", gap: 4,
    }}>{label}</button>
  );
}

function TBtn({ icon, label, onClick }) {
  const t = useTheme();
  return (
    <button onClick={onClick} style={{
      display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
      padding: "8px 18px", background: t.bg4, border: `1px solid ${t.border}`,
      borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 500, color: t.text2,
    }}>{icon} {label}</button>
  );
}

function Dot({ color }) {
  return <span style={{
    display: "inline-block", width: 8, height: 8, borderRadius: "50%",
    background: color, marginRight: 4, verticalAlign: "middle",
    boxShadow: `0 0 4px ${color}44`,
  }} />;
}

function Bar({ color }) {
  return <span style={{
    display: "inline-block", width: 3, height: 14, borderRadius: 2,
    background: color, verticalAlign: "middle",
  }} />;
}
