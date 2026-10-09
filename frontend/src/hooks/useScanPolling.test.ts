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

// Advance fake timers + flush the resulting async polls inside act().
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

describe("useScanPolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockGet.mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reports a lookup running→idle transition once", async () => {
    mockGet
      .mockResolvedValueOnce(status({ kind: "lookup", running: true }))
      .mockResolvedValue(status({ kind: "lookup", running: false }));
    const onComplete = vi.fn();

    renderHook(() => useScanPolling({ kinds: ["lookup"], onComplete, intervalMs: 3000 }));

    // The immediate poll sees it running: no transition yet.
    await advance(0);
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith("/discovery/scan-status");
    expect(onComplete).not.toHaveBeenCalled();

    await advance(3000);
    expect(onComplete).toHaveBeenCalledExactlyOnceWith(["lookup"]);

    // Later idle polls don't report it again.
    await advance(3000);
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it("reports two scans finishing in the same poll in one call", async () => {
    mockGet
      .mockResolvedValueOnce(status({ kind: "lookup", running: true }, { kind: "mam", running: true }))
      .mockResolvedValue(status({ kind: "lookup", running: false }, { kind: "mam", running: false }));
    const onComplete = vi.fn();

    renderHook(() => useScanPolling({ onComplete }));
    await advance(0);
    await advance(3000);
    expect(onComplete).toHaveBeenCalledExactlyOnceWith(["lookup", "mam"]);
  });

  it("does not report a scan that was never running", async () => {
    mockGet.mockResolvedValue(status({ kind: "lookup", running: false }));
    const onComplete = vi.fn();

    renderHook(() => useScanPolling({ kinds: ["lookup"], onComplete }));
    await advance(0);
    await advance(3000);
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("ignores a kind it wasn't asked to watch", async () => {
    mockGet
      .mockResolvedValueOnce(status({ kind: "library", running: true }))
      .mockResolvedValue(status({ kind: "library", running: false }));
    const onComplete = vi.fn();

    renderHook(() => useScanPolling({ onComplete }));
    await advance(0);
    await advance(3000);
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("keeps polling after a failed request", async () => {
    mockGet
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(status({ kind: "mam", running: true }))
      .mockResolvedValue(status({ kind: "mam", running: false }));
    const onComplete = vi.fn();

    renderHook(() => useScanPolling({ onComplete }));
    await advance(0);
    await advance(3000);
    await advance(3000);
    expect(onComplete).toHaveBeenCalledExactlyOnceWith(["mam"]);
  });

  it("restarts with fresh flags and an immediate poll when restartKey changes", async () => {
    mockGet.mockResolvedValueOnce(status({ kind: "lookup", running: true }));
    const onComplete = vi.fn();

    const { rerender } = renderHook(({ k }) => useScanPolling({ onComplete, restartKey: k }), {
      initialProps: { k: 1 },
    });
    await advance(0);
    expect(mockGet).toHaveBeenCalledTimes(1);

    // New key: an immediate poll, and the old "running" flag is gone, so
    // an idle answer is not a transition.
    mockGet.mockResolvedValue(status({ kind: "lookup", running: false }));
    rerender({ k: 2 });
    await advance(0);
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("does not restart when only the callback changes", async () => {
    mockGet.mockResolvedValue(status({ kind: "lookup", running: false }));
    const { rerender } = renderHook(({ cb }) => useScanPolling({ onComplete: cb }), {
      initialProps: { cb: vi.fn() },
    });
    await advance(0);
    rerender({ cb: vi.fn() });
    await advance(0);
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("stops polling when enabled=false, and on unmount", async () => {
    mockGet.mockResolvedValue(status({ kind: "lookup", running: false }));
    renderHook(() => useScanPolling({ enabled: false }));
    await advance(6000);
    expect(mockGet).not.toHaveBeenCalled();

    const { unmount } = renderHook(() => useScanPolling());
    await advance(0);
    unmount();
    await advance(9000);
    expect(mockGet).toHaveBeenCalledTimes(1);
  });
});
