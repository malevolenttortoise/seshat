// v3.6.0 frontend parity — per-author Goodreads cache status badge.
//
// Sibling to AuthorCacheStatusBadge (which serves Amazon's per-book
// detail cache). GR caches list pages instead of per-book detail
// (ADR-0018 §1, Path B), so this badge surfaces page-count and
// aggregate-book-count instead of the Amazon per-book number.
//
// Renders directly below the SourceBadgeRow on the author detail
// page, alongside the Amazon badge if the author has both source
// IDs. Only mounts when the author has a stored `goodreads_id`
// (otherwise there's nothing to look up). Shows one line per
// library the author exists in:
//
//   "Goodreads (calibre-library): scanned 2h ago — 4 pages / 399 books"
//   "Goodreads (abs-audiobooks): in queue (priority 100)"
//   "Goodreads (calibre-library): never scanned"
//   "Goodreads: scan failed permanently — needs triage"
//   "Goodreads: scanning now"
//
// Polls /api/v1/metadata-cache/goodreads/author/{goodreads_author_id}
// every 30s while mounted. Silent on 404 / 401 / network failures.

import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import {
  cacheBadgeContainerStyle,
  cacheChipStyle,
  formatSecondsAgo,
} from "./cacheBadgeStyles";

type ListPageEntry = {
  page_num: number;
  fetched_at: number;
  book_count: number;
};

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
  list_pages: ListPageEntry[] | null;
};

type AuthorCacheResponse = {
  source: string;
  author_id: string;
  amazon_author_id: string;  // empty for GR
  libraries: LibraryRow[];
  cooldown: { blocked: boolean; remaining_s: number };
};


const POLL_INTERVAL_MS = 30_000;


function _composeStatusLine(row: LibraryRow): {
  text: string; tone: "ok" | "info" | "warn" | "err";
} {
  if (row.queue?.status === "in_progress") {
    return { text: "scanning now", tone: "info" };
  }
  if (row.queue?.status === "failed_permanent") {
    return { text: "scan failed permanently — needs triage", tone: "err" };
  }
  if (row.state) {
    const outcome = row.state.last_outcome;
    const scanned = row.state.last_scanned_at;
    const bookCount = row.state.book_count ?? 0;
    const pages = row.list_pages ?? [];
    if (outcome === "ok" && scanned !== null) {
      const ago = formatSecondsAgo(Date.now() / 1000 - scanned);
      if (pages.length > 0) {
        const totalBookIds = pages.reduce((s, p) => s + p.book_count, 0);
        return {
          text:
            `scanned ${ago} — ${pages.length} page${pages.length === 1 ? "" : "s"}` +
            ` / ${totalBookIds} book${totalBookIds === 1 ? "" : "s"} cached`,
          tone: "ok",
        };
      }
      // State row says ok but no list_pages — fall back to book_count
      // from the state row (older scans may not have populated pages).
      return {
        text: bookCount > 0
          ? `scanned ${ago} — ${bookCount} book${bookCount === 1 ? "" : "s"} cached`
          : `scanned ${ago} — no books returned`,
        tone: "ok",
      };
    }
    if (outcome === "error" && scanned !== null) {
      return {
        text: `last scan errored ${formatSecondsAgo(Date.now() / 1000 - scanned)}`,
        tone: "warn",
      };
    }
  }
  if (row.queue?.status === "pending") {
    const priorityNote = row.queue.priority >= 500
      ? " (priority bumped)"
      : "";
    return { text: `in queue${priorityNote}`, tone: "info" };
  }
  return { text: "never scanned", tone: "info" };
}


export function GoodreadsAuthorCacheStatusBadge({
  goodreadsAuthorId,
}: {
  goodreadsAuthorId: string | null | undefined;
}) {
  const t = useTheme();
  const [data, setData] = useState<AuthorCacheResponse | null>(null);

  useEffect(() => {
    if (!goodreadsAuthorId) {
      setData(null);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;
    const fetchStatus = async () => {
      try {
        const r = await api.get<AuthorCacheResponse>(
          `/v1/metadata-cache/goodreads/author/${encodeURIComponent(goodreadsAuthorId)}`,
        );
        if (!cancelled) setData(r);
      } catch {
        // Silent — could be 401 / 404 / network issue. GR cache may
        // also be in mode=disabled (the worker still returns the
        // status, but with no rows; no special handling needed here).
      }
    };
    fetchStatus();
    timer = setInterval(fetchStatus, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [goodreadsAuthorId]);

  if (!goodreadsAuthorId) return null;
  if (data === null) return null;
  if (data.libraries.length === 0) {
    return (
      <div style={cacheBadgeContainerStyle(t)}>
        <span style={cacheChipStyle("goodreads")}>goodreads</span>
        <span style={{ color: t.textDim, fontSize: 12 }}>
          never scanned — will enqueue on next lookup
        </span>
      </div>
    );
  }

  return (
    <div style={cacheBadgeContainerStyle(t)}>
      <span style={cacheChipStyle("goodreads")}>goodreads</span>
      <div style={{
        display: "flex", flexDirection: "column", gap: 4,
        fontSize: 12, minWidth: 0,
      }}>
        {data.libraries.map(row => {
          const status = _composeStatusLine(row);
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
      </div>
    </div>
  );
}
