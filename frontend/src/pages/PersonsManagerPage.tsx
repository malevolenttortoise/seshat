/* eslint-disable @typescript-eslint/no-explicit-any */
// PersonsManagerPage — v2.22.0
//
// Central place to edit cross-library source IDs for a person. The
// API (PATCH /discovery/persons/{id}/source-id) writes through to
// every linked library row via `mirror_source_id`, so editing once
// here updates all libraries.
//
// Multi-link persons only — single-library persons have nothing to
// manage across libraries.

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { toast } from "../lib/toast";
import { useTheme } from "../theme";
import type { NavFn } from "../types";

interface PersonLink {
  library_slug: string;
  author_id: number;
  author_name?: string | null;
}

interface PersonRow {
  person_id: number;
  canonical_name: string;
  display_name: string;
  normalized_name: string;
  links: PersonLink[];
  source_ids: Record<string, string | null>;
  divergent: string[];
}

interface ListResponse {
  persons: PersonRow[];
}

const SOURCE_LABELS: Record<string, string> = {
  amazon_id: "Amazon",
  goodreads_id: "Goodreads",
  hardcover_id: "Hardcover",
  audible_id: "Audible",
  kobo_id: "Kobo",
  openlibrary_id: "OpenLibrary",
  google_books_id: "Google Books",
  ibdb_id: "IBDb",
  fictiondb_id: "FictionDB",
};

const SOURCE_ORDER = [
  "goodreads_id", "amazon_id", "hardcover_id", "audible_id",
  "openlibrary_id", "google_books_id", "kobo_id", "ibdb_id",
  "fictiondb_id",
];

function sourceKey(col: string): string {
  // "amazon_id" → "amazon" for the PATCH body.
  return col.endsWith("_id") ? col.slice(0, -3) : col;
}

interface PersonsManagerPageProps {
  onNav?: NavFn;
}

export default function PersonsManagerPage({ onNav }: PersonsManagerPageProps) {
  const t = useTheme();
  const [data, setData] = useState<ListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [onlyDivergent, setOnlyDivergent] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.get<ListResponse>("/discovery/persons/source-ids");
      setData(r);
    } catch (e) {
      toast.error((e as Error).message || "Failed to load persons");
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    return data.persons.filter((p) => {
      if (onlyDivergent && p.divergent.length === 0) return false;
      if (!q) return true;
      return (
        p.display_name.toLowerCase().includes(q) ||
        p.normalized_name.toLowerCase().includes(q)
      );
    });
  }, [data, query, onlyDivergent]);

  const saveSourceId = async (
    person_id: number, column: string, raw: string,
  ) => {
    const busyKey = `${person_id}:${column}`;
    setBusy(busyKey);
    try {
      const r = await api.patch<{
        parsed: string | null;
        mirrored_rows: number;
      }>(`/discovery/persons/${person_id}/source-id`, {
        source: sourceKey(column),
        value: raw,
      });
      toast.success(
        `${SOURCE_LABELS[column] ?? column}: ${r.parsed ?? "cleared"} ` +
        `· ${r.mirrored_rows} row(s) updated`,
      );
      await refresh();
    } catch (e) {
      toast.error((e as Error).message || "Save failed");
    }
    setBusy(null);
  };

  if (loading) {
    return (
      <div style={{ padding: 20, color: t.td }}>Loading persons…</div>
    );
  }
  if (!data) {
    return (
      <div style={{ padding: 20, color: t.redt }}>Failed to load.</div>
    );
  }

  return (
    <div style={{ padding: 16, color: t.td }}>
      <h2 style={{ marginTop: 0, color: t.text }}>Persons &amp; Source IDs</h2>
      <p style={{ color: t.tg, fontSize: 13, marginTop: 0 }}>
        Multi-library persons in the identity graph. Edit a source ID
        here and it writes through to every linked library row via{" "}
        <code>mirror_source_id</code>. Conflicts (siblings with
        different values) are highlighted — these are auto-resolved
        by Hygiene Job 8 (ebook wins), but a manual edit here lets
        you override the policy.
      </p>

      <div style={{ display: "flex", gap: 10, margin: "12px 0", alignItems: "center" }}>
        <input
          type="search"
          placeholder="Filter by name…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{
            padding: "6px 8px", background: t.bg, color: t.td,
            border: `1px solid ${t.border}`, borderRadius: 4, minWidth: 220,
          }}
        />
        <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 6 }}>
          <input
            type="checkbox"
            checked={onlyDivergent}
            onChange={(e) => setOnlyDivergent(e.target.checked)}
          />
          Only show divergent
        </label>
        <span style={{ marginLeft: "auto", color: t.tg, fontSize: 12 }}>
          {filtered.length} of {data.persons.length} persons
        </span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {filtered.map((p) => (
          <div
            key={p.person_id}
            style={{
              border: `1px solid ${
                p.divergent.length ? t.redt : t.border
              }`,
              borderRadius: 6, padding: 12, background: t.bg,
            }}
          >
            <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
              <span style={{ fontWeight: 600, color: t.text }}>
                {p.display_name}
              </span>
              <span style={{ fontSize: 11, color: t.tg }}>
                person #{p.person_id} · normalized <code>{p.normalized_name}</code>
              </span>
              {p.divergent.length > 0 && (
                <span style={{
                  marginLeft: "auto", fontSize: 11, color: t.redt,
                }}>
                  ⚠ divergent: {p.divergent.map(c => SOURCE_LABELS[c] ?? c).join(", ")}
                </span>
              )}
              {onNav && (
                <button
                  type="button"
                  onClick={() => onNav("disc-author-detail", p.links[0]?.author_id)}
                  style={{
                    fontSize: 11, padding: "2px 8px",
                    background: t.bg2, color: t.td,
                    border: `1px solid ${t.border}`, borderRadius: 3,
                    cursor: "pointer",
                  }}
                >
                  Detail →
                </button>
              )}
            </div>

            <div style={{ fontSize: 11, color: t.tg, margin: "4px 0 8px" }}>
              Linked rows:{" "}
              {p.links.map((l, i) => (
                <span key={`${l.library_slug}:${l.author_id}`}>
                  {i > 0 && " · "}
                  <code>{l.library_slug}</code>:{" "}
                  {l.author_name ?? `#${l.author_id}`}
                </span>
              ))}
            </div>

            <div style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
              gap: 8,
            }}>
              {SOURCE_ORDER.map((col) => (
                <SourceIdField
                  key={col}
                  label={SOURCE_LABELS[col] ?? col}
                  initial={p.source_ids[col] ?? ""}
                  divergent={p.divergent.includes(col)}
                  busy={busy === `${p.person_id}:${col}`}
                  onSave={(v) => saveSourceId(p.person_id, col, v)}
                  t={t}
                />
              ))}
            </div>
          </div>
        ))}
        {filtered.length === 0 && (
          <div style={{ color: t.tg, fontStyle: "italic", padding: 8 }}>
            No persons match the current filters.
          </div>
        )}
      </div>
    </div>
  );
}

function SourceIdField({
  label, initial, divergent, busy, onSave, t,
}: {
  label: string;
  initial: string;
  divergent: boolean;
  busy: boolean;
  onSave: (v: string) => void | Promise<void>;
  t: any;
}) {
  const [val, setVal] = useState(initial);
  useEffect(() => { setVal(initial); }, [initial]);
  const dirty = val !== initial;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <label style={{
        fontSize: 10, color: divergent ? t.redt : t.tg,
        fontWeight: divergent ? 600 : 400,
      }}>
        {label}{divergent ? " (conflict)" : ""}
      </label>
      <div style={{ display: "flex", gap: 4 }}>
        <input
          type="text"
          value={val}
          onChange={(e) => setVal(e.target.value)}
          placeholder="—"
          disabled={busy}
          style={{
            flex: 1, padding: "4px 6px", fontSize: 12,
            background: t.bg2, color: t.td,
            border: `1px solid ${dirty ? t.accent : t.border}`,
            borderRadius: 3,
          }}
        />
        {dirty && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onSave(val)}
            style={{
              fontSize: 11, padding: "2px 8px",
              background: t.accent, color: "#fff",
              border: "none", borderRadius: 3, cursor: "pointer",
            }}
          >
            {busy ? "…" : "Save"}
          </button>
        )}
      </div>
    </div>
  );
}
