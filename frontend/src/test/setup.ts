// Browser APIs jsdom doesn't provide, stubbed so pages can render.
//
// Each stub is inert: the render snapshots (src/test/pages) prove a
// page's markup and its load requests, not live updates, so an
// EventSource that never opens or an observer that never fires is the
// honest stand-in.
import { afterEach } from "vitest";

class InertEventSource {
  url: string;
  readyState = 0;
  constructor(url: string) {
    this.url = url;
  }
  addEventListener() {}
  removeEventListener() {}
  close() {}
}

class InertObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}

const g = globalThis as Record<string, unknown>;
g.EventSource ??= InertEventSource;
g.ResizeObserver ??= InertObserver;
g.IntersectionObserver ??= InertObserver;

if (typeof window !== "undefined") {
  window.matchMedia ??= ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
  Element.prototype.scrollIntoView ??= function scrollIntoView() {};
  window.scrollTo = (() => {}) as typeof window.scrollTo;
}

afterEach(() => {
  try {
    sessionStorage.clear();
    localStorage.clear();
  } catch {
    /* storage unavailable */
  }
});
