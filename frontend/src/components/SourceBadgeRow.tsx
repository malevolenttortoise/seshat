// v2.20.0 Phase 3 — source-ID badges on the author detail page.
//
// Visual parity with the per-book source badges in BookSidebar
// (`badgeColors` there) — dark muted bg + bright fg + medium border
// per source. Each pill renders the source label + a ↗ icon when
// populated (clicks open the canonical author URL in a new tab) plus
// a ✏ edit affordance (always visible on populated badges) that
// opens the edit modal. Empty badges render faded and click-anywhere
// opens the modal directly.
//
// Edit modal accepts a pasted ID or full URL; live preview hits
// `/persons/{id}/source-id/preview` per keystroke and shows
// "Parsed as: X → URL" or "Unrecognized — please paste an ID or a
// {Source} URL". Confirm calls PATCH; Clear button shows when a value
// exists and lets the user wipe the ID across every linked library
// in one click.
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { toast } from "../lib/toast";
import { useTheme } from "../theme";

const SOURCE_ORDER = [
  "amazon",
  "goodreads",
  "hardcover",
  "openlibrary",
  "audible",
  "kobo",
  "ibdb",
  "google_books",
  "fictiondb",
] as const;

type SourceKey = (typeof SOURCE_ORDER)[number];

const SOURCE_LABELS: Record<SourceKey, string> = {
  amazon: "amazon",
  goodreads: "goodreads",
  hardcover: "hardcover",
  openlibrary: "openlibrary",
  audible: "audible",
  kobo: "kobo",
  ibdb: "ibdb",
  google_books: "google books",
  fictiondb: "fictiondb",
};

// Palette mirrors `badgeColors` in BookSidebar.tsx so the author
// detail badges and per-book metadata badges read as the same
// visual language. (fictiondb is new in v2.20.0 — slate-blue tone
// to differentiate from goodreads brown.)
interface BadgePalette { bg: string; fg: string; br: string }

const SOURCE_PALETTE: Record<SourceKey, BadgePalette> = {
  goodreads:    { bg: "#553b1a", fg: "#e8c070", br: "#88642a" },
  hardcover:    { bg: "#1a3355", fg: "#70a8e8", br: "#2a5588" },
  kobo:         { bg: "#1a4533", fg: "#70e8a8", br: "#2a8855" },
  amazon:       { bg: "#3d2e1a", fg: "#f0a83c", br: "#7a5c2a" },
  audible:      { bg: "#3d2010", fg: "#f08838", br: "#7a4218" },
  ibdb:         { bg: "#2a1a3d", fg: "#c070e8", br: "#5a2a88" },
  google_books: { bg: "#1a3333", fg: "#70c8e8", br: "#2a7788" },
  openlibrary:  { bg: "#3a1d1a", fg: "#e87a6a", br: "#88332a" },
  fictiondb:    { bg: "#1a2533", fg: "#7090c8", br: "#2a4577" },
};

interface SourceBadgeRowProps {
  personId: number;
  sourceIds: Record<string, string | null | undefined>;
  onUpdate?: () => void | Promise<void>;
}

interface PreviewResponse {
  source: string;
  parsed: string | null;
  url: string | null;
}

interface PatchResponse {
  person_id: number;
  source: string;
  parsed: string | null;
  url: string | null;
  old_value: string | null;
  mirrored_rows: number;
}

export function SourceBadgeRow({
  personId, sourceIds, onUpdate,
}: SourceBadgeRowProps) {
  const t = useTheme();
  const [editing, setEditing] = useState<SourceKey | null>(null);

  return (
    <>
      <div style={{ marginBottom: 12 }}>
        <div
          style={{
            fontSize: 11,
            fontWeight: 600,
            color: t.tg,
            textTransform: "uppercase",
            marginBottom: 6,
            letterSpacing: "0.04em",
          }}
        >
          Source IDs
        </div>
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {SOURCE_ORDER.map((src) => {
            const value = sourceIds[src] || null;
            return (
              <SourceBadge
                key={src}
                source={src}
                value={value}
                onEdit={() => setEditing(src)}
              />
            );
          })}
        </div>
      </div>
      {editing && (
        <SourceBadgeEditModal
          personId={personId}
          source={editing}
          currentValue={sourceIds[editing] || ""}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            if (onUpdate) await onUpdate();
          }}
        />
      )}
    </>
  );
}


function SourceBadge({
  source, value, onEdit,
}: {
  source: SourceKey;
  value: string | null;
  onEdit: () => void;
}) {
  const populated = !!value;
  const palette = SOURCE_PALETTE[source];
  const url = populated ? buildCanonicalUrl(source, value || "") : null;

  // Common pill styling matches the BookSidebar metadata badges so
  // both views read as one visual system.
  const pillStyle: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 4,
    padding: "3px 8px 3px 10px",
    borderRadius: 5,
    fontSize: 12,
    fontWeight: 600,
    background: palette.bg,
    color: palette.fg,
    border: `1px solid ${palette.br}`,
    textDecoration: "none",
    opacity: populated ? 1 : 0.45,
  };

  // Empty badge: whole pill opens the modal — there's nothing to
  // open in a new tab, so we don't need two click zones.
  if (!populated) {
    return (
      <button
        type="button"
        onClick={onEdit}
        style={{ ...pillStyle, cursor: "pointer" }}
        title={`Add ${SOURCE_LABELS[source]} ID`}
      >
        {SOURCE_LABELS[source]}
        <span style={{ fontSize: 10, opacity: 0.7 }}>+</span>
      </button>
    );
  }

  // Populated badge: two zones in one pill.
  //   Left  (label + ↗) → opens the canonical URL in a new tab.
  //   Right (✏)         → opens the edit modal.
  // Both visible at all times; no hover-gated controls.
  const labelInner = (
    <>
      <span>{SOURCE_LABELS[source]}</span>
      <span style={{ fontSize: 10, opacity: 0.7 }}>↗</span>
    </>
  );
  const labelZone = url ? (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        color: palette.fg,
        textDecoration: "none",
      }}
      title={`Open ${SOURCE_LABELS[source]} (${value})`}
    >
      {labelInner}
    </a>
  ) : (
    // Source has no canonical URL (e.g. fictiondb with bare ID).
    // Label still renders but isn't a link; the edit ✏ remains.
    <span
      style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
      title={`${SOURCE_LABELS[source]}: ${value}`}
    >
      {labelInner}
    </span>
  );

  return (
    <div style={pillStyle}>
      {labelZone}
      <button
        type="button"
        onClick={onEdit}
        style={{
          background: "transparent",
          border: "none",
          color: palette.fg,
          cursor: "pointer",
          padding: "0 0 0 6px",
          marginLeft: 2,
          fontSize: 11,
          lineHeight: 1,
          opacity: 0.75,
          borderLeft: `1px solid ${palette.br}`,
        }}
        title={`Edit ${SOURCE_LABELS[source]} ID`}
        aria-label={`Edit ${SOURCE_LABELS[source]} ID`}
      >
        ✏
      </button>
    </div>
  );
}


function buildCanonicalUrl(source: string, value: string): string | null {
  if (!value) return null;
  switch (source) {
    case "amazon":
      return `https://www.amazon.com/stores/author/${value}/allbooks`;
    case "goodreads":
      return `https://www.goodreads.com/author/show/${value}`;
    case "openlibrary":
      return `https://openlibrary.org/authors/${value}`;
    case "audible":
      return `https://www.audible.com/author/-/${value}`;
    case "hardcover":
      // Numeric ID has no canonical URL — Hardcover routes by slug.
      return /^\d+$/.test(value) ? null : `https://hardcover.app/authors/${value}`;
    default:
      return null;
  }
}


function SourceBadgeEditModal({
  personId, source, currentValue, onClose, onSaved,
}: {
  personId: number;
  source: SourceKey;
  currentValue: string;
  onClose: () => void;
  onSaved: () => void | Promise<void>;
}) {
  const t = useTheme();
  const palette = SOURCE_PALETTE[source];
  const [input, setInput] = useState(currentValue);
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const debounceRef = useRef<number | null>(null);

  useEffect(() => {
    if (debounceRef.current) {
      window.clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    debounceRef.current = window.setTimeout(async () => {
      try {
        const r = await api.get<PreviewResponse>(
          `/discovery/persons/${personId}/source-id/preview?source=${encodeURIComponent(source)}&value=${encodeURIComponent(input)}`,
        );
        setPreview(r);
      } catch (e) {
        if (!api.isAbort(e)) console.error(e);
      }
    }, 200);
    return () => {
      if (debounceRef.current) {
        window.clearTimeout(debounceRef.current);
        debounceRef.current = null;
      }
    };
  }, [personId, source, input]);

  const commit = async (clear: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.patch<PatchResponse>(
        `/discovery/persons/${personId}/source-id`,
        { source, value: clear ? "" : input },
      );
      const action = r.parsed
        ? `Set ${SOURCE_LABELS[source]} = ${r.parsed}`
        : `Cleared ${SOURCE_LABELS[source]}`;
      toast.success(`${action} (${r.mirrored_rows} libraries updated)`);
      await onSaved();
    } catch (e) {
      setErr((e as Error).message || "Update failed");
      setBusy(false);
    }
  };

  const isEmpty = !input.trim();
  const parseFailed = !isEmpty && preview && preview.parsed === null;
  const canSave = !isEmpty && !parseFailed && preview?.parsed;
  const hasExistingValue = !!currentValue;

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: t.bg,
          color: t.text,
          padding: 20,
          borderRadius: 8,
          minWidth: 420,
          maxWidth: 560,
          border: `1px solid ${t.border}`,
          boxShadow: "0 8px 32px rgba(0,0,0,0.4)",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            marginBottom: 12,
          }}
        >
          <span
            style={{
              display: "inline-block",
              padding: "3px 10px",
              borderRadius: 5,
              background: palette.bg,
              color: palette.fg,
              border: `1px solid ${palette.br}`,
              fontSize: 13,
              fontWeight: 700,
            }}
          >
            {SOURCE_LABELS[source]}
          </span>
          <h3 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>
            Author ID
          </h3>
        </div>
        <p style={{ fontSize: 13, color: t.td, margin: "0 0 12px 0" }}>
          Paste the author ID or a full URL. Live preview shows the
          parsed canonical form before save.
        </p>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="e.g. B001IGFHW6 or https://amazon.com/stores/.../author/B001IGFHW6"
          autoFocus
          style={{
            width: "100%",
            padding: 8,
            background: t.bg2,
            color: t.text,
            border: `1px solid ${t.border}`,
            borderRadius: 4,
            fontSize: 13,
            boxSizing: "border-box",
          }}
        />
        <div style={{ marginTop: 10, minHeight: 40, fontSize: 12 }}>
          {isEmpty ? (
            <span style={{ color: t.td }}>
              Empty value will clear this ID across every linked library.
            </span>
          ) : parseFailed ? (
            <span style={{ color: t.redt }}>
              Unrecognized — please paste an ID or a {SOURCE_LABELS[source]} URL.
            </span>
          ) : preview?.parsed ? (
            <div>
              <div>
                <strong>Parsed as:</strong>{" "}
                <code
                  style={{
                    background: t.bg2,
                    padding: "1px 4px",
                    borderRadius: 3,
                    color: palette.fg,
                  }}
                >
                  {preview.parsed}
                </code>
              </div>
              {preview.url && (
                <div style={{ marginTop: 4 }}>
                  <strong>URL:</strong>{" "}
                  <a
                    href={preview.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{ color: palette.fg }}
                  >
                    {preview.url} ↗
                  </a>
                </div>
              )}
            </div>
          ) : (
            <span style={{ opacity: 0.5 }}>Parsing…</span>
          )}
        </div>
        {err && (
          <div style={{ color: t.redt, fontSize: 12, marginTop: 8 }}>
            {err}
          </div>
        )}
        <div
          style={{
            marginTop: 16,
            display: "flex",
            gap: 8,
            justifyContent: "flex-end",
            alignItems: "center",
          }}
        >
          {hasExistingValue ? (
            <button
              type="button"
              onClick={() => commit(true)}
              disabled={busy}
              style={{
                padding: "6px 12px",
                background: "transparent",
                color: t.redt,
                border: `1px solid ${t.red || t.border}`,
                borderRadius: 4,
                cursor: busy ? "wait" : "pointer",
                marginRight: "auto",
              }}
              title="Remove this ID from every linked library"
            >
              Remove
            </button>
          ) : null}
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            style={{
              padding: "6px 12px",
              background: t.bg2,
              color: t.text,
              border: `1px solid ${t.border}`,
              borderRadius: 4,
              cursor: "pointer",
            }}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => commit(false)}
            disabled={busy || !canSave}
            style={{
              padding: "6px 12px",
              background: palette.bg,
              color: palette.fg,
              border: `1px solid ${palette.br}`,
              borderRadius: 4,
              fontWeight: 600,
              cursor: busy ? "wait" : (canSave ? "pointer" : "not-allowed"),
              opacity: !canSave && !busy ? 0.5 : 1,
            }}
          >
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
