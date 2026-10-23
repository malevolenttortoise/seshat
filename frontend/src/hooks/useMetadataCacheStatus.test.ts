import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useMetadataCacheStatus, type CacheSource } from "./useMetadataCacheStatus";
import { api } from "../api";

vi.mock("../api", () => ({
  api: {
    get: vi.fn(),
    isAbort: (e: unknown) => e instanceof DOMException && e.name === "AbortError",
  },
}));
const mockGet = vi.mocked(api.get);

type Status = { n: number; mode?: string };

const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const calls = (source: CacheSource) =>
  mockGet.mock.calls.filter(([u]) => u === `/v1/metadata-cache/${source}/status`).length;

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("useMetadataCacheStatus", () => {
  let n = 0;
  beforeEach(() => {
    vi.useFakeTimers();
    n = 0;
    mockGet.mockReset();
    mockGet.mockImplementation(async () => ({ n: ++n }));
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("fetches on mount and polls at its pace", async () => {
    const { result, unmount } = renderHook(() => useMetadataCacheStatus<Status>("amazon", { paceMs: 60_000 }));
    await advance(0);
    expect(calls("amazon")).toBe(1);
    expect(result.current.status).toEqual({ n: 1 });
    await advance(60_000);
    expect(calls("amazon")).toBe(2);
    unmount();
  });

  it("shares one request per tick between subscribers of a source", async () => {
    const a = renderHook(() => useMetadataCacheStatus<Status>("goodreads", { paceMs: 60_000 }));
    const b = renderHook(() => useMetadataCacheStatus<Status>("goodreads", { paceMs: 60_000 }));
    await advance(0);
    // Both mounted in the same tick: one in-flight request served both.
    expect(calls("goodreads")).toBe(1);
    expect(a.result.current.status).toEqual(b.result.current.status);
    await advance(60_000);
    expect(calls("goodreads")).toBe(2);
    await advance(60_000);
    expect(calls("goodreads")).toBe(3);
    a.unmount();
    b.unmount();
  });

  it("polls at the fastest subscriber's pace, and slows when it leaves", async () => {
    const icon = renderHook(() => useMetadataCacheStatus<Status>("amazon", { paceMs: 60_000 }));
    await advance(0);
    const card = renderHook(() => useMetadataCacheStatus<Status>("amazon", { paceMs: 30_000 }));
    await advance(0);
    expect(calls("amazon")).toBe(2); // the card's mount fetch
    await advance(30_000);
    expect(calls("amazon")).toBe(3);
    await advance(30_000);
    expect(calls("amazon")).toBe(4);
    card.unmount();
    await advance(30_000);
    expect(calls("amazon")).toBe(4);
    await advance(30_000);
    expect(calls("amazon")).toBe(5);
    icon.unmount();
  });

  it("hands a late subscriber the last status at once, then refreshes", async () => {
    const first = renderHook(() => useMetadataCacheStatus<Status>("amazon", { paceMs: 60_000 }));
    await advance(0);
    let resolve!: (v: Status) => void;
    mockGet.mockImplementationOnce(() => new Promise((r) => { resolve = r as (v: Status) => void; }));
    const late = renderHook(() => useMetadataCacheStatus<Status>("amazon", { paceMs: 60_000 }));
    await act(async () => { await Promise.resolve(); });
    expect(late.result.current.status).toEqual({ n: 1 });
    await act(async () => { resolve({ n: 99 }); });
    expect(late.result.current.status).toEqual({ n: 99 });
    expect(first.result.current.status).toEqual({ n: 99 });
    first.unmount();
    late.unmount();
  });

  it("skips ticks while the tab is hidden and catches up when it's back", async () => {
    const { unmount } = renderHook(() => useMetadataCacheStatus<Status>("goodreads", { paceMs: 60_000 }));
    await advance(0);
    setHidden(true);
    await advance(180_000);
    expect(calls("goodreads")).toBe(1);
    setHidden(false);
    await advance(0);
    expect(calls("goodreads")).toBe(2);
    // The cadence restarts from the catch-up.
    await advance(59_000);
    expect(calls("goodreads")).toBe(2);
    await advance(1_000);
    expect(calls("goodreads")).toBe(3);
    unmount();
  });

  it("reports a failed fetch to subscribers that listen, keeping the last status", async () => {
    const onError = vi.fn();
    const card = renderHook(() => useMetadataCacheStatus<Status>("amazon", { paceMs: 30_000, onError }));
    const icon = renderHook(() => useMetadataCacheStatus<Status>("amazon", { paceMs: 60_000 }));
    await advance(0);
    mockGet.mockRejectedValueOnce(new Error("503"));
    await advance(30_000);
    expect(onError).toHaveBeenCalledExactlyOnceWith("503");
    expect(card.result.current.status).toEqual({ n: 1 });
    expect(icon.result.current.status).toEqual({ n: 1 });
    card.unmount();
    icon.unmount();
  });

  it("an optimistic override shows until the next fetched status, for that subscriber only", async () => {
    const onData = vi.fn();
    const card = renderHook(() => useMetadataCacheStatus<Status>("goodreads", { paceMs: 30_000, onData }));
    const icon = renderHook(() => useMetadataCacheStatus<Status>("goodreads", { paceMs: 60_000 }));
    await advance(0);
    expect(onData).toHaveBeenCalledTimes(1);
    act(() => card.result.current.setStatus({ n: 1, mode: "disabled" }));
    expect(card.result.current.status).toEqual({ n: 1, mode: "disabled" });
    expect(icon.result.current.status).toEqual({ n: 1 });
    expect(onData).toHaveBeenCalledTimes(1); // overrides aren't "fetched"
    await advance(30_000);
    expect(card.result.current.status).toEqual({ n: 2 });
    card.unmount();
    icon.unmount();
  });

  it("refresh() fetches now for everyone", async () => {
    const a = renderHook(() => useMetadataCacheStatus<Status>("amazon", { paceMs: 60_000 }));
    const b = renderHook(() => useMetadataCacheStatus<Status>("amazon", { paceMs: 60_000 }));
    await advance(0);
    await act(async () => { await a.result.current.refresh(); });
    expect(calls("amazon")).toBe(2);
    expect(b.result.current.status).toEqual({ n: 2 });
    a.unmount();
    b.unmount();
  });

  it("refresh() during a fetch in flight fetches again after it (an action changed the status)", async () => {
    const card = renderHook(() => useMetadataCacheStatus<Status>("amazon", { paceMs: 30_000 }));
    await advance(0);
    let resolve!: (v: Status) => void;
    mockGet.mockImplementationOnce(() => new Promise((r) => { resolve = r as (v: Status) => void; }));
    await advance(30_000); // a poll is now in flight, started before the action
    expect(calls("amazon")).toBe(2);
    let refreshed!: Promise<void>;
    act(() => { refreshed = card.result.current.refresh(); });
    await act(async () => { resolve({ n: 50 }); await refreshed; });
    expect(calls("amazon")).toBe(3);
    // The fetch after the action wins ({ n: 2 }: the pending one didn't count).
    expect(card.result.current.status).toEqual({ n: 2 });
    card.unmount();
  });

  it("forgets the status and stops polling when the last subscriber leaves", async () => {
    const first = renderHook(() => useMetadataCacheStatus<Status>("goodreads", { paceMs: 60_000 }));
    await advance(0);
    first.unmount();
    await advance(180_000);
    expect(calls("goodreads")).toBe(1);

    let resolve!: (v: Status) => void;
    mockGet.mockImplementationOnce(() => new Promise((r) => { resolve = r as (v: Status) => void; }));
    const next = renderHook(() => useMetadataCacheStatus<Status>("goodreads", { paceMs: 60_000 }));
    await act(async () => { await Promise.resolve(); });
    expect(next.result.current.status).toBeNull(); // "loading", as a fresh mount always was
    await act(async () => { resolve({ n: 7 }); });
    expect(next.result.current.status).toEqual({ n: 7 });
    next.unmount();
  });

  it("keeps the two sources apart", async () => {
    const a = renderHook(() => useMetadataCacheStatus<Status>("amazon", { paceMs: 60_000 }));
    const g = renderHook(() => useMetadataCacheStatus<Status>("goodreads", { paceMs: 30_000 }));
    await advance(0);
    await advance(60_000);
    expect(calls("amazon")).toBe(2);
    expect(calls("goodreads")).toBe(3);
    a.unmount();
    g.unmount();
  });
});
