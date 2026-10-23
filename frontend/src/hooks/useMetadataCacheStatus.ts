// One shared poller for the metadata-cache workers' status
// (`GET /api/v1/metadata-cache/{amazon,goodreads}/status`).
//
// Three places show it: the navbar cloud icon (both sources), the
// Dashboard's Amazon / Goodreads cache rails, and the Settings → Metadata
// Sources cache cards. Each used to run its own raw `setInterval`, so
// with the Dashboard open every status was fetched twice a minute (the
// audit's L4-03: ~4.5× the requests of any other polled endpoint, in
// pairs), and kept going in background tabs.
//
// Now (wave 5b S5, G137): one request per source per tick, shared by
// every mounted subscriber, at the fastest subscriber's pace (60s; 30s
// while a Settings card is open); ticks are skipped while the tab is
// hidden, and coming back fetches at once. A subscriber that mounts
// fetches at once too (as each component did on mount) and is handed
// the last status meanwhile. When the last subscriber of a source
// unmounts, that source's poll stops and its status is forgotten, so a
// later mount starts from "loading" as before.
//
// What each place shows is unchanged: errors go to the subscribers that
// display them (`onError`, the Settings card); the others keep showing
// the last status they had. `setStatus` is a per-subscriber optimistic
// override (the card's mode / schedule changes) that the next fetched
// status replaces, exactly as the card's own state did.
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";

export type CacheSource = "amazon" | "goodreads";

interface Subscriber {
  pace: number;
  onData: (status: unknown) => void;
  onError: (message: string) => void;
}

interface Store {
  subs: Set<Subscriber>;
  status: unknown | null;
  timer: ReturnType<typeof setInterval> | null;
  timerPace: number;
  inflight: Promise<void> | null;
  /** Bumped when the store resets, so a late response is dropped. */
  generation: number;
}

const stores: Record<CacheSource, Store> = {
  amazon: newStore(),
  goodreads: newStore(),
};

function newStore(): Store {
  return { subs: new Set(), status: null, timer: null, timerPace: 0, inflight: null, generation: 0 };
}

function fetchStatus(source: CacheSource): Promise<void> {
  const s = stores[source];
  if (s.inflight) return s.inflight;
  const generation = s.generation;
  const done = api
    .get<unknown>(`/v1/metadata-cache/${source}/status`)
    .then(
      (status) => {
        if (generation !== s.generation) return;
        s.status = status;
        s.subs.forEach((sub) => sub.onData(status));
      },
      (e) => {
        if (generation !== s.generation || api.isAbort(e)) return;
        const message = e instanceof Error ? e.message : "Status fetch failed";
        s.subs.forEach((sub) => sub.onError(message));
      },
    )
    .finally(() => {
      if (generation === s.generation) s.inflight = null;
    });
  s.inflight = done;
  return done;
}

function stopTimer(s: Store): void {
  if (s.timer !== null) clearInterval(s.timer);
  s.timer = null;
  s.timerPace = 0;
}

/** Poll at the fastest subscriber's pace; restart only when it changes. */
function schedule(source: CacheSource, restart = false): void {
  const s = stores[source];
  if (s.subs.size === 0) {
    stopTimer(s);
    return;
  }
  const pace = Math.min(...[...s.subs].map((sub) => sub.pace));
  if (!restart && s.timer !== null && s.timerPace === pace) return;
  stopTimer(s);
  s.timerPace = pace;
  s.timer = setInterval(() => {
    if (!document.hidden) void fetchStatus(source);
  }, pace);
}

/** A fetch that starts after this call (an action changed the status). */
async function refetchStatus(source: CacheSource): Promise<void> {
  const inflight = stores[source].inflight;
  if (inflight) await inflight;
  return fetchStatus(source);
}

let visibilityHooked = false;
function hookVisibility(): void {
  if (visibilityHooked || typeof document === "undefined") return;
  visibilityHooked = true;
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    // Back in view: catch up now, then resume the cadence from here.
    for (const source of Object.keys(stores) as CacheSource[]) {
      if (stores[source].subs.size === 0) continue;
      void fetchStatus(source);
      schedule(source, true);
    }
  });
}

function subscribe(source: CacheSource, sub: Subscriber): () => void {
  hookVisibility();
  const s = stores[source];
  s.subs.add(sub);
  if (s.status !== null) sub.onData(s.status);
  void fetchStatus(source);
  schedule(source);
  return () => {
    s.subs.delete(sub);
    if (s.subs.size === 0) {
      stopTimer(s);
      s.status = null;
      s.inflight = null;
      s.generation += 1;
    } else {
      schedule(source);
    }
  };
}

export interface UseMetadataCacheStatusOptions<T> {
  /** How often this place wants a fresh status, in ms. */
  paceMs: number;
  /** Each fetched status (not optimistic overrides), e.g. to seed inputs. */
  onData?: (status: T) => void;
  /** A failed fetch; places that don't show errors leave it out. */
  onError?: (message: string) => void;
}

export interface UseMetadataCacheStatus<T> {
  /** The optimistic override if one is set, else the last fetched status. */
  status: T | null;
  /** Override what this subscriber shows until the next fetched status. */
  setStatus: (status: T) => void;
  /** Fetch now, after any fetch already in flight (shared with every subscriber). */
  refresh: () => Promise<void>;
}

export function useMetadataCacheStatus<T>(
  source: CacheSource,
  { paceMs, onData, onError }: UseMetadataCacheStatusOptions<T>,
): UseMetadataCacheStatus<T> {
  const [fetched, setFetched] = useState<T | null>(null);
  const [override, setOverride] = useState<T | null>(null);
  const onDataRef = useRef(onData);
  onDataRef.current = onData;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  useEffect(() => {
    return subscribe(source, {
      pace: paceMs,
      onData: (status) => {
        setOverride(null);
        setFetched(status as T);
        onDataRef.current?.(status as T);
      },
      onError: (message) => onErrorRef.current?.(message),
    });
  }, [source, paceMs]);

  const refresh = useCallback(() => refetchStatus(source), [source]);

  return { status: override ?? fetched, setStatus: setOverride, refresh };
}
