import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { parseAuthorId, useAuthorDetail } from "./useAuthorDetail";
import { loadAuthorDetailViaPerson, type AuthorDetail } from "../lib/authorDetail";

vi.mock("../lib/authorDetail", () => ({ loadAuthorDetailViaPerson: vi.fn() }));
vi.mock("../api", () => ({
  api: { isAbort: (e: unknown) => (e as { name?: string })?.name === "AbortError" },
}));
const mockLoad = vi.mocked(loadAuthorDetailViaPerson);

describe("parseAuthorId", () => {
  it("splits a slug:id compound id", () => {
    expect(parseAuthorId("cwa-library:5")).toEqual({ authorSlug: "cwa-library", authorIdNum: 5 });
  });
  it("treats a bare numeric string as id with null slug", () => {
    expect(parseAuthorId("7")).toEqual({ authorSlug: null, authorIdNum: 7 });
  });
  it("accepts a numeric id", () => {
    expect(parseAuthorId(9)).toEqual({ authorSlug: null, authorIdNum: 9 });
  });
  it("yields id 0 for an unparseable id", () => {
    expect(parseAuthorId("not-a-number")).toEqual({ authorSlug: null, authorIdNum: 0 });
  });
});

describe("useAuthorDetail", () => {
  beforeEach(() => mockLoad.mockReset());

  it("loads detail on mount with the parsed id + slug, then clears loading", async () => {
    const detail = { id: 5, name: "Sanderson" } as unknown as AuthorDetail;
    mockLoad.mockResolvedValue(detail);

    const { result } = renderHook(() => useAuthorDetail("cwa-library:5"));
    expect(result.current.ld).toBe(true); // starts in loading state

    await waitFor(() => expect(result.current.ld).toBe(false));
    expect(mockLoad).toHaveBeenCalledWith(5, "cwa-library", expect.any(AbortSignal));
    expect(result.current.a).toBe(detail);
    expect(result.current.authorIdNum).toBe(5);
    expect(result.current.authorSlug).toBe("cwa-library");
  });

  it("does not fetch when the id is invalid (0)", async () => {
    mockLoad.mockResolvedValue({} as AuthorDetail);
    renderHook(() => useAuthorDetail("not-a-number"));
    // give the mount effect a tick to (not) fire
    await new Promise((r) => setTimeout(r, 0));
    expect(mockLoad).not.toHaveBeenCalled();
  });
});
