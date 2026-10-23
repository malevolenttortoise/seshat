import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsDraft } from "./useSettingsDraft";
import { api } from "../api";

vi.mock("../api", () => ({ api: { get: vi.fn(), patch: vi.fn() } }));
const mockGet = vi.mocked(api.get);
const mockPatch = vi.mocked(api.patch);

const creds = { items: [{ key: "qbit_password", label: "qBittorrent password", configured: true }] };

function serve(settings: Record<string, unknown> | Error) {
  mockGet.mockImplementation(async (url: string) => {
    if (url === "/v1/credentials") return creds;
    if (url === "/v1/settings") {
      if (settings instanceof Error) throw settings;
      return settings;
    }
    throw new Error(`no route ${url}`);
  });
}

async function loaded() {
  const hook = renderHook(() => useSettingsDraft());
  await waitFor(() => expect(hook.result.current.s).not.toBeNull());
  return hook;
}

describe("useSettingsDraft", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPatch.mockReset();
    serve({ dry_run: false, policy_use_wedge: true });
  });
  afterEach(() => { vi.useRealTimers(); });

  it("loads the settings and the credential list", async () => {
    const { result } = await loaded();
    expect(result.current.s).toEqual({ dry_run: false, policy_use_wedge: true });
    await waitFor(() => expect(result.current.creds).toEqual(creds.items));
    expect(result.current.msg).toBe("");
  });

  it("a failed load says so in the status line", async () => {
    serve(new Error("database is locked"));
    const { result } = renderHook(() => useSettingsDraft());
    await waitFor(() => expect(result.current.msg).toBe("Error: Error: database is locked"));
    expect(result.current.s).toBeNull();
  });

  it("upd edits one key of the draft", async () => {
    const { result } = await loaded();
    act(() => { result.current.upd("dry_run", true); });
    expect(result.current.s).toEqual({ dry_run: true, policy_use_wedge: true });
  });

  it("save PATCHes the whole draft, shows what the server kept, and says Saved! for 3s", async () => {
    const { result } = await loaded();
    act(() => { result.current.upd("dry_run", true); });
    mockPatch.mockResolvedValue({});
    serve({ dry_run: true, policy_use_wedge: true, added_by_server: 1 });
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    await act(async () => { await result.current.save(); });
    expect(mockPatch).toHaveBeenCalledWith("/v1/settings", { dry_run: true, policy_use_wedge: true });
    expect(result.current.s).toEqual({ dry_run: true, policy_use_wedge: true, added_by_server: 1 });
    expect(result.current.msg).toBe("Saved!");
    expect(result.current.saving).toBe(false);
    act(() => { vi.advanceTimersByTime(3000); });
    expect(result.current.msg).toBe("");
  });

  it("a failed save says Error saving and keeps the draft", async () => {
    const { result } = await loaded();
    act(() => { result.current.upd("dry_run", true); });
    mockPatch.mockRejectedValue(new Error("422"));
    await act(async () => { await result.current.save(); });
    expect(result.current.msg).toBe("Error saving");
    expect(result.current.saving).toBe(false);
    expect(result.current.s).toEqual({ dry_run: true, policy_use_wedge: true });
  });

  it("loadCreds re-reads the credential list", async () => {
    const { result } = await loaded();
    await waitFor(() => expect(result.current.creds).toHaveLength(1));
    const calls = () => mockGet.mock.calls.filter((c) => c[0] === "/v1/credentials").length;
    const before = calls();
    act(() => { result.current.loadCreds(); });
    await waitFor(() => expect(calls()).toBe(before + 1));
  });
});
