import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { parseAuthorId, useAuthorDetail } from "./useAuthorDetail";
import { loadAuthorDetailViaPerson, type AuthorDetail } from "../lib/authorDetail";

vi.mock("../lib/authorDetail", () => ({ loadAuthorDetailViaPerson: vi.fn() }));
const mockLoad = vi.mocked(loadAuthorDetailViaPerson);

const author = (name: string) => ({ id: 11, name }) as AuthorDetail;

describe("parseAuthorId", () => {
  it("splits a cross-library slug:id arg", () => {
    expect(parseAuthorId("calibre-library:619")).toEqual({ authorSlug: "calibre-library", authorIdNum: 619 });
  });
  it("takes a bare numeric string", () => {
    expect(parseAuthorId("42")).toEqual({ authorSlug: null, authorIdNum: 42 });
  });
  it("takes a number", () => {
    expect(parseAuthorId(7)).toEqual({ authorSlug: null, authorIdNum: 7 });
  });
  it("gives 0 for an id that doesn't parse", () => {
    expect(parseAuthorId("calibre-library:abc")).toEqual({ authorSlug: "calibre-library", authorIdNum: 0 });
    expect(parseAuthorId("abc")).toEqual({ authorSlug: null, authorIdNum: 0 });
  });
});

describe("useAuthorDetail", () => {
  beforeEach(() => {
    mockLoad.mockReset();
  });

  it("loads the author with the parsed id and slug on mount", async () => {
    mockLoad.mockResolvedValue(author("Ada Quill"));
    const { result } = renderHook(() => useAuthorDetail("calibre-library:11"));
    expect(result.current.ld).toBe(true);
    await waitFor(() => expect(result.current.ld).toBe(false));
    expect(mockLoad).toHaveBeenCalledTimes(1);
    expect(mockLoad.mock.calls[0].slice(0, 2)).toEqual([11, "calibre-library"]);
    expect(result.current.a?.name).toBe("Ada Quill");
    expect(result.current.loadErr).toBeNull();
    expect(result.current).toMatchObject({ authorIdNum: 11, authorSlug: "calibre-library" });
  });

  it("records the reason and stops loading when the load fails", async () => {
    mockLoad.mockRejectedValue(new Error("database is locked"));
    vi.spyOn(console, "error").mockImplementation(() => {});
    const { result } = renderHook(() => useAuthorDetail(11));
    await waitFor(() => expect(result.current.ld).toBe(false));
    expect(result.current.a).toBeNull();
    expect(result.current.loadErr).toBe("database is locked");
  });

  it("stays silent on an aborted load", async () => {
    mockLoad.mockRejectedValue(new DOMException("Aborted", "AbortError"));
    const { result } = renderHook(() => useAuthorDetail(11));
    await act(async () => { await Promise.resolve(); });
    expect(result.current.loadErr).toBeNull();
    expect(result.current.ld).toBe(true);
  });

  it("requests nothing for an id that doesn't parse", async () => {
    const { result } = renderHook(() => useAuthorDetail("abc"));
    await waitFor(() => expect(result.current.ld).toBe(false));
    expect(mockLoad).not.toHaveBeenCalled();
    expect(result.current.loadErr).toBe("no author id");
  });

  it("reloads on loadA() and when the author changes", async () => {
    mockLoad.mockResolvedValue(author("Ada Quill"));
    const { result, rerender } = renderHook(({ id }) => useAuthorDetail(id), { initialProps: { id: "11" } });
    await waitFor(() => expect(result.current.ld).toBe(false));

    await act(async () => { await result.current.loadA(); });
    expect(mockLoad).toHaveBeenCalledTimes(2);

    mockLoad.mockResolvedValue(author("Bram Okafor"));
    rerender({ id: "12" });
    await waitFor(() => expect(result.current.a?.name).toBe("Bram Okafor"));
    expect(mockLoad).toHaveBeenCalledTimes(3);
    expect(mockLoad.mock.calls[2][0]).toBe(12);
  });

  it("aborts the in-flight load on unmount", async () => {
    let signal: AbortSignal | undefined;
    mockLoad.mockImplementation((_id, _slug, s) => {
      signal = s;
      return new Promise(() => {});
    });
    const { unmount } = renderHook(() => useAuthorDetail(11));
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
  });
});
