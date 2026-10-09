import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLogFeed } from "./useLogFeed";
import { api } from "../api";

vi.mock("../api", () => ({ api: { get: vi.fn() } }));
const mockGet = vi.mocked(api.get);

const urls = () => mockGet.mock.calls.map(([u]) => String(u));

describe("useLogFeed", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockGet.mockImplementation(async (url: string) =>
      url.startsWith("/v1/announces")
        ? { rows: [{ id: 1 }], total_matched: 1, decision_counts: { allow: 1 } }
        : { entries: [{ ts: "t", level: "INFO", logger: "seshat", message: "m", is_announce: false }], total_buffered: 7 });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads the last 2000 lines on mount", async () => {
    const { result } = renderHook(() => useLogFeed());
    await waitFor(() => expect(result.current.entries).toHaveLength(1));
    expect(urls()).toEqual(["/v1/logs?lines=2000"]);
    expect(result.current.total).toBe(7);
  });

  it("asks for a category per tab", async () => {
    const { result } = renderHook(() => useLogFeed());
    await waitFor(() => expect(result.current.entries).not.toBeNull());
    for (const tab of ["application", "irc", "scans"] as const) {
      act(() => result.current.setTab(tab));
      await waitFor(() => expect(urls()).toContain(`/v1/logs?lines=2000&category=${tab}`));
    }
  });

  it("the Announces tab asks the audit, with the decision and the text filter (debounced)", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useLogFeed());
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    act(() => result.current.setTab("announces"));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(urls()).toContain("/v1/announces?limit=500");
    expect(result.current.announces?.total_matched).toBe(1);
    expect(result.current.entries).toEqual([]);
    act(() => result.current.setDecisionFilter("skip"));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(urls()).toContain("/v1/announces?limit=500&decision=skip");
    act(() => result.current.setFilter("  orchard "));
    await act(async () => { await vi.advanceTimersByTimeAsync(249); });
    expect(urls()).not.toContain("/v1/announces?limit=500&decision=skip&q=orchard");
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(urls()).toContain("/v1/announces?limit=500&decision=skip&q=orchard");
  });

  it("reloads every 5s while auto-scroll is on, and not while it's off", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useLogFeed());
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(mockGet).toHaveBeenCalledTimes(2);
    act(() => result.current.setAutoScroll(false));
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it("reports a failed load", async () => {
    mockGet.mockRejectedValue(new Error("502"));
    const { result } = renderHook(() => useLogFeed());
    await waitFor(() => expect(result.current.error).toBe("Error: 502"));
  });
});
