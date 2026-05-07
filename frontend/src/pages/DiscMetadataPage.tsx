// v2.3.4 Metadata Manager page.
//
// Four tabs of pending review work:
//   1. Calibre diffs       — review queue rows where source='calibre'.
//   2. ABS diffs           — source='abs'.
//   3. Source-scan diffs   — source IN (goodreads, hardcover, kobo, ibdb, ...).
//   4. Series moves        — pending rows from the legacy
//                            book_series_suggestions table, surfaced here so
//                            the old DiscSuggestionsPage can retire.
//
// Tabs 1-3 share the new metadata_review_queue endpoints. Tab 4 keeps
// hitting the existing /discovery/series-suggestions endpoints —
// retired routes were the goal, not retired tables.
//
// Status filter: pending-only by default; a checkbox surfaces
// applied/ignored history (currently a no-op for tabs 1-3 since the
// queue table has no status column — accept/reject hard-deletes —
// but the contract is in place for when we add soft-delete).

import { useEffect, useMemo, useState } from "react";
import { useTheme } from "../theme";
import { api, ApiError } from "../api";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { Load } from "../components/Load";
import { usePersist } from "../hooks/usePersist";

type TabId = "calibre" | "abs" | "source-scan" | "series-moves";

const TABS: { id: TabId; label: string; sources: string[] | null }[] = [
  { id: "calibre", label: "Calibre", sources: ["calibre"] },
  { id: "abs", label: "Audiobookshelf", sources: ["abs"] },
  {
    id: "source-scan", label: "Source scans",
    sources: ["goodreads", "hardcover", "kobo", "ibdb", "google_books", "amazon", "audible"],
  },
  { id: "series-moves", label: "Series moves", sources: null },
];

interface QueueRow {
  id: number;
  book_id: number;
  field: string;
  old_value: string | null;
  new_value: string | null;
  source: string;
  proposed_at: number;
  book_title: string;
  author_name: string;
}

interface QueueListResponse {
  rows: QueueRow[];
  total: number;
  limit: number;
  offset: number;
}

interface SeriesSuggestion {
  id: number;
  book_id: number;
  book_title: string;
  author_name: string | null;
  current_series_name: string | null;
  current_series_index: number | null;
  suggested_series_name: string | null;
  suggested_series_index: number | null;
  sources_agreeing: string[];
  status: string;
}

const PAGE_SIZE = 50;

export default function DiscMetadataPage() {
  const t = useTheme();
  const [tab, setTab] = usePersist<TabId>("md_tab", "calibre");
  const [showHistory, setShowHistory] = usePersist<boolean>("md_history", false);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div>
        <h1
          style={{
            fontSize: 26,
            fontWeight: 700,
            color: t.text,
            margin: 0,
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <span style={{ fontSize: 22 }}>📋</span> Metadata Manager
        </h1>
        <p style={{ fontSize: 14, color: t.td, marginTop: 4 }}>
          Review per-field metadata diffs the dual-source-of-truth pipeline
          flagged for your attention. Calibre + ABS diffs only appear for
          fields you've manually edited (auto-flow handles the rest); source-
          scan diffs surface when an enrichment source proposes a value that
          conflicts with what's already stored. Series moves surfaces source-
          consensus suggestions for books that look like they belong to a
          series.
        </p>
      </div>

      <div
        style={{
          display: "flex",
          gap: 6,
          borderBottom: `1px solid ${t.borderL}`,
        }}
      >
        {TABS.map((tt) => (
          <button
            key={tt.id}
            onClick={() => setTab(tt.id)}
            style={{
              padding: "10px 16px",
              background: "none",
              border: "none",
              borderBottom:
                tab === tt.id
                  ? `2px solid ${t.accent}`
                  : "2px solid transparent",
              color: tab === tt.id ? t.accent : t.tf,
              fontWeight: tab === tt.id ? 600 : 500,
              fontSize: 14,
              cursor: "pointer",
              marginBottom: -1,
            }}
          >
            {tt.label}
          </button>
        ))}
      </div>

      {/* History filter (currently scaffolding only — applied/ignored
          rows aren't retained for queue tabs since accept/reject
          hard-deletes; series-moves tab honors it for real). */}
      <label
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          cursor: "pointer",
          fontSize: 13,
          color: t.tf,
        }}
      >
        <input
          type="checkbox"
          checked={showHistory}
          onChange={(e) => setShowHistory(e.target.checked)}
        />
        <span>
          Show ignored / applied history{" "}
          <span style={{ color: t.tg, fontStyle: "italic", fontSize: 11 }}>
            (Series moves only — Calibre / ABS / source-scan tabs hard-
            delete on accept/reject)
          </span>
        </span>
      </label>

      {tab === "series-moves" ? (
        <SeriesMovesPanel showHistory={showHistory} />
      ) : (
        <QueuePanel
          tabId={tab}
          sources={TABS.find((tt) => tt.id === tab)!.sources!}
        />
      )}
    </div>
  );
}


// ── Calibre / ABS / source-scan tab ──────────────────────────────────


function QueuePanel({ tabId, sources }: { tabId: TabId; sources: string[] }) {
  const t = useTheme();
  const [data, setData] = useState<QueueListResponse | null>(null);
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  const load = () => {
    setData(null);
    // For tabs that span multiple sources (source-scan), we hit the
    // queue endpoint per source and merge — the endpoint accepts only
    // a single `source` filter. Three concurrent requests are fine
    // for our scale; if the source-scan tab grows it can move
    // server-side later.
    Promise.all(
      sources.map((src) =>
        api
          .get<QueueListResponse>(
            `/discovery/queue?source=${encodeURIComponent(src)}` +
              `&limit=${PAGE_SIZE}&offset=${offset}`,
          )
          .catch(() => ({ rows: [], total: 0, limit: PAGE_SIZE, offset })),
      ),
    ).then((results) => {
      const merged: QueueRow[] = [];
      let total = 0;
      for (const r of results) {
        merged.push(...r.rows);
        total += r.total;
      }
      // Sort by proposed_at desc to give a single coherent feed.
      merged.sort((a, b) => b.proposed_at - a.proposed_at);
      setData({ rows: merged, total, limit: PAGE_SIZE, offset });
    });
  };

  // Reset offset when tab changes; reload otherwise.
  useEffect(() => {
    setOffset(0);
    setSelected(new Set());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabId]);
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabId, offset]);

  const action = async (qid: number, kind: "apply" | "dismiss") => {
    setBusy(qid);
    try {
      await api.post(`/discovery/queue/${qid}/${kind}`);
      load();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`${kind} failed: ${msg}`);
    } finally {
      setBusy(null);
    }
  };

  const bulkAction = async (kind: "apply" | "dismiss") => {
    if (selected.size === 0) return;
    setBulkBusy(true);
    try {
      const res = await api.post<{ succeeded: number; total: number }>(
        "/discovery/queue/bulk",
        { action: kind, ids: Array.from(selected) },
      );
      setSelected(new Set());
      load();
      if (res.succeeded < res.total) {
        alert(
          `${kind}: ${res.succeeded}/${res.total} succeeded (others may have been deleted concurrently)`,
        );
      }
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`bulk ${kind} failed: ${msg}`);
    } finally {
      setBulkBusy(false);
    }
  };

  // Group rows by book — one card per book with each diffing field
  // beneath. Matches the user mental model better than a flat list.
  const groupedByBook = useMemo(() => {
    if (!data) return [];
    const groups = new Map<number, { book: QueueRow; rows: QueueRow[] }>();
    for (const r of data.rows) {
      if (!groups.has(r.book_id)) {
        groups.set(r.book_id, { book: r, rows: [] });
      }
      groups.get(r.book_id)!.rows.push(r);
    }
    return Array.from(groups.values());
  }, [data]);

  if (data === null) return <Load />;

  if (data.rows.length === 0) {
    return (
      <div
        style={{
          background: t.bg2,
          border: `1px solid ${t.border}`,
          borderRadius: 12,
          padding: 40,
          textAlign: "center",
          color: t.tg,
        }}
      >
        <div style={{ fontSize: 32, marginBottom: 8 }}>—</div>
        <div style={{ fontSize: 14 }}>
          No pending diffs from {sources.join(", ")}.
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {selected.size > 0 ? (
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "center",
            padding: "8px 12px",
            background: `${t.accent}15`,
            border: `1px solid ${t.accent}55`,
            borderRadius: 8,
          }}
        >
          <span style={{ fontSize: 13, color: t.text }}>
            {selected.size} selected
          </span>
          <Btn
            variant="accent"
            size="sm"
            onClick={() => bulkAction("apply")}
            disabled={bulkBusy}
          >
            {bulkBusy ? <Spin /> : null} Accept all
          </Btn>
          <Btn
            variant="ghost"
            size="sm"
            onClick={() => bulkAction("dismiss")}
            disabled={bulkBusy}
          >
            Reject all
          </Btn>
          <Btn
            variant="ghost"
            size="sm"
            onClick={() => setSelected(new Set())}
          >
            Clear
          </Btn>
        </div>
      ) : null}

      {groupedByBook.map(({ book, rows }) => (
        <BookCard
          key={book.book_id}
          book={book}
          rows={rows}
          selected={selected}
          onToggle={(qid) =>
            setSelected((s) => {
              const n = new Set(s);
              if (n.has(qid)) n.delete(qid);
              else n.add(qid);
              return n;
            })
          }
          onAction={action}
          busy={busy}
        />
      ))}
    </div>
  );
}


function BookCard({
  book,
  rows,
  selected,
  onToggle,
  onAction,
  busy,
}: {
  book: QueueRow;
  rows: QueueRow[];
  selected: Set<number>;
  onToggle: (qid: number) => void;
  onAction: (qid: number, kind: "apply" | "dismiss") => void;
  busy: number | null;
}) {
  const t = useTheme();
  return (
    <div
      style={{
        background: t.bg2,
        border: `1px solid ${t.border}`,
        borderRadius: 10,
        padding: 14,
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
        }}
      >
        <div>
          <div style={{ fontSize: 15, fontWeight: 600, color: t.text }}>
            {book.book_title}
          </div>
          <div style={{ fontSize: 12, color: t.tf }}>
            by {book.author_name}
          </div>
        </div>
        <div style={{ fontSize: 11, color: t.tg }}>
          {rows.length} pending field{rows.length === 1 ? "" : "s"}
        </div>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {rows.map((r) => (
          <FieldDiff
            key={r.id}
            row={r}
            checked={selected.has(r.id)}
            onToggle={() => onToggle(r.id)}
            onApply={() => onAction(r.id, "apply")}
            onDismiss={() => onAction(r.id, "dismiss")}
            busy={busy === r.id}
          />
        ))}
      </div>
    </div>
  );
}


function FieldDiff({
  row, checked, onToggle, onApply, onDismiss, busy,
}: {
  row: QueueRow;
  checked: boolean;
  onToggle: () => void;
  onApply: () => void;
  onDismiss: () => void;
  busy: boolean;
}) {
  const t = useTheme();
  const fmt = (v: string | null): string => {
    if (v === null || v === undefined || !v.trim()) return "—";
    return v;
  };
  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 12,
        padding: "8px 10px",
        background: t.bg,
        border: `1px solid ${t.borderL}`,
        borderRadius: 6,
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        style={{ marginTop: 4 }}
      />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 4 }}>
        <div style={{ fontSize: 11, color: t.tg, textTransform: "uppercase", letterSpacing: "0.04em" }}>
          {row.field} <span style={{ color: t.tf, marginLeft: 6 }}>via {row.source}</span>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, fontSize: 13 }}>
          <div>
            <div style={{ fontSize: 10, color: t.tg }}>current</div>
            <div style={{ color: t.text2, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
              {fmt(row.old_value)}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 10, color: t.tg }}>proposed</div>
            <div style={{ color: t.text, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
              {fmt(row.new_value)}
            </div>
          </div>
        </div>
      </div>
      <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
        <Btn variant="accent" size="sm" onClick={onApply} disabled={busy}>
          {busy ? <Spin /> : null} Accept
        </Btn>
        <Btn variant="ghost" size="sm" onClick={onDismiss} disabled={busy}>
          Reject
        </Btn>
      </div>
    </div>
  );
}


// ── Series moves tab ─────────────────────────────────────────────────


function SeriesMovesPanel({ showHistory }: { showHistory: boolean }) {
  const t = useTheme();
  const [data, setData] = useState<SeriesSuggestion[] | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const load = () => {
    setData(null);
    const status = showHistory ? "all" : "pending";
    api
      .get<{ suggestions?: SeriesSuggestion[]; rows?: SeriesSuggestion[] }>(
        `/discovery/series-suggestions?status=${status}`,
      )
      .then((r) => setData(r.suggestions || r.rows || []))
      .catch(() => setData([]));
  };

  useEffect(load, [showHistory]);

  const act = async (sug: SeriesSuggestion, action: "apply" | "ignore" | "delete") => {
    setBusy(sug.id);
    try {
      if (action === "delete") {
        await api.del(`/discovery/series-suggestions/${sug.id}`);
      } else {
        await api.post(`/discovery/series-suggestions/${sug.id}/${action}`);
      }
      load();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`${action} failed: ${msg}`);
    } finally {
      setBusy(null);
    }
  };

  if (data === null) return <Load />;

  if (data.length === 0) {
    return (
      <div
        style={{
          background: t.bg2,
          border: `1px solid ${t.border}`,
          borderRadius: 12,
          padding: 40,
          textAlign: "center",
          color: t.tg,
        }}
      >
        <div style={{ fontSize: 32, marginBottom: 8 }}>—</div>
        <div style={{ fontSize: 14 }}>
          {showHistory
            ? "No series-move history yet."
            : "No pending series moves."}
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {data.map((sug) => (
        <div
          key={sug.id}
          style={{
            background: t.bg2,
            border: `1px solid ${t.border}`,
            borderRadius: 10,
            padding: 14,
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          <div>
            <div style={{ fontSize: 15, fontWeight: 600, color: t.text }}>
              {sug.book_title}
            </div>
            <div style={{ fontSize: 12, color: t.tf }}>
              by {sug.author_name || "—"}
            </div>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, fontSize: 13 }}>
            <div>
              <div style={{ fontSize: 10, color: t.tg }}>current</div>
              <div style={{ color: t.text2 }}>
                {sug.current_series_name
                  ? `${sug.current_series_name}${sug.current_series_index ? " #" + sug.current_series_index : ""}`
                  : "standalone"}
              </div>
            </div>
            <div>
              <div style={{ fontSize: 10, color: t.tg }}>proposed</div>
              <div style={{ color: t.text }}>
                {sug.suggested_series_name
                  ? `${sug.suggested_series_name}${sug.suggested_series_index ? " #" + sug.suggested_series_index : ""}`
                  : "standalone"}
              </div>
            </div>
          </div>
          <div style={{ fontSize: 11, color: t.tg }}>
            consensus: {sug.sources_agreeing.join(", ")}
          </div>
          <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
            {sug.status === "pending" ? (
              <>
                <Btn
                  variant="accent"
                  size="sm"
                  onClick={() => act(sug, "apply")}
                  disabled={busy === sug.id}
                >
                  {busy === sug.id ? <Spin /> : null} Accept
                </Btn>
                <Btn
                  variant="ghost"
                  size="sm"
                  onClick={() => act(sug, "ignore")}
                  disabled={busy === sug.id}
                >
                  Ignore
                </Btn>
              </>
            ) : (
              <span style={{ fontSize: 11, color: t.tg, fontStyle: "italic" }}>
                {sug.status}
              </span>
            )}
            <Btn
              variant="ghost"
              size="sm"
              onClick={() => act(sug, "delete")}
              disabled={busy === sug.id}
            >
              Delete
            </Btn>
          </div>
        </div>
      ))}
    </div>
  );
}
