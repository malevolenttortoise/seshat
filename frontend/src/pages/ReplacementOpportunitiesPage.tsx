// ReplacementOpportunitiesPage — v2.27.0 (Bundle A.2 Phase 5b complete).
//
// Detection-only list of cases where a freshly-grabbed torrent scored
// higher than an owned book in a replacement-enabled library. Phase
// 5b adds the destructive enact path: per-row Enact (with confirmation
// modal), multi-select bulk Enact, and per-row Restore on the
// `enacted` tab to reverse a swap from the `.seshat-replaced/` folder.
//
// The "Restore" action on `dismissed` rows just un-dismisses (PATCH
// back to `detected`) — different code path from the Phase 5b restore.
// We label the buttons distinctly to avoid confusing the two.
//
// Reached from Settings → Active Replacement → View, or via the
// "replacement-opportunities" page id in App.tsx.
import { useEffect, useMemo, useState } from "react";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { api } from "../api";
import { useTheme } from "../theme";

interface Opportunity {
  id: number;
  detected_at: number;
  candidate_grab_id: number;
  candidate_mam_torrent_id: string;
  candidate_format: string | null;
  candidate_score: number[];
  candidate_torrent_name: string | null;
  candidate_author_blob: string | null;
  owned_library_slug: string;
  owned_book_id: number;
  owned_mam_torrent_id: string | null;
  owned_torrent_name: string | null;
  owned_format: string | null;
  owned_score: number[] | null;
  media_type: string;
  status: "detected" | "enacted" | "dismissed";
  acted_at: number | null;
  acted_by: string | null;
}

interface Counts {
  detected: number;
  enacted: number;
  dismissed: number;
}

interface ListResponse {
  opportunities: Opportunity[];
  counts: Counts;
}

interface EnactmentResultBody {
  status: "enacted" | "restored" | "blocked" | "not_found" | "no_sink" | "failed";
  opportunity_id: number;
  enactment_id: number | null;
  detail: string;
  error: string | null;
  opportunity: Opportunity | null;
}

interface BulkResponse {
  results: EnactmentResultBody[];
  counts: Record<string, number>;
}

type StatusFilter = "detected" | "dismissed" | "enacted" | "all";

function formatTimestamp(epoch: number): string {
  const d = new Date(epoch * 1000);
  return d.toLocaleString();
}

function scoreLabel(score: number[] | null): string {
  if (!score) return "—";
  return `(${score.join(", ")})`;
}

// Map an EnactmentResult status to a user-facing label + tone.
function summarizeResult(status: EnactmentResultBody["status"]): {
  label: string; tone: "ok" | "warn" | "err";
} {
  switch (status) {
    case "enacted":   return { label: "Enacted",   tone: "ok"  };
    case "restored":  return { label: "Restored",  tone: "ok"  };
    case "blocked":   return { label: "Blocked",   tone: "warn" };
    case "not_found": return { label: "Not found", tone: "warn" };
    case "no_sink":   return { label: "No sink",   tone: "err" };
    case "failed":    return { label: "Failed",    tone: "err" };
    default:          return { label: String(status), tone: "err" };
  }
}


export default function ReplacementOpportunitiesPage() {
  const t = useTheme();
  const [status, setStatus] = useState<StatusFilter>("detected");
  const [data, setData] = useState<ListResponse | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // Confirmation modal state: list of opportunities about to be
  // enacted (1 for per-row, N for bulk). null = closed.
  const [confirmEnact, setConfirmEnact] = useState<Opportunity[] | null>(null);
  // Persisted result of the most recent enact/bulk-enact for UI
  // surfacing. The page renders one summary panel above the list.
  const [lastResults, setLastResults] = useState<EnactmentResultBody[] | null>(null);
  const [bulkRunning, setBulkRunning] = useState(false);

  const refresh = async () => {
    try {
      const q = status === "all" ? "?status=" : `?status=${status}`;
      const r = await api.get<ListResponse>(
        `/quality/replacement-opportunities${q}`,
      );
      setData(r);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    refresh();
    // Clear selection when the tab changes — the visible row set
    // changes so the prior selection is stale.
    setSelected(new Set());
    setLastResults(null);
  }, [status]);

  const dismiss = async (id: number, target: "dismissed" | "detected") => {
    setBusyId(id);
    try {
      await api.patch(
        `/quality/replacement-opportunities/${id}`,
        { status: target },
      );
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  };

  // Per-row enact: opens the confirmation modal with one entry.
  const askEnact = (op: Opportunity) => {
    setConfirmEnact([op]);
  };

  // Bulk enact: gathers the selected detected rows + opens the modal.
  const askBulkEnact = () => {
    if (!data) return;
    const rows = data.opportunities.filter(
      o => selected.has(o.id) && o.status === "detected",
    );
    if (rows.length === 0) return;
    setConfirmEnact(rows);
  };

  // Runs after the user confirms the modal. Handles both single-id
  // (HTTP code mapped — 4xx/5xx still return useful JSON bodies) and
  // bulk (always 200 with per-item results).
  const performEnact = async () => {
    if (!confirmEnact || confirmEnact.length === 0) return;
    setBulkRunning(true);
    setError(null);
    try {
      let results: EnactmentResultBody[];
      if (confirmEnact.length === 1) {
        const op = confirmEnact[0];
        try {
          const r = await api.post<EnactmentResultBody>(
            `/quality/replacement-opportunities/${op.id}/enact`,
          );
          results = [r];
        } catch (e) {
          // api.post throws for 4xx/5xx; the body lives on .detail per
          // FastAPI's HTTPException shape. Try to extract it; otherwise
          // synthesize a failure shape so the UI can render uniformly.
          const detail = extractDetailFromError(e, op.id);
          results = [detail];
        }
      } else {
        const r = await api.post<BulkResponse>(
          "/quality/replacement-opportunities/enact-bulk",
          { ids: confirmEnact.map(o => o.id) },
        );
        results = r.results;
      }
      setLastResults(results);
      // Clear selection + close modal whichever the path was.
      setSelected(new Set());
      setConfirmEnact(null);
      await refresh();
    } finally {
      setBulkRunning(false);
    }
  };

  // Per-row Restore (Phase 5b inverse of Enact). Calls
  // POST /restore which looks up the latest active enactment
  // internally. Only meaningful on the `enacted` tab.
  const restoreFromEnacted = async (op: Opportunity) => {
    setBusyId(op.id);
    setError(null);
    try {
      try {
        const r = await api.post<EnactmentResultBody>(
          `/quality/replacement-opportunities/${op.id}/restore`,
        );
        setLastResults([r]);
      } catch (e) {
        setLastResults([extractDetailFromError(e, op.id)]);
      }
      await refresh();
    } finally {
      setBusyId(null);
    }
  };

  if (!data) {
    return (
      <div style={{ display: "flex", justifyContent: "center", padding: 40 }}>
        <Spin />
      </div>
    );
  }

  const rows = data.opportunities;
  const counts = data.counts;

  const tabStyle = (active: boolean): React.CSSProperties => ({
    padding: "6px 14px",
    fontSize: 13,
    fontWeight: 600,
    background: active ? t.abg : "transparent",
    color: active ? t.accent : t.tm,
    border: `1px solid ${active ? t.accent : t.border}`,
    borderRadius: 6,
    cursor: "pointer",
  });

  // Bulk-action bar visible only when in `detected` tab AND ≥1 row picked.
  const showBulkBar = status === "detected" && selected.size > 0;

  return (
    <div>
      <h1 style={{ fontSize: 24, fontWeight: 700, color: t.text, marginBottom: 4 }}>
        Replacement Opportunities
      </h1>
      <p style={{ fontSize: 14, color: t.textDim, marginBottom: 20, lineHeight: 1.5, maxWidth: 800 }}>
        When a freshly-grabbed torrent scores higher than an owned book in a
        replacement-enabled library, the opportunity is logged here. Click{" "}
        <b>Enact</b> on a detected row to replace the owned copy with the
        higher-quality candidate; the owned file is soft-deleted to a
        <code style={{ marginLeft: 4 }}>.seshat-replaced/</code> folder for{" "}
        <b>30 days</b> (configurable in Settings) before the retention sweeper
        purges it. Click <b>Restore</b> on an enacted row to reverse the swap
        within the retention window.
      </p>

      <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
        <button style={tabStyle(status === "detected")} onClick={() => setStatus("detected")}>
          Detected ({counts.detected})
        </button>
        <button style={tabStyle(status === "dismissed")} onClick={() => setStatus("dismissed")}>
          Dismissed ({counts.dismissed})
        </button>
        <button style={tabStyle(status === "enacted")} onClick={() => setStatus("enacted")}>
          Enacted ({counts.enacted})
        </button>
        <button style={tabStyle(status === "all")} onClick={() => setStatus("all")}>
          All
        </button>
        <Btn variant="ghost" onClick={refresh}>Refresh</Btn>
      </div>

      {showBulkBar && (
        <div style={{
          background: t.abg, border: `1px solid ${t.accent}55`,
          borderRadius: 8, padding: "10px 14px", marginBottom: 12,
          display: "flex", alignItems: "center", gap: 12,
        }}>
          <span style={{ fontSize: 13, color: t.text, fontWeight: 600 }}>
            {selected.size} selected
          </span>
          <Btn onClick={askBulkEnact} disabled={bulkRunning}>
            {bulkRunning ? <Spin size={12} /> : `Enact ${selected.size} ${selected.size === 1 ? "row" : "rows"}`}
          </Btn>
          <Btn variant="ghost" onClick={() => setSelected(new Set())}>Clear</Btn>
        </div>
      )}

      {lastResults && lastResults.length > 0 && (
        <ResultSummaryPanel results={lastResults} onDismiss={() => setLastResults(null)} />
      )}

      {error && (
        <div style={{
          background: t.err + "22", border: `1px solid ${t.err}55`,
          color: t.err, padding: "10px 14px", borderRadius: 8,
          fontSize: 13, marginBottom: 16,
        }}>
          {error}
        </div>
      )}

      {rows.length === 0 && (
        <div style={{
          padding: 32, textAlign: "center", color: t.textDim,
          background: t.bg2, borderRadius: 8, fontSize: 14,
        }}>
          {status === "detected"
            ? "No replacement opportunities yet. They appear here when a higher-quality torrent matches a book you already own."
            : `No ${status} opportunities.`}
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {rows.map(op => {
          const candTitle = op.candidate_torrent_name || `tid ${op.candidate_mam_torrent_id}`;
          const ownedTitle = op.owned_torrent_name
            || (op.owned_mam_torrent_id ? `tid ${op.owned_mam_torrent_id}` : `book #${op.owned_book_id}`);
          const isChecked = selected.has(op.id);
          const selectable = op.status === "detected";
          return (
            <div key={op.id} style={{
              background: t.bg2,
              border: `1px solid ${isChecked ? t.accent : t.border}`,
              borderRadius: 8, padding: "12px 14px",
              display: "flex", alignItems: "center", gap: 12,
            }}>
              {selectable && (
                <input
                  type="checkbox"
                  checked={isChecked}
                  onChange={() => {
                    setSelected(prev => {
                      const next = new Set(prev);
                      if (next.has(op.id)) next.delete(op.id);
                      else next.add(op.id);
                      return next;
                    });
                  }}
                  style={{
                    width: 16, height: 16, accentColor: t.accent,
                    cursor: "pointer", flexShrink: 0,
                  }}
                />
              )}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                  <span style={{
                    fontSize: 10, fontWeight: 700, color: t.accent,
                    background: t.bg3, padding: "2px 6px", borderRadius: 4,
                    textTransform: "uppercase", letterSpacing: "0.04em",
                  }}>
                    {op.media_type}
                  </span>
                  <span style={{
                    fontSize: 10, fontWeight: 700, color: t.text2,
                    background: t.bg3, padding: "2px 6px", borderRadius: 4,
                  }}>
                    {op.owned_library_slug}
                  </span>
                  <span style={{ fontSize: 11, color: t.textDim }}>
                    detected {formatTimestamp(op.detected_at)}
                  </span>
                </div>
                <div style={{ fontSize: 13, color: t.text, marginBottom: 2 }}>
                  <b>Candidate:</b> {candTitle}
                  {op.candidate_format && <> · {op.candidate_format}</>}
                  <span style={{ color: t.textDim, marginLeft: 8 }}>
                    {scoreLabel(op.candidate_score)}
                  </span>
                </div>
                <div style={{ fontSize: 13, color: t.text2 }}>
                  <b>Owned:</b> {ownedTitle}
                  {op.owned_format && <> · {op.owned_format}</>}
                  <span style={{ color: t.textDim, marginLeft: 8 }}>
                    {scoreLabel(op.owned_score)}
                  </span>
                </div>
                {op.status !== "detected" && (
                  <div style={{ fontSize: 11, color: t.textDim, marginTop: 4 }}>
                    {op.status} {op.acted_by && <>by {op.acted_by}</>}
                    {op.acted_at && <> · {formatTimestamp(op.acted_at)}</>}
                  </div>
                )}
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                {op.status === "detected" && (
                  <>
                    <Btn onClick={() => askEnact(op)} disabled={busyId === op.id || bulkRunning}>
                      {busyId === op.id ? <Spin size={12} /> : "Enact"}
                    </Btn>
                    <Btn variant="ghost" onClick={() => dismiss(op.id, "dismissed")} disabled={busyId === op.id || bulkRunning}>
                      Dismiss
                    </Btn>
                  </>
                )}
                {op.status === "dismissed" && (
                  <Btn variant="ghost" onClick={() => dismiss(op.id, "detected")} disabled={busyId === op.id}>
                    {busyId === op.id ? <Spin size={12} /> : "Un-dismiss"}
                  </Btn>
                )}
                {op.status === "enacted" && (
                  <Btn variant="ghost" onClick={() => restoreFromEnacted(op)} disabled={busyId === op.id}>
                    {busyId === op.id ? <Spin size={12} /> : "Restore"}
                  </Btn>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {confirmEnact && (
        <ConfirmEnactModal
          opportunities={confirmEnact}
          running={bulkRunning}
          onCancel={() => setConfirmEnact(null)}
          onConfirm={performEnact}
        />
      )}
    </div>
  );
}


// ─── Helpers ─────────────────────────────────────────────────


/**
 * Pull an EnactmentResult-shaped detail out of an api.* throw. FastAPI
 * 4xx/5xx responses carry the orchestrator's JSON body inside
 * .response.data.detail; the api helper's thrown error preserves that
 * shape. If we can't unwrap, synthesise a failure entry so the UI's
 * uniform rendering still works.
 */
function extractDetailFromError(e: unknown, opportunity_id: number): EnactmentResultBody {
  // Shape: { response: { data: { detail: {...} } } } for HTTP errors;
  // axios-like. The api helper may also throw a plain Error.
  const anyE = e as { response?: { data?: { detail?: unknown } }; message?: string };
  const detail = anyE?.response?.data?.detail;
  if (detail && typeof detail === "object") {
    const d = detail as Partial<EnactmentResultBody>;
    if (d.status) return d as EnactmentResultBody;
  }
  return {
    status: "failed",
    opportunity_id,
    enactment_id: null,
    detail: anyE?.message || "request failed",
    error: anyE?.message || null,
    opportunity: null,
  };
}


// ─── Result summary panel ────────────────────────────────────


function ResultSummaryPanel({ results, onDismiss }: {
  results: EnactmentResultBody[]; onDismiss: () => void;
}) {
  const t = useTheme();
  const counts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const r of results) m[r.status] = (m[r.status] || 0) + 1;
    return m;
  }, [results]);

  const headline = Object.entries(counts)
    .map(([k, v]) => `${v} ${summarizeResult(k as EnactmentResultBody["status"]).label.toLowerCase()}`)
    .join(", ");

  const failedItems = results.filter(r => r.status !== "enacted" && r.status !== "restored");

  return (
    <div style={{
      background: t.bg2, border: `1px solid ${t.border}`,
      borderRadius: 8, padding: "12px 14px", marginBottom: 12,
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ fontSize: 13, color: t.text, fontWeight: 600 }}>
          Last action result: {headline}
        </div>
        <Btn size="sm" variant="ghost" onClick={onDismiss}>Dismiss</Btn>
      </div>
      {failedItems.length > 0 && (
        <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 4 }}>
          {failedItems.map((r, i) => {
            const sum = summarizeResult(r.status);
            const colour = sum.tone === "err" ? t.err : sum.tone === "warn" ? t.warn : t.text;
            return (
              <div key={i} style={{ fontSize: 12, color: colour }}>
                <b>#{r.opportunity_id}</b> — <b>{sum.label}</b>: {r.detail}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}


// ─── Confirmation modal ──────────────────────────────────────


function ConfirmEnactModal({ opportunities, running, onCancel, onConfirm }: {
  opportunities: Opportunity[];
  running: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const t = useTheme();
  const bulk = opportunities.length > 1;
  return (
    <div
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)",
        zIndex: 200, display: "flex", alignItems: "center",
        justifyContent: "center", animation: "fadeOverlay 0.2s ease-out",
      }}
      onClick={running ? undefined : onCancel}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: t.bg2, border: `1px solid ${t.border}`,
          borderRadius: 12, padding: 24, width: 600, maxWidth: "90vw",
          maxHeight: "85vh", display: "flex", flexDirection: "column", gap: 14,
        }}
      >
        <h2 style={{ fontSize: 18, fontWeight: 700, color: t.text, margin: 0 }}>
          Confirm enact{bulk ? ` (${opportunities.length} rows)` : ""}
        </h2>
        <div style={{
          padding: "10px 12px", background: t.warn + "1a",
          border: `1px solid ${t.warn}55`, borderRadius: 6, fontSize: 13,
          color: t.text, lineHeight: 1.5,
        }}>
          <b>This is destructive.</b> The owned file{bulk ? "s" : ""} will be
          moved out of the library to <code>.seshat-replaced/&lt;timestamp&gt;/</code>{" "}
          and the library row{bulk ? "s" : ""} removed via{" "}
          <code>calibredb remove</code> (or CWA admin API on slim images).
          The candidate copy is what remains in the library.
          <br /><br />
          You have <b>30 days</b> (or whatever the retention setting is) to
          click <b>Restore</b> from the Enacted tab to reverse this.
        </div>

        <div style={{
          maxHeight: 280, overflowY: "auto", display: "flex",
          flexDirection: "column", gap: 6,
        }}>
          {opportunities.map(op => (
            <div key={op.id} style={{
              fontSize: 12, color: t.text, background: t.bg3,
              borderRadius: 6, padding: "8px 10px",
            }}>
              <div style={{ color: t.textDim, marginBottom: 2 }}>
                #{op.id} · {op.owned_library_slug} · {op.media_type}
              </div>
              <div><b>Candidate:</b> {op.candidate_torrent_name || `tid ${op.candidate_mam_torrent_id}`} {op.candidate_format && <>· {op.candidate_format}</>}</div>
              <div style={{ color: t.text2 }}>
                <b>Owned:</b> {op.owned_torrent_name || `book #${op.owned_book_id}`} {op.owned_format && <>· {op.owned_format}</>}
              </div>
            </div>
          ))}
        </div>

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Btn variant="ghost" onClick={onCancel} disabled={running}>Cancel</Btn>
          <Btn onClick={onConfirm} disabled={running}>
            {running ? <Spin size={12} /> : `Enact ${bulk ? `${opportunities.length} rows` : "this"}`}
          </Btn>
        </div>
      </div>
    </div>
  );
}
