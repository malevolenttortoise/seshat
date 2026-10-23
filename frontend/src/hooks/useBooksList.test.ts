import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useBooksList, type UseBooksListOptions } from "./useBooksList";
import { api } from "../api";

vi.mock("../api", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    del: vi.fn(),
    isAbort: (e: unknown) => e instanceof DOMException && e.name === "AbortError",
  },
  slugQuery: (slug?: string | null) => (slug ? `?slug=${encodeURIComponent(slug)}` : ""),
}));
const mockGet = vi.mocked(api.get);
const mockPost = vi.mocked(api.post);
const mockDel = vi.mocked(api.del);

const base: UseBooksListOptions = {
  title: "Missing", apiPath: "/discovery/missing", extraParams: {}, showFormatTabs: true, showOwnedFilter: false,
};
const gets = () => mockGet.mock.calls.map(([u]) => String(u));
const lastGet = () => gets()[gets().length - 1];

describe("useBooksList", () => {
  beforeEach(() => {
    sessionStorage.clear();
    mockGet.mockReset(); mockPost.mockReset(); mockDel.mockReset();
    mockGet.mockResolvedValue({ books: [{ id: 1, title: "A" }], total: 130 });
  });

  it("the phone's request: no sort_dir, 60 per page", async () => {
    const { result } = renderHook(() => useBooksList(base));
    await waitFor(() => expect(result.current.ld).toBe(false));
    expect(gets()).toEqual(["/discovery/missing?search=&sort=title&per_page=60&page=1&content_type=all"]);
    expect(result.current.totalPages).toBe(3);
  });

  it("desktop's request: sort_dir, and grouping overrides the sort and fetches everything", async () => {
    const { result, rerender } = renderHook((o: UseBooksListOptions) => useBooksList(o), {
      initialProps: { ...base, sortDir: "asc", perPage: 60 },
    });
    await waitFor(() => expect(result.current.ld).toBe(false));
    expect(lastGet()).toBe("/discovery/missing?search=&sort=title&sort_dir=asc&per_page=60&page=1&content_type=all");
    rerender({ ...base, sortDir: "asc", perPage: 5000, sortOverride: "series" });
    await waitFor(() => expect(lastGet()).toBe(
      "/discovery/missing?search=&sort=series&sort_dir=asc&per_page=5000&page=1&content_type=all"));
  });

  it("sends extra params, the MAM filter and the owned filter where the page has one", async () => {
    const { result } = renderHook(() => useBooksList({
      ...base, apiPath: "/discovery/books/hidden", title: "Hidden Books", showOwnedFilter: true, showFormatTabs: false,
      extraParams: { owned: "1" },
    }));
    await waitFor(() => expect(result.current.ld).toBe(false));
    expect(lastGet()).toBe("/discovery/books/hidden?search=&sort=title&per_page=60&page=1&owned=1&filtered=false");
    act(() => { result.current.setMamFilter("found"); result.current.setOwnedFilter("discovered"); });
    await waitFor(() => expect(lastGet()).toBe(
      "/discovery/books/hidden?search=&sort=title&per_page=60&page=1&owned=false&mam_status=found&filtered=false"));
  });

  it("Hidden: the two tabs, a chip's reasons, and the counts per tab and chip (F19)", async () => {
    mockGet.mockImplementation(async (u: string) =>
      String(u).startsWith("/discovery/books/hidden/reasons")
        ? { counts: { mine: 4, comic: 10, split: 2, sampler: 1, junk: 3, author_off: 7 } }
        : { books: [], total: 0 });
    const { result } = renderHook(() => useBooksList({
      ...base, apiPath: "/discovery/books/hidden", title: "Hidden Books", showOwnedFilter: true,
    }));
    await waitFor(() => expect(result.current.ld).toBe(false));
    expect(gets()).toEqual([
      "/discovery/books/hidden/reasons?content_type=all",
      "/discovery/books/hidden?search=&sort=title&per_page=60&page=1&content_type=all&filtered=false",
    ]);
    expect(result.current.hiddenCounts).toEqual({
      mine: 4, filtered: 23,
      byGroup: { comic: 10, box_set: 0, anthology: 0, not_english: 0, split: 3, not_a_book: 3, author_off: 7 },
    });
    act(() => { result.current.setHiddenTab("filtered"); result.current.setReasonGroup("split"); });
    await waitFor(() => expect(lastGet()).toBe(
      "/discovery/books/hidden?search=&sort=title&per_page=60&page=1&content_type=all&filtered=true&reason=split%2Csampler%2Clisting"));
    act(() => { result.current.setReasonGroup(""); });
    await waitFor(() => expect(lastGet()).toBe(
      "/discovery/books/hidden?search=&sort=title&per_page=60&page=1&content_type=all&filtered=true"));
  });

  it("Missing and Library hide short books when asked; other lists have no such filter (F11)", async () => {
    const { result } = renderHook(() => useBooksList(base));
    await waitFor(() => expect(result.current.ld).toBe(false));
    expect(result.current.showShortFilter).toBe(true);
    act(() => { result.current.setHideShort(true); });
    await waitFor(() => expect(lastGet()).toBe(
      "/discovery/missing?search=&sort=title&per_page=60&page=1&content_type=all&hide_short=true"));
    const up = renderHook(() => useBooksList({ ...base, title: "Upcoming", apiPath: "/discovery/upcoming" }));
    await waitFor(() => expect(up.result.current.ld).toBe(false));
    expect(up.result.current.showShortFilter).toBe(false);
    expect(lastGet()).toBe("/discovery/upcoming?search=&sort=title&per_page=60&page=1&content_type=all");
  });

  it("keeps search / sort / filters per page title", async () => {
    const a = renderHook(() => useBooksList(base));
    await waitFor(() => expect(a.result.current.ld).toBe(false));
    act(() => { a.result.current.setQ("orchard"); a.result.current.setSort("author"); });
    a.unmount();
    const b = renderHook(() => useBooksList(base));
    expect(b.result.current.q).toBe("orchard");
    expect(b.result.current.sort).toBe("author");
    const other = renderHook(() => useBooksList({ ...base, title: "Upcoming" }));
    expect(other.result.current.q).toBe("");
  });

  it("acts on a book and reloads the current page", async () => {
    const { result } = renderHook(() => useBooksList(base));
    await waitFor(() => expect(result.current.ld).toBe(false));
    await act(async () => { await result.current.load(2); });
    mockPost.mockResolvedValue({}); mockDel.mockResolvedValue({});
    await act(async () => { await result.current.onAction("unhide", 4, "audiobookshelf"); });
    expect(mockPost).toHaveBeenCalledWith("/discovery/books/4/unhide?slug=audiobookshelf");
    expect(lastGet()).toContain("&page=2");
    await act(async () => { await result.current.onAction("delete", 5); });
    expect(mockDel).toHaveBeenCalledWith("/discovery/books/5");
  });
});
