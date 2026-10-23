// The Logs page's feed: log lines by category, or the Announces audit
// (the dispatcher's decision on every IRC announce), reloaded every 5s
// while auto-scroll is on.
//
// LogsPage + MobileLogsPage each carried this (wave 5b S10). The shells
// keep the rendering: tabs, level colours, the scroll-to-bottom, and the
// text filter, which narrows log lines on screen and is sent to the
// server for the Announces tab.
import { useEffect, useState } from "react";
import { api } from "../api";
import { useVisibleInterval } from "./useVisibleInterval";

export interface LogEntry {
  ts: string;
  level: string;
  logger: string;
  message: string;
  is_announce: boolean;
}

interface LogsResponse {
  entries: LogEntry[];
  total_buffered: number;
}

export interface AnnounceRow {
  id: number;
  seen_at: string;
  torrent_name: string;
  author_blob: string;
  category: string;
  filetype: string;
  decision: string;
  decision_reason: string;
  matched_author: string;
  // Every MAM content tag the announce carried (wave 5a).
  categories?: string[] | null;
}

export interface AnnouncesResponse {
  rows: AnnounceRow[];
  total_matched: number;
  decision_counts: Record<string, number>;
}

export type DecisionFilter = "all" | "allow" | "skip" | "hold";

export type LogTab = "all" | "announces" | "application" | "irc" | "scans";

export function useLogFeed() {
  const [tab, setTab] = useState<LogTab>("all");
  const [entries, setEntries] = useState<LogEntry[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  // Narrows the visible log lines in real time (case-insensitive, logger
  // + message); for Announces it's also sent as `q`.
  const [filter, setFilter] = useState("");
  // v2.9.0 Announce Log: a separate shape, because the Announces tab
  // swaps data sources.
  const [announces, setAnnounces] = useState<AnnouncesResponse | null>(null);
  const [decisionFilter, setDecisionFilter] = useState<DecisionFilter>("all");

  async function load() {
    try {
      if (tab === "announces") {
        // Structured decisions from the v2.9.0 audit endpoint. The text
        // filter and the decision chip both narrow server-side so
        // dedup-skipped rows surface immediately.
        const params = new URLSearchParams({ limit: "500" });
        if (decisionFilter !== "all") params.set("decision", decisionFilter);
        if (filter.trim()) params.set("q", filter.trim());
        const r = await api.get<AnnouncesResponse>(`/v1/announces?${params}`);
        setAnnounces(r);
        setEntries([]); // hide the log-line code path
        setTotal(r.total_matched);
        setError(null);
        return;
      }
      // 2000 lines balances "enough history to actually be useful"
      // against "render fast on slower machines." The backend ring
      // buffer holds 20000 records; a user who needs more can query
      // /api/v1/logs?lines=... directly.
      const params = new URLSearchParams({ lines: "2000" });
      // "application" / "irc" / "scans" map to the backend's category
      // query param, which slices by logger-name prefix.
      if (tab === "application") params.set("category", "application");
      else if (tab === "irc") params.set("category", "irc");
      else if (tab === "scans") params.set("category", "scans");
      const r = await api.get<LogsResponse>(`/v1/logs?${params}`);
      setEntries(r.entries);
      setAnnounces(null);
      setTotal(r.total_buffered);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [tab, decisionFilter]);
  // For the Announces tab the text filter is server-side, so re-query
  // when it changes too (debounced lightly).
  useEffect(() => {
    if (tab !== "announces") return;
    const t = setTimeout(load, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, tab]);
  // useVisibleInterval pauses while the tab is hidden; auto-scroll off
  // freezes the feed.
  useVisibleInterval(() => { if (autoScroll) load(); }, 5000);

  /** Show "loading" until the next load lands (desktop, on a tab click). */
  const clearEntries = () => setEntries(null);

  return {
    tab, setTab, entries, clearEntries, total, error, autoScroll, setAutoScroll, filter, setFilter,
    announces, decisionFilter, setDecisionFilter, load,
  };
}
