// State machine for a Manual Grab review list (up to 30 rows, pasted
// links and dropped .torrent files mixed).
//
//   add → de-duplicated against the list → previews fetched one at a
//   time (rows fill in as they arrive; the server's pacer spaces the
//   MAM calls) → the user ticks rows → Grab all starts a server-side
//   job → the hook polls it until done.
//
// The job runs on the server, so leaving the page doesn't stop it; the
// grabs it makes show up in the usual grab history.
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { economyApi } from "../../lib/economyApi";
import {
  BLOCKING_STATUSES,
  MAX_BATCH,
  torrentIdOf,
  wedgeEligible,
  type EntryKind,
  type GrabEntry,
  type GrabJob,
  type PreviewRow,
  type WedgeBudget,
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
    overrideSnatched: false, confirming: false, result: null,
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

// The torrent an entry is about, as far as we know before/after preview.
const tidOf = (e: GrabEntry) =>
  e.preview?.torrent_id ?? (e.kind === "link" ? torrentIdOf(e.input) : null);

interface SnatchBudget {
  budget_used: number;
  budget_cap: number;
}

export function useManualGrabBatch() {
  const [entries, setEntries] = useState<GrabEntry[]>([]);
  const [job, setJob] = useState<GrabJob | null>(null);
  const [grabbing, setGrabbing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [useWedges, setUseWedgesState] = useState(false);
  // The MAM page's "Show 'use wedge' checkbox on manual grabs" setting
  // (`mam_economy_manual_wedge_offer_enabled`) governs this page too
  // (D34). Off, or unreadable: no wedge toggle, no wedges.
  const [offerWedges, setOfferWedges] = useState(false);
  const [wedges, setWedges] = useState<WedgeBudget | null>(null);
  const [wedgeError, setWedgeError] = useState<string | null>(null);
  const [snatch, setSnatch] = useState<SnatchBudget | null>(null);

  const mounted = useRef(true);
  const queue = useRef<GrabEntry[]>([]);
  const running = useRef(false);
  // Mirror of `entries` for the add paths, which must de-duplicate
  // against the list as it is right now, not as of the last render.
  const current = useRef<GrabEntry[]>([]);

  const commit = useCallback((next: (prev: GrabEntry[]) => GrabEntry[]) => {
    setEntries((prev) => {
      const out = next(prev);
      current.current = out;
      return out;
    });
  }, []);

  const loadSnatch = useCallback(async () => {
    try {
      const b = await api.get<SnatchBudget>("/v1/grabs/budget");
      if (mounted.current) setSnatch(b);
    } catch {
      if (mounted.current) setSnatch(null); // totals just skip the queue line
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void loadSnatch();
    economyApi
      .getConfig()
      .then((cfg) => {
        if (mounted.current) setOfferWedges(!!cfg.mam_economy_manual_wedge_offer_enabled);
      })
      .catch(() => {});
    return () => {
      mounted.current = false;
    };
  }, [loadSnatch]);

  const patch = useCallback(
    (key: string, p: Partial<GrabEntry>) =>
      commit((prev) => prev.map((e) => (e.key === key ? { ...e, ...p } : e))),
    [commit],
  );

  // A preview lands: apply it, keeping one row per torrent. A file row
  // beats a link row for the same torrent (it needs no MAM download);
  // otherwise the earlier row stays. Previews arrive one at a time, long
  // after the last render, so `current` is up to date here.
  const land = useCallback(
    (key: string, preview: PreviewRow) => {
      const tid = preview.torrent_id;
      const me = current.current.find((e) => e.key === key);
      const twin = tid
        ? current.current.find((e) => e.key !== key && !e.result && tidOf(e) === tid)
        : undefined;
      let loserKey: string | null = null;
      if (me && twin) {
        loserKey = me.kind === "file" && twin.kind === "link" ? twin.key : key;
        const loser = loserKey === key ? me : twin;
        setNotice(`"${loser.input}" is the same torrent as another row; kept one.`);
      }
      commit((prev) =>
        prev
          .map((e) => (e.key === key ? { ...e, preview, ticked: preview.status === "ready" } : e))
          .filter((e) => e.key !== loserKey),
      );
    },
    [commit],
  );

  const drain = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    try {
      while (queue.current.length && mounted.current) {
        const entry = queue.current.shift()!;
        if (!current.current.some((e) => e.key === entry.key)) continue; // removed meanwhile
        let preview: PreviewRow;
        try {
          preview = await api.post<PreviewRow>("/v1/manual-grab/preview", itemBody(entry));
        } catch (e) {
          preview = errorPreview(entry.kind, entry.input, `Preview failed: ${String(e)}`);
        }
        if (!mounted.current) break;
        land(entry.key, preview);
      }
    } finally {
      running.current = false;
    }
  }, [land]);

  // Room left under the 30 cap; reports what didn't fit.
  const admit = useCallback((fresh: GrabEntry[], dupes: number) => {
    const room = Math.max(0, MAX_BATCH - current.current.length);
    const taken = fresh.slice(0, room);
    const parts: string[] = [];
    if (fresh.length > room) parts.push(`${fresh.length - room} left out (${MAX_BATCH} per batch)`);
    if (dupes) parts.push(`${dupes} already in the list`);
    setNotice(parts.length ? `${parts.join("; ")}.` : null);
    if (!taken.length) return;
    current.current = [...current.current, ...taken];
    commit((prev) => [...prev, ...taken]);
    queue.current.push(...taken);
    void drain();
  }, [commit, drain]);

  const addLinks = useCallback(
    (inputs: string[]) => {
      const seen = new Set(current.current.map(tidOf).filter(Boolean) as string[]);
      const fresh: GrabEntry[] = [];
      let dupes = 0;
      for (const raw of inputs) {
        const input = raw.trim();
        if (!input) continue;
        const tid = torrentIdOf(input);
        if (tid && seen.has(tid)) { dupes++; continue; }
        if (tid) seen.add(tid);
        fresh.push(newEntry("link", input));
      }
      admit(fresh, dupes);
    },
    [admit],
  );

  const addFiles = useCallback(
    async (files: File[]) => {
      const seen = new Set(current.current.map((e) => e.dataB64).filter(Boolean) as string[]);
      const fresh: GrabEntry[] = [];
      let dupes = 0;
      for (const f of files) {
        let data: string;
        try {
          data = await readBase64(f);
        } catch (e) {
          setError(`Couldn't read ${f.name}: ${String(e)}`);
          continue;
        }
        if (seen.has(data)) { dupes++; continue; }
        seen.add(data);
        fresh.push(newEntry("file", f.name, data));
      }
      if (mounted.current) admit(fresh, dupes);
    },
    [admit],
  );

  // Re-run one row's preview (D26): a lookup that failed for a passing
  // reason (MAM timed out, account unreadable). Goes back in the queue,
  // so it's paced like any other lookup.
  const retry = useCallback(
    (key: string) => {
      const entry = current.current.find((e) => e.key === key);
      if (!entry || entry.result) return;
      const reset = { ...entry, preview: null, ticked: false, confirming: false };
      commit((prev) => prev.map((e) => (e.key === key ? reset : e)));
      queue.current.push(reset);
      void drain();
    },
    [commit, drain],
  );

  const remove = useCallback(
    (key: string) => commit((prev) => prev.filter((e) => e.key !== key || !!e.result)),
    [commit],
  );

  const clear = useCallback(() => {
    queue.current = [];
    commit(() => []);
    setJob(null);
    setError(null);
    setNotice(null);
  }, [commit]);

  // Ticking a row MAM marks as already snatched opens the "Download
  // again?" confirm instead (D13); only `confirmSnatched` sets the override.
  const setTicked = useCallback(
    (key: string, on: boolean) => {
      commit((prev) =>
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
    [commit],
  );

  const confirmSnatched = useCallback(
    (key: string) => patch(key, { ticked: true, overrideSnatched: true, confirming: false }),
    [patch],
  );
  const cancelConfirm = useCallback(
    (key: string) => patch(key, { confirming: false }),
    [patch],
  );

  const setUseWedges = useCallback(async (on: boolean) => {
    setUseWedgesState(on);
    setWedgeError(null);
    if (!on) return;
    try {
      const w = await api.get<WedgeBudget>("/v1/manual-grab/wedges");
      if (mounted.current) setWedges(w);
    } catch (e) {
      if (mounted.current) {
        setWedges(null);
        setWedgeError(String(e));
      }
    }
  }, []);

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
          override_mam_snatched: e.overrideSnatched,
          use_wedge: offerWedges && useWedges && wedgeEligible(e),
        })),
      });
    } catch (e) {
      // 409 = the batch needs more wedges than MAM says you can spend.
      setError(String(e));
      setGrabbing(false);
      if (useWedges) void setUseWedges(true); // refresh the count shown
      return;
    }
    const keyByIndex = chosen.map((e) => e.key);
    const apply = (j: GrabJob) => {
      setJob(j);
      commit((prev) =>
        prev.map((e) => {
          const i = keyByIndex.indexOf(e.key);
          return i >= 0 && j.rows[i] ? { ...e, result: j.rows[i], ticked: false } : e;
        }),
      );
    };
    apply(started);
    let latest = started;
    while (!latest.done && mounted.current) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      try {
        latest = await api.get<GrabJob>(`/v1/manual-grab/grab/${started.job_id}`);
      } catch (e) {
        setError(String(e));
        break;
      }
      if (mounted.current) apply(latest);
    }
    if (mounted.current) {
      setGrabbing(false);
      void loadSnatch();
    }
  }, [entries, offerWedges, useWedges, setUseWedges, commit, loadSnatch]);

  const pending = entries.some((e) => e.preview === null);
  const open = entries.filter((e) => e.ticked && !e.result);
  const tickedCount = open.length;
  const wedgesOn = offerWedges && useWedges;
  const wedgeCount = wedgesOn ? open.filter(wedgeEligible).length : 0;
  const eligibleForWedges = entries.filter(wedgeEligible).length;
  const wedgeShort = wedgesOn && !!wedges && wedgeCount > wedges.spendable;
  const freeSlots = snatch ? Math.max(0, snatch.budget_cap - snatch.budget_used) : null;
  const willQueue = freeSlots === null ? 0 : Math.max(0, tickedCount - freeSlots);

  return {
    entries,
    job,
    grabbing,
    error,
    notice,
    pending,
    tickedCount,
    offerWedges,
    useWedges: wedgesOn,
    wedges,
    wedgeError,
    wedgeCount,
    eligibleForWedges,
    wedgeShort,
    willQueue,
    addLinks,
    addFiles,
    retry,
    remove,
    clear,
    setTicked,
    confirmSnatched,
    cancelConfirm,
    setUseWedges,
    grabAll,
  };
}

export type ManualGrabBatch = ReturnType<typeof useManualGrabBatch>;
