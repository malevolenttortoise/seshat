import { renderHook, waitFor, act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useMamEnabled } from "./useMamEnabled";
import { api } from "../api";

vi.mock("../api", () => ({ api: { get: vi.fn() } }));
const mockGet = vi.mocked(api.get);

describe("useMamEnabled", () => {
  // Braces: a function returned from beforeEach is run as a teardown,
  // and mockReset() returns the mock.
  beforeEach(() => {
    mockGet.mockReset();
  });

  it("is false until MAM status answers, then follows `enabled`", async () => {
    mockGet.mockResolvedValue({ enabled: true });
    const { result } = renderHook(() => useMamEnabled());
    expect(result.current).toBe(false);
    await waitFor(() => expect(result.current).toBe(true));
    expect(mockGet).toHaveBeenCalledExactlyOnceWith("/discovery/mam/status");
  });

  it("stays false when MAM is off or the request fails", async () => {
    mockGet.mockResolvedValue({ enabled: false });
    const off = renderHook(() => useMamEnabled());
    await act(async () => { await Promise.resolve(); });
    expect(off.result.current).toBe(false);

    mockGet.mockRejectedValue(new Error("401"));
    const failed = renderHook(() => useMamEnabled());
    await act(async () => { await Promise.resolve(); });
    expect(failed.result.current).toBe(false);
  });

  it("asks once per mount, not per render", async () => {
    mockGet.mockResolvedValue({ enabled: true });
    const { rerender } = renderHook(() => useMamEnabled());
    rerender();
    rerender();
    await act(async () => { await Promise.resolve(); });
    expect(mockGet).toHaveBeenCalledTimes(1);
  });
});
