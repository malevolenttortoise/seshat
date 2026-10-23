import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useDashboard, useDashboardCommands, useDashboardData } from "./useDashboard";
import { api } from "../api";
import { useVisibleInterval } from "./useVisibleInterval";
import { useVisibleEventSource } from "./useVisibleEventSource";

vi.mock("../api", () => ({ api: { get: vi.fn(), post: vi.fn() } }));
vi.mock("./useVisibleInterval", () => ({ useVisibleInterval: vi.fn() }));
vi.mock("./useVisibleEventSource", () => ({ useVisibleEventSource: vi.fn() }));
const mockGet = vi.mocked(api.get);
const mockPost = vi.mocked(api.post);
const mockInterval = vi.mocked(useVisibleInterval);
const mockSse = vi.mocked(useVisibleEventSource);

// No Array.prototype.at under the app's ES2020 lib.
const last = <T,>(xs: T[]): T => xs[xs.length - 1];

const lib = (slug: string, content_type: string, running = false) =>
  ({ kind: "library", slug, content_type, running, type: "library", label: slug, current: 0, total: 0 });
const scansIdle = { scans: [lib("cal", "ebook"), lib("abs", "audiobook"), { kind: "lookup", running: false }] };

function routes(over: Record<string, unknown> = {}) {
  const table: Record<string, unknown> = {
    "/discovery/stats": { owned_books: 1, content_type: "ebook" },
    "/discovery/stats?slug=cal": { owned_books: 10, content_type: "ebook" },
    "/discovery/stats?slug=abs": { owned_books: 20, content_type: "audiobook" },
    "/health": { dispatcher_ready: true },
    "/v1/mam/status": { enabled: true, ratio: 2, wedges: 3 },
    "/v1/grabs/budget": { budget_used: 5, budget_cap: 200 },
    "/v1/review": { pending_count: 4 },
    "/v1/tentative": { items: [{}, {}] },
    "/v1/data/counts": { grabs: 9 },
    "/v1/grabs/recent": { grabs: [{ torrent_name: "A" }] },
    "/v1/settings": { abs_web_url: "https://abs.invalid" },
    "/discovery/scan-status": scansIdle,
    ...over,
  };
  mockGet.mockImplementation(async (url: string) => {
    const v = table[url];
    if (v instanceof Error) throw v;
    if (v === undefined) throw new Error(`no route ${url}`);
    return v;
  });
}

async function loaded() {
  const hook = renderHook(() => useDashboardData());
  await waitFor(() => expect(hook.result.current.refreshes).toBe(1));
  return hook;
}

describe("useDashboardData", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockSse.mockReset();
    routes();
  });

  it("loads the fan-out and one /stats per library, ebook and audiobook side by side", async () => {
    const { result } = await loaded();
    expect(result.current.reviewCount).toBe(4);
    expect(result.current.tentativeCount).toBe(2);
    expect(result.current.grabs).toEqual([{ torrent_name: "A" }]);
    expect(result.current.statsBySlug).toEqual({
      cal: { owned_books: 10, content_type: "ebook" },
      abs: { owned_books: 20, content_type: "audiobook" },
    });
    expect(result.current.ebookStats.owned_books).toBe(10);
    expect(result.current.audiobookStats?.owned_books).toBe(20);
  });

  it("each failed request falls back on its own; the active library stands in for the ebook side", async () => {
    const down = new Error("down");
    routes({
      "/v1/review": down, "/v1/tentative": down, "/v1/grabs/recent": down, "/health": down,
      "/discovery/stats?slug=cal": down, "/discovery/stats?slug=abs": down,
    });
    const { result } = await loaded();
    expect(result.current.reviewCount).toBe(0);
    expect(result.current.tentativeCount).toBe(0);
    expect(result.current.grabs).toEqual([]);
    expect(result.current.health).toBeNull();
    expect(result.current.mam?.ratio).toBe(2);
    expect(result.current.statsBySlug).toEqual({});
    expect(result.current.ebookStats.owned_books).toBe(1);
    expect(result.current.audiobookStats).toBeUndefined();
  });

  it("a failed scan-status keeps the scans last shown", async () => {
    const { result } = await loaded();
    expect(result.current.scans).toHaveLength(3);
    routes({ "/discovery/scan-status": new Error("down") });
    await act(async () => { await result.current.refresh(); });
    expect(result.current.refreshes).toBe(2);
    expect(result.current.scans).toHaveLength(3);
  });

  it("an SSE mam-stats event patches the MAM figures in place", async () => {
    const { result } = await loaded();
    const handlers = last(mockSse.mock.calls)[0] as Record<string, (e: unknown) => void>;
    act(() => { handlers["mam-stats"]({ ratio: 2.5, seedbonus: 7, wedges: 9, upload_buffer_bytes: 1 }); });
    expect(result.current.mam).toMatchObject({ enabled: true, ratio: 2.5, wedges: 9, seedbonus: 7 });
  });
});

describe("useDashboardCommands", () => {
  beforeEach(() => {
    mockPost.mockReset();
  });

  const commands = () => {
    const refresh = vi.fn(async () => {});
    const onFail = vi.fn();
    const hook = renderHook(() => useDashboardCommands(refresh, onFail));
    return { ...hook, refresh, onFail };
  };

  it("each command posts its path and refreshes after", async () => {
    mockPost.mockResolvedValue({});
    const { result, refresh, onFail } = commands();
    await act(async () => { await result.current.triggerSync("calibre-library"); });
    await act(async () => { await result.current.triggerSync(); });
    await act(async () => { await result.current.triggerEbookSources(); });
    await act(async () => { await result.current.triggerAudiobookSources(); });
    await act(async () => { await result.current.triggerMam(); });
    await act(async () => { await result.current.cancelSources(); });
    await act(async () => { await result.current.cancelMam(); });
    await act(async () => { await result.current.triggerHygiene(); });
    await act(async () => { await result.current.cancelHygiene(); });
    expect(mockPost.mock.calls.map((c) => c[0])).toEqual([
      "/discovery/sync/library?slug=calibre-library",
      "/discovery/sync/library",
      "/discovery/lookup?content_type=ebook",
      "/discovery/lookup?content_type=audiobook",
      "/discovery/mam/scan",
      "/discovery/lookup/cancel",
      "/discovery/mam/scan/cancel",
      "/discovery/hygiene/run",
      "/discovery/hygiene/cancel",
    ]);
    expect(refresh).toHaveBeenCalledTimes(9);
    expect(onFail).not.toHaveBeenCalled();
  });

  it("a failure goes to onFail as '<what>: <reason>' and the busy flag clears", async () => {
    mockPost.mockRejectedValue(new Error("A source scan is already running"));
    const { result, onFail, refresh } = commands();
    await act(async () => { await result.current.triggerEbookSources(); });
    expect(onFail).toHaveBeenCalledWith("Couldn't start the ebook source scan: A source scan is already running");
    expect(result.current.scanning).toBe(false);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("the clicked library's Sync spins while it runs", async () => {
    let finish!: () => void;
    mockPost.mockImplementation(() => new Promise((r) => { finish = () => r({}); }));
    const { result } = commands();
    let run!: Promise<void>;
    act(() => { run = result.current.triggerSync("abs"); });
    expect(result.current.syncingSlug).toBe("abs");
    await act(async () => { finish(); await run; });
    expect(result.current.syncingSlug).toBeNull();
  });

  it("starting Data Hygiene closes its confirm, even when the start fails", async () => {
    mockPost.mockRejectedValue(new Error("database is locked"));
    const { result, onFail } = commands();
    act(() => { result.current.setShowHygieneConfirm(true); });
    await act(async () => { await result.current.triggerHygiene(); });
    expect(result.current.showHygieneConfirm).toBe(false);
    expect(result.current.hygieneStarting).toBe(false);
    expect(onFail).toHaveBeenCalledWith("Couldn't start Data Hygiene: database is locked");
  });
});

describe("useDashboard", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
    mockInterval.mockReset();
    routes();
  });

  const lastPace = () => last(mockInterval.mock.calls)[1];

  it("polls every 30s when idle, every 3s while a scan runs", async () => {
    const { result } = renderHook(() => useDashboard(vi.fn()));
    await waitFor(() => expect(result.current.refreshes).toBe(1));
    expect(lastPace()).toBe(30_000);
    routes({ "/discovery/scan-status": { scans: [lib("cal", "ebook", true)] } });
    await act(async () => { await result.current.refresh(); });
    expect(lastPace()).toBe(3000);
  });

  it("polls every 3s while a sync request is in flight", async () => {
    const { result } = renderHook(() => useDashboard(vi.fn()));
    await waitFor(() => expect(result.current.refreshes).toBe(1));
    let finish!: () => void;
    mockPost.mockImplementation(() => new Promise((r) => { finish = () => r({}); }));
    let run!: Promise<void>;
    act(() => { run = result.current.triggerSync("cal"); });
    expect(lastPace()).toBe(3000);
    await act(async () => { finish(); await run; });
    await waitFor(() => expect(lastPace()).toBe(30_000));
  });
});
