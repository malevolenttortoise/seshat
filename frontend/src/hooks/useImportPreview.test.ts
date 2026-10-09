import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useImportPreview } from "./useImportPreview";
import { api } from "../api";

vi.mock("../api", () => ({ api: { post: vi.fn() } }));
const mockPost = vi.mocked(api.post);

const preview = {
  results: [
    { status: "new", book: { title: "Rootbound", author_name: "Ada Quill", series_options: [{ name: "Orchard Cycle", position: 3 }, { name: "Botanicals", position: 1 }] } },
    { status: "owned", book: { title: "Ninefold Tide" } },
    { status: "error", error: "Couldn't read the page" },
  ],
};
const label = (n: number) => `Fetching ${n}`;

describe("useImportPreview", () => {
  beforeEach(() => {
    mockPost.mockReset();
  });

  it("looks up only the http lines that aren't MAM links", async () => {
    mockPost.mockResolvedValue(preview);
    const { result } = renderHook(() => useImportPreview());
    const text = [
      "  https://books.example.invalid/show/1  ",
      "not a link",
      "https://www.myanonamouse.net/t/900001",
      "https://books.example.invalid/show/2",
    ].join("\n");
    let p!: Promise<void>;
    act(() => { p = result.current.fetchPreview(text, label); });
    expect(result.current.progress).toBe("Fetching 2");
    expect(result.current.fetching).toBe(true);
    await act(async () => { await p; });
    expect(mockPost).toHaveBeenCalledWith("/discovery/books/import-preview", {
      urls: ["https://books.example.invalid/show/1", "https://books.example.invalid/show/2"],
    });
    expect(result.current.progress).toBe("");
    expect(result.current.newRows.map((r) => r.book?.title)).toEqual(["Rootbound"]);
  });

  it("sends nothing when no line qualifies, and says so when the lookup fails", async () => {
    const { result } = renderHook(() => useImportPreview());
    await act(async () => { await result.current.fetchPreview("nothing here", label); });
    expect(mockPost).not.toHaveBeenCalled();
    mockPost.mockRejectedValue(new Error("502"));
    await act(async () => { await result.current.fetchPreview("https://books.example.invalid/x", label); });
    expect(result.current.progress).toBe("Error fetching books");
    expect(result.current.fetching).toBe(false);
  });

  it("adds books and marks the new rows with those titles as added", async () => {
    mockPost.mockResolvedValueOnce(preview).mockResolvedValueOnce({ added: 1, updated: 0 });
    const { result } = renderHook(() => useImportPreview());
    await act(async () => { await result.current.fetchPreview("https://books.example.invalid/1", label); });
    await act(async () => { await result.current.addBooks([{ title: "Rootbound", author_name: "Ada Quill" }]); });
    expect(mockPost).toHaveBeenLastCalledWith("/discovery/books/import-add", {
      books: [{ title: "Rootbound", author_name: "Ada Quill" }],
    });
    expect(result.current.addResult).toEqual({ added: 1, updated: 0 });
    expect(result.current.results?.map((r) => r.status)).toEqual(["added", "owned", "error"]);
    expect(result.current.newRows).toEqual([]);
  });

  it("a failed add reports an error and marks nothing", async () => {
    mockPost.mockResolvedValueOnce(preview).mockRejectedValueOnce(new Error("500"));
    const { result } = renderHook(() => useImportPreview());
    await act(async () => { await result.current.fetchPreview("https://books.example.invalid/1", label); });
    await act(async () => { await result.current.addBooks([{ title: "Rootbound" }]); });
    expect(result.current.addResult).toEqual({ added: 0, updated: 0, error: true });
    expect(result.current.results?.[0].status).toBe("new");
    expect(result.current.adding).toBe(false);
  });

  it("pickSeries puts one of a row's series options on its book", async () => {
    mockPost.mockResolvedValue(preview);
    const { result } = renderHook(() => useImportPreview());
    await act(async () => { await result.current.fetchPreview("https://books.example.invalid/1", label); });
    act(() => result.current.pickSeries(0, { name: "Botanicals", position: 1 }));
    expect(result.current.results?.[0].book).toMatchObject({ series_name: "Botanicals", series_index: 1 });
    expect(result.current.results?.[1].book).toEqual({ title: "Ninefold Tide" });
  });
});
