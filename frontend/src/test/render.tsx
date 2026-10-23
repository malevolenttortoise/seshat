// Render a page with canned API responses and record what it asked for.
//
// The render snapshots (src/test/pages) are wave 5b's proof that moving
// page logic into shared hooks changes nothing you see and nothing the
// page sends: each test renders a page on a fixed clock and viewport,
// answers every request from a fixture table, and snapshots both the
// markup and the ordered list of requests the page made while loading.
//
// `fetch` itself is replaced (not `api.ts`), so the `/api` prefix, the
// JSON handling and the error path all run as in the browser. A request
// with no fixture answers 404 and is listed in `unmatched`: every test
// asserts that list is empty, so a page that quietly fell back to an
// error state can't pass as "rendered".
import type { ReactElement } from "react";
import { act, render, type RenderResult } from "@testing-library/react";
import { expect, vi } from "vitest";

export const FIXED_NOW = "2026-10-09T16:00:00Z";

/** A fixture that answers with a non-2xx status. */
export interface StatusReply {
  readonly __status: number;
  readonly body?: unknown;
}
export const reply = (status: number, body?: unknown): StatusReply => ({ __status: status, body });

export interface FetchCall {
  method: string;
  url: string;
  body?: unknown;
}
export type RouteValue = unknown | StatusReply | ((call: FetchCall) => unknown);

/**
 * Keys are `"<METHOD> <path>"` with the path as the page passes it to
 * `api.*` (no `/api` prefix). Lookup order: the exact path with its
 * query string, then the path without the query, then the longest
 * key ending in `*` that prefixes it.
 */
export type Routes = Record<string, RouteValue>;

export type Viewport = "desktop" | "phone";

const WIDTH: Record<Viewport, number> = { desktop: 1800, phone: 390 };

export function setViewport(viewport: Viewport): void {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: WIDTH[viewport],
  });
}

function lookup(routes: Routes, method: string, url: string): { found: boolean; value?: RouteValue } {
  const exact = `${method} ${url}`;
  if (exact in routes) return { found: true, value: routes[exact] };
  const bare = `${method} ${url.split("?")[0]}`;
  if (bare in routes) return { found: true, value: routes[bare] };
  let best: string | null = null;
  for (const key of Object.keys(routes)) {
    if (!key.endsWith("*")) continue;
    const prefix = key.slice(0, -1);
    if (exact.startsWith(prefix) && (best === null || prefix.length > best.length - 1)) best = key;
  }
  return best === null ? { found: false } : { found: true, value: routes[best] };
}

function isStatusReply(v: unknown): v is StatusReply {
  return typeof v === "object" && v !== null && "__status" in v;
}

export interface PageRender extends RenderResult {
  /** Every request in the order the page made it: `"GET /v1/review"`. */
  requests: string[];
  /** Requests no fixture answered (each got a 404). */
  unmatched: string[];
  /** Wait for the requests an interaction started to land. */
  settle: () => Promise<void>;
}

export interface RenderPageOptions {
  viewport: Viewport;
  routes: Routes;
  now?: string;
}

export async function renderPage(ui: ReactElement, opts: RenderPageOptions): Promise<PageRender> {
  setViewport(opts.viewport);
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(opts.now ?? FIXED_NOW));

  const requests: string[] = [];
  const unmatched: string[] = [];
  let pending = 0;

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = raw.startsWith("/api") ? raw.slice(4) : raw;
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    requests.push(body === undefined ? `${method} ${url}` : `${method} ${url} ${JSON.stringify(body)}`);
    if (init?.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    pending += 1;
    try {
      const hit = lookup(opts.routes, method, url);
      if (!hit.found) {
        unmatched.push(`${method} ${url}`);
        return new Response(JSON.stringify({ detail: "no fixture" }), { status: 404 });
      }
      let value = hit.value;
      if (typeof value === "function") value = (value as (c: FetchCall) => unknown)({ method, url, body });
      if (isStatusReply(value)) {
        return new Response(value.body === undefined ? null : JSON.stringify(value.body), {
          status: value.__status,
        });
      }
      return new Response(JSON.stringify(value ?? null), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    } finally {
      pending -= 1;
    }
  });
  vi.stubGlobal("fetch", fetchMock);

  const result = render(ui);
  const settleThis = () => settle(() => pending, () => requests.length);
  await settleThis();
  return Object.assign(result, { requests, unmatched, settle: settleThis });
}

/**
 * Wait until no request is in flight and none has started for 350ms,
 * so data fetched after the first response (a second fan-out, a
 * debounced reload — the Logs page waits 250ms) has landed before the
 * snapshot.
 */
async function settle(pending: () => number, count: () => number): Promise<void> {
  let stable = 0;
  let last = -1;
  for (let i = 0; i < 400 && stable < 14; i += 1) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 25));
    });
    const now = count();
    stable = pending() === 0 && now === last ? stable + 1 : 0;
    last = now;
  }
}

/** Undo `renderPage`'s globals; call from `afterEach`. */
export function resetPageEnv(): void {
  vi.useRealTimers();
  vi.unstubAllGlobals();
}

/**
 * The checks every render snapshot makes: no request went unanswered,
 * the page got past loading (each `marker` text is on screen), then the
 * requests (sorted) and the markup match their snapshots.
 */
export function expectRendered(r: PageRender, ...markers: string[]): void {
  if (r.unmatched.length) throw new Error(`No fixture for:\n  ${r.unmatched.join("\n  ")}`);
  for (const m of markers) {
    expect(r.queryAllByText(m, { exact: false }).length, `"${m}" on screen`).toBeGreaterThan(0);
  }
  // Sorted: effects that fire on mount run concurrently, so a hook
  // refactor may reorder them; the snapshot holds which requests (and
  // how many of each), not who asked first.
  expect([...r.requests].sort()).toMatchSnapshot("requests");
  expect(r.asFragment()).toMatchSnapshot("markup");
}
