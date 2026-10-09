import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthorScans } from "./useAuthorScans";
import { api } from "../api";
import { useScanPolling } from "./useScanPolling";

vi.mock("../api", () => ({ api: { post: vi.fn() } }));
vi.mock("./useScanPolling", () => ({ useScanPolling: vi.fn() }));
const mockPost = vi.mocked(api.post);
const mockPoll = vi.mocked(useScanPolling);

// No Array.prototype.at under the app's ES2020 lib.
const last = <T,>(xs: T[]): T => xs[xs.length - 1];

function scans(onFinished?: () => void) {
  const loadA = vi.fn();
  const hook = renderHook(() =>
    useAuthorScans({ authorIdNum: 11, authorSlug: "calibre-library", loadA, onFinished }),
  );
  /** What the scan-finished poll calls when scans of these kinds end. */
  const finish = (...kinds: ("lookup" | "mam")[]) =>
    act(() => { last(mockPoll.mock.calls)[0]!.onComplete!(kinds); });
  return { ...hook, loadA, finish };
}

describe("useAuthorScans", () => {
  beforeEach(() => {
    mockPost.mockReset();
    mockPoll.mockReset();
  });

  it("a started source scan stays busy until the poll sees it finish, then the page reloads", async () => {
    mockPost.mockResolvedValue({ status: "started", author: "Ada Quill" });
    const onFinished = vi.fn();
    const { result, loadA, finish } = scans(onFinished);
    await act(async () => { await result.current.scanSources(); });
    expect(mockPost).toHaveBeenCalledWith("/discovery/authors/11/lookup?slug=calibre-library");
    expect(result.current.sourceBusy).toBe(true);
    await act(async () => { await result.current.scanSources(); });
    expect(mockPost).toHaveBeenCalledTimes(1);
    finish("lookup");
    expect(result.current.sourceBusy).toBe(false);
    expect(loadA).toHaveBeenCalled();
    expect(onFinished).toHaveBeenCalled();
  });

  it("a full re-scan posts full-rescan", async () => {
    mockPost.mockResolvedValue({ status: "started" });
    const { result } = scans();
    await act(async () => { await result.current.fullRescan(); });
    expect(mockPost).toHaveBeenCalledWith("/discovery/authors/11/full-rescan?slug=calibre-library");
  });

  it("a start that fails frees the button at once", async () => {
    mockPost.mockRejectedValue(new Error("busy"));
    const { result } = scans();
    await act(async () => { await result.current.scanSources(); });
    expect(result.current.sourceBusy).toBe(false);
    await act(async () => { await result.current.scanMam(); });
    expect(result.current.mamBusy).toBe(false);
  });

  it("a MAM scan with nothing to scan frees it; a started one waits for the poll", async () => {
    mockPost.mockResolvedValueOnce({ status: "complete", message: "No un-scanned books" });
    const { result, finish } = scans();
    await act(async () => { await result.current.scanMam(); });
    expect(mockPost).toHaveBeenLastCalledWith("/discovery/mam/scan-author/11?slug=calibre-library");
    expect(result.current.mamBusy).toBe(false);
    mockPost.mockResolvedValueOnce({ status: "started", total: 3 });
    await act(async () => { await result.current.scanMam(); });
    expect(result.current.mamBusy).toBe(true);
    finish("lookup");
    expect(result.current.mamBusy).toBe(true);
    finish("mam");
    expect(result.current.mamBusy).toBe(false);
  });
});
