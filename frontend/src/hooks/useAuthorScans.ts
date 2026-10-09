// The author pages' scan buttons (source scan, MAM scan), for
// DiscAuthorDetailPage and MobileAuthorDetailPage (wave 5b S17, G160).
//
// Scans run as background tasks: the POST starts one and returns. A
// started scan keeps its button busy (and blocks a second start) until
// the scan-finished poll sees it end, then the page reloads; a start
// that fails, or a MAM scan with nothing to scan, frees it at once.
// Desktop's other scans (full re-scan, across libraries) use the same
// busy flag.
import { useState } from "react";
import { api } from "../api";
import { toast } from "../lib/toast";
import { useScanPolling } from "./useScanPolling";

interface ScanStartedResponse {
  status?: string;
  author?: string;
  message?: string;
  total?: number;
}

export function useAuthorScans({
  authorIdNum, authorSlug, loadA, onFinished,
}: {
  authorIdNum: number;
  authorSlug: string | null | undefined;
  loadA: () => Promise<void> | void;
  /** After the page reloads on a finished scan (desktop bumps its series refresh key). */
  onFinished?: () => void;
}) {
  const [sourceBusy, setSourceBusy] = useState(false);
  const [mamBusy, setMamBusy] = useState(false);

  useScanPolling({
    kinds: ["lookup", "mam"],
    restartKey: loadA,
    onComplete: (finished) => {
      if (finished.includes("lookup")) setSourceBusy(false);
      if (finished.includes("mam")) setMamBusy(false);
      loadA();
      onFinished?.();
    },
  });

  const scanQs = authorSlug ? `?slug=${encodeURIComponent(authorSlug)}` : "";

  const startSourceScan = async (full: boolean) => {
    if (sourceBusy) return;
    setSourceBusy(true);
    try {
      const r = await api.post<ScanStartedResponse>(
        `/discovery/authors/${authorIdNum}/${full ? "full-rescan" : "lookup"}${scanQs}`,
      );
      toast.info(
        `${full ? "Full re-scan" : "Source scan"} started for ${r.author || "author"}`,
      );
      window.dispatchEvent(new CustomEvent("seshat:scan-started"));
    } catch (e) {
      toast.error((e as Error).message || "Scan failed to start");
      setSourceBusy(false);
    }
  };

  const scanMam = async () => {
    if (mamBusy) return;
    setMamBusy(true);
    try {
      const r = await api.post<ScanStartedResponse>(
        `/discovery/mam/scan-author/${authorIdNum}${scanQs}`,
      );
      if (r.status === "complete") {
        toast.info(r.message || "No un-scanned books for this author");
        setMamBusy(false);
      } else {
        toast.info(`MAM scan started — ${r.total || 0} books`);
        window.dispatchEvent(new CustomEvent("seshat:scan-started"));
      }
    } catch (e) {
      toast.error((e as Error).message || "MAM scan failed to start");
      setMamBusy(false);
    }
  };

  return {
    sourceBusy,
    setSourceBusy,
    mamBusy,
    scanSources: () => startSourceScan(false),
    fullRescan: () => startSourceScan(true),
    scanMam,
  };
}
