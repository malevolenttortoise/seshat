import { afterEach, describe, it, vi } from "vitest";
import { GlobalMetadataCacheStatusIcon } from "../../components/GlobalMetadataCacheStatusIcon";
import { expectRendered, renderPage, resetPageEnv } from "../render";
import {
  cacheStatusCooldownRoutes, cacheStatusRoutes, cacheStatusRoutineRoutes, cacheStatusStoppedRoutes,
} from "../fixtures/cacheStatus";

afterEach(resetPageEnv);

// The navbar's cloud icon (desktop nav only): worst-of-both health.
describe("Navbar cache-worker icon (desktop)", () => {
  it("both workers active", async () => {
    const r = await renderPage(<GlobalMetadataCacheStatusIcon onClick={vi.fn()} />, {
      viewport: "desktop",
      routes: cacheStatusRoutes,
    });
    expectRendered(r, "☁");
  });

  it("a short cooldown and two authors failed for good: still green (G269)", async () => {
    const r = await renderPage(<GlobalMetadataCacheStatusIcon onClick={vi.fn()} />, {
      viewport: "desktop",
      routes: cacheStatusRoutineRoutes,
    });
    expectRendered(r, "☁");
  });

  it("Goodreads stopped: red", async () => {
    const r = await renderPage(<GlobalMetadataCacheStatusIcon onClick={vi.fn()} />, {
      viewport: "desktop",
      routes: cacheStatusStoppedRoutes,
    });
    expectRendered(r, "☁");
  });

  it("Goodreads in cooldown", async () => {
    const r = await renderPage(<GlobalMetadataCacheStatusIcon onClick={vi.fn()} />, {
      viewport: "desktop",
      routes: cacheStatusCooldownRoutes,
    });
    expectRendered(r, "☁");
  });
});
