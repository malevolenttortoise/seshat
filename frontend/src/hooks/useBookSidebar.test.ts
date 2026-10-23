import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SIDEBAR_CLOSE_MS, useBookSidebar } from "./useBookSidebar";
import type { Book } from "../types";

const book = (id: number) => ({ id, title: `Book ${id}` }) as Book;

describe("useBookSidebar", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("opens a book, closes it after the animation", () => {
    const { result } = renderHook(() => useBookSidebar());
    act(() => result.current.openSb(book(1)));
    expect(result.current.sb?.id).toBe(1);
    act(() => result.current.closeSb());
    expect(result.current.sbClosing).toBe(true);
    expect(result.current.sb?.id).toBe(1);
    act(() => { vi.advanceTimersByTime(SIDEBAR_CLOSE_MS - 1); });
    expect(result.current.sb?.id).toBe(1);
    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current.sb).toBeNull();
    expect(result.current.sbClosing).toBe(false);
  });

  it("closing with nothing open does nothing", () => {
    const { result } = renderHook(() => useBookSidebar());
    act(() => result.current.closeSb());
    expect(result.current.sbClosing).toBe(false);
  });

  it("toggle: the same book closes, another book switches", () => {
    const { result } = renderHook(() => useBookSidebar());
    act(() => result.current.toggleSb(book(1)));
    expect(result.current.sb?.id).toBe(1);
    act(() => result.current.toggleSb(book(2)));
    expect(result.current.sb?.id).toBe(2);
    expect(result.current.sbClosing).toBe(false);
    act(() => result.current.toggleSb(book(2)));
    expect(result.current.sbClosing).toBe(true);
    act(() => { vi.advanceTimersByTime(SIDEBAR_CLOSE_MS); });
    expect(result.current.sb).toBeNull();
  });

  it("setSb opens without touching the closing flag (the phone pages)", () => {
    const { result } = renderHook(() => useBookSidebar());
    act(() => result.current.setSb(book(4)));
    expect(result.current.sb?.id).toBe(4);
  });
});
