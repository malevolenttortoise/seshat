import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useFilterSettings } from "./useFilterSettings";
import { api } from "../api";

vi.mock("../api", () => ({ api: { get: vi.fn(), patch: vi.fn() } }));
const mockGet = vi.mocked(api.get);
const mockPatch = vi.mocked(api.patch);

const enums = {
  categories: [
    { id: "63", name: "Fantasy", main_id: "14", main_name: "E-Books", normalized: "ebooks fantasy" },
    { id: "41", name: "Fantasy", main_id: "13", main_name: "AudioBooks", normalized: "audiobooks fantasy" },
    { id: "64", name: "Sci-Fi", main_id: "14", main_name: "E-Books", normalized: "ebooks sci-fi" },
  ],
  languages: ["English"],
  formats: ["epub", "m4b"],
};
const saved = { allowed_categories: ["ebooks fantasy"], allowed_formats: [], excluded_formats: ["pdf"] };

async function loaded() {
  const hook = renderHook(() => useFilterSettings());
  await waitFor(() => expect(hook.result.current.settings).not.toBeNull());
  return hook;
}

describe("useFilterSettings", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPatch.mockReset();
    mockGet.mockImplementation(async (url: string) => (url === "/v1/enums" ? enums : saved));
  });

  it("loads the enums and the settings, and groups categories by main name", async () => {
    const { result } = await loaded();
    expect(mockGet).toHaveBeenCalledWith("/v1/enums");
    expect(mockGet).toHaveBeenCalledWith("/v1/settings");
    expect(Object.keys(result.current.catGroups)).toEqual(["E-Books", "AudioBooks"]);
    expect(result.current.catGroups["E-Books"].map((c) => c.id)).toEqual(["63", "64"]);
  });

  it("reports a failed load", async () => {
    mockGet.mockRejectedValue(new Error("502"));
    const { result } = renderHook(() => useFilterSettings());
    await waitFor(() => expect(result.current.error).toBe("Error: 502"));
  });

  it("drafts a change, and drops it again when it equals the saved value", async () => {
    const { result } = await loaded();
    act(() => result.current.setField("allowed_categories", ["ebooks fantasy", "ebooks sci-fi"]));
    expect(result.current.draft).toEqual({ allowed_categories: ["ebooks fantasy", "ebooks sci-fi"] });
    expect([...result.current.allowedCats]).toEqual(["ebooks fantasy", "ebooks sci-fi"]);
    act(() => result.current.setField("allowed_categories", ["ebooks fantasy"]));
    expect(result.current.draft).toEqual({});
  });

  it("derives the sets and audiobook acceptance from saved + draft", async () => {
    const { result } = await loaded();
    expect(result.current.acceptAudiobooks).toBe(true); // empty allowed_formats = accept all
    expect([...result.current.excludedFormats]).toEqual(["pdf"]);
    act(() => result.current.setField("allowed_formats", ["ebooks"]));
    expect(result.current.acceptAudiobooks).toBe(false);
    act(() => result.current.setField("allowed_formats", ["ebooks", "audiobooks"]));
    expect(result.current.acceptAudiobooks).toBe(true);
  });

  it("saves the draft, reloads the settings and clears the draft", async () => {
    const { result } = await loaded();
    act(() => result.current.setField("excluded_formats", []));
    mockPatch.mockResolvedValue({ ok: true, updated: ["excluded_formats"], rejected: [] });
    await act(async () => { await result.current.save(); });
    expect(mockPatch).toHaveBeenCalledWith("/v1/settings", { excluded_formats: [] });
    expect(mockGet).toHaveBeenCalledTimes(3);
    expect(result.current.draft).toEqual({});
    expect(result.current.ok).toBe("Updated 1 filter(s).");
    expect(result.current.saving).toBe(false);
  });

  it("names the keys the server rejected; nothing to save sends nothing", async () => {
    const { result } = await loaded();
    await act(async () => { await result.current.save(); });
    expect(mockPatch).not.toHaveBeenCalled();
    act(() => result.current.setField("bogus", 1));
    mockPatch.mockResolvedValue({ ok: false, updated: [], rejected: ["bogus"] });
    await act(async () => { await result.current.save(); });
    expect(result.current.error).toBe("Rejected: bogus");
  });

  it("discard drops every change", async () => {
    const { result } = await loaded();
    act(() => result.current.setField("allowed_languages", ["german"]));
    act(() => result.current.discard());
    expect(result.current.draft).toEqual({});
  });
});
