// Banner shown above the app while the startup library sync runs.
//
// Two modes, both driven by `/api/discovery/scan-status`:
//
// - **Splash** — full-screen overlay rendered on first-ever boot
//   (`startup_complete === false` AND no library has any prior
//   `completed_at`). The library tables are empty, so an inline
//   banner over an empty Library page would be confusing — the
//   splash explains *why* it's empty.
//
// - **Banner** — sticky top strip on every subsequent run while a
//   library sync is in flight (`running === true` for any library,
//   regardless of `startup_complete`). The user can still navigate,
//   pages render whatever data is currently in the DB, and the
//   banner advances per-library as Pass 3's `current/total` ticks
//   forward.
//
// Polls every 3s using the same pattern DiscBooksPage uses for the
// MAM scan banner. Stops polling once nothing is running AND
// `startup_complete === true`.
import { useEffect, useState } from "react";
import { useTheme } from "../theme";
import { api } from "../api";
import type { ScanProgress } from "../types";

interface ScanStatusResponse {
  scans: ScanProgress[];
  startup_complete: boolean;
}

const POLL_MS = 3000;

export function LibrarySyncBanner() {
  const t = useTheme();
  const [status, setStatus] = useState<ScanStatusResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const poll = async () => {
      try {
        const r = await api.get<ScanStatusResponse>("/discovery/scan-status");
        if (cancelled) return;
        setStatus(r);
        const anyRunning = (r.scans || []).some(
          s => s.kind === "library" && s.running,
        );
        // Keep polling while sync is in flight OR startup hasn't yet
        // signalled complete. Once both go quiet, stop — the next
        // event-driven refresh (page nav, manual sync trigger) will
        // re-arm us if needed.
        if (anyRunning || !r.startup_complete) {
          timer = setTimeout(poll, POLL_MS);
        }
      } catch {
        // Network/auth error — back off and try again. Don't surface
        // the error in the banner; users have bigger problems than
        // "sync banner can't poll" if the API is down.
        if (!cancelled) timer = setTimeout(poll, POLL_MS * 2);
      }
    };
    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  if (!status) return null;
  const libraries = (status.scans || []).filter(s => s.kind === "library");
  const running = libraries.filter(s => s.running);
  const anyEverCompleted = libraries.some(s => !!s.completed_at);
  // Splash gate: pre-first-sync (startup_complete=false) with no
  // library having ever finished a real sync.
  const splashMode = !status.startup_complete && !anyEverCompleted;
  // Banner gate: something is actively running.
  if (!splashMode && running.length === 0) return null;

  if (splashMode) {
    return (
      <div
        role="status"
        aria-live="polite"
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 1000,
          background: t.bg,
          color: t.text,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 24,
          padding: 32,
        }}
      >
        <img src="/icon.svg" alt="" style={{ width: 64, height: 64 }} />
        <div style={{ fontSize: 22, fontWeight: 700, color: t.accent }}>
          Setting up Seshat
        </div>
        <div style={{ fontSize: 14, color: t.td, maxWidth: 520, textAlign: "center" }}>
          First-time library import. This can take a few minutes for
          large Calibre libraries — we're reading every book once so
          future startups only touch the changes.
        </div>
        <div style={{ width: "100%", maxWidth: 520, display: "flex", flexDirection: "column", gap: 10 }}>
          {libraries.map(lib => (
            <SyncRow key={lib.slug || lib.label} lib={lib} />
          ))}
        </div>
      </div>
    );
  }

  // Inline banner — sticky strip above the page content.
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: "sticky",
        top: 0,
        zIndex: 40,
        background: t.bg2,
        borderBottom: `2px solid ${t.accent}`,
        padding: "8px 16px",
        display: "flex",
        flexDirection: "column",
        gap: 4,
        fontSize: 13,
      }}
    >
      <div style={{ fontWeight: 600, color: t.accent }}>
        Library sync in progress — pages may show stale counts until this finishes.
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {running.map(lib => (
          <SyncRow key={lib.slug || lib.label} lib={lib} compact />
        ))}
      </div>
    </div>
  );
}

interface SyncRowProps {
  lib: ScanProgress;
  compact?: boolean;
}

function SyncRow({ lib, compact = false }: SyncRowProps) {
  const t = useTheme();
  const pct = lib.total > 0
    ? Math.min(100, Math.round((lib.current / lib.total) * 100))
    : (lib.running ? -1 : 100);
  const statusText = lib.running
    ? lib.total > 0
      ? `${lib.current} / ${lib.total}`
      : "starting…"
    : lib.completed_at
      ? "complete"
      : "idle";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <div style={{
        display: "flex",
        justifyContent: "space-between",
        fontSize: compact ? 12 : 14,
        color: t.text,
      }}>
        <span style={{ fontWeight: 600 }}>{lib.label}</span>
        <span style={{ color: t.td }}>{statusText}</span>
      </div>
      {lib.current_book ? (
        <div style={{ fontSize: 11, color: t.td, fontStyle: "italic" }}>
          {lib.current_book}
        </div>
      ) : null}
      {pct >= 0 ? (
        <div style={{
          width: "100%",
          height: compact ? 3 : 6,
          background: t.bg,
          borderRadius: 3,
          overflow: "hidden",
        }}>
          <div style={{
            width: `${pct}%`,
            height: "100%",
            background: t.accent,
            transition: "width 0.5s ease-out",
          }} />
        </div>
      ) : null}
    </div>
  );
}
