// The book sidebar's series-suggestion card: shown only when an active
// (pending or ignored) suggestion exists for the book. Apply / Ignore /
// Delete hit the same endpoints SuggestionsPage uses and dispatch the
// same EVT.SuggestionsChanged event so the navbar badge count stays in
// sync.
//
// The sidebar calls the hook (the suggestion outlives its edit mode, which
// hides the card) and places the card (audit G158).
import { useEffect, useState } from "react";
import { api } from "../../api";
import { useTheme } from "../../theme";
import { Ic } from "../../icons";
import { EVT } from "../../types";
import { Btn } from "../Btn";
import { Spin } from "../Spin";

// Returned by /discovery/series-suggestions/by-book/{id} as
// `{suggestion: ... | null}`.
interface SeriesSuggestion {
  id: number;
  status: string;
  current_series_name: string | null;
  current_series_index: number | null;
  suggested_series_name: string | null;
  suggested_series_index: number | null;
  sources_agreeing: string[];
}

type SuggestionAction = "apply" | "ignore" | "delete";

export function useSeriesSuggestion(
  bookId: number | undefined,
  onEdit?: () => void | Promise<void>,
) {
  const [suggestion, setSuggestion] = useState<SeriesSuggestion | null>(null);
  const [busy, setBusy] = useState<SuggestionAction | null>(null);

  // The endpoint returns `{suggestion: null}` rather than 404 when
  // nothing exists, so we always reach a deterministic terminal state
  // without branching on HTTP status.
  useEffect(() => {
    if (!bookId) {
      setSuggestion(null);
      return;
    }
    let cancelled = false;
    api
      .get<{ suggestion: SeriesSuggestion | null }>(
        `/discovery/series-suggestions/by-book/${bookId}`,
      )
      .then((r) => {
        if (!cancelled) setSuggestion(r.suggestion || null);
      })
      .catch(() => {
        if (!cancelled) setSuggestion(null);
      });
    return () => {
      cancelled = true;
    };
  }, [bookId]);

  const act = async (action: SuggestionAction) => {
    if (!suggestion || busy) return;
    setBusy(action);
    try {
      if (action === "apply")
        await api.post(`/discovery/series-suggestions/${suggestion.id}/apply`);
      else if (action === "ignore")
        await api.post(`/discovery/series-suggestions/${suggestion.id}/ignore`);
      else if (action === "delete")
        await api.del(`/discovery/series-suggestions/${suggestion.id}`);
      try {
        window.dispatchEvent(new CustomEvent(EVT.SuggestionsChanged));
      } catch {
        /* ignore */
      }
      setSuggestion(null);
      if (action === "apply" && onEdit) await onEdit();
    } catch (e) {
      alert(`${action} failed: ${(e as Error).message || e}`);
    }
    setBusy(null);
  };

  return { suggestion, busy, act };
}

export type SeriesSuggestionState = ReturnType<typeof useSeriesSuggestion>;

const fmtSuggestion = (name: string | null, idx: number | null) =>
  name ? (idx != null ? `${name} #${idx}` : name) : "standalone";

export function SeriesSuggestionCard({ s }: { s: SeriesSuggestionState }) {
  const t = useTheme();
  const { suggestion, busy: sugBusy, act: sugAction } = s;
  if (!suggestion) return null;
  const isPending = suggestion.status === "pending";
  const sources = Array.isArray(suggestion.sources_agreeing)
    ? suggestion.sources_agreeing
    : [];
  return (
    <div
      style={{
        background: t.accent + "12",
        border: `1px solid ${t.accent}44`,
        borderRadius: 10,
        padding: "12px 14px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <span style={{ fontSize: 14 }}>💡</span>
        <span
          style={{
            fontSize: 12,
            fontWeight: 700,
            color: t.accent,
            textTransform: "uppercase",
            letterSpacing: "0.06em",
          }}
        >
          Series Suggestion
        </span>
        {!isPending ? (
          <span
            style={{
              fontSize: 10,
              fontWeight: 600,
              color: t.tg,
              textTransform: "uppercase",
              padding: "1px 6px",
              borderRadius: 4,
              background: t.bg4,
              border: `1px solid ${t.borderL}`,
            }}
          >
            {suggestion.status}
          </span>
        ) : null}
      </div>
      <div
        style={{
          fontSize: 12,
          color: t.text2,
          lineHeight: 1.5,
        }}
      >
        <span style={{ color: t.tg }}>Currently:</span>{" "}
        <span style={{ color: t.text2 }}>
          {fmtSuggestion(
            suggestion.current_series_name,
            suggestion.current_series_index,
          )}
        </span>
        <br />
        <span style={{ color: t.tg }}>Suggested:</span>{" "}
        <span
          style={{ color: t.accent, fontWeight: 600 }}
        >
          {fmtSuggestion(
            suggestion.suggested_series_name,
            suggestion.suggested_series_index,
          )}
        </span>
      </div>
      <div style={{ fontSize: 11, color: t.tg }}>
        Agreed by: {sources.join(", ") || "—"}
      </div>
      <div
        style={{ display: "flex", gap: 6, flexWrap: "wrap" }}
      >
        {isPending ? (
          <>
            <Btn
              size="sm"
              variant="accent"
              onClick={() => sugAction("apply")}
              disabled={!!sugBusy}
            >
              {sugBusy === "apply" ? (
                <Spin />
              ) : (
                <>
                  {Ic.check} Apply
                </>
              )}
            </Btn>
            <Btn
              size="sm"
              variant="ghost"
              onClick={() => sugAction("ignore")}
              disabled={!!sugBusy}
            >
              {sugBusy === "ignore" ? <Spin /> : "Ignore"}
            </Btn>
          </>
        ) : null}
        <Btn
          size="sm"
          variant="ghost"
          onClick={() => sugAction("delete")}
          disabled={!!sugBusy}
          style={{ color: t.redt }}
        >
          {sugBusy === "delete" ? <Spin /> : Ic.trash}
        </Btn>
      </div>
    </div>
  );
}
