import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useListMissing } from "./useListMissing";
import { api } from "../api";
import { toast } from "../lib/toast";

vi.mock("../api", () => ({
  api: {
    get: vi.fn(),
    put: vi.fn(),
    isAbort: (e: unknown) => e instanceof DOMException && e.name === "AbortError",
  },
}));
const mockGet = vi.mocked(api.get);
const mockPut = vi.mocked(api.put);

const opts = { authorId: 7482, slug: "calibre-library", authorName: "Tom Clancy" };

describe("useListMissing (D6 / F18, G200)", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPut.mockReset();
  });

  it("reads this library row's switch", async () => {
    mockGet.mockResolvedValue({ on: true, missing: 434 });
    const { result } = renderHook(() => useListMissing(opts));
    await waitFor(() => expect(result.current.st).toEqual({ on: true, missing: 434 }));
    expect(mockGet.mock.calls[0][0]).toBe("/discovery/authors/7482/list-missing?slug=calibre-library");
  });

  it("turning it off sends on: false, says how many were hidden, reloads", async () => {
    const success = vi.spyOn(toast, "success");
    mockGet.mockResolvedValueOnce({ on: true, missing: 434 }).mockResolvedValueOnce({ on: false, missing: 434 });
    mockPut.mockResolvedValue({ on: false, hidden: 434 });
    const onChanged = vi.fn();
    const { result } = renderHook(() => useListMissing({ ...opts, onChanged }));
    await waitFor(() => expect(result.current.st?.on).toBe(true));
    await act(async () => { await result.current.toggle(); });
    expect(mockPut).toHaveBeenCalledWith("/discovery/authors/7482/list-missing?slug=calibre-library", { on: false });
    expect(success).toHaveBeenCalledWith("Tom Clancy: missing books no longer listed, 434 hidden");
    expect(result.current.st).toEqual({ on: false, missing: 434 });
    expect(onChanged).toHaveBeenCalled();
    success.mockRestore();
  });

  it("a load that fails says so instead of hiding the switch", async () => {
    mockGet.mockRejectedValue(new Error("database is locked"));
    const { result } = renderHook(() => useListMissing(opts));
    await waitFor(() => expect(result.current.err).toBe("database is locked"));
    expect(result.current.st).toBeNull();
  });

  it("no author id, no request", async () => {
    renderHook(() => useListMissing({ ...opts, authorId: undefined }));
    expect(mockGet).not.toHaveBeenCalled();
  });
});
