// v2.3.4 Compare panel — per-field side-by-side Seshat / Calibre /
// ABS metadata view, with per-field "← pull from X" buttons.
//
// Fetches from `GET /api/discovery/books/{bid}/compare`. Pulls write
// the chosen snapshot value(s) into Seshat-live via
// `POST /api/discovery/books/{bid}/pull` and add the field to
// `user_edited_fields` so the next sync's auto-flow doesn't roll
// the change back.
//
// Diff cells are visually highlighted; user-edited fields get a
// small badge so the user knows the auto-flow is already gated.

import { useEffect, useState } from "react";
import { useTheme } from "../theme";
import { api, ApiError } from "../api";
import { Btn } from "./Btn";
import { Spin } from "./Spin";

interface CompareField {
  field: string;
  label: string;
  seshat: unknown;
  calibre: unknown;
  abs: unknown;
  calibre_diff: boolean;
  abs_diff: boolean;
  user_edited: boolean;
}

interface CompareResponse {
  book_id: number;
  user_edited_fields: string[];
  calibre_synced_at: number | null;
  abs_synced_at: number | null;
  fields: CompareField[];
}

interface CompareModalProps {
  bookId: number;
  bookTitle: string;
  onClose: () => void;
  onChanged: () => void; // parent refresh hook (sidebar re-fetches the book)
}

export function CompareModal({
  bookId,
  bookTitle,
  onClose,
  onChanged,
}: CompareModalProps) {
  const t = useTheme();
  const [data, setData] = useState<CompareResponse | null>(null);
  const [busy, setBusy] = useState<string>(""); // `${field}|${source}`
  const [err, setErr] = useState("");

  const refresh = () => {
    api
      .get<CompareResponse>(`/discovery/books/${bookId}/compare`)
      .then(setData)
      .catch((e) => {
        setErr(`Failed to load: ${(e as Error).message}`);
        setData({
          book_id: bookId,
          user_edited_fields: [],
          calibre_synced_at: null,
          abs_synced_at: null,
          fields: [],
        });
      });
  };

  useEffect(refresh, [bookId]);

  const pull = async (field: string, source: "calibre" | "abs") => {
    const key = `${field}|${source}`;
    setBusy(key);
    setErr("");
    try {
      await api.post(`/discovery/books/${bookId}/pull`, {
        source,
        fields: [field],
      });
      onChanged();
      refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      setErr(`Pull failed: ${msg}`);
    } finally {
      setBusy("");
    }
  };

  const fmt = (v: unknown): string => {
    if (v === null || v === undefined) return "—";
    if (typeof v === "string" && !v.trim()) return "—";
    return String(v);
  };

  const fmtSyncedAt = (ts: number | null): string => {
    if (!ts) return "never synced";
    const d = new Date(ts * 1000);
    return d.toLocaleDateString();
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        zIndex: 220,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        animation: "fadeOverlay 0.2s ease-out",
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="modal-panel"
        style={{
          background: t.bg2,
          border: `1px solid ${t.border}`,
          borderRadius: 12,
          padding: 24,
          animation: "fadeIn 0.2s ease-out",
          width: 1100,
          maxWidth: "95vw",
          maxHeight: "90vh",
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        {/* Header */}
        <div>
          <h2
            style={{
              fontSize: 18,
              fontWeight: 700,
              color: t.text,
              margin: 0,
            }}
          >
            Compare metadata — {bookTitle}
          </h2>
          <div style={{ fontSize: 12, color: t.td, marginTop: 4 }}>
            Calibre {fmtSyncedAt(data?.calibre_synced_at ?? null)} · ABS{" "}
            {fmtSyncedAt(data?.abs_synced_at ?? null)}
          </div>
        </div>

        {err ? (
          <div
            style={{
              fontSize: 13,
              color: t.redt || t.red,
              background: `${t.red}22`,
              border: `1px solid ${t.red}66`,
              borderRadius: 6,
              padding: "8px 10px",
            }}
          >
            {err}
          </div>
        ) : null}

        {/* Table */}
        {!data ? (
          <Spin />
        ) : data.fields.length === 0 ? (
          <div style={{ fontSize: 13, color: t.tg, fontStyle: "italic" }}>
            No comparable fields yet — neither Calibre nor ABS has a
            snapshot for this book.
          </div>
        ) : (
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: 13,
            }}
          >
            <thead>
              <tr style={{ background: t.bg, color: t.tf }}>
                <th style={hth(t)}>Field</th>
                <th style={hth(t)}>Seshat</th>
                <th style={hth(t)}>Calibre</th>
                <th style={hth(t)}>ABS</th>
              </tr>
            </thead>
            <tbody>
              {data.fields.map((f) => (
                <tr
                  key={f.field}
                  style={{ borderTop: `1px solid ${t.borderL}` }}
                >
                  <td style={td(t)}>
                    <div
                      style={{ fontWeight: 600, color: t.text }}
                    >
                      {f.label}
                    </div>
                    {f.user_edited ? (
                      <div
                        style={{
                          fontSize: 10,
                          color: t.accent,
                          marginTop: 2,
                          textTransform: "uppercase",
                          letterSpacing: "0.04em",
                        }}
                      >
                        user-edited
                      </div>
                    ) : null}
                  </td>
                  <td style={{ ...td(t), color: t.text2, maxWidth: 280 }}>
                    <div style={cellStyle(f.seshat)}>{fmt(f.seshat)}</div>
                  </td>
                  <CompareCell
                    value={f.calibre}
                    diff={f.calibre_diff}
                    onPull={() => pull(f.field, "calibre")}
                    busy={busy === `${f.field}|calibre`}
                    fmt={fmt}
                    cellStyle={cellStyle}
                    t={t}
                    label="← pull from Calibre"
                  />
                  <CompareCell
                    value={f.abs}
                    diff={f.abs_diff}
                    onPull={() => pull(f.field, "abs")}
                    busy={busy === `${f.field}|abs`}
                    fmt={fmt}
                    cellStyle={cellStyle}
                    t={t}
                    label="← pull from ABS"
                  />
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {/* Footer */}
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            borderTop: `1px solid ${t.borderL}`,
            paddingTop: 14,
          }}
        >
          <Btn variant="ghost" onClick={onClose}>
            Close
          </Btn>
        </div>
      </div>
    </div>
  );
}

interface CompareCellProps {
  value: unknown;
  diff: boolean;
  onPull: () => void;
  busy: boolean;
  fmt: (v: unknown) => string;
  cellStyle: (v: unknown) => React.CSSProperties;
  t: ReturnType<typeof useTheme>;
  label: string;
}

function CompareCell({
  value,
  diff,
  onPull,
  busy,
  fmt,
  cellStyle,
  t,
  label,
}: CompareCellProps) {
  const empty = value === null || value === undefined ||
    (typeof value === "string" && !value.trim());
  return (
    <td
      style={{
        ...td(t),
        color: t.text2,
        maxWidth: 280,
        background: diff ? `${t.accent}10` : undefined,
      }}
    >
      <div style={cellStyle(value)}>{fmt(value)}</div>
      {!empty && diff ? (
        <Btn
          variant="ghost"
          size="xs"
          onClick={onPull}
          disabled={busy}
          style={{ marginTop: 6 }}
        >
          {busy ? <Spin /> : null} {label}
        </Btn>
      ) : null}
    </td>
  );
}

function hth(t: ReturnType<typeof useTheme>): React.CSSProperties {
  return {
    padding: "10px 14px",
    textAlign: "left",
    fontWeight: 600,
    fontSize: 12,
    color: t.tf,
    borderBottom: `1px solid ${t.border}`,
  };
}

function td(t: ReturnType<typeof useTheme>): React.CSSProperties {
  return {
    padding: "10px 14px",
    color: t.tf,
    verticalAlign: "top",
  };
}

function cellStyle(v: unknown): React.CSSProperties {
  // Long string values get clamped + line-clamped to keep rows from
  // ballooning when descriptions or tags vary in length. Short
  // values render inline.
  const text = typeof v === "string" ? v : "";
  const long = text.length > 80;
  return long
    ? {
        display: "-webkit-box",
        WebkitLineClamp: 4,
        WebkitBoxOrient: "vertical" as const,
        overflow: "hidden",
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      }
    : { whiteSpace: "pre-wrap", wordBreak: "break-word" };
}
