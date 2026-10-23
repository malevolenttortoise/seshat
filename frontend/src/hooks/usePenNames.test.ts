import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePenNames } from "./usePenNames";
import { api } from "../api";

vi.mock("../api", () => ({ api: { get: vi.fn(), post: vi.fn(), del: vi.fn() } }));
const mockGet = vi.mocked(api.get);
const mockPost = vi.mocked(api.post);
const mockDel = vi.mocked(api.del);

const link = (id: number, alias: string) => ({
  id, canonical_author_id: 11, alias_author_id: 20 + id, canonical_name: "Ada Quill", alias_name: alias, link_type: "pen_name" as const,
});
const hit = (person_id: number, name: string) => ({
  person_id, canonical_name: name, display_name: name, normalized_name: name.toLowerCase(),
  library_slugs: [], author_ids_by_slug: {}, content_types: [],
});

describe("usePenNames", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
    mockDel.mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads the author's links", async () => {
    mockGet.mockResolvedValue({ links: [link(3, "A. Q. Thorne")] });
    const { result } = renderHook(() => usePenNames({ authorIdNum: 11, personId: 5 }));
    await waitFor(() => expect(result.current.penLinks).toHaveLength(1));
    expect(mockGet).toHaveBeenCalledWith("/discovery/authors/11/pen-names");
  });

  it("loads nothing without an author id", async () => {
    renderHook(() => usePenNames({ authorIdNum: 0, personId: null }));
    await act(async () => { await Promise.resolve(); });
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("searches people 300ms after 2+ characters, leaving this author out", async () => {
    vi.useFakeTimers();
    mockGet.mockImplementation(async (url: string) =>
      url.startsWith("/discovery/persons/search")
        ? { q: "", persons: [hit(5, "Ada Quill"), hit(9, "A. Q. Thorne")] }
        : { links: [] });
    const { result } = renderHook(() => usePenNames({ authorIdNum: 11, personId: 5 }));

    act(() => result.current.setPenQ("t"));
    await act(async () => { await vi.advanceTimersByTimeAsync(400); });
    expect(mockGet.mock.calls.some(([u]) => String(u).startsWith("/discovery/persons/search"))).toBe(false);

    act(() => result.current.setPenQ("a q&"));
    await act(async () => { await vi.advanceTimersByTimeAsync(299); });
    expect(mockGet.mock.calls.some(([u]) => String(u).startsWith("/discovery/persons/search"))).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(mockGet).toHaveBeenCalledWith("/discovery/persons/search?q=a%20q%26");
    expect(result.current.penResults.map((p) => p.person_id)).toEqual([9]);

    act(() => result.current.clearSearch());
    expect(result.current.penQ).toBe("");
    expect(result.current.penResults).toEqual([]);
  });

  it("links: posts, re-reads the list, runs onLinked, clears the search", async () => {
    mockGet.mockResolvedValueOnce({ links: [] }).mockResolvedValue({ links: [link(4, "B. Okafor")] });
    mockPost.mockResolvedValue({ ok: true });
    const order: string[] = [];
    const onLinked = vi.fn(async () => { order.push("onLinked"); });
    const { result } = renderHook(() => usePenNames({ authorIdNum: 11, personId: 5, onLinked }));
    act(() => result.current.setPenQ("bo"));

    await act(async () => { await result.current.link(9, "co_author"); });
    expect(mockPost).toHaveBeenCalledWith("/discovery/persons/link-pen-names", {
      canonical_person_id: 5, alias_person_id: 9, link_type: "co_author",
    });
    expect(mockGet).toHaveBeenLastCalledWith("/discovery/authors/11/pen-names");
    expect(onLinked).toHaveBeenCalledTimes(1);
    expect(result.current.penLinks.map((l) => l.id)).toEqual([4]);
    expect(result.current.penQ).toBe("");
    expect(result.current.penBusy).toBe(false);
  });

  it("unlinks by the routed DELETE, then re-reads the list", async () => {
    mockGet.mockResolvedValueOnce({ links: [link(3, "A. Q. Thorne")] }).mockResolvedValue({ links: [] });
    mockDel.mockResolvedValue({ ok: true });
    const { result } = renderHook(() => usePenNames({ authorIdNum: 11, personId: 5 }));
    await waitFor(() => expect(result.current.penLinks).toHaveLength(1));

    await act(async () => { await result.current.unlink(3); });
    expect(mockDel).toHaveBeenCalledWith("/discovery/authors/pen-name-link/3");
    expect(mockGet).toHaveBeenLastCalledWith("/discovery/authors/11/pen-names");
    expect(result.current.penLinks).toEqual([]);
  });

  it("is busy while a link or unlink runs, and throws a failure to the caller", async () => {
    mockGet.mockResolvedValue({ links: [] });
    let fail!: (e: Error) => void;
    mockDel.mockReturnValue(new Promise((_, rej) => { fail = rej; }));
    const { result } = renderHook(() => usePenNames({ authorIdNum: 11, personId: 5 }));

    let p!: Promise<void>;
    act(() => { p = result.current.unlink(3); });
    expect(result.current.penBusy).toBe(true);
    await act(async () => {
      fail(new Error("500"));
      await expect(p).rejects.toThrow("500");
    });
    expect(result.current.penBusy).toBe(false);
  });
});
