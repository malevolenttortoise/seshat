import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useMamSection } from "./useMamSection";
import { api } from "../api";

vi.mock("../api", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    isAbort: (e: unknown) => e instanceof DOMException && e.name === "AbortError",
  },
  slugQuery: (slug?: string | null) => (slug ? `?slug=${encodeURIComponent(slug)}` : ""),
}));
const mockGet = vi.mocked(api.get);
const mockPost = vi.mocked(api.post);

let scanState: { running: boolean } = { running: false };

function routes(url: string): unknown {
  if (url === "/discovery/mam/status") return { enabled: true, stats: { upload_candidates: 7, available_to_download: 3, missing_everywhere: 2, total_unscanned: 9 } };
  if (url === "/discovery/mam/scan/status") return scanState;
  if (url === "/discovery/scan-status") return { scans: [
    { kind: "library", slug: "calibre-library", content_type: "ebook", label: "Calibre Library Sync" },
    { kind: "lookup", slug: "", label: "Source scan" },
  ] };
  if (url === "/discovery/pipeline/status") return { configured: true, reachable: true };
  if (url.startsWith("/discovery/mam/books?")) return { books: [{ id: 1, title: "A" }], total: 120 };
  throw new Error(`unexpected ${url}`);
}

const booksCalls = () => mockGet.mock.calls.map(([u]) => String(u)).filter((u) => u.startsWith("/discovery/mam/books?"));
// (No Array.prototype.at: the app's lib is ES2020.)
const lastBooksCall = () => booksCalls()[booksCalls().length - 1];

describe("useMamSection", () => {
  beforeEach(() => {
    sessionStorage.clear();
    scanState = { running: false };
    mockGet.mockReset();
    mockPost.mockReset();
    mockGet.mockImplementation(async (url: string) => routes(url));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads counts, libraries, pipeline and the first page on mount", async () => {
    const { result } = renderHook(() => useMamSection());
    await waitFor(() => expect(result.current.ld).toBe(false));
    await waitFor(() => expect(result.current.pipelineReady).toBe(true));
    expect(result.current.counts).toEqual({ upload: 7, download: 3, missing: 2, unscanned: 9 });
    expect(result.current.libs).toEqual([{ slug: "calibre-library", content_type: "ebook", label: "Calibre Library" }]);
    expect(booksCalls()).toEqual(["/discovery/mam/books?section=upload&search=&sort=title&page=1&per_page=50"]);
    expect(result.current.totalPages).toBe(3);
  });

  it("adds the library slug and resets search and sort when the tab changes", async () => {
    const { result } = renderHook(() => useMamSection());
    await waitFor(() => expect(result.current.ld).toBe(false));
    act(() => result.current.setLibSlug("audiobookshelf"));
    await waitFor(() => expect(lastBooksCall()).toContain("&slug=audiobookshelf"));
    act(() => { result.current.setQ("orchard"); result.current.setSort("author"); });
    act(() => result.current.switchTab("download"));
    await waitFor(() => expect(lastBooksCall()).toBe(
      "/discovery/mam/books?section=download&search=&sort=title&page=1&per_page=50&slug=audiobookshelf"));
  });

  it("startScan reports the server's error, or starts the progress banner", async () => {
    const { result } = renderHook(() => useMamSection());
    await waitFor(() => expect(result.current.ld).toBe(false));
    mockPost.mockResolvedValueOnce({ error: "MAM is disabled" });
    let err: string | null = null;
    await act(async () => { err = await result.current.startScan(25); });
    expect(mockPost).toHaveBeenCalledWith("/discovery/mam/scan?limit=25");
    expect(err).toBe("MAM is disabled");
    expect(result.current.mamScan).toBeNull();

    mockPost.mockResolvedValueOnce({ total: 0 });
    await act(async () => { err = await result.current.startScan(""); });
    expect(err).toBeNull();
    expect(result.current.mamScan).toMatchObject({ running: true, total: 100, status: "scanning" });
    expect(result.current.scanStarting).toBe(false);

    mockPost.mockRejectedValueOnce(new Error("net"));
    await act(async () => { err = await result.current.startScan(10); });
    expect(err).toBe("Failed to start scan");
  });

  it("polls a running scan every 5s and refreshes counts + list when it ends", async () => {
    vi.useFakeTimers();
    scanState = { running: true };
    const { result } = renderHook(() => useMamSection());
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.mamScan?.running).toBe(true);
    const before = booksCalls().length;
    scanState = { running: false };
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(result.current.mamScan?.running).toBe(false);
    expect(booksCalls().length).toBe(before + 1);
    const statusCalls = mockGet.mock.calls.filter(([u]) => u === "/discovery/mam/scan/status").length;
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(mockGet.mock.calls.filter(([u]) => u === "/discovery/mam/scan/status").length).toBe(statusCalls);
  });

  it("hides / dismisses a book and reloads the current page", async () => {
    const { result } = renderHook(() => useMamSection());
    await waitFor(() => expect(result.current.ld).toBe(false));
    mockPost.mockResolvedValue({});
    const before = booksCalls().length;
    await act(async () => { await result.current.onAction("hide", 5, "audiobookshelf"); });
    expect(mockPost).toHaveBeenCalledWith("/discovery/books/5/hide?slug=audiobookshelf");
    await act(async () => { await result.current.onAction("dismiss", 6); });
    expect(mockPost).toHaveBeenLastCalledWith("/discovery/books/6/dismiss");
    expect(booksCalls().length).toBe(before + 2);
  });
});
