import { afterEach, describe, it, vi } from "vitest";
import UnifiedDashboard from "../../pages/UnifiedDashboard";
import { expectRendered, renderPage, resetPageEnv, type Viewport } from "../render";
import { dashboardRoutes, dashboardScanningRoutes } from "../fixtures/dashboard";

afterEach(resetPageEnv);

describe.each<Viewport>(["desktop", "phone"])("Dashboard (%s)", (viewport) => {
  it("both libraries, idle", async () => {
    const r = await renderPage(<UnifiedDashboard onNav={vi.fn()} />, { viewport, routes: dashboardRoutes });
    expectRendered(r, "ExampleMouse");
  });

  it("mid-scan", async () => {
    const r = await renderPage(<UnifiedDashboard onNav={vi.fn()} />, { viewport, routes: dashboardScanningRoutes });
    expectRendered(r, "ExampleMouse");
  });
});
