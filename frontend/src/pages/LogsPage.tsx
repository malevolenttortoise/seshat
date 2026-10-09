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
import { CategoryChips } from "../components/CategoryChips";
import { Spin } from "../components/Spin";
import { api } from "../api";
import { useTheme } from "../theme";
import { useVisibleInterval } from "../hooks/useVisibleInterval";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import MobileLogsPage from "./MobileLogsPage";

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

// v2.9.0 Announce Log: structured rows from the SQLite announces table.
// Distinct from the in-memory log buffer above. The "Announces" tab
// renders these instead of LogEntry rows so format-dedup decisions
// (hold / skip with reason) are filter-able + glanceable.
interface AnnounceRow {
  id: number;
  seen_at: string;
  torrent_name: string;
  author_blob: string;
  category: string;
  filetype: string;
  decision: string;
  decision_reason: string;
  matched_author: string;
  // Every MAM content tag the announce carried (wave 5a); null when
  // only `category` is known.
  categories?: string[] | null;
}

interface AnnouncesResponse {
  rows: AnnounceRow[];
  total_matched: number;
  decision_counts: Record<string, number>;
}

type DecisionFilter = "all" | "allow" | "skip" | "hold";

// Tab set mirrors the backend category query param +
// existing "announces" pseudo-category. "application" and "irc"
// slice by logger-name prefix (everything not under
// `seshat.mam.irc` vs everything under it).
type Tab = "all" | "announces" | "application" | "irc" | "scans";

export default function LogsPage() {
  const vp = useViewport();
  if (useMobileCodepath(vp)) return <MobileLogsPage />;
  return <DesktopLogsPage />;
}

function DesktopLogsPage() {
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
  // v2.9.0 Announce Log: separate state shape, kept in parallel
  // because the "Announces" tab swaps data sources.
  const [announces, setAnnounces] = useState<AnnouncesResponse | null>(null);
  const [decisionFilter, setDecisionFilter] = useState<DecisionFilter>("all");
  const bottomRef = useRef<HTMLDivElement>(null);

  async function load() {
    try {
      if (tab === "announces") {
        // Pull structured decisions from the v2.9.0 audit endpoint.
        // The substring filter input + decision chip both narrow
        // server-side so dedup-skipped rows surface immediately.
        const params = new URLSearchParams({ limit: "500" });
        if (decisionFilter !== "all") {
          params.set("decision", decisionFilter);
        }
        if (filter.trim()) params.set("q", filter.trim());
        const r = await api.get<AnnouncesResponse>(
          `/v1/announces?${params}`,
        );
        setAnnounces(r);
        setEntries([]);  // hide the log-line code path
        setTotal(r.total_matched);
        setError(null);
        return;
      }
      // 2000 lines balances "enough history to actually be useful"
      // against "render fast on slower machines." The backend ring
      // buffer holds 20000 records; a user who needs more can
      // query /api/v1/logs?lines=... directly.
      const params = new URLSearchParams({ lines: "2000" });
      // "application" / "irc" map to the backend's category query
      // param which slices by logger-name prefix.
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

  useEffect(() => { load(); }, [tab, decisionFilter]);
  // For the Announces tab the substring filter is server-side, so
  // re-query on input change too (debounced lightly via a timeout).
  useEffect(() => {
    if (tab !== "announces") return;
    const t = setTimeout(load, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, tab]);
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

      {tab === "announces" && (
        <DecisionChips
          counts={announces?.decision_counts ?? {}}
          current={decisionFilter}
          onChange={setDecisionFilter}
        />
      )}

      {tab === "announces" ? (
        announces === null ? (
          <div style={{ display: "flex", justifyContent: "center", padding: 40 }}>
            <Spin />
          </div>
        ) : announces.rows.length === 0 ? (
          <p style={{ color: theme.textDim, fontSize: 13 }}>
            No announces match {filter ? <code>{filter}</code> : "the current filters"}.
          </p>
        ) : (
          <AnnouncesTable rows={announces.rows} />
        )
      ) : entries === null ? (
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


// v2.9.0 Announce Log: decision filter chips. Counts come from the
// /v1/announces endpoint's `decision_counts` which honors the q/reason
// filters but NOT the decision filter itself — so each chip shows
// "what would be visible if I clicked this", which is the right UX
// for chip pickers.
function DecisionChips({
  counts,
  current,
  onChange,
}: {
  counts: Record<string, number>;
  current: DecisionFilter;
  onChange: (d: DecisionFilter) => void;
}) {
  const theme = useTheme();
  const total =
    (counts.allow ?? 0) + (counts.skip ?? 0) + (counts.hold ?? 0);
  const options: { value: DecisionFilter; label: string; n: number; tone: string }[] = [
    { value: "all", label: "All", n: total, tone: theme.text2 },
    { value: "allow", label: "Allow", n: counts.allow ?? 0, tone: theme.grn },
    { value: "skip", label: "Skip", n: counts.skip ?? 0, tone: theme.red },
    { value: "hold", label: "Hold", n: counts.hold ?? 0, tone: theme.warn },
  ];
  return (
    <div style={{ display: "flex", gap: 6, marginBottom: 10, flexWrap: "wrap" }}>
      {options.map((o) => {
        const active = current === o.value;
        return (
          <button
            key={o.value}
            onClick={() => onChange(o.value)}
            style={{
              padding: "5px 12px",
              borderRadius: 999,
              border: `1px solid ${active ? o.tone : theme.borderL}`,
              background: active ? o.tone + "22" : "transparent",
              color: active ? o.tone : theme.text2,
              fontSize: 12,
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            {o.label}{" "}
            <span
              style={{
                color: active ? o.tone : theme.textDim,
                fontWeight: 500,
              }}
            >
              ({o.n})
            </span>
          </button>
        );
      })}
    </div>
  );
}


function AnnouncesTable({ rows }: { rows: AnnounceRow[] }) {
  const theme = useTheme();
  const decisionStyle = (decision: string) => {
    switch (decision) {
      case "allow":
        return { bg: theme.grn + "22", fg: theme.grn };
      case "skip":
        return { bg: theme.red + "22", fg: theme.red };
      case "hold":
        return { bg: theme.warn + "22", fg: theme.warn };
      default:
        return { bg: theme.bg3, fg: theme.text2 };
    }
  };
  return (
    <div
      style={{
        background: theme.bg2,
        border: `1px solid ${theme.borderL}`,
        borderRadius: 8,
        maxHeight: "70vh",
        overflowY: "auto",
        fontSize: 12,
      }}
    >
      {rows.map((row, i) => {
        const ds = decisionStyle(row.decision);
        return (
          <div
            key={row.id}
            style={{
              display: "grid",
              gridTemplateColumns: "150px 60px 70px 1fr 1fr 1.5fr",
              gap: 10,
              padding: "8px 12px",
              borderBottom:
                i < rows.length - 1 ? `1px solid ${theme.borderL}` : "none",
              alignItems: "center",
            }}
          >
            <span style={{ color: theme.textDim, fontFamily: "ui-monospace" }}>
              {row.seen_at}
            </span>
            <span
              style={{
                display: "inline-block",
                textAlign: "center",
                padding: "2px 6px",
                borderRadius: 4,
                background: ds.bg,
                color: ds.fg,
                fontWeight: 700,
                textTransform: "uppercase",
                letterSpacing: 0.4,
                fontSize: 11,
              }}
            >
              {row.decision}
            </span>
            <span
              style={{
                color: theme.textDim,
                fontFamily: "ui-monospace",
                textTransform: "uppercase",
              }}
            >
              {row.filetype || "—"}
            </span>
            <span style={{ color: theme.text2, fontWeight: 500 }}>
              {row.torrent_name || "(no name)"}
              {row.categories && row.categories.length > 0 && (
                <div style={{ marginTop: 3 }}><CategoryChips categories={row.categories} /></div>
              )}
            </span>
            <span style={{ color: theme.textDim }}>
              {row.author_blob || "—"}
            </span>
            <span
              style={{
                color: theme.textDim,
                fontFamily: "ui-monospace",
                fontSize: 11,
              }}
              title={row.category}
            >
              {row.decision_reason}
            </span>
          </div>
        );
      })}
    </div>
  );
}
