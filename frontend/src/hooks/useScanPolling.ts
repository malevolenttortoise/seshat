// Poll the unified scan-status endpoint and fire a callback when a scan
// finishes.
//
// Before this hook, DiscAuthorDetailPage + MobileAuthorDetailPage each
// carried a verbatim copy of the same 3s `/discovery/scan-status`
// poller: track the running flag per kind, and on a running→idle
// transition reload the page's data + bump a remount key. Several other
// pages (DiscBooksPage, the MAM pages) poll the same endpoint with small
// variations. This hook is the single home for the transition logic so a
// fix lands once, not N times.
//
// The interface is deliberately small: name the `kinds` to watch and
// give an `onComplete(kind)` callback; it returns the current running
// flag per kind for spinner state. `onComplete` and `kinds` are read
// through refs so the poll loop never tears down and restarts when the
// caller passes a fresh closure each render — a restart would reset the
// running-state tracking and could miss a transition.
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ScanProgress, ScanStatusResponse } from "../types";

export type ScanKind = ScanProgress["kind"];

const DEFAULT_KINDS: ScanKind[] = ["lookup", "mam"];

export interface UseScanPollingOptions {
  /** Kinds to watch for a running→idle transition. Default: lookup + mam. */
  kinds?: ScanKind[];
  /** Fired once per kind when it transitions from running to idle. */
  onComplete?: (kind: ScanKind) => void;
  /** Poll cadence in ms. Default 3000 (matches the pages it replaces). */
  intervalMs?: number;
  /** Pause polling when false. Default true. */
  enabled?: boolean;
}

export type ScanRunning = Partial<Record<ScanKind, boolean>>;

export function useScanPolling(
  options: UseScanPollingOptions = {},
): { running: ScanRunning } {
  const { intervalMs = 3000, enabled = true } = options;

  // Read callback + kinds through refs so a new closure each render
  // doesn't restart the poll loop.
  const onCompleteRef = useRef(options.onComplete);
  onCompleteRef.current = options.onComplete;
  const kindsRef = useRef(options.kinds ?? DEFAULT_KINDS);
  kindsRef.current = options.kinds ?? DEFAULT_KINDS;

  const [running, setRunning] = useState<ScanRunning>({});

  useEffect(() => {
    if (!enabled) return;
    let active = true;
    const prev: ScanRunning = {};

    const tick = async () => {
      try {
        const r = await api.get<ScanStatusResponse>("/discovery/scan-status");
        if (!active) return;
        const scans = r.scans || [];
        const next: ScanRunning = {};
        for (const kind of kindsRef.current) {
          const isRunning = scans.some((s) => s.kind === kind && s.running);
          next[kind] = isRunning;
          // running → idle edge: the scan just finished.
          if (prev[kind] && !isRunning) onCompleteRef.current?.(kind);
          prev[kind] = isRunning;
        }
        setRunning(next);
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
  }, [enabled, intervalMs]);

  return { running };
}
