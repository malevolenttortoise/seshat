// Settings → Data Management: per-author and global discovery data clears.
// Split out of SettingsPage (wave 5b S16, issue 23).
import { useEffect, useState } from "react";
import { api } from "../../api";
import { useTheme } from "../../theme";
import { useMamEnabled } from "../../hooks/useMamEnabled";
import type { Author, AuthorsResponse } from "../../types";
import { Btn } from "../Btn";
import { SF } from "./fields";

// Per-author + global discovery data clears. Mirrors the AthenaScout
// "Data Management" UX so the same wipe operations are reachable
// from Settings without having to enter Select-mode on the Authors
// page (or visit per-author detail pages). Backed by:
//   POST /discovery/authors/clear-scan-data  — per-author multi
//   POST /discovery/sources/reset            — wipe all source data
//   POST /discovery/mam/reset                — wipe all MAM data
export function DiscoveryDataSection() {
  const t = useTheme();
  const mamOn = useMamEnabled();
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Author[]>([]);
  const [picked, setPicked] = useState<Array<{ id: number; name: string }>>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  // Debounced author search — 300ms idle window so we don't fire on
  // every keystroke. Below 2 chars we clear results to avoid the
  // dropdown showing "every author whose name starts with 'a'".
  useEffect(() => {
    if (q.length < 2) { setResults([]); return; }
    const tm = setTimeout(() => {
      const params = new URLSearchParams({ search: q });
      api.get<AuthorsResponse>(`/discovery/authors?${params}`)
        .then((r) => setResults((r.authors || []).slice(0, 20)))
        .catch(() => {});
    }, 300);
    return () => clearTimeout(tm);
  }, [q]);

  const pick = (a: Author) => {
    if (!picked.find((p) => p.id === a.id)) setPicked([...picked, { id: a.id, name: a.name }]);
    setQ("");
    setResults([]);
  };
  const drop = (id: number) => setPicked(picked.filter((p) => p.id !== id));
  const flash = (m: string) => { setMsg(m); setTimeout(() => setMsg(""), 4000); };

  const clearForPicked = async (kind: "source" | "mam" | "both") => {
    if (!picked.length) return;
    const verb = kind === "both" ? "ALL scan data (source + MAM)" : `${kind.toUpperCase()} scan data`;
    if (!confirm(`Clear ${verb} for ${picked.length} author(s)? This will DELETE all discovered books${kind === "both" ? " AND reset MAM status" : kind === "source" ? "" : " and reset MAM status"}.`)) return;
    setBusy(true);
    try {
      await api.post("/discovery/authors/clear-scan-data", {
        author_ids: picked.map((p) => p.id),
        clear_source: kind === "source" || kind === "both",
        clear_mam: kind === "mam" || kind === "both",
      });
      setPicked([]);
      flash(`Cleared ${verb.toLowerCase()} for ${picked.length} author(s)`);
    } catch (e) {
      flash(`Error: ${(e as Error).message || e}`);
    }
    setBusy(false);
  };

  const wipeAllSource = async () => {
    if (!confirm("Reset ALL source scan data?\n\nThis DELETES every discovered book across the entire library and resets every author's last-scanned timestamp so future scans treat them as never-scanned.\n\nOwned books and MAM data are NOT affected.\n\nThis cannot be undone.")) return;
    setBusy(true);
    try {
      const r = await api.post<{ books_deleted?: number; series_cleaned?: number }>("/discovery/sources/reset");
      flash(`Source data reset — ${r.books_deleted || 0} books deleted${r.series_cleaned ? `, ${r.series_cleaned} empty series cleaned` : ""}`);
    } catch (e) {
      flash(`Error: ${(e as Error).message || e}`);
    }
    setBusy(false);
  };

  const wipeAllMam = async () => {
    if (!confirm("Wipe ALL MAM scan data?\n\nClears mam_url and mam_status on every book. Every book becomes 'unscanned' and will be re-checked on the next MAM scan.")) return;
    setBusy(true);
    try {
      await api.post("/discovery/mam/reset");
      flash("MAM scan data wiped");
    } catch (e) {
      flash(`Error: ${(e as Error).message || e}`);
    }
    setBusy(false);
  };

  return (
    <>
      {msg && <div style={{ fontSize: 12, color: msg.startsWith("Error") ? t.err : t.ok, marginBottom: 8, fontWeight: 600 }}>{msg.startsWith("Error") ? "✗" : "✓"} {msg}</div>}

      <SF
        wide
        label="Clear scan data by author"
        desc={mamOn
          ? "Search for authors, then clear their source data, MAM data, or both. Owned books are kept."
          : "Search for authors, then clear their source scan data. Owned books are kept. (Enable MAM to also clear MAM data.)"}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 10, minWidth: 320, width: "100%" }}>
          <div style={{ position: "relative" }}>
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search authors..."
              disabled={busy}
              style={{ width: "100%", padding: "8px 10px", background: t.inp, border: `1px solid ${t.border}`, borderRadius: 6, color: t.text, fontSize: 14 }}
            />
            {results.length > 0 && (
              <div style={{ position: "absolute", top: "100%", left: 0, right: 0, maxHeight: 200, overflowY: "auto", background: t.bg2, border: `1px solid ${t.border}`, borderRadius: "0 0 6px 6px", zIndex: 10, boxShadow: "0 4px 12px rgba(0,0,0,0.3)" }}>
                {results.map((a) => (
                  <div
                    key={a.id}
                    onClick={() => pick(a)}
                    style={{ padding: "6px 10px", cursor: "pointer", fontSize: 13, color: t.text, borderBottom: `1px solid ${t.borderL}` }}
                  >
                    {a.name}{" "}
                    <span style={{ color: t.textDim, fontSize: 12 }}>
                      ({a.total_books || 0} books)
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
          {picked.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
              {picked.map((a) => (
                <span key={a.id} style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "3px 8px", borderRadius: 4, fontSize: 12, background: t.accent + "22", color: t.accent, border: `1px solid ${t.accent}44` }}>
                  {a.name}
                  <button onClick={() => drop(a.id)} disabled={busy} style={{ background: "none", border: "none", cursor: busy ? "not-allowed" : "pointer", color: t.accent, padding: 0, fontSize: 14 }}>×</button>
                </span>
              ))}
              <button onClick={() => setPicked([])} disabled={busy} style={{ background: "none", border: "none", cursor: busy ? "not-allowed" : "pointer", color: t.textDim, fontSize: 11, padding: "2px 6px" }}>clear all</button>
            </div>
          )}
          {picked.length > 0 && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <Btn size="sm" disabled={busy} onClick={() => clearForPicked("source")}>Clear Source</Btn>
              {mamOn && <Btn size="sm" disabled={busy} onClick={() => clearForPicked("mam")}>Clear MAM</Btn>}
              {mamOn && <Btn size="sm" variant="danger" disabled={busy} onClick={() => clearForPicked("both")}>Clear Both</Btn>}
            </div>
          )}
        </div>
      </SF>

      <SF
        label="Wipe ALL source scan data"
        desc="Delete every discovered (non-Calibre, non-owned) book and reset every author's last-scanned timestamp. Owned books and MAM data are kept."
      >
        <Btn variant="danger" disabled={busy} onClick={wipeAllSource}>⚠ Wipe all source data</Btn>
      </SF>

      {mamOn && (
        <SF
          label="Wipe ALL MAM scan data"
          desc="Clear mam_url and mam_status on every book. Every book becomes 'unscanned' and will be re-checked on the next MAM scan."
        >
          <Btn variant="danger" disabled={busy} onClick={wipeAllMam}>⚠ Wipe all MAM data</Btn>
        </SF>
      )}
    </>
  );
}
