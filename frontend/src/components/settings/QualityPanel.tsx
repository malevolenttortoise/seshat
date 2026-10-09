// Settings → Quality Metadata: coverage and the backfill.
// Split out of SettingsPage (wave 5b S16, issue 23).
import { useEffect, useState } from "react";
import { api } from "../../api";
import { useTheme } from "../../theme";
import { Btn } from "../Btn";
import { Spin } from "../Spin";
import { SF } from "./fields";

// ── v2.25.0 Quality Metadata panel ───────────────────────────

interface QualityStats {
  linked_torrents: number;
  extracted: number;
  missing: number;
  by_source: {
    mediainfo: number;
    description: number;
    tags: number;
    mixed: number;
    none: number;
    unavailable: number;
  };
}

interface BackfillStatus {
  running: boolean;
  started_at: number | null;
  finished_at: number | null;
  processed: number;
  skipped: number;
  failed: number;
  total_at_start: number;
  current_torrent_id: string | null;
  last_error: string | null;
  cancel_requested: boolean;
}

export function QualityPanel() {
  const t = useTheme();
  const [stats, setStats] = useState<QualityStats | null>(null);
  const [bf, setBf] = useState<BackfillStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    try {
      const [s, b] = await Promise.all([
        api.get<QualityStats>("/quality/stats"),
        api.get<BackfillStatus>("/quality/backfill/status"),
      ]);
      setStats(s);
      setBf(b);
    } catch {
      /* ignore — surface error in the UI on first manual click */
    }
  };

  useEffect(() => { refresh(); }, []);
  // Poll while a backfill is running so progress feels live.
  useEffect(() => {
    if (!bf?.running) return;
    const handle = setInterval(refresh, 3000);
    return () => clearInterval(handle);
  }, [bf?.running]);

  const startBackfill = async () => {
    setBusy(true);
    try { await api.post("/quality/backfill/start"); await refresh(); }
    finally { setBusy(false); }
  };
  const stopBackfill = async () => {
    setBusy(true);
    try { await api.post("/quality/backfill/stop"); await refresh(); }
    finally { setBusy(false); }
  };

  const pct = stats && stats.linked_torrents > 0
    ? Math.round((stats.extracted / stats.linked_torrents) * 100)
    : 0;

  return (
    <>
      <SF
        label="Coverage"
        desc="Quality metadata is extracted from MAM's API on every new grab. Use the backfill below to catch up existing owned books that pre-date v2.25.0."
        wide
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ fontSize: 13, color: t.text2 }}>
            {stats
              ? <><b>{stats.extracted}</b> / {stats.linked_torrents} linked torrents extracted ({pct}%) — <b>{stats.missing}</b> missing</>
              : "loading…"}
          </div>
          {stats && (
            <div style={{ fontSize: 11, color: t.textDim, lineHeight: 1.5 }}>
              By source: mediainfo {stats.by_source.mediainfo} · description {stats.by_source.description} · tags {stats.by_source.tags} · mixed {stats.by_source.mixed} · none {stats.by_source.none} · unavailable {stats.by_source.unavailable}
            </div>
          )}
        </div>
      </SF>
      <SF
        label="Backfill"
        desc="Walks every owned torrent missing quality data and runs the extraction pipeline against each. Rate-limited by `quality_backfill_interval_s` (default 5s) so MAM's per-IP throttle isn't tripped."
        warn="Backfill issues one MAM API call per torrent. For libraries with hundreds of books this can take 10+ minutes; safe to leave running in the background."
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {bf?.running
              ? <Btn variant="ghost" onClick={stopBackfill} disabled={busy}>{busy ? <Spin size={14} /> : "Stop"}</Btn>
              : <Btn variant="ghost" onClick={startBackfill} disabled={busy || (stats?.missing ?? 0) === 0}>{busy ? <Spin size={14} /> : "Start"}</Btn>}
            <Btn variant="ghost" onClick={refresh} disabled={busy}>Refresh</Btn>
          </div>
          {bf && (
            <div style={{ fontSize: 11, color: t.textDim, lineHeight: 1.5 }}>
              {bf.running
                ? <>
                    Running · processed <b style={{ color: t.text2 }}>{bf.processed}</b> · skipped {bf.skipped} · failed {bf.failed} of {bf.total_at_start} at start
                    {bf.current_torrent_id && <> · current tid <code>{bf.current_torrent_id}</code></>}
                    {bf.cancel_requested && <> · <span style={{ color: t.err }}>stop requested</span></>}
                  </>
                : bf.finished_at
                  ? <>Last run: processed {bf.processed} · skipped {bf.skipped} · failed {bf.failed}</>
                  : <>Not running</>}
              {bf.last_error && (
                <div style={{ color: t.err, marginTop: 4 }}>Last error: {bf.last_error}</div>
              )}
            </div>
          )}
        </div>
      </SF>
    </>
  );
}
