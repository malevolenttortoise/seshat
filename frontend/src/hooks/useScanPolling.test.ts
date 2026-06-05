import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useScanPolling } from "./useScanPolling";
import { api } from "../api";
import type { ScanProgress, ScanStatusResponse } from "../types";

// Mock the api module — the hook's only external dependency.
vi.mock("../api", () => ({ api: { get: vi.fn() } }));
const mockGet = vi.mocked(api.get);

function status(...entries: { kind: ScanProgress["kind"]; running: boolean }[]): ScanStatusResponse {
  return {
    scans: entries.map((e) => ({
      kind: e.kind,
      type: "scan",
      label: e.kind,
      running: e.running,
      current: 0,
      total: 0,
      status: e.running ? "running" : "idle",
    })),
  };
}

// Advance fake timers + flush the resulting async polls inside act() so
// React state updates from setRunning aren't flagged as unwrapped.
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

describe("useScanPolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockGet.mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("fires onComplete once on a lookup running→idle transition", async () => {
    // poll #1 (immediate): running. poll #2+ : idle.
    mockGet
      .mockResolvedValueOnce(status({ kind: "lookup", running: true }))
      .mockResolvedValue(status({ kind: "lookup", running: false }));
    const onComplete = vi.fn();

    renderHook(() => useScanPolling({ kinds: ["lookup"], onComplete, intervalMs: 3000 }));

    // flush the immediate tick (running=true) — no transition yet.
    await advance(0);
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(onComplete).not.toHaveBeenCalled();

    // next poll sees idle → running→idle edge fires exactly once.
    await advance(3000);
    expect(onComplete).toHaveBeenCalledExactlyOnceWith("lookup");

    // subsequent idle polls don't re-fire.
    await advance(3000);
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it("does not fire onComplete when a scan is never running", async () => {
    mockGet.mockResolvedValue(status({ kind: "lookup", running: false }));
    const onComplete = vi.fn();

    renderHook(() => useScanPolling({ kinds: ["lookup"], onComplete }));
    await advance(0);
    await advance(3000);
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("tracks running flags per kind independently", async () => {
    mockGet.mockResolvedValue(
      status({ kind: "lookup", running: true }, { kind: "mam", running: false }),
    );
    const { result } = renderHook(() => useScanPolling({ kinds: ["lookup", "mam"] }));
    await advance(0);
    expect(result.current.running).toEqual({ lookup: true, mam: false });
  });

  it("stops polling when enabled=false", async () => {
    mockGet.mockResolvedValue(status({ kind: "lookup", running: false }));
    renderHook(() => useScanPolling({ kinds: ["lookup"], enabled: false }));
    await advance(6000);
    expect(mockGet).not.toHaveBeenCalled();
  });
});
