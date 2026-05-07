// v2.3 Series Manager page.
//
// Lists every series across the active library with shared/per-author
// indicator, contributor count, and book count. Supports the four
// management actions exposed by the backend:
//
//   - Promote: select 2+ per-author rows, merge into one shared row
//     (covers Halo cases the auto-detect missed because the books
//     aren't yet in Calibre).
//   - Demote: split a shared row back into per-author rows.
//   - Rename: change the series name in place. 409 surfaces the
//     conflict id so the user can opt to merge into the existing.
//   - Delete: remove the series; books fall back to standalone.
//
// Membership editing (add/remove individual books) lives in the
// existing book-detail sidebar — the Series Manager focuses on
// row-level structural changes that can't be done one-book-at-a-time.

import { useEffect, useMemo, useState } from "react";
import { useTheme } from "../theme";
import { api } from "../api";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { Load } from "../components/Load";
import { usePersist } from "../hooks/usePersist";

interface SeriesRow {
  id: number;
  name: string;
  author_id: number | null;
  author_name: string | null;
  book_count: number;
  owned_count: number;
  missing_count: number;
  multi_author: number;
  is_shared: number;
  contributor_count: number;
}

interface SeriesListResponse {
  series: SeriesRow[];
}

type FilterMode = "all" | "shared" | "per-author";

export default function SeriesManagerPage() {
  const t = useTheme();
  const [filter, setFilter] = usePersist<FilterMode>("sm_filter", "all");
  const [search, setSearch] = useState("");
  const [data, setData] = useState<SeriesListResponse | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState<Record<number, string>>({});
  const [bulkBusy, setBulkBusy] = useState(false);

  const load = () => {
    setData(null);
    const params = new URLSearchParams();
    if (search.trim()) params.set("search", search.trim());
    if (filter === "shared") params.set("shared", "true");
    if (filter === "per-author") params.set("shared", "false");
    api
      .get<SeriesListResponse>(`/discovery/series?${params}`)
      .then(setData)
      .catch((e) => {
        console.error(e);
        setData({ series: [] });
      });
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  const onSearch = (e: React.FormEvent) => {
    e.preventDefault();
    load();
  };

  // Selected per-author rows that share a name are eligible for
  // promote. We surface the count + warn if names don't match.
  const selectedRows = useMemo(() => {
    if (!data) return [] as SeriesRow[];
    return data.series.filter((s) => selected.has(s.id));
  }, [data, selected]);

  const promoteEligible = useMemo(() => {
    if (selectedRows.length < 2) return false;
    if (selectedRows.some((r) => r.is_shared === 1)) return false;
    return true;
  }, [selectedRows]);

  const promoteNamesMatch = useMemo(() => {
    if (selectedRows.length < 2) return false;
    const first = selectedRows[0].name.toLowerCase();
    return selectedRows.every((r) => r.name.toLowerCase() === first);
  }, [selectedRows]);

  const toggleSelected = (id: number) => {
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  };

  const clearSelection = () => setSelected(new Set());

  const promote = async () => {
    if (!promoteEligible) return;
    const name = promoteNamesMatch
      ? selectedRows[0].name
      : window.prompt(
          "Selected rows have different names. What name should the shared series use?",
          selectedRows[0].name,
        );
    if (!name) return;
    setBulkBusy(true);
    try {
      const res = await api.post<{
        shared_id: number;
        promoted_from: number[];
        books_moved: number;
      }>("/discovery/series/promote", {
        series_ids: selectedRows.map((r) => r.id),
        name,
      });
      alert(
        `Promoted ${res.promoted_from.length} rows into shared series id=${res.shared_id}; ${res.books_moved} books moved.`,
      );
      clearSelection();
      load();
    } catch (e) {
      alert(`Promote failed: ${(e as Error).message || e}`);
    } finally {
      setBulkBusy(false);
    }
  };

  const demote = async (s: SeriesRow) => {
    if (
      !window.confirm(
        `Split "${s.name}" into ${s.contributor_count} per-author rows?`,
      )
    )
      return;
    setBusy((b) => ({ ...b, [s.id]: "demote" }));
    try {
      await api.post(`/discovery/series/${s.id}/demote`);
      load();
    } catch (e) {
      alert(`Demote failed: ${(e as Error).message || e}`);
    } finally {
      setBusy((b) => {
        const n = { ...b };
        delete n[s.id];
        return n;
      });
    }
  };

  const rename = async (s: SeriesRow) => {
    const next = window.prompt(`Rename "${s.name}" to:`, s.name);
    if (!next || next.trim() === s.name) return;
    setBusy((b) => ({ ...b, [s.id]: "rename" }));
    try {
      await api.patch(`/discovery/series/${s.id}`, { name: next.trim() });
      load();
    } catch (e) {
      alert(`Rename failed: ${(e as Error).message || e}`);
    } finally {
      setBusy((b) => {
        const n = { ...b };
        delete n[s.id];
        return n;
      });
    }
  };

  const remove = async (s: SeriesRow) => {
    if (
      !window.confirm(
        `Delete "${s.name}"? ${s.book_count} book(s) will fall back to standalone.`,
      )
    )
      return;
    setBusy((b) => ({ ...b, [s.id]: "delete" }));
    try {
      await api.del(`/discovery/series/${s.id}`);
      setSelected((sel) => {
        const n = new Set(sel);
        n.delete(s.id);
        return n;
      });
      load();
    } catch (e) {
      alert(`Delete failed: ${(e as Error).message || e}`);
    } finally {
      setBusy((b) => {
        const n = { ...b };
        delete n[s.id];
        return n;
      });
    }
  };

  if (data === null) return <Load />;

  const series = data.series || [];
  const filterTabs: { id: FilterMode; label: string }[] = [
    { id: "all", label: "All" },
    { id: "per-author", label: "Per-Author" },
    { id: "shared", label: "Shared" },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {/* Header */}
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
          <span style={{ fontSize: 22 }}>🗂️</span> Series Manager
        </h1>
        <p style={{ fontSize: 14, color: t.td, marginTop: 4 }}>
          Promote per-author series rows into shared rows (Halo, Star Wars,
          franchise novels), split a shared row back into per-author, rename,
          or delete. Calibre-organized shared series are auto-detected on
          sync; this page covers the cases where Calibre's organization
          doesn't tell us — source-discovered books not yet acquired,
          coincidentally-named series that were merged in error, or undoing a
          previous decision.
        </p>
      </div>

      {/* Filter tabs */}
      <div
        style={{
          display: "flex",
          gap: 6,
          borderBottom: `1px solid ${t.borderL}`,
        }}
      >
        {filterTabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setFilter(tab.id)}
            style={{
              padding: "10px 16px",
              background: "none",
              border: "none",
              borderBottom:
                filter === tab.id
                  ? `2px solid ${t.accent}`
                  : "2px solid transparent",
              color: filter === tab.id ? t.accent : t.tf,
              fontWeight: filter === tab.id ? 600 : 500,
              fontSize: 14,
              cursor: "pointer",
              marginBottom: -1,
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Search + bulk action bar */}
      <div
        style={{
          display: "flex",
          gap: 12,
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <form onSubmit={onSearch} style={{ flex: "1 1 300px", maxWidth: 400 }}>
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by series or author name..."
            style={{
              width: "100%",
              padding: "8px 12px",
              fontSize: 14,
              background: t.bg2,
              color: t.text,
              border: `1px solid ${t.border}`,
              borderRadius: 6,
            }}
          />
        </form>

        {selected.size > 0 ? (
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span style={{ fontSize: 13, color: t.td }}>
              {selected.size} selected
            </span>
            <Btn
              onClick={promote}
              disabled={!promoteEligible || bulkBusy}
              variant="primary"
              size="sm"
            >
              {bulkBusy ? <Spin /> : null} Promote to shared
            </Btn>
            <Btn onClick={clearSelection} variant="ghost" size="sm">
              Clear
            </Btn>
          </div>
        ) : null}
      </div>

      {/* Empty state */}
      {series.length === 0 ? (
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
          <div style={{ fontSize: 14 }}>No series match the current filter.</div>
        </div>
      ) : null}

      {/* Table */}
      {series.length > 0 ? (
        <div
          style={{
            background: t.bg2,
            border: `1px solid ${t.border}`,
            borderRadius: 12,
            overflow: "hidden",
          }}
        >
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: 14,
            }}
          >
            <thead>
              <tr style={{ background: t.bg, color: t.tf }}>
                <th style={hth(t)}></th>
                <th style={hth(t)}>Name</th>
                <th style={hth(t)}>Author</th>
                <th style={hth(t)}>Books</th>
                <th style={hth(t)}>Type</th>
                <th style={{ ...hth(t), textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {series.map((s) => {
                const busyAction = busy[s.id];
                const isShared = s.is_shared === 1;
                return (
                  <tr
                    key={s.id}
                    style={{ borderTop: `1px solid ${t.borderL}` }}
                  >
                    <td style={td(t)}>
                      <input
                        type="checkbox"
                        checked={selected.has(s.id)}
                        onChange={() => toggleSelected(s.id)}
                        disabled={isShared}
                        title={
                          isShared
                            ? "shared rows can't be promoted"
                            : "select for bulk promote"
                        }
                      />
                    </td>
                    <td style={{ ...td(t), color: t.text, fontWeight: 500 }}>
                      {s.name}
                    </td>
                    <td style={{ ...td(t), color: t.tf }}>
                      {isShared ? (
                        <span style={{ color: t.accent }}>
                          shared ({s.contributor_count} authors)
                        </span>
                      ) : (
                        s.author_name || "—"
                      )}
                    </td>
                    <td style={{ ...td(t), color: t.tf }}>
                      {s.book_count} ({s.owned_count} owned, {s.missing_count}{" "}
                      missing)
                    </td>
                    <td style={td(t)}>
                      <span
                        style={{
                          display: "inline-block",
                          padding: "2px 8px",
                          borderRadius: 4,
                          fontSize: 12,
                          background: isShared ? t.abg : t.bg,
                          color: isShared ? t.accent : t.tf,
                          border: `1px solid ${isShared ? t.abr : t.border}`,
                        }}
                      >
                        {isShared ? "Shared" : "Per-Author"}
                      </span>
                    </td>
                    <td style={{ ...td(t), textAlign: "right" }}>
                      <div
                        style={{
                          display: "inline-flex",
                          gap: 6,
                        }}
                      >
                        {isShared ? (
                          <Btn
                            onClick={() => demote(s)}
                            disabled={!!busyAction}
                            variant="ghost"
                            size="sm"
                          >
                            {busyAction === "demote" ? <Spin /> : null} Demote
                          </Btn>
                        ) : null}
                        <Btn
                          onClick={() => rename(s)}
                          disabled={!!busyAction}
                          variant="ghost"
                          size="sm"
                        >
                          {busyAction === "rename" ? <Spin /> : null} Rename
                        </Btn>
                        <Btn
                          onClick={() => remove(s)}
                          disabled={!!busyAction}
                          variant="ghost"
                          size="sm"
                        >
                          {busyAction === "delete" ? <Spin /> : null} Delete
                        </Btn>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

function hth(t: ReturnType<typeof useTheme>): React.CSSProperties {
  return {
    padding: "10px 14px",
    textAlign: "left",
    fontWeight: 600,
    fontSize: 13,
    color: t.tf,
    borderBottom: `1px solid ${t.border}`,
  };
}

function td(t: ReturnType<typeof useTheme>): React.CSSProperties {
  return {
    padding: "10px 14px",
    color: t.tf,
  };
}
