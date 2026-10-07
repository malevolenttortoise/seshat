// State machine for a Manual Grab review list.
//
//   add → previews fetched one at a time (rows fill in as they arrive;
//   the server's pacer spaces the MAM calls) → the user ticks rows →
//   Grab all starts a server-side job → the hook polls it until done.
//
// The job runs on the server, so leaving the page doesn't stop it; the
// grabs it makes show up in the usual grab history.
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import {
  BLOCKING_STATUSES,
  type EntryKind,
  type GrabEntry,
  type GrabJob,
  type PreviewRow,
} from "./types";

const POLL_MS = 1500;

let keySeq = 0;
const nextKey = () => `mg-${Date.now()}-${keySeq++}`;

// A .torrent as base64 (D18: uploads travel inside JSON).
function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result || "");
      resolve(url.slice(url.indexOf(",") + 1));
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function itemBody(e: GrabEntry) {
  return e.kind === "file"
    ? { kind: "file", name: e.input, data_b64: e.dataB64 }
    : { kind: "link", value: e.input };
}

function newEntry(kind: EntryKind, input: string, dataB64: string | null = null): GrabEntry {
  return {
    key: nextKey(), kind, input, dataB64, preview: null, ticked: false,
    buyFl: false, overrideSnatched: false, confirming: false, result: null,
  };
}

function errorPreview(kind: EntryKind, input: string, message: string): PreviewRow {
  return {
    kind, input, status: "lookup_failed", message,
    torrent_id: null, title: "", authors: [], narrators: [], series: [],
    category: "", filetype: "", size_bytes: null, seeders: null,
    vip: false, freeleech: false, personal_freeleech: false,
    my_snatched: false, owned_in: [], in_flight: false,
    policy_tier: "", policy_grab: true, wedge_eligible: false,
    grab_id: null, cover_url: null, info_hash: null,
  };
}

export function useManualGrabBatch() {
  const [entries, setEntries] = useState<GrabEntry[]>([]);
  const [job, setJob] = useState<GrabJob | null>(null);
  const [grabbing, setGrabbing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mounted = useRef(true);
  const queue = useRef<GrabEntry[]>([]);
  const running = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const patch = useCallback((key: string, p: Partial<GrabEntry>) => {
    setEntries((prev) => prev.map((e) => (e.key === key ? { ...e, ...p } : e)));
  }, []);

  const drain = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    try {
      while (queue.current.length && mounted.current) {
        const entry = queue.current.shift()!;
        let preview: PreviewRow;
        try {
          preview = await api.post<PreviewRow>("/v1/manual-grab/preview", itemBody(entry));
        } catch (e) {
          preview = errorPreview(entry.kind, entry.input, `Preview failed: ${String(e)}`);
        }
        if (!mounted.current) break;
        patch(entry.key, { preview, ticked: preview.status === "ready" });
      }
    } finally {
      running.current = false;
    }
  }, [patch]);

  const enqueue = useCallback(
    (fresh: GrabEntry[]) => {
      setEntries((prev) => [...prev, ...fresh]);
      queue.current.push(...fresh);
      void drain();
    },
    [drain],
  );

  const addLinks = useCallback(
    (inputs: string[]) => enqueue(inputs.map((input) => newEntry("link", input))),
    [enqueue],
  );

  const addFiles = useCallback(
    async (files: File[]) => {
      const fresh: GrabEntry[] = [];
      for (const f of files) {
        try {
          fresh.push(newEntry("file", f.name, await readBase64(f)));
        } catch (e) {
          setError(`Couldn't read ${f.name}: ${String(e)}`);
        }
      }
      if (mounted.current && fresh.length) enqueue(fresh);
    },
    [enqueue],
  );

  const clear = useCallback(() => {
    queue.current = [];
    setEntries([]);
    setJob(null);
    setError(null);
  }, []);

  // Ticking a row MAM marks as already snatched opens the "Download
  // again?" confirm instead (D13); only `confirmSnatched` sets the override.
  const setTicked = useCallback(
    (key: string, on: boolean) => {
      setEntries((prev) =>
        prev.map((e) => {
          if (e.key !== key || !e.preview) return e;
          if (BLOCKING_STATUSES.has(e.preview.status)) return e;
          if (on && e.preview.status === "snatched_on_mam" && !e.overrideSnatched) {
            return { ...e, confirming: true };
          }
          return on
            ? { ...e, ticked: true }
            : { ...e, ticked: false, overrideSnatched: false, confirming: false };
        }),
      );
    },
    [],
  );

  const confirmSnatched = useCallback(
    (key: string) => patch(key, { ticked: true, overrideSnatched: true, confirming: false }),
    [patch],
  );
  const cancelConfirm = useCallback(
    (key: string) => patch(key, { confirming: false }),
    [patch],
  );
  const setBuyFl = useCallback(
    (key: string, on: boolean) => patch(key, { buyFl: on }),
    [patch],
  );

  const grabAll = useCallback(async () => {
    const chosen = entries.filter((e) => e.ticked && e.preview && !e.result);
    if (!chosen.length) return;
    setGrabbing(true);
    setError(null);
    let started: GrabJob;
    try {
      started = await api.post<GrabJob>("/v1/manual-grab/grab", {
        items: chosen.map((e) => ({
          ...itemBody(e),
          buy_personal_fl: e.buyFl,
          override_mam_snatched: e.overrideSnatched,
        })),
      });
    } catch (e) {
      setError(String(e));
      setGrabbing(false);
      return;
    }
    const keyByIndex = chosen.map((e) => e.key);
    const apply = (j: GrabJob) => {
      setJob(j);
      setEntries((prev) =>
        prev.map((e) => {
          const i = keyByIndex.indexOf(e.key);
          return i >= 0 && j.rows[i] ? { ...e, result: j.rows[i], ticked: false } : e;
        }),
      );
    };
    apply(started);
    let current = started;
    while (!current.done && mounted.current) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      try {
        current = await api.get<GrabJob>(`/v1/manual-grab/grab/${started.job_id}`);
      } catch (e) {
        setError(String(e));
        break;
      }
      if (mounted.current) apply(current);
    }
    if (mounted.current) setGrabbing(false);
  }, [entries]);

  const pending = entries.some((e) => e.preview === null);
  const tickedCount = entries.filter((e) => e.ticked && !e.result).length;
  const flCount = entries.filter((e) => e.ticked && e.buyFl && !e.result).length;

  return {
    entries,
    job,
    grabbing,
    error,
    pending,
    tickedCount,
    flCount,
    addLinks,
    addFiles,
    clear,
    setTicked,
    confirmSnatched,
    cancelConfirm,
    setBuyFl,
    grabAll,
  };
}

export type ManualGrabBatch = ReturnType<typeof useManualGrabBatch>;
