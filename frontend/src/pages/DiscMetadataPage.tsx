// v2.3.5 Metadata Manager page.
//
// Five tabs:
//   1. Calibre diffs        — review queue rows where source='calibre'.
//   2. ABS diffs            — source='abs'.
//   3. Source-scan diffs    — source IN (goodreads, hardcover, kobo, ibdb, ...).
//   4. Series moves         — pending rows from the legacy
//                             book_series_suggestions table, surfaced here so
//                             the old DiscSuggestionsPage can retire.
//   5. Pending manual edits — books with non-empty user_edited_fields
//                             (v2.3.5). The "what edits do I have pending
//                             push-back?" surface that doesn't fit the
//                             incoming-review-queue model.
//
// Tabs 1-3 share the metadata_review_queue endpoints. Tab 4 keeps
// hitting the existing /discovery/series-suggestions endpoints. Tab 5
// hits /discovery/pending-edits which synthesizes a cross-library view
// from the books table.
//
// Status filter: pending-only by default; a checkbox surfaces
// applied/ignored history (currently a no-op for tabs 1-3 since the
// queue table has no status column — accept/reject hard-deletes —
// but the contract is in place for when we add soft-delete).

import { useEffect, useMemo, useState } from "react";
import { useTheme } from "../theme";
import { api, ApiError, slugQuery } from "../api";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { Load } from "../components/Load";
import { CompareModal } from "../components/CompareModal";
import { toast } from "../lib/toast";
import { usePersist } from "../hooks/usePersist";

type TabId =
  | "calibre"
  | "abs"
  | "source-scan"
  | "series-moves"
  | "pending-edits";

const TABS: { id: TabId; label: string; sources: string[] | null }[] = [
  { id: "calibre", label: "Calibre", sources: ["calibre"] },
  { id: "abs", label: "Audiobookshelf", sources: ["abs"] },
  {
    id: "source-scan", label: "Source scans",
    sources: ["goodreads", "hardcover", "kobo", "ibdb", "google_books", "amazon", "audible"],
  },
  { id: "series-moves", label: "Series moves", sources: null },
  { id: "pending-edits", label: "Pending manual edits", sources: null },
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

// v3.3.0 slice 03 (ADR-0017 §7) — field-type filter chip taxonomy for
// the 3 queue-shaped tabs (Calibre / Audiobookshelf / Source scans).
// Order = display order; "Authors" first because it's the newest +
// highest-semantic-weight category, "Description" next as typically
// the largest pile. Series moves + Pending manual edits tabs use
// different data shapes and don't carry these chips.
const FIELD_ORDER = [
  "authors",
  "description",
  "isbn",
  "cover_url",
  "pub_date",
  "expected_date",
  "page_count",
] as const;
const FIELD_LABEL: Record<string, string> = {
  authors: "Authors",
  description: "Description",
  isbn: "ISBN",
  cover_url: "Cover",
  pub_date: "Pub date",
  expected_date: "Expected date",
  page_count: "Page count",
};

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
          series. Pending manual edits lists books you've edited locally
          but haven't yet pushed back to Calibre / ABS — push them upstream
          or pull the original value back to abandon the edit.
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
      ) : tab === "pending-edits" ? (
        <PendingEditsPanel />
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
  // v3.3.0 slice 03 (ADR-0017 §7) — field-type filter chip state.
  // Empty set = show all (same effect as all-active). OR-within-the-set
  // multi-select: clicking a chip toggles it; visible rows = union of
  // selected field-types. Local useState (not usePersist) — momentary
  // review focus, not a sticky preference. Resets on tab change below.
  const [activeFields, setActiveFields] = useState<Set<string>>(new Set());

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
    setActiveFields(new Set());
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

  // v3.3.0 slice 03 — per-chip counts derive from the FULL fetched list
  // (not filtered), so chip counts stay stable as the operator filters.
  // Disabled chips (count=0) are still rendered so the chip set is
  // predictable across loads.
  const fieldCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    if (!data) return counts;
    for (const r of data.rows) {
      counts[r.field] = (counts[r.field] ?? 0) + 1;
    }
    return counts;
  }, [data]);

  // Filtered rows = data.rows if no chip active, else OR-within the
  // active field-types.
  const filteredRows = useMemo(() => {
    if (!data) return [] as QueueRow[];
    if (activeFields.size === 0) return data.rows;
    return data.rows.filter((r) => activeFields.has(r.field));
  }, [data, activeFields]);

  // Group rows by book — one card per book with each diffing field
  // beneath. Matches the user mental model better than a flat list.
  const groupedByBook = useMemo(() => {
    const groups = new Map<number, { book: QueueRow; rows: QueueRow[] }>();
    for (const r of filteredRows) {
      if (!groups.has(r.book_id)) {
        groups.set(r.book_id, { book: r, rows: [] });
      }
      groups.get(r.book_id)!.rows.push(r);
    }
    return Array.from(groups.values());
  }, [filteredRows]);

  const toggleField = (f: string) =>
    setActiveFields((s) => {
      const n = new Set(s);
      if (n.has(f)) n.delete(f);
      else n.add(f);
      return n;
    });

  if (data === null) return <Load />;

  // No rows from the backend at all (nothing to review on this tab).
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
      {/* v3.3.0 slice 03 — field-type chip filter row. */}
      <FieldChipRow
        t={t}
        counts={fieldCounts}
        active={activeFields}
        onToggle={toggleField}
        onClear={() => setActiveFields(new Set())}
      />

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

      {/* Filter-aware empty state: distinguishes "no rows at all" (handled
          above) from "no rows match the active filter" so the operator
          isn't stuck wondering why the list is empty after they filtered
          themselves into a corner. */}
      {groupedByBook.length === 0 && activeFields.size > 0 ? (
        <div
          style={{
            background: t.bg2,
            border: `1px solid ${t.border}`,
            borderRadius: 12,
            padding: 30,
            textAlign: "center",
            color: t.tg,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 12,
          }}
        >
          <div style={{ fontSize: 14 }}>
            No rows match the active filter.
          </div>
          <Btn
            variant="ghost"
            size="sm"
            onClick={() => setActiveFields(new Set())}
          >
            Clear filter
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


// ── Field-type chip filter row (v3.3.0 / ADR-0017 §7) ────────────────


function FieldChipRow({
  t, counts, active, onToggle, onClear,
}: {
  t: ReturnType<typeof useTheme>;
  counts: Record<string, number>;
  active: Set<string>;
  onToggle: (f: string) => void;
  onClear: () => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 6,
        alignItems: "center",
      }}
    >
      {FIELD_ORDER.map((f) => {
        const count = counts[f] ?? 0;
        const isActive = active.has(f);
        const disabled = count === 0;
        return (
          <button
            key={f}
            onClick={() => !disabled && onToggle(f)}
            disabled={disabled}
            style={{
              padding: "4px 10px",
              borderRadius: 14,
              fontSize: 12,
              fontWeight: isActive ? 600 : 400,
              cursor: disabled ? "default" : "pointer",
              opacity: disabled ? 0.45 : 1,
              background: isActive ? t.grnb : t.bg,
              border: `1px solid ${isActive ? t.grnt : t.border}`,
              color: t.text,
            }}
          >
            {FIELD_LABEL[f] ?? f}{" "}
            <span style={{ color: t.tg, marginLeft: 2 }}>({count})</span>
          </button>
        );
      })}
      {active.size > 0 ? (
        <button
          onClick={onClear}
          style={{
            padding: "4px 10px",
            borderRadius: 14,
            fontSize: 12,
            fontWeight: 500,
            cursor: "pointer",
            background: t.bg,
            border: `1px solid ${t.border}`,
            color: t.tf,
            marginLeft: 6,
          }}
        >
          Clear ({active.size})
        </button>
      ) : null}
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
  // v3.3.0 (ADR-0017 §4) — authors proposed-changes carry a JSON
  // payload of `{name, source_id?}` records; render as a list-diff
  // (added markers, primary-position markers) instead of scalar text.
  const isAuthors = row.field === "authors";
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
        {isAuthors ? (
          <AuthorsDiff t={t} oldValue={row.old_value} newValue={row.new_value} />
        ) : (
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
        )}
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


// ── Authors list-diff variant (v3.3.0 / ADR-0017 §4) ─────────────────


type AuthorRec = { name: string; source_id?: string | null };

function _parseAuthors(raw: string | null): AuthorRec[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((r) => r && typeof r === "object" && typeof r.name === "string")
      .map((r) => ({ name: r.name, source_id: r.source_id ?? null }));
  } catch {
    return [];
  }
}

function AuthorsDiff({
  t, oldValue, newValue,
}: { t: ReturnType<typeof useTheme>; oldValue: string | null; newValue: string | null }) {
  const oldRecs = _parseAuthors(oldValue);
  const newRecs = _parseAuthors(newValue);
  // Normalized-name set membership (matches the backend's
  // `_norm_author_name` in lookup.py) so "Smith" vs " smith " stays
  // one entry visually.
  const norm = (n: string) => (n || "").trim().toLowerCase();
  const oldSet = new Set(oldRecs.map((r) => norm(r.name)));
  const oldPrimary = oldRecs[0]?.name ?? null;
  const newPrimary = newRecs[0]?.name ?? null;
  const primaryChanged =
    !!newPrimary && !!oldPrimary && norm(newPrimary) !== norm(oldPrimary);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, fontSize: 13 }}>
      <div>
        <div style={{ fontSize: 10, color: t.tg }}>current</div>
        <div style={{ color: t.text2, display: "flex", flexDirection: "column", gap: 2 }}>
          {oldRecs.length === 0 ? (
            <span>—</span>
          ) : (
            oldRecs.map((r, i) => (
              <span key={`old-${i}-${r.name}`}>
                {r.name}
                {primaryChanged && i === 0 ? (
                  <span style={{ color: t.tg, marginLeft: 6, fontSize: 11 }}>
                    ← was primary
                  </span>
                ) : null}
              </span>
            ))
          )}
        </div>
      </div>
      <div>
        <div style={{ fontSize: 10, color: t.tg }}>proposed</div>
        <div style={{ color: t.text, display: "flex", flexDirection: "column", gap: 2 }}>
          {newRecs.length === 0 ? (
            <span>—</span>
          ) : (
            newRecs.map((r, i) => {
              const isNew = !oldSet.has(norm(r.name));
              return (
                <span key={`new-${i}-${r.name}`}>
                  <span
                    style={
                      isNew
                        ? {
                            background: t.grnb,
                            border: `1px solid ${t.grnt}`,
                            borderRadius: 3,
                            padding: "1px 5px",
                          }
                        : undefined
                    }
                  >
                    {r.name}
                  </span>
                  {isNew ? (
                    <span style={{ color: t.grnt, marginLeft: 6, fontSize: 11 }}>
                      (new)
                    </span>
                  ) : null}
                  {primaryChanged && i === 0 ? (
                    <span style={{ color: t.tg, marginLeft: 6, fontSize: 11 }}>
                      ← primary
                    </span>
                  ) : null}
                </span>
              );
            })
          )}
        </div>
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


// ── Pending manual edits tab (v2.3.5) ───────────────────────────────


interface PendingEditRow {
  book_id: number;
  title: string;
  author_name: string | null;
  library_slug: string | null;
  library_name: string | null;
  fields: string[];
  has_calibre_snapshot: boolean;
  has_abs_snapshot: boolean;
  calibre_synced_at: number | null;
  abs_synced_at: number | null;
  calibre_id: number | null;
  audiobookshelf_id: string | null;
}

interface PendingEditsResponse {
  rows: PendingEditRow[];
  total: number;
  limit: number;
  offset: number;
}

function PendingEditsPanel() {
  const t = useTheme();
  const [data, setData] = useState<PendingEditsResponse | null>(null);
  const [busy, setBusy] = useState<string>(""); // `${book_id}|${verb}|${source}`
  const [compareFor, setCompareFor] = useState<PendingEditRow | null>(null);

  const load = () => {
    setData(null);
    api
      .get<PendingEditsResponse>("/discovery/pending-edits?limit=200")
      .then(setData)
      .catch(() => setData({ rows: [], total: 0, limit: 200, offset: 0 }));
  };

  useEffect(load, []);

  const bulkAction = async (
    row: PendingEditRow,
    verb: "push" | "pull",
    source: "calibre" | "abs",
  ) => {
    const key = `${row.book_id}|${verb}|${source}`;
    setBusy(key);
    try {
      const r = await api.post<{ applied: string[] }>(
        `/discovery/books/${row.book_id}/${verb}${slugQuery(row.library_slug ?? undefined)}`,
        { source, all_user_edited: true },
      );
      const n = r.applied?.length ?? 0;
      const sourceLabel = source === "calibre" ? "Calibre" : "ABS";
      const direction = verb === "push" ? "to" : "from";
      if (n === 0) {
        toast.info(`No fields applicable for ${verb} ${direction} ${sourceLabel}.`);
      } else {
        toast.success(
          `${verb === "push" ? "Pushed" : "Pulled"} ${n} field${n === 1 ? "" : "s"} ${direction} ${sourceLabel}`,
        );
      }
      load();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      toast.error(`${verb} ${verb === "push" ? "to" : "from"} ${source}: ${msg}`);
    } finally {
      setBusy("");
    }
  };

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
          No pending manual edits. Edits made in the book sidebar
          appear here until you push them upstream or pull the
          original value back.
        </div>
      </div>
    );
  }

  return (
    <>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ fontSize: 12, color: t.tg }}>
          {data.total} book{data.total === 1 ? "" : "s"} with pending
          edits across all libraries.
        </div>
        {data.rows.map((row) => (
          <PendingEditCard
            key={`${row.library_slug}:${row.book_id}`}
            row={row}
            onCompare={() => setCompareFor(row)}
            onAction={(verb, source) => bulkAction(row, verb, source)}
            busy={busy}
          />
        ))}
      </div>
      {compareFor ? (
        <CompareModal
          bookId={compareFor.book_id}
          bookTitle={compareFor.title}
          librarySlug={compareFor.library_slug ?? undefined}
          onClose={() => setCompareFor(null)}
          onChanged={load}
        />
      ) : null}
    </>
  );
}


function PendingEditCard({
  row,
  onCompare,
  onAction,
  busy,
}: {
  row: PendingEditRow;
  onCompare: () => void;
  onAction: (verb: "push" | "pull", source: "calibre" | "abs") => void;
  busy: string;
}) {
  const t = useTheme();
  const isBusy = (verb: string, source: string) =>
    busy === `${row.book_id}|${verb}|${source}`;
  const anyBusy = busy.startsWith(`${row.book_id}|`);

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
          gap: 12,
          flexWrap: "wrap",
        }}
      >
        <div>
          <div style={{ fontSize: 15, fontWeight: 600, color: t.text }}>
            {row.title}
          </div>
          <div style={{ fontSize: 12, color: t.tf }}>
            by {row.author_name || "—"}
            {row.library_name ? (
              <span style={{ color: t.tg }}> · {row.library_name}</span>
            ) : null}
          </div>
        </div>
        <div style={{ fontSize: 11, color: t.tg }}>
          {row.fields.length} edited field
          {row.fields.length === 1 ? "" : "s"}
        </div>
      </div>

      {/* Field chips */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {row.fields.map((f) => (
          <span
            key={f}
            style={{
              fontSize: 11,
              padding: "3px 8px",
              borderRadius: 999,
              background: `${t.accent}15`,
              border: `1px solid ${t.accent}55`,
              color: t.text2,
              fontFamily: "monospace",
            }}
          >
            {f}
          </span>
        ))}
      </div>

      {/* Actions */}
      <div
        style={{
          display: "flex",
          gap: 6,
          flexWrap: "wrap",
          paddingTop: 8,
          borderTop: `1px solid ${t.borderL}`,
        }}
      >
        <Btn variant="ghost" size="sm" onClick={onCompare} disabled={anyBusy}>
          Compare…
        </Btn>
        <div style={{ flex: 1 }} />
        {row.has_calibre_snapshot ? (
          <>
            <Btn
              variant="ghost"
              size="sm"
              onClick={() => onAction("push", "calibre")}
              disabled={anyBusy}
            >
              {isBusy("push", "calibre") ? <Spin /> : null} → Push to Calibre
            </Btn>
            <Btn
              variant="ghost"
              size="sm"
              onClick={() => onAction("pull", "calibre")}
              disabled={anyBusy}
            >
              {isBusy("pull", "calibre") ? <Spin /> : null} ← Pull from Calibre
            </Btn>
          </>
        ) : null}
        {row.has_abs_snapshot ? (
          <>
            <Btn
              variant="ghost"
              size="sm"
              onClick={() => onAction("push", "abs")}
              disabled={anyBusy}
            >
              {isBusy("push", "abs") ? <Spin /> : null} → Push to ABS
            </Btn>
            <Btn
              variant="ghost"
              size="sm"
              onClick={() => onAction("pull", "abs")}
              disabled={anyBusy}
            >
              {isBusy("pull", "abs") ? <Spin /> : null} ← Pull from ABS
            </Btn>
          </>
        ) : null}
      </div>
    </div>
  );
}
