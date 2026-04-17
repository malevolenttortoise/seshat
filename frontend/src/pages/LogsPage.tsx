// LogsPage — live log viewer with All / Announces tab filter.
//
// Reads from the in-memory log buffer via /api/v1/logs. Two tabs:
//   - All: full application log (dispatcher, budget, IRC, pipeline)
//   - Announces: only IRC announce events + dispatcher decisions
//
// Auto-refreshes every 5 seconds while the tab is visible. Pauses
// when the user scrolls up (reading older entries) to avoid jumping.
import { useEffect, useRef, useState } from "react";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { api } from "../api";
import { useTheme } from "../theme";
import { useVisibleInterval } from "../hooks/useVisibleInterval";

interface LogEntry {
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

// Tab set mirrors the backend category query param +
// existing "announces" pseudo-category. "application" and "irc"
// slice by logger-name prefix (everything not under
// `seshat.mam.irc` vs everything under it).
type Tab = "all" | "announces" | "application" | "irc" | "scans";

export default function LogsPage() {
  const theme = useTheme();
  const [tab, setTab] = useState<Tab>("all");
  const [entries, setEntries] = useState<LogEntry[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  // Client-side filter — narrows the visible rows in real time
  // without re-querying the backend. Case-insensitive substring
  // match against logger + message.
  const [filter, setFilter] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  async function load() {
    try {
      // 2000 lines balances "enough history to actually be useful"
      // against "render fast on slower machines." The backend ring
      // buffer holds 20000 records; a user who needs more can
      // query /api/v1/logs?lines=... directly.
      const params = new URLSearchParams({ lines: "2000" });
      // "announces" maps to the existing is_announce pseudo-filter;
      // "application" / "irc" map to the backend's category query
      // param which slices by logger-name prefix.
      if (tab === "announces") params.set("filter", "announces");
      else if (tab === "application") params.set("category", "application");
      else if (tab === "irc") params.set("category", "irc");
      else if (tab === "scans") params.set("category", "scans");
      const r = await api.get<LogsResponse>(`/v1/logs?${params}`);
      setEntries(r.entries);
      setTotal(r.total_buffered);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => { load(); }, [tab]);
  // useVisibleInterval handles document.hidden internally; only the
  // autoScroll gate stays in the closure here.
  useVisibleInterval(() => { if (autoScroll) load(); }, 5000);

  const levelColor = (level: string) => {
    switch (level) {
      case "ERROR":
        return theme.err;
      case "WARNING":
        return theme.warn;
      case "DEBUG":
        return theme.textDim;
      default:
        return theme.text2;
    }
  };

  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 16,
        }}
      >
        <h1 style={{ fontSize: 24, fontWeight: 700, color: theme.text }}>
          Logs
        </h1>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter…"
            style={{
              padding: "6px 10px",
              fontSize: 12,
              background: theme.bg2,
              border: `1px solid ${theme.borderL}`,
              borderRadius: 6,
              color: theme.text2,
              minWidth: 180,
              fontFamily: "inherit",
            }}
          />
          <span style={{ fontSize: 12, color: theme.textDim }}>
            {total} buffered
          </span>
          <Btn variant="ghost" onClick={load}>
            Refresh
          </Btn>
        </div>
      </div>

      {error && (
        <div
          style={{
            background: theme.err + "22",
            border: `1px solid ${theme.err}55`,
            color: theme.err,
            padding: "10px 14px",
            borderRadius: 8,
            fontSize: 13,
            marginBottom: 16,
          }}
        >
          {error}
        </div>
      )}

      <div
        style={{
          display: "flex",
          gap: 4,
          marginBottom: 12,
          borderBottom: `1px solid ${theme.borderL}`,
        }}
      >
        {(["all", "application", "irc", "announces", "scans"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => {
              setTab(t);
              setEntries(null);
            }}
            style={{
              background: "transparent",
              border: "none",
              borderBottom: `2px solid ${t === tab ? theme.accent : "transparent"}`,
              color: t === tab ? theme.accent : theme.text2,
              padding: "10px 16px",
              fontSize: 14,
              fontWeight: 600,
              cursor: "pointer",
              marginBottom: -1,
              textTransform: "capitalize",
            }}
          >
            {t === "all" ? "All Logs"
              : t === "application" ? "Application"
              : t === "irc" ? "IRC"
              : t === "announces" ? "Announces"
              : "Scans"}
          </button>
        ))}
      </div>

      {entries === null ? (
        <div style={{ display: "flex", justifyContent: "center", padding: 40 }}>
          <Spin />
        </div>
      ) : entries.length === 0 ? (
        <p style={{ color: theme.textDim, fontSize: 13 }}>No log entries yet.</p>
      ) : (() => {
        const q = filter.trim().toLowerCase();
        const visible = q
          ? entries.filter(
              (e) =>
                e.message.toLowerCase().includes(q) ||
                (e.logger || "").toLowerCase().includes(q),
            )
          : entries;
        if (visible.length === 0) {
          return (
            <p style={{ color: theme.textDim, fontSize: 13 }}>
              No entries match <code>{filter}</code>. {entries.length} hidden.
            </p>
          );
        }
        return (
        <div
          style={{
            background: theme.bg2,
            border: `1px solid ${theme.borderL}`,
            borderRadius: 8,
            padding: 12,
            maxHeight: "70vh",
            overflowY: "auto",
            fontFamily:
              "ui-monospace, SFMono-Regular, Consolas, 'Liberation Mono', monospace",
            fontSize: 12,
            lineHeight: 1.6,
          }}
          onScroll={(e) => {
            const el = e.currentTarget;
            const nearBottom =
              el.scrollHeight - el.scrollTop - el.clientHeight < 40;
            setAutoScroll(nearBottom);
          }}
        >
          {visible.map((entry, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                gap: 8,
                padding: "2px 0",
                borderBottom:
                  i < visible.length - 1
                    ? `1px solid ${theme.borderL}`
                    : "none",
              }}
            >
              <span style={{ color: theme.textDim, flexShrink: 0, width: 150 }}>
                {entry.ts}
              </span>
              <span
                style={{
                  color: levelColor(entry.level),
                  flexShrink: 0,
                  width: 55,
                  fontWeight: 700,
                }}
              >
                {entry.level}
              </span>
              <span style={{ color: theme.text2, wordBreak: "break-word" }}>
                {entry.message}
              </span>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
        );
      })()}
    </div>
  );
}
