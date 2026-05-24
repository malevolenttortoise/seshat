// ReplacementOpportunitiesPage — v2.26.0 (Bundle A.2 Phase 6c).
//
// Detection-only list of cases where a freshly-grabbed torrent scored
// higher than an owned book in a replacement-enabled library. Each
// row shows the candidate vs. owned context + a Dismiss action. The
// actual file-swap path lands in a follow-up release (Phase 5b).
//
// Reached from Settings → Active Replacement → View, or via the
// "replacement-opportunities" page id in App.tsx.
import { useEffect, useState } from "react";
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

type StatusFilter = "detected" | "dismissed" | "enacted" | "all";

function formatTimestamp(epoch: number): string {
  const d = new Date(epoch * 1000);
  return d.toLocaleString();
}

function scoreLabel(score: number[] | null): string {
  if (!score) return "—";
  return `(${score.join(", ")})`;
}

export default function ReplacementOpportunitiesPage() {
  const t = useTheme();
  const [status, setStatus] = useState<StatusFilter>("detected");
  const [data, setData] = useState<ListResponse | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

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

  useEffect(() => { refresh(); }, [status]);

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

  return (
    <div>
      <h1 style={{ fontSize: 24, fontWeight: 700, color: t.text, marginBottom: 4 }}>
        Replacement Opportunities
      </h1>
      <p style={{ fontSize: 14, color: t.textDim, marginBottom: 20, lineHeight: 1.5, maxWidth: 800 }}>
        When a freshly-grabbed torrent scores higher than an owned book in a
        replacement-enabled library, the opportunity is logged here. v2.26.0
        is detection-only — review the queue, dismiss anything you don't want
        acted on. The file-swap path lands in a follow-up release; until then
        you can manually upgrade via your library app if you want.
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
          return (
            <div key={op.id} style={{
              background: t.bg2, border: `1px solid ${t.border}`,
              borderRadius: 8, padding: "12px 14px",
              display: "flex", alignItems: "center", gap: 12,
            }}>
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
                  <Btn variant="ghost" onClick={() => dismiss(op.id, "dismissed")} disabled={busyId === op.id}>
                    {busyId === op.id ? <Spin size={12} /> : "Dismiss"}
                  </Btn>
                )}
                {op.status === "dismissed" && (
                  <Btn variant="ghost" onClick={() => dismiss(op.id, "detected")} disabled={busyId === op.id}>
                    {busyId === op.id ? <Spin size={12} /> : "Restore"}
                  </Btn>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
