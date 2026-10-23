import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useBulkSelection } from "./useBulkSelection";

const ids = (s: Set<number>) => [...s].sort((a, b) => a - b);

describe("useBulkSelection", () => {
  it("starts out of select mode with nothing selected", () => {
    const { result } = renderHook(() => useBulkSelection());
    expect(result.current.selMode).toBe(false);
    expect(result.current.sel.size).toBe(0);
  });

  it("toggles an id in and out", () => {
    const { result } = renderHook(() => useBulkSelection());
    act(() => result.current.toggle(3));
    expect(ids(result.current.sel)).toEqual([3]);
    act(() => result.current.toggle(5));
    expect(ids(result.current.sel)).toEqual([3, 5]);
    act(() => result.current.toggle(3));
    expect(ids(result.current.sel)).toEqual([5]);
  });

  it("selects and deselects many, keeping the rest", () => {
    const { result } = renderHook(() => useBulkSelection());
    act(() => result.current.toggle(1));
    act(() => result.current.selectMany([2, 3]));
    expect(ids(result.current.sel)).toEqual([1, 2, 3]);
    act(() => result.current.selectMany([3, 4]));
    expect(ids(result.current.sel)).toEqual([1, 2, 3, 4]);
    act(() => result.current.deselectMany([2, 9]));
    expect(ids(result.current.sel)).toEqual([1, 3, 4]);
  });

  it("selectOnly replaces the selection; clear empties it but keeps select mode", () => {
    const { result } = renderHook(() => useBulkSelection());
    act(() => result.current.setSelMode(true));
    act(() => result.current.selectMany([1, 2]));
    act(() => result.current.selectOnly([7, 8]));
    expect(ids(result.current.sel)).toEqual([7, 8]);
    act(() => result.current.clear());
    expect(result.current.sel.size).toBe(0);
    expect(result.current.selMode).toBe(true);
  });

  it("setSelMode takes an updater", () => {
    const { result } = renderHook(() => useBulkSelection());
    act(() => result.current.setSelMode((m) => !m));
    expect(result.current.selMode).toBe(true);
  });

  it("returns a new Set on each change (React sees it) and stable updaters", () => {
    const { result } = renderHook(() => useBulkSelection());
    const first = result.current.sel;
    const { toggle, selectMany, deselectMany, selectOnly, clear } = result.current;
    act(() => result.current.toggle(1));
    expect(result.current.sel).not.toBe(first);
    expect(first.size).toBe(0);
    expect(result.current.toggle).toBe(toggle);
    expect(result.current.selectMany).toBe(selectMany);
    expect(result.current.deselectMany).toBe(deselectMany);
    expect(result.current.selectOnly).toBe(selectOnly);
    expect(result.current.clear).toBe(clear);
  });
});
