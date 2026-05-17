// DatabasePage — SQLite browser with inline cell editing + row delete.
//
// Left pane: whitelisted table list with row counts. Right pane: paginated
// grid with case-insensitive search across TEXT columns. v1.2 added click-
// to-edit cells, a sticky pending-changes tray (commit or revert the whole
// batch at once), and a per-row delete action.
//
// Writes go through POST /api/v1/db/table/{name}/update and DELETE
// /api/v1/db/table/{name}/row/{id}. Every edit is type-coerced + NOT NULL
// validated server-side; the pending tray surfaces any returned errors in
// place of a blind optimistic update.
import { useEffect, useRef, useState } from "react";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { api } from "../api";
import { useTheme } from "../theme";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import MobileDatabasePage from "./MobileDatabasePage";

interface TableEntry {
  name: string;
  row_count: number;
}

interface TablesResponse {
  tables: TableEntry[];
}

interface RowsResponse {
  table: string;
  total: number;
  page: number;
  per_page: number;
  rows: Record<string, unknown>[];
}

interface ColumnInfo {
  name: string;
  type: string;
  not_null: boolean;
  primary_key: boolean;
}

interface SchemaResponse {
  table: string;
  columns: ColumnInfo[];
}

interface UpdateResponse {
  status: string;
  updated?: number;
  errors?: { row: string | number; column: string; error: string }[];
}

const PER_PAGE = 50;

// Key used in the per-row edits map. We accept string OR number because the
// primary key could be either INTEGER or TEXT; stringify on read to make
// lookups consistent.
type RowKey = string;
type PendingEdits = Record<RowKey, Record<string, unknown>>;

// v2.14.x #F — per-column max-widths. Unbounded ellipsis on every cell
// (the pre-#F default of 320px-everywhere) made wide text columns push
// numeric columns off-screen. Tighter caps mean the table fits more
// columns into the viewport; the hover title still shows the full
// value when ellipsis fires.
const WIDE_TEXT_COLS = new Set([
  "description", "source_url", "cover_url", "cover_phash", "bio",
  "audio_formats", "file_path", "image_url", "mam_url", "formats",
  "tags", "raw",
]);
function colMaxWidth(col: string, type: string | undefined): number {
  const t = (type || "").toUpperCase();
  if (
    t.includes("INT") || t.includes("REAL") ||
    t.includes("NUMERIC") || t.includes("FLOAT") || t.includes("DOUBLE")
  ) return 90;
  if (WIDE_TEXT_COLS.has(col)) return 280;
  return 180;
}

const HIDDEN_COLS_KEY = (table: string) => `seshat:db-hidden-cols:${table}`;

export default function DatabasePage() {
  const vp = useViewport();
  if (useMobileCodepath(vp)) return <MobileDatabasePage />;
  return <DesktopDatabasePage />;
}

function DesktopDatabasePage() {
  const t = useTheme();
  const [tables, setTables] = useState<TableEntry[] | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [rows, setRows] = useState<Record<string, unknown>[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Pending batch of cell edits keyed by row PK. Cleared on table switch,
  // refresh, or successful save. Displayed cells read from here when a
  // pending edit exists for that (row, col) pair.
  const [edits, setEdits] = useState<PendingEdits>({});
  // Cell currently being edited (focus target). `null` means no active
  // editor — the cell shows its value as plain text.
  const [focusCell, setFocusCell] = useState<{ rowKey: RowKey; col: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<RowKey | null>(null);

  // v2.14.x #F — sort + column-visibility state. `sort=null` means
  // natural insert order (matches pre-#F behavior). Hidden columns
  // persist per-table in localStorage so users don't have to re-hide
  // the giant `description` column every visit.
  const [sort, setSort] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [hiddenCols, setHiddenCols] = useState<Set<string>>(new Set());
  const [colMenuOpen, setColMenuOpen] = useState(false);

  useEffect(() => {
    api.get<TablesResponse>("/v1/db/tables")
      .then((r) => {
        setTables(r.tables);
        const firstNonEmpty = r.tables.find((x) => x.row_count > 0);
        setSelected(firstNonEmpty?.name || (r.tables[0]?.name ?? ""));
      })
      .catch((e) => setError(String(e)));
  }, []);

  // Fetch schema whenever the selected table changes. The PK column drives
  // which cells are editable (and which identifies a row for updates).
  useEffect(() => {
    if (!selected) return;
    setSchema(null);
    api.get<SchemaResponse>(`/v1/db/table/${selected}/schema`)
      .then(setSchema)
      .catch((e) => setError(String(e)));
  }, [selected]);

  useEffect(() => {
    if (!selected) return;
    setLoading(true);
    const params = new URLSearchParams({
      page: String(page),
      per_page: String(PER_PAGE),
    });
    if (search.trim()) params.set("search", search.trim());
    if (sort) {
      params.set("sort", sort);
      params.set("sort_dir", sortDir);
    }
    api.get<RowsResponse>(`/v1/db/table/${selected}?${params}`)
      .then((r) => {
        setRows(r.rows);
        setTotal(r.total);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [selected, page, search, sort, sortDir]);

  // Clear pending edits on table switch or search/page/sort change — saving
  // across a different view would be surprising.
  useEffect(() => { setEdits({}); setFocusCell(null); }, [selected, page, search, sort, sortDir]);

  // Click-outside-to-close for the Columns visibility menu. Without
  // this, the menu stays open until the user clicks the trigger again,
  // which is fine but slightly annoying when they just want to pick
  // one column and move on.
  const colMenuRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!colMenuOpen) return;
    function onDown(e: MouseEvent) {
      if (!colMenuRef.current) return;
      if (e.target instanceof Node && colMenuRef.current.contains(e.target)) return;
      setColMenuOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [colMenuOpen]);

  // Reset sort + load saved column-visibility whenever the table changes.
  // Hidden-cols are per-table so the user's last-set visibility for THIS
  // table comes back on revisit.
  useEffect(() => {
    if (!selected) return;
    setSort(null);
    setSortDir("asc");
    try {
      const raw = localStorage.getItem(HIDDEN_COLS_KEY(selected));
      const parsed = raw ? JSON.parse(raw) : [];
      setHiddenCols(new Set(Array.isArray(parsed) ? parsed : []));
    } catch {
      setHiddenCols(new Set());
    }
  }, [selected]);

  const pkCol = schema?.columns.find((c) => c.primary_key)?.name;
  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE));
  const allColumns = rows && rows.length > 0 ? Object.keys(rows[0]) : [];
  // After hidden-cols filter — what actually renders in thead/tbody.
  // PK is never hideable so the row-action column has a stable anchor.
  const columns = allColumns.filter((c) => !hiddenCols.has(c) || c === pkCol);
  const pendingCount = Object.values(edits).reduce(
    (acc, r) => acc + Object.keys(r).length, 0,
  );

  // Sort header click → cycle natural → asc → desc → natural.
  function onSortClick(col: string) {
    if (sort !== col) { setSort(col); setSortDir("asc"); setPage(1); return; }
    if (sortDir === "asc") { setSortDir("desc"); setPage(1); return; }
    setSort(null); setSortDir("asc"); setPage(1);
  }

  function toggleColHidden(col: string) {
    if (col === pkCol) return; // PK always visible
    setHiddenCols((prev) => {
      const next = new Set(prev);
      if (next.has(col)) next.delete(col); else next.add(col);
      try {
        localStorage.setItem(
          HIDDEN_COLS_KEY(selected),
          JSON.stringify(Array.from(next)),
        );
      } catch { /* localStorage full / disabled — drop silently */ }
      return next;
    });
  }

  function showAllCols() {
    setHiddenCols(new Set());
    try { localStorage.removeItem(HIDDEN_COLS_KEY(selected)); } catch {
      /* drop silently */
    }
  }

  // Pagination JSX hoisted as a const so it can render BOTH above and
  // below the table without code duplication. Two mounted copies share
  // state through the closure (page, totalPages, loading, setPage).
  const pagerJsx = (
    <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
      <Btn variant="ghost" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1 || loading}>←</Btn>
      <span style={{ fontSize: 12, color: t.textDim, padding: "0 8px" }}>
        {page} / {totalPages}
      </span>
      <Btn variant="ghost" onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page >= totalPages || loading}>→</Btn>
    </div>
  );

  function rowKey(row: Record<string, unknown>): RowKey {
    if (!pkCol) return "";
    const v = row[pkCol];
    return v === null || v === undefined ? "" : String(v);
  }

  function cellValue(row: Record<string, unknown>, col: string): unknown {
    const rk = rowKey(row);
    if (rk && edits[rk] && col in edits[rk]) return edits[rk][col];
    return row[col];
  }

  function setPending(row: Record<string, unknown>, col: string, val: unknown) {
    const rk = rowKey(row);
    if (!rk) return;
    setEdits((prev) => {
      const prevRow = prev[rk] || {};
      const original = row[col];
      // If the user edits back to the original value, drop the pending
      // entry instead of recording a no-op.
      const isNoOp = original === val
        || (original == null && (val === "" || val == null));
      const nextRow = { ...prevRow };
      if (isNoOp) delete nextRow[col];
      else nextRow[col] = val;
      const next = { ...prev };
      if (Object.keys(nextRow).length === 0) delete next[rk];
      else next[rk] = nextRow;
      return next;
    });
  }

  async function saveAll() {
    if (pendingCount === 0) return;
    setSaving(true);
    setError(null);
    try {
      const r = await api.post<UpdateResponse>(
        `/v1/db/table/${selected}/update`,
        { edits },
      );
      if (r.status !== "ok") {
        const errs = (r.errors || []).map(
          (e) => `row ${e.row} · ${e.column}: ${e.error}`,
        ).join("\n");
        setError(errs || "update failed");
        return;
      }
      setEdits({});
      setFocusCell(null);
      // Refresh the rows so pending-colored cells revert to plain display
      // with the committed values. Preserve the active sort so the row
      // order doesn't jump under the user post-commit.
      const params = new URLSearchParams({
        page: String(page), per_page: String(PER_PAGE),
      });
      if (search.trim()) params.set("search", search.trim());
      if (sort) {
        params.set("sort", sort);
        params.set("sort_dir", sortDir);
      }
      const fresh = await api.get<RowsResponse>(
        `/v1/db/table/${selected}?${params}`,
      );
      setRows(fresh.rows);
      setTotal(fresh.total);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function deleteRow(row: Record<string, unknown>) {
    const rk = rowKey(row);
    if (!rk) return;
    if (!confirm(`Delete row where ${pkCol} = ${rk}? This cannot be undone.`)) return;
    setDeleting(rk);
    setError(null);
    try {
      await api.del(`/v1/db/table/${selected}/row/${encodeURIComponent(rk)}`);
      setRows((prev) => (prev || []).filter((r) => rowKey(r) !== rk));
      setTotal((v) => Math.max(0, v - 1));
      setEdits((prev) => {
        const next = { ...prev };
        delete next[rk];
        return next;
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
        <h1 style={{ fontSize: 24, fontWeight: 700, color: t.text }}>Database</h1>
        <span style={{ fontSize: 12, color: t.textDim }}>
          Click a cell to edit · delete via the trash icon
        </span>
      </div>

      {error && (
        <div style={{ background: t.err + "22", border: `1px solid ${t.err}55`, color: t.err, padding: "10px 14px", borderRadius: 8, fontSize: 13, marginBottom: 16, whiteSpace: "pre-wrap" }}>
          {error}
        </div>
      )}

      {!tables ? (
        <div style={{ display: "flex", justifyContent: "center", padding: 40 }}><Spin /></div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "220px 1fr", gap: 16 }}>
          {/* Table list */}
          <div style={{ background: t.bg2, border: `1px solid ${t.borderL}`, borderRadius: 8, padding: 8, alignSelf: "start", maxHeight: "75vh", overflowY: "auto" }}>
            {(() => {
              const DISC = new Set(["authors", "series", "books", "sync_log", "mam_scan_log", "book_series_suggestions", "pen_name_links"]);
              const disc = tables.filter((x) => DISC.has(x.name));
              const pipe = tables.filter((x) => !DISC.has(x.name));
              const renderGroup = (label: string, list: TableEntry[]) => (
                <>
                  <div style={{ fontSize: 10, fontWeight: 700, color: t.textDim, textTransform: "uppercase", letterSpacing: "0.06em", padding: "8px 10px 4px" }}>{label}</div>
                  {list.map((tbl) => (
                    <button
                      key={tbl.name}
                      onClick={() => { setSelected(tbl.name); setPage(1); setSearch(""); }}
                      style={{
                        display: "flex", justifyContent: "space-between", alignItems: "center",
                        width: "100%", padding: "8px 10px", margin: "1px 0",
                        background: selected === tbl.name ? t.bg4 : "transparent",
                        color: selected === tbl.name ? t.accent : t.text2,
                        border: "none", borderRadius: 6,
                        fontSize: 13, fontFamily: "ui-monospace, Consolas, monospace",
                        cursor: "pointer", textAlign: "left",
                      }}
                    >
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{tbl.name}</span>
                      <span style={{ fontSize: 11, color: t.textDim, flexShrink: 0 }}>
                        {tbl.row_count.toLocaleString()}
                      </span>
                    </button>
                  ))}
                </>
              );
              return <>{renderGroup("Pipeline", pipe)}{disc.length > 0 && <div style={{ borderTop: `1px solid ${t.borderL}`, margin: "6px 0" }} />}{disc.length > 0 && renderGroup("Discovery", disc)}</>;
            })()}
          </div>

          {/* Right pane: rows */}
          <div>
            <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
              <input
                type="search"
                placeholder="Search text + numbers…"
                value={search}
                onChange={(e) => { setSearch(e.target.value); setPage(1); }}
                style={{ padding: "7px 10px", fontSize: 12, background: t.bg2, border: `1px solid ${t.borderL}`, borderRadius: 6, color: t.text2, minWidth: 240, fontFamily: "inherit" }}
              />
              <span style={{ fontSize: 12, color: t.textDim }}>
                {total.toLocaleString()} row{total === 1 ? "" : "s"}
                {search && ` matching “${search}”`}
              </span>
              <div style={{ position: "relative" }} ref={colMenuRef}>
                <Btn
                  variant="ghost"
                  onClick={() => setColMenuOpen((o) => !o)}
                  disabled={!schema || allColumns.length === 0}
                  title="Show / hide columns"
                >
                  Columns{hiddenCols.size > 0 ? ` (${allColumns.length - hiddenCols.size}/${allColumns.length})` : ""}
                </Btn>
                {colMenuOpen && (
                  <div
                    role="menu"
                    style={{
                      position: "absolute", top: "calc(100% + 4px)", left: 0,
                      background: t.bg2, border: `1px solid ${t.borderL}`,
                      borderRadius: 8, padding: 8, zIndex: 20,
                      minWidth: 220, maxHeight: 360, overflowY: "auto",
                      boxShadow: "0 4px 16px rgba(0,0,0,0.3)",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "4px 6px 8px", borderBottom: `1px solid ${t.borderL}`, marginBottom: 4 }}>
                      <span style={{ fontSize: 11, color: t.textDim, textTransform: "uppercase", fontWeight: 700 }}>Visible columns</span>
                      <button
                        onClick={showAllCols}
                        disabled={hiddenCols.size === 0}
                        style={{ background: "none", border: "none", color: hiddenCols.size === 0 ? t.textDim : t.accent, cursor: hiddenCols.size === 0 ? "default" : "pointer", fontSize: 11, padding: 0 }}
                      >
                        Show all
                      </button>
                    </div>
                    {allColumns.map((c) => {
                      const checked = !hiddenCols.has(c);
                      const isPk = c === pkCol;
                      return (
                        <label
                          key={c}
                          style={{
                            display: "flex", alignItems: "center", gap: 8,
                            padding: "4px 6px", fontSize: 12,
                            color: isPk ? t.textDim : t.text2,
                            cursor: isPk ? "default" : "pointer",
                            fontFamily: "ui-monospace, Consolas, monospace",
                          }}
                        >
                          <input
                            type="checkbox"
                            checked={checked}
                            disabled={isPk}
                            onChange={() => toggleColHidden(c)}
                          />
                          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c}</span>
                          {isPk && <span style={{ color: t.accent, fontSize: 10 }}>★ PK</span>}
                        </label>
                      );
                    })}
                  </div>
                )}
              </div>
              <div style={{ marginLeft: "auto" }}>
                {pagerJsx}
              </div>
            </div>

            {pendingCount > 0 && (
              <div
                style={{
                  display: "flex", alignItems: "center", gap: 10,
                  background: t.accent + "15",
                  border: `1px solid ${t.accent}44`,
                  padding: "8px 12px", borderRadius: 8, marginBottom: 10,
                  fontSize: 13, color: t.text2,
                }}
              >
                <span style={{ fontWeight: 600, color: t.accent }}>
                  {pendingCount} pending {pendingCount === 1 ? "change" : "changes"}
                </span>
                <Btn variant="primary" disabled={saving} onClick={saveAll}>
                  {saving ? <Spin size={14} /> : "Commit"}
                </Btn>
                <Btn variant="ghost" disabled={saving} onClick={() => { setEdits({}); setFocusCell(null); }}>
                  Revert
                </Btn>
                {!pkCol && (
                  <span style={{ fontSize: 12, color: t.err }}>
                    no primary key — saving is disabled
                  </span>
                )}
              </div>
            )}

            {loading && !rows ? (
              <div style={{ display: "flex", justifyContent: "center", padding: 40 }}><Spin /></div>
            ) : rows && rows.length === 0 ? (
              <p style={{ color: t.textDim, fontSize: 13 }}>
                {search ? `No rows match “${search}”.` : "No rows in this table."}
              </p>
            ) : rows ? (
              <div style={{ background: t.bg2, border: `1px solid ${t.borderL}`, borderRadius: 8, overflow: "auto", maxHeight: "70vh" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, fontFamily: "ui-monospace, Consolas, monospace" }}>
                  <thead style={{ position: "sticky", top: 0, background: t.bg3, zIndex: 1 }}>
                    <tr>
                      <th style={{ width: 28, borderBottom: `1px solid ${t.borderL}` }}></th>
                      {columns.map((c) => {
                        const meta = schema?.columns.find((sc) => sc.name === c);
                        const mx = colMaxWidth(c, meta?.type);
                        const isActive = sort === c;
                        const arrow = isActive ? (sortDir === "asc" ? " ↑" : " ↓") : "";
                        return (
                          <th
                            key={c}
                            onClick={() => onSortClick(c)}
                            title={`Click to sort by ${c}`}
                            style={{
                              padding: "8px 10px", textAlign: "left",
                              fontWeight: 600,
                              color: isActive ? t.accent : t.textDim,
                              background: isActive ? t.accent + "15" : undefined,
                              borderBottom: `1px solid ${t.borderL}`,
                              whiteSpace: "nowrap",
                              cursor: "pointer",
                              userSelect: "none",
                              maxWidth: mx,
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                            }}
                          >
                            {c}
                            {meta?.primary_key && <span style={{ color: t.accent, marginLeft: 4 }}>★</span>}
                            <span style={{ color: t.accent }}>{arrow}</span>
                          </th>
                        );
                      })}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row, i) => {
                      const rk = rowKey(row);
                      const isDeleting = deleting === rk;
                      return (
                        <tr
                          key={rk || i}
                          style={{
                            borderBottom: i < rows.length - 1 ? `1px solid ${t.borderL}` : "none",
                            opacity: isDeleting ? 0.4 : 1,
                          }}
                        >
                          <td style={{ textAlign: "center", padding: "4px 0", verticalAlign: "top" }}>
                            {pkCol && rk && (
                              <button
                                title="Delete row"
                                disabled={isDeleting}
                                onClick={() => deleteRow(row)}
                                style={{ background: "none", border: "none", color: t.textDim, cursor: "pointer", padding: 4, fontSize: 14, lineHeight: 1 }}
                                onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.color = t.err; }}
                                onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.color = t.textDim; }}
                              >
                                ×
                              </button>
                            )}
                          </td>
                          {columns.map((c) => {
                            const meta = schema?.columns.find((sc) => sc.name === c);
                            const isPk = meta?.primary_key ?? false;
                            const hasPending = !!(rk && edits[rk] && c in edits[rk]);
                            const v = cellValue(row, c);
                            const editable = !!pkCol && !isPk;
                            const isFocused = focusCell?.rowKey === rk && focusCell?.col === c;
                            const mx = colMaxWidth(c, meta?.type);

                            if (isFocused && editable) {
                              return (
                                <td key={c} style={{ padding: 0, verticalAlign: "top", background: t.accent + "22", maxWidth: mx }}>
                                  <input
                                    autoFocus
                                    defaultValue={v === null || v === undefined ? "" : String(v)}
                                    onBlur={(e) => {
                                      const raw = e.target.value;
                                      // Store null for empty string so NOT NULL
                                      // columns get caught server-side rather than
                                      // silently saved as "".
                                      const stored = raw === "" ? null : raw;
                                      setPending(row, c, stored);
                                      setFocusCell(null);
                                    }}
                                    onKeyDown={(e) => {
                                      if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                                      if (e.key === "Escape") { e.preventDefault(); setFocusCell(null); }
                                    }}
                                    style={{
                                      width: "100%", padding: "6px 10px", border: "none",
                                      background: "transparent", color: t.text,
                                      fontFamily: "inherit", fontSize: 12, outline: `1px solid ${t.accent}`,
                                    }}
                                  />
                                </td>
                              );
                            }

                            const display = v === null || v === undefined
                              ? <span style={{ color: t.textDim, fontStyle: "italic" }}>NULL</span>
                              : typeof v === "object"
                                ? JSON.stringify(v)
                                : String(v);
                            return (
                              <td
                                key={c}
                                onClick={() => { if (editable) setFocusCell({ rowKey: rk, col: c }); }}
                                style={{
                                  padding: "6px 10px",
                                  color: hasPending ? t.accent : t.text2,
                                  background: hasPending ? t.accent + "15" : undefined,
                                  verticalAlign: "top",
                                  maxWidth: mx,
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                  whiteSpace: "nowrap",
                                  cursor: editable ? "text" : "default",
                                }}
                                title={typeof display === "string" ? display : undefined}
                              >
                                {display}
                              </td>
                            );
                          })}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : null}

            {/* Bottom pagination — duplicate of the top toolbar's pager
               so the user can flip pages without scrolling back up. Only
               shows when there's more than one page (avoids dangling UI
               on a tiny table). */}
            {rows && rows.length > 0 && totalPages > 1 ? (
              <div
                style={{
                  display: "flex", justifyContent: "flex-end",
                  alignItems: "center", gap: 10,
                  marginTop: 12, paddingTop: 12,
                  borderTop: `1px solid ${t.borderL}`,
                }}
              >
                <span style={{ fontSize: 12, color: t.textDim }}>
                  {total.toLocaleString()} row{total === 1 ? "" : "s"}
                </span>
                {pagerJsx}
              </div>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}
