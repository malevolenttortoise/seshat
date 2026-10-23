import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useReviewQueue } from "./useReviewQueue";
import { api } from "../api";

vi.mock("../api", () => ({ api: { get: vi.fn(), post: vi.fn() } }));
vi.mock("./useVisibleInterval", () => ({ useVisibleInterval: vi.fn() }));
const mockGet = vi.mocked(api.get);
const mockPost = vi.mocked(api.post);

type Item = { id: number; status: string };
const list = (...items: Item[]) => ({ items, pending_count: items.filter((i) => i.status === "pending").length });

async function mounted() {
  const hook = renderHook(() => useReviewQueue<Item>());
  await waitFor(() => expect(hook.result.current.items).not.toBeNull());
  return hook;
}

describe("useReviewQueue", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
    mockGet.mockResolvedValue(list({ id: 1, status: "pending" }, { id: 2, status: "import_failed" }, { id: 3, status: "pending" }));
  });

  it("loads the list on mount and splits import failures from pending", async () => {
    const { result } = await mounted();
    expect(mockGet).toHaveBeenCalledWith("/v1/review");
    expect(result.current.failed.map((i) => i.id)).toEqual([2]);
    expect(result.current.pending.map((i) => i.id)).toEqual([1, 3]);
  });

  it("approves with the metadata (or null), refreshes, and clears the row's busy flag", async () => {
    const { result } = await mounted();
    mockPost.mockResolvedValue({});
    await act(async () => { await result.current.approve(1); });
    expect(mockPost).toHaveBeenCalledWith("/v1/review/1/approve", { metadata: null });
    await act(async () => { await result.current.approve(3, { title: "X" }); });
    expect(mockPost).toHaveBeenLastCalledWith("/v1/review/3/approve", { metadata: { title: "X" } });
    expect(mockGet).toHaveBeenCalledTimes(3);
    expect(result.current.busyId).toBeNull();
  });

  it("is busy on the row while an action runs", async () => {
    const { result } = await mounted();
    let done!: () => void;
    mockPost.mockReturnValue(new Promise((r) => { done = () => r({}); }));
    let p!: Promise<void>;
    act(() => { p = result.current.reject(3); });
    expect(result.current.busyId).toBe(3);
    await act(async () => { done(); await p; });
    expect(mockPost).toHaveBeenCalledWith("/v1/review/3/reject", { note: "rejected via UI" });
    expect(result.current.busyId).toBeNull();
  });

  it("sends each shell's claim note", async () => {
    const { result } = await mounted();
    mockPost.mockResolvedValue({});
    await act(async () => { await result.current.claimForOwned(1, "calibre-library", 77, "claimed for owned via mobile UI"); });
    expect(mockPost).toHaveBeenCalledWith("/v1/review/1/claim-for-owned", {
      library_slug: "calibre-library", book_id: 77, note: "claimed for owned via mobile UI",
    });
  });

  // G155: an action's failure stays until the next action starts; a
  // failed list refresh clears itself on the next good one.
  it("a failed action's message survives refreshes and clears when the next action starts", async () => {
    const { result } = await mounted();
    mockPost.mockRejectedValueOnce(new Error("500"));
    await act(async () => { await result.current.saveEdits(1, {}); });
    expect(result.current.error).toBe("Error: 500");
    await act(async () => { await result.current.refresh(); });
    expect(result.current.error).toBe("Error: 500");
    mockPost.mockResolvedValueOnce({});
    await act(async () => { await result.current.approve(3); });
    expect(result.current.error).toBeNull();
  });

  it("a failed list refresh shows until a refresh succeeds", async () => {
    const { result } = await mounted();
    mockGet.mockRejectedValueOnce(new Error("database is locked"));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.error).toBe("Error: database is locked");
    await act(async () => { await result.current.refresh(); });
    expect(result.current.error).toBeNull();
  });

  it("an action's failure shows over a refresh failure, which shows again once the action's clears", async () => {
    const { result } = await mounted();
    mockPost.mockResolvedValueOnce({ ok: false, error: "CWA ingest folder not writable" });
    await act(async () => { await result.current.redrop(2); });
    mockGet.mockRejectedValueOnce(new Error("database is locked"));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.error).toBe("Re-drop failed: CWA ingest folder not writable");
    // The next action starts: its own message is gone; the list is still
    // unreadable (this action's refresh fails too).
    mockPost.mockResolvedValueOnce({});
    mockGet.mockRejectedValueOnce(new Error("database is locked"));
    await act(async () => { await result.current.approve(1); });
    expect(result.current.error).toBe("Error: database is locked");
  });

  it("re-enrich reports success / failure", async () => {
    const { result } = await mounted();
    mockPost.mockResolvedValueOnce({}).mockRejectedValueOnce(new Error("no match"));
    let ok: boolean | undefined;
    await act(async () => { ok = await result.current.reEnrich(1, { title: "A" }); });
    expect(ok).toBe(true);
    await act(async () => { ok = await result.current.reEnrich(1, { title: "A" }); });
    expect(ok).toBe(false);
    expect(result.current.error).toBe("Error: no match");
  });

  it("keeps a failed Re-drop's reason after the refresh", async () => {
    const { result } = await mounted();
    mockPost.mockResolvedValue({ ok: false, error: "CWA ingest folder not writable" });
    await act(async () => { await result.current.redrop(2); });
    expect(mockPost).toHaveBeenCalledWith("/v1/review/2/redrop");
    expect(result.current.error).toBe("Re-drop failed: CWA ingest folder not writable");
    mockPost.mockResolvedValue({ ok: false });
    await act(async () => { await result.current.markImported(2); });
    expect(mockPost).toHaveBeenLastCalledWith("/v1/review/2/mark-imported");
    expect(result.current.error).toBe("Couldn't mark as imported: unknown error");
  });

  it("bulk sends the note only when given, and keeps partial failures after the refresh", async () => {
    const { result } = await mounted();
    mockPost.mockResolvedValue({ processed: 2, failed: 0, errors: [] });
    await act(async () => { await result.current.bulk("approve"); });
    expect(mockPost).toHaveBeenCalledWith("/v1/review/bulk/approve", undefined);
    expect(result.current.error).toBeNull();

    mockPost.mockResolvedValue({ processed: 1, failed: 2, errors: ["a", "b", "c", "d"] });
    await act(async () => { await result.current.bulk("reject", "bulk rejected via UI"); });
    expect(mockPost).toHaveBeenLastCalledWith("/v1/review/bulk/reject", { note: "bulk rejected via UI" });
    expect(result.current.error).toBe("Rejected 1, 2 failed. First errors: a; b; c");
    expect(result.current.bulkBusy).toBe(false);
  });
});
