// Mobile-native series-suggestions review page.
//
// Card-per-suggestion with: book title + author, current → suggested
// series diff, source-agreement badges, and three big buttons (Apply
// / Ignore / Delete). Status filter chips at the top, format tabs.
import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { usePersist } from "../hooks/usePersist";
import {
  MobileChip,
  MobileBtn,
  MobileBadge,
  MobileBackButton,
} from "../components/mobile";
import type { NavFn } from "../types";

interface SeriesSuggestion {
  id: number;
  book_id: number;
  book_title: string;
  author_id: number;
  author_name: string;
  status: "pending" | "applied" | "ignored";
  suggested_series_name: string | null;
  suggested_series_index: number | null;
  snapshot_series_name: string | null;
  snapshot_series_index: number | null;
  live_series_name: string | null;
  live_series_index: number | null;
  drifted: boolean;
  sources_agreeing: string[];
  created_at?: number;
}

interface SuggestionsListResponse {
  suggestions: SeriesSuggestion[];
  count: number;
}

type SuggestionAction = "apply" | "ignore" | "delete";

const SOURCE_TONE: Record<
  string,
  "ok" | "warn" | "err" | "info" | "accent" | "neutral"
> = {
  goodreads: "warn",
  hardcover: "info",
  kobo: "ok",
};

function fmtSeries(name: string | null, idx: number | null): string {
  if (!name) return "standalone";
  return idx != null ? `${name} #${idx}` : name;
}

export default function MobileSuggestionsPage({ onNav }: { onNav: NavFn }) {
  void onNav;
  const t = useTheme();
  const [status, setStatus] = useState<string>("pending");
  const [fmt, setFmt] = usePersist<string>("sg_fmt", "all");
  const [data, setData] = useState<SuggestionsListResponse | null>(null);
  const [busy, setBusy] = useState<Record<number, SuggestionAction>>({});

  const load = () => {
    setData(null);
    const params = new URLSearchParams({ status, content_type: fmt });
    api
      .get<SuggestionsListResponse>(`/discovery/series-suggestions?${params}`)
      .then(setData)
      .catch(() => setData({ suggestions: [], count: 0 }));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, fmt]);

  const act = async (sug: SeriesSuggestion, action: SuggestionAction) => {
    if (busy[sug.id]) return;
    setBusy((b) => ({ ...b, [sug.id]: action }));
    try {
      if (action === "apply")
        await api.post(`/discovery/series-suggestions/${sug.id}/apply`);
      else if (action === "ignore")
        await api.post(`/discovery/series-suggestions/${sug.id}/ignore`);
      else if (action === "delete")
        await api.del(`/discovery/series-suggestions/${sug.id}`);
      window.dispatchEvent(new CustomEvent("seshat:suggestions-changed"));
      load();
    } catch (e) {
      alert(`${action} failed: ${(e as Error).message || e}`);
    } finally {
      setBusy((b) => {
        const n = { ...b };
        delete n[sug.id];
        return n;
      });
    }
  };

  const suggestions = data?.suggestions || [];
  const isLoading = data === null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />
      <div>
        <h1
          style={{
            margin: 0,
            fontSize: 22,
            fontWeight: 700,
            color: t.text,
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          💡 Series Suggestions
        </h1>
        <p style={{ fontSize: 13, color: t.td, margin: "4px 0 0" }}>
          Sources agreeing on a series change you haven't applied yet.
        </p>
      </div>

      {/* Format tabs */}
      <div
        style={{
          display: "flex",
          gap: 6,
          overflowX: "auto",
          scrollbarWidth: "none",
        }}
      >
        {[
          { v: "all", label: "All" },
          { v: "ebook", label: "📖 Ebooks" },
          { v: "audiobook", label: "🎧 Audiobooks" },
        ].map((opt) => (
          <MobileChip
            key={opt.v}
            active={fmt === opt.v}
            onClick={() => setFmt(opt.v)}
          >
            {opt.label}
          </MobileChip>
        ))}
      </div>

      {/* Status chips */}
      <div
        style={{
          display: "flex",
          gap: 6,
          flexWrap: "wrap",
        }}
      >
        {[
          { v: "pending", label: "Pending" },
          { v: "applied", label: "Applied" },
          { v: "ignored", label: "Ignored" },
          { v: "all", label: "All" },
        ].map((opt) => (
          <MobileChip
            key={opt.v}
            active={status === opt.v}
            onClick={() => setStatus(opt.v)}
          >
            {opt.label}
          </MobileChip>
        ))}
      </div>

      {isLoading && (
        <div
          style={{
            padding: 24,
            textAlign: "center",
            color: t.tg,
            fontSize: 14,
          }}
        >
          Loading…
        </div>
      )}

      {!isLoading && suggestions.length === 0 && (
        <div
          style={{
            padding: 40,
            textAlign: "center",
            color: t.tg,
            fontSize: 14,
            background: t.bg2,
            border: `1px solid ${t.borderL}`,
            borderRadius: 12,
          }}
        >
          No {status === "all" ? "" : status} suggestions.
        </div>
      )}

      {/* Suggestion cards */}
      {suggestions.map((sug) => {
        const fromStr = fmtSeries(
          sug.live_series_name ?? sug.snapshot_series_name,
          sug.live_series_index ?? sug.snapshot_series_index,
        );
        const toStr = fmtSeries(
          sug.suggested_series_name,
          sug.suggested_series_index,
        );
        const isPending = sug.status === "pending";
        return (
          <div
            key={sug.id}
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 8,
              padding: 12,
              background: t.bg2,
              border: `1px solid ${sug.drifted ? t.ylwt : t.border}`,
              borderRadius: 12,
            }}
          >
            {/* Title + author */}
            <div>
              <div
                style={{
                  fontSize: 15,
                  fontWeight: 700,
                  color: t.text,
                  lineHeight: 1.3,
                }}
              >
                {sug.book_title}
              </div>
              <div style={{ fontSize: 13, color: t.td, marginTop: 2 }}>
                {sug.author_name}
              </div>
            </div>

            {/* Drift warning */}
            {sug.drifted && (
              <MobileBadge tone="warn">
                Live state differs from snapshot
              </MobileBadge>
            )}

            {/* Series diff */}
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 4,
                padding: 10,
                background: t.bg3,
                borderRadius: 8,
                fontSize: 13,
              }}
            >
              <div style={{ display: "flex", gap: 8 }}>
                <span style={{ color: t.tg, minWidth: 50 }}>From:</span>
                <span style={{ color: t.text2 }}>{fromStr}</span>
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <span style={{ color: t.tg, minWidth: 50 }}>To:</span>
                <span style={{ color: t.accent, fontWeight: 600 }}>{toStr}</span>
              </div>
            </div>

            {/* Source agreement */}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {sug.sources_agreeing.map((src) => (
                <MobileBadge key={src} tone={SOURCE_TONE[src] || "neutral"}>
                  {src}
                </MobileBadge>
              ))}
            </div>

            {/* Actions */}
            {isPending && (
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr 1fr",
                  gap: 8,
                  marginTop: 4,
                }}
              >
                <MobileBtn
                  variant="primary"
                  primary
                  fullWidth
                  onClick={() => act(sug, "apply")}
                  disabled={!!busy[sug.id]}
                >
                  {busy[sug.id] === "apply" ? "…" : "Apply"}
                </MobileBtn>
                <MobileBtn
                  variant="secondary"
                  fullWidth
                  onClick={() => act(sug, "ignore")}
                  disabled={!!busy[sug.id]}
                >
                  {busy[sug.id] === "ignore" ? "…" : "Ignore"}
                </MobileBtn>
                <MobileBtn
                  variant="danger"
                  fullWidth
                  onClick={() => act(sug, "delete")}
                  disabled={!!busy[sug.id]}
                >
                  {busy[sug.id] === "delete" ? "…" : "Delete"}
                </MobileBtn>
              </div>
            )}
            {!isPending && (
              <div
                style={{
                  fontSize: 12,
                  color: t.tg,
                  paddingTop: 4,
                  borderTop: `1px solid ${t.borderL}`,
                }}
              >
                Status: {sug.status}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
