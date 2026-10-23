// Poll the unified scan-status endpoint and report scans that finished.
//
// DiscAuthorDetailPage + MobileAuthorDetailPage each carried a verbatim
// copy of the same 3s `/discovery/scan-status` poller: track the running
// flag per kind, and on a running→idle transition reload the page's data
// (desktop also bumps a remount key). This hook is the single home for
// that transition logic so a fix lands once.
//
// Behaviour kept from the pages it replaced (wave 5b, issue 22):
// - one `onComplete` call per poll, with every kind that went
//   running→idle in that poll (so two scans finishing together reload
//   the page once, not twice);
// - an immediate poll on mount, then every `intervalMs`;
// - the loop restarts (fresh running flags + an immediate poll) when
//   `restartKey` changes: the pages pass their `loadA`, whose identity
//   changes when the author does, exactly when their old effect re-ran;
// - fetch errors are ignored (scan-status is non-critical);
// - no React state: a poll that finds nothing new doesn't re-render.
//
// `onComplete` and `kinds` are read through refs, so a fresh closure on
// every render doesn't restart the loop.
import { useEffect, useRef } from "react";
import { api } from "../api";
import type { ScanProgress, ScanStatusResponse } from "../types";

export type ScanKind = ScanProgress["kind"];

const DEFAULT_KINDS: ScanKind[] = ["lookup", "mam"];

export interface UseScanPollingOptions {
  /** Kinds to watch for a running→idle transition. Default: lookup + mam. */
  kinds?: ScanKind[];
  /** Called once per poll with the kinds that just finished (never empty). */
  onComplete?: (finished: ScanKind[]) => void;
  /** Poll cadence in ms. Default 3000 (the pages it replaced). */
  intervalMs?: number;
  /** Pause polling when false. Default true. */
  enabled?: boolean;
  /** Restart the loop when this changes. */
  restartKey?: unknown;
}

export function useScanPolling(options: UseScanPollingOptions = {}): void {
  const { intervalMs = 3000, enabled = true, restartKey } = options;

  const onCompleteRef = useRef(options.onComplete);
  onCompleteRef.current = options.onComplete;
  const kindsRef = useRef(options.kinds ?? DEFAULT_KINDS);
  kindsRef.current = options.kinds ?? DEFAULT_KINDS;

  useEffect(() => {
    if (!enabled) return;
    let active = true;
    const prev: Partial<Record<ScanKind, boolean>> = {};

    const tick = async () => {
      try {
        const r = await api.get<ScanStatusResponse>("/discovery/scan-status");
        if (!active) return;
        const scans = r.scans || [];
        const finished: ScanKind[] = [];
        for (const kind of kindsRef.current) {
          const isRunning = scans.some((s) => s.kind === kind && s.running);
          if (prev[kind] && !isRunning) finished.push(kind);
          prev[kind] = isRunning;
        }
        if (finished.length) onCompleteRef.current?.(finished);
      } catch {
        /* ignore — scan-status is non-critical */
      }
    };

    tick();
    const id = window.setInterval(tick, intervalMs);
    return () => {
      active = false;
      window.clearInterval(id);
    };
  }, [enabled, intervalMs, restartKey]);
}
