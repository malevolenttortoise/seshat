import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { rowNote, stepVerb, useMissingCleanup, type CleanupStatus } from "./useMissingCleanup";
import { api } from "../api";
import { useVisibleInterval } from "./useVisibleInterval";

vi.mock("../api", () => ({ api: { get: vi.fn(), post: vi.fn() } }));
vi.mock("./useVisibleInterval", () => ({ useVisibleInterval: vi.fn() }));
const mockGet = vi.mocked(api.get);
const mockPost = vi.mocked(api.post);
const mockInterval = vi.mocked(useVisibleInterval);

// No Array.prototype.at under the app's ES2020 lib.
const last = <T,>(xs: T[]): T => xs[xs.length - 1];

const NOW_S = 1_800_000_000;
const report = (id: string, agoS: number, mode: "preview" | "apply" = "preview", preview_id?: string) => ({
  id, mode, preview_id, started_at: NOW_S - agoS - 10, finished_at: NOW_S - agoS, errors: [],
  steps: [
    { key: "no_eligible_author", name: "No author you scan", action: "remove" as const, count: 3, sample: [], skipped_count: 0 },
    { key: "filtered", name: "Comics", action: "hide" as const, count: 4, sample: [], skipped_count: 1 },
  ],
});
const status = (over: Partial<CleanupStatus> = {}): CleanupStatus => ({
  running: false, mode: null, preview_ttl_s: 3600, last_preview: null, last_apply: null, ...over,
});

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockInterval.mockReset();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW_S * 1000);
});
afterEach(() => { vi.useRealTimers(); });

async function opened(st: CleanupStatus, onFail = vi.fn()) {
  mockGet.mockResolvedValue(st);
  const hook = renderHook(() => useMissingCleanup(onFail));
  act(() => { hook.result.current.show(); });
  await waitFor(() => expect(hook.result.current.status).not.toBeNull());
  return hook;
}

describe("useMissingCleanup", () => {
  it("reads nothing until opened, then the status", async () => {
    const hook = renderHook(() => useMissingCleanup(vi.fn()));
    expect(mockGet).not.toHaveBeenCalled();
    mockGet.mockResolvedValue(status());
    act(() => { hook.result.current.show(); });
    await waitFor(() => expect(hook.result.current.status).not.toBeNull());
    expect(mockGet).toHaveBeenCalledWith("/discovery/missing-cleanup/status");
    expect(hook.result.current.open).toBe(true);
    expect(hook.result.current.canApply).toBe(false);
  });

  it("a fresh preview can be applied, with its own id", async () => {
    const hook = await opened(status({ last_preview: report("p1", 300) }));
    expect(hook.result.current.canApply).toBe(true);
    expect(hook.result.current.changes).toBe(7);
    mockPost.mockResolvedValue({ status: "started" });
    await act(async () => { await hook.result.current.apply(); });
    expect(mockPost).toHaveBeenCalledWith("/discovery/missing-cleanup/apply", { preview_id: "p1" });
    // Reloaded after starting.
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["over an hour old", status({ last_preview: report("p1", 3601) })],
    ["already applied", status({ last_preview: report("p1", 300), last_apply: report("a1", 60, "apply", "p1") })],
    ["while a run goes on", status({ running: true, mode: "preview", last_preview: report("p1", 300) })],
  ])("a preview %s can't be applied", async (_why, st) => {
    const hook = await opened(st);
    expect(hook.result.current.canApply).toBe(false);
  });

  it("an Apply of an older preview isn't this preview's", async () => {
    const hook = await opened(status({
      last_preview: report("p2", 30), last_apply: report("a1", 600, "apply", "p1"),
    }));
    expect(hook.result.current.applied).toBeNull();
    expect(hook.result.current.canApply).toBe(true);
  });

  it("polls while open and running only", async () => {
    const hook = await opened(status({ running: true, mode: "apply" }));
    expect(last(mockInterval.mock.calls)[1]).toBe(2000);
    act(() => { hook.result.current.close(); });
    expect(last(mockInterval.mock.calls)[1]).toBe(0);
  });

  it("a failure says what failed", async () => {
    const onFail = vi.fn();
    mockGet.mockRejectedValue(new Error("database is locked"));
    const hook = renderHook(() => useMissingCleanup(onFail));
    act(() => { hook.result.current.show(); });
    await waitFor(() => expect(onFail).toHaveBeenCalled());
    expect(onFail).toHaveBeenCalledWith("Couldn't read the cleanup's status: database is locked");
    mockPost.mockRejectedValue(new Error("a source scan is running"));
    await act(async () => { await hook.result.current.preview(); });
    expect(onFail).toHaveBeenCalledWith("Couldn't start the preview: a source scan is running");
  });
});

describe("the dialog's words", () => {
  it("verbs follow the mode", () => {
    expect(stepVerb("remove", "preview")).toBe("Remove");
    expect(stepVerb("merge", "apply")).toBe("Added");
  });

  it("each step says why it takes a row", () => {
    const row = { library: "l", id: 1, title: "T" };
    expect(rowNote("filtered", { ...row, reason: "comic" })).toBe("Comic");
    expect(rowNote("filtered", { ...row, reason: "listing", stays_shown: true }))
      .toBe("Split / sampler / listing · stays shown: that filter is off");
    expect(rowNote("owned_copy", { ...row, into_title: "Truthwitch" })).toBe("into “Truthwitch”");
    expect(rowNote("listed_copy", { ...row, into_title: "Warship" })).toBe("into “Warship”");
    expect(rowNote("series_in_title", { ...row, detail: "Xenowealth #5" })).toBe("Xenowealth #5");
    expect(rowNote("cache_remerge", { ...row, hidden: true, hidden_reason: "box_set" })).toBe("hidden: Box set");
    expect(rowNote("cache_remerge", { ...row, hidden: false })).toBe("listed as missing");
    expect(rowNote("no_eligible_author", row)).toBe("");
  });
});
