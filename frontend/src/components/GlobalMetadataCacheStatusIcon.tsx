// v2.21.0 Phase F tier 1 — global header status icon.
//
// A passive 18×18 indicator that lives in the navbar's right-rail
// alongside the other tool icons. Color-coded so the operator can
// glance at the bar and know whether the Amazon metadata cache
// worker is healthy:
//
//   gray   — disabled (intentional pause)
//   green  — enabled, recent heartbeat, no cooldown
//   amber  — enabled but worker is stalled (>5min since last heartbeat)
//            OR cooldown engaged
//   red    — enabled, failed_permanent rows present (manual triage
//            needed)
//
// Hover tooltip surfaces brief stats. Click navigates to the Settings
// page where the full cache status card lives.
//
// Polls /api/v1/metadata-cache/amazon/status every 60s. Silently
// no-ops on fetch errors so a transient blip (network hiccup, auth
// redirect, endpoint not yet deployed on a legacy install) doesn't
// crash the navbar.

import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";

type WorkerStatus = {
  enabled: boolean;
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
  };
  cache: {
    state_rows: number;
    ok_authors: number;
  };
};

const POLL_INTERVAL_MS = 60_000;
const HEARTBEAT_STALE_S = 300;  // 5 min — supervised task is dead


export function GlobalMetadataCacheStatusIcon({
  onClick,
}: {
  onClick: () => void;
}) {
  const t = useTheme();
  const [status, setStatus] = useState<WorkerStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;
    const fetchStatus = async () => {
      try {
        const r = await api.get<WorkerStatus>(
          "/v1/metadata-cache/amazon/status",
        );
        if (!cancelled) setStatus(r);
      } catch {
        // Silent — could be 401 during sign-out, 404 on a legacy
        // image, or a transient network issue. We just don't update
        // the indicator. A persistent failure will keep showing
        // stale state until the next successful poll.
      }
    };
    fetchStatus();
    timer = setInterval(fetchStatus, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, []);

  // Pre-load state: gray dot, "loading…" tooltip. Renders something
  // immediately so the navbar layout doesn't shift when the first
  // poll resolves.
  if (status === null) {
    return (
      <button
        onClick={onClick}
        title="Cache worker: loading…"
        aria-label="Amazon cache worker status"
        style={iconButtonStyle(t.textDim)}
      >
        ☁
      </button>
    );
  }

  const stalled = status.enabled
    && (status.worker.seconds_since_heartbeat === null
        || status.worker.seconds_since_heartbeat > HEARTBEAT_STALE_S);

  let dotColor: string;
  let primaryLabel: string;
  if (!status.enabled) {
    dotColor = t.textDim;
    primaryLabel = "Disabled";
  } else if (status.queue.failed_permanent > 0) {
    dotColor = t.err;
    primaryLabel = `${status.queue.failed_permanent} failed permanent`;
  } else if (status.cooldown.blocked) {
    dotColor = "#cc9933";  // amber
    primaryLabel = "Cooldown";
  } else if (stalled) {
    dotColor = "#cc9933";  // amber
    primaryLabel = "Stalled";
  } else {
    dotColor = t.ok;
    primaryLabel = "Active";
  }

  const tooltipLines = [
    `Cache worker: ${primaryLabel}`,
    `Queue pending: ${status.queue.pending.toLocaleString()}`,
    `Cached authors: ${status.cache.ok_authors.toLocaleString()} / ${status.cache.state_rows.toLocaleString()}`,
    `Today: ${status.worker.today_scan_count} scans, ${status.worker.today_block_count} blocks`,
  ];
  if (status.cooldown.blocked) {
    tooltipLines.push(`Cooldown clears in ${Math.round(status.cooldown.remaining_s)}s`);
  }
  const tooltip = tooltipLines.join("\n");

  return (
    <button
      onClick={onClick}
      title={tooltip}
      aria-label={`Amazon cache worker: ${primaryLabel}`}
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
