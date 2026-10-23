// The Dashboard's Amazon and Goodreads cache rails, at the bottom of the
// Seshat Stats widget, on the shared metadata-cache status poller (G137).
// The wave-4 trial is read here: what they show must not change.
import { useTheme } from "../../theme";
import { fmtNum } from "../../lib/format";
import { useMetadataCacheStatus } from "../../hooks/useMetadataCacheStatus";
import { Tile } from "./Tile";

// ─── Amazon cache rail (v2.21.0 Phase F.3 / v3.6.0 dual-source) ──
//
// Lives at the bottom of the Seshat Stats widget. Four compact tiles:
// enabled state, cached authors fraction, scans today, blocks today
// (tone warn when >0). The pre-v3.6.0 "Recent finds" list was
// dropped when the dual-source layout landed — keeping two sources'
// rails compact + symmetric matters more than the celebratory list,
// and the worker's progress is observable through Scans Today.
//
// Polls /status every 60s, fails silently on auth / network /
// legacy-image errors so the dashboard doesn't crash for unrelated
// reasons.

type AmazonCacheStatus = {
  enabled: boolean;
  cooldown: { blocked: boolean; remaining_s: number };
  worker: {
    today_scan_count: number;
    today_block_count: number;
    seconds_since_heartbeat: number | null;
  };
  queue: { pending: number; due_now?: number; scheduled_later?: number };
  cache: {
    state_rows: number;
    ok_authors: number;
    // v2.22.0 — author-level dedup (DISTINCT author_id) so 2-library
    // setups don't double-count. Fall back to legacy fields if the
    // server is pre-v2.22.0.
    unique_total_authors?: number;
    unique_ok_authors?: number;
  };
};

const STATUS_POLL_MS = 60_000;

export function AmazonCacheRail({
  mobileMode,
  wideMode,
  sectionHdrStyle,
  onNavSettings,
}: {
  mobileMode: boolean;
  wideMode: boolean;
  sectionHdrStyle: React.CSSProperties;
  onNavSettings: () => void;
}) {
  const t = useTheme();
  // Shared poller (one request per tick with the navbar icon and the
  // Settings card); errors stay silent here — auth, network, legacy image.
  const { status } = useMetadataCacheStatus<AmazonCacheStatus>("amazon", { paceMs: STATUS_POLL_MS });

  // Cached authors fraction: author-level (DISTINCT author_id),
  // not per-library state rows. A 2-library setup with 645 unique
  // authors should report 645/645, not the inflated 1290/1290 that
  // the per-library row count produces. Falls back to legacy
  // state_rows/ok_authors when talking to a pre-v2.22.0 server.
  const cachedTotal = (
    status?.cache.unique_total_authors ?? status?.cache.state_rows ?? 0
  );
  const queuePending = (status?.queue.pending ?? 0);
  const totalAuthors = Math.max(cachedTotal, queuePending);
  const okAuthors = (
    status?.cache.unique_ok_authors ?? status?.cache.ok_authors ?? 0
  );

  return (
    <>
      <div style={sectionHdrStyle}>Amazon Cache</div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: mobileMode
            ? "repeat(2, 1fr)"
            : wideMode
            ? "repeat(2, 1fr)"
            : "repeat(4, 1fr)",
          gap: 6,
          marginBottom: 8,
        }}
      >
        <Tile
          compact
          label="Worker"
          value={
            status === null
              ? null
              : status.cooldown.blocked
              ? "Cooldown"
              : status.enabled
              ? "On"
              : "Off"
          }
          color={
            status === null
              ? undefined
              : status.cooldown.blocked
              ? t.warn
              : status.enabled
              ? t.ok
              : t.td
          }
          onClick={onNavSettings}
        />
        <Tile
          compact
          label="Cached"
          value={
            status === null
              ? null
              : `${fmtNum(okAuthors)} / ${fmtNum(totalAuthors)}`
          }
          color={t.accent}
          sub="authors"
          onClick={onNavSettings}
        />
        <Tile
          compact
          label="Scans"
          value={status === null ? null : fmtNum(status.worker.today_scan_count)}
          color={t.jade}
          sub="today"
        />
        <Tile
          compact
          label="Blocks"
          value={status === null ? null : fmtNum(status.worker.today_block_count)}
          color={
            (status?.worker.today_block_count ?? 0) > 0 ? t.warn : t.td
          }
          sub="today"
        />
      </div>
    </>
  );
}


// ─── Goodreads cache rail (v3.6.0) ──────────────────────────────
//
// Parity with the Amazon rail. Same 4-tile layout, different polling
// endpoint and different 4th-tile metric (Pages instead of Blocks).
// No "Recent finds" list — GR caches list pages, not per-book detail
// (ADR-0018 §1 Path B), so the celebratory per-book feed doesn't have
// a data source on the GR side. The list-page count is a cumulative
// glance metric: "how much GR data does the worker have cached."
//
// Polls /goodreads/status every 60s with the same silent-fail
// posture as the Amazon rail.

type GoodreadsCacheStatus = {
  enabled: boolean;
  mode?: string;
  cooldown: { blocked: boolean; remaining_s: number };
  worker: {
    today_scan_count: number;
    today_block_count: number;
    seconds_since_heartbeat: number | null;
  };
  queue: { pending: number; due_now?: number };
  cache: {
    state_rows: number;
    ok_authors: number;
    unique_total_authors?: number;
    unique_ok_authors?: number;
    list_pages_rows: number;
    today_budget_exhaust_count: number;
  };
};

export function GoodreadsCacheRail({
  mobileMode,
  wideMode,
  sectionHdrStyle,
  onNavSettings,
}: {
  mobileMode: boolean;
  wideMode: boolean;
  sectionHdrStyle: React.CSSProperties;
  onNavSettings: () => void;
}) {
  const t = useTheme();
  // Shared poller, as the Amazon rail; errors stay silent here.
  const { status } = useMetadataCacheStatus<GoodreadsCacheStatus>("goodreads", { paceMs: STATUS_POLL_MS });

  const cachedTotal = (
    status?.cache.unique_total_authors ?? status?.cache.state_rows ?? 0
  );
  const queuePending = (status?.queue.pending ?? 0);
  const totalAuthors = Math.max(cachedTotal, queuePending);
  const okAuthors = (
    status?.cache.unique_ok_authors ?? status?.cache.ok_authors ?? 0
  );
  const listPages = status?.cache.list_pages_rows ?? 0;

  // Worker tile: GR has no IP-level cooldown (ADR-0018 — Goodreads
  // softly degrades via per-author budget exhaust rather than hard
  // walls), so the "Cooldown" path that Amazon shows folds into a
  // generic "Off" when the operator disables the worker via mode.
  const workerLabel = (
    status === null ? null
    : status.mode === "disabled" || !status.enabled ? "Off"
    : (status.cache.today_budget_exhaust_count ?? 0) > 0 ? "Budget"
    : "On"
  );
  const workerColor = (
    status === null ? undefined
    : workerLabel === "On" ? t.ok
    : workerLabel === "Budget" ? t.warn
    : t.td
  );

  return (
    <>
      <div style={sectionHdrStyle}>Goodreads Cache</div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: mobileMode
            ? "repeat(2, 1fr)"
            : wideMode
            ? "repeat(2, 1fr)"
            : "repeat(4, 1fr)",
          gap: 6,
          marginBottom: 8,
        }}
      >
        <Tile
          compact
          label="Worker"
          value={workerLabel}
          color={workerColor}
          onClick={onNavSettings}
        />
        <Tile
          compact
          label="Cached"
          value={
            status === null
              ? null
              : `${fmtNum(okAuthors)} / ${fmtNum(totalAuthors)}`
          }
          color={t.accent}
          sub="authors"
          onClick={onNavSettings}
        />
        <Tile
          compact
          label="Scans"
          value={status === null ? null : fmtNum(status.worker.today_scan_count)}
          color={t.jade}
          sub="today"
        />
        <Tile
          compact
          label="Pages"
          value={status === null ? null : fmtNum(listPages)}
          color={t.accent}
          sub="cached"
        />
      </div>
    </>
  );
}
