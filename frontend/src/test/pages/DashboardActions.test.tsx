// Wave 5b S15a (G144 / G148): a Dashboard command that fails says so.
// Both shells used to swallow every Sync / Scan / Cancel / Hygiene
// failure: the button stopped spinning and nothing else happened.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import UnifiedDashboard from "../../pages/UnifiedDashboard";
import { EVT } from "../../types";
import { renderPage, reply, resetPageEnv, type PageRender, type Viewport } from "../render";
import { dashboardRoutes } from "../fixtures/dashboard";
import { runningScans, scan } from "../fixtures/common";

let toasts: string[] = [];
const onToast = (e: Event) => {
  const d = (e as CustomEvent<{ kind: string; msg: string }>).detail;
  toasts.push(`${d.kind}: ${d.msg}`);
};
beforeEach(() => {
  toasts = [];
  window.addEventListener(EVT.Toast, onToast);
});
afterEach(() => {
  window.removeEventListener(EVT.Toast, onToast);
  resetPageEnv();
});

const startFails = {
  ...dashboardRoutes,
  "POST /discovery/sync/library": reply(503, { detail: "Calibre didn't answer" }),
  "POST /discovery/lookup?content_type=ebook": reply(409, { detail: "A source scan is already running" }),
  "POST /discovery/lookup?content_type=audiobook": reply(409, { detail: "A source scan is already running" }),
  "POST /discovery/mam/scan": reply(409, { detail: "MAM is switched off" }),
  "POST /discovery/hygiene/run": reply(500, { detail: "database is locked" }),
};

const cancelFails = {
  ...dashboardRoutes,
  "GET /discovery/scan-status": {
    scans: [
      ...runningScans.scans,
      scan("hygiene", { label: "Data Hygiene", running: true, status: "running", current: 2, total: 9 }),
    ],
  },
  "POST /discovery/lookup/cancel": reply(500, { detail: "no scan to cancel" }),
  "POST /discovery/mam/scan/cancel": reply(500, { detail: "no scan to cancel" }),
  "POST /discovery/hygiene/cancel": reply(500, { detail: "no chain to cancel" }),
};

const buttons = (r: PageRender, label: RegExp) =>
  r.getAllByRole("button").filter((b) => label.test((b.textContent ?? "").trim()));

async function press(r: PageRender, el: Element) {
  act(() => { fireEvent.click(el); });
  await r.settle();
}

describe.each<Viewport>(["desktop", "phone"])("Dashboard commands (%s)", (viewport) => {
  it("a failed start says why", async () => {
    const r = await renderPage(<UnifiedDashboard onNav={vi.fn()} />, { viewport, routes: startFails });
    const sync = viewport === "desktop" ? /Sync Calibre Library$/ : /^Sync Library$/;
    await press(r, buttons(r, sync)[0]);
    await press(r, buttons(r, /Scan Ebooks$/)[0]);
    await press(r, buttons(r, /Scan Audiobooks$/)[0]);
    await press(r, buttons(r, /MAM Scan$/)[0]);
    await press(r, buttons(r, /Data Hygiene$/)[0]);
    await press(r, buttons(r, /Run Hygiene$/)[0]);
    expect(r.unmatched).toEqual([]);
    expect(toasts).toEqual([
      "error: Couldn't start the library sync: Calibre didn't answer",
      "error: Couldn't start the ebook source scan: A source scan is already running",
      "error: Couldn't start the audiobook source scan: A source scan is already running",
      "error: Couldn't start the MAM scan: MAM is switched off",
      "error: Couldn't start Data Hygiene: database is locked",
    ]);
  });

  it("a failed cancel says why", async () => {
    const r = await renderPage(<UnifiedDashboard onNav={vi.fn()} />, { viewport, routes: cancelFails });
    const stops = () => buttons(r, /^Stop$/);
    expect(stops()).toHaveLength(3);
    await press(r, stops()[0]);
    await press(r, stops()[1]);
    await press(r, stops()[2]);
    expect(r.unmatched).toEqual([]);
    expect(r.requests).toEqual(expect.arrayContaining([
      "POST /discovery/lookup/cancel",
      "POST /discovery/mam/scan/cancel",
      "POST /discovery/hygiene/cancel",
    ]));
    expect(toasts).toEqual([
      "error: Couldn't cancel the source scan: no scan to cancel",
      "error: Couldn't cancel the MAM scan: no scan to cancel",
      "error: Couldn't cancel Data Hygiene: no chain to cancel",
    ]);
  });
});
