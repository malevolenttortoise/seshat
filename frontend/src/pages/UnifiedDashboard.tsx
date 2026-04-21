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
  const [statsBySlug, setStatsBySlug] = useState<Record<string, any>>({});

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
    // Second pass: fan out one /stats call per discovered library so
    // Calibre + ABS widgets render with their own numbers. Bounded by
    // the number of libraries (2 in practice); the parallel fetch adds
    // one network round-trip to the 30s poll loop.
    const libs = ((r[9] as any)?.scans || []).filter((s: any) => s.kind === "library");
    if (libs.length > 0) {
      const byPair = await Promise.all(
        libs.map(async (ls: any) => {
          const s = await api.get(`/discovery/stats?slug=${encodeURIComponent(ls.slug)}`).catch(() => null);
          return [ls.slug, s] as const;
        })
      );
      const map: Record<string, any> = {};
      for (const [slug, stats] of byPair) {
        if (stats) map[slug] = stats;
      }
      setStatsBySlug(map);
    }
    setCd(POLL);
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const ds = d || {};
  const b = budget || {};
  const allowed = counts?.authors_allowed ?? 0;
  const ignored = counts?.authors_ignored ?? 0;
  const totalGrabs = counts?.grabs ?? 0;
  const calibreAdds = counts?.calibre_additions ?? 0;
  const cwaUrl = settings?.cwa_web_url || "";
  const calibreUrl = settings?.calibre_web_url || "";
  const absWebUrl = settings?.abs_web_url || "";

  // Per-library stats split. Each content type picks the first library
  // of that kind from statsBySlug — matches the current 1-ebook +
  // 1-audiobook setup. Multi-library-per-kind users fall back to the
  // first discovered; a proper per-library tab can come later.
  const statsEntries = Object.values(statsBySlug);
  const ebookStats = statsEntries.find((s: any) => s?.content_type === "ebook") || ds;
  const audiobookStats = statsEntries.find((s: any) => s?.content_type === "audiobook");
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
  const mamStats = ebookStats?.mam || {};
  const mamFound = mamStats.available_to_download ?? 0;

  // Scan progress — API returns {scans: [...{kind: "lookup"}, {kind: "mam"}, {kind: "library", slug}...]}
  // Libraries are now keyed per-slug: Calibre and Audiobookshelf each
  // get their own entry so the Command Center shows dedicated rows
  // with independent in-flight progress + "(Last Sync: …)" timestamps.
  const scansArr = scanStatus?.scans || [];
  const libScans = scansArr.filter(s => s.kind === "library");
  const srcScan = scansArr.find(s => s.kind === "lookup") || {};
  const mamScan = scansArr.find(s => s.kind === "mam") || {};

  const triggerSync = async (slug?: string) => {
    setSyncingSlug(slug || "__active__");
    try {
      const qs = slug ? `?slug=${encodeURIComponent(slug)}` : "";
      await api.post(`/discovery/sync/library${qs}`);
    } catch {}
    setSyncingSlug(null);
    refresh();
  };
  const triggerSources = async () => { setScanning(true); try { await api.post("/discovery/lookup"); } catch {} setScanning(false); refresh(); };
  const triggerMam = async () => { setMamScanning(true); try { await api.post("/discovery/mam/scan"); } catch {} setMamScanning(false); refresh(); };
  const cancelSources = async () => { try { await api.post("/discovery/lookup/cancel"); } catch {} refresh(); };
  const cancelMam = async () => { try { await api.post("/discovery/mam/scan/cancel"); } catch {} refresh(); };

  const anyLibRunning = libScans.some(s => s.running);
  const anyRunning = anyLibRunning || srcScan.running || mamScan.running || syncingSlug !== null;
  const pollMs = anyRunning ? 3000 : POLL * 1000;
  useVisibleInterval(refresh, pollMs);
  useVisibleInterval(() => setCd(c => Math.max(0, c - 1)), 1000);

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

  const hdr = (color?) => ({ fontSize: 15, fontWeight: 700, color: color || t.accent, textTransform: "uppercase" as const, letterSpacing: "0.05em" });
  const vsep = { borderLeft: `1px solid ${t.border}`, paddingLeft: 20, marginLeft: 4 };

  // Grid area names: left (Athena+CC stacked), middle (Hermes with
  // absorbed MAM Activity), stats (narrow right rail), actions
  // (full-width bottom bar). Wide mode pins stats to a right column
  // spanning both the content and actions rows; narrow mode wraps
  // stats below the actions bar.
  const gridStyle = wideMode
    ? {
        display: "grid",
        gridTemplateColumns: "1fr 1fr 380px",
        gridTemplateAreas: `"left middle stats" "actions actions stats"`,
        gap: 10,
        alignItems: "start" as const,
      }
    : {
        display: "grid",
        gridTemplateColumns: "1fr 1fr",
        gridTemplateAreas: `"left middle" "actions actions" "stats stats"`,
        gap: 10,
        alignItems: "start" as const,
      };

  const sectionHdr = { fontSize: 13, fontWeight: 600, color: t.td, textTransform: "uppercase" as const, marginBottom: 8, letterSpacing: "0.04em" };
  const hsep = { height: 1, background: t.borderL, margin: "14px 0" };

  return (
    <div style={gridStyle}>

      {/* ══════ LEFT COLUMN: Athena + Command Center ══════ */}
      <div style={{ gridArea: "left", display: "flex", flexDirection: "column", gap: 10 }}>

        {/* ATHENA — ebook and audiobook sections stacked. Each has
            its own ownership percentage, progress bar, MAM metrics,
            and external-link column so the user can see both libraries'
            state simultaneously. */}
        <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "16px 20px" }}>
          <div style={{ ...hdr(), marginBottom: 10 }}><Dot color={t.accent} /> Athena</div>
          <LibrarySection
            stats={ebookStats}
            color={t.jade}
            accent={t.accent}
            links={[
              cwaUrl ? { label: "CWA", color: t.ylw, href: cwaUrl } : null,
              calibreUrl ? { label: "Calibre", color: t.jade, href: calibreUrl } : null,
            ].filter(Boolean) as any}
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
                  absWebUrl ? { label: "Audiobookshelf", color: t.pur, href: absWebUrl } : null,
                ].filter(Boolean) as any}
                onNavMam={() => onNav("disc-mam")}
                t={t}
              />
            </>
          )}
        </div>

        {/* COMMAND CENTER — triggers on top, progress rows below.
            Restructured from the old 3-col (buttons | progress | last)
            layout into a 2-row top/bottom so it reads cleanly at
            half-dashboard width instead of being squeezed horizontally. */}
        <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "16px 20px" }}>
          <div style={{ ...hdr(), marginBottom: 12 }}><Dot color={t.accent} /> Command Center</div>
          {/* Buttons row */}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 14 }}>
            {libScans.map(ls => {
              const color = ls.content_type === "audiobook" ? t.pur : t.jade;
              const shortLabel = ls.label?.replace(/\s*Sync$/, "") || ls.slug;
              return (
                <CmdBtn
                  key={ls.slug}
                  label={<><Dot color={color} /> Sync {shortLabel}</>}
                  busy={syncingSlug === ls.slug || ls.running}
                  onClick={() => triggerSync(ls.slug)}
                />
              );
            })}
            <CmdBtn label={<><Dot color={t.cyan} /> Scan Sources</>} busy={scanning || srcScan.running} onClick={triggerSources} />
            <CmdBtn label={<><Dot color={t.ylw} /> MAM Scan</>} busy={mamScanning || mamScan.running} onClick={triggerMam} />
            <CmdBtn label={`Review ${reviewCount ? `(${reviewCount})` : ""}`} highlight onClick={() => onNav("pipe-review")} />
            <CmdBtn label={`New Authors ${tentativeCount ? `(${tentativeCount})` : ""}`} onClick={() => onNav("pipe-tentative")} />
          </div>
          {/* Progress below */}
          <div style={sectionHdr}>Progress</div>
          {libScans.map(ls => (
            <ProgressRow key={ls.slug} label={ls.label || "Library Sync"} scan={ls} t={t} />
          ))}
          <ProgressRow label="Source Scan" scan={srcScan} t={t} onCancel={srcScan.running ? cancelSources : undefined} />
          <ProgressRow label="MAM Scan" scan={mamScan} t={t} onCancel={mamScan.running ? cancelMam : undefined} />
          {/* Last-scan summary inline, below the progress rows */}
          {(srcScan.extra?.new_books != null || (srcScan.extra?.source_timeouts && Object.keys(srcScan.extra.source_timeouts).length > 0)) && (
            <div style={{ marginTop: 12, paddingTop: 10, borderTop: `1px solid ${t.borderL}` }}>
              {srcScan.status === "complete" && srcScan.extra?.source_timeouts && Object.keys(srcScan.extra.source_timeouts).length > 0 ? (
                <div style={{ fontSize: 13, color: t.warn }}>
                  {Object.entries(srcScan.extra.source_timeouts).map(([src, sec]) => (
                    <div key={src}>{src}: timed out ({sec as any}s)</div>
                  ))}
                </div>
              ) : (
                <div style={{ fontSize: 13, color: t.text2 }}>
                  <span style={{ color: t.td }}>Last source scan: </span>
                  {srcScan.current ?? 0} authors checked · <span style={{ color: t.jade, fontWeight: 600 }}>{srcScan.extra?.new_books ?? 0} new books</span>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ══════ MIDDLE COLUMN: Hermes (expanded) ══════ */}
      {/* Absorbs the former MAM Activity row. Top subsection keeps the
          existing header + status pills + username card; below the
          divider sit Snatch Budget, Recent Activity, and Seeding
          Progress as stacked mini-sections. Widget is tall (roughly
          Athena+CC combined height) which matches the sketch. */}
      <div style={{ gridArea: "middle", background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "16px 20px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
          <div style={{ flex: 1 }}>
            <div style={hdr(t.jade)}><Dot color={t.jade} /> Hermes</div>
            <div style={{ fontSize: 14, color: t.td, marginTop: 3 }}>
              {health?.dispatcher_ready ? `${fmtNum(totalGrabs)} grabs · ${fmtNum(calibreAdds)} to Calibre` : "Starting…"}
            </div>
            <div style={{ display: "flex", gap: 18, marginTop: 14, flexWrap: "wrap", alignItems: "center" }}>
              <Pill label="Dispatcher" ok={health?.dispatcher_ready} />
              <Pill label="IRC" ok={health?.dispatcher_ready} />
              <Pill label="Cookie" ok={mam?.validation_ok} warn={mam?.cookie_configured && !mam?.validation_ok} />
              <Pill label="Watcher" ok={health?.dispatcher_ready} />
              <div style={{ background: t.bg3, borderRadius: 8, padding: "8px 16px", display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontSize: 12, color: t.td, textTransform: "uppercase", fontWeight: 600 }}>Poll</span>
                <span style={{ fontSize: 20, fontWeight: 700, color: cd <= 5 ? t.accent : t.text2 }}>{cd}s</span>
              </div>
            </div>
          </div>
          {mam?.username && (
            <div style={{ background: t.bg3, borderRadius: 10, padding: "14px 18px", textAlign: "right", minWidth: 190 }}>
              <div style={{ fontSize: 13, color: t.td, textTransform: "uppercase", fontWeight: 600 }}>{mam.username}</div>
              {mam.classname && <div style={{ fontSize: 12, color: t.tf }}>{mam.classname}</div>}
              <div style={{ display: "flex", gap: 18, justifyContent: "flex-end", marginTop: 8 }}>
                {mam.ratio != null && <div><div style={{ fontSize: 26, fontWeight: 700, color: mam.ratio >= 1 ? t.ok : t.warn }}>{fmtRatio(mam.ratio)}</div><div style={{ fontSize: 12, color: t.td }}>Ratio</div></div>}
                {mam.wedges != null && <div><div style={{ fontSize: 26, fontWeight: 700, color: t.accent }}>{fmtNum(mam.wedges)}</div><div style={{ fontSize: 12, color: t.td }}>Wedges</div></div>}
              </div>
              {(mam.uploaded_bytes || mam.downloaded_bytes) && (
                <div style={{ fontSize: 12, color: t.tf, marginTop: 6 }}>↑ {fmtBytes(mam.uploaded_bytes)} · ↓ {fmtBytes(mam.downloaded_bytes)}</div>
              )}
            </div>
          )}
        </div>

        <div style={hsep} />

        {/* Snatch Budget */}
        <div>
          <div style={sectionHdr}>Snatch Budget</div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
            <span style={{ fontSize: 32, fontWeight: 700, color: (b.budget_used ?? 0) >= (b.budget_cap ?? 1) ? t.warn : t.accent }}>{b.budget_used ?? 0}</span>
            <span style={{ fontSize: 16, color: t.td }}>/ {b.budget_cap ?? 0}</span>
            {b.next_release_seconds > 0 && <span style={{ fontSize: 13, color: t.accent, marginLeft: 12 }}>Next release in {fmtDuration(b.next_release_seconds)}</span>}
          </div>
          <div style={{ fontSize: 13, color: t.td, marginTop: 4 }}>
            {b.ledger_active ?? 0} active + {b.qbit_extras ?? 0} manual
            {(b.queue_size ?? 0) > 0 && <span style={{ color: t.warn }}> · {b.queue_size} queued</span>}
          </div>
        </div>

        <div style={hsep} />

        {/* Recent Activity */}
        <div>
          <div style={sectionHdr}>Recent Activity</div>
          {grabs.length > 0 ? grabs.slice(0, 5).map((g, i) => (
            <div key={i} style={{ display: "flex", fontSize: 13, padding: "4px 0", borderBottom: i < 4 ? `1px solid ${t.borderL}` : "none", overflow: "hidden" }}>
              <span style={{ color: t.text2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, minWidth: 0 }}>{g.torrent_name}</span>
              <span style={{ color: t.tf, marginLeft: 10, flexShrink: 0, fontSize: 12 }}>{g.grabbed_at ? new Date(g.grabbed_at + "Z").toLocaleDateString() : ""}</span>
            </div>
          )) : <div style={{ fontSize: 13, color: t.tf, fontStyle: "italic" }}>No recent grabs</div>}
        </div>

        <div style={hsep} />

        {/* Seeding Progress */}
        <div>
          <div style={sectionHdr}>Seeding Progress</div>
          {b.entries?.length > 0 ? (
            <div style={{ maxHeight: 180, overflowY: "auto" }}>
              {b.entries.map((e, i) => {
                const sp = Math.min(100, (e.seeding_seconds / (b.seed_seconds_required || 1)) * 100);
                return (
                  <div key={e.grab_id ?? i} style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0", borderBottom: `1px solid ${t.borderL}`, fontSize: 12 }}>
                    {e.source === "external" && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: t.td + "22", color: t.td, fontWeight: 600 }}>EXT</span>}
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: t.text2, minWidth: 0 }}>{e.torrent_name}</span>
                    <div style={{ width: 80, height: 4, borderRadius: 2, background: t.bg4, flexShrink: 0 }}>
                      <div style={{ width: `${sp}%`, height: "100%", borderRadius: 2, background: sp >= 100 ? t.ok : t.accent }} />
                    </div>
                    <span style={{ width: 55, textAlign: "right", color: t.tf, fontSize: 11, flexShrink: 0 }}>{e.remaining_seconds > 0 ? fmtDuration(e.remaining_seconds) : "done"}</span>
                  </div>
                );
              })}
            </div>
          ) : <div style={{ fontSize: 13, color: t.tf, fontStyle: "italic" }}>No active seeds</div>}
        </div>
      </div>

      {/* ══════ ACTIONS BAR: Quick Actions + Tools (full-width) ══════ */}
      <div style={{ gridArea: "actions", background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "14px 20px" }}>
        <div style={{ display: "flex", gap: 20, alignItems: "flex-start", flexWrap: "wrap" }}>
          <div style={{ flex: 2, minWidth: 280 }}>
            <div style={{ ...sectionHdr }}>Discovery</div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <QBtn label={<><Dot color={t.accent} /> Library</>} onClick={() => onNav("disc-library")} />
              <QBtn label={<><Dot color={t.accent} /> Authors</>} onClick={() => onNav("disc-authors")} />
              <QBtn label={<><Dot color={t.ylw} /> Missing</>} onClick={() => onNav("disc-missing")} />
              <QBtn label={<><Dot color={t.cyan} /> Upcoming</>} onClick={() => onNav("disc-upcoming")} />
              <QBtn label={<><Dot color={t.jade} /> MAM Search</>} onClick={() => onNav("disc-mam")} />
              <QBtn label={<><Dot color={t.pur} /> Suggestions</>} onClick={() => onNav("disc-suggestions")} />
              <QBtn label={<><Dot color={t.accent} /> Works</>} onClick={() => onNav("works")} />
            </div>
          </div>
          <div style={{ flex: 2, minWidth: 280, borderLeft: wideMode ? `1px solid ${t.border}` : "none", paddingLeft: wideMode ? 20 : 0 }}>
            <div style={sectionHdr}>Pipeline</div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <QBtn label={`Review ${reviewCount ? `(${reviewCount})` : ""}`} primary onClick={() => onNav("pipe-review")} />
              <QBtn label={<><Dot color={t.warn} /> New Authors</>} onClick={() => onNav("pipe-tentative")} />
              <QBtn label={<><Dot color={t.td} /> Weekly Ignored</>} onClick={() => onNav("pipe-ignored")} />
              <QBtn label={<><Dot color={t.td} /> Author Lists</>} onClick={() => onNav("pipe-authors")} />
              <QBtn label={<><Dot color={t.td} /> Filters</>} onClick={() => onNav("filters")} />
              <QBtn label={<><Dot color={t.td} /> Delayed</>} onClick={() => onNav("pipe-delayed")} />
            </div>
          </div>
          <div style={{ flex: 1, minWidth: 240, borderLeft: `1px solid ${t.border}`, paddingLeft: 20 }}>
            <div style={sectionHdr}>Tools</div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <TBtn icon={<Bar color={t.ylw} />} label="Migration" onClick={() => onNav("pipe-migration")} />
              <TBtn icon={<Bar color={t.cyan} />} label="MAM" onClick={() => onNav("pipe-mam")} />
              <TBtn icon={<Bar color={t.tf} />} label="Logs" onClick={() => onNav("logs")} />
              <TBtn icon={<Bar color={t.td} />} label="Settings" onClick={() => onNav("settings")} />
            </div>
          </div>
        </div>
      </div>

      {/* ══════ SESHAT STATS: right rail on wide, wraps below on narrow ══════ */}
      {/* Tiles are grouped into Ebook / Audiobook / Pipeline sections
          with sub-headers so the narrow column reads as three short
          stacks instead of one long run. Stays in a 2-col grid inside
          the rail — a single column would be too tall; 3+ would be
          too cramped at ~380px. When wrapped below the actions bar
          (narrow viewport) it widens to 4 columns for density. */}
      <div style={{ gridArea: "stats", background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: "16px 20px" }}>
        <div style={{ ...hdr(), marginBottom: 12 }}><Dot color={t.accent} /> Seshat Stats</div>

        <div style={sectionHdr}>Ebook</div>
        <div style={{ display: "grid", gridTemplateColumns: wideMode ? "repeat(2, 1fr)" : "repeat(4, 1fr)", gap: 8, marginBottom: 14 }}>
          <Tile label="Owned" value={fmtNum(owned)} color={t.accent} onClick={() => onNav("disc-library")} />
          <Tile label="Missing" value={fmtNum(missing)} color={t.ylw} onClick={() => onNav("disc-missing")} />
          <Tile label="New" value={fmtNum(newBooks)} color={t.jade} />
          <Tile label="Upcoming" value={fmtNum(upcoming)} color={t.cyan} onClick={() => onNav("disc-upcoming")} />
          <Tile label="Authors" value={fmtNum(authors)} onClick={() => onNav("disc-authors")} />
          <Tile label="Series" value={fmtNum(series)} />
          <Tile label="Suggestions" value={fmtNum(ds.suggestions ?? 0)} color={t.pur} onClick={() => onNav("disc-suggestions")} />
        </div>

        {audiobookStats && (
          <>
            <div style={sectionHdr}>Audiobook</div>
            <div style={{ display: "grid", gridTemplateColumns: wideMode ? "repeat(2, 1fr)" : "repeat(4, 1fr)", gap: 8, marginBottom: 14 }}>
              <Tile label="Owned" value={fmtNum(audiobookStats.owned_books ?? 0)} color={t.pur} onClick={() => onNav("disc-library")} />
              <Tile label="Hours" value={fmtNum(Math.round((audiobookStats.total_duration_sec ?? 0) / 3600))} color={t.pur} />
              <Tile label="Narrators" value={fmtNum(audiobookStats.narrator_count ?? 0)} color={t.pur} />
              <Tile label="Unabridged" value={fmtNum(audiobookStats.unabridged_count ?? 0)} color={t.jade} />
            </div>
          </>
        )}

        <div style={sectionHdr}>Pipeline</div>
        <div style={{ display: "grid", gridTemplateColumns: wideMode ? "repeat(2, 1fr)" : "repeat(4, 1fr)", gap: 8 }}>
          <Tile label="To Review" value={reviewCount} color={(reviewCount ?? 0) > 0 ? t.accent : t.td} onClick={() => onNav("pipe-review")} />
          <Tile label="New Authors" value={tentativeCount} color={(tentativeCount ?? 0) > 0 ? t.warn : t.td} onClick={() => onNav("pipe-tentative")} />
          <Tile label="Allowed" value={fmtNum(allowed)} color={t.ok} onClick={() => onNav("pipe-authors")} />
          <Tile label="Ignored" value={fmtNum(ignored)} color={t.red} onClick={() => onNav("pipe-authors")} />
          <Tile label="To Calibre" value={fmtNum(calibreAdds)} color={t.ok} />
          <Tile label="Total Grabs" value={fmtNum(totalGrabs)} />
        </div>
      </div>
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────

function LibrarySection({ stats, color, accent, links, onNavMam, t }: any) {
  // Renders one library's ownership summary inside the Athena widget.
  // Owned/total + percentage + progress bar on top, then MAM 3-box
  // + external-link column below so ebook + audiobook sections have
  // visual parity.
  const owned = stats?.owned_books ?? 0;
  const total = stats?.total_books ?? 0;
  const comp = total > 0 ? pct(owned, total) : 0;
  const mam = stats?.mam || {};
  const available = mam.available_to_download ?? 0;
  const upload = mam.upload_candidates ?? 0;
  const missing = mam.missing_everywhere ?? 0;
  const name = stats?.library_display_name || stats?.library_name || "Library";
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <div>
          <div style={{ fontSize: 14, fontWeight: 600, color: t.text2 }}>
            <Dot color={color} /> {name}
          </div>
          <div style={{ fontSize: 14, color: t.td, marginTop: 3 }}>
            {fmtNum(owned)} of {fmtNum(total)} {stats?.content_type === "audiobook" ? "audiobooks" : "books"} owned
          </div>
        </div>
        <div style={{ fontSize: 34, fontWeight: 800, color: accent, lineHeight: 1 }}>{comp}%</div>
      </div>
      <div style={{ height: 6, background: t.bg4, borderRadius: 3, overflow: "hidden", marginBottom: 12 }}>
        <div style={{ height: "100%", width: `${Math.min(comp, 100)}%`, background: `linear-gradient(90deg, ${color}, ${accent})`, borderRadius: 3 }} />
      </div>
      <div style={{ display: "flex", gap: 12, alignItems: "stretch" }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10, flex: 1 }}>
          <MiniBox value={fmtNum(available)} label="Available on MAM" color={color} onClick={onNavMam} />
          <MiniBox value={fmtNum(upload)} label="Upload Candidates" color={t.ylw} onClick={onNavMam} />
          <MiniBox value={fmtNum(missing)} label="Missing Everywhere" color={t.red} />
        </div>
        <div style={{ borderLeft: `1px solid ${t.border}`, paddingLeft: 12, display: "flex", flexDirection: "column", gap: 6, justifyContent: "center", minWidth: 130 }}>
          {links.length > 0 ? links.map((l: any) => (
            <TBtn key={l.label} icon={<Bar color={l.color} />} label={l.label} onClick={() => window.open(l.href, "_blank")} />
          )) : <span style={{ fontSize: 12, color: t.tf }}>No links</span>}
        </div>
      </div>
    </div>
  );
}

function MiniBox({ value, label, color, onClick }) {
  const t = useTheme();
  return (
    <div onClick={onClick} style={{
      background: t.bg3, borderRadius: 8, padding: "14px 10px",
      cursor: onClick ? "pointer" : "default", textAlign: "center",
      display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
    }}>
      <div style={{ fontSize: 26, fontWeight: 700, color: color || t.text, lineHeight: 1.1 }}>{value}</div>
      <div style={{ fontSize: 12, color: t.td, marginTop: 5 }}>{label}</div>
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
    <div onClick={onClick} style={{ background: t.bg3, borderRadius: 8, padding: "12px 14px", cursor: onClick ? "pointer" : "default" }}>
      <div style={{ fontSize: 24, fontWeight: 700, color: color || t.text, lineHeight: 1.1 }}>{value === null ? <Spin size={16} /> : value}</div>
      <div style={{ fontSize: 13, color: t.td, marginTop: 4 }}>{label}</div>
      {sub && <div style={{ fontSize: 10, color: t.tf, marginTop: 2, textTransform: "uppercase", letterSpacing: "0.04em" }}>{sub}</div>}
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
    <div style={{ padding: "8px 0", borderBottom: `1px solid ${t.borderL}` }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 14, fontWeight: 600, color: running ? t.accent : t.td }}>{label}</span>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 14, color: running ? t.text2 : t.tf }}>
            {running ? `${checked}/${total} (${pctDone}%)` : (
              <>
                {ago && <span style={{ fontStyle: "italic" }}>(Last {kind}: {ago}) </span>}
                {statusText}
              </>
            )}
          </span>
          {running && onCancel && (
            <button onClick={onCancel} style={{
              padding: "3px 10px", fontSize: 11, fontWeight: 600, borderRadius: 4,
              background: t.red + "22", color: t.red, border: `1px solid ${t.red}44`,
              cursor: "pointer",
            }}>Stop</button>
          )}
        </div>
      </div>
      {running && (
        <>
          <div style={{ height: 5, background: t.bg4, borderRadius: 2, marginTop: 5, overflow: "hidden" }}>
            <div style={{ height: "100%", width: `${pctDone}%`, background: t.accent, borderRadius: 2, transition: "width 0.3s" }} />
          </div>
          {(authorName || bookName) && (
            <div style={{ fontSize: 13, color: t.td, marginTop: 5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
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
      padding: "9px 14px", borderRadius: 6, fontSize: 13, fontWeight: 600,
      background: highlight ? t.accent : t.bg4, color: highlight ? t.bg : t.text2,
      border: `1px solid ${highlight ? t.accent : t.border}`, cursor: busy ? "wait" : "pointer",
      opacity: busy ? 0.6 : 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
      whiteSpace: "nowrap",
    }}>{busy ? <Spin size={14} /> : null}{label}</button>
  );
}

function QBtn({ label, primary, onClick }) {
  const t = useTheme();
  return (
    <button onClick={onClick} style={{
      padding: "7px 10px", borderRadius: 5, fontSize: 13, fontWeight: 500,
      background: primary ? t.accent : t.bg4, color: primary ? t.bg : t.text2,
      border: `1px solid ${primary ? t.accent : t.border}`, cursor: "pointer",
      textAlign: "center", display: "flex", alignItems: "center", justifyContent: "center", gap: 5,
    }}>{label}</button>
  );
}

function TBtn({ icon, label, onClick }) {
  const t = useTheme();
  return (
    <button onClick={onClick} style={{
      display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
      padding: "9px 18px", background: t.bg4, border: `1px solid ${t.border}`,
      borderRadius: 6, cursor: "pointer", fontSize: 13, fontWeight: 500, color: t.text2,
    }}>{icon} {label}</button>
  );
}

function Dot({ color }) {
  return <span style={{
    display: "inline-block", width: 9, height: 9, borderRadius: "50%",
    background: color, marginRight: 5, verticalAlign: "middle",
    boxShadow: `0 0 4px ${color}44`,
  }} />;
}

function Bar({ color }) {
  return <span style={{
    display: "inline-block", width: 3, height: 14, borderRadius: 2,
    background: color, verticalAlign: "middle",
  }} />;
}
