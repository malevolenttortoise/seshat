// AuthorsPage — manage allowed / ignored / tentative review lists.
//
// Three tabs share a single search box, an add-author form (allowed
// + ignored only), and a paginated table. Per-row actions:
//   - allowed   → "Move to ignored", "Remove"
//   - ignored   → "Move to allowed", "Remove"
//   - tentative → "Promote to allowed", "Send to ignored", "Remove"
//
// The "paste a list" workflow is supported by the textarea: split
// on newlines/commas, send the up-to-500-name batch as a single
// POST. The route caps at 500 per request.
import { useEffect, useState } from "react";
import { Btn } from "../components/Btn";
import { Section } from "../components/Section";
import { Spin } from "../components/Spin";
import { api } from "../api";
import { useTheme } from "../theme";

type ListName = "allowed" | "ignored" | "tentative_review";

interface AuthorRow {
  name: string;
  normalized: string;
  source: string;
  added_at: string;
}

interface ListResponse {
  list_name: ListName;
  count: number;
  items: AuthorRow[];
}

interface OverviewResponse {
  counts: Record<ListName, number>;
  samples: Record<ListName, AuthorRow[]>;
}

const TAB_LABELS: Record<ListName, string> = {
  allowed: "Allowed",
  ignored: "Ignored",
  tentative_review: "Tentative review",
};

const PAGE_SIZE = 100;

export default function AuthorsPage() {
  const theme = useTheme();
  const [tab, setTab] = useState<ListName>("allowed");
  const [counts, setCounts] = useState<Record<ListName, number> | null>(null);
  const [items, setItems] = useState<AuthorRow[] | null>(null);
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [addText, setAddText] = useState("");

  async function refreshCounts() {
    try {
      const r = await api.get<OverviewResponse>("/v1/authors");
      setCounts(r.counts);
    } catch (e) {
      setError(String(e));
    }
  }

  async function refreshList() {
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (search) params.set("search", search);
      const r = await api.get<ListResponse>(`/v1/authors/${tab}?${params}`);
      setItems(r.items);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    refreshCounts();
  }, []);

  // Re-fetch the list whenever the tab, search query, or page changes.
  // Debounced search would be nice; for typical author list sizes
  // (single thousands) the LIKE query is fast enough that the
  // immediate refresh feels live.
  useEffect(() => {
    setItems(null);
    refreshList();
  }, [tab, search, offset]);

  function changeTab(next: ListName) {
    if (next === tab) return;
    setTab(next);
    setOffset(0);
    setSearch("");
  }

  async function addAuthors() {
    if (!addText.trim()) return;
    if (tab === "tentative_review") {
      setError("Tentative review is auto-populated; cannot add manually.");
      return;
    }
    const names = addText
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter(Boolean)
      .slice(0, 500);
    if (names.length === 0) return;

    setBusy(true);
    try {
      const r = await api.post<{ added: number; skipped: number }>(
        `/v1/authors/${tab}`,
        { names },
      );
      setAddText("");
      setError(
        r.skipped > 0
          ? `Added ${r.added}, skipped ${r.skipped} (blank/duplicate).`
          : null,
      );
      await Promise.all([refreshCounts(), refreshList()]);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(row: AuthorRow) {
    setBusy(true);
    try {
      await api.del(
        `/v1/authors/${tab}/${encodeURIComponent(row.normalized)}`,
      );
      await Promise.all([refreshCounts(), refreshList()]);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function move(row: AuthorRow, to: "allowed" | "ignored") {
    setBusy(true);
    try {
      await api.post<{ ok: boolean }>(
        `/v1/authors/${tab}/${encodeURIComponent(row.normalized)}/move`,
        { to },
      );
      await Promise.all([refreshCounts(), refreshList()]);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <h1
        style={{
          fontSize: 24,
          fontWeight: 700,
          color: theme.text,
          marginBottom: 4,
        }}
      >
        Authors
      </h1>
      <p style={{ fontSize: 14, color: theme.textDim, marginBottom: 20 }}>
        Manage the allow / ignore lists that gate every announce. Auto-train
        adds also land here so you can audit what the pipeline learned.
      </p>

      {error && (
        <div
          style={{
            background: theme.warn + "22",
            border: `1px solid ${theme.warn}55`,
            color: theme.warn,
            padding: "10px 14px",
            borderRadius: 8,
            fontSize: 13,
            marginBottom: 16,
          }}
        >
          {error}
        </div>
      )}

      {/* Tab strip */}
      <div
        style={{
          display: "flex",
          gap: 4,
          marginBottom: 16,
          borderBottom: `1px solid ${theme.borderL}`,
        }}
      >
        {(Object.keys(TAB_LABELS) as ListName[]).map((id) => {
          const active = id === tab;
          const count = counts?.[id];
          return (
            <button
              key={id}
              onClick={() => changeTab(id)}
              style={{
                background: "transparent",
                border: "none",
                borderBottom: `2px solid ${active ? theme.accent : "transparent"}`,
                color: active ? theme.accent : theme.text2,
                padding: "10px 16px",
                fontSize: 14,
                fontWeight: 600,
                cursor: "pointer",
                marginBottom: -1,
              }}
            >
              {TAB_LABELS[id]}
              {count !== undefined && (
                <span
                  style={{
                    marginLeft: 8,
                    fontSize: 12,
                    color: theme.textDim,
                    fontWeight: 500,
                  }}
                >
                  {count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Add author form (hidden for tentative_review) */}
      {tab !== "tentative_review" && (
        <Section
          title={`Add to ${TAB_LABELS[tab].toLowerCase()}`}
          subtitle="One author per line, or a comma-separated paste. Up to 500 per submit."
        >
          <textarea
            value={addText}
            onChange={(e) => setAddText(e.target.value)}
            rows={3}
            placeholder="Brandon Sanderson&#10;Isaac Asimov&#10;..."
            style={{
              width: "100%",
              padding: "10px 12px",
              borderRadius: 8,
              border: `1px solid ${theme.border}`,
              background: theme.inp,
              color: theme.text,
              fontSize: 13,
              resize: "vertical",
              fontFamily: "inherit",
              outline: "none",
            }}
          />
          <div
            style={{
              marginTop: 10,
              display: "flex",
              justifyContent: "flex-end",
              gap: 8,
            }}
          >
            <Btn variant="ghost" onClick={() => setAddText("")} disabled={busy}>
              Clear
            </Btn>
            <Btn
              variant="primary"
              onClick={addAuthors}
              disabled={busy || !addText.trim()}
            >
              {busy ? <Spin size={14} /> : "Add"}
            </Btn>
          </div>
        </Section>
      )}

      {/* Search + list */}
      <Section
        title={`${TAB_LABELS[tab]} list`}
        subtitle={
          counts?.[tab] !== undefined
            ? `${counts[tab]} total`
            : undefined
        }
        right={
          <input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setOffset(0);
            }}
            placeholder="Search…"
            style={{
              padding: "6px 10px",
              borderRadius: 8,
              border: `1px solid ${theme.border}`,
              background: theme.inp,
              color: theme.text,
              fontSize: 13,
              width: 200,
              outline: "none",
            }}
          />
        }
      >
        {items === null ? (
          <div style={{ display: "flex", justifyContent: "center", padding: 20 }}>
            <Spin />
          </div>
        ) : items.length === 0 ? (
          <p style={{ fontSize: 13, color: theme.textDim }}>
            {search ? "No matches." : "Empty."}
          </p>
        ) : (
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: 13,
            }}
          >
            <thead>
              <tr
                style={{
                  textAlign: "left",
                  color: theme.textDim,
                  fontWeight: 600,
                  fontSize: 11,
                  textTransform: "uppercase",
                  letterSpacing: 0.4,
                }}
              >
                <th style={{ padding: "8px 6px" }}>Name</th>
                <th style={{ padding: "8px 6px" }}>Source</th>
                <th
                  style={{
                    padding: "8px 6px",
                    textAlign: "right",
                    width: 220,
                  }}
                >
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => (
                <tr
                  key={row.normalized}
                  style={{ borderTop: `1px solid ${theme.borderL}` }}
                >
                  <td style={{ padding: "8px 6px", color: theme.text }}>
                    {row.name}
                  </td>
                  <td style={{ padding: "8px 6px", color: theme.textDim }}>
                    {row.source}
                  </td>
                  <td style={{ padding: "8px 6px", textAlign: "right" }}>
                    <RowActions
                      tab={tab}
                      busy={busy}
                      onMove={(to) => move(row, to)}
                      onRemove={() => remove(row)}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {items && items.length === PAGE_SIZE && (
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              marginTop: 12,
            }}
          >
            <Btn
              variant="ghost"
              disabled={offset === 0 || busy}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              ← Previous
            </Btn>
            <Btn
              variant="ghost"
              disabled={busy}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next →
            </Btn>
          </div>
        )}
      </Section>
    </div>
  );
}

function RowActions({
  tab,
  busy,
  onMove,
  onRemove,
}: {
  tab: ListName;
  busy: boolean;
  onMove: (to: "allowed" | "ignored") => void;
  onRemove: () => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        gap: 6,
        justifyContent: "flex-end",
        flexWrap: "wrap",
      }}
    >
      {tab === "allowed" && (
        <Btn variant="secondary" disabled={busy} onClick={() => onMove("ignored")}>
          Ignore
        </Btn>
      )}
      {tab === "ignored" && (
        <Btn variant="secondary" disabled={busy} onClick={() => onMove("allowed")}>
          Allow
        </Btn>
      )}
      {tab === "tentative_review" && (
        <>
          <Btn
            variant="primary"
            disabled={busy}
            onClick={() => onMove("allowed")}
          >
            Allow
          </Btn>
          <Btn variant="danger" disabled={busy} onClick={() => onMove("ignored")}>
            Ignore
          </Btn>
        </>
      )}
      <Btn variant="ghost" disabled={busy} onClick={onRemove}>
        Remove
      </Btn>
    </div>
  );
}
