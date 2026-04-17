// MigrationPage v3 — server-side background migration with polling.
//
// The migration runs entirely on the backend so the user can navigate
// away or close the browser without losing progress. The frontend
// polls GET /api/v1/migration/status every 2s to update the progress
// bar and result log.
//
// After migration completes: resume torrents, then scan for empty
// leftover folders and offer to clean them up.
import { useEffect, useRef, useState } from "react";
import { Btn } from "../components/Btn";
import { Section } from "../components/Section";
import { Spin } from "../components/Spin";
import { api } from "../api";
import { useTheme } from "../theme";
import { useVisibleInterval } from "../hooks/useVisibleInterval";

interface PreviewItem {
  hash: string; name: string; current_path: string; current_folder: string;
  target_folder: string | null; target_path: string | null;
  needs_move: boolean; file_mtime: string | null;
}
interface PreviewResponse { items: PreviewItem[]; need_move_count: number; already_ok_count: number; total: number; }
interface MigrationStatus {
  running: boolean; done: number; total: number;
  succeeded: number; failed: number; finished: boolean;
  dry_run: boolean; results: ResultItem[];
}
interface ResultItem { hash: string; name: string; ok: boolean; error: string | null; action: string | null; }
interface EmptyFolder { name: string; path: string; }
interface EmptyFoldersResponse { folders: EmptyFolder[]; root: string; }

export default function MigrationPage() {
  const t = useTheme();
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [status, setStatus] = useState<MigrationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"pending" | "done">("pending");
  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  // Polling is state-driven so the visibility-aware hook can pause
  // it when the tab is hidden. Imperative start/stop callers just
  // flip this flag; the hook handles the rest.
  const [polling, setPolling] = useState(false);
  const [resultOffset, setResultOffset] = useState(0);
  const RESULTS_PAGE = 100;

  // Cleanup step state.
  const [cleanupPhase, setCleanupPhase] = useState<"hidden" | "scanning" | "ready" | "done">("hidden");
  const [emptyFolders, setEmptyFolders] = useState<EmptyFolder[]>([]);
  const [cleanupResult, setCleanupResult] = useState<{ deleted: number; failed: number; errors: string[] } | null>(null);

  // On mount, check if a migration is already running.
  useEffect(() => {
    checkExistingJob();
    return () => stopPolling();
  }, []);

  async function checkExistingJob() {
    try {
      const s = await api.get<MigrationStatus>("/v1/migration/status");
      if (s.running || (s.finished && s.total > 0)) {
        setStatus(s);
        setResultOffset(Math.max(0, s.results.length - RESULTS_PAGE));
        if (s.running) startPolling();
      }
    } catch { /* no job yet */ }
  }

  function startPolling() { setPolling(true); }
  function stopPolling() { setPolling(false); }

  // Single polling tick — runs on the visible-interval cadence
  // while `polling` is true. Auto-stops when the backend reports
  // the migration finished.
  useVisibleInterval(async () => {
    try {
      const s = await api.get<MigrationStatus>("/v1/migration/status");
      setStatus(s);
      setResultOffset(Math.max(0, s.results.length - RESULTS_PAGE));
      if (!s.running) setPolling(false);
    } catch { /* swallow */ }
  }, polling ? 2000 : 0);

  async function scan() {
    setBusy(true); setError(null);
    try {
      const r = await api.get<PreviewResponse>("/v1/migration/preview");
      setPreview(r);
      setSelected(new Set(r.items.filter(i => i.needs_move).map(i => i.hash)));
      setTab("pending");
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }

  async function startMigration(dryRun: boolean) {
    const hashes = [...selected];
    if (hashes.length === 0) return;
    setBusy(true); setError(null); setSuccessMsg(null);
    setCleanupPhase("hidden"); setCleanupResult(null);
    try {
      await api.post<{ ok: boolean; total: number }>("/v1/migration/start", { hashes, dry_run: dryRun });
      const s = await api.get<MigrationStatus>("/v1/migration/status");
      setStatus(s);
      setResultOffset(0);
      startPolling();
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }

  async function cancelMigration() {
    try {
      await api.post<{ ok: boolean }>("/v1/migration/cancel");
      stopPolling();
      const s = await api.get<MigrationStatus>("/v1/migration/status");
      setStatus(s);
    } catch (e) { setError(String(e)); }
  }

  async function resumeAll() {
    if (!confirm("Resume all stopped torrents in the watched category?")) return;
    setBusy(true);
    try {
      const r = await api.post<{ resumed: number; total: number }>("/v1/migration/resume-all");
      setSuccessMsg(`Resumed ${r.resumed} of ${r.total} torrents`);
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }

  async function scanEmptyFolders() {
    setCleanupPhase("scanning"); setError(null);
    try {
      const r = await api.get<EmptyFoldersResponse>("/v1/migration/empty-folders");
      setEmptyFolders(r.folders);
      setCleanupPhase("ready");
    } catch (e) { setError(String(e)); setCleanupPhase("hidden"); }
  }

  async function deleteEmptyFolders() {
    if (emptyFolders.length === 0) return;
    setBusy(true); setError(null);
    try {
      const r = await api.post<{ deleted: number; failed: number; errors: string[] }>(
        "/v1/migration/cleanup",
        { folders: emptyFolders.map(f => f.path) },
      );
      setCleanupResult(r);
      setCleanupPhase("done");
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }

  function clearJob() {
    setStatus(null);
    setResultOffset(0);
    setCleanupPhase("hidden");
    setCleanupResult(null);
    stopPolling();
  }

  async function clearAndRescan() {
    clearJob();
    await scan();
  }

  function toggle(hash: string) { setSelected(s => { const n = new Set(s); n.has(hash) ? n.delete(hash) : n.add(hash); return n; }); }
  function toggleAll() {
    if (!preview) return;
    const movable = preview.items.filter(i => i.needs_move);
    setSelected(selected.size === movable.length ? new Set() : new Set(movable.map(i => i.hash)));
  }

  const pendingItems = preview?.items.filter(i => i.needs_move) ?? [];
  const doneItems = preview?.items.filter(i => !i.needs_move) ?? [];
  const visibleResults = status?.results.slice(resultOffset, resultOffset + RESULTS_PAGE) ?? [];
  const totalResults = status?.results.length ?? 0;
  const isRunning = status?.running ?? false;
  const isFinished = status?.finished ?? false;

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ fontSize: 24, fontWeight: 700, color: t.text, marginBottom: 4 }}>Migration Wizard</h1>
      <p style={{ fontSize: 14, color: t.textDim, marginBottom: 20 }}>
        Move existing downloads into the configured folder structure based on file modification dates.
      </p>

      {error && <div style={{ background: t.err + "22", border: `1px solid ${t.err}55`, color: t.err, padding: "10px 14px", borderRadius: 8, fontSize: 13, marginBottom: 16 }}>{error}</div>}
      {successMsg && <div style={{ background: t.ok + "22", border: `1px solid ${t.ok}55`, color: t.ok, padding: "10px 14px", borderRadius: 8, fontSize: 13, marginBottom: 16 }}>{successMsg}</div>}

      {/* Active / completed migration job */}
      {status && (isRunning || isFinished) && (
        <>
          <Section
            title={isRunning ? "Migration in progress" : `Migration ${status.dry_run ? "(dry run) " : ""}complete`}
            subtitle={`${status.succeeded} succeeded, ${status.failed} failed of ${status.total}`}
            right={isRunning
              ? <Btn variant="danger" onClick={cancelMigration}>Cancel</Btn>
              : <Btn variant="ghost" onClick={clearJob}>Dismiss</Btn>
            }
          >
            {/* Progress bar */}
            <div style={{ marginBottom: 12 }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: t.textDim, marginBottom: 4 }}>
                <span>{isRunning ? `Processing ${status.done} / ${status.total}...` : `Done: ${status.done} / ${status.total}`}</span>
                <span>{status.total > 0 ? Math.round(status.done / status.total * 100) : 0}%</span>
              </div>
              <div style={{ height: 6, borderRadius: 3, background: t.bg4, overflow: "hidden" }}>
                <div style={{
                  width: `${status.total > 0 ? status.done / status.total * 100 : 0}%`,
                  height: "100%",
                  background: status.failed > 0 ? t.warn : t.accent,
                  borderRadius: 3,
                  transition: "width 0.3s",
                }} />
              </div>
            </div>

            {status.dry_run && isFinished && (
              <p style={{ fontSize: 13, color: t.accent, marginBottom: 12 }}>Dry run complete — no files moved. Review, then run the real migration.</p>
            )}

            {/* Result log */}
            {totalResults > 0 && (
              <div style={{ maxHeight: 350, overflowY: "auto", marginTop: 8 }}>
                {resultOffset > 0 && (
                  <div style={{ textAlign: "center", padding: "4px 0", fontSize: 11, color: t.textDim }}>
                    ...{resultOffset} earlier result{resultOffset !== 1 ? "s" : ""} not shown...
                  </div>
                )}
                {visibleResults.map((r, i) => (
                  <div key={resultOffset + i} style={{
                    display: "flex", justifyContent: "space-between", alignItems: "baseline",
                    padding: "5px 0", borderBottom: `1px solid ${t.borderL}`, fontSize: 12, gap: 8,
                  }}>
                    <div style={{ minWidth: 0, flex: 1 }}>
                      <span style={{ color: r.ok ? t.text2 : t.err }}>{r.name}</span>
                      {r.action && <span style={{ color: t.textDim, marginLeft: 8, fontFamily: "monospace", fontSize: 11 }}>{r.action}</span>}
                    </div>
                    <span style={{ color: r.ok ? t.ok : t.err, fontWeight: 600, flexShrink: 0 }}>
                      {r.ok ? "\u2713" : "\u2717"} {r.error || ""}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </Section>

          {/* Post-migration actions: Resume + Cleanup */}
          {isFinished && !status.dry_run && status.succeeded > 0 && (
            <div style={{
              marginTop: 16, padding: "16px 20px",
              background: t.ok + "12", border: `1px solid ${t.ok}33`, borderRadius: 10,
              display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12,
            }}>
              <div>
                <div style={{ fontSize: 14, fontWeight: 600, color: t.text }}>Migration complete</div>
                <div style={{ fontSize: 12, color: t.textDim, marginTop: 4 }}>
                  {status.succeeded} torrent(s) relocated. Resume torrents, then clean up empty folders.
                </div>
              </div>
              <div style={{ display: "flex", gap: 10 }}>
                <Btn variant="primary" onClick={resumeAll} disabled={busy}>
                  {busy ? <Spin size={14} /> : "\u25b6 Start All Torrents"}
                </Btn>
                <Btn variant="secondary" onClick={scanEmptyFolders} disabled={busy || cleanupPhase !== "hidden"}>
                  Clean Up Folders
                </Btn>
                <Btn variant="ghost" onClick={clearAndRescan}>Re-scan</Btn>
              </div>
            </div>
          )}

          {/* Cleanup: scanning */}
          {cleanupPhase === "scanning" && (
            <Section title="Scanning for empty folders...">
              <div style={{ display: "flex", justifyContent: "center", padding: 20 }}><Spin /></div>
            </Section>
          )}

          {/* Cleanup: show empty folders */}
          {cleanupPhase === "ready" && (
            <Section
              title={`${emptyFolders.length} empty folder${emptyFolders.length !== 1 ? "s" : ""} found`}
              subtitle="These folders contain no files and can be safely removed."
            >
              {emptyFolders.length === 0 ? (
                <p style={{ fontSize: 13, color: t.textDim }}>No empty folders found. Everything is clean.</p>
              ) : (
                <>
                  <div style={{ maxHeight: 300, overflowY: "auto", marginBottom: 12 }}>
                    {emptyFolders.map((f) => (
                      <div key={f.path} style={{
                        padding: "6px 8px", borderBottom: `1px solid ${t.borderL}`,
                        fontSize: 12, color: t.text2, fontFamily: "monospace",
                      }}>
                        {f.name}
                      </div>
                    ))}
                  </div>
                  <div style={{ display: "flex", gap: 10 }}>
                    <Btn variant="danger" onClick={deleteEmptyFolders} disabled={busy}>
                      {busy ? <Spin size={14} /> : `Delete ${emptyFolders.length} Empty Folder${emptyFolders.length !== 1 ? "s" : ""}`}
                    </Btn>
                    <Btn variant="ghost" onClick={() => setCleanupPhase("hidden")} disabled={busy}>
                      Skip
                    </Btn>
                  </div>
                </>
              )}
            </Section>
          )}

          {/* Cleanup: results */}
          {cleanupPhase === "done" && cleanupResult && (
            <Section title={`Cleanup: ${cleanupResult.deleted} deleted, ${cleanupResult.failed} failed`}>
              {cleanupResult.errors.length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  {cleanupResult.errors.map((e, i) => (
                    <div key={i} style={{ fontSize: 12, color: t.err, padding: "3px 0" }}>{e}</div>
                  ))}
                </div>
              )}
              <p style={{ fontSize: 13, color: t.ok }}>
                Folder cleanup complete.
              </p>
            </Section>
          )}
        </>
      )}

      {/* Getting started (no preview yet and no active job) */}
      {!preview && !isRunning && !isFinished && (
        <Section title="Getting started">
          <div style={{
            background: t.warn + "14", border: `1px solid ${t.warn}33`, borderRadius: 10,
            padding: "14px 18px", marginBottom: 16, fontSize: 13, lineHeight: 1.6, color: t.text2,
          }}>
            <div style={{ fontWeight: 700, color: t.warn, marginBottom: 6 }}>Before you start</div>
            <ol style={{ margin: 0, paddingLeft: 20 }}>
              <li style={{ marginBottom: 4 }}><strong>Stop all torrents</strong> in your download client first. This prevents tracker count spikes during the move.</li>
              <li style={{ marginBottom: 4 }}>Wait ~1 minute for the tracker to register the stop.</li>
              <li>Run the migration. Seshat will <strong>not</strong> auto-resume stopped torrents — use the "Start All" button at the end when you're satisfied.</li>
            </ol>
          </div>
          <Btn variant="primary" onClick={scan} disabled={busy}>{busy ? <Spin size={14} /> : "Scan Torrents"}</Btn>
        </Section>
      )}

      {/* Preview table (hidden while a job is running) */}
      {preview && !isRunning && (
        <>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
            <div style={{ display: "flex", gap: 4, borderBottom: `1px solid ${t.borderL}` }}>
              <TabBtn active={tab === "pending"} label={`Needs migration (${pendingItems.length})`} onClick={() => setTab("pending")} />
              <TabBtn active={tab === "done"} label={`Already correct (${doneItems.length})`} onClick={() => setTab("done")} />
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span style={{ fontSize: 12, color: t.textDim }}>{preview.total} total</span>
              <Btn variant="ghost" onClick={scan} disabled={busy}>Re-scan</Btn>
              {tab === "pending" && <Btn variant="ghost" onClick={toggleAll}>{selected.size === pendingItems.length ? "Deselect all" : "Select all"}</Btn>}
            </div>
          </div>

          <Section title={tab === "pending" ? `${pendingItems.length} torrents to migrate` : `${doneItems.length} already correct`}>
            <div style={{ maxHeight: 500, overflowY: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                <thead><tr style={{ textAlign: "left", color: t.textDim, fontWeight: 600, fontSize: 11, textTransform: "uppercase" }}>
                  {tab === "pending" && <th style={{ padding: "6px 4px", width: 30 }}></th>}
                  <th style={{ padding: "6px 4px" }}>Name</th>
                  <th style={{ padding: "6px 4px" }}>Current folder</th>
                  <th style={{ padding: "6px 4px" }}>Target</th>
                  <th style={{ padding: "6px 4px" }}>File date</th>
                </tr></thead>
                <tbody>
                  {(tab === "pending" ? pendingItems : doneItems).map(item => (
                    <tr key={item.hash} style={{ borderTop: `1px solid ${t.borderL}` }}>
                      {tab === "pending" && <td style={{ padding: "6px 4px" }}><input type="checkbox" checked={selected.has(item.hash)} onChange={() => toggle(item.hash)} /></td>}
                      <td style={{ padding: "6px 4px", color: t.text, maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={item.name}>{item.name}</td>
                      <td style={{ padding: "6px 4px", color: item.needs_move ? t.warn : t.ok, fontSize: 11 }}>{item.current_folder}</td>
                      <td style={{ padding: "6px 4px", color: t.accent, fontSize: 11 }}>{item.target_folder || "root"}</td>
                      <td style={{ padding: "6px 4px", color: t.textDim, fontSize: 11 }}>{item.file_mtime?.slice(0, 10) || "\u2014"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          {tab === "pending" && pendingItems.length > 0 && !isFinished && (
            <Section title="Execute" subtitle={`${selected.size} selected`}>
              <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                <Btn variant="secondary" onClick={() => startMigration(true)} disabled={busy || selected.size === 0 || isRunning}>
                  Dry Run ({selected.size})
                </Btn>
                <Btn variant="primary" onClick={() => startMigration(false)} disabled={busy || selected.size === 0 || isRunning}>
                  Migrate {selected.size}
                </Btn>
                <span style={{ fontSize: 12, color: t.textDim }}>
                  Runs server-side — you can navigate away safely.
                </span>
              </div>
            </Section>
          )}
        </>
      )}
    </div>
  );
}

function TabBtn({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  const t = useTheme();
  return (
    <button onClick={onClick} style={{
      background: "transparent", border: "none",
      borderBottom: `2px solid ${active ? t.accent : "transparent"}`,
      color: active ? t.accent : t.text2,
      padding: "10px 16px", fontSize: 14, fontWeight: 600,
      cursor: "pointer", marginBottom: -1,
    }}>{label}</button>
  );
}
