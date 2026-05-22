// v2.21.0 Phase F tier 3 — per-author cache status badge.
//
// Renders directly below the SourceBadgeRow on the author detail
// page. Only mounts when the author has a stored `amazon_id`
// (otherwise there's nothing to look up). Shows one short line per
// library the author exists in, with the most actionable cache
// state:
//
//   "Amazon (calibre-library): scanned 3d ago — 60 books cached"
//   "Amazon (abs-audio-library): in queue (priority 1000)"
//   "Amazon (calibre-library): never scanned"
//   "Amazon: scan failed permanently — needs triage"
//   "Amazon: scanning now"
//
// Polls /api/v1/metadata-cache/amazon/author/{amazon_author_id}
// every 30s while mounted so the line stays fresh during a long
// session. Silent on 404 (legacy install) / 401 (signed out) /
// transient failures.

import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";

type LibraryRow = {
  library_slug: string;
  state: {
    last_scanned_at: number | null;
    last_outcome: string | null;
    book_count: number | null;
  } | null;
  queue: {
    status: string;
    priority: number;
    next_scan_due_at: number;
    consecutive_failures: number;
  } | null;
};

type AuthorCacheResponse = {
  source: string;
  amazon_author_id: string;
  libraries: LibraryRow[];
  cooldown: { blocked: boolean; remaining_s: number };
};


const POLL_INTERVAL_MS = 30_000;

// Amazon palette mirrors SourceBadgeRow.SOURCE_PALETTE.amazon so
// the cache badge visually anchors to the existing Amazon source-ID
// badge above it.
const AMAZON_BADGE_BG = "#3d2e1a";
const AMAZON_BADGE_FG = "#f0a83c";
const AMAZON_BADGE_BR = "#7a5c2a";


function _formatSecondsAgo(s: number): string {
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${(s / 86400).toFixed(1)}d ago`;
}


function _composeStatusLine(row: LibraryRow, cooldownActive: boolean): {
  text: string; tone: "ok" | "info" | "warn" | "err";
} {
  // Active in_progress wins regardless — the worker is actively
  // touching this author RIGHT NOW.
  if (row.queue?.status === "in_progress") {
    return { text: "scanning now", tone: "info" };
  }
  // failed_permanent surfaces immediately so operators see triage
  // candidates without scanning the queue.
  if (row.queue?.status === "failed_permanent") {
    return { text: "scan failed permanently — needs triage", tone: "err" };
  }
  // State row present → show the most recent scan outcome.
  if (row.state) {
    const outcome = row.state.last_outcome;
    const scanned = row.state.last_scanned_at;
    const count = row.state.book_count ?? 0;
    if (outcome === "ok" && scanned !== null) {
      const ago = _formatSecondsAgo(Date.now() / 1000 - scanned);
      return {
        text: count > 0
          ? `scanned ${ago} — ${count} book${count === 1 ? "" : "s"} cached`
          : `scanned ${ago} — no books returned`,
        tone: "ok",
      };
    }
    if (outcome === "error" && scanned !== null) {
      return {
        text: `last scan errored ${_formatSecondsAgo(Date.now() / 1000 - scanned)}`,
        tone: "warn",
      };
    }
  }
  // No state row but a pending queue row → "in queue."
  if (row.queue?.status === "pending") {
    const priorityNote = row.queue.priority >= 500
      ? " (priority bumped)"
      : "";
    if (cooldownActive) {
      return {
        text: `in queue${priorityNote} — waiting on cooldown`,
        tone: "warn",
      };
    }
    return { text: `in queue${priorityNote}`, tone: "info" };
  }
  // Neither state nor queue → never enqueued.
  return { text: "never scanned", tone: "info" };
}


export function AuthorCacheStatusBadge({
  amazonAuthorId,
}: {
  amazonAuthorId: string | null | undefined;
}) {
  const t = useTheme();
  const [data, setData] = useState<AuthorCacheResponse | null>(null);

  useEffect(() => {
    if (!amazonAuthorId) {
      setData(null);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;
    const fetchStatus = async () => {
      try {
        const r = await api.get<AuthorCacheResponse>(
          `/v1/metadata-cache/amazon/author/${encodeURIComponent(amazonAuthorId)}`,
        );
        if (!cancelled) setData(r);
      } catch {
        // Silent — could be 401 / 404 / network issue.
      }
    };
    fetchStatus();
    timer = setInterval(fetchStatus, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [amazonAuthorId]);

  if (!amazonAuthorId) return null;
  if (data === null) return null;
  if (data.libraries.length === 0) {
    // Author has an amazon_id, but the worker has never enqueued or
    // scanned them — likely a brand-new resolution that the worker
    // hasn't observed yet (the cache reader auto-enqueues on miss,
    // so this state usually resolves itself by the next page poll).
    return (
      <div style={containerStyle(t)}>
        <span style={amazonChipStyle()}>amazon</span>
        <span style={{ color: t.textDim, fontSize: 12 }}>
          never scanned — will enqueue on next lookup
        </span>
      </div>
    );
  }

  const cooldownActive = data.cooldown.blocked;

  return (
    <div style={containerStyle(t)}>
      <span style={amazonChipStyle()}>amazon</span>
      <div style={{
        display: "flex", flexDirection: "column", gap: 4,
        fontSize: 12, minWidth: 0,
      }}>
        {data.libraries.map(row => {
          const status = _composeStatusLine(row, cooldownActive);
          const toneColor =
            status.tone === "ok" ? t.ok
            : status.tone === "warn" ? "#cc9933"
            : status.tone === "err" ? t.err
            : t.text2;
          return (
            <div key={row.library_slug} style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
              <span style={{ color: t.textDim, fontSize: 11, fontFamily: "ui-monospace, Consolas, monospace" }}>
                {row.library_slug}
              </span>
              <span style={{ color: toneColor }}>{status.text}</span>
            </div>
          );
        })}
        {cooldownActive && (
          <div style={{ color: t.textDim, fontSize: 11, fontStyle: "italic" }}>
            (worker cooldown engaged — scans paused for {Math.round(data.cooldown.remaining_s)}s)
          </div>
        )}
      </div>
    </div>
  );
}


function containerStyle(t: ReturnType<typeof useTheme>): React.CSSProperties {
  return {
    display: "flex",
    gap: 12,
    alignItems: "flex-start",
    padding: "6px 12px",
    background: t.bg2,
    border: `1px solid ${t.borderL}`,
    borderRadius: 8,
    marginTop: 6,
    minHeight: 28,
  };
}


function amazonChipStyle(): React.CSSProperties {
  return {
    background: AMAZON_BADGE_BG,
    color: AMAZON_BADGE_FG,
    border: `1px solid ${AMAZON_BADGE_BR}`,
    padding: "1px 7px",
    borderRadius: 99,
    fontSize: 10,
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: 0.5,
    flexShrink: 0,
    height: 18,
    display: "inline-flex",
    alignItems: "center",
  };
}
