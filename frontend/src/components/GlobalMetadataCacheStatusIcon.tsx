// v2.21.0 Phase F tier 1 — global header status icon.
// v3.6.0 — extended to dual-source (Amazon + Goodreads).
//
// A passive 18×18 indicator that lives in the navbar's right-rail
// alongside the other tool icons. Color-coded to the worst-of-both-
// sources health so the operator can glance at the bar and know
// whether either metadata cache worker needs attention:
//
//   gray   — all enabled sources disabled (intentional pause)
//   green  — all enabled sources healthy
//   amber  — at least one source stalled / cooldown / off-hours
//   red    — at least one source has failed_permanent rows
//
// Hover tooltip surfaces brief per-source stats. Click navigates to
// the Settings page where the full cache status cards live.
//
// Polls /api/v1/metadata-cache/{amazon,goodreads}/status in parallel
// every 60s. Silently no-ops on individual fetch errors so a transient
// blip (network hiccup, auth redirect, 404 on a legacy install)
// doesn't crash the navbar — the icon falls back to whichever
// source's status is still available.

import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";

type CacheMode = "continuous" | "scheduled" | "disabled";

type WorkerStatus = {
  enabled: boolean;
  mode?: CacheMode;
  schedule?: { active_hours: string; timezone: string };
  inside_schedule_window?: boolean;
  seconds_until_window_open?: number;
  cooldown: { blocked: boolean; remaining_s: number };
  worker: {
    today_scan_count: number;
    today_block_count: number;
    seconds_since_heartbeat: number | null;
  };
  queue: {
    pending: number;
    in_progress: number;
    failed_permanent: number;
    due_now?: number;
    scheduled_later?: number;
  };
  cache: {
    state_rows: number;
    ok_authors: number;
    unique_total_authors?: number;
    unique_ok_authors?: number;
  };
};

type SourceKey = "amazon" | "goodreads";
type SourceState = Record<SourceKey, WorkerStatus | null>;

const SOURCE_LABEL: Record<SourceKey, string> = {
  amazon: "Amazon",
  goodreads: "Goodreads",
};

const POLL_INTERVAL_MS = 60_000;
const HEARTBEAT_STALE_S = 300;  // 5 min — supervised task is dead


type Health = "loading" | "disabled" | "ok" | "off-hours" | "stalled" | "cooldown" | "failed";

function _classify(status: WorkerStatus | null): Health {
  if (status === null) return "loading";
  if (!status.enabled || status.mode === "disabled") return "disabled";
  if (status.queue.failed_permanent > 0) return "failed";
  if (status.cooldown.blocked) return "cooldown";
  const insideWindow = status.inside_schedule_window ?? true;
  if (status.mode === "scheduled" && !insideWindow) return "off-hours";
  const stale = status.worker.seconds_since_heartbeat === null
    || status.worker.seconds_since_heartbeat > HEARTBEAT_STALE_S;
  if (insideWindow && stale) return "stalled";
  return "ok";
}


// Worst-of-both aggregation: red > amber > green > gray > loading.
// Returns a tuple of (dotColor, primaryLabel) for the icon.
const HEALTH_RANK: Record<Health, number> = {
  loading: 0,
  disabled: 1,
  ok: 2,
  "off-hours": 3,
  stalled: 4,
  cooldown: 4,
  failed: 5,
};


function _worstHealth(states: SourceState): Health {
  let worst: Health = "loading";
  for (const k of Object.keys(states) as SourceKey[]) {
    const h = _classify(states[k]);
    if (HEALTH_RANK[h] > HEALTH_RANK[worst]) worst = h;
  }
  return worst;
}


function _composeTooltipLines(states: SourceState): string[] {
  const lines: string[] = ["Metadata cache workers:"];
  for (const key of ["amazon", "goodreads"] as SourceKey[]) {
    const status = states[key];
    const label = SOURCE_LABEL[key];
    if (status === null) {
      lines.push(`  ${label}: loading…`);
      continue;
    }
    const h = _classify(status);
    const dueNow = status.queue.due_now ?? status.queue.pending;
    const okAuthors = status.cache.unique_ok_authors ?? status.cache.ok_authors;
    const totalAuthors = status.cache.unique_total_authors ?? status.cache.state_rows;
    let primary: string;
    switch (h) {
      case "disabled":  primary = "disabled"; break;
      case "failed":    primary = `${status.queue.failed_permanent} failed perm`; break;
      case "cooldown":  primary = `cooldown (${Math.round(status.cooldown.remaining_s)}s)`; break;
      case "off-hours": primary = "off-hours"; break;
      case "stalled":   primary = "stalled"; break;
      case "ok":        primary = "active"; break;
      default:          primary = "—";
    }
    lines.push(
      `  ${label}: ${primary} · queue ${dueNow.toLocaleString()}` +
      ` · cached ${okAuthors.toLocaleString()}/${totalAuthors.toLocaleString()}` +
      ` · today ${status.worker.today_scan_count} scans`,
    );
  }
  return lines;
}


export function GlobalMetadataCacheStatusIcon({
  onClick,
}: {
  onClick: () => void;
}) {
  const t = useTheme();
  const [states, setStates] = useState<SourceState>({
    amazon: null, goodreads: null,
  });

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;
    const fetchOne = async (key: SourceKey): Promise<[SourceKey, WorkerStatus | null]> => {
      try {
        const r = await api.get<WorkerStatus>(
          `/v1/metadata-cache/${key}/status`,
        );
        return [key, r];
      } catch {
        // 404 on a legacy install / 401 on signed-out / network. Leave
        // the prior cached value in place; the next poll will retry.
        return [key, null];
      }
    };
    const fetchAll = async () => {
      const results = await Promise.all([fetchOne("amazon"), fetchOne("goodreads")]);
      if (cancelled) return;
      setStates(prev => {
        const next = { ...prev };
        for (const [key, status] of results) {
          if (status !== null) next[key] = status;
          else if (prev[key] === null) next[key] = null;
        }
        return next;
      });
    };
    fetchAll();
    timer = setInterval(fetchAll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, []);

  // Pre-load state: gray dot, "loading…" tooltip. Renders something
  // immediately so the navbar layout doesn't shift when polls resolve.
  if (states.amazon === null && states.goodreads === null) {
    return (
      <button
        onClick={onClick}
        title="Cache workers: loading…"
        aria-label="Metadata cache workers — loading"
        style={iconButtonStyle(t.textDim)}
      >
        ☁
      </button>
    );
  }

  const worst = _worstHealth(states);
  let dotColor: string;
  let primaryLabel: string;
  switch (worst) {
    case "failed":
      dotColor = t.err;
      primaryLabel = "Triage needed";
      break;
    case "stalled":
    case "cooldown":
      dotColor = "#cc9933";  // amber
      primaryLabel = worst === "stalled" ? "Stalled" : "Cooldown";
      break;
    case "off-hours":
      dotColor = t.textDim;
      primaryLabel = "Off-hours";
      break;
    case "ok":
      dotColor = t.ok;
      primaryLabel = "Active";
      break;
    case "disabled":
    default:
      dotColor = t.textDim;
      primaryLabel = "Disabled";
  }

  const tooltipLines = [
    `Cache workers: ${primaryLabel}`,
    ..._composeTooltipLines(states),
  ];
  const tooltip = tooltipLines.join("\n");

  return (
    <button
      onClick={onClick}
      title={tooltip}
      aria-label={`Metadata cache workers: ${primaryLabel}`}
      style={iconButtonStyle(dotColor)}
    >
      ☁
    </button>
  );
}


function iconButtonStyle(color: string): React.CSSProperties {
  return {
    background: "transparent",
    border: "none",
    cursor: "pointer",
    fontSize: 18,
    padding: "4px 8px",
    borderRadius: 4,
    color,
    opacity: 0.85,
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
  };
}
