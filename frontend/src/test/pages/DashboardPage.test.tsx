import { afterEach, describe, it, vi } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import UnifiedDashboard from "../../pages/UnifiedDashboard";
import { expectRendered, renderPage, reply, resetPageEnv, type Viewport } from "../render";
import { dashboardRoutes, dashboardScanningRoutes } from "../fixtures/dashboard";
import { idleScans, scan } from "../fixtures/common";
import { amazonStatus, goodreadsStatus } from "../fixtures/cacheStatus";

afterEach(resetPageEnv);

const railsAlertRoutes = {
  ...dashboardRoutes,
  "GET /v1/metadata-cache/amazon/status": {
    ...amazonStatus,
    cooldown: { blocked: true, remaining_s: 1800, reason: "captcha" },
    worker: { ...amazonStatus.worker, today_block_count: 2 },
  },
  "GET /v1/metadata-cache/goodreads/status": {
    ...goodreadsStatus,
    cache: { ...goodreadsStatus.cache, today_budget_exhaust_count: 4 },
  },
};

const hygieneRunningRoutes = {
  ...dashboardRoutes,
  "GET /discovery/scan-status": {
    scans: [
      ...idleScans.scans,
      scan("hygiene", { label: "Data Hygiene", running: true, status: "running", current: 2, total: 9 }),
    ],
  },
};

const allFailRoutes = Object.fromEntries(
  Object.keys(dashboardRoutes).map((k) => [k, reply(500, { detail: "database is locked" })]),
);

describe.each<Viewport>(["desktop", "phone"])("Dashboard (%s)", (viewport) => {
  it("both libraries, idle", async () => {
    const r = await renderPage(<UnifiedDashboard onNav={vi.fn()} />, { viewport, routes: dashboardRoutes });
    expectRendered(r, "ExampleMouse");
  });

  it("mid-scan", async () => {
    const r = await renderPage(<UnifiedDashboard onNav={vi.fn()} />, { viewport, routes: dashboardScanningRoutes });
    expectRendered(r, "ExampleMouse");
  });

  it("cache workers in cooldown / out of budget", async () => {
    const r = await renderPage(<UnifiedDashboard onNav={vi.fn()} />, { viewport, routes: railsAlertRoutes });
    expectRendered(r, "ExampleMouse");
  });

  it("Data Hygiene running", async () => {
    const r = await renderPage(<UnifiedDashboard onNav={vi.fn()} />, { viewport, routes: hygieneRunningRoutes });
    expectRendered(r, "ExampleMouse", "Data Hygiene");
  });

  it("Data Hygiene asks first", async () => {
    const r = await renderPage(<UnifiedDashboard onNav={vi.fn()} />, { viewport, routes: dashboardRoutes });
    const open = r.getAllByRole("button").find((b) => /Data Hygiene$/.test((b.textContent ?? "").trim()))!;
    act(() => { fireEvent.click(open); });
    await r.settle();
    expectRendered(r, "Run Hygiene");
  });

  it("every request fails", async () => {
    const r = await renderPage(<UnifiedDashboard onNav={vi.fn()} />, { viewport, routes: allFailRoutes });
    expectRendered(r, "Command Center");
  });
});
