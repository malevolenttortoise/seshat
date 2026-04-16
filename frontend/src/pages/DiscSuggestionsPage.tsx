// @ts-nocheck
import { useState, useEffect } from "react";
import { useTheme } from "../theme";
import { api } from "../api";
import { Ic } from "../icons";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { Load } from "../components/Load";
import type { NavFn } from "../types";

// Source-consensus series suggestion review page.
//
// Lists rows from /api/series-suggestions filtered by status. Each
// row shows the current vs suggested series state plus a chip list
// of agreeing source names. Apply, Ignore, and Delete actions hit
// the corresponding endpoints and dispatch the
// `seshat:suggestions-changed` event so App.jsx refetches the
// pending-count badge in the nav bar.

const SOURCE_BADGE = {
  goodreads: { bg: "#553b1a", fg: "#e8c070", br: "#88642a" },
  hardcover: { bg: "#1a3355", fg: "#70a8e8", br: "#2a5588" },
  kobo:      { bg: "#1a4533", fg: "#70e8a8", br: "#2a8855" },
};

function notifyChanged() {
  try { window.dispatchEvent(new CustomEvent("seshat:suggestions-changed")); } catch {}
}

function fmtSeriesValue(name: string | null | undefined, idx: number | null | undefined) {
  if (!name) return <em style={{ opacity: 0.7 }}>standalone</em>;
  return idx != null ? `${name} #${idx}` : name;
}

export default function SuggestionsPage({ onNav }: { onNav: NavFn }) {
  const t = useTheme();
  const [status, setStatus] = useState("pending");
  const [data, setData] = useState<any>(null);
  const [busy, setBusy] = useState<any>({});

  const load = () => {
    setData(null);
    api.get(`/series-suggestions?status=${status}`)
      .then(setData)
      .catch(e => { console.error(e); setData({ suggestions: [], count: 0 }); });
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [status]);

  const act = async (sug, action) => {
    if (busy[sug.id]) return;
    setBusy(b => ({ ...b, [sug.id]: action }));
    try {
      if (action === "apply")       await api.post(`/series-suggestions/${sug.id}/apply`);
      else if (action === "ignore") await api.post(`/series-suggestions/${sug.id}/ignore`);
      else if (action === "delete") await api.del(`/series-suggestions/${sug.id}`);
      notifyChanged();
      load();
    } catch (e) {
      alert(`${action} failed: ${e.message || e}`);
    } finally {
      setBusy(b => { const n = { ...b }; delete n[sug.id]; return n; });
    }
  };

  if (data === null) return <Load />;

  const suggestions = data.suggestions || [];
  const tabs = [
    { id: "pending",  label: "Pending"  },
    { id: "applied",  label: "Applied"  },
    { id: "ignored",  label: "Ignored"  },
    { id: "all",      label: "All"      },
  ];

  return <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>

    {/* Header */}
    <div>
      <h1 style={{ fontSize: 26, fontWeight: 700, color: t.text, margin: 0, display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 22 }}>💡</span> Series Suggestions
      </h1>
      <p style={{ fontSize: 14, color: t.td, marginTop: 4 }}>
        When 2 or more sources agree on a series name or index that differs from what's currently stored on a book,
        the disagreement appears here for your review. Apply to accept, Ignore to suppress this exact suggestion,
        or Delete to remove it (a future scan may recreate it if the consensus still holds).
      </p>
    </div>

    {/* Status tabs */}
    <div style={{ display: "flex", gap: 6, borderBottom: `1px solid ${t.borderL}`, paddingBottom: 0 }}>
      {tabs.map(tab => <button key={tab.id} onClick={() => setStatus(tab.id)} style={{
        padding: "10px 16px",
        background: "none",
        border: "none",
        borderBottom: status === tab.id ? `2px solid ${t.accent}` : "2px solid transparent",
        color: status === tab.id ? t.accent : t.tf,
        fontWeight: status === tab.id ? 600 : 500,
        fontSize: 14,
        cursor: "pointer",
        marginBottom: -1,
      }}>{tab.label}</button>)}
    </div>

    {/* Empty state */}
    {suggestions.length === 0 ? <div style={{
      background: t.bg2,
      border: `1px solid ${t.border}`,
      borderRadius: 12,
      padding: 40,
      textAlign: "center",
      color: t.tg,
    }}>
      <div style={{ fontSize: 32, marginBottom: 8 }}>✓</div>
      <div style={{ fontSize: 14 }}>
        {status === "pending" ? "No pending suggestions — your library and your sources agree!" :
         status === "applied" ? "No suggestions have been applied yet." :
         status === "ignored" ? "No ignored suggestions." :
         "No suggestions in any state."}
      </div>
    </div> : null}

    {/* List */}
    {suggestions.map(sug => {
      const sources = Array.isArray(sug.sources_agreeing) ? sug.sources_agreeing : [];
      const drift = sug.drifted;
      const isPending = sug.status === "pending";
      const isIgnored = sug.status === "ignored";
      const isApplied = sug.status === "applied";
      const busyAction = busy[sug.id];

      return <div key={sug.id} style={{
        background: t.bg2,
        border: `1px solid ${isPending ? t.border : t.borderL}`,
        borderRadius: 12,
        padding: "16px 20px",
        opacity: isApplied ? 0.7 : 1,
      }}>

        {/* Title row */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, marginBottom: 10, flexWrap: "wrap" }}>
          <div style={{ flex: "1 1 300px", minWidth: 0 }}>
            <div style={{ fontSize: 16, fontWeight: 600, color: t.text, marginBottom: 2 }}>{sug.book_title}</div>
            <button onClick={() => onNav("author", sug.author_id)} style={{
              background: "none", border: "none", padding: 0, cursor: "pointer",
              fontSize: 13, color: t.purt, textDecoration: "none",
            }}>{sug.author_name}</button>
          </div>

          {/* Status chip */}
          <span style={{
            fontSize: 11,
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            padding: "3px 9px",
            borderRadius: 5,
            background: isPending ? t.accent + "22" : isIgnored ? t.tg + "22" : t.grn + "22",
            color: isPending ? t.accent : isIgnored ? t.tg : t.grnt,
            border: `1px solid ${isPending ? t.accent + "44" : isIgnored ? t.tg + "44" : t.grn + "44"}`,
          }}>{sug.status}</span>
        </div>

        {/* Diff */}
        <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
          <div style={{ flex: "1 1 220px", minWidth: 0 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: t.tg, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Currently</div>
            <div style={{ fontSize: 14, color: t.text2 }}>{fmtSeriesValue(sug.live_series_name, sug.live_series_index)}</div>
            {drift ? <div style={{ fontSize: 11, color: t.ylwt, marginTop: 4, fontStyle: "italic" }}>
              ⚠ Changed since suggestion was generated (snapshot was: {fmtSeriesValue(sug.snapshot_series_name, sug.snapshot_series_index)})
            </div> : null}
          </div>
          <div style={{ fontSize: 18, color: t.tg }}>→</div>
          <div style={{ flex: "1 1 220px", minWidth: 0 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: t.tg, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Suggested</div>
            <div style={{ fontSize: 14, color: t.accent, fontWeight: 600 }}>{fmtSeriesValue(sug.suggested_series_name, sug.suggested_series_index)}</div>
          </div>
        </div>

        {/* Sources + actions */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
            <span style={{ fontSize: 11, fontWeight: 600, color: t.tg, textTransform: "uppercase", letterSpacing: "0.06em" }}>Agreed by</span>
            {sources.map(src => {
              const c = SOURCE_BADGE[src] || { bg: t.bg4, fg: t.td, br: t.border };
              return <span key={src} style={{
                display: "inline-flex", alignItems: "center",
                padding: "2px 9px", borderRadius: 5,
                fontSize: 11, fontWeight: 600,
                background: c.bg, color: c.fg, border: `1px solid ${c.br}`,
              }}>{src}</span>;
            })}
          </div>

          <div className="sb-actions" style={{ display: "flex", gap: 6, flexShrink: 0 }}>
            {isPending ? <>
              <Btn size="sm" variant="accent" onClick={() => act(sug, "apply")} disabled={!!busyAction}>
                {busyAction === "apply" ? <Spin /> : <>{Ic.check} Apply</>}
              </Btn>
              <Btn size="sm" variant="ghost" onClick={() => act(sug, "ignore")} disabled={!!busyAction}>
                {busyAction === "ignore" ? <Spin /> : "Ignore"}
              </Btn>
            </> : null}
            <Btn size="sm" variant="ghost" onClick={() => act(sug, "delete")} disabled={!!busyAction} style={{ color: t.redt }}>
              {busyAction === "delete" ? <Spin /> : Ic.trash}
            </Btn>
          </div>
        </div>
      </div>;
    })}

  </div>;
}
